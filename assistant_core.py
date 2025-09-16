# file: assistant_core.py
from __future__ import annotations

import os
import re
import json
import sqlite3
import logging
from enum import Enum
from typing import Any, Dict, List, Optional, Tuple
from datetime import datetime, timedelta

from zoneinfo import ZoneInfo  # NEW: timezone-aware dates

import requests
from pydantic import BaseModel, Field, ValidationError, conlist
from openai import OpenAI

logging.basicConfig(level=logging.INFO)

# ---------- Env / Clients ----------
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
assert OPENAI_API_KEY, "OPENAI_API_KEY is required"
OPENAI_MODEL = os.getenv("OPENAI_MODEL", "gpt-4.1")
client = OpenAI(api_key=OPENAI_API_KEY)

HOSTAWAY_API_BASE = os.getenv("HOSTAWAY_API_BASE", "https://api.hostaway.com/v1")
HOSTAWAY_CLIENT_ID = os.getenv("HOSTAWAY_CLIENT_ID")
HOSTAWAY_CLIENT_SECRET = os.getenv("HOSTAWAY_CLIENT_SECRET")
LEARNING_DB_PATH = os.getenv("LEARNING_DB_PATH", "learning.db")

DEFAULT_CHECKIN = os.getenv("DEFAULT_CHECKIN_TIME", "4:00 PM")
DEFAULT_CHECKOUT = os.getenv("DEFAULT_CHECKOUT_TIME", "11:00 AM")
EARLY_FEE = int(os.getenv("EARLY_CHECKIN_FEE", "50"))
LATE_FEE = int(os.getenv("LATE_CHECKOUT_FEE", "50"))

GOOGLE_PLACES_API_KEY = os.getenv("GOOGLE_PLACES_API_KEY")
GOOGLE_DISTANCE_MATRIX_API_KEY = os.getenv("GOOGLE_DISTANCE_MATRIX_API_KEY")

RES_STATUS_ALLOWED = [
    "new", "modified", "cancelled", "ownerStay", "pending", "awaitingPayment",
    "declined", "expired", "inquiry", "inquiryPreapproved", "inquiryDenied",
    "inquiryTimedout", "inquiryNotPossible",
]

SYSTEM_PROMPT = """You are Host Concierge v3 — a warm, concise, highly competent human host.
No emojis. No sign-offs. 1–3 short sentences unless details are required. Use contractions.

HARD RULES:
1) Only mention check-in/checkout times if the guest asks about timing or it clearly unblocks them.
2) If the guest asks for the door/lock code:
   - If it’s check-in day (or later) and a code is available in context, give the code and 1 helpful tip.
   - If it’s before check-in day, say you’ll send full arrival instructions closer to arrival and offer a heads-up window (e.g., morning of arrival).
3) Early check-in/late checkout/extensions:
   - Never confirm unless calendar/policy allows. Mention the fee only if they’re asking about timing or it’s relevant.
4) Pets:
   - Respect listing pet policy. If pets are not allowed, say so plainly and kindly. Don’t conflate pet deposits with security deposits.
5) Safety & issues: apologize briefly and offer the correct action (e.g., send cleaners; troubleshoot; escalate).
5a) Never offer cleaners or apologize when the guest is thanking you or saying things are “all set / fixed / resolved”. Offer cleaners only when the guest reports an actual cleanliness issue (dirty, smells, overflowing trash, etc.).
6) Deposits & payments:
   - Only send a payment link if the guest explicitly asks for a link/pay now.
   - If they ask “is it $X?”, answer the exact amount from context and note if it’s a refundable hold.
7) Events: acknowledge and offer help only if it adds value (parking, local tips).
8) Tone: friendly, human, no corporate filler. Avoid repeating info they already know unless it answers their question.
9) Local food/drink requests: if context includes curated nearby places, recommend 3–6 specific spots with a one-line why each (rating or vibe) and rough travel time. Don’t ask for preferences first.
10) Keep replies typo-free and natural. Avoid odd hyphenation or missing spaces.
11) If an estimated subtotal for an extension is provided in context, include it succinctly (e.g., “Rough subtotal for +N nights: USD 540 before taxes/fees.”).
12) If the guest’s message is brief or vague (e.g., “yes”, “that works”, “please authorize”), infer intent from the latest prior host message(s) in conversation_history (questions, proposals, or pending actions) and respond accordingly—do not ask for clarification if the context clearly disambiguates it.


Return only JSON with: intent, confidence, needs_clarification, clarifying_question, reply, citations[], actions{}.
"""

# ---------- JSON Schema ----------
class Intent(str, Enum):
    question = "question"
    early_check_in = "early_check_in"
    late_checkout = "late_checkout"
    extend_stay = "extend_stay"
    price_quote = "price_quote"
    discount_request = "discount_request"
    issue_report = "issue_report"
    directions = "directions"
    amenities = "amenities"
    rules = "rules"
    checkin_help = "checkin_help"
    checkout_help = "checkout_help"
    food_recs = "food_recs"
    other = "other"

class Actions(BaseModel):
    check_calendar: bool = False
    create_hostaway_offer: bool = False
    send_house_manual: bool = False
    log_issue: bool = False
    tag_learning_example: bool = False

class AIResponse(BaseModel):
    intent: Intent
    confidence: float = Field(ge=0.0, le=1.0)
    needs_clarification: bool
    clarifying_question: str
    reply: str
    citations: conlist(str, max_length=10) = []
    actions: Actions

# ---------- Learning store ----------
def _init_db(path: str = LEARNING_DB_PATH) -> None:
    conn = sqlite3.connect(path)
    cur = conn.cursor()
    cur.execute("""CREATE TABLE IF NOT EXISTS learning_examples (id INTEGER PRIMARY KEY AUTOINCREMENT)""")
    conn.commit()
    cur.execute("PRAGMA table_info(learning_examples)")
    cols = {row[1] for row in cur.fetchall()}
    if "intent" not in cols:     cur.execute("ALTER TABLE learning_examples ADD COLUMN intent TEXT")
    if "question" not in cols:   cur.execute("ALTER TABLE learning_examples ADD COLUMN question TEXT")
    if "answer" not in cols:     cur.execute("ALTER TABLE learning_examples ADD COLUMN answer TEXT")
    if "created_at" not in cols: cur.execute("ALTER TABLE learning_examples ADD COLUMN created_at TEXT DEFAULT CURRENT_TIMESTAMP")
    conn.commit()
    if {"guest_message", "ai_suggestion"}.issubset(cols):
        cur.execute("""
            UPDATE learning_examples
            SET question = COALESCE(NULLIF(question, ''), guest_message),
                answer   = COALESCE(NULLIF(answer, ''), ai_suggestion)
            WHERE (question IS NULL OR question = '')
               OR (answer   IS NULL OR answer   = '')
        """)
        conn.commit()
    conn.close()

def _ensure_learning_schema(conn: sqlite3.Connection) -> None:
    cur = conn.cursor()
    cur.execute("""
        CREATE TABLE IF NOT EXISTS learning_examples (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            intent TEXT,
            question TEXT,
            answer TEXT,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP
        )
    """)
    cur.execute("PRAGMA table_info(learning_examples)")
    cols = {row[1] for row in cur.fetchall()}
    if "question" not in cols:
        try: cur.execute("ALTER TABLE learning_examples ADD COLUMN question TEXT")
        except Exception: pass
    if "answer" not in cols:
        try: cur.execute("ALTER TABLE learning_examples ADD COLUMN answer TEXT")
        except Exception: pass
    if "intent" not in cols:
        try: cur.execute("ALTER TABLE learning_examples ADD COLUMN intent TEXT")
        except Exception: pass
    conn.commit()

def _similar_examples(q: str, limit: int = 3) -> List[Dict[str, str]]:
    conn = sqlite3.connect(LEARNING_DB_PATH)
    conn.row_factory = sqlite3.Row
    _ensure_learning_schema(conn)
    cur = conn.cursor()
    cur.execute("PRAGMA table_info(learning_examples)")
    cols = {row[1] for row in cur.fetchall()}
    examples: List[Dict[str, str]] = []
    try:
        if {"question", "answer"}.issubset(cols):
            cur.execute(
                """
                SELECT COALESCE(intent,'') AS intent,
                       COALESCE(question,'') AS question,
                       COALESCE(answer,'') AS answer
                FROM learning_examples
                WHERE (question LIKE ? OR answer LIKE ?)
                ORDER BY id DESC
                LIMIT ?
                """,
                (f"%{q[:200]}%", f"%{q[:200]}%", limit)
            )
            rows = cur.fetchall()
            examples = [{"intent": r["intent"], "question": r["question"], "answer": r["answer"]} for r in rows]
        elif {"guest_message", "ai_suggestion", "user_reply"}.issubset(cols):
            cur.execute(
                """
                SELECT COALESCE(guest_message,'') AS guest_message,
                       COALESCE(user_reply,'') AS user_reply,
                       COALESCE(ai_suggestion,'') AS ai_suggestion
                FROM learning_examples
                WHERE (guest_message LIKE ? OR user_reply LIKE ? OR ai_suggestion LIKE ?)
                ORDER BY id DESC
                LIMIT ?
                """,
                (f"%{q[:200]}%", f"%{q[:200]}%", f"%{q[:200]}%", limit)
            )
            rows = cur.fetchall()
            examples = [{
                "intent": "",
                "question": r["guest_message"],
                "answer": r["user_reply"] or r["ai_suggestion"]
            } for r in rows]
    finally:
        conn.close()
    return examples

# ---------- Date & parse helpers ----------
def _coerce_iso_day(s: str) -> Optional[datetime]:
    """Return a datetime for the YYYY-MM-DD part of s, or None."""
    if not s:
        return None
    try:
        return datetime.fromisoformat(str(s)[:10])
    except Exception:
        return None

def _day_before(date_iso: str) -> Optional[str]:
    d = _coerce_iso_day(date_iso)
    return (d - timedelta(days=1)).strftime("%Y-%m-%d") if d else None

def _day_after(date_iso: str) -> Optional[str]:
    d = _coerce_iso_day(date_iso)
    return (d + timedelta(days=1)).strftime("%Y-%m-%d") if d else None

def _daterange(start_iso: str, end_iso: str) -> List[str]:
    """Half-open range of ISO dates: [start, end). Useful for nightly pricing or extension quotes."""
    s = _coerce_iso_day(start_iso)
    e = _coerce_iso_day(end_iso)
    if not s or not e or e <= s:
        return []
    days: List[str] = []
    cur = s
    while cur < e:
        days.append(cur.strftime("%Y-%m-%d"))
        cur += timedelta(days=1)
    return days

def _us_date(iso: Optional[str]) -> str:
    try:
        return datetime.fromisoformat((iso or "")[:10]).strftime("%m/%d/%Y")
    except Exception:
        return iso or "N/A"

# Existing numeric parser
_EXTRA_NIGHTS_RE = re.compile(r'(?:add|extend|extra)\s+(\d+)\s*(?:more\s*)?(?:day|days|night|nights)', re.I)
# NEW: wordy night counts
_WORD_NIGHTS = {"one": 1, "an": 1, "another": 1, "two": 2, "couple": 2, "few": 3, "three": 3}

def _parse_extra_nights(text: str) -> Optional[int]:
    t = text or ""
    m = _EXTRA_NIGHTS_RE.search(t)
    if m:
        try:
            return int(m.group(1))
        except Exception:
            pass
    # Wordy phrasing like "another night", "couple nights"
    for w, n in _WORD_NIGHTS.items():
        if re.search(rf"\b{w}\b.*\bnight", t, re.I):
            return n
    return None

# ---------- Hostaway helpers ----------
def _token() -> Optional[str]:
    if not HOSTAWAY_CLIENT_ID or not HOSTAWAY_CLIENT_SECRET:
        return None
    try:
        url = f"{HOSTAWAY_API_BASE}/accessTokens"
        r = requests.post(
            url,
            data={
                "grant_type": "client_credentials",
                "client_id": HOSTAWAY_CLIENT_ID,
                "client_secret": HOSTAWAY_CLIENT_SECRET,
                "scope": "general",
            },
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            timeout=15,
        )
        r.raise_for_status()
        return r.json().get("access_token")
    except Exception as e:
        logging.error(f"Hostaway token error: {e}")
        return None

def _api_get(path: str, params: Dict[str, Any] | None = None) -> Optional[Dict[str, Any]]:
    t = _token()
    if not t:
        return None
    try:
        url = f"{HOSTAWAY_API_BASE}{path}"
        r = requests.get(url, headers={"Authorization": f"Bearer {t}"}, params=params, timeout=15)
        r.raise_for_status()
        return r.json()
    except Exception as e:
        logging.error(f"GET {path} error: {e}")
        return None

def _fetch_calendar(listing_id: str, start: str, end: str) -> Dict[str, Any]:
    data = _api_get(f"/listings/{listing_id}/calendar", {"startDate": start, "endDate": end})
    return {"ok": bool(data), "data": data or {}}

def _calendar_days(payload: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Extract list of day dicts from varied Hostaway calendar response shapes."""
    try:
        data = payload.get("data") or {}
        result = data.get("result")
        if isinstance(result, dict):
            return result.get("calendar", []) or []
        if isinstance(result, list):
            return result
        if isinstance(data, list):
            return data
        return []
    except Exception:
        return []

def _extract_rate_from_day(day: Dict[str, Any]) -> Optional[float]:
    """Best-effort: look for any nightly rate-looking field on a calendar day."""
    for k in ("price", "dailyRate", "rate", "priceNative", "nightly", "baseDailyRate"):
        v = day.get(k)
        try:
            f = float(v)
            if f >= 0:
                return f
        except (TypeError, ValueError):
            continue
    return None

def _is_available(payload: Dict[str, Any], day: str) -> bool:
    try:
        for d in _calendar_days(payload):
            if str(d.get("date")) == day:
                if "isAvailable" in d:
                    return bool(d["isAvailable"])
                if d.get("status"):
                    return d["status"] == "available"
                return not d.get("blocked") and not d.get("reservationId")
        return False
    except Exception:
        return False

def _estimate_extension_from_calendar(listing_id: str, start: str, end: str) -> Dict[str, Any]:
    """
    Try to price nights [start, end) from calendar daily rates.
    Returns {"ok": bool, "nights": [...], "subtotal": float | None, "breakdown": [{"date":, "rate":}]}
    """
    payload = _fetch_calendar(listing_id, start, end)
    nights = _daterange(start, end)
    if not payload.get("ok") or not nights:
        return {"ok": False, "nights": nights, "subtotal": None, "breakdown": []}

    day_list = _calendar_days(payload)
    day_map = {str(d.get("date")): _extract_rate_from_day(d) for d in day_list}
    breakdown = []
    subtotal = 0.0
    complete = True
    for n in nights:
        rate = day_map.get(n)
        if rate is None:
            complete = False
        else:
            subtotal += float(rate)
        breakdown.append({"date": n, "rate": rate})
    return {"ok": True, "nights": nights, "subtotal": subtotal if complete else None, "breakdown": breakdown}

# ---------- Guest charges integration ----------
CHARGE_DEPOSIT_HINTS = ("deposit", "hold")

def _fetch_guest_charges(reservation_id: Optional[int], listing_map_id: Optional[int]) -> Dict[str, Any]:
    if not reservation_id and not listing_map_id:
        return {"ok": False, "result": []}
    params: Dict[str, Any] = {}
    if reservation_id:
        params["reservationId"] = reservation_id
    if listing_map_id:
        params["listingMapId"] = listing_map_id
    data = _api_get("/guestPayments/charges", params) or {}
    result = data.get("result") or []
    return {"ok": bool(result or data.get("status") == "success"), "result": result}

def _extract_deposit_facts(charges: List[Dict[str, Any]]) -> Dict[str, Any]:
    candidates = []
    for ch in charges:
        typ = (ch.get("type") or "").lower()
        title = (ch.get("title") or "").lower()
        desc = (ch.get("description") or "").lower()
        if typ == "preauth" or any(k in title or k in desc for k in CHARGE_DEPOSIT_HINTS):
            candidates.append(ch)
    def _key(ch):
        return (ch.get("scheduledDate") or "", ch.get("chargeDate") or "", ch.get("id") or 0)
    candidates.sort(key=_key, reverse=True)
    dep = candidates[0] if candidates else None
    if not dep:
        return {"present": False}
    status = (dep.get("status") or "").lower()
    facts = {
        "present": True,
        "type": dep.get("type"),
        "status": status,
        "amount": dep.get("amount"),
        "capturedAmount": dep.get("capturedAmount"),
        "currency": dep.get("currency"),
        "paymentMethod": dep.get("paymentMethod"),
        "scheduledDate": dep.get("scheduledDate"),
        "chargeDate": dep.get("chargeDate"),
        "holdReleaseDate": dep.get("holdReleaseDate"),
        "paymentProvider": dep.get("paymentProvider"),
        "id": dep.get("id"),
    }
    active_hold = (str(dep.get("type")).lower() == "preauth") and (status in {"awaitinghold", "paid"})
    facts["active_hold"] = bool(active_hold)
    return facts

def _summarize_charges(charges: List[Dict[str, Any]]) -> Dict[str, Any]:
    awaiting = [c for c in charges if (c.get("status") or "").lower() == "awaiting"]
    upcoming = next((c for c in charges if c.get("scheduledDate")), None)
    return {
        "has_awaiting": bool(awaiting),
        "awaiting_total": sum(float(c.get("amount") or 0) for c in awaiting) if awaiting else 0.0,
        "next_scheduled": upcoming.get("scheduledDate") if upcoming else None,
    }

# ---------- Cheap intent/keywords ----------
_CLEAN = ["dirty", "messy", "sand", "sandy", "smell", "smelly", "sticky", "dust", "trash", "bug", "bugs", "roach", "ants", "stain"]
_ECI = ["early check in", "early check-in", "arrive early", "check in early", "check-in early", " 1-3", " 1 to 3", " 1–3"]
_LCO = ["late check out", "late check-out", "leave late", "check out late", "check-out late"]
_EVENTS = ["lone star rally", "lone star bike rally", "mardi gras", "spring break", "rodeo", "festival"]
_REST = ["restaurant", "stingaree", "stingray", "marina"]
_DEP_LINK = ["link", "portal", "send link", "pay now", "payment link"]
_DEP_AMT = ["how much", "amount", "$", "is the security deposit", "is deposit", "deposit $"]
_FOOD = [
    "dinner","lunch","breakfast","brunch","coffee","restaurant","eat","food",
    "bbq","barbecue","italian","pizza","sandwich","deli","tacos","seafood","burger",
    "recommend","recommendation","where should we eat","good places to eat"
]
_CODE_PHRASES = [
    "door code","keypad","lock code","entry code","code to the door","code for the door",
    "front door code","gate code","smart lock"
]

# --- Precise issue / gratitude detectors (NEW) ---
_CLEAN_COMPLAINT_WORDS = [
    "dirty","messy","smell","smelly","sticky","dust","stain","stained",
    "bug","bugs","roach","roaches","ant","ants","mold","mildew"
]
_TRASH_COMPLAINT_RE = re.compile(
    r"""(?ix)
    \b(trash|garbage|bin|can)s?\b.*\b(
        full|overflow|overflowing|smell|stink|not\s*empt(?:y|ied)
    )\b
    """
)
_GRATITUDE_RE = re.compile(r"\b(thanks?|thank you|appreciate|all (?:good|set)|fixed|resolved)\b", re.I)

def _is_cleaning_issue(msg: str) -> bool:
    m = (msg or "").lower()
    if not m:
        return False
    if _GRATITUDE_RE.search(m):
        return False
    if any(w in m for w in _CLEAN_COMPLAINT_WORDS):
        return True
    if _TRASH_COMPLAINT_RE.search(m):
        return True
    return False

def _detect_intent(msg: str) -> Intent:
    m = (msg or "").lower()
    if any(w in m for w in _ECI):
        return Intent.early_check_in
    if any(w in m for w in _LCO):
        return Intent.late_checkout
    if "extend" in m or "extra night" in m or "stay longer" in m or re.search(_EXTRA_NIGHTS_RE, m or ""):
        return Intent.extend_stay
    if any(w in m for w in _FOOD):
        return Intent.food_recs
    if "how far" in m or "distance" in m or "drive time" in m:
        return Intent.directions
    if "deposit" in m or "security deposit" in m:
        return Intent.rules
    # NEW: precise cleaning detection (don’t misfire on thanks)
    if _is_cleaning_issue(m):
        return Intent.issue_report
    # NEW: if purely a thank-you / all-set, treat as other
    if _GRATITUDE_RE.search(m):
        return Intent.other
    if any(w in m for w in _REST):
        return Intent.directions
    return Intent.other

# ---------- Context scaffolding ----------
def _profile(meta: Dict[str, Any]) -> Dict[str, Any]:
    p = (meta.get("property_profile") or {}).copy()
    p.setdefault("checkin_time", DEFAULT_CHECKIN)
    p.setdefault("checkout_time", DEFAULT_CHECKOUT)
    return p

def _policies(meta: Dict[str, Any]) -> Dict[str, Any]:
    pol = (meta.get("policies") or {}).copy()
    pol.setdefault("early_checkin_fee", EARLY_FEE)
    pol.setdefault("late_checkout_fee", LATE_FEE)
    return pol

# ---------- Google Places helpers ----------
def _places_nearby(lat: float, lng: float, keyword: str, max_results: int = 4) -> List[Dict[str, Any]]:
    if not GOOGLE_PLACES_API_KEY:
        return []
    url = "https://maps.googleapis.com/maps/api/place/nearbysearch/json"
    params = {
        "location": f"{lat},{lng}",
        "radius": 8000,  # ~5 miles
        "type": "restaurant",
        "keyword": keyword,
        "key": GOOGLE_PLACES_API_KEY,
        "opennow": False,
    }
    try:
        r = requests.get(url, params=params, timeout=12)
        data = r.json()
        results = data.get("results", [])
        filtered = []
        for p in results:
            rating = float(p.get("rating") or 0)
            reviews = int(p.get("user_ratings_total") or 0)
            if rating >= 4.3 and reviews >= 150:
                filtered.append({
                    "name": p.get("name"),
                    "rating": rating,
                    "reviews": reviews,
                    "price_level": p.get("price_level"),
                    "lat": p.get("geometry", {}).get("location", {}).get("lat"),
                    "lng": p.get("geometry", {}).get("location", {}).get("lng"),
                    "place_id": p.get("place_id"),
                })
        filtered.sort(key=lambda x: (x["rating"], x["reviews"]), reverse=True)
        return filtered[:max_results]
    except Exception as e:
        logging.error(f"Places nearby error for {keyword}: {e}")
        return []

def _distance_matrix(lat: float, lng: float, dests: List[Tuple[float, float]]) -> List[Dict[str, str]]:
    if not dests or not GOOGLE_DISTANCE_MATRIX_API_KEY:
        return []
    url = "https://maps.googleapis.com/maps/api/distancematrix/json"
    params = {
        "origins": f"{lat},{lng}",
        "destinations": "|".join([f"{d[0]},{d[1]}" for d in dests]),
        "mode": "driving",
        "units": "imperial",
        "key": GOOGLE_DISTANCE_MATRIX_API_KEY,
    }
    try:
        r = requests.get(url, params=params, timeout=12)
        data = r.json()
        rows = data.get("rows", [])
        if not rows:
            return []
        els = rows[0].get("elements", [])
        out = []
        for el in els:
            dist = (el.get("distance") or {}).get("text")
            dur = (el.get("duration") or {}).get("text")
            out.append({"distance": dist, "duration": dur})
        return out
    except Exception as e:
        logging.error(f"Distance matrix error: {e}")
        return []

def _build_food_recs(lat: Optional[float], lng: Optional[float]) -> List[Dict[str, Any]]:
    """Return a list of {label, name, rating, reviews, distance, duration, place_id} buckets."""
    if not (lat and lng):
        return []
    categories = [
        ("BBQ", "bbq barbecue"),
        ("Italian", "italian pizza"),
        ("Sandwich", "sandwich deli"),
        ("Breakfast/Coffee", "breakfast brunch coffee"),
    ]
    all_picks: List[Dict[str, Any]] = []
    for label, kw in categories:
        picks = _places_nearby(lat, lng, kw, max_results=4)
        if not picks:
            continue
        top = picks[0]
        all_picks.append({"label": label, **top})
    dests = [(p["lat"], p["lng"]) for p in all_picks if p.get("lat") and p.get("lng")]
    dists = _distance_matrix(lat, lng, dests) if dests else []
    for i, p in enumerate(all_picks):
        if i < len(dists):
            p["distance"] = dists[i].get("distance")
            p["duration"] = dists[i].get("duration")
    return all_picks

def _format_food_recs(recs: List[Dict[str, Any]]) -> str:
    """Visible formatting only; no raw place IDs/CIDs in text."""
    if not recs:
        return ""
    lines = []
    for r in recs:
        label = f"{r['label']}: " if r.get("label") else ""
        rating = f"{r['rating']:.1f}★" if r.get("rating") is not None else ""
        reviews = f" ({int(r['reviews']):,})" if r.get("reviews") else ""
        tail = f", ~{r['duration']}" if r.get("duration") else (f", {r['distance']}" if r.get("distance") else "")
        lines.append(f"- {label}{r['name']} — {rating}{reviews}{tail}".strip())
    return "Here are a few solid nearby picks:\n" + "\n".join(lines)

# ---------- Context ----------
def _context(guest_message: str, history: List[Dict[str, str]], meta: Dict[str, Any]) -> Dict[str, Any]:
    prof = _profile(meta)
    pol = _policies(meta)
    learned = _similar_examples(guest_message, 3)

    latest_guest_msg = None
    for m in reversed(history or []):
        if (m.get("role") or "").lower() == "guest" and (m.get("text") or "").strip():
            latest_guest_msg = m["text"].strip()
            break

    # Arrival/access/pets
    tz_name = (meta.get("timezone")
               or (meta.get("property_profile") or {}).get("timezone")
               or "UTC")
    try:
        today_str = datetime.now(ZoneInfo(tz_name)).date().isoformat()
    except Exception:
        today_str = datetime.utcnow().date().isoformat()
    is_checkin_day = (str(meta.get("check_in") or "")[:10] == today_str)

    ctx_access = meta.get("access") or {}
    door_code_available = bool((ctx_access.get("door_code") or "").strip())

    pet_allowed = pol.get("pets_allowed")
    pet_fee = pol.get("pet_fee")
    pet_deposit_refundable = pol.get("pet_deposit_refundable")

    # Calendar facts
    calendar: Dict[str, Any] = {"looked_up": False}
    listing_id = meta.get("listing_id")
    ci = meta.get("check_in")
    co = meta.get("check_out")

    if listing_id and ci and co:
        cal_payload = _fetch_calendar(str(listing_id), ci, co)
        calendar["looked_up"] = bool(cal_payload.get("ok"))
        calendar["checkin_available"] = _is_available(cal_payload, ci) if calendar["looked_up"] else None
        calendar["checkout_available"] = _is_available(cal_payload, co) if calendar["looked_up"] else None

        day_before = _day_before(ci)
        day_after = _day_after(co)
        if day_before or day_after:
            span_start = day_before or ci
            span_end = day_after or co
            span_payload = _fetch_calendar(str(listing_id), span_start, span_end)
            calendar["looked_up_span"] = bool(span_payload.get("ok"))
            if day_before:
                calendar["day_before_available"] = _is_available(span_payload, day_before)
            if day_after:
                calendar["day_after_available"] = _is_available(span_payload, day_after)

    # Payments / deposit
    reservation_id = meta.get("reservation_id")
    listing_map_id = meta.get("listing_map_id") or listing_id
    charges_payload = _fetch_guest_charges(int(reservation_id) if reservation_id else None,
                                           int(listing_map_id) if listing_map_id else None)
    charges = charges_payload.get("result", [])
    deposit_facts = _extract_deposit_facts(charges)
    payments_summary = _summarize_charges(charges)

    status = (meta.get("reservation_status") or "").strip()
    intent_guess = _detect_intent(latest_guest_msg or guest_message)

    # Location for Places
    loc = meta.get("location") or {}
    lat = loc.get("lat")
    lng = loc.get("lng")

    # Build food recs if asked and we have location + keys
    food_recs: List[Dict[str, Any]] = []
    if intent_guess == Intent.food_recs and lat and lng and GOOGLE_PLACES_API_KEY:
        food_recs = _build_food_recs(float(lat), float(lng))

    # --- Dates & extension context ---
    ci_iso = (str(ci)[:10] if isinstance(ci, str) else (str(ci)[:10] if ci else ""))
    co_iso = (str(co)[:10] if isinstance(co, str) else (str(co)[:10] if co else ""))
    nights = None
    try:
        if ci_iso and co_iso:
            nights = (datetime.fromisoformat(co_iso) - datetime.fromisoformat(ci_iso)).days
    except Exception:
        nights = None

    extra_nights = _parse_extra_nights(latest_guest_msg or guest_message)
    new_co_iso = None
    new_co_us = None
    if extra_nights and co_iso:
        try:
            new_co_iso = (datetime.fromisoformat(co_iso) + timedelta(days=extra_nights)).strftime("%Y-%m-%d")
            new_co_us = _us_date(new_co_iso)
        except Exception:
            pass

    # --- Extension pricing (best-effort nightly-rate lookup) ---
    currency_guess = (deposit_facts.get("currency") if isinstance(deposit_facts, dict) else None) or "USD"
    ext_quote: Dict[str, Any] = {"subtotal": None, "nightly_breakdown": [], "currency": currency_guess}

    if extra_nights and co_iso and new_co_iso and listing_id:
        cal_quote = _estimate_extension_from_calendar(str(listing_id), co_iso, new_co_iso)
        if cal_quote.get("ok") and cal_quote.get("subtotal") is not None:
            ext_quote["subtotal"] = float(cal_quote["subtotal"])
            ext_quote["nightly_breakdown"] = cal_quote["breakdown"]

    return {
        "profile": prof,
        "policies": pol,
        "learned": learned,
        "conversation_history": history,  # use full prepared history from main.py
        "latest_guest_message": latest_guest_msg or guest_message,
        "calendar": calendar,
        "reservation_status": status,
        "status_vocab": RES_STATUS_ALLOWED,
        "intent_guess": intent_guess,
        "payments": {
            "charges_looked_up": charges_payload.get("ok"),
            "charges_count": len(charges),
            "summary": payments_summary,
        },
        "deposit_facts": deposit_facts,

        # Exposed to the model
        "arrival_context": {
            "is_checkin_day": is_checkin_day,
            "door_code_available": door_code_available,
        },
        "access": ctx_access,
        "pet_policy": {
            "allowed": pet_allowed,
            "fee": pet_fee,
            "deposit_refundable": pet_deposit_refundable,
        },
        "location": {"lat": lat, "lng": lng},
        "food_recs": food_recs,  # structured picks for direct formatting

        # Dates block for smarter replies
        "dates": {
            "check_in": ci_iso,
            "check_out": co_iso,
            "check_in_us": _us_date(ci_iso),
            "check_out_us": _us_date(co_iso),
            "nights": nights,
        },
        "extension": {
            "extra_nights_requested": extra_nights,
            "new_check_out": new_co_iso,
            "new_check_out_us": new_co_us,
            "quote": ext_quote,
        },
    }

# ---------- Coercion / normalization for LLM JSON ----------
_INTENT_SYNONYMS = {
    "report_issue": "issue_report",
    "issue": "issue_report",
    "complaint": "issue_report",
    "question_general": "question",
    "checkin": "checkin_help",
    "checkout": "checkout_help",
    "restaurants": "food_recs",
    "food": "food_recs",
}

def _coerce_ai_json(d: Dict[str, Any]) -> Dict[str, Any]:
    out = dict(d or {})
    intent = str(out.get("intent", "other") or "other").lower()
    intent = _INTENT_SYNONYMS.get(intent, intent)
    if intent not in {i.value for i in Intent}:
        intent = "other"
    out["intent"] = intent
    out["needs_clarification"] = bool(out.get("needs_clarification", False))
    try:
        out["confidence"] = float(out.get("confidence", 0.6))
    except Exception:
        out["confidence"] = 0.6
    cq = out.get("clarifying_question")
    out["clarifying_question"] = "" if cq is None else str(cq)
    rep = out.get("reply")
    out["reply"] = "" if rep is None else str(rep)
    cits = out.get("citations")
    if not isinstance(cits, list):
        cits = []
    cits = [str(x) for x in cits][:10]
    out["citations"] = cits
    actions = out.get("actions")
    if not isinstance(actions, dict):
        actions = {}
    out["actions"] = {
        "check_calendar": bool(actions.get("check_calendar", False)),
        "create_hostaway_offer": bool(actions.get("create_hostaway_offer", False)),
        "send_house_manual": bool(actions.get("send_house_manual", False)),
        "log_issue": bool(actions.get("log_issue", False)),
        "tag_learning_example": bool(actions.get("tag_learning_example", False)),
    }
    return out

# ---------- LLM call + validation ----------
def _llm(system_prompt: str, ctx: Dict[str, Any]) -> AIResponse:
    resp = client.chat.completions.create(
        model=OPENAI_MODEL,
        temperature=0.3,
        top_p=0.9,
        max_tokens=700,
        response_format={"type": "json_object"},
        messages=[
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": json.dumps({"context": ctx}, ensure_ascii=False)},
        ],
    )
    raw = (resp.choices[0].message.content or "").strip()
    try:
        parsed = json.loads(raw)
        coerced = _coerce_ai_json(parsed)
        return AIResponse(**coerced)
    except ValidationError as ve:
        logging.error(f"AI JSON validation error: {ve.errors()}; raw={raw[:300]}")
    except Exception as e:
        logging.error(f"AI JSON parse error: {e}; raw={raw[:300]}")
    return AIResponse(
        intent=Intent.other,
        confidence=0.4,
        needs_clarification=True,
        clarifying_question="Could you share your dates and guest count so I can confirm?",
        reply="Happy to help. Once I have your dates and guest count, I can confirm next steps.",
        citations=[],
        actions=Actions(),
    )

# ---------- Text polish ----------
def _polish(text: str) -> str:
    """
    Keep fixes minimal; avoid breaking numbers/IDs like 4.4★ or 3,757 or place IDs.
    """
    if not text:
        return text
    fixes = {
        "openwould": "open would",
        "open—would": "open — would",
        "AMif": "AM — if",
        "PMif": "PM — if",
        "knowcongratulations": "know — congratulations",
    }
    for k, v in fixes.items():
        text = text.replace(k, v)
    # IMPORTANT: do NOT split lowercase→Uppercase (it mangles IDs like ChIJ...)
    text = re.sub(r"\s{2,}", " ", text).strip()
    # no trailing stray punctuation dashes
    text = re.sub(r"[—–-]\s*$", "", text).strip()
    return text

# ---------- Guardrails ----------
def _guards(ai: AIResponse, ctx: Dict[str, Any]) -> AIResponse:
    prof, pol = ctx.get("profile", {}), ctx.get("policies", {})
    status = (ctx.get("reservation_status") or "").lower()
    latest = (ctx.get("latest_guest_message") or "").lower()
    text = ai.reply or ""

    # Helper: only surface food recs when intent/keywords suggest it
    def _intent_allows_food(ai_intent: str, msg: str) -> bool:
        return (ai_intent == "food_recs") or any(w in msg for w in _FOOD)

    # If we have curated food recs, format them ONLY when appropriate
    curated = ctx.get("food_recs") or []
    if curated and _intent_allows_food(getattr(ai.intent, "value", str(ai.intent)), latest):
        formatted = _format_food_recs(curated)
        if formatted:
            ai.intent = Intent.food_recs
            ai.needs_clarification = False
            ai.clarifying_question = ""
            ai.reply = _polish(formatted)
            # Add place IDs to citations (not visible in text)
            cits = []
            for r in curated:
                pid = r.get("place_id")
                if pid:
                    cits.append(f"place_id:{pid}")
                if len(cits) >= 10:
                    break
            ai.citations = cits
            return ai  # done

    # Door code hard-guard
    if any(phrase in latest for phrase in _CODE_PHRASES):
        arr = (ctx.get("arrival_context") or {})
        acc = (ctx.get("access") or {})
        code = (acc.get("door_code") or "").strip()
        if arr.get("is_checkin_day") and code:
            text = f"Your door code is {code}. After entering, press # to confirm."
            ai.needs_clarification = False
            ai.clarifying_question = ""
        else:
            text = "I’ll send the full arrival instructions (including the code) closer to your check-in."
            ai.needs_clarification = False
            ai.clarifying_question = ""

    # Gratitude / “all set” → brief acknowledgement only
    if _GRATITUDE_RE.search(latest):
        ai.intent = Intent.other
        ai.needs_clarification = False
        ai.clarifying_question = ""
        ai.reply = _polish("You're welcome! If anything else comes up, just let me know.")
        return ai

    if status in {"cancelled", "expired", "declined"}:
        text = "This reservation isn’t active. I can share available dates or set you up with a new booking."
        ai.needs_clarification = True
        ai.clarifying_question = ai.clarifying_question or "Want me to check fresh dates for you?"
        ai.actions.check_calendar = True

    if status == "ownerstay":
        text = "Those dates aren’t available due to an owner stay. I can suggest nearby dates that are open."
        ai.needs_clarification = True
        ai.clarifying_question = ai.clarifying_question or "Are your dates flexible?"
        ai.actions.check_calendar = True

    if status in {"pending", "awaitingpayment"}:
        if "confirmed" in text.lower() or "you’re all set" in text.lower():
            text = "I can hold this while payment is completed. Once that’s done, I’ll confirm right away."

    if ai.intent in (Intent.early_check_in, Intent.late_checkout, Intent.extend_stay):
        ci_time, co_time = prof.get("checkin_time", DEFAULT_CHECKIN), prof.get("checkout_time", DEFAULT_CHECKOUT)
        cal = ctx.get("calendar") or {}
        checkin_avail = cal.get("checkin_available")
        checkout_avail = cal.get("checkout_available")
        day_after_open = cal.get("day_after_available")

        if ai.intent == Intent.early_check_in:
            text = f"Standard check-in is {ci_time}."
            if checkin_avail:
                text += f" I can request early check-in if the schedule allows (typically ${pol.get('early_checkin_fee', EARLY_FEE)})."
            else:
                text += " The night before is booked, so early check-in may not be possible."
        elif ai.intent == Intent.late_checkout:
            text = f"Check-out is {co_time}."
            if checkout_avail:
                text += f" I can request late checkout if possible (typically ${pol.get('late_checkout_fee', LATE_FEE)})."
            else:
                text += " The next guest arrives the same day, so late checkout may not be possible."
        else:  # extend_stay
            d = ctx.get("dates") or {}
            ext = ctx.get("extension") or {}
            ci_us = d.get("check_in_us") or "your current check-in date"
            co_us = d.get("check_out_us") or "your current check-out date"
            extra = ext.get("extra_nights_requested")
            new_co_us = ext.get("new_check_out_us")
            base = f"You're booked {ci_us}–{co_us}. "
            if extra and new_co_us:
                base += f"Adding {extra} more night(s) would take you to {new_co_us}. "
            quote = (ext.get("quote") or {})
            subtotal = quote.get("subtotal")
            currency = (quote.get("currency") or "USD").upper()
            if subtotal is not None:
                base += f"Rough subtotal for {extra} night(s): {currency} {subtotal:,.0f} before taxes/fees. "
            base += "I can check availability and send the exact quote."
            text = base
            ai.needs_clarification = True
            ai.clarifying_question = "Want me to proceed and send the quote?"
            ai.actions.check_calendar = True

        if (ai.intent in (Intent.early_check_in, Intent.late_checkout)) and day_after_open:
            text += " By the way, the night after is open—would you like me to check if extending your stay works?"

    # Only apologize / send cleaners for actual cleanliness issues
    if ai.intent == Intent.issue_report or _is_cleaning_issue(latest):
        base = text or "Thanks for flagging that."
        if "sorry" not in base.lower():
            base = "I’m sorry about that. " + base

        if re.search(r"(dirty|trash|garbage|spill|mess|stain|smell)", latest, re.I) and not _GRATITUDE_RE.search(latest):
            if "cleaner" not in base.lower():
                base += (" " if base else "") + "I can send our cleaners—what time works for you?"

        base = re.sub(
            r"(we can leave|i can leave|there are) (a )?(vacuum|broom|cleaning supplies).*",
            "",
            base,
            flags=re.IGNORECASE
        ).strip()

        ai.reply = _polish(base)
        return ai

    if any(ev in latest for ev in _EVENTS) and "tip" not in text.lower():
        text += (" " if text else "") + "Great time to visit—if you need parking or local tips for the event, I’ve got you."

    if ("how far" in latest or "distance" in latest or "drive time" in latest) and any(w in latest for w in _REST):
        if "busy" not in text.lower():
            text += (" " if text else "") + "It can get busy on weekends—going a bit early helps."

    dep = ctx.get("deposit_facts") or {}
    payments = ctx.get("payments") or {}
    wants_link = any(w in latest for w in _DEP_LINK)
    asks_amount = any(w in latest for w in _DEP_AMT)
    mentions_deposit = ("deposit" in latest) or ("security deposit" in latest)

    if mentions_deposit:
        amount = dep.get("amount")
        currency = (dep.get("currency") or "USD").upper()
        status_dep = (dep.get("status") or "").lower()
        release = dep.get("holdReleaseDate")
        active_hold = bool(dep.get("active_hold"))

        if asks_amount and amount:
            text = f"Yes—{currency} {amount:.0f}. It’s a refundable hold processed before arrival."
        elif active_hold and amount:
            text = f"We already have a refundable hold on file for {currency} {amount:.0f}."
            if release:
                text += f" It auto-releases on {release}."
        elif status_dep == "awaiting" and amount:
            summary = payments.get("summary") or {}
            text = f"A refundable hold of {currency} {amount:.0f} is scheduled/awaiting."
            if summary.get("next_scheduled"):
                text += f" Next scheduled step: {summary['next_scheduled']}."
        else:
            if not text:
                text = "It’s a refundable hold processed before arrival."
        if not wants_link:
            text = re.sub(r"https?://\S+", "", text).strip()

    # If we already have dates, never ask the guest to repeat them
    have_dates = bool((ctx.get("dates") or {}).get("check_in")) and bool((ctx.get("dates") or {}).get("check_out"))
    if have_dates:
        if re.search(r'\b(what|which|share|send|provide).{0,30}\bdate', (ai.reply or ""), re.I):
            text = re.sub(r'(?is)\b(what|which|share|send|provide).{0,80}\bdate(s)?\??\.?', '', text).strip()
        if re.search(r'\b(what|which|share|send|provide).{0,30}\bdate', (ai.clarifying_question or ""), re.I):
            ai.clarifying_question = "Want me to proceed and send the quote?"

    ai.reply = _polish((text or "").strip())
    return ai

# ---------- Public API ----------
def compose_reply(
    guest_message: str,
    conversation_history: List[Dict[str, str]],
    meta: Dict[str, Any]
) -> Tuple[Dict[str, Any], List[Dict[str, Any]]]:
    """
    meta expects (as available):
      listing_id, listing_map_id, reservation_id, reservation_status, check_in, check_out,
      property_profile, policies, access, location {lat,lng}, timezone?
    """
    _init_db()
    ctx = _context(guest_message, conversation_history, meta)
    ai = _llm(SYSTEM_PROMPT, ctx)
    ai = _guards(ai, ctx)
    return json.loads(ai.model_dump_json()), []
