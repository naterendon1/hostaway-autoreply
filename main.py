# =========================
# File: main.py
# =========================
import os
import logging
import json
import sqlite3
from typing import List, Dict
from datetime import date

from fastapi import FastAPI, Depends, HTTPException, Header, Query
from pydantic import BaseModel
from openai import OpenAI

from slack_interactivity import router as slack_router
from db import init_db

from utils import (
    fetch_hostaway_listing, fetch_hostaway_reservation, fetch_hostaway_conversation,
    get_cancellation_policy_summary, get_similar_learning_examples, clean_ai_reply,
    extract_date_range_from_message, fetch_hostaway_calendar, is_date_available, next_available_dates,
    detect_intent, get_property_location, search_google_places, detect_place_type, extract_destination_from_message,
    resolve_place_textsearch, get_distance_drive_time, detect_time_adjust_request, evaluate_time_adjust_options,
    detect_deposit_request, detect_deposit_intent_type, compose_reply, detect_special_event
)

logging.basicConfig(level=logging.INFO)
app = FastAPI()
app.include_router(slack_router, prefix="/slack")
init_db()

LEARNING_DB_PATH = os.getenv("LEARNING_DB_PATH", "learning.db")
ADMIN_TOKEN = os.getenv("ADMIN_TOKEN", "dev-token")

REQUIRED_ENV_VARS = [
    "HOSTAWAY_CLIENT_ID","HOSTAWAY_CLIENT_SECRET","OPENAI_API_KEY",
    "SLACK_CHANNEL","SLACK_BOT_TOKEN","SLACK_SIGNING_SECRET",
    "GOOGLE_PLACES_API_KEY","GOOGLE_DISTANCE_MATRIX_API_KEY",
]
missing = [v for v in REQUIRED_ENV_VARS if not os.getenv(v)]
if missing:
    raise RuntimeError(f"Missing required environment variables: {missing}")

HOSTAWAY_API_BASE = "https://api.hostaway.com/v1"
SLACK_CHANNEL = os.getenv("SLACK_CHANNEL")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
openai_client = OpenAI(api_key=OPENAI_API_KEY or None)
MAX_THREAD_MESSAGES = 10

def require_admin(x_admin_token: str | None = Header(None, alias="X-Admin-Token"), token: str | None = Query(None)):
    supplied = x_admin_token or token
    if supplied != ADMIN_TOKEN:
        raise HTTPException(status_code=401, detail="Unauthorized")

def trip_phase(check_in: str | None, check_out: str | None) -> str:
    try:
        ci = date.fromisoformat(str(check_in)) if check_in else None
        co = date.fromisoformat(str(check_out)) if check_out else None
    except Exception:
        return "unknown"
    today = date.today()
    if ci and today < ci: return "upcoming"
    if ci and co and ci <= today <= co: return "during"
    if co and today > co: return "past"
    return "unknown"

def pretty_status(s: str | None) -> str:
    if not isinstance(s, str): return "Unknown"
    m = s.strip().replace("_"," ").replace("-"," ")
    return m[:1].upper() + m[1:] if m else "Unknown"

class HostawayUnifiedWebhook(BaseModel):
    object: str
    event: str
    accountId: int
    data: dict
    body: str | None = None
    listingName: str | None = None
    date: str | None = None

def make_ai_reply(prompt: str, system_prompt: str) -> str:
    try:
        logging.info(f"Prompt length: {len(prompt)}")
        resp = openai_client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[{"role":"system","content":system_prompt},{"role":"user","content":prompt}],
            timeout=20,
        )
        return (resp.choices[0].message.content or "").strip()
    except Exception as e:
        logging.error(f"❌ OpenAI error: {e}")
        return "(Error generating reply.)"

@app.get("/learning", dependencies=[Depends(require_admin)])
def list_learning(limit: int = 100) -> List[Dict]:
    conn = sqlite3.connect(LEARNING_DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("""
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
    rows = conn.execute(
        "SELECT id, guest_message, ai_suggestion, user_reply, listing_id, guest_id, created_at FROM learning_examples ORDER BY id DESC LIMIT ?",
        (limit,),
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]

@app.get("/feedback", dependencies=[Depends(require_admin)])
def list_feedback(limit: int = 100) -> List[Dict]:
    conn = sqlite3.connect(LEARNING_DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("""
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
    rows = conn.execute(
        "SELECT id, conversation_id, question, ai_answer, rating, user, created_at FROM ai_feedback ORDER BY id DESC LIMIT ?",
        (limit,),
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]

@app.post("/unified-webhook")
async def unified_webhook(payload: HostawayUnifiedWebhook):
    logging.info(f"📬 Webhook received: {json.dumps(payload.dict(), indent=2)}")
    if payload.event != "message.received" or payload.object != "conversationMessage":
        return {"status": "ignored"}

    guest_msg = payload.data.get("body", "")
    if not guest_msg:
        if payload.data.get("attachments"): logging.info("📷 Skipping image-only message.")
        else: logging.info("🧾 Empty message skipped.")
        return {"status": "ignored"}

    conv_id = payload.data.get("conversationId")
    reservation_id = payload.data.get("reservationId")
    listing_id = payload.data.get("listingMapId")
    guest_id = payload.data.get("userId", "")
    communication_type = payload.data.get("communicationType", "channel")

    reservation = fetch_hostaway_reservation(reservation_id) or {}
    res = reservation.get("result", {}) or {}
    guest_name = res.get("guestFirstName", "Guest")
    check_in = res.get("arrivalDate", "N/A")
    check_out = res.get("departureDate", "N/A")
    guest_count = res.get("numberOfGuests", "N/A")
    status = pretty_status(payload.data.get("status") or res.get("status") or "Unknown")
    phase = trip_phase(check_in, check_out)

    if not listing_id: listing_id = res.get("listingId")
    if not guest_id: guest_id = res.get("guestId", "")

    CHANNEL_MAP = {"airbnb":"Airbnb","vrbo":"Vrbo","bookingcom":"Booking.com","direct":"Direct","expedia":"Expedia","channel":"Channel"}
    raw_channel = payload.data.get("communicationType") or res.get("source") or res.get("channelId") or "channel"
    channel_pretty = CHANNEL_MAP.get(str(raw_channel).lower(), str(raw_channel).capitalize())

    listing_obj = fetch_hostaway_listing(listing_id)
    addr_raw = (listing_obj or {}).get("result", {}).get("address") or "Address unavailable"
    if isinstance(addr_raw, dict):
        property_address = ", ".join(str(addr_raw.get(k, "")).strip() for k in ["address","city","state","zip","country"] if addr_raw.get(k)) or "Address unavailable"
    else:
        property_address = str(addr_raw)

    # Conversation context
    convo_obj = fetch_hostaway_conversation(conv_id) or {}
    msgs = []
    if "result" in convo_obj and "conversationMessages" in convo_obj["result"]:
        msgs = convo_obj["result"]["conversationMessages"] or []
    conversation_context = []
    for m in msgs[-MAX_THREAD_MESSAGES:]:
        sender = "Guest" if m.get("isIncoming") else "Host"
        body = m.get("body", "")
        if body: conversation_context.append(f"{sender}: {body}")
    context_str = "\n".join(conversation_context)
    if len(context_str) > 1200: context_str = context_str[-1200:]

    detected_intent = detect_intent(guest_msg)
    prev_examples = get_similar_learning_examples(guest_msg, listing_id)
    prev_answer = (f"Previously, you replied:\n\"{prev_examples[0][2]}\"\nUse this only as context.\n" if prev_examples and prev_examples[0][2] else "")
    _ = get_cancellation_policy_summary({}, res)

    # Calendar (if date-ish)
    calendar_summary = "No calendar check for this inquiry."
    if any(word in guest_msg.lower() for word in [
        "available","availability","book","open","stay","dates","night","reserve","weekend","extend",
        "extra night","holiday","christmas","spring break","july","august","september","october",
        "november","december","january","february","march","april","may","june","thanksgiving",
    ]):
        start_date, end_date = extract_date_range_from_message(guest_msg, res)
        calendar_days = fetch_hostaway_calendar(listing_id, start_date, end_date)
        if calendar_days:
            available_days = [d["date"] for d in calendar_days if d.get("isAvailable") or d.get("status") == "available"]
            unavailable_days = [d["date"] for d in calendar_days if not (d.get("isAvailable") or d.get("status") == "available")]
            if available_days:
                calendar_summary = f"For {start_date} to {end_date}: Available nights: {', '.join(available_days)}."
                if unavailable_days: calendar_summary += f" Unavailable: {', '.join(unavailable_days)}."
            else:
                calendar_summary = f"No available nights between {start_date} and {end_date}."
        else:
            calendar_summary = "Calendar data not available for these dates."

    # ECI/LCO policy + flags
    eci_flags = detect_time_adjust_request(guest_msg)
    eci_lco_eval = evaluate_time_adjust_options(listing_id, res) if (eci_flags["early"] or eci_flags["late"]) else None

    # Local recs / distance
    local_recs = ""
    distance_block = ""
    lat, lng = get_property_location(listing_obj, reservation)
    place_type, keyword = detect_place_type(guest_msg)
    if place_type and lat and lng:
        places = search_google_places(keyword, lat, lng, type_hint=place_type) or []
        local_recs = (f"Nearby {keyword}s from Google Maps for this property:\n" + "\n".join([f"- {p['name']} ({p.get('rating','N/A')}) – {p['address']}" for p in places[:3]])) if places else f"No {keyword}s found nearby from Google Maps."

    resolved = None
    distance_sentence = None
    try:
        dest_text = extract_destination_from_message(guest_msg)
        if dest_text and lat and lng:
            resolved = resolve_place_textsearch(dest_text, lat=lat, lng=lng)
            destination_for_matrix = f"{resolved['lat']},{resolved['lng']}" if resolved and resolved.get("lat") and resolved.get("lng") else dest_text
            distance_sentence = get_distance_drive_time(lat, lng, destination_for_matrix, units="imperial")
    except Exception as e:
        logging.exception(f"[DISTANCE] Pipeline error: {e}")

    # Special events
    special_event = detect_special_event(guest_msg)

    # Deterministic composer (preferred over LLM for risky cases)
    guest_portal = (res.get("guestPortalUrl") or "").strip()
    composed = compose_reply(
        guest_msg=guest_msg,
        intent=detected_intent,
        phase=phase,
        listing_result=listing_obj,
        reservation_result=res,
        eci_lco_eval=eci_lco_eval,
        eci_flags=eci_flags,
        distance_sentence=distance_sentence,
        place_type=place_type,
        resolved_place_name=(resolved or {}).get("name") if resolved else None,
        special_event=special_event,
        guest_portal_url=guest_portal if guest_portal else None,
    )

    if composed:
        ai_reply = clean_ai_reply(composed)
    else:
        # Fallback LLM with stricter rules
        eci_summary_line = ""
        if eci_lco_eval:
            eci_summary_line = (
                "ECI/LCO truth: "
                f"{eci_lco_eval['policy_summary']} requestedEarly={eci_flags['early']} requestedLate={eci_flags['late']}."
            )
        ai_prompt = (
            f"Thread (newest last):\n{context_str}\n"
            f"Reservation: {json.dumps({k: res.get(k) for k in ['arrivalDate','departureDate','numberOfGuests','guestFirstName','guestLastName','status','listingId']})}\n"
            f"Listing (trim): {json.dumps((listing_obj or {}).get('result', {}))[:600]}\n"
            f"Calendar: {calendar_summary}\n"
            f"{local_recs}\n"
            f"{('From property: ' + distance_sentence) if distance_sentence else ''}\n"
            f"{eci_summary_line}\n"
            f"Intent: {detected_intent}\nTrip phase: {phase}\nEvent: {special_event or 'None'}\n"
            f"{prev_answer}\n"
            "---\n"
            "Write a brief, human reply to the latest guest message. "
            "Rules:\n"
            "- Start with a short apology if the message implies a cleanliness/quality issue; offer to send cleaners; do NOT suggest the guest clean.\n"
            "- Never confirm early check-in/late check-out times. Instead, state the standard times and note the paid option if policy allows.\n"
            "- If the question is about deposit amount/policy, answer it concisely; do NOT send a payment link unless they explicitly ask for the link.\n"
            "- If the message mentions a known local event, acknowledge it and offer help.\n"
            "- If the destination is a restaurant, include one practical tip like going early on weekends; avoid invented specifics.\n"
            "- No greeting or sign-off. No emojis. Keep it concise and specific."
        )
        system_prompt = (
            "You are a human vacation-rental host texting from your phone. "
            "Voice: modern, relaxed, concise. Use contractions. No sign-offs, no emojis. "
            "Follow the rules above exactly."
        )
        ai_reply = clean_ai_reply(make_ai_reply(ai_prompt, system_prompt))

    button_meta = {
        "conv_id": payload.data.get("conversationId"),
        "listing_id": listing_id,
        "guest_id": guest_id,
        "type": communication_type,
        "guest_name": guest_name,
        "guest_message": guest_msg,
        "ai_suggestion": ai_reply,
        "check_in": check_in,
        "check_out": check_out,
        "guest_count": guest_count,
        "status": status,
        "detected_intent": detected_intent,
        "channel_pretty": channel_pretty,
        "property_address": property_address,
    }
    logging.info("button_meta: %s", json.dumps(button_meta, indent=2))

    from slack_sdk import WebClient
    slack_client = WebClient(token=os.getenv("SLACK_BOT_TOKEN"))
    try:
        slack_client.chat_postMessage(
            channel=os.getenv("SLACK_CHANNEL"),
            blocks=[
                {"type":"section","text":{"type":"mrkdwn","text":(
                    f"*{channel_pretty} message* from *{guest_name}*\n"
                    f"Property: *{property_address}*\n"
                    f"Dates: *{check_in} → {check_out}*\n"
                    f"Guests: *{guest_count}* | Status: *{status}*"
                )}},
                {"type":"section","text":{"type":"mrkdwn","text":f"> {guest_msg}"}},
                {"type":"section","text":{"type":"mrkdwn","text":f"*Suggested Reply:*\n>{ai_reply}"}},
                {"type":"context","elements":[{"type":"mrkdwn","text":f"*Intent:* {detected_intent}"}]},
                {"type":"actions","elements":[
                    {"type":"button","text":{"type":"plain_text","text":"✅ Send"},
                     "value": json.dumps({**button_meta,"action":"send"}),"action_id":"send"},
                    {"type":"button","text":{"type":"plain_text","text":"✏️ Edit"},
                     "value": json.dumps({**button_meta,"action":"edit"}),"action_id":"edit"},
                    {"type":"button","text":{"type":"plain_text","text":"📝 Write Your Own"},
                     "value": json.dumps({**button_meta,"action":"write_own"}),"action_id":"write_own"},
                ]},
            ],
            text="New message from guest",
        )
    except Exception as e:
        logging.error(f"❌ Slack send error: {e}")

    return {"status": "ok"}

@app.get("/ping")
def ping():
    return {"status": "ok"}
