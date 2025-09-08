# db.py
import os
import sqlite3
from datetime import datetime
import logging
from typing import Optional, Dict, Any

# Use one DB file everywhere; override with env if desired
DB_PATH = os.getenv("LEARNING_DB_PATH", "learning.db")


def init_db() -> None:
    """
    Initialize all tables we use:
      - custom_responses         (original learning store)
      - ai_feedback              (original feedback log)
      - slack_threads            (threading info so we can update a Slack message)
      - guests                   (to detect returning guests by email)
      - processed_events         (idempotency for webhooks/actions)
      - learning_examples        (simple examples store; includes coach_prompt)
    """
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()

    # Original table
    c.execute("""
        CREATE TABLE IF NOT EXISTS custom_responses (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            listing_id INTEGER,
            question_text TEXT,
            response_text TEXT,
            created_at TEXT
        )
    """)

    # Original feedback table
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

    # Idempotency for incoming webhooks / Slack actions
    c.execute("""
        CREATE TABLE IF NOT EXISTS processed_events (
            event_id TEXT PRIMARY KEY,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP
        )
    """)

    # Simple examples table used by "Save for learning" in your UI
    c.execute("""
        CREATE TABLE IF NOT EXISTS learning_examples (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            intent TEXT,
            question TEXT,
            answer TEXT,
            coach_prompt TEXT,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP
        )
    """)

    # Backward-compatibility: if an older deploy created learning_examples without coach_prompt,
    # try to add it. (This will no-op on newer DBs.)
    try:
        c.execute("ALTER TABLE learning_examples ADD COLUMN coach_prompt TEXT")
    except Exception:
        pass

    conn.commit()
    conn.close()


# ---------------- Existing functions (kept) ----------------

def save_custom_response(listing_id: Any, question: str, response: str) -> None:
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("""
        INSERT INTO custom_responses (listing_id, question_text, response_text, created_at)
        VALUES (?, ?, ?, ?)
    """, (listing_id, question, response, datetime.utcnow().isoformat()))
    conn.commit()
    conn.close()


def get_similar_response(listing_id: Any, question: str, threshold: float = 0.6) -> Optional[str]:
    """
    Extremely simple "similarity": word overlap ratio vs. previously saved questions
    for that listing. Good enough as a lightweight fallback.
    """
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("""
        SELECT question_text, response_text FROM custom_responses WHERE listing_id = ?
    """, (listing_id,))
    results = c.fetchall()
    conn.close()

    question_words = set((question or "").lower().split())
    if not question_words:
        return None

    for prev_q, prev_resp in results:
        prev_words = set((prev_q or "").lower().split())
        if not prev_words:
            continue
        overlap = question_words & prev_words
        score = len(overlap) / max(len(prev_words), 1)
        if score >= threshold:
            return prev_resp
    return None


def save_learning_example(listing_id: Any, question: str, corrected_reply: str) -> None:
    """
    Kept for backward-compat compatibility with your older code.
    Writes into custom_responses as your original "learning store".
    (Your newer UI also saves to learning_examples; that path is handled elsewhere.)
    """
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("""
        INSERT INTO custom_responses (listing_id, question_text, response_text, created_at)
        VALUES (?, ?, ?, ?)
    """, (listing_id, question, corrected_reply, datetime.utcnow().isoformat()))
    conn.commit()
    conn.close()


def save_ai_feedback(conv_id: str, question: str, answer: str, rating: str, user: str) -> None:
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("""
        INSERT INTO ai_feedback (conversation_id, question, ai_answer, rating, user, created_at)
        VALUES (?, ?, ?, ?, ?, ?)
    """, (conv_id, question, answer, rating, user, datetime.utcnow().isoformat()))
    conn.commit()
    conn.close()


# Backwards-compat exports expected elsewhere in your code
store_learning_example = save_learning_example
store_ai_feedback = save_ai_feedback


# ---------------- New functions for features ----------------

def upsert_slack_thread(conv_id: str, channel: str, ts: str) -> None:
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


def get_slack_thread(conv_id: str) -> Optional[Dict[str, str]]:
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


def note_guest(email: str) -> int:
    """
    Upsert a guest by email and increment their count. Returns the visit count.
    Used by your returning-guest logic.
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


# ---------------- Idempotency helpers (Option A) ----------------

def already_processed(event_id: Optional[str]) -> bool:
    """
    Return True if we've seen this event_id before (for webhook/action idempotency).
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
    Mark an event_id as processed. Safe to call repeatedly.
    """
    if not event_id:
        return
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    try:
        c.execute(
            "INSERT OR IGNORE INTO processed_events (event_id, created_at) VALUES (?, ?)",
            (event_id, datetime.utcnow().isoformat()),
        )
        conn.commit()
    finally:
        conn.close()
