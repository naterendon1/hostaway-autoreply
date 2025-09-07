# db.py
import os
import sqlite3
from datetime import datetime
import logging
from typing import Optional, Dict, Any, Iterable

# Keep one DB for everything. Default aligns with main.py's default.
DB_PATH = os.getenv("LEARNING_DB_PATH", "learning.db")


# --------------------------- Core bootstrap ---------------------------

def init_db():
    """
    Initialize all tables we use:
      - custom_responses (your original learning store)
      - ai_feedback (your original feedback log)
      - slack_threads (threading info so we can update messages)
      - guests (to detect returning guests by email)
      - processed_events (idempotency for webhooks)
      - learning_examples (richer store for "Save for learning" with coach_prompt)
    """
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()

    # Your original table
    c.execute("""
        CREATE TABLE IF NOT EXISTS custom_responses (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            listing_id INTEGER,
            question_text TEXT,
            response_text TEXT,
            created_at TEXT
        )
    """)

    # Your original feedback table
    c.execute("""
        CREATE TABLE IF NOT EXISTS ai_feedback (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            conversation_id TEXT,
            question TEXT,
            ai_answer TEXT,
            rating TEXT,
            user TEXT,
            created_at TEXT
        )
    """)

    # Slack thread binding (one Slack thread per Hostaway conversation)
    c.execute("""
        CREATE TABLE IF NOT EXISTS slack_threads (
            conv_id TEXT PRIMARY KEY,
            channel TEXT NOT NULL,
            ts TEXT NOT NULL
        )
    """)

    # Simple guest registry to detect "returning guest"
    c.execute("""
        CREATE TABLE IF NOT EXISTS guests (
            email TEXT PRIMARY KEY,
            first_seen TEXT DEFAULT CURRENT_TIMESTAMP,
            last_seen TEXT DEFAULT CURRENT_TIMESTAMP,
            count INTEGER DEFAULT 1
        )
    """)

    # Idempotency for incoming webhooks / event messages
    c.execute("""
        CREATE TABLE IF NOT EXISTS processed_events (
            event_id TEXT PRIMARY KEY,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP
        )
    """)

    # Rich learning store for "Save for learning"
    c.execute("""
        CREATE TABLE IF NOT EXISTS learning_examples (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            intent TEXT,
            question TEXT,
            answer TEXT,
            coach_prompt TEXT,
            listing_id TEXT,
            guest_id TEXT,
            conversation_id TEXT,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP
        )
    """)

    # Backward-compat migrations: add missing columns if this table predated them
    _ensure_learning_examples_columns(c, [
        ("coach_prompt", "TEXT"),
        ("listing_id", "TEXT"),
        ("guest_id", "TEXT"),
        ("conversation_id", "TEXT"),
    ])

    conn.commit()
    conn.close()


def _ensure_learning_examples_columns(cur: sqlite3.Cursor, cols: Iterable[tuple[str, str]]):
    """
    Adds columns to learning_examples if they don't exist (SQLite simple migration).
    """
    try:
        cur.execute("PRAGMA table_info(learning_examples)")
        existing = {row[1] for row in cur.fetchall()}  # column names
    except Exception:
        existing = set()

    for name, ddl in cols:
        if name not in existing:
            try:
                cur.execute(f"ALTER TABLE learning_examples ADD COLUMN {name} {ddl}")
            except Exception:
                # Safe to ignore if the column exists or SQLite version quirks
                pass


# --------------------------- Existing functions (kept) ---------------------------

def save_custom_response(listing_id, question, response):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("""
        INSERT INTO custom_responses (listing_id, question_text, response_text, created_at)
        VALUES (?, ?, ?, ?)
    """, (listing_id, question, response, datetime.utcnow().isoformat()))
    conn.commit()
    conn.close()


def get_similar_response(listing_id, question, threshold=0.6):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("""
        SELECT question_text, response_text FROM custom_responses WHERE listing_id = ?
    """, (listing_id,))
    results = c.fetchall()
    conn.close()
    question_words = set((question or "").lower().split())
    for prev_q, prev_resp in results:
        prev_words = set((prev_q or "").lower().split())
        if not prev_words:
            continue
        overlap = question_words & prev_words
        if len(overlap) / max(len(prev_words), 1) >= threshold:
            return prev_resp
    return None


def save_learning_example(listing_id, question, corrected_reply):
    """
    Backward-compat: your original function. This now writes into learning_examples
    with minimal fields (no coach_prompt).
    """
    insert_learning_example(
        intent="other",
        question=question,
        answer=corrected_reply,
        coach_prompt=None,
        listing_id=str(listing_id) if listing_id is not None else None,
        guest_id=None,
        conversation_id=None,
    )


def save_ai_feedback(conv_id, question, answer, rating, user):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("""
        CREATE TABLE IF NOT EXISTS ai_feedback (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            conversation_id TEXT,
            question TEXT,
            ai_answer TEXT,
            rating TEXT,
            user TEXT,
            created_at TEXT
        )
    """)
    c.execute("""
        INSERT INTO ai_feedback (conversation_id, question, ai_answer, rating, user, created_at)
        VALUES (?, ?, ?, ?, ?, ?)
    """, (conv_id, question, answer, rating, user, datetime.utcnow().isoformat()))
    conn.commit()
    conn.close()


# Backwards-compat exports (some parts of your code import these names)
store_learning_example = save_learning_example
store_ai_feedback = save_ai_feedback


# --------------------------- Slack thread helpers ---------------------------

def upsert_slack_thread(conv_id: str, channel: str, ts: str):
    """
    Save or update the Slack thread (ts) used for a Hostaway conversation.
    """
    if not conv_id or not channel or not ts:
        logging.warning("upsert_slack_thread called with missing args")
        return
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("""
      INSERT INTO slack_threads (conv_id, channel, ts)
      VALUES (?, ?, ?)
      ON CONFLICT(conv_id) DO UPDATE SET channel=excluded.channel, ts=excluded.ts
    """, (str(conv_id), str(channel), str(ts)))
    conn.commit()
    conn.close()


def get_slack_thread(conv_id: str) -> Optional[Dict[str, Any]]:
    """
    Return {'channel': 'C123', 'ts': '1700000000.123456'} if we know the thread.
    """
    if not conv_id:
        return None
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("SELECT channel, ts FROM slack_threads WHERE conv_id=?", (str(conv_id),))
    row = c.fetchone()
    conn.close()
    return {"channel": row[0], "ts": row[1]} if row else None


# --------------------------- Guest visit counter ---------------------------

def note_guest(email: str) -> int:
    """
    Upsert a guest by email and increment their count. Returns the visit count.
    """
    if not email:
        return 1
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("SELECT count FROM guests WHERE email=?", (email,))
    row = c.fetchone()
    if row:
        cnt = int(row[0]) + 1
        c.execute("UPDATE guests SET count=?, last_seen=CURRENT_TIMESTAMP WHERE email=?", (cnt, email))
    else:
        cnt = 1
        c.execute("INSERT INTO guests (email) VALUES (?)", (email,))
    conn.commit()
    conn.close()
    return cnt


def get_guest_count(email: str) -> int:
    """
    Read-only helper: returns how many times we've seen this email. 0 if unknown.
    """
    if not email:
        return 0
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("SELECT count FROM guests WHERE email=?", (email,))
    row = c.fetchone()
    conn.close()
    return int(row[0]) if row else 0


# --------------------------- Idempotency helpers ---------------------------

def already_processed(event_id: Optional[str]) -> bool:
    """
    Returns True if we've seen this event_id before.
    """
    if not event_id:
        return False
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("SELECT 1 FROM processed_events WHERE event_id=?", (event_id,))
    exists = c.fetchone() is not None
    conn.close()
    return exists


def mark_processed(event_id: Optional[str]) -> None:
    """
    Marks an event_id as processed (no-op if event_id is None).
    """
    if not event_id:
        return
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    try:
        c.execute("INSERT OR IGNORE INTO processed_events (event_id, created_at) VALUES (?, ?)",
                  (event_id, datetime.utcnow().isoformat()))
        conn.commit()
    finally:
        conn.close()


# --------------------------- Learning examples (rich) ---------------------------

def insert_learning_example(
    intent: Optional[str],
    question: Optional[str],
    answer: Optional[str],
    coach_prompt: Optional[str] = None,
    listing_id: Optional[str] = None,
    guest_id: Optional[str] = None,
    conversation_id: Optional[str] = None,
) -> None:
    """
    Unified writer used by "Save for learning" (and your legacy wrapper).
    Stores an optional coach_prompt so you can later analyze what you told the AI.
    """
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()

    # Ensure base table + columns exist (safe to call often)
    c.execute("""
        CREATE TABLE IF NOT EXISTS learning_examples (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            intent TEXT,
            question TEXT,
            answer TEXT,
            coach_prompt TEXT,
            listing_id TEXT,
            guest_id TEXT,
            conversation_id TEXT,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP
        )
    """)
    _ensure_learning_examples_columns(c, [
        ("coach_prompt", "TEXT"),
        ("listing_id", "TEXT"),
        ("guest_id", "TEXT"),
        ("conversation_id", "TEXT"),
    ])

    c.execute("""
        INSERT INTO learning_examples (intent, question, answer, coach_prompt, listing_id, guest_id, conversation_id, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
    """, (
        (intent or "other"),
        (question or "")[:2000],
        (answer or "")[:4000],
        (coach_prompt or None),
        (listing_id or None),
        (guest_id or None),
        (conversation_id or None),
        datetime.utcnow().isoformat(),
    ))
    conn.commit()
    conn.close()
