import os
import sys
import sqlite3
import re
import json
import tempfile
from datetime import datetime, timezone
from flask import Flask, render_template, jsonify, request, send_file

app = Flask(__name__)

# ---- Write startup diagnostics to stderr so Zeabur logs capture them ----
print("[startup] Python", sys.version, file=sys.stderr)
print("[startup] CWD:", os.getcwd(), file=sys.stderr)
print("[startup] PORT env:", os.environ.get("PORT", "(not set)"), file=sys.stderr)
print("[startup] ZEABUR env:", os.environ.get("ZEABUR", "(not set)"), file=sys.stderr)

# ---- Database path: use /tmp on cloud platforms (detected via PORT env) ----
_is_cloud = bool(os.environ.get("PORT")) or bool(os.environ.get("ZEABUR"))
if os.environ.get("ZEABUR"):
    # Zeabur: try persistent /data first, fall back to /tmp
    _data_dir = "/data"
    os.makedirs(_data_dir, exist_ok=True)
    DATABASE = os.path.join(_data_dir, "japanese.db")
elif _is_cloud or os.environ.get("VERCEL"):
    DATABASE = "/tmp/japanese.db"
else:
    DATABASE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "japanese.db")

BOOK_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "book", "chapters")
print("[startup] DATABASE:", DATABASE, file=sys.stderr)
print("[startup] BOOK_DIR exists:", os.path.exists(BOOK_DIR), file=sys.stderr)
GRAMMAR_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static", "grammar")
LISTENING_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static", "listening")
IMAGES_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static", "images")
print("[startup] GRAMMAR_DIR exists:", os.path.exists(GRAMMAR_DIR), file=sys.stderr)

# ---------------------------------------------------------------------------
# Database helpers
# ---------------------------------------------------------------------------

def get_db():
    conn = sqlite3.connect(DATABASE)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_db():
    """Create tables if they don't already exist."""
    conn = get_db()
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS users (
            id       INTEGER PRIMARY KEY AUTOINCREMENT,
            username TEXT    UNIQUE NOT NULL,
            created_at      TEXT    NOT NULL DEFAULT (datetime('now'))
        );

        CREATE TABLE IF NOT EXISTS progress (
            id             INTEGER PRIMARY KEY AUTOINCREMENT,
            username       TEXT    NOT NULL,
            chapter        INTEGER NOT NULL,
            sentence_index INTEGER NOT NULL,
            updated_at     TEXT    NOT NULL DEFAULT (datetime('now')),
            FOREIGN KEY (username) REFERENCES users(username),
            UNIQUE(username, chapter, sentence_index)
        );

        CREATE TABLE IF NOT EXISTS grammar_progress (
            id               INTEGER PRIMARY KEY AUTOINCREMENT,
            username         TEXT NOT NULL,
            grammar_key      TEXT NOT NULL,
            status           TEXT NOT NULL DEFAULT 'unmastered',
            trigger_condition TEXT DEFAULT '',
            personal_example  TEXT DEFAULT '',
            error_record      TEXT DEFAULT '',
            updated_at       TEXT NOT NULL DEFAULT (datetime('now')),
            FOREIGN KEY (username) REFERENCES users(username),
            UNIQUE(username, grammar_key)
        );

        CREATE TABLE IF NOT EXISTS listening_progress (
            id                INTEGER PRIMARY KEY AUTOINCREMENT,
            username          TEXT NOT NULL,
            listening_id      INTEGER NOT NULL,
            score             INTEGER DEFAULT 0,
            particles_correct INTEGER DEFAULT 0,
            particles_total   INTEGER DEFAULT 0,
            grammar_correct   INTEGER DEFAULT 0,
            grammar_total     INTEGER DEFAULT 0,
            nouns_correct     INTEGER DEFAULT 0,
            nouns_total       INTEGER DEFAULT 0,
            completed_at      TEXT NOT NULL DEFAULT (datetime('now')),
            FOREIGN KEY (username) REFERENCES users(username)
        );
    """)
    conn.commit()
    conn.close()


# ---------------------------------------------------------------------------
# User helpers
# ---------------------------------------------------------------------------

def ensure_user(username):
    """Insert a user if they don't exist; return True if newly created."""
    conn = get_db()
    cur = conn.execute("SELECT id FROM users WHERE username = ?", (username,))
    exists = cur.fetchone()
    if not exists:
        conn.execute("INSERT INTO users (username) VALUES (?)", (username,))
        conn.commit()
        conn.close()
        return True
    conn.close()
    return False


# ---------------------------------------------------------------------------
# Progress helpers
# ---------------------------------------------------------------------------

def get_user_progress(username):
    """Return a dict keyed by 'ch:sentence_index' -> updated_at for one user."""
    conn = get_db()
    rows = conn.execute(
        "SELECT chapter, sentence_index, updated_at FROM progress WHERE username = ?",
        (username,),
    ).fetchall()
    conn.close()
    return {f"{r['chapter']}:{r['sentence_index']}": r["updated_at"] for r in rows}


def update_user_progress(username, chapter, sentence_index):
    """Upsert a progress row and return the new updated_at timestamp."""
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    conn = get_db()
    conn.execute(
        """INSERT INTO progress (username, chapter, sentence_index, updated_at)
           VALUES (?, ?, ?, ?)
           ON CONFLICT(username, chapter, sentence_index)
           DO UPDATE SET updated_at = excluded.updated_at""",
        (username, chapter, sentence_index, now),
    )
    conn.commit()
    conn.close()
    return now


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _chapter_number(filename):
    """Extract chapter integer from a filename like 'chapter1.txt' -> 1."""
    m = re.search(r"chapter(\d+)\.txt", filename)
    return int(m.group(1)) if m else 0


# ---------------------------------------------------------------------------
# Chapter cache
# ---------------------------------------------------------------------------

_chapter_cache = {}


def _load_chapter(chapter_num):
    """Load sentences for a chapter, using in-memory cache.  Returns None if file missing."""
    if chapter_num in _chapter_cache:
        return _chapter_cache[chapter_num]

    path = os.path.join(BOOK_DIR, f"chapter{chapter_num}.txt")
    if not os.path.exists(path):
        return None

    with open(path, "r", encoding="utf-8") as f:
        sentences = []
        current_unit = ""
        for line in f:
            line = line.strip()
            if not line:
                continue
            if line.startswith("## "):
                current_unit = line[3:].strip()
                continue
            if '——' in line:
                jp, rest = line.split('——', 1)
                jp = jp.strip()
                zh = rest.strip()
                reading = ""
                if "|" in jp:
                    jp, reading = jp.split("|", 1)
                    jp = jp.strip()
                    reading = reading.strip()
                sentences.append({
                    "jp": jp,
                    "reading": reading,
                    "zh": zh,
                    "unit": current_unit
                })
            else:
                sentences.append({
                    "jp": line,
                    "reading": "",
                    "zh": "",
                    "unit": current_unit
                })

    units = []
    seen = {}
    for i, s in enumerate(sentences):
        u = s["unit"]
        if u and u not in seen:
            seen[u] = i
            units.append({"name": u, "start": i})
    for i, u in enumerate(units):
        u["end"] = units[i + 1]["start"] if i + 1 < len(units) else len(sentences)

    _chapter_cache[chapter_num] = {"sentences": sentences, "units": units}
    return _chapter_cache[chapter_num]


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@app.route("/")
def index():
    template_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "templates", "index.html")
    with open(template_path, "rb") as f:
        html_bytes = f.read()
    from flask import Response
    return Response(html_bytes, mimetype="text/html; charset=utf-8")

@app.route("/api/chapters")
def get_chapters():
    chapters = []
    if os.path.exists(BOOK_DIR):
        for filename in sorted(os.listdir(BOOK_DIR)):
            if filename.endswith(".txt"):
                path = os.path.join(BOOK_DIR, filename)
                with open(path, "r", encoding="utf-8") as f:
                    lines = [line.strip() for line in f if line.strip()]
                chapters.append({
                    "name": filename,
                    "number": _chapter_number(filename),
                    "lines": lines,
                })
    return jsonify(chapters)


@app.route("/api/chapter/<int:chapter_num>")
def get_chapter(chapter_num):
    data = _load_chapter(chapter_num)
    if data is None:
        return jsonify({"error": "chapter not found"}), 404
    return jsonify({
        "chapter": chapter_num,
        "sentences": data["sentences"],
        "units": data["units"]
    })


@app.route("/api/progress", methods=["GET"])
def get_progress():
    username = request.args.get("username", "default")
    ensure_user(username)
    conn = get_db()
    row = conn.execute(
        "SELECT chapter, sentence_index FROM progress WHERE username = ? ORDER BY updated_at DESC LIMIT 1",
        (username,),
    ).fetchone()
    conn.close()
    if row is None:
        return jsonify({"chapter": 1, "sentence_index": 0})
    return jsonify({"chapter": row["chapter"], "sentence_index": row["sentence_index"]})


@app.route("/api/progress", methods=["POST"])
def save_progress():
    payload = request.get_json()
    username = payload.get("username", "default")
    chapter = payload.get("chapter")
    sentence_index = payload.get("sentence_index")

    if chapter is None or sentence_index is None:
        return jsonify({"error": "chapter and sentence_index are required"}), 400

    ensure_user(username)
    update_user_progress(username, int(chapter), int(sentence_index))
    return jsonify({"status": "ok"})


@app.route("/api/tts")
def get_tts():
    text = request.args.get("text", "")
    if not text:
        return jsonify({"error": "text required"}), 400
    try:
        import edge_tts, asyncio

        async def generate():
            tmp = tempfile.NamedTemporaryFile(suffix=".mp3", delete=False)
            tmp.close()
            comm = edge_tts.Communicate(text, "ja-JP-NanamiNeural")
            await comm.save(tmp.name)
            return tmp.name

        path = asyncio.run(generate())
        return send_file(path, mimetype="audio/mpeg", as_attachment=False,
                        download_name="audio.mp3")
    except Exception:
        return jsonify({"error": "tts failed"}), 500


@app.route("/api/audio")
def get_audio():
    chapter = request.args.get("chapter", type=int)
    sentence_index = request.args.get("sentence_index", type=int)
    if chapter is None or sentence_index is None:
        return jsonify({"error": "chapter and sentence_index are required"}), 400
    filename = f"chapter{chapter}_sent{sentence_index}.mp3"
    return jsonify({"url": "/static/audio/" + filename})


@app.route("/api/grammar")
def get_grammar():
    keyword = request.args.get("keyword", "")
    results = []
    if os.path.exists(GRAMMAR_DIR):
        for filename in sorted(os.listdir(GRAMMAR_DIR)):
            if filename.endswith(".json"):
                path = os.path.join(GRAMMAR_DIR, filename)
                with open(path, "r", encoding="utf-8") as f:
                    entry = json.load(f)
                key = filename.replace(".json", "")
                entry_with_key = dict(entry)
                entry_with_key["_key"] = key
                if not keyword:
                    results.append(entry_with_key)
                else:
                    title = entry.get("title", "")
                    explanation = entry.get("explanation", "")
                    if keyword in title or keyword in explanation:
                        results.append(entry_with_key)
    return jsonify(results)


# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# Image route (NEW)
# ---------------------------------------------------------------------------

@app.route("/api/image")
def get_image():
    chapter = request.args.get("chapter", type=int)
    sentence_index = request.args.get("sentence_index", type=int)
    if chapter is None or sentence_index is None:
        return jsonify({"url": ""})
    filename = f"chapter{chapter}_sent{sentence_index}.jpg"
    path = os.path.join(IMAGES_DIR, filename)
    if os.path.exists(path):
        return jsonify({"url": "/static/images/" + filename})
    return jsonify({"url": ""})




# Grammar list / progress routes (REWRITTEN)
# ---------------------------------------------------------------------------

@app.route("/api/grammar-list")
def get_grammar_list():
    """Return all grammar days with their grammar point summaries."""
    results = []
    if os.path.exists(GRAMMAR_DIR):
        for filename in sorted(os.listdir(GRAMMAR_DIR)):
            if filename.endswith(".json"):
                key = filename.replace(".json", "")
                path = os.path.join(GRAMMAR_DIR, filename)
                with open(path, "r", encoding="utf-8") as f:
                    entry = json.load(f)

                if "grammar_points" in entry:
                    # New format: per-day file with grammar_points array
                    gps = []
                    for gi, gp in enumerate(entry["grammar_points"]):
                        gps.append({
                            "key": f"{key}_{gi}",
                            "title": gp.get("title", ""),
                            "conjugation": gp.get("conjugation", ""),
                            "usage_preview": (gp.get("usage", "") or gp.get("explanation", ""))[:80],
                            "example_count": len(gp.get("examples", []))
                        })
                    results.append({
                        "key": key,
                        "type": "day",
                        "week": entry.get("week"),
                        "day": entry.get("day"),
                        "day_title": entry.get("day_title", ""),
                        "week_title": entry.get("week_title", ""),
                        "grammar_points": gps
                    })
                else:
                    # Old format: single grammar entry
                    results.append({
                        "key": key,
                        "type": "single",
                        "title": entry.get("title", ""),
                        "explanation": (entry.get("explanation", "") or entry.get("usage", ""))[:120],
                        "has_quiz": bool(entry.get("quiz_items")),
                        "week": entry.get("week"),
                        "day": entry.get("day"),
                        "day_title": entry.get("day_title", ""),
                        "grammar_points": [{
                            "key": key,
                            "title": entry.get("title", ""),
                            "usage_preview": (entry.get("explanation", "") or entry.get("usage", ""))[:80],
                            "example_count": len(entry.get("examples", []))
                        }]
                    })
    return jsonify(results)


@app.route("/api/grammar-day")
def get_grammar_day():
    """Return full content for a specific grammar day."""
    week = request.args.get("week", type=int)
    day = request.args.get("day", type=int)
    key = request.args.get("key", "")

    if not key and (week is None or day is None):
        return jsonify({"error": "week+day or key required"}), 400

    results = []
    if os.path.exists(GRAMMAR_DIR):
        for filename in sorted(os.listdir(GRAMMAR_DIR)):
            if not filename.endswith(".json"):
                continue
            fkey = filename.replace(".json", "")
            path = os.path.join(GRAMMAR_DIR, filename)
            with open(path, "r", encoding="utf-8") as f:
                entry = json.load(f)

            # Match by key or by week+day
            match = False
            if key and fkey == key:
                match = True
            elif week is not None and day is not None:
                if entry.get("week") == week and entry.get("day") == day:
                    match = True

            if not match:
                continue

            if "grammar_points" in entry:
                return jsonify({
                    "key": fkey,
                    "type": "day",
                    "week": entry.get("week"),
                    "day": entry.get("day"),
                    "day_title": entry.get("day_title", ""),
                    "week_title": entry.get("week_title", ""),
                    "grammar_points": entry["grammar_points"]
                })
            else:
                return jsonify({
                    "key": fkey,
                    "type": "single",
                    "title": entry.get("title", ""),
                    "conjugation": entry.get("conjugation", ""),
                    "explanation": entry.get("explanation", ""),
                    "examples": entry.get("examples", []),
                    "quiz_items": entry.get("quiz_items", []),
                    "week": entry.get("week"),
                    "day": entry.get("day"),
                    "day_title": entry.get("day_title", ""),
                    "grammar_points": [{
                        "title": entry.get("title", ""),
                        "usage": entry.get("explanation", ""),
                        "conjugation": entry.get("conjugation", ""),
                        "examples": entry.get("examples", []),
                        "quiz_items": entry.get("quiz_items", [])
                    }]
                })

    return jsonify({"error": "not found"}), 404


@app.route("/api/grammar-progress", methods=["GET"])
def get_grammar_progress():
    username = request.args.get("username", "default")
    ensure_user(username)
    conn = get_db()
    rows = conn.execute(
        "SELECT grammar_key, status, trigger_condition, personal_example, error_record, updated_at FROM grammar_progress WHERE username = ?",
        (username,),
    ).fetchall()
    conn.close()
    return jsonify([dict(r) for r in rows])


@app.route("/api/grammar-progress", methods=["POST"])
def save_grammar_progress():
    payload = request.get_json()
    username = payload.get("username", "default")
    grammar_key = payload.get("grammar_key")
    if not grammar_key:
        return jsonify({"error": "grammar_key is required"}), 400

    conn = get_db()
    # Ensure user exists using same connection
    cur = conn.execute("SELECT id FROM users WHERE username = ?", (username,))
    if not cur.fetchone():
        conn.execute("INSERT INTO users (username) VALUES (?)", (username,))
        conn.commit()

    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")

    fields = []
    field_vals = []
    for col in ["status", "trigger_condition", "personal_example", "error_record"]:
        if col in payload:
            fields.append(col)
            field_vals.append(payload[col])

    if fields:
        set_clause = ", ".join(f"{f} = ?" for f in fields)
        # Order: username, grammar_key, field_vals..., now, field_vals... (for UPDATE)
        upsert_values = [username, grammar_key] + field_vals + [now] + field_vals
        conn.execute(
            f"INSERT INTO grammar_progress (username, grammar_key, {', '.join(fields)}, updated_at) "
            f"VALUES (?, ?, {', '.join('?' for _ in fields)}, ?) "
            f"ON CONFLICT(username, grammar_key) "
            f"DO UPDATE SET {set_clause}, updated_at = excluded.updated_at",
            upsert_values,
        )
        conn.commit()

    conn.close()
    return jsonify({"status": "ok", "updated_at": now})


# ---------------------------------------------------------------------------
# Grammar quiz generation (NEW)
# ---------------------------------------------------------------------------

@app.route("/api/grammar-quiz", methods=["POST"])
def generate_grammar_quiz():
    """Generate 2 quiz items from a grammar point: one from example, one from rules."""
    payload = request.get_json()
    if not payload:
        return jsonify({"error": "missing payload"}), 400

    gp = payload  # {title, usage, conjugation, examples: [{jp, reading, cn}, ...]}

    title = gp.get("title", "")
    conjugation = gp.get("conjugation", "")
    usage = gp.get("usage", "")
    examples = gp.get("examples", [])

    quiz_items = []

    # --- Question 1: from a random example sentence ---
    if examples:
        import random as _random
        ex = _random.choice(examples)
        jp = ex.get("jp", "")
        cn = ex.get("cn", "")
        reading = ex.get("reading", "")

        # Determine question type (random: 50% fill, 50% choice)
        qtype = "fill" if _random.random() < 0.5 else "choice"

        if qtype == "fill":
            # Find the grammar-related word to blank (longest word with kanji/kana)
            words = jp.replace("「", "").replace("」", "").replace("（", " ").replace("）", " ").replace("、", " ").replace("。", "").split()
            if words:
                # Pick a meaningful word (longer than 1 char, prefer kanji)
                candidates = [w for w in words if len(w) >= 2]
                if not candidates:
                    candidates = [w for w in words if len(w) >= 1]
                blank_word = _random.choice(candidates) if candidates else words[-1]
                jp_blanked = jp.replace(blank_word, "___", 1)
                quiz_items.append({
                    "type": "fill",
                    "jp_blanked": jp_blanked,
                    "cn_hint": cn,
                    "answer": blank_word,
                    "source": "example",
                    "original_jp": jp,
                    "reading": reading
                })
            else:
                quiz_items.append({
                    "type": "fill",
                    "jp_blanked": jp[:len(jp)//2] + "___",
                    "cn_hint": cn,
                    "answer": jp[len(jp)//2:],
                    "source": "example",
                    "original_jp": jp,
                    "reading": reading
                })
        else:
            # Choice: the blanked word is the answer, generate distractors
            words = jp.replace("「", "").replace("」", "").replace("（", " ").replace("）", " ").replace("、", " ").replace("。", "").split()
            candidates = [w for w in words if len(w) >= 2]
            if not candidates:
                candidates = [w for w in words if len(w) >= 1]
            answer = _random.choice(candidates) if candidates else words[-1] if words else ""
            jp_blanked = jp.replace(answer, "___", 1) if answer else jp

            # Collect distractors from all examples
            all_words = set()
            for e in examples:
                ejp = e.get("jp", "")
                for w in ejp.replace("「", "").replace("」", "").replace("（", " ").replace("）", " ").replace("、", " ").replace("。", "").split():
                    if len(w) >= 1:
                        all_words.add(w)
            all_words.discard(answer)
            distractors = _random.sample(list(all_words), min(3, len(all_words)))
            while len(distractors) < 3:
                distractors.append("___" + str(len(distractors) + 1))

            quiz_items.append({
                "type": "choice",
                "jp_blanked": jp_blanked,
                "cn_hint": cn,
                "answer": answer,
                "options": [answer] + distractors,
                "source": "example",
                "original_jp": jp,
                "reading": reading
            })

    # --- Question 2: 翻译题（中文→日语） ---
    # 根据语法知识点生成中文语句，让用户翻译成日语，让用户翻译成日语
    import random as _random2

    cn_text, jp_answer = _generate_translation_pair(title, usage, conjugation)
    q2_reading = jp_answer

    quiz_items.append({
        "type": "translate",
        "cn_text": cn_text,
        "cn_hint": "请使用语法\u300c{0}\u300d将以下中文翻译成日语：".format(title),
        "answer": jp_answer,
        "source": "grammar",
        "original_jp": jp_answer,
        "reading": q2_reading
    })

    return jsonify({"quiz_items": quiz_items})



def _generate_distractors_for_pattern(correct_pattern, examples):
    """Generate plausible-looking distractors for grammar patterns."""
    import random as _random
    # Common grammar patterns that look similar
    common_patterns = [
        "〜ていく", "〜てくる", "〜てみる", "〜てしまう", "〜てある",
        "〜ことにする", "〜ことになる", "〜はずだ", "〜わけだ",
        "〜にとって", "〜のわりには", "〜くせに", "〜おかげで", "〜せいで",
        "〜かわりに", "〜にかわって", "〜くらい", "〜ほど", "〜ば〜ほど",
        "〜ことはない", "〜ということだ", "〜ことだ", "〜しかない",
        "〜んじゃない", "〜わけにはいかない", "〜にちがいない",
        "〜ようになる", "〜ようにする", "〜つもりだ", "〜ところだ"
    ]
    # Remove the correct pattern from candidates
    clean_correct = correct_pattern.replace(" ", "").replace("・", "/").split("/")[0]
    candidates = [p for p in common_patterns if p != clean_correct and p not in correct_pattern]
    _random.shuffle(candidates)
    return [correct_pattern] + candidates[:3]


def _generate_sentence_distractors(correct_word, sentence, examples):
    """Generate distractors for a word blanked from a sentence."""
    import random as _random
    # Collect candidate words from all examples
    all_words = set()
    for e in examples:
        ejp = e.get("jp", "")
        for w in ejp.replace("「", "").replace("」", "").replace("（", " ").replace("）", " ").replace("、", " ").replace("。", "").split():
            if len(w) >= 1 and w != correct_word:
                all_words.add(w)
    # Also add words from the sentence itself
    for w in sentence.replace("「", "").replace("」", "").replace("（", " ").replace("）", " ").replace("、", " ").replace("。", "").split():
        if len(w) >= 1 and w != correct_word:
            all_words.add(w)
    all_words.discard(correct_word)
    dist = _random.sample(list(all_words), min(3, len(all_words)))
    while len(dist) < 3:
        dist.append("＿" + str(len(dist) + 1))
    return [correct_word] + dist


def _build_grammar_sentence(pattern, conj):
    """Generate a new Japanese sentence demonstrating the grammar pattern."""
    import random as _random
    # Strip decorative prefix for natural insertion
    clean_pat = pattern.lstrip("~").lstrip("〜")

    # Word pools for slot filling
    nouns = ["私", "彼", "彼女", "学生", "先生", "日本人", "外国人", "子供", "大人", "友達", "家族", "会社", "学校", "日本", "東京", "料理", "宿題", "仕事", "天気", "時間", "お金", "健康", "旅行", "映画", "音楽", "日本語", "英語", "言葉", "約束", "生活"]
    verbs_dict = ["食べる", "飲む", "行く", "来る", "見る", "聞く", "読む", "書く", "話す", "作る", "使う", "買う", "売る", "歩く", "走る", "勉強する", "練習する", "運動する", "掃除する", "料理する", "旅行する", "考える", "思う", "言う", "教える", "習う", "待つ", "座る", "立つ"]
    adj_i = ["高い", "安い", "大きい", "小さい", "美味しい", "楽しい", "忙しい", "優しい", "厳しい", "嬉しい", "悲しい", "正しい", "新しい", "古い", "良い", "悪い", "面白い", "難しい", "易しい", "若い"]
    adj_na = ["静か", "賑やか", "綺麗", "有名", "便利", "元気", "親切", "簡単", "複雑", "重要", "必要", "安全", "危険", "失礼", "丁寧", "真面目", "自由", "豊か"]

    # Conjugation type detection and sentence generation
    if "名词" in conj or "名詞" in conj:
        n = _random.choice(nouns)
        # Pattern like: N + にとって → Nにとって
        tmpls = [
            n + clean_pat + "、これはとても大事なことです。",
            n + clean_pat + "、毎日が勉強になります。",
            n + clean_pat + "、この問題は難しいです。",
            n + clean_pat + "、何が一番必要ですか。",
        ]
        return _random.choice(tmpls)

    if "动词" in conj or "動詞" in conj:
        v = _random.choice(verbs_dict)
        tmpls = [
            "毎日" + v + clean_pat + "、上手になりました。",
            "ちゃんと" + v + clean_pat + "、後で確認します。",
        ]
        return _random.choice(tmpls)

    if "形容词" in conj or "形容詞" in conj:
        a = _random.choice(adj_i)
        tmpls = [
            "この店は" + a + clean_pat + "、よく来ます。",
            "あの人は" + a + clean_pat + "、いつも頑張っています。",
        ]
        return _random.choice(tmpls)

    if "形容动词" in conj or "形容動詞" in conj:
        a = _random.choice(adj_na)
        tmpls = [
            "この街は" + a + clean_pat + "、住みやすいです。",
            "彼は" + a + clean_pat + "、みんなに好かれています。",
        ]
        return _random.choice(tmpls)

    # Generic: construct from pattern itself
    n = _random.choice(nouns)
    return n + clean_pat + "、毎日の生活に欠かせません。"






def _generate_translation_pair(title, usage, conjugation):
    """根据语法知识点生成中文语句及其日语翻译。
    使用预置的自然句式库，每个语法点多组候选，随机选取。
    返回 (cn_text, jp_answer) 元组。"""
    import random as _random
    import re as _re

    # 清理标题
    def _clean(t):
        t = t.lstrip("~\u300c").rstrip("\u300d")
        if "/" in t:
            t = _random.choice([x.strip() for x in t.split("/")])
        t = _re.sub(r'[（(][^）)]*[）)]', '', t).strip()
        return t.lstrip("~\u300c").rstrip("\u300d")

    ct = _clean(title)

    # ============================================================
    # 每个语法点的自然句对库：(中文, 日语)
    # ============================================================

    PAIRS = {
        "にとって": [
            ("对我来说，家人比什么都重要。", "私にとって、家族が何より大切です。"),
            ("对日本人来说，米饭是不可缺少的食物。", "日本人にとって、ごはんは欠かせない食べ物です。"),
            ("对小孩来说，这个药太苦了。", "子供にとって、この薬は苦すぎます。"),
            ("对留学生来说，找房子很不容易。", "留学生にとって、部屋を探すのは大変です。"),
        ],
        "のわりには": [
            ("他个子不高，跑得却很快。", "彼は背が低いわりには、足が速い。"),
            ("这家店价格便宜，味道却很好。", "この店は値段が安いわりには、味がいい。"),
            ("她看起来很年轻，实际上已经是三个孩子的妈妈了。", "彼女は若く見えるわりには、もう三人の子の母親だ。"),
        ],
        "くせに": [
            ("明明知道答案，却什么都不说。", "答えを知っているくせに、何も言わない。"),
            ("明明不会喝酒，却硬要喝。", "お酒が飲めないくせに、無理して飲んでいる。"),
            ("明明是个大人了，还这么爱哭。", "大人のくせに、すぐ泣くんだから。"),
        ],
        "なんか": [
            ("纳豆这种东西，我一点都不喜欢。", "納豆なんか、全然好きじゃない。"),
            ("我可不做什么晚饭。", "夕飯なんか作らないよ。"),
            ("他那种人的话，我才不信呢。", "あいつの言うことなんか、信じない。"),
        ],
        "なんて": [
            ("我根本开不了日语演讲。", "日本語でスピーチなんてできません。"),
            ("我才不化妆呢。", "お化粧なんてしません。"),
        ],
        "など": [
            ("这种事，没必要特意去问。", "こんなことなど、わざわざ聞く必要はない。"),
        ],
        "おかげで": [
            ("多亏了老师的指导，我考上了大学。", "先生の指導のおかげで、大学に合格できました。"),
            ("托大家的福，工作顺利完成。", "皆さんのおかげで、仕事がうまくいきました。"),
            ("多亏每天坚持跑步，身体变好了。", "毎日走っているおかげで、体が丈夫になった。"),
        ],
        "せいで": [
            ("就因为熬夜，早上起不来了。", "夜更かししたせいで、朝起きられなかった。"),
            ("因为下雨，运动会取消了。", "雨のせいで、運動会が中止になった。"),
            ("都怪我没注意，把钱包弄丢了。", "私が注意しなかったせいで、財布をなくした。"),
        ],
        "かわりに": [
            ("今天我不去，由他替我去。", "今日は私が行かないかわりに、彼が行ってくれる。"),
            ("不吃肉，改吃鱼。", "肉を食べないかわりに、魚を食べる。"),
            ("这间房虽然小，但很干净。", "この部屋は狭いかわりに、きれいだ。"),
        ],
        "にかわって": [
            ("我代替部长出席明天的会议。", "部長にかわって、明日の会議に出席します。"),
            ("由姐姐代替母亲照顾弟弟。", "母にかわって、姉が弟の世話をしている。"),
        ],
        "くらい": [
            ("累得一步都走不动了。", "一歩も歩けないくらい疲れた。"),
            ("高兴得眼泪都流出来了。", "涙が出るくらい嬉しかった。"),
            ("忙到连吃饭的时间都没有。", "ご飯を食べる時間もないくらい忙しい。"),
        ],
        "ほど": [
            ("今天没有昨天那么冷。", "今日は昨日ほど寒くない。"),
            ("越学越觉得有意思。", "勉強すればするほど、面白くなる。"),
            ("他是班里最聪明的人。", "彼はクラスで一番頭がいい。"),
            ("没有比健康更重要的东西了。", "健康ほど大切なものはない。"),
            ("越贵的东西不一定越好。", "高ければ高いほど、いいとは限らない。"),
        ],
        "ことはない": [
            ("没必要特意跑一趟。", "わざわざ行くことはない。"),
            ("不用担心，肯定会顺利的。", "心配することはない、きっとうまくいく。"),
            ("用不着那么着急。", "そんなに急ぐことはない。"),
        ],
        "ということだ": [
            ("听说他下个月要调去东京了。", "彼は来月、東京に転勤するということだ。"),
            ("据说今年的冬天会特别冷。", "今年の冬は特に寒いということだ。"),
            ("天气预报说明天会下雨。", "天気予報によると、明日は雨が降るということだ。"),
        ],
        "ことだ": [
            ("想变厉害的话，就是要多练习。", "上手になりたければ、たくさん練習することだ。"),
            ("最好不要熬夜。", "夜更かししないことだ。"),
            ("重要的是坚持每天学一点。", "毎日少しずつ勉強を続けることだ。"),
        ],
        "っけ": [
            ("咖啡是加糖来着？", "コーヒーには砂糖を入れるんだっけ？"),
            ("昨天是说要交报告来着？", "昨日、レポートを提出するって言ったっけ？"),
            ("以前这里好像有一家书店来着？", "昔ここに本屋さんがあったっけ？"),
        ],
        "しかない": [
            ("事已至此，只能努力了。", "こうなったら、頑張るしかない。"),
            ("来不及了，只能打车去。", "間に合わないから、タクシーで行くしかない。"),
            ("这个问题除了他没人能解决。", "この問題は彼に頼むしかない。"),
        ],
        "だって": [
            ("听说那家店的拉面特别好吃。", "あの店のラーメン、すごく美味しいんだって。"),
            ("听说他辞职了。", "彼、会社を辞めたんだって。"),
            ("据说那个公园樱花很漂亮。", "その公園は桜が綺麗なんだって。"),
        ],
        "だもん": [
            ("不借给你，因为我自己要用嘛。", "貸さないよ、自分で使うんだもん。"),
            ("没办法呀，谁让下雨了呢。", "仕方ないよ、雨が降ってるんだもん。"),
            ("当然会生气啦，太过分了嘛。", "怒るよ、ひどいんだもん。"),
        ],
        "つまり": [
            ("他没有手机也没有电脑，也就是说没法联系。", "彼は携帯もパソコンも持っていない。つまり、連絡が取れないということだ。"),
            ("比赛取消了。也就是说，今天白来了。", "試合は中止になった。つまり、今日は無駄足だったということだ。"),
        ],
        "そのため": [
            ("想出国留学，为此正在打工攒钱。", "留学したいと思っている。そのために、アルバイトでお金をためている。"),
            ("明天有重要的发表，因此今晚要熬夜准备了。", "明日大事なプレゼンがある。そのため、今夜は徹夜で準備する。"),
        ],
        "その結果": [
            ("坚持减肥三个月，结果瘦了五公斤。", "三か月ダイエットを続けた。その結果、五キロ痩せた。"),
            ("反复实验，终于成功了。", "何度も実験を繰り返した。その結果、ついに成功した。"),
        ],
        "なぜなら": [
            ("我不能去。因为身体不太舒服。", "私は行けません。なぜなら、体調があまり良くないからです。"),
            ("这本书很推荐。因为内容简单易懂。", "この本はおすすめです。なぜなら、内容が簡単でわかりやすいからです。"),
        ],
        "なぜかというと": [
            ("我选了日语课。因为一直对日本文化很感兴趣。", "日本語の授業を選びました。なぜかというと、ずっと日本文化に興味があったからです。"),
        ],
    }

    # ---- 匹配语法点 ----
    candidates = []
    for key, pairs in PAIRS.items():
        if key in ct or key in title:
            candidates.extend(pairs)

    if candidates:
        cn_text, jp_answer = _random.choice(candidates)
    else:
        # 未匹配的语法点：生成简单后备句
        cn_text = "请使用\u300c{0}\u300d将以下内容翻译成日语。".format(title)
        jp_answer = "この文法を使って文を作ってください。"

    if not cn_text or not jp_answer:
        cn_text = usage[:60] if usage else "请使用\u300c{title}\u300d造句".format(title=title)
        jp_answer = ct

    return cn_text, jp_answer



# Listening routes (NEW)
# ---------------------------------------------------------------------------

@app.route("/api/listening")
def get_listening_list():
    results = []
    if os.path.exists(LISTENING_DIR):
        for filename in sorted(os.listdir(LISTENING_DIR)):
            if filename.endswith(".json"):
                path = os.path.join(LISTENING_DIR, filename)
                with open(path, "r", encoding="utf-8") as f:
                    entry = json.load(f)
                results.append({
                    "id": entry.get("id"),
                    "title": entry.get("title", ""),
                    "translation": entry.get("translation", ""),
                })
    return jsonify(results)


@app.route("/api/listening/<int:listening_id>")
def get_listening_detail(listening_id):
    path = os.path.join(LISTENING_DIR, f"{listening_id}.json")
    if not os.path.exists(path):
        return jsonify({"error": "listening material not found"}), 404
    with open(path, "r", encoding="utf-8") as f:
        entry = json.load(f)
    return jsonify({
        "id": entry.get("id"),
        "title": entry.get("title", ""),
        "text": entry.get("text", ""),
        "translation": entry.get("translation", ""),
        "particles_count": len(entry.get("particles", [])),
        "grammar_links_count": len(entry.get("grammar_links", [])),
        "core_nouns_count": len(entry.get("core_nouns", []))
    })


@app.route("/api/listening/<int:listening_id>/answers")
def get_listening_answers(listening_id):
    path = os.path.join(LISTENING_DIR, f"{listening_id}.json")
    if not os.path.exists(path):
        return jsonify({"error": "listening material not found"}), 404
    with open(path, "r", encoding="utf-8") as f:
        entry = json.load(f)
    return jsonify({
        "id": entry.get("id"),
        "text": entry.get("text", ""),
        "translation": entry.get("translation", ""),
        "particles": entry.get("particles", []),
        "grammar_links": entry.get("grammar_links", []),
        "core_nouns": entry.get("core_nouns", [])
    })


@app.route("/api/listening/<int:listening_id>/audio")
def get_listening_audio(listening_id):
    path = os.path.join(LISTENING_DIR, f"{listening_id}.json")
    if not os.path.exists(path):
        return jsonify({"error": "listening material not found"}), 404
    with open(path, "r", encoding="utf-8") as f:
        entry = json.load(f)
    text = entry.get("text", "")
    if not text:
        return jsonify({"error": "no text to speak"}), 400
    try:
        import edge_tts, asyncio

        async def generate():
            tmp = tempfile.NamedTemporaryFile(suffix=".mp3", delete=False)
            tmp.close()
            comm = edge_tts.Communicate(text, "ja-JP-NanamiNeural")
            await comm.save(tmp.name)
            return tmp.name

        audio_path = asyncio.run(generate())
        return send_file(audio_path, mimetype="audio/mpeg", as_attachment=False,
                        download_name=f"listening_{listening_id}.mp3")
    except Exception:
        return jsonify({"error": "tts failed"}), 500


@app.route("/api/listening-progress", methods=["POST"])
def save_listening_progress():
    payload = request.get_json()
    username = payload.get("username", "default")
    listening_id = payload.get("listening_id")
    if listening_id is None:
        return jsonify({"error": "listening_id is required"}), 400

    ensure_user(username)
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")

    conn = get_db()
    conn.execute(
        "INSERT INTO listening_progress "
        "(username, listening_id, score, particles_correct, particles_total, "
        "grammar_correct, grammar_total, nouns_correct, nouns_total, completed_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            username,
            int(listening_id),
            payload.get("score", 0),
            payload.get("particles_correct", 0),
            payload.get("particles_total", 0),
            payload.get("grammar_correct", 0),
            payload.get("grammar_total", 0),
            payload.get("nouns_correct", 0),
            payload.get("nouns_total", 0),
            now,
        ),
    )
    conn.commit()
    conn.close()
    return jsonify({"status": "ok", "completed_at": now})


@app.route("/api/listening-progress", methods=["GET"])
def get_listening_progress():
    username = request.args.get("username", "default")
    ensure_user(username)
    conn = get_db()
    rows = conn.execute(
        "SELECT listening_id, score, particles_correct, particles_total, "
        "grammar_correct, grammar_total, nouns_correct, nouns_total, "
        "completed_at FROM listening_progress WHERE username = ? ORDER BY completed_at DESC",
        (username,),
    ).fetchall()
    conn.close()
    return jsonify([dict(r) for r in rows])

# 
@app.route("/test-chinese")
def test_chinese():
    from flask import Response
    return Response("<h1>?????</h1><p>?????????</p>", mimetype="text/html; charset=utf-8")

# # Entry point
# ---------------------------------------------------------------------------

# Auto-initialize database on every startup
try:
    init_db()
    print("[startup] Database initialized OK", file=sys.stderr)
except Exception as e:
    print("[startup] Database init FAILED:", e, file=sys.stderr)

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    print(f"[startup] Starting Flask on 0.0.0.0:{port}", file=sys.stderr)
    app.run(host="0.0.0.0", port=port, debug=False)
