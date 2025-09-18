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
    p = Path(DB_PATH)
    if p.parent and not p.parent.exists():
        p.parent.mkdir(parents=True, exist_ok=True)  # why: /var/data on first boot
    conn = sqlite3.connect(str(p), check_same_thread=False)  # why: uvicorn threads
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

    # --- base tables ---
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

    # --- analytics_events (for message-level logs) ---
    c.execute("""
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

    # --- ai_exchanges (for model suggestion logs) ---
    c.execute("""
        CREATE TABLE IF NOT EXISTS ai_exchanges (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            conversation_id TEXT,
            guest_message TEXT,
            ai_suggestion TEXT,
            intent TEXT,
            meta TEXT,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP
        )
    """)

    conn.commit()
    conn.close()

# -------- learning store + feedback ----------
def save_custom_response(listing_id: Any, question: str, response: str) -> None:
    conn = _connect()
    c = conn.cursor()
    c.execute("""
        INSERT INTO custom_responses (listing_id, question_text, response_text, created_at)
        VALUES (?, ?, ?, ?)
    """, (str(listing_id) if listing_id is not None else "", question, response, datetime.utcnow().isoformat()))
    conn.commit(); conn.close()

def get_similar_response(listing_id: Any, question: str, threshold: float = 0.6) -> Optional[str]:
    conn = _connect(); c = conn.cursor()
    c.execute("SELECT question_text, response_text FROM custom_responses WHERE listing_id = ?", (str(listing_id) if listing_id is not None else "",))
    results = c.fetchall(); conn.close()
    q_words = set((question or "").lower().split())
    if not q_words: return None
    for prev_q, prev_resp in results:
        p_words = set((prev_q or "").lower().split())
        if p_words and len(q_words & p_words) / max(len(p_words), 1) >= threshold:
            return prev_resp
    return None

def save_learning_example(listing_id: Any, question: str, corrected_reply: str) -> None:
    conn = _connect(); c = conn.cursor()
    c.execute("""
        INSERT INTO custom_responses (listing_id, question_text, response_text, created_at)
        VALUES (?, ?, ?, ?)
    """, (str(listing_id) if listing_id is not None else "", question, corrected_reply, datetime.utcnow().isoformat()))
    conn.commit(); conn.close()

def save_ai_feedback(conv_id: str, question: str, answer: str, rating: str, user: str, reason: str = "") -> None:
    conn = _connect(); _ensure_columns(conn, "ai_feedback", {"reason": "TEXT", "created_at": "TEXT DEFAULT CURRENT_TIMESTAMP"})
    c = conn.cursor()
    c.execute("""
        INSERT INTO ai_feedback (conversation_id, question, ai_answer, rating, reason, user, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?)
    """, (str(conv_id) if conv_id is not None else "", question or "", answer or "", rating or "", reason or "", user or "", datetime.utcnow().isoformat()))
    conn.commit(); conn.close()

store_learning_example = save_learning_example
store_ai_feedback = save_ai_feedback

# -------- slack threading / guests ----------
def upsert_slack_thread(conv_id: str, channel: str, ts: str) -> None:
    if not conv_id or not channel or not ts:
        logger.warning("upsert_slack_thread called with missing args"); return
    conn = _connect(); c = conn.cursor()
    c.execute("""
      INSERT INTO slack_threads (conv_id, channel, ts)
      VALUES (?, ?, ?)
      ON CONFLICT(conv_id) DO UPDATE SET channel=excluded.channel, ts=excluded.ts
    """, (str(conv_id), str(channel), str(ts)))
    conn.commit(); conn.close()

def get_slack_thread(conv_id: str) -> Optional[Dict[str, str]]:
    if not conv_id: return None
    conn = _connect(); c = conn.cursor()
    c.execute("SELECT channel, ts FROM slack_threads WHERE conv_id=?", (str(conv_id),))
    row = c.fetchone(); conn.close()
    return {"channel": row["channel"], "ts": row["ts"]} if row else None

def note_guest(email: str) -> int:
    if not email: return 1
    conn = _connect(); c = conn.cursor()
    c.execute("SELECT count FROM guests WHERE email=?", (email,))
    row = c.fetchone()
    if row:
        cnt = int(row["count"]) + 1
        c.execute("UPDATE guests SET count=?, last_seen=CURRENT_TIMESTAMP WHERE email=?", (cnt, email))
    else:
        cnt = 1
        c.execute("INSERT INTO guests (email) VALUES (?)", (email,))
    conn.commit(); conn.close()
    return cnt

# -------- idempotency ----------
def already_processed(event_id: Optional[str]) -> bool:
    if not event_id: return False
    conn = _connect(); c = conn.cursor()
    c.execute("SELECT 1 FROM processed_events WHERE event_id=?", (event_id,))
    exists = c.fetchone() is not None; conn.close()
    return exists

def mark_processed(event_id: Optional[str]) -> None:
    if not event_id: return
    conn = _connect(); c = conn.cursor()
    try:
        c.execute("INSERT OR IGNORE INTO processed_events (event_id, created_at) VALUES (?, ?)", (event_id, datetime.utcnow().isoformat()))
        conn.commit()
    finally:
        conn.close()

# -------- analytics / logging ----------
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
        "source": "TEXT","event": "TEXT","conversation_id": "TEXT","reservation_id": "TEXT",
        "listing_id": "TEXT","guest_id": "TEXT","user_id": "TEXT","rating": "TEXT",
        "reason": "TEXT","intent": "TEXT","metadata": "TEXT","created_at": "TEXT DEFAULT CURRENT_TIMESTAMP",
    })
    conn.commit()

def record_event(source: str, event: str, **fields: Any) -> None:
    allowed = {"conversation_id","reservation_id","listing_id","guest_id","user_id","rating","reason","intent"}
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
        cur.execute("""
            INSERT INTO analytics_events (
                source, event,
                conversation_id, reservation_id, listing_id, guest_id, user_id,
                rating, reason, intent, metadata, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (source, event, row["conversation_id"], row["reservation_id"], row["listing_id"], row["guest_id"], row["user_id"],
              row["rating"], row["reason"], row["intent"], metadata, datetime.utcnow().isoformat()))
        conn.commit()
    finally:
        conn.close()

def log_message_event(*args: Any, **fields: Any) -> None:
    """
    Flexible logger → analytics_events as event='message'.
    Dedupes canonical keys so record_event won't receive duplicate kwargs.
    """
    source = "app"
    conversation_id = ""
    role = fields.pop("role", "user")
    message = fields.pop("message", "")

    # Legacy positional forms
    if len(args) == 4:
        source, conversation_id, role, message = args
    elif len(args) == 3:
        conversation_id, role, message = args
    elif len(args) == 2:
        conversation_id, message = args
    elif len(args) == 1:
        message = args[0]

    # Pop canonical keys from **fields** to avoid "multiple values" error
    canonical = {
        "conversation_id", "reservation_id", "listing_id",
        "guest_id", "user_id", "rating", "reason", "intent",
    }
    picked = {k: fields.pop(k, None) for k in canonical}

    record_event(
        source=source,
        event="message",
        conversation_id=str(picked.get("conversation_id") or conversation_id or ""),
        reservation_id=picked.get("reservation_id"),
        listing_id=picked.get("listing_id"),
        guest_id=picked.get("guest_id"),
        user_id=picked.get("user_id"),
        rating=picked.get("rating"),
        reason=picked.get("reason"),
        intent=picked.get("intent"),
        role=role,            # goes to metadata
        message=message,      # goes to metadata
        **fields,             # remaining extras → metadata
    )

def log_ai_exchange(conversation_id: Optional[str], guest_message: str, ai_suggestion: str, intent: Optional[str] = None, meta: Optional[Dict[str, Any]] = None) -> None:
    """
    Persists model suggestions to ai_exchanges. Meta is JSON-encoded.
    """
    conn = _connect()
    try:
        cur = conn.cursor()
        cur.execute("""
            INSERT INTO ai_exchanges (conversation_id, guest_message, ai_suggestion, intent, meta, created_at)
            VALUES (?, ?, ?, ?, ?, ?)
        """, (str(conversation_id) if conversation_id else "",
              guest_message or "",
              ai_suggestion or "",
              intent or "",
              json.dumps(meta or {}, ensure_ascii=False),
              datetime.utcnow().isoformat()))
        conn.commit()
    finally:
        conn.close()
