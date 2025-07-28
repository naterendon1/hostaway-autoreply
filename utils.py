import os
import sqlite3
from datetime import datetime
import requests

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
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("""
        SELECT question_text, response_text FROM custom_responses WHERE listing_id = ?
    """, (listing_id,))
    results = c.fetchall()
    conn.close()
    question_words = set(question.lower().split())
    for prev_q, prev_resp in results:
        prev_words = set(prev_q.lower().split())
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
        INSERT INTO ai_feedback (conversation_id, question, ai_answer, rating, user, created_at)
        VALUES (?, ?, ?, ?, ?, ?)
    """, (conv_id, question, answer, rating, user, datetime.utcnow().isoformat()))
    conn.commit()
    conn.close()

def fetch_hostaway_listing(listing_id: int) -> dict:
    url = f"https://api.hostaway.com/listings/{listing_id}"
    headers = {
        "Authorization": f"Bearer {os.getenv('HOSTAWAY_API_KEY')}",
        "Content-Type": "application/json"
    }
    response = requests.get(url, headers=headers)
    response.raise_for_status()
    return response.json()

def fetch_hostaway_reservation(reservation_id: int) -> dict:
    url = f"https://api.hostaway.com/reservations/{reservation_id}"
    headers = {
        "Authorization": f"Bearer {os.getenv('HOSTAWAY_API_KEY')}",
        "Content-Type": "application/json"
    }
    response = requests.get(url, headers=headers)
    response.raise_for_status()
    return response.json()

# Export aliases
store_learning_example = save_learning_example
store_ai_feedback = save_ai_feedback

# Dummy placeholders
def notify_admin_of_custom_response(metadata, reply_text):
    pass

def make_ai_reply(prompt: str, previous_examples: list = None) -> str:
    return f"Auto-response: {prompt}"

def get_cancellation_policy_summary(listing: dict, reservation: dict) -> str:
    return "Flexible cancellation policy. Full refund 5 days prior."
