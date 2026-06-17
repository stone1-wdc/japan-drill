import os
import sqlite3
import re
import json
from datetime import datetime, timezone
from flask import Flask, render_template, jsonify, request

app = Flask(__name__)

DATABASE = os.path.join("/tmp" if os.environ.get("VERCEL") else os.path.dirname(os.path.abspath(__file__)), "japanese.db")
BOOK_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "book", "chapters")


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
                # Extract pronunciation if present: jp|reading
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

    # Build unit list
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
    return render_template("index.html")


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
    if not keyword:
        return jsonify([])

    grammar_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static", "grammar")
    results = []
    if os.path.exists(grammar_dir):
        for filename in sorted(os.listdir(grammar_dir)):
            if filename.endswith(".json"):
                path = os.path.join(grammar_dir, filename)
                with open(path, "r", encoding="utf-8") as f:
                    entry = json.load(f)
                title = entry.get("title", "")
                explanation = entry.get("explanation", "")
                if keyword in title or keyword in explanation:
                    results.append(entry)
    return jsonify(results)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

# Auto-initialize database on every startup
init_db()

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=True)
