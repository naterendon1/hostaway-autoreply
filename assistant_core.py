# file: assistant_core.py
from __future__ import annotations

import os
import re
import json
import sqlite3
import logging
from enum import Enum
from typing import Any, Dict, List, Optional, Tuple

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

RES_STATUS_ALLOWED = [
    "new", "modified", "cancelled", "ownerStay", "pending", "awaitingPayment",
    "declined", "expired", "inquiry", "inquiryPreapproved", "inquiryDenied",
    "inquiryTimedout", "inquiryNotPossible",
]

SYSTEM_PROMPT = """You are Host Concierge v3 — a warm, concise, highly competent human host.
No emojis. No sign-offs. Short paragraphs.

HARD RULES:
1) Never confirm early check-in/late checkout/extension unless policy/calendar confirms. State standard times and that you can check; include fee if applicable.
2) If missing critical info, ask ONE crisp clarifier and propose next step.
3) Quote policies accurately; if unknown, say you’ll check and follow up.
4) Safety: urgent issues → escalate + immediate steps.
5) Cleanliness: brief apology + offer to send cleaners; do NOT suggest the guest clean.
6) Deposit & payments:
   - Only send a payment link if guest explicitly asks for a link/pay now.
   - If they ask “is it $X?”, answer amount precisely (from context). Mention it’s a refundable hold if applicable.
   - If a deposit hold already exists or is awaiting-hold, say it’s on file + release date.
7) Events: acknowledge if mentioned (e.g., Lone Star Rally) and offer tips.
8) Restaurant distance questions: add one practical tip (“gets busy on weekends—going a bit early helps.”); no invented details.
9) Respect reservation status:
   - cancelled/expired/declined: do NOT confirm anything; offer alternatives/help only.
   - ownerStay: unavailable; offer other dates.
   - pending/awaitingPayment: do NOT confirm; nudge the next step.
   - inquiry/new/modified: normal, but don’t over-promise.

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
    """
    Create/upgrade the learning_examples table safely across old/new schemas.
    NOTE: SQLite does not allow parameter placeholders in DDL; build statements directly.
    """
    conn = sqlite3.connect(path)
    cur = conn.cursor()

    # Ensure table exists (minimal form)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS learning_examples (
            id INTEGER PRIMARY KEY AUTOINCREMENT
        )
    """)
    conn.commit()

    # Inspect current columns
    cur.execute("PRAGMA table_info(learning_examples)")
    cols = {row[1] for row in cur.fetchall()}

    # Add missing columns for new schema
    if "intent" not in cols:
        cur.execute("ALTER TABLE learning_examples ADD COLUMN intent TEXT")
    if "question" not in cols:
        cur.execute("ALTER TABLE learning_examples ADD COLUMN question TEXT")
    if "answer" not in cols:
        cur.execute("ALTER TABLE learning_examples ADD COLUMN answer TEXT")
    if "created_at" not in cols:
        cur.execute("ALTER TABLE learning_examples ADD COLUMN created_at TEXT DEFAULT CURRENT_TIMESTAMP")
    conn.commit()

    # If old columns exist, backfill once
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
    # Create if missing (new schema)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS learning_examples (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            intent TEXT,
            question TEXT,
            answer TEXT,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP
        )
    """)
    # Check existing columns
    cur.execute("PRAGMA table_info(learning_examples)")
    cols = {row[1] for row in cur.fetchall()}

    # Add missing columns if running against an old table
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

    # Inspect columns to choose the right query
    cur.execute("PRAGMA table_info(learning_examples)")
    cols = {row[1] for row in cur.fetchall()}

    examples: List[Dict[str, str]] = []

    try:
        if {"question", "answer"}.issubset(cols):
            # New schema
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
            # Old schema → map to new field names
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
        else:
            # Unknown/mixed schema → return empty gracefully
            examples = []

    finally:
        conn.close()

    return examples

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

def _is_available(payload: Dict[str, Any], day: str) -> bool:
    try:
        data = payload.get("data") or {}
        result = data.get("result")
        if isinstance(result, dict):
            days = result.get("calendar", [])
        else:
            days = result or data if isinstance(result, list) else []
        for d in days:
            if str(d.get("date")) == day:
                if "isAvailable" in d:
                    return bool(d["isAvailable"])
                if d.get("status"):
                    return d["status"] == "available"
                return not d.get("blocked") and not d.get("reservationId")
        return False
    except Exception:
        return False

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

def _detect_intent(msg: str) -> Intent:
    m = (msg or "").lower()
    if any(w in m for w in _ECI):
        return Intent.early_check_in
    if any(w in m for w in _LCO):
        return Intent.late_checkout
    if "extend" in m or "extra night" in m or "stay longer" in m:
        return Intent.extend_stay
    if "how far" in m or "distance" in m or "drive time" in m:
        return Intent.directions
    if "deposit" in m or "security deposit" in m:
        return Intent.rules
    if any(w in m for w in _CLEAN):
        return Intent.issue_report
    if any(w in m for w in _REST):
        return Intent.directions
    return Intent.other

# ---------- Context ----------
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

def _context(guest_message: str, history: List[Dict[str, str]], meta: Dict[str, Any]) -> Dict[str, Any]:
    prof = _profile(meta)
    pol = _policies(meta)
    learned = _similar_examples(guest_message, 3)

    latest_guest_msg = None
    for m in reversed(history or []):
        if (m.get("role") or "").lower() == "guest" and (m.get("text") or "").strip():
            latest_guest_msg = m["text"].strip()
            break

    calendar: Dict[str, Any] = {"looked_up": False}
    listing_id, ci, co = meta.get("listing_id"), meta.get("check_in"), meta.get("check_out")
    if listing_id and ci and co:
        cal_payload = _fetch_calendar(str(listing_id), ci, co)
        calendar["looked_up"] = bool(cal_payload.get("ok"))
        calendar["checkin_available"] = _is_available(cal_payload, ci) if calendar["looked_up"] else None
        calendar["checkout_available"] = _is_available(cal_payload, co) if calendar["looked_up"] else None

    reservation_id = meta.get("reservation_id")
    listing_map_id = meta.get("listing_map_id") or listing_id
    charges_payload = _fetch_guest_charges(int(reservation_id) if reservation_id else None,
                                           int(listing_map_id) if listing_map_id else None)
    charges = charges_payload.get("result", [])
    deposit_facts = _extract_deposit_facts(charges)
    payments_summary = _summarize_charges(charges)

    status = (meta.get("reservation_status") or "").strip()
    intent_guess = _detect_intent(latest_guest_msg or guest_message)

    return {
        "profile": prof,
        "policies": pol,
        "learned": learned,
        "conversation_history": (history or [])[-8:],
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
    }

# ---------- Coercion / normalization for LLM JSON ----------
_INTENT_SYNONYMS = {
    "report_issue": "issue_report",
    "issue": "issue_report",
    "complaint": "issue_report",
    "question_general": "question",
    "checkin": "checkin_help",
    "checkout": "checkout_help",
}

def _coerce_ai_json(d: Dict[str, Any]) -> Dict[str, Any]:
    """Normalize LLM JSON so Pydantic validation won't fail on minor variations."""
    out = dict(d or {})

    # intent mapping
    intent = str(out.get("intent", "other") or "other").lower()
    intent = _INTENT_SYNONYMS.get(intent, intent)
    # ensure it matches our Enum values
    if intent not in {i.value for i in Intent}:
        intent = "other"
    out["intent"] = intent

    # booleans / numbers
    out["needs_clarification"] = bool(out.get("needs_clarification", False))
    try:
        out["confidence"] = float(out.get("confidence", 0.6))
    except Exception:
        out["confidence"] = 0.6

    # strings
    cq = out.get("clarifying_question")
    out["clarifying_question"] = "" if cq is None else str(cq)
    rep = out.get("reply")
    out["reply"] = "" if rep is None else str(rep)

    # citations: list of strings, ≤10
    cits = out.get("citations")
    if not isinstance(cits, list):
        cits = []
    cits = [str(x) for x in cits][:10]
    out["citations"] = cits

    # actions object
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
        # First parse as dict, then coerce, then validate
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

# ---------- Guardrails ----------
def _guards(ai: AIResponse, ctx: Dict[str, Any]) -> AIResponse:
    prof, pol = ctx.get("profile", {}), ctx.get("policies", {})
    status = (ctx.get("reservation_status") or "").lower()
    latest = (ctx.get("latest_guest_message") or "").lower()
    text = ai.reply or ""

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
        ci, co = prof.get("checkin_time", DEFAULT_CHECKIN), prof.get("checkout_time", DEFAULT_CHECKOUT)
        if ai.intent == Intent.early_check_in:
            text = f"Standard check-in is {ci}. I can request early check-in if the schedule allows (typically ${pol.get('early_checkin_fee', EARLY_FEE)})."
        elif ai.intent == Intent.late_checkout:
            text = f"Check-out is {co}. I can request late checkout if possible (typically ${pol.get('late_checkout_fee', LATE_FEE)})."
        else:
            text = "I can check an extension for you and confirm if the dates are open."
        ai.needs_clarification = True
        ai.clarifying_question = ai.clarifying_question or "What time are you hoping for?"
        ai.actions.check_calendar = True

    if any(w in latest for w in _CLEAN) or ai.intent == Intent.issue_report:
        if "sorry" not in text.lower() and "apolog" not in text.lower():
            text = "I’m sorry about that. " + text
        if "cleaner" not in text.lower():
            text += (" " if text else "") + "I can send our cleaners back—what time works for you?"
        # Remove any suggestion to guest-clean
        text = re.sub(r"(we can leave|i can leave|there are) (a )?(vacuum|broom|cleaning supplies).*", "", text, flags=re.IGNORECASE).strip()

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
        currency = dep.get("currency") or "USD"
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

    ai.reply = text.strip()
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
      property_profile, policies
    """
    _init_db()
    ctx = _context(guest_message, conversation_history, meta)
    ai = _llm(SYSTEM_PROMPT, ctx)
    ai = _guards(ai, ctx)
    return json.loads(ai.model_dump_json()), []
