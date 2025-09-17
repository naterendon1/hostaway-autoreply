# db.py
import os
import sqlite3
import logging
from datetime import datetime
from typing import Optional, Dict, Any

# Use one DB file everywhere; override with env if desired
DB_PATH = os.getenv("LEARNING_DB_PATH", "learning.db")

logger = logging.getLogger(__name__)


def _connect() -> sqlite3.Connection:
    """
    Open a connection with a sane default row factory.
    """
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def _ensure_columns(conn: sqlite3.Connection, table: str, columns: Dict[str, str]) -> None:
    """
    Idempotently add missing columns to an existing table.
    columns: {"col_name": "SQL_TYPE [DEFAULT ...]"}
    """
    cur = conn.cursor()
    cur.execute(f"PRAGMA table_info({table})")
    existing = {row[1] for row in cur.fetchall()}  # column names
    for name, decl in columns.items():
        if name not in existing:
            try:
                cur.execute(f"ALTER TABLE {table} ADD COLUMN {name} {decl}")
            except Exception as e:
                logger.debug(f"ALTER TABLE {table} ADD COLUMN {name} failed or already exists: {e}")
    conn.commit()


def init_db() -> None:
    """
    Initialize all tables we use:
      - custom_responses         (original learning store)
      - ai_feedback              (feedback log; now includes 'reason' + created_at default)
      - slack_threads            (threading info so we can update a Slack message)
      - guests                   (to detect returning guests by email)
      - processed_events         (idempotency for webhooks/actions)
      - learning_examples        (simple examples store; includes coach_prompt)
    Also performs light, idempotent migrations to add newer columns on older DBs.
    """
    conn = _connect()
    c = conn.cursor()

    # Original table (legacy learning store)
    c.execute("""
        CREATE TABLE IF NOT EXISTS custom_responses (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            listing_id TEXT,
            question_text TEXT,
            response_text TEXT,
            created_at TEXT
        )
    """)

    # Feedback table (enhanced: includes reason + created_at default)
    c.execute("""
        CREATE TABLE IF NOT EXISTS ai_feedback (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            conversation_id TEXT,
            question TEXT,
            ai_answer TEXT,
            rating TEXT,
            user TEXT,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP
        )
    """)
    # Migrations: add missing columns on older DBs
    _ensure_columns(conn, "ai_feedback", {
        "reason": "TEXT",
        "created_at": "TEXT DEFAULT CURRENT_TIMESTAMP",
    })

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

    # Examples used by "Save for learning"
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
    # Backwards-compat migration for learning_examples (older DBs may miss some columns)
    _ensure_columns(conn, "learning_examples", {
        "intent": "TEXT",
        "question": "TEXT",
        "answer": "TEXT",
        "coach_prompt": "TEXT",
        "created_at": "TEXT DEFAULT CURRENT_TIMESTAMP",
    })

    conn.commit()
    conn.close()


# ---------------- Existing functions (kept) ----------------

def save_custom_response(listing_id: Any, question: str, response: str) -> None:
    conn = _connect()
    c = conn.cursor()
    c.execute("""
        INSERT INTO custom_responses (listing_id, question_text, response_text, created_at)
        VALUES (?, ?, ?, ?)
    """, (str(listing_id) if listing_id is not None else "", question, response, datetime.utcnow().isoformat()))
    conn.commit()
    conn.close()


def get_similar_response(listing_id: Any, question: str, threshold: float = 0.6) -> Optional[str]:
    """
    Extremely simple "similarity": word overlap ratio vs. previously saved questions
    for that listing. Good enough as a lightweight fallback.
    """
    conn = _connect()
    c = conn.cursor()
    c.execute("""
        SELECT question_text, response_text FROM custom_responses WHERE listing_id = ?
    """, (str(listing_id) if listing_id is not None else "",))
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
    Kept for backward-compatibility with older codepaths.
    Writes into custom_responses as your original "learning store".
    (Your newer UI also saves to learning_examples; that path is handled elsewhere.)
    """
    conn = _connect()
    c = conn.cursor()
    c.execute("""
        INSERT INTO custom_responses (listing_id, question_text, response_text, created_at)
        VALUES (?, ?, ?, ?)
    """, (str(listing_id) if listing_id is not None else "", question, corrected_reply, datetime.utcnow().isoformat()))
    conn.commit()
    conn.close()


def save_ai_feedback(conv_id: str, question: str, answer: str, rating: str, user: str, reason: str = "") -> None:
    """
    Store a feedback row. 'reason' is optional for legacy callers.
    """
    conn = _connect()
    # Ensure enhanced schema exists even if init_db wasn't called yet in this process
    _ensure_columns(conn, "ai_feedback", {"reason": "TEXT", "created_at": "TEXT DEFAULT CURRENT_TIMESTAMP"})
    c = conn.cursor()
    c.execute("""
        INSERT INTO ai_feedback (conversation_id, question, ai_answer, rating, reason, user, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?)
    """, (str(conv_id) if conv_id is not None else "",
          question or "",
          answer or "",
          rating or "",
          reason or "",
          user or "",
          datetime.utcnow().isoformat()))
    conn.commit()
    conn.close()


# Backwards-compat exports expected elsewhere in your code
store_learning_example = save_learning_example
store_ai_feedback = save_ai_feedback


# ---------------- New/maintained functions for features ----------------

def upsert_slack_thread(conv_id: str, channel: str, ts: str) -> None:
    """
    Save or update the Slack thread (ts) used for a Hostaway conversation.
    """
    if not conv_id or not channel or not ts:
        logger.warning("upsert_slack_thread called with missing args")
        return
    conn = _connect()
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
    conn = _connect()
    c = conn.cursor()
    c.execute("SELECT channel, ts FROM slack_threads WHERE conv_id=?", (str(conv_id),))
    row = c.fetchone()
    conn.close()
    return {"channel": row["channel"], "ts": row["ts"]} if row else None


def note_guest(email: str) -> int:
    """
    Upsert a guest by email and increment their count. Returns the visit count.
    Used by your returning-guest logic.
    """
    if not email:
        return 1
    conn = _connect()
    c = conn.cursor()
    c.execute("SELECT count FROM guests WHERE email=?", (email,))
    row = c.fetchone()
    if row:
        cnt = int(row["count"]) + 1
        c.execute("UPDATE guests SET count=?, last_seen=CURRENT_TIMESTAMP WHERE email=?", (cnt, email))
    else:
        cnt = 1
        c.execute("INSERT INTO guests (email) VALUES (?)", (email,))
    conn.commit()
    conn.close()
    return cnt


# ---------------- Idempotency helpers ----------------

def already_processed(event_id: Optional[str]) -> bool:
    """
    Return True if we've seen this event_id before (for webhook/action idempotency).
    """
    if not event_id:
        return False
    conn = _connect()
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
    conn = _connect()
    c = conn.cursor()
    try:
        c.execute(
            "INSERT OR IGNORE INTO processed_events (event_id, created_at) VALUES (?, ?)",
            (event_id, datetime.utcnow().isoformat()),
        )
        conn.commit()
    finally:
        conn.close()
