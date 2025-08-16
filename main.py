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

# New guarded AI core
from assistant_core import compose_reply as ac_compose

# Slack interactivity (events/actions) router
from slack_interactivity import router as slack_router

# DB init
from db import init_db

# Utils (keep only what we actually use)
from utils import (
    fetch_hostaway_listing,
    fetch_hostaway_reservation,
    fetch_hostaway_conversation,
    get_cancellation_policy_summary,
    clean_ai_reply,
    extract_date_range_from_message,
    fetch_hostaway_calendar,
    is_date_available,
    next_available_dates,
    detect_intent,
    get_property_location,
    search_google_places,
    detect_place_type,
    extract_destination_from_message,
    resolve_place_textsearch,
    get_distance_drive_time,
    detect_time_adjust_request,
    evaluate_time_adjust_options,
    detect_deposit_request,
)

logging.basicConfig(level=logging.INFO)
app = FastAPI()
app.include_router(slack_router, prefix="/slack")
init_db()

# -------------------- Admin + DB paths --------------------
LEARNING_DB_PATH = os.getenv("LEARNING_DB_PATH", "learning.db")
ADMIN_TOKEN = os.getenv("ADMIN_TOKEN", "dev-token")

def require_admin(
    x_admin_token: str | None = Header(None, alias="X-Admin-Token"),
    token: str | None = Query(None),
):
    supplied = x_admin_token or token
    if supplied != ADMIN_TOKEN:
        raise HTTPException(status_code=401, detail="Unauthorized")

# -------------------- Env checks --------------------
REQUIRED_ENV_VARS = [
    "HOSTAWAY_CLIENT_ID",
    "HOSTAWAY_CLIENT_SECRET",
    "OPENAI_API_KEY",
    "SLACK_CHANNEL",
    "SLACK_BOT_TOKEN",
    "SLACK_SIGNING_SECRET",
    "GOOGLE_PLACES_API_KEY",
    "GOOGLE_DISTANCE_MATRIX_API_KEY",
]
missing = [v for v in REQUIRED_ENV_VARS if not os.getenv(v)]
if missing:
    raise RuntimeError(f"Missing required environment variables: {missing}")

HOSTAWAY_API_BASE = "https://api.hostaway.com/v1"
SLACK_CHANNEL = os.getenv("SLACK_CHANNEL")
MAX_THREAD_MESSAGES = 10

# -------------------- Helpers --------------------
def trip_phase(check_in: str | None, check_out: str | None) -> str:
    try:
        ci = date.fromisoformat(str(check_in)) if check_in else None
        co = date.fromisoformat(str(check_out)) if check_out else None
    except Exception:
        return "unknown"
    today = date.today()
    if ci and today < ci:
        return "upcoming"
    if ci and co and ci <= today <= co:
        return "during"
    if co and today > co:
        return "past"
    return "unknown"

def pretty_status(s: str | None) -> str:
    if not isinstance(s, str):
        return "Unknown"
    m = s.strip().replace("_", " ").replace("-", " ")
    return m[:1].upper() + m[1:] if m else "Unknown"

def _listing_times(listing_obj: dict) -> Dict[str, str]:
    """Extract check-in/out times with defaults (why: prevents ECI/LCO misconfirmations)."""
    res = (listing_obj or {}).get("result", {}) if isinstance(listing_obj, dict) else {}
    return {
        "checkin_time": res.get("checkInTime") or "4:00 PM",
        "checkout_time": res.get("checkOutTime") or "11:00 AM",
    }

# -------------------- Models --------------------
class HostawayUnifiedWebhook(BaseModel):
    object: str
    event: str
    accountId: int
    data: dict
    body: str | None = None
    listingName: str | None = None
    date: str | None = None

# -------------------- Admin endpoints --------------------
@app.get("/learning", dependencies=[Depends(require_admin)])
def list_learning(limit: int = 100) -> List[Dict]:
    conn = sqlite3.connect(LEARNING_DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS learning_examples (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            guest_message TEXT,
            ai_suggestion TEXT,
            user_reply TEXT,
            listing_id TEXT,
            guest_id TEXT,
            created_at TEXT
        )
        """
    )
    rows = conn.execute(
        "SELECT id, guest_message, ai_suggestion, user_reply, listing_id, guest_id, created_at "
        "FROM learning_examples ORDER BY id DESC LIMIT ?",
        (limit,),
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]

@app.get("/feedback", dependencies=[Depends(require_admin)])
def list_feedback(limit: int = 100) -> List[Dict]:
    conn = sqlite3.connect(LEARNING_DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS ai_feedback (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            conversation_id TEXT,
            question TEXT,
            ai_answer TEXT,
            rating TEXT,
            user TEXT,
            created_at TEXT
        )
        """
    )
    rows = conn.execute(
        "SELECT id, conversation_id, question, ai_answer, rating, user, created_at "
        "FROM ai_feedback ORDER BY id DESC LIMIT ?",
        (limit,),
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]

# -------------------- Webhook --------------------
@app.post("/unified-webhook")
async def unified_webhook(payload: HostawayUnifiedWebhook):
    logging.info(f"📬 Webhook received: {json.dumps(payload.dict(), indent=2)}")

    # Only handle inbound guest messages
    if payload.event != "message.received" or payload.object != "conversationMessage":
        return {"status": "ignored"}

    guest_msg = payload.data.get("body", "")
    if not guest_msg:
        if payload.data.get("attachments"):
            logging.info("📷 Skipping image-only message.")
        else:
            logging.info("🧾 Empty message skipped.")
        return {"status": "ignored"}

    # Basic IDs/context
    conv_id = payload.data.get("conversationId")
    reservation_id = payload.data.get("reservationId")
    listing_id = payload.data.get("listingMapId")
    guest_id = payload.data.get("userId", "")
    communication_type = payload.data.get("communicationType", "channel")  # airbnb, vrbo, direct

    # Reservation
    reservation = fetch_hostaway_reservation(reservation_id) or {}
    res = reservation.get("result", {}) or {}

    guest_name = res.get("guestFirstName", "Guest")
    check_in = res.get("arrivalDate", "N/A")
    check_out = res.get("departureDate", "N/A")
    guest_count = res.get("numberOfGuests", "N/A")
    status_raw = payload.data.get("status") or res.get("status") or "Unknown"
    status = pretty_status(status_raw)
    phase = trip_phase(check_in, check_out)

    if not listing_id:
        listing_id = res.get("listingId")
    if not guest_id:
        guest_id = res.get("guestId", "")

    # Channel label
    CHANNEL_MAP = {
        "airbnb": "Airbnb",
        "vrbo": "Vrbo",
        "bookingcom": "Booking.com",
        "direct": "Direct",
        "expedia": "Expedia",
        "channel": "Channel",
    }
    raw_channel = payload.data.get("communicationType") or res.get("source") or res.get("channelId") or "channel"
    channel_pretty = CHANNEL_MAP.get(str(raw_channel).lower(), str(raw_channel).capitalize())

    # Listing (for address + check-in/out times)
    listing_obj = fetch_hostaway_listing(listing_id)
    addr_raw = (listing_obj or {}).get("result", {}).get("address") or "Address unavailable"
    if isinstance(addr_raw, dict):
        property_address = (
            ", ".join(
                str(addr_raw.get(k, "")).strip()
                for k in ["address", "city", "state", "zip", "country"]
                if addr_raw.get(k)
            )
            or "Address unavailable"
        )
    else:
        property_address = str(addr_raw)

    # Conversation context (last few)
    convo_obj = fetch_hostaway_conversation(conv_id) or {}
    msgs = []
    if "result" in convo_obj and "conversationMessages" in convo_obj["result"]:
        msgs = convo_obj["result"]["conversationMessages"] or []

    # -------------------- Local recs + distance (optional) --------------------
    # These enrich Slack context and can be used by your human follow-up.
    local_recs = ""
    distance_block = ""

    lat, lng = get_property_location(listing_obj, reservation)

    place_type, keyword = detect_place_type(guest_msg)
    if place_type and lat and lng:
        places = search_google_places(keyword, lat, lng, type_hint=place_type) or []
        if places:
            local_recs = (
                f"Nearby {keyword}s from Google Maps for this property:\n"
                + "\n".join([f"- {p['name']} ({p.get('rating','N/A')}) – {p['address']}" for p in places[:3]])
            )

    try:
        dest_text = extract_destination_from_message(guest_msg)
        if dest_text and lat and lng:
            resolved = resolve_place_textsearch(dest_text, lat=lat, lng=lng)
            destination_for_matrix = (
                f"{resolved['lat']},{resolved['lng']}"
                if resolved and resolved.get("lat") and resolved.get("lng")
                else dest_text
            )
            distance_sentence = get_distance_drive_time(lat, lng, destination_for_matrix, units="imperial")
            pretty_name = resolved["name"] if resolved and resolved.get("name") else dest_text
            distance_block = f"From the property to {pretty_name}: {distance_sentence}"
    except Exception as e:
        logging.exception(f"[DISTANCE] Pipeline error: {e}")

    # -------------------- Calendar peek if guest asked about dates --------------------
    calendar_summary = "No calendar check for this inquiry."
    if any(
        word in guest_msg.lower()
        for word in [
            "available",
            "availability",
            "book",
            "open",
            "stay",
            "dates",
            "night",
            "reserve",
            "weekend",
            "extend",
            "extra night",
            "holiday",
            "christmas",
            "spring break",
            "july",
            "august",
            "september",
            "october",
            "november",
            "december",
            "january",
            "february",
            "march",
            "april",
            "may",
            "june",
            "thanksgiving",
        ]
    ):
        start_date, end_date = extract_date_range_from_message(guest_msg, res)
        calendar_days = fetch_hostaway_calendar(listing_id, start_date, end_date)
        if calendar_days:
            available_days = [
                d["date"]
                for d in calendar_days
                if d.get("isAvailable") or d.get("status") == "available"
            ]
            unavailable_days = [
                d["date"]
                for d in calendar_days
                if not (d.get("isAvailable") or d.get("status") == "available")
            ]
            if available_days:
                calendar_summary = (
                    f"For {start_date} to {end_date}: "
                    f"Available nights: {', '.join(available_days)}."
                )
                if unavailable_days:
                    calendar_summary += f" Unavailable: {', '.join(unavailable_days)}."
            else:
                calendar_summary = f"No available nights between {start_date} and {end_date}."
        else:
            calendar_summary = "Calendar data not available for these dates."

    # -------------------- ECI/LCO smart policy injection (optional info) --------------------
    eci_lco_flags = detect_time_adjust_request(guest_msg)
    if eci_lco_flags["early"] or eci_lco_flags["late"]:
        _ = evaluate_time_adjust_options(listing_id, res)  # assistant_core enforces guardrails

    # -------------------- Build conversation history for the AI --------------------
    conversation_history = [
        {"role": "guest" if m.get("isIncoming") else "host", "text": m.get("body", "")}
        for m in msgs[-MAX_THREAD_MESSAGES:]
        if m.get("body")
    ]

    # -------------------- Property profile + meta for assistant_core --------------------
    property_profile = _listing_times(listing_obj)
    meta_for_ai = {
        "listing_id": str(listing_id) if listing_id is not None else "",
        "listing_map_id": listing_id,
        "reservation_id": reservation_id,
        "check_in": check_in if isinstance(check_in, str) else str(check_in),
        "check_out": check_out if isinstance(check_out, str) else str(check_out),
        "reservation_status": (res.get("status") or "").strip(),  # e.g., pending, awaitingPayment, inquiry
        "property_profile": property_profile,
        "policies": {
            # Optionally override defaults via env:
            # "early_checkin_fee": 50,
            # "late_checkout_fee": 50,
        },
    }

    # -------------------- Call guarded AI --------------------
    ai_json, _unused = ac_compose(
        guest_message=guest_msg,
        conversation_history=conversation_history,
        meta=meta_for_ai,
    )
    ai_reply = clean_ai_reply(ai_json.get("reply", "") or "")
    detected_intent = ai_json.get("intent", detect_intent(guest_msg))

    # -------------------- Slack message --------------------
    button_meta = {
        "conv_id": conv_id,
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
        # Optional context if you want to see what the AI used:
        # "calendar_summary": calendar_summary,
        # "local_recs": local_recs,
        # "distance_block": distance_block,
    }

    logging.info("button_meta: %s", json.dumps(button_meta, indent=2))

    from slack_sdk import WebClient
    slack_client = WebClient(token=os.getenv("SLACK_BOT_TOKEN"))
    try:
        slack_client.chat_postMessage(
            channel=os.getenv("SLACK_CHANNEL"),
            blocks=[
                {
                    "type": "section",
                    "text": {
                        "type": "mrkdwn",
                        "text": (
                            f"*{channel_pretty} message* from *{guest_name}*\n"
                            f"Property: *{property_address}*\n"
                            f"Dates: *{check_in} → {check_out}*\n"
                            f"Guests: *{guest_count}* | Status: *{status}*"
                        ),
                    },
                },
                {"type": "section", "text": {"type": "mrkdwn", "text": f"> {guest_msg}"}},
                {
                    "type": "section",
                    "text": {"type": "mrkdwn", "text": f"*Suggested Reply:*\n>{ai_reply}"},
                },
                {
                    "type": "context",
                    "elements": [{"type": "mrkdwn", "text": f"*Intent:* {detected_intent}"}],
                },
                {
                    "type": "actions",
                    "elements": [
                        {
                            "type": "button",
                            "text": {"type": "plain_text", "text": "✅ Send"},
                            "value": json.dumps({**button_meta, "action": "send"}),
                            "action_id": "send",
                        },
                        {
                            "type": "button",
                            "text": {"type": "plain_text", "text": "✏️ Edit"},
                            "value": json.dumps({**button_meta, "action": "edit"}),
                            "action_id": "edit",
                        },
                        {
                            "type": "button",
                            "text": {"type": "plain_text", "text": "📝 Write Your Own"},
                            "value": json.dumps({**button_meta, "action": "write_own"}),
                            "action_id": "write_own",
                        },
                    ],
                },
            ],
            text="New message from guest",
        )
    except Exception as e:
        logging.error(f"❌ Slack send error: {e}")

    return {"status": "ok"}

# -------------------- Health --------------------
@app.get("/ping")
def ping():
    return {"status": "ok"}
