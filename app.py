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

    # --- Question 2: generated from grammar rules ---
    # Build a template sentence using the grammar pattern
    q2_type = "choice"

    # Extract the core grammar pattern from title
    core_pattern = title.split("(")[0].strip().replace(" ", "") if title else ""
    # Try to construct a fill-in based on conjugation pattern
    conj_parts = conjugation.replace(" ", "").split("/")[0].strip() if conjugation else ""

    # Build audio text
    audio_jp = ""
    if examples:
        audio_jp = examples[0].get("jp", "")
    if not audio_jp and core_pattern:
        audio_jp = "「" + core_pattern + "」の使い方です。"""

    if core_pattern and conj_parts:
        # Choice question: which conjugation/usage is correct
        quiz_items.append({
            "type": "choice",
            "jp_blanked": "次の文の___に入るものを選びなさい。",
            "cn_hint": "语法：「" + title + "」— " + (usage[:80] if usage else "") + "",
            "answer": core_pattern,
            "options": _generate_distractors_for_pattern(core_pattern, examples),
            "source": "grammar",
            "original_jp": audio_jp,
            "reading": audio_jp
        })
    elif core_pattern:
        quiz_items.append({
            "type": "choice",
            "jp_blanked": "「" + core_pattern + "」の使い方として正しいものは？",
            "cn_hint": "语法：「" + title + "」",
            "answer": usage[:60] if usage else core_pattern,
            "options": [usage[:60] if usage else core_pattern, "原因・理由を表す", "命令・禁止を表す", "推量・推定を表す"],
            "source": "grammar",
            "original_jp": audio_jp,
            "reading": audio_jp
        })
    else:
        quiz_items.append({
            "type": "choice",
            "jp_blanked": "「" + title + "」の意味として最も適切なものは？",
            "cn_hint": "",
            "answer": usage[:60] if usage else title,
            "options": [usage[:60] if usage else title, "逆接を表す", "理由を表す", "仮定を表す"],
            "source": "grammar",
            "original_jp": audio_jp,
            "reading": audio_jp
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
