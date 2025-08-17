# db.py
import os
import sqlite3
from datetime import datetime
import logging

# Use your existing DB file by default; allow override via env for consistency
DB_PATH = os.getenv("LEARNING_DB_PATH", "custom_responses.db")


def init_db():
    """
    Initialize all tables we use:
      - custom_responses (your original learning store)
      - ai_feedback (your original feedback log)
      - slack_threads (threading info so we reply in-thread)
      - guests (to detect returning guests by email)
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

    # NEW: Slack thread binding (one Slack thread per Hostaway conversation)
    c.execute("""
        CREATE TABLE IF NOT EXISTS slack_threads (
            conv_id TEXT PRIMARY KEY,
            channel TEXT NOT NULL,
            ts TEXT NOT NULL
        )
    """)

    # NEW: simple guest registry to detect "returning guest"
    c.execute("""
        CREATE TABLE IF NOT EXISTS guests (
            email TEXT PRIMARY KEY,
            first_seen TEXT DEFAULT CURRENT_TIMESTAMP,
            last_seen TEXT DEFAULT CURRENT_TIMESTAMP,
            count INTEGER DEFAULT 1
        )
    """)

    conn.commit()
    conn.close()


# ---------------- Existing functions (unchanged) ----------------

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
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("""
        INSERT INTO custom_responses (listing_id, question_text, response_text, created_at)
        VALUES (?, ?, ?, ?)
    """, (listing_id, question, corrected_reply, datetime.utcnow().isoformat()))
    conn.commit()
    conn.close()


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


# Backwards-compat exports
store_learning_example = save_learning_example
store_ai_feedback = save_ai_feedback


# ---------------- New functions for features ----------------

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


def get_slack_thread(conv_id: str):
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
