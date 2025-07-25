# utils.py
import os
import requests
import logging
import sqlite3
from datetime import datetime
from difflib import get_close_matches

HOSTAWAY_CLIENT_ID = os.getenv("HOSTAWAY_CLIENT_ID")
HOSTAWAY_CLIENT_SECRET = os.getenv("HOSTAWAY_CLIENT_SECRET")
HOSTAWAY_API_BASE = "https://api.hostaway.com/v1"
LEARNING_DB_PATH = os.getenv("LEARNING_DB_PATH", "learning.db")

def get_hostaway_access_token() -> str:
    url = f"{HOSTAWAY_API_BASE}/accessTokens"
    data = {
        "grant_type": "client_credentials",
        "client_id": HOSTAWAY_CLIENT_ID,
        "client_secret": HOSTAWAY_CLIENT_SECRET,
        "scope": "general"
    }
    try:
        r = requests.post(url, data=data, headers={"Content-Type": "application/x-www-form-urlencoded"})
        r.raise_for_status()
        return r.json().get("access_token")
    except Exception as e:
        logging.error(f"\u274c Token error: {e}")
        return None

def fetch_hostaway_resource(resource: str, resource_id: int):
    token = get_hostaway_access_token()
    if not token:
        return None
    url = f"{HOSTAWAY_API_BASE}/{resource}/{resource_id}"
    try:
        r = requests.get(url, headers={"Authorization": f"Bearer {token}"})
        r.raise_for_status()
        return r.json()
    except Exception as e:
        logging.error(f"\u274c Fetch {resource} error: {e}")
        return None

def fetch_hostaway_listing(listing_id, fields=None):
    if not listing_id:
        return None
    token = get_hostaway_access_token()
    if not token:
        return None
    url = f"{HOSTAWAY_API_BASE}/listings/{listing_id}?includeResources=1&attachObjects[]=bookingEngineUrls"
    try:
        r = requests.get(url, headers={"Authorization": f"Bearer {token}"})
        r.raise_for_status()
        result = r.json()
        if fields:
            filtered = {k: v for k, v in result.get("result", {}).items() if k in fields}
            return {"result": filtered}
        return result
    except Exception as e:
        logging.error(f"\u274c Fetch listing error: {e}")
        return None

def get_property_info(listing_result: dict, fields: list[str]) -> dict:
    result = listing_result.get("result", {}) if isinstance(listing_result, dict) else {}
    return {field: result.get(field) for field in fields if field in result}

def fetch_hostaway_reservation(reservation_id):
    return fetch_hostaway_resource("reservations", reservation_id)

def fetch_hostaway_conversation(conversation_id):
    token = get_hostaway_access_token()
    if not token:
        return None
    url = f"{HOSTAWAY_API_BASE}/conversations/{conversation_id}?includeScheduledMessages=1"
    try:
        r = requests.get(url, headers={"Authorization": f"Bearer {token}"})
        r.raise_for_status()
        logging.info(f"\u2705 Conversation {conversation_id} fetched with messages.")
        return r.json()
    except Exception as e:
        logging.error(f"\u274c Fetch conversation error: {e}")
        return None

def fetch_conversation_messages(conversation_id):
    obj = fetch_hostaway_conversation(conversation_id)
    if obj and "conversationMessages" in obj:
        return obj["conversationMessages"]
    return []

def send_reply_to_hostaway(conversation_id: str, reply_text: str, communication_type: str = "email") -> bool:
    token = get_hostaway_access_token()
    if not token:
        return False
    url = f"{HOSTAWAY_API_BASE}/conversations/{conversation_id}/messages"
    payload = {
        "body": reply_text,
        "isIncoming": 0,
        "communicationType": communication_type
    }
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json"
    }
    try:
        r = requests.post(url, headers=headers, json=payload)
        r.raise_for_status()
        logging.info(f"\u2705 Sent to Hostaway: {r.text}")
        return True
    except Exception as e:
        logging.error(f"\u274c Send error: {e}")
        return False

def get_cancellation_policy_summary(listing_result, reservation_result):
    policy = reservation_result.get("cancellationPolicy") or listing_result.get("cancellationPolicy")
    if not policy:
        return "No cancellation policy found."
    desc = {
        "flexible": "Flexible: Full refund 1 day prior to arrival.",
        "moderate": "Moderate: Full refund 5 days prior to arrival.",
        "strict": "Strict: 50% refund up to 1 week before arrival."
    }
    policy_text = desc.get(policy, f"Policy: {policy}")
    return policy_text

# --- SQLite Learning Functions ---
def _init_learning_db():
    try:
        conn = sqlite3.connect(LEARNING_DB_PATH)
        c = conn.cursor()
        c.execute('''
            CREATE TABLE IF NOT EXISTS learning_examples (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                guest_message TEXT,
                ai_suggestion TEXT,
                user_reply TEXT,
                listing_id TEXT,
                guest_id TEXT,
                created_at TEXT
            )
        ''')
        c.execute('''
            CREATE TABLE IF NOT EXISTS clarifications (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                conversation_id TEXT,
                guest_message TEXT,
                clarification TEXT,
                tags TEXT,
                created_at TEXT
            )
        ''')
        conn.commit()
        conn.close()
    except Exception as e:
        logging.error(f"\u274c DB init error: {e}")

def store_learning_example(guest_message, ai_suggestion, user_reply, listing_id, guest_id):
    _init_learning_db()
    try:
        conn = sqlite3.connect(LEARNING_DB_PATH)
        c = conn.cursor()
        c.execute(
            '''INSERT INTO learning_examples (guest_message, ai_suggestion, user_reply, listing_id, guest_id, created_at)
               VALUES (?, ?, ?, ?, ?, ?)''',
            (
                guest_message or "",
                ai_suggestion or "",
                user_reply or "",
                str(listing_id) if listing_id else "",
                str(guest_id) if guest_id else "",
                datetime.utcnow().isoformat()
            )
        )
        conn.commit()
        conn.close()
        logging.info("[LEARNING] Example saved to database.")
    except Exception as e:
        logging.error(f"\u274c DB save error: {e}")

def store_clarification_log(conversation_id, guest_message, clarification, tags):
    _init_learning_db()
    try:
        conn = sqlite3.connect(LEARNING_DB_PATH)
        c = conn.cursor()
        c.execute(
            '''INSERT INTO clarifications (conversation_id, guest_message, clarification, tags, created_at)
               VALUES (?, ?, ?, ?, ?)''',
            (
                str(conversation_id) if conversation_id else "",
                guest_message or "",
                clarification or "",
                ",".join(tags) if tags else "",
                datetime.utcnow().isoformat()
            )
        )
        conn.commit()
        conn.close()
        logging.info(f"[CLARIFY] Clarification stored for conversation {conversation_id}")
    except Exception as e:
        logging.error(f"\u274c Clarification DB error: {e}")

def get_similar_learning_examples(guest_message, listing_id):
    _init_learning_db()
    try:
        conn = sqlite3.connect(LEARNING_DB_PATH)
        c = conn.cursor()
        c.execute('''
            SELECT guest_message, ai_suggestion, user_reply FROM learning_examples
            WHERE listing_id = ? AND guest_message LIKE ?
            ORDER BY created_at DESC
            LIMIT 5
        ''', (str(listing_id), f"%{guest_message[:10]}%"))
        results = c.fetchall()
        conn.close()
        return results
    except Exception as e:
        logging.error(f"\u274c DB fetch error: {e}")
        return []

def retrieve_learned_answer(guest_message, listing_id, guest_id=None, cutoff=0.8):
    _init_learning_db()
    try:
        conn = sqlite3.connect(LEARNING_DB_PATH)
        c = conn.cursor()
        c.execute('''
            SELECT guest_message, user_reply, guest_id
            FROM learning_examples
            WHERE listing_id = ?
            ORDER BY created_at DESC
        ''', (str(listing_id),))
        rows = c.fetchall()
        conn.close()
        questions = [row[0] for row in rows]
        matches = get_close_matches(guest_message, questions, n=1, cutoff=cutoff)
        if matches:
            idx = questions.index(matches[0])
            user_reply = rows[idx][1]
            found_guest_id = rows[idx][2]
            if guest_id and found_guest_id == guest_id:
                return user_reply
            if not guest_id:
                return user_reply
        return None
    except Exception as e:
        logging.error(f"\u274c Retrieval error: {e}")
        return None
