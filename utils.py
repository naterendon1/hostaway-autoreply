import os
import requests
import logging
import sqlite3
import json
import time
from datetime import datetime, timedelta
from difflib import get_close_matches
import re
from openai import OpenAI

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

OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
openai_client = OpenAI(api_key=OPENAI_API_KEY)

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

def extract_date_range_from_message(message, reservation=None):
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
    # Holidays (simple, can expand)
    if "christmas" in msg or "xmas" in msg:
        year = datetime.now().year
        return f"{year}-12-20", f"{year}-12-27"
    if "thanksgiving" in msg:
        # Thanksgiving: 4th Thursday of November
        year = datetime.now().year
        november = datetime(year, 11, 1)
        thursdays = [november + timedelta(days=i) for i in range(31) if (november + timedelta(days=i)).weekday() == 3 and (november + timedelta(days=i)).month == 11]
        thanksgiving = thursdays[3]
        return thanksgiving.strftime("%Y-%m-%d"), (thanksgiving + timedelta(days=3)).strftime("%Y-%m-%d")
    if "new year" in msg:
        year = datetime.now().year
        return f"{year}-12-28", f"{year+1}-01-03"
    if "spring break" in msg:
        year = datetime.now().year
        return f"{year}-03-10", f"{year}-03-20"
    if "next weekend" in msg:
        today = datetime.now()
        days_ahead = 5 - today.weekday()
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

def clean_ai_reply(reply: str):
    reply = reply.rstrip(",. ")
    reply = ''.join(c for c in reply if c.isprintable() and (ord(c) < 0x1F300 or ord(c) > 0x1FAD6))
    lower_reply = reply.lower()
    holiday_terms = ["enjoy your holidays", "merry christmas", "happy holidays", "happy new year"]
    if any(term in lower_reply for term in holiday_terms):
        if datetime.now().month != 12:
            for term in holiday_terms:
                reply = re.sub(term, "", reply, flags=re.IGNORECASE)
            reply = ' '.join(reply.split())
            reply = reply.strip()
    for bad in ["Best,", "Best regards,", "[Your Name]", "Sincerely,", "Thanks!", "Thank you!", "All the best,", "Cheers,", "Kind regards,", "—", "--"]:
        reply = reply.replace(bad, "")
    reply = reply.replace("  ", " ").replace("..", ".").strip()
    if "[your name]" in reply.lower():
        reply = reply[:reply.lower().find("[your name]")]
    reply = reply.rstrip(". ").strip()
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

def get_listing_amenities(listing_id):
    """
    Returns a list of amenity names for a given Hostaway listing.
    Returns [] on error or if no amenities.
    """
    # 1. Fetch the listing to get amenities IDs
    listing = fetch_hostaway_listing(listing_id)
    if not listing or "result" not in listing:
        logging.error(f"[AMENITY] Failed to fetch listing {listing_id}")
        return []
    amenities_ids = listing["result"].get("amenities", [])
    if not isinstance(amenities_ids, list):
        logging.warning(f"[AMENITY] Amenities in listing {listing_id} not a list: {amenities_ids}")
        return []

    # 2. Fetch all available amenities from Hostaway
    token = get_hostaway_access_token()
    if not token:
        logging.error("[AMENITY] No Hostaway API token")
        return []
    try:
        url = "https://api.hostaway.com/v1/amenities"
        headers = {"Authorization": f"Bearer {token}"}
        resp = requests.get(url, headers=headers)
        resp.raise_for_status()
        amenities_list = resp.json()
    except Exception as e:
        logging.error(f"[AMENITY] Fetch error: {e}")
        return []

    # 3. Map amenity IDs to names
    id_to_name = {int(a["id"]): a["name"] for a in amenities_list if "id" in a and "name" in a}
    amenity_names = [id_to_name.get(int(aid), f"ID:{aid}") for aid in amenities_ids if isinstance(aid, int) or str(aid).isdigit()]

    return amenity_names

# --- Intent Detection ---
INTENT_LABELS = [
    "booking inquiry",
    "cancellation",
    "general question",
    "complaint",
    "extend stay",
    "amenities",
    "check-in info",
    "check-out info",
    "pricing inquiry",
    "other"
]

def detect_intent(message: str) -> str:
    """
    Uses OpenAI to classify guest messages into predefined intent categories.
    """
    system_prompt = (
        "You are an intent classification assistant for a vacation rental business. "
        "Given a guest message, return ONLY the intent label from this list: "
        f"{', '.join(INTENT_LABELS)}. "
        "Return just the label, nothing else."
    )
    user_prompt = f"Message: {message}"
    try:
        response = openai_client.chat.completions.create(
            model="gpt-4",
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt}
            ],
            max_tokens=10,
            temperature=0
        )
        intent = response.choices[0].message.content.strip().lower()
        for label in INTENT_LABELS:
            if label in intent:
                return label
        return "other"
    except Exception as e:
        logging.error(f"Intent detection failed: {e}")
        return "other"

def get_modal_blocks(guest_name, guest_msg, draft_text, action_id="write_own", checkbox_checked=False):
    """
    Returns a list of Slack Block Kit blocks for guest reply/edit modals.

    - guest_name: str
    - guest_msg: str
    - draft_text: str (initial text in input)
    - action_id: str ("write_own" or "edit")
    - checkbox_checked: bool (pre-select 'Save this answer' checkbox)
    """
    reply_block = {
        "type": "input",
        "block_id": "reply_input",
        "label": {
            "type": "plain_text",
            "text": "Your reply:" if action_id == "write_own" else "Edit below:",
            "emoji": True
        },
        "element": {
            "type": "plain_text_input",
            "action_id": "reply",
            "multiline": True,
            "initial_value": draft_text or ""
        }
    }
    checkbox_block = {
        "type": "input",
        "block_id": "save_answer_block",
        "element": {
            "type": "checkboxes",
            "action_id": "save_answer",
            "options": [{
                "text": {"type": "plain_text", "text": "Save this answer for next time", "emoji": True},
                "value": "save"
            }]
        },
        "label": {"type": "plain_text", "text": "Learning", "emoji": True},
        "optional": True
    }
    if checkbox_checked:
        checkbox_block["element"]["initial_options"] = [{
            "text": {"type": "plain_text", "text": "Save this answer for next time", "emoji": True},
            "value": "save"
        }]
    return [
        {
            "type": "section",
            "block_id": "guest_message_section",
            "text": {"type": "mrkdwn", "text": f"*Guest*: {guest_name}\n*Message*: {guest_msg}"}
        },
        reply_block,
        {
            "type": "actions",
            "block_id": "improve_ai_block",
            "elements": [
                {
                    "type": "button",
                    "action_id": "improve_with_ai",
                    "text": {"type": "plain_text", "text": "Improve with AI", "emoji": True}
                }
            ]
        },
        checkbox_block
    ]

def build_full_prompt(
    guest_message,
    thread_msgs,
    reservation,
    listing,
    calendar_summary,
    intent,
    similar_examples=None,
    extra_instructions=None
):
    """
    Builds the full AI prompt for generating a reply, using conversation thread, reservation,
    listing, calendar, and similar previous examples as context.
    """
    prompt = "The following is the conversation so far (newest last):\n"
    for m in thread_msgs:
        prompt += m + "\n"
    prompt += f"\nReservation info: {reservation}\n"
    prompt += f"Listing info: {listing}\n"
    prompt += f"Calendar info: {calendar_summary}\n"
    prompt += f"Intent: {intent}\n"
    if similar_examples:
        prompt += "\nSimilar previous guest questions and replies:\n"
        for eg in similar_examples:
            prompt += f"Q: {eg[0]}\nA: {eg[2]}\n"
    if extra_instructions:
        prompt += f"\nExtra Instructions: {extra_instructions}\n"
    prompt += (
        "\n\nReply to the most recent guest message above (at the end of the thread) "
        "in a natural, modern, human way, only as needed."
    )
    return prompt


