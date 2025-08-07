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

# --- Intent Labels (always keep above detect_intent!) ---
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

# --- ENVIRONMENT VARIABLE CHECKS ---
REQUIRED_ENV_VARS = [
    "HOSTAWAY_CLIENT_ID",
    "HOSTAWAY_CLIENT_SECRET",
    "GOOGLE_PLACES_API_KEY"
]
missing = [v for v in REQUIRED_ENV_VARS if not os.getenv(v)]
if missing:
    raise RuntimeError(f"Missing required environment variables: {missing}")

HOSTAWAY_CLIENT_ID = os.getenv("HOSTAWAY_CLIENT_ID")
HOSTAWAY_CLIENT_SECRET = os.getenv("HOSTAWAY_CLIENT_SECRET")
HOSTAWAY_API_BASE = os.getenv("HOSTAWAY_API_BASE", "https://api.hostaway.com/v1")
LEARNING_DB_PATH = os.getenv("LEARNING_DB_PATH", "learning.db")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
GOOGLE_API_KEY = os.getenv("GOOGLE_PLACES_API_KEY")
openai_client = OpenAI(api_key=OPENAI_API_KEY)

# --- detect_intent uses the global INTENT_LABELS above! ---
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

# ---- Rest of your code unchanged below this line ----
# ... (keep your functions for hostaway, db, google places, etc. as before)

PROPERTY_LOCATIONS = {
    "crystal_beach": {"lat": 29.4472, "lng": -94.6296, "city": "Crystal Beach, TX"},
    "galveston": {"lat": 29.3013, "lng": -94.7977, "city": "Galveston, TX"},
    "austin": {"lat": 30.2672, "lng": -97.7431, "city": "Austin, TX"},
    "georgetown": {"lat": 30.6333, "lng": -97.6770, "city": "Georgetown, TX"},
}

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

# ... other Hostaway and DB functions go here (unchanged) ...

def get_property_location(listing, reservation):
    if listing and "result" in listing:
        lat = listing["result"].get("latitude")
        lng = listing["result"].get("longitude")
        if lat and lng:
            return float(lat), float(lng)
        address = (listing["result"].get("city") or "").lower()
        if "crystal" in address:
            loc = PROPERTY_LOCATIONS["crystal_beach"]
        elif "galveston" in address:
            loc = PROPERTY_LOCATIONS["galveston"]
        elif "austin" in address:
            loc = PROPERTY_LOCATIONS["austin"]
        elif "georgetown" in address:
            loc = PROPERTY_LOCATIONS["georgetown"]
        else:
            loc = None
        if loc:
            return loc["lat"], loc["lng"]
    return None, None

def detect_place_type(msg):
    location_question_types = {
        "restaurant": "restaurant",
        "restaurants": "restaurant",
        "bar": "bar",
        "bars": "bar",
        "club": "night_club",
        "clubs": "night_club",
        "grocery": "supermarket",
        "shopping": "shopping_mall",
        "things to do": "tourist_attraction",
        "coffee": "cafe",
        "breakfast": "restaurant",
        "lunch": "restaurant",
        "dinner": "restaurant",
        "fish": "restaurant",   # for fishing, could also match "fishing charter"
        "fishing": "fishing",
        "charter": "fishing",
        "supermarket": "supermarket",
        "liquor": "liquor_store"
    }
    for k, v in location_question_types.items():
        if k in msg.lower():
            return v, k
    return None, None

def search_google_places(query, lat, lng, radius=4000, type_hint=None):
    if not GOOGLE_API_KEY or not lat or not lng:
        logging.warning("[PLACES] Missing API key or coordinates.")
        return []
    endpoint = "https://maps.googleapis.com/maps/api/place/nearbysearch/json"
    params = {
        "key": GOOGLE_API_KEY,
        "location": f"{lat},{lng}",
        "radius": radius,
        "keyword": query,
    }
    if type_hint:
        params["type"] = type_hint
    logging.info(f"[PLACES] Calling Google Places: {endpoint} with params: {params}")
    try:
        resp = requests.get(endpoint, params=params)
        logging.info(f"[PLACES] Response status: {resp.status_code}")
        logging.debug(f"[PLACES] Response body: {resp.text[:500]}")
        resp.raise_for_status()
        results = resp.json().get("results", [])
        places = [{
            "name": r.get("name"),
            "address": r.get("vicinity"),
            "rating": r.get("rating")
        } for r in results[:5]]
        logging.info(f"[PLACES] Top results: {places}")
        return places
    except Exception as e:
        logging.error(f"[PLACES] Error calling Google Places: {e}")
        return []

def build_places_summary_block(places, query=None):
    if not places:
        return ""
    summary = []
    if query:
        summary.append(f"Search results for '{query}':")
    for p in places:
        if p["rating"]:
            summary.append(f"- {p['name']} ({p['address']}, rating {p['rating']})")
        else:
            summary.append(f"- {p['name']} ({p['address']})")
    return "\n".join(summary)

def build_full_prompt(
    guest_message,
    thread_msgs,
    reservation,
    listing,
    calendar_summary,
    intent,
    similar_examples,
    extra_instructions=None,  # <-- pass in Google Places summary block here!
    max_messages=12,          # Limit number of messages
    max_field_chars=600       # Truncate very large fields
):
    """
    Compose the full prompt for OpenAI, always referencing last guest/host messages,
    and (if present) dynamic Google Places results right before reply instructions.
    """
    # --- Limit history ---
    recent_msgs = thread_msgs[-max_messages:] if len(thread_msgs) > max_messages else thread_msgs
    prompt = "You are a real human host. Here is the conversation so far (newest last):\n"
    for m in recent_msgs:
        # Optionally truncate single message if very large
        if len(m) > 600:
            prompt += m[:600] + " ...[truncated]\n"
        else:
            prompt += m + "\n"

    # --- Truncate large fields for listing/reservation/calendar ---
    def trunc(s):
        s = str(s)
        return (s[:max_field_chars] + " ...[truncated]") if len(s) > max_field_chars else s

    prompt += (
        f"\nReservation info: {trunc(reservation)}\n"
        f"Listing info: {trunc(listing)}\n"
        f"Calendar info: {trunc(calendar_summary)}\n"
        f"Intent: {intent}\n"
    )

    # --- Truncate similar examples if needed ---
    if similar_examples:
        prompt += "\nSimilar previous guest questions and replies:\n"
        for eg in similar_examples[:3]:  # Only use 3 for brevity
            q, a = trunc(eg[0]), trunc(eg[2])
            prompt += f"Q: {q}\nA: {a}\n"

    # Always add any extra context (Google results, etc) right here:
    if extra_instructions:
        prompt += "\nNearby recommendations (from Google Places):\n" + trunc(extra_instructions) + "\n"

    prompt += (
        "\nReply ONLY as the host to the latest guest message at the end of this conversation. "
        "Always use the actual conversation history above for your reply. "
        "If an item is being sent back by your cleaner, acknowledge the cleaner's favor, not the guest's."
    )
    return prompt


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
        # Map amenity IDs to names (if needed)
        id_to_name = {str(item["id"]): item["name"] for item in amenities_list.get("result", [])}
        return [id_to_name.get(str(aid), f"Unknown({aid})") for aid in amenities_ids]
    except Exception as e:
        logging.error(f"[AMENITY] Fetch error: {e}")
        return []

def get_modal_blocks(guest_name, guest_msg, draft_text="", action_id="edit", checkbox_checked=False):
    """
    Returns Slack modal blocks for editing/writing a reply.
    """
    blocks = [
        {
            "type": "input",
            "block_id": "reply_input",
            "element": {
                "type": "plain_text_input",
                "action_id": "reply",
                "multiline": True,
                "initial_value": draft_text or "",
                "placeholder": {"type": "plain_text", "text": "Write your reply here..."}
            },
            "label": {"type": "plain_text", "text": f"Reply to {guest_name}", "emoji": True}
        },
        {
            "type": "section",
            "block_id": "guest_message",
            "text": {
                "type": "mrkdwn",
                "text": f"*Guest message:*\n>{guest_msg}"
            }
        },
        {
            "type": "input",
            "block_id": "save_answer_block",
            "element": {
                "type": "checkboxes",
                "action_id": "save_answer",
                "options": [
                    {
                        "text": {
                            "type": "plain_text",
                            "text": "Save this reply to suggest for similar future questions",
                            "emoji": True
                        },
                        "value": "save_answer"
                    }
                ],
                "initial_options": [
                    {
                        "text": {
                            "type": "plain_text",
                            "text": "Save this reply to suggest for similar future questions",
                            "emoji": True
                        },
                        "value": "save_answer"
                    }
                ] if checkbox_checked else []
            },
            "label": {
                "type": "plain_text",
                "text": "Save reply",
                "emoji": True
            },
            "optional": True
        }
    ]
    return blocks
