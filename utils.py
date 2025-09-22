# utils.py — consolidated & fixed

import os
import re
import json
import time
import logging
import sqlite3
from datetime import datetime, timedelta, date as _date
from difflib import get_close_matches
from typing import Any, Dict, List, Optional, Tuple, Literal
from places import should_fetch_local_recs, build_local_recs

import requests
from openai import OpenAI

OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
_router_client = OpenAI(api_key=OPENAI_API_KEY) if OPENAI_API_KEY else None

PrimaryIntent = Literal[
    "arrival_update", "trash_help", "accessibility", "food_recs",
    "parking", "check_in_help", "check_out_help", "house_rules",
    "booking_question", "other"
]

def route_message(msg: str) -> Dict[str, Any]:
    """
    Returns { "summary": str, "primary_intent": PrimaryIntent, "secondary": [..] }.
    JSON-only. No chain-of-thought.
    """
    if not _router_client:
        # simple fallback: keywordy but with strong negatives
        text = msg.lower()
        if any(k in text for k in ["restaurant", "eat", "dinner", "breakfast", "coffee", "food"]) \
           and not any(k in text for k in ["trash", "garbage", "disabled", "elevator", "access", "portal"]):
            intent = "food_recs"
        elif any(k in text for k in ["trash", "garbage", "bins"]):
            intent = "trash_help"
        elif any(k in text for k in ["disabled", "wheelchair", "elevator", "accessible", "accessibility"]):
            intent = "accessibility"
        else:
            intent = "other"
        return {"summary": msg[:280], "primary_intent": intent, "secondary": []}

    sys = (
        "You are an intent router for short guest messages to a vacation rental host. "
        "You MUST return concise JSON with fields: summary, primary_intent, secondary. "
        "Primary intents: arrival_update, trash_help, accessibility, food_recs, parking, "
        "check_in_help, check_out_help, house_rules, booking_question, other. "
        "Rules: infer intent from the whole message, not keywords alone. "
        "If a guest mentions trash or accessibility, those beat food_recs. "
        "If multiple intents exist, choose the most urgent practical one as primary."
    )
    user = f"Guest message:\n{msg}\n\nReturn JSON only."

    resp = _router_client.chat.completions.create(
        model="gpt-4o-mini",
        response_format={"type": "json_object"},
        messages=[{"role":"system","content":sys},{"role":"user","content":user}],
        temperature=0
    )
    out = resp.choices[0].message.content or "{}"
    try:
        data = json.loads(out)
    except Exception:
        data = {"summary": msg[:280], "primary_intent": "other", "secondary": []}
    return {
        "summary": data.get("summary", msg[:280]),
        "primary_intent": data.get("primary_intent", "other"),
        "secondary": data.get("secondary", []),
    }

def make_suggested_reply(guest_message: str, context: Dict[str, Any]) -> Tuple[str, str]:
    """
    Returns (reply_text, detected_intent).
    - Routes the message to get an intent
    - Optionally fetches local POIs if intent == 'food_recs'
    - Calls OpenAI with strict guardrails
    - Cleans/sanitizes the reply so it doesn't apologize or go off-topic
    """
    # 1) Route
    routing = route_message(guest_message)
    intent: str = routing.get("primary_intent", "other")

    # 2) Only allow POI injection for food_recs
    local_recs: List[Dict[str, Any]] = []
    try:
        if intent == "food_recs" and should_fetch_local_recs(guest_message):
            lat = (context.get("location") or {}).get("lat")
            lng = (context.get("location") or {}).get("lng")
            if lat is not None and lng is not None:
                local_recs = build_local_recs(lat, lng, guest_message)[:4]
    except Exception as e:
        logging.warning(f"[make_suggested_reply] local recs failed: {e}")
        local_recs = []

    # 3) Draft with strict guardrails
    sys = (
        "You write short replies to guests for a vacation rental. "
        "ALWAYS read the entire message; do not fixate on a single keyword. "
        "If the message mentions trash or accessibility, address that first. "
        "If two actionable items exist, address both briefly. "
        "Do not pivot to unrelated topics. No greetings, no sign-offs, no emojis. "
        "Be concrete and concise."
    )
    facts = {
        "intent": intent,
        "recs_count": len(local_recs),
        "recs": local_recs,
    }
    user = (
        f"GUEST_MESSAGE:\n{guest_message}\n\n"
        f"FACTS_JSON:\n{json.dumps(facts, ensure_ascii=False)}\n\n"
        "Write the reply text ONLY (no labels). Use bullets only if listing steps or multiple options."
    )

    reply_text = ""
    if openai_client:
        try:
            resp = openai_client.chat.completions.create(
                model="gpt-4o-mini",
                messages=[{"role": "system", "content": sys}, {"role": "user", "content": user}],
                temperature=0.2,
            )
            reply_text = (resp.choices[0].message.content or "").strip()
        except Exception as e:
            logging.error(f"[make_suggested_reply] OpenAI error: {e}")

    # Heuristic fallback if model failed/disabled
    if not reply_text:
        if intent == "trash_help":
            reply_text = (
                "Thanks for flagging that. Please use the bins by the driveway; if they’re full, tie bags and place them next to the cans. "
                "Pickup is early morning—we’ll notify the service if overflow continues."
            )
        elif intent == "accessibility":
            reply_text = (
                "Thanks for checking. We don’t have a freight elevator. If you need step-free access, "
                "I can share the most accessible route and nearby options."
            )
        elif intent == "food_recs" and local_recs:
            lines = []
            for r in local_recs[:3]:
                name = r.get("name") or "Option"
                rating = r.get("rating")
                reviews = r.get("reviews")
                approx = r.get("approx_time") or r.get("approx_distance")
                bits = [name]
                if rating: bits.append(f"{rating}★")
                if reviews: bits.append(f"({reviews})")
                if approx: bits.append(f"~{approx}")
                lines.append("- " + " ".join(bits))
            reply_text = "Nearby picks:\n" + "\n".join(lines) if lines else "A few nearby spots look good."
        else:
            reply_text = "Got it—happy to help. Can you share a bit more detail so I can point you the right way?"

    # Final tidy & guardrails
    reply_text = clean_ai_reply(reply_text)
    reply_text = sanitize_ai_reply(reply_text, guest_message)

    return reply_text, intent


# --------------------------- Config / Env ---------------------------

HOSTAWAY_API_BASE = os.getenv("HOSTAWAY_API_BASE", "https://api.hostaway.com/v1")
HOSTAWAY_CLIENT_ID = os.getenv("HOSTAWAY_CLIENT_ID")
HOSTAWAY_CLIENT_SECRET = os.getenv("HOSTAWAY_CLIENT_SECRET")

LEARNING_DB_PATH = os.getenv("LEARNING_DB_PATH", "learning.db")

OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
openai_client = OpenAI(api_key=OPENAI_API_KEY) if OPENAI_API_KEY else None

GOOGLE_API_KEY = os.getenv("GOOGLE_PLACES_API_KEY") or ""
GOOGLE_DISTANCE_MATRIX_API_KEY = os.getenv("GOOGLE_DISTANCE_MATRIX_API_KEY") or ""

if not HOSTAWAY_CLIENT_ID or not HOSTAWAY_CLIENT_SECRET:
    logging.warning("HOSTAWAY client env vars are missing; API calls will fail.")

if not OPENAI_API_KEY:
    logging.warning("OPENAI_API_KEY missing; intent classification will be disabled.")

if not GOOGLE_API_KEY:
    logging.info("GOOGLE_PLACES_API_KEY not set; places features disabled.")

if not GOOGLE_DISTANCE_MATRIX_API_KEY:
    logging.info("GOOGLE_DISTANCE_MATRIX_API_KEY not set; distance/time features disabled.")

# --------------------------- Small helpers ---------------------------

def can_share_access(meta: dict, today: Optional[_date] = None) -> bool:
    """
    Only allow sharing access codes for confirmed bookings (new/modified)
    and within a short window before check-in (<= 2 days by default).
    """
    today = today or _date.today()
    status = (meta.get("reservation_status") or "").strip().lower()
    if status not in {"new", "modified"}:
        return False

    ci_raw = meta.get("check_in")
    if not ci_raw:
        return False
    try:
        ci = _date.fromisoformat(str(ci_raw)[:10])
    except Exception:
        return False

    return (ci - today).days <= 2


# --------------------------- Hostaway auth/cache ---------------------------

_HOSTAWAY_TOKEN_CACHE: Dict[str, Any] = {"access_token": None, "expires_at": 0.0}

def get_hostaway_access_token() -> Optional[str]:
    global _HOSTAWAY_TOKEN_CACHE
    now = time.time()
    if _HOSTAWAY_TOKEN_CACHE["access_token"] and now < _HOSTAWAY_TOKEN_CACHE["expires_at"]:
        return _HOSTAWAY_TOKEN_CACHE["access_token"]
    if not (HOSTAWAY_CLIENT_ID and HOSTAWAY_CLIENT_SECRET):
        return None
    url = f"{HOSTAWAY_API_BASE}/accessTokens"
    data = {
        "grant_type": "client_credentials",
        "client_id": HOSTAWAY_CLIENT_ID,
        "client_secret": HOSTAWAY_CLIENT_SECRET,
        "scope": "general",
    }
    try:
        r = requests.post(url, data=data, headers={"Content-Type": "application/x-www-form-urlencoded"}, timeout=15)
        r.raise_for_status()
        resp = r.json()
        _HOSTAWAY_TOKEN_CACHE["access_token"] = resp.get("access_token")
        _HOSTAWAY_TOKEN_CACHE["expires_at"] = now + 3480  # ~58 minutes
        return _HOSTAWAY_TOKEN_CACHE["access_token"]
    except Exception as e:
        logging.error(f"❌ Hostaway token error: {e}")
        return None


# --------------------------- Hostaway API helpers ---------------------------

def fetch_hostaway_resource(resource: str, resource_id: int) -> Optional[Dict[str, Any]]:
    t = get_hostaway_access_token()
    if not t:
        return None
    url = f"{HOSTAWAY_API_BASE}/{resource}/{resource_id}"
    try:
        r = requests.get(url, headers={"Authorization": f"Bearer {t}"}, timeout=15)
        r.raise_for_status()
        return r.json()
    except Exception as e:
        logging.error(f"❌ Fetch {resource} error: {e}")
        return None

def fetch_hostaway_listing(listing_id: Optional[int], fields: Optional[List[str]] = None) -> Optional[Dict[str, Any]]:
    if not listing_id:
        return None
    t = get_hostaway_access_token()
    if not t:
        return None
    url = f"{HOSTAWAY_API_BASE}/listings/{listing_id}?includeResources=1&attachObjects[]=bookingEngineUrls"
    try:
        r = requests.get(url, headers={"Authorization": f"Bearer {t}"}, timeout=15)
        r.raise_for_status()
        result = r.json()
        if fields:
            filtered = {k: v for k, v in (result.get("result") or {}).items() if k in fields}
            return {"result": filtered}
        return result
    except Exception as e:
        logging.error(f"❌ Fetch listing error: {e}")
        return None

def fetch_hostaway_reservation(reservation_id: Optional[int]) -> Optional[Dict[str, Any]]:
    if not reservation_id:
        return None
    return fetch_hostaway_resource("reservations", reservation_id)

def fetch_hostaway_conversation(conversation_id: Optional[int]) -> Optional[Dict[str, Any]]:
    t = get_hostaway_access_token()
    if not t or not conversation_id:
        return None
    url = f"{HOSTAWAY_API_BASE}/conversations/{conversation_id}?includeScheduledMessages=1"
    try:
        r = requests.get(url, headers={"Authorization": f"Bearer {t}"}, timeout=15)
        r.raise_for_status()
        logging.info(f"✅ Conversation {conversation_id} fetched with messages.")
        return r.json()
    except Exception as e:
        logging.error(f"❌ Fetch conversation error: {e}")
        return None

def fetch_conversation_messages(conversation_id: Optional[int]) -> List[Dict[str, Any]]:
    obj = fetch_hostaway_conversation(conversation_id)
    if obj and "result" in obj and "conversationMessages" in obj["result"]:
        return obj["result"]["conversationMessages"] or []
    return []

def send_reply_to_hostaway(conversation_id: str, reply_text: str, communication_type: str = "email") -> bool:
    t = get_hostaway_access_token()
    if not t or not conversation_id or not reply_text:
        return False
    url = f"{HOSTAWAY_API_BASE}/conversations/{conversation_id}/messages"
    payload = {"body": reply_text, "isIncoming": 0, "communicationType": communication_type}
    headers = {"Authorization": f"Bearer {t}", "Content-Type": "application/json"}
    try:
        r = requests.post(url, headers=headers, json=payload, timeout=15)
        r.raise_for_status()
        logging.info(f"✅ Sent to Hostaway: {r.text}")
        return True
    except Exception as e:
        logging.error(f"❌ Send error: {e}")
        return False


# --------------------------- Access / Policy extractors ---------------------------

def extract_access_details(listing_result: Optional[Dict[str, Any]], reservation_result: Optional[Dict[str, Any]]) -> Dict[str, Optional[str]]:
    """
    Return {door_code, arrival_instructions}, preferring reservation-level fields.
    """
    R = (reservation_result or {}).get("result", {}) or {}
    L = (listing_result or {}).get("result", {}) or {}

    code = (
        R.get("doorCode") or R.get("accessCode") or R.get("keypadCode")
        or L.get("doorCode") or L.get("accessCode") or L.get("keypadCode") or L.get("gateCode")
    )
    instructions = (
        R.get("arrivalInstructions") or R.get("checkInInstructions")
        or L.get("arrivalInstructions") or L.get("checkInInstructions") or L.get("houseManual")
    )

    if isinstance(instructions, dict):
        instructions = instructions.get("text") or instructions.get("url") or str(instructions)

    return {
        "door_code": (str(code).strip() if code else None),
        "arrival_instructions": (str(instructions).strip() if instructions else None),
    }

def extract_pet_policy(listing_result: Optional[Dict[str, Any]]) -> Dict[str, Optional[Any]]:
    L = (listing_result or {}).get("result", {}) or {}
    pets_allowed = L.get("petsAllowed")
    pet_fee = L.get("petFee") or L.get("pet_fee")
    refundable = L.get("petDepositRefundable")

    try:
        pet_fee = float(pet_fee) if pet_fee is not None else None
    except Exception:
        pet_fee = None

    if isinstance(pets_allowed, str):
        pets_allowed = pets_allowed.lower() in {"true", "yes", "1"}
    if isinstance(refundable, str):
        refundable = refundable.lower() in {"true", "yes", "1"}

    return {
        "pets_allowed": pets_allowed if isinstance(pets_allowed, bool) else None,
        "pet_fee": pet_fee,
        "pet_deposit_refundable": refundable if isinstance(refundable, bool) else None,
    }


# --------------------------- Google Places / Distance ---------------------------

DISTANCE_TRIGGERS = [
    "how far", "distance to", "distance from", "how long to", "drive to",
    "drive time", "driving time", "travel time", "how long is the drive",
]

def extract_destination_from_message(msg: str) -> Optional[str]:
    mlow = (msg or "").lower()
    if not any(t in mlow for t in DISTANCE_TRIGGERS):
        return None
    cleaned = re.sub(
        r"(how\s+far\s+is|how\s+far\s+to|distance\s+to|distance\s+from|how\s+long\s+to|drive(\s+time)?\s+to|driving\s+time\s+to|travel\s+time\s+to)\s+",
        "",
        mlow,
        flags=re.IGNORECASE,
    )
    cleaned = re.sub(r"^\s*(the|a|an)\s+", "", cleaned, flags=re.IGNORECASE).strip(" ?!.,:")
    return cleaned if len(cleaned) >= 3 else None

def resolve_place_textsearch(query: str, lat: Optional[float] = None, lng: Optional[float] = None) -> Optional[Dict[str, Any]]:
    if not GOOGLE_API_KEY:
        return None
    url = "https://maps.googleapis.com/maps/api/place/textsearch/json"
    params: Dict[str, Any] = {"key": GOOGLE_API_KEY, "query": query}
    if lat and lng:
        params["location"] = f"{lat},{lng}"
        params["radius"] = 30000  # bias within ~30km
    try:
        resp = requests.get(url, params=params, timeout=10)
        resp.raise_for_status()
        j = resp.json()
        results = j.get("results") or []
        if not results:
            return None
        top = results[0]
        geom = (top.get("geometry") or {}).get("location") or {}
        return {"name": top.get("name") or query, "place_id": top.get("place_id"), "lat": geom.get("lat"), "lng": geom.get("lng")}
    except Exception as e:
        logging.error(f"[PLACES] TextSearch error: {e}")
        return None

def get_distance_drive_time(origin_lat: float, origin_lng: float, destination: str, units: str = "imperial") -> str:
    if not GOOGLE_DISTANCE_MATRIX_API_KEY:
        return "Distance service is not configured."
    endpoint = "https://maps.googleapis.com/maps/api/distancematrix/json"
    params = {
        "origins": f"{origin_lat},{origin_lng}",
        "destinations": destination,
        "mode": "driving",
        "departure_time": "now",
        "units": units,
        "key": GOOGLE_DISTANCE_MATRIX_API_KEY,
    }
    try:
        r = requests.get(endpoint, params=params, timeout=10)
        r.raise_for_status()
        data = r.json()
        rows = data.get("rows") or []
        if not rows or not rows[0].get("elements"):
            return f"Sorry, I couldn’t get distance to {destination}."
        el = rows[0]["elements"][0]
        if el.get("status") != "OK":
            return f"Sorry, I couldn’t get distance to {destination}."
        dist = (el.get("distance") or {}).get("text")
        dur = (el.get("duration_in_traffic") or {}).get("text") or (el.get("duration") or {}).get("text")
        return f"{destination} is about {dur} by car ({dist})."
    except Exception as e:
        logging.error(f"[DISTANCE] Matrix error: {e}")
        return f"Sorry, there was a problem calculating distance to {destination}."

def haversine_fallback_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    from math import radians, sin, cos, asin, sqrt
    R = 6371.0
    dlat = radians(lat2 - lat1)
    dlon = radians(lon2 - lon1)
    a = sin(dlat / 2) ** 2 + cos(radians(lat1)) * cos(radians(lat2)) * sin(dlon / 2) ** 2
    return 2 * R * asin(sqrt(a))

def detect_place_type(msg: str) -> Tuple[Optional[str], Optional[str]]:
    mapping = {
        "restaurant": "restaurant", "restaurants": "restaurant",
        "bar": "bar", "bars": "bar",
        "club": "night_club", "clubs": "night_club",
        "grocery": "supermarket", "supermarket": "supermarket",
        "shopping": "shopping_mall",
        "things to do": "tourist_attraction",
        "coffee": "cafe",
        "breakfast": "restaurant", "lunch": "restaurant", "dinner": "restaurant",
        "liquor": "liquor_store",
    }
    msgl = (msg or "").lower()
    for k, v in mapping.items():
        if k in msgl:
            return v, k
    return None, None

def search_google_places(query: str, lat: Optional[float], lng: Optional[float], radius: int = 4000, type_hint: Optional[str] = None) -> List[Dict[str, Any]]:
    if not (GOOGLE_API_KEY and lat and lng):
        return []
    endpoint = "https://maps.googleapis.com/maps/api/place/nearbysearch/json"
    params: Dict[str, Any] = {"key": GOOGLE_API_KEY, "location": f"{lat},{lng}", "radius": radius, "keyword": query}
    VALID_TYPES = {"restaurant", "bar", "night_club", "supermarket", "shopping_mall", "tourist_attraction", "cafe", "liquor_store"}
    if type_hint in VALID_TYPES:
        params["type"] = type_hint
    try:
        resp = requests.get(endpoint, params=params, timeout=10)
        resp.raise_for_status()
        results = resp.json().get("results", [])[:5]
        return [{"name": r.get("name"), "address": r.get("vicinity"), "rating": r.get("rating")} for r in results]
    except Exception as e:
        logging.error(f"[PLACES] Nearby error: {e}")
        return []

def build_places_summary_block(places: List[Dict[str, Any]], query: Optional[str] = None) -> str:
    if not places:
        return ""
    lines = []
    if query:
        lines.append(f"Search results for '{query}':")
    for p in places:
        if p.get("rating"):
            lines.append(f"- {p['name']} ({p['address']}, rating {p['rating']})")
        else:
            lines.append(f"- {p['name']} ({p['address']})")
    return "\n".join(lines)


# --------------------------- Text normalization ---------------------------

BANNED_PHRASES = [
    "thank you for your patience",
    "we apologize for any inconvenience",
    "kindly note",
    "please be advised",
    "sincerely,",
    "best regards",
]

_CONTRACTIONS = [
    (r"\bdo not\b", "don't"),
    (r"\bdoes not\b", "doesn't"),
    (r"\bdid not\b", "didn't"),
    (r"\bit is\b", "it's"),
    (r"\bwe are\b", "we're"),
    (r"\bwe will\b", "we'll"),
    (r"\bwe have\b", "we've"),
    (r"\byou are\b", "you're"),
    (r"\bthat is\b", "that's"),
    (r"\bthere is\b", "there's"),
    (r"\bI will\b", "I'll"),
    (r"\bI have\b", "I've"),
    (r"\bcannot\b", "can't"),
]

def _preserve_case(src: str, repl: str) -> str:
    return repl[:1].upper() + repl[1:] if src[:1].isupper() else repl

def _apply_contractions(txt: str) -> str:
    s = txt
    for pattern, repl in _CONTRACTIONS:
        s = re.sub(pattern, lambda m: _preserve_case(m.group(0), repl), s, flags=re.IGNORECASE)
    return s

def clean_ai_reply(text: str) -> str:
    """
    Gentle normalizer for AI drafts:
    - Normalize smart punctuation & invisible spaces.
    - Keep lists intact; light spacing around punctuation.
    - Remove sign-offs and banned boilerplate.
    - Apply contractions.
    - Avoid semantic changes.
    """
    if not isinstance(text, str):
        return ""

    s = text.translate(str.maketrans({
        "–": "—", "‒": "—",
        "“": '"', "”": '"', "„": '"',
        "’": "'", "‘": "'",
        "…": "...",
        "\u00A0": " ", "\u2009": " ", "\u200A": " ", "\u202F": " ",
        "\u200B": "", "\u2060": "", "\uFEFF": "",
    }))

    is_listy = bool(re.search(r"(^|\n)\s*[-•]\s+", s))
    s = re.sub(r"\s*—\s*", " — ", s)

    if not is_listy:
        s = re.sub(r"(?<!\d)\.(?!\d)\s*", ". ", s)   # periods not in decimals
        s = re.sub(r"(?<!\d),(?!\d)\s*", ", ", s)    # commas not in thousands
        s = re.sub(r"\s*([!?;:])\s*", r"\1 ", s)
        s = re.sub(r"\s{2,}", " ", s)
    else:
        s = re.sub(r"[ \t]+", " ", s)

    s = re.sub(r" \n", "\n", s)

    for bad in [r"\bBest regards,?", r"\bBest,?", r"\bSincerely,?", r"\bAll the best,?", r"\bCheers,?", r"\bKind regards,?"]:
        s = re.sub(bad, "", s, flags=re.IGNORECASE)

    low = s.lower()
    for p in BANNED_PHRASES:
        idx = low.find(p)
        if idx != -1:
            s = s[:idx].strip()
            break

    s = _apply_contractions(s)
    s = re.sub(r"\s+", " ", s).strip()
    if not is_listy:
        s = s.rstrip(". ").strip()
    return s


# --------------------------- Reply sanitizer (fixed) ---------------------------

_ISSUE_TRIGGERS = [
    "dirty", "unclean", "mess", "messy", "bugs", "roaches", "ants", "mold",
    "leak", "broken", "smell", "stain", "hair", "not clean", "cleaning issue",
    "trash", "disgust", "filthy", "soiled",
]

def _looks_like_issue(text: str) -> bool:
    t = (text or "").lower()
    return any(k in t for k in _ISSUE_TRIGGERS)

def _looks_informational(text: str) -> bool:
    """
    True if guest is just stating plans or excitement, not asking for anything.
    Heuristic only; we DO NOT change content based on this in sanitizer anymore.
    """
    t = (text or "").strip().lower()
    if "?" in t:
        return False
    if any(kw in t for kw in ["can you", "could you", "please", "send", "need", "what time", "how do", "where do"]):
        return False
    return True

# --- Off-topic dining heuristics (add near your other regex constants) ---
DINING_WORDS = re.compile(r"\b(restaurant|restaurants|eat|dinner|lunch|breakfast|coffee|cafe|caf\u00e9|bar|brewery|bistro)\b", re.I)
BLOCKING_TOPICS = re.compile(r"\b(trash|garbage|bin[s]?|dumpster|disabled|wheelchair|elevator|accessib|ramp|portal|code|lock|door\s*code|check[- ]?in|check[- ]?out|parking|park|driveway|garage)\b", re.I)
# Bullet lines that look like local recs (begin with -/• and contain dining words)
DINING_BULLET = re.compile(r"(?im)^\s*[-•]\s.*\b(restaurant|breakfast|coffee|cafe|bar|brewery|bistro|pizza|taco|sushi|steak|italian|thai|mexican)\b.*$")

def sanitize_ai_reply(reply: str, guest_msg: str) -> str:
    """
    Conservative scrub, layered:
      1) If the guest asked about a *blocking* operational topic (trash, access, parking, etc.),
         strip dining bullets and dining-y filler from the reply.
      2) If the reply contains bulleted dining recs but the guest didn't mention dining at all,
         strip just those dining bullets.
      3) Remove unprompted apologies & cleaner offers when the guest didn't report an issue.
      4) Whitespace tidy.
    Never inject new content; only remove.
    """
    if not reply:
        return reply

    r = reply.strip()
    g = (guest_msg or "").lower()

    # --- 1) Block dining when the guest asked an operational question ---
    if BLOCKING_TOPICS.search(g):
        # remove dining-looking bullet lines
        r = re.sub(DINING_BULLET, "", r).strip()
        # remove short “nearby picks” one-liners
        r = re.sub(r"(?is)\b(nearby|local)\s+(picks|spots|places)\b.*?$", "", r).strip()

    # --- 2) If reply has dining bullets but guest didn't ask about dining, drop them ---
    if DINING_BULLET.search(r) and not DINING_WORDS.search(g):
        r = re.sub(DINING_BULLET, "", r).strip()

    # ---------- Keep your prior “unprompted apology / cleaner offer” guardrails ----------
    ISSUE_TRIGGERS = [
        "dirty","unclean","mess","messy","bugs","roaches","ants","mold","leak","broken","smell","stain",
        "hair","not clean","cleaning issue","trash","disgust","filthy","soiled","problem","issue","complain"
    ]
    def _looks_like_issue(text: str) -> bool:
        t = (text or "").lower()
        return any(k in t for k in ISSUE_TRIGGERS)

    # Block unprompted apologies
    if not _looks_like_issue(guest_msg):
        r = re.sub(r"(?is)\b(i\s*am|i'?m|we\s*are|we'?re)\s*sorry\b.*?(?:[.!?\n]|$)", "", r).strip()

    # Block unprompted cleaner offers
    if not _looks_like_issue(guest_msg):
        kept = []
        for line in r.splitlines():
            if re.search(r"(?i)(send|schedule|have)\s+(?:the\s+)?(cleaner|housekeep|maid|cleaning)\b", line):
                continue
            kept.append(line)
        r = "\n".join(kept).strip()

    # --- 4) Gentle whitespace tidy ---
    r = re.sub(r"\n{3,}", "\n\n", r).strip()
    return r



# --------------------------- Learning DB ---------------------------

def _init_learning_db():
    try:
        conn = sqlite3.connect(LEARNING_DB_PATH)
        c = conn.cursor()
        c.execute("""
            CREATE TABLE IF NOT EXISTS learning_examples (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                guest_message TEXT,
                ai_suggestion TEXT,
                user_reply TEXT,
                listing_id TEXT,
                guest_id TEXT,
                created_at TEXT
            )
        """)
        c.execute("""
            CREATE TABLE IF NOT EXISTS clarifications (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                conversation_id TEXT,
                guest_message TEXT,
                clarification TEXT,
                tags TEXT,
                created_at TEXT
            )
        """)
        conn.commit()
        conn.close()
    except Exception as e:
        logging.error(f"❌ DB init error: {e}")

_init_learning_db()

def store_learning_example(guest_message: str, ai_suggestion: str, user_reply: str, listing_id: Any, guest_id: Any) -> None:
    try:
        conn = sqlite3.connect(LEARNING_DB_PATH)
        c = conn.cursor()
        c.execute(
            """INSERT INTO learning_examples (guest_message, ai_suggestion, user_reply, listing_id, guest_id, created_at)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (guest_message or "", ai_suggestion or "", user_reply or "", str(listing_id) if listing_id else "", str(guest_id) if guest_id else "", datetime.utcnow().isoformat()),
        )
        conn.commit()
        conn.close()
        logging.info("[LEARNING] Example saved.")
    except Exception as e:
        logging.error(f"❌ DB save error: {e}")

def store_clarification_log(conversation_id: Any, guest_message: str, clarification: str, tags: Optional[List[str]]) -> None:
    try:
        conn = sqlite3.connect(LEARNING_DB_PATH)
        c = conn.cursor()
        c.execute(
            """INSERT INTO clarifications (conversation_id, guest_message, clarification, tags, created_at)
               VALUES (?, ?, ?, ?, ?)""",
            (str(conversation_id), guest_message or "", clarification or "", ",".join(tags) if tags else "", datetime.utcnow().isoformat()),
        )
        conn.commit()
        conn.close()
        logging.info(f"[CLARIFY] Clarification stored for conversation {conversation_id}")
    except Exception as e:
        logging.error(f"❌ Clarification DB error: {e}")

def get_similar_learning_examples(guest_message: str, listing_id: Any) -> List[Tuple[str, str, str]]:
    try:
        conn = sqlite3.connect(LEARNING_DB_PATH)
        c = conn.cursor()
        c.execute(
            """
            SELECT guest_message, ai_suggestion, user_reply
            FROM learning_examples
            WHERE listing_id = ? AND guest_message LIKE ?
            ORDER BY created_at DESC
            LIMIT 5
            """,
            (str(listing_id), f"%{guest_message[:10]}%"),
        )
        results = c.fetchall()
        conn.close()
        return results or []
    except Exception as e:
        logging.error(f"❌ DB fetch error: {e}")
        return []

def retrieve_learned_answer(guest_message: str, listing_id: Any, guest_id: Optional[Any] = None, cutoff: float = 0.8) -> Optional[str]:
    try:
        conn = sqlite3.connect(LEARNING_DB_PATH)
        c = conn.cursor()
        c.execute(
            """
            SELECT guest_message, user_reply, guest_id
            FROM learning_examples
            WHERE listing_id = ?
            ORDER BY created_at DESC
            """,
            (str(listing_id),),
        )
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


# --------------------------- Optional intent (OpenAI) ---------------------------

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
    "other",
]

def detect_intent(message: str) -> str:
    """
    Simple classifier using OpenAI; returns one of INTENT_LABELS (lowercased), or 'other' on failure.
    """
    if not openai_client:
        return "other"
    system_prompt = (
        "You are an intent classification assistant for a vacation rental business. "
        "Given a guest message, return ONLY the intent label from this list: "
        f"{', '.join(INTENT_LABELS)}. Return just the label, nothing else."
    )
    user_prompt = f"Message: {message}"
    try:
        response = openai_client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[{"role": "system", "content": system_prompt}, {"role": "user", "content": user_prompt}],
            max_tokens=10,
            temperature=0,
        )
        intent = (response.choices[0].message.content or "").strip().lower()
        for label in INTENT_LABELS:
            if label in intent:
                return label
        return "other"
    except Exception as e:
        logging.error(f"Intent detection failed: {e}")
        return "other"
