import os
import requests
import logging
import sqlite3
import json
import time
from datetime import datetime, timedelta
from difflib import get_close_matches
import re

# --- ENVIRONMENT VARIABLE CHECKS ---
REQUIRED_ENV_VARS = [
    "HOSTAWAY_CLIENT_ID",
    "HOSTAWAY_CLIENT_SECRET"
]
missing = [v for v in REQUIRED_ENV_VARS if not os.getenv(v)]
if missing:
    raise RuntimeError(f"Missing required environment variables: {missing}")

HOSTAWAY_CLIENT_ID = os.getenv("HOSTAWAY_CLIENT_ID")
HOSTAWAY_CLIENT_SECRET = os.getenv("HOSTAWAY_CLIENT_SECRET")
HOSTAWAY_API_BASE = os.getenv("HOSTAWAY_API_BASE", "https://api.hostaway.com/v1")
LEARNING_DB_PATH = os.getenv("LEARNING_DB_PATH", "learning.db")

# --- HOSTAWAY TOKEN CACHE ---
_HOSTAWAY_TOKEN_CACHE = {"access_token": None, "expires_at": 0}

def get_hostaway_access_token() -> str:
    global _HOSTAWAY_TOKEN_CACHE
    now = time.time()
    if _HOSTAWAY_TOKEN_CACHE["access_token"] and now < _HOSTAWAY_TOKEN_CACHE["expires_at"]:
        return _HOSTAWAY_TOKEN_CACHE["access_token"]

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
        resp = r.json()
        _HOSTAWAY_TOKEN_CACHE["access_token"] = resp.get("access_token")
        _HOSTAWAY_TOKEN_CACHE["expires_at"] = now + 3480
        return _HOSTAWAY_TOKEN_CACHE["access_token"]
    except Exception as e:
        logging.error(f"❌ Token error: {e}")
        return None

def fetch_hostaway_calendar(listing_id, start_date, end_date):
    token = get_hostaway_access_token()
    if not token:
        return None
    url = f"{HOSTAWAY_API_BASE}/listings/{listing_id}/calendar?startDate={start_date}&endDate={end_date}"
    try:
        r = requests.get(url, headers={"Authorization": f"Bearer {token}"})
        r.raise_for_status()
        data = r.json()
        # Defensive: Sometimes Hostaway gives a list, sometimes dict with 'result' key
        if isinstance(data, dict) and "result" in data and isinstance(data["result"], list):
            return data["result"]
        elif isinstance(data, dict) and "result" in data and isinstance(data["result"], dict):
            return data["result"].get("calendar", [])
        elif isinstance(data, list):
            return data
        else:
            return []
    except Exception as e:
        logging.error(f"❌ Fetch calendar error: {e}")
        return []

def is_date_available(calendar_days, date_str):
    for day in calendar_days:
        if day.get("date") == date_str:
            if "isAvailable" in day:
                return bool(day["isAvailable"])
            return day.get("status", "") == "available"
    return False

def next_available_dates(calendar_days, days_wanted=5):
    available = []
    for day in calendar_days:
        if ("isAvailable" in day and day["isAvailable"]) or (day.get("status", "") == "available"):
            available.append(day["date"])
            if len(available) >= days_wanted:
                break
    return available

# --- NEW: Extract date ranges or calendar intent from guest messages ---
def extract_date_range_from_message(message, reservation=None):
    """
    Attempts to extract a date range from a guest message.
    Returns (start_date, end_date) as YYYY-MM-DD or (None, None) if not found.
    """
    # MM/DD/YYYY - MM/DD/YYYY or Month D - Month D
    date_patterns = [
        r'(\d{1,2}/\d{1,2}/\d{4})\s*(?:to|-|through|until)\s*(\d{1,2}/\d{1,2}/\d{4})',
        r'([A-Za-z]+ \d{1,2})\s*(?:to|-|through|until)\s*([A-Za-z]+ \d{1,2})',
        r'from ([A-Za-z]+ \d{1,2}) to ([A-Za-z]+ \d{1,2})',
    ]
    msg = message.lower()
    for pat in date_patterns:
        m = re.search(pat, msg)
        if m:
            try:
                start, end = m.group(1), m.group(2)
                # Try parsing with/without year
                try:
                    start_date = datetime.strptime(start, "%m/%d/%Y")
                    end_date = datetime.strptime(end, "%m/%d/%Y")
                except:
                    now = datetime.now()
                    year = now.year
                    start_date = datetime.strptime(f"{start} {year}", "%B %d %Y")
                    end_date = datetime.strptime(f"{end} {year}", "%B %d %Y")
                return start_date.strftime("%Y-%m-%d"), end_date.strftime("%Y-%m-%d")
            except Exception:
                continue
    # Holidays (simple)
    if "christmas" in msg or "xmas" in msg:
        year = datetime.now().year
        return f"{year}-12-20", f"{year}-12-27"
    if "new year" in msg:
        year = datetime.now().year
        return f"{year}-12-28", f"{year+1}-01-03"
    if "spring break" in msg:
        year = datetime.now().year
        return f"{year}-03-10", f"{year}-03-20"
    if "next weekend" in msg:
        today = datetime.now()
        days_ahead = 5 - today.weekday()  # Next Saturday
        if days_ahead <= 0: days_ahead += 7
        start = today + timedelta(days=days_ahead)
        end = start + timedelta(days=2)
        return start.strftime("%Y-%m-%d"), end.strftime("%Y-%m-%d")
    if reservation and any(w in msg for w in ["extend", "extra night", "stay longer"]):
        check_out = reservation.get("departureDate")
        if check_out:
            dt = datetime.strptime(check_out, "%Y-%m-%d")
            return check_out, (dt + timedelta(days=1)).strftime("%Y-%m-%d")
    # Default: next 14 days
    today = datetime.now().strftime("%Y-%m-%d")
    week = (datetime.now() + timedelta(days=13)).strftime("%Y-%m-%d")
    return today, week

# --- Rest of your utils (fetch_hostaway_listing, fetch_hostaway_reservation, etc.) unchanged ---
# ... (keep your existing code here)


def get_hostaway_access_token() -> str:
    global _HOSTAWAY_TOKEN_CACHE
    now = time.time()
    if _HOSTAWAY_TOKEN_CACHE["access_token"] and now < _HOSTAWAY_TOKEN_CACHE["expires_at"]:
        return _HOSTAWAY_TOKEN_CACHE["access_token"]

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
        resp = r.json()
        _HOSTAWAY_TOKEN_CACHE["access_token"] = resp.get("access_token")
        # Hostaway tokens typically last an hour, use 58 mins to be safe
        _HOSTAWAY_TOKEN_CACHE["expires_at"] = now + 3480
        return _HOSTAWAY_TOKEN_CACHE["access_token"]
    except Exception as e:
        logging.error(f"❌ Token error: {e}")
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
        logging.error(f"❌ Fetch {resource} error: {e}")
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
        logging.error(f"❌ Fetch listing error: {e}")
        return None

def get_property_info(listing_result: dict, fields: list) -> dict:
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
        logging.info(f"✅ Conversation {conversation_id} fetched with messages.")
        resp_json = r.json()
        logging.info(f"[DEBUG] Full conversation object: {json.dumps(resp_json, indent=2)[:1000]}")
        return resp_json
    except Exception as e:
        logging.error(f"❌ Fetch conversation error: {e}")
        return None

def fetch_conversation_messages(conversation_id):
    obj = fetch_hostaway_conversation(conversation_id)
    if obj and "result" in obj and "conversationMessages" in obj["result"]:
        return obj["result"]["conversationMessages"]
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
        logging.info(f"✅ Sent to Hostaway: {r.text}")
        return True
    except Exception as e:
        logging.error(f"❌ Send error: {e}")
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
    return desc.get(policy, f"Policy: {policy}")

# --- CENTRALIZED AI REPLY CLEANING ---
def clean_ai_reply(reply: str):
    bad_signoffs = [
        "Enjoy your meal", "Enjoy your meals", "Enjoy!", "Best,", "Best regards,",
        "Cheers,", "Sincerely,", "[Your Name]", "Best", "Sincerely"
    ]
    for signoff in bad_signoffs:
        reply = reply.replace(signoff, "")
    lines = reply.split('\n')
    filtered_lines = []
    for line in lines:
        stripped = line.strip()
        if any(stripped.lower().startswith(s.lower().replace(",", "")) for s in ["Best", "Cheers", "Sincerely"]):
            continue
        if "[Your Name]" in stripped:
            continue
        filtered_lines.append(line)
    reply = ' '.join(filtered_lines)
    reply = ' '.join(reply.split())
    reply = reply.strip().replace(" ,", ",").replace(" .", ".")
    # Remove trailing punctuation and any emojis
    reply = reply.rstrip(",. ")
    reply = ''.join(c for c in reply if c.isprintable() and (ord(c) < 0x1F300 or ord(c) > 0x1FAD6)) # crude emoji filter
    return reply

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
        logging.error(f"❌ DB init error: {e}")

# Initialize DB only once at import
_init_learning_db()

def store_learning_example(guest_message, ai_suggestion, user_reply, listing_id, guest_id):
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
        logging.error(f"❌ DB save error: {e}")

def store_clarification_log(conversation_id, guest_message, clarification, tags):
    try:
        conn = sqlite3.connect(LEARNING_DB_PATH)
        c = conn.cursor()
        c.execute(
            '''INSERT INTO clarifications (conversation_id, guest_message, clarification, tags, created_at)
               VALUES (?, ?, ?, ?, ?)''',
            (
                str(conversation_id),
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
        logging.error(f"❌ Clarification DB error: {e}")

def get_similar_learning_examples(guest_message, listing_id):
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
        logging.error(f"❌ DB fetch error: {e}")
        return []

def retrieve_learned_answer(guest_message, listing_id, guest_id=None, cutoff=0.8):
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
        logging.error(f"❌ Retrieval error: {e}")
        return None
