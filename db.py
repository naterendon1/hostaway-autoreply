# path: db.py
import os
import sqlite3
import logging
import json
from datetime import datetime
from pathlib import Path
from typing import Optional, Dict, Any

DB_PATH = os.getenv("LEARNING_DB_PATH", "learning.db")
logger = logging.getLogger(__name__)

def _connect() -> sqlite3.Connection:
    """Open a connection; make sure parent dir exists (Render disk path)."""
    p = Path(DB_PATH)
    if p.parent and not p.parent.exists():
        p.parent.mkdir(parents=True, exist_ok=True)  # why: first boot on /var/data
    # why: uvicorn can handle requests on different threads
    conn = sqlite3.connect(str(p), check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn

def _ensure_columns(conn: sqlite3.Connection, table: str, columns: Dict[str, str]) -> None:
    cur = conn.cursor()
    cur.execute(f"PRAGMA table_info({table})")
    existing = {row[1] for row in cur.fetchall()}
    for name, decl in columns.items():
        if name not in existing:
            try:
                cur.execute(f"ALTER TABLE {table} ADD COLUMN {name} {decl}")
            except Exception as e:
                logger.debug(f"ALTER TABLE {table} ADD COLUMN {name} failed/exists: {e}")
    conn.commit()

def init_db() -> None:
    conn = _connect()
    c = conn.cursor()
    c.execute("""
        CREATE TABLE IF NOT EXISTS custom_responses (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            listing_id TEXT,
            question_text TEXT,
            response_text TEXT,
            created_at TEXT
        )
    """)
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
    _ensure_columns(conn, "ai_feedback", {
        "reason": "TEXT",
        "created_at": "TEXT DEFAULT CURRENT_TIMESTAMP",
    })
    c.execute("""
        CREATE TABLE IF NOT EXISTS slack_threads (
            conv_id TEXT PRIMARY KEY,
            channel TEXT NOT NULL,
            ts TEXT NOT NULL
        )
    """)
    c.execute("""
        CREATE TABLE IF NOT EXISTS guests (
            email TEXT PRIMARY KEY,
            first_seen TEXT DEFAULT CURRENT_TIMESTAMP,
            last_seen TEXT DEFAULT CURRENT_TIMESTAMP,
            count INTEGER DEFAULT 1
        )
    """)
    c.execute("""
        CREATE TABLE IF NOT EXISTS processed_events (
            event_id TEXT PRIMARY KEY,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP
        )
    """)
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
    _ensure_columns(conn, "learning_examples", {
        "intent": "TEXT",
        "question": "TEXT",
        "answer": "TEXT",
        "coach_prompt": "TEXT",
        "created_at": "TEXT DEFAULT CURRENT_TIMESTAMP",
    })
    conn.commit()
    conn.close()

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
    conn = _connect()
    c = conn.cursor()
    c.execute("SELECT question_text, response_text FROM custom_responses WHERE listing_id = ?", (str(listing_id) if listing_id is not None else "",))
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
    conn = _connect()
    c = conn.cursor()
    c.execute("""
        INSERT INTO custom_responses (listing_id, question_text, response_text, created_at)
        VALUES (?, ?, ?, ?)
    """, (str(listing_id) if listing_id is not None else "", question, corrected_reply, datetime.utcnow().isoformat()))
    conn.commit()
    conn.close()

def save_ai_feedback(conv_id: str, question: str, answer: str, rating: str, user: str, reason: str = "") -> None:
    conn = _connect()
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

store_learning_example = save_learning_example
store_ai_feedback = save_ai_feedback

def upsert_slack_thread(conv_id: str, channel: str, ts: str) -> None:
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
    if not conv_id:
        return None
    conn = _connect()
    c = conn.cursor()
    c.execute("SELECT channel, ts FROM slack_threads WHERE conv_id=?", (str(conv_id),))
    row = c.fetchone()
    conn.close()
    return {"channel": row["channel"], "ts": row["ts"]} if row else None

def note_guest(email: str) -> int:
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

def already_processed(event_id: Optional[str]) -> bool:
    if not event_id:
        return False
    conn = _connect()
    c = conn.cursor()
    c.execute("SELECT 1 FROM processed_events WHERE event_id=?", (event_id,))
    exists = c.fetchone() is not None
    conn.close()
    return exists

def mark_processed(event_id: Optional[str]) -> None:
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

def _ensure_events_table(conn: sqlite3.Connection) -> None:
    cur = conn.cursor()
    cur.execute("""
        CREATE TABLE IF NOT EXISTS analytics_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            source TEXT,
            event TEXT,
            conversation_id TEXT,
            reservation_id TEXT,
            listing_id TEXT,
            guest_id TEXT,
            user_id TEXT,
            rating TEXT,
            reason TEXT,
            intent TEXT,
            metadata TEXT,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP
        )
    """)
    _ensure_columns(conn, "analytics_events", {
        "source": "TEXT",
        "event": "TEXT",
        "conversation_id": "TEXT",
        "reservation_id": "TEXT",
        "listing_id": "TEXT",
        "guest_id": "TEXT",
        "user_id": "TEXT",
        "rating": "TEXT",
        "reason": "TEXT",
        "intent": "TEXT",
        "metadata": "TEXT",
        "created_at": "TEXT DEFAULT CURRENT_TIMESTAMP",
    })
    conn.commit()

def record_event(source: str, event: str, **fields: Any) -> None:
    allowed = {
        "conversation_id", "reservation_id", "listing_id",
        "guest_id", "user_id", "rating", "reason", "intent",
    }
    row = {k: (fields.get(k) or "") for k in allowed}
    extra = {k: v for k, v in fields.items() if k not in allowed}
    try:
        metadata = json.dumps(extra, ensure_ascii=False) if extra else None
    except Exception:
        metadata = json.dumps({k: str(v) for k, v in extra.items()}, ensure_ascii=False) if extra else None
    conn = _connect()
    try:
        _ensure_events_table(conn)
        cur = conn.cursor()
        cur.execute(
            """
            INSERT INTO analytics_events (
                source, event,
                conversation_id, reservation_id, listing_id, guest_id, user_id,
                rating, reason, intent, metadata, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                source, event,
                row["conversation_id"], row["reservation_id"], row["listing_id"], row["guest_id"], row["user_id"],
                row["rating"], row["reason"], row["intent"], metadata,
                datetime.utcnow().isoformat(),
            ),
        )
        conn.commit()
    finally:
        conn.close()

# path: main.py  (add this if not present)
# from fastapi import FastAPI
# from db import init_db
# app = FastAPI()
# @app.on_event("startup")
# def _startup() -> None:
#     init_db()  # why: ensure tables exist before first request
