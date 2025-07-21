import sqlite3
from datetime import datetime

DB_PATH = "custom_responses.db"

def init_db():
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("""
        CREATE TABLE IF NOT EXISTS custom_responses (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            listing_id INTEGER,
            question_text TEXT,
            response_text TEXT,
            created_at TEXT
        )
    """)
    conn.commit()
    conn.close()

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
    # Very basic: looks for substring match. Can improve with fuzzy matching or embedding similarity
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("""
        SELECT question_text, response_text FROM custom_responses WHERE listing_id = ?
    """, (listing_id,))
    results = c.fetchall()
    conn.close()
    # Naive similarity: if 60% of the words overlap, use it
    question_words = set(question.lower().split())
    for prev_q, prev_resp in results:
        prev_words = set(prev_q.lower().split())
        if not prev_words: continue
        overlap = question_words & prev_words
        if len(overlap) / max(len(prev_words), 1) >= threshold:
            return prev_resp
    return None
