# =========================
# File: main.py
# =========================
import os
import logging
import json
import sqlite3
from typing import List, Dict
from datetime import date, datetime

from fastapi import FastAPI, Depends, HTTPException, Header, Query
from pydantic import BaseModel

# Slack interactivity router
from slack_interactivity import router as slack_router

# DB bootstrap (legacy custom_responses)
from db import init_db

# Guarded AI composer
from assistant_core import compose_reply as ac_compose

# Utils you already have
from utils import (
    fetch_hostaway_listing,
    fetch_hostaway_reservation,
    fetch_hostaway_conversation,
    clean_ai_reply,
)

# ---- App & logging ----
logging.basicConfig(level=logging.INFO)
app = FastAPI()
app.include_router(slack_router, prefix="/slack")
init_db()

# ---- Env checks ----
LEARNING_DB_PATH = os.getenv("LEARNING_DB_PATH", "learning.db")
ADMIN_TOKEN = os.getenv("ADMIN_TOKEN", "dev-token")

REQUIRED_ENV_VARS = [
    "HOSTAWAY_CLIENT_ID",
    "HOSTAWAY_CLIENT_SECRET",
    "OPENAI_API_KEY",           # assistant_core needs this
    "SLACK_CHANNEL",
    "SLACK_BOT_TOKEN",
    "SLACK_SIGNING_SECRET",
    "GOOGLE_PLACES_API_KEY",
    "GOOGLE_DISTANCE_MATRIX_API_KEY",
]
missing = [v for v in REQUIRED_ENV_VARS if not os.getenv(v)]
if missing:
    raise RuntimeError(f"Missing required environment variables: {missing}")

MAX_THREAD_MESSAGES = 10

# ---------- Helpers for Slack header formatting ----------
CHANNEL_ID_MAP = {
    2018: "Airbnb (Official)",
    2002: "HomeAway",
    2005: "Booking.com",
    2007: "Expedia",
    2009: "HomeAway (iCal)",
    2010: "Vrbo (iCal)",
    2000: "Direct",
    2013: "Booking Engine",
    2015: "Custom iCal",
    2016: "TripAdvisor (iCal)",
    2017: "WordPress",
    2019: "Marriott",
    2020: "Partner",
    2021: "GDS",
    2022: "Google",
}

COMM_TYPE_MAP = {
    "airbnb": "Airbnb",
    "airbnbofficial": "Airbnb (Official)",
    "vrbo": "Vrbo",
    "bookingcom": "Booking.com",
    "direct": "Direct",
    "expedia": "Expedia",
    "channel": "Channel",
}

RES_STATUS_ALLOWED = {
    "new": "New",
    "modified": "Modified",
    "cancelled": "Cancelled",
    "ownerstay": "Owner Stay",
    "pending": "Pending",
    "awaitingpayment": "Awaiting Payment",
    "declined": "Declined",
    "expired": "Expired",
    "inquiry": "Inquiry",
    "inquirypreapproved": "Inquiry (Preapproved)",
    "inquirydenied": "Inquiry (Denied)",
    "inquirytimedout": "Inquiry (Timed Out)",
    "inquirynotpossible": "Inquiry (Not Possible)",
}

def format_us_date(d: str | None) -> str:
    """
    Converts 'YYYY-MM-DD' or 'YYYY-MM-DDTHH:MM:SS' -> 'MM/DD/YYYY'.
    Returns original string if parsing fails.
    """
    if not d:
        return "N/A"
    s = str(d).strip()
    for fmt in ("%Y-%m-%d", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M:%S"):
        try:
            dt = datetime.strptime(s[:19], fmt)
            return dt.strftime("%m/%d/%Y")
        except Exception:
            continue
    return s

def format_price(amount, currency: str | None = "USD") -> str:
    try:
        val = float(amount)
    except (TypeError, ValueError):
        return "—"
    cur = (currency or "USD").upper()
    symbol = "$" if cur == "USD" else f"{cur} "
    return f"{symbol}{val:,.2f}"

def pretty_res_status(s: str | None) -> str:
    if not s:
        return "Unknown"
    key = str(s).strip().lower().replace("_", "").replace("-", "")
    return RES_STATUS_ALLOWED.get(key, s.capitalize())

def pretty_status(s: str | None) -> str:
    if not isinstance(s, str):
        return "Unknown"
    m = s.strip().replace("_", " ").replace("-", " ")
    return m[:1].upper() + m[1:] if m else "Unknown"

def channel_label_from(channel_id: int | None, communication_type: str | None) -> str:
    if isinstance(channel_id, int) and channel_id in CHANNEL_ID_MAP:
        name = CHANNEL_ID_MAP[channel_id]
        # Normalize to clean labels
        if "vrbo" in name.lower():
            return "Vrbo"
        if "airbnb" in name.lower():
            return "Airbnb"
        return name
    if communication_type:
        key = str(communication_type).lower().strip()
        return COMM_TYPE_MAP.get(key, key.capitalize())
    return "Channel"

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

# ---------- Admin endpoints ----------
def require_admin(
    x_admin_token: str | None = Header(None, alias="X-Admin-Token"),
    token: str | None = Query(None),
):
    supplied = x_admin_token or token
    if supplied != ADMIN_TOKEN:
        raise HTTPException(status_code=401, detail="Unauthorized")

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

# ---------- Webhook ----------
class HostawayUnifiedWebhook(BaseModel):
    object: str
    event: str
    accountId: int
    data: dict
    body: str | None = None
    listingName: str | None = None
    date: str | None = None

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
    communication_type = payload.data.get("communicationType", "channel")
    channel_id = payload.data.get("channelId")

    # Reservation
    reservation = fetch_hostaway_reservation(reservation_id) or {}
    res = reservation.get("result", {}) or {}

    guest_name = res.get("guestFirstName", "Guest")
    check_in = res.get("arrivalDate", "N/A")
    check_out = res.get("departureDate", "N/A")
    guest_count = res.get("numberOfGuests", "N/A")

    # Reservation status & total price
    res_status_pretty = pretty_res_status(res.get("status"))
    total_price_str = format_price(res.get("totalPrice"), (res.get("currency") or "USD"))

    # Message transport status (from webhook payload)
    msg_status = pretty_status(payload.data.get("status") or "sent")

    phase = trip_phase(check_in, check_out)

    if not listing_id:
        listing_id = res.get("listingId")
    if not guest_id:
        guest_id = res.get("guestId", "")

    # Channel label from channelId/communicationType
    channel_pretty = channel_label_from(channel_id, communication_type)

    # Fetch listing to build address
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

    # ---- Guarded AI (assistant_core) ----
    conversation_history = [
        {"role": "guest" if m.get("isIncoming") else "host", "text": m.get("body", "")}
        for m in msgs[-MAX_THREAD_MESSAGES:]
        if m.get("body")
    ]

    # Minimal property profile (sane defaults; assistant_core also has defaults)
    property_profile = {"checkin_time": "4:00 PM", "checkout_time": "11:00 AM"}

    meta_for_ai = {
        "listing_id": (str(listing_id) if listing_id is not None else ""),
        "listing_map_id": listing_id,
        "reservation_id": reservation_id,
        "check_in": check_in if isinstance(check_in, str) else str(check_in),
        "check_out": check_out if isinstance(check_out, str) else str(check_out),
        "reservation_status": (res.get("status") or "").strip(),  # raw status for guards
        "property_profile": property_profile,
        "policies": {},
    }

    ai_json, _unused_blocks = ac_compose(
        guest_message=guest_msg,
        conversation_history=conversation_history,
        meta=meta_for_ai,
    )
    ai_reply = clean_ai_reply(ai_json.get("reply", "") or "")
    detected_intent = ai_json.get("intent", "other")

    # US dates
    us_check_in = format_us_date(check_in)
    us_check_out = format_us_date(check_out)

    # Slack button meta
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
        "status": res_status_pretty,  # reservation status
        "detected_intent": detected_intent,
        "channel_pretty": channel_pretty,
        "property_address": property_address,
        "price": total_price_str,
    }
    logging.info("button_meta: %s", json.dumps(button_meta, indent=2))

    # Slack blocks
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
                            f"Dates: *{us_check_in} → {us_check_out}*\n"
                            f"Guests: *{guest_count}* | Res: *{res_status_pretty}* | Price: *{total_price_str}*"
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
                    "elements": [
                        {"type": "mrkdwn", "text": f"*Intent:* {detected_intent}  •  *Msg:* {msg_status}"}
                    ],
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

# ---------- Health ----------
@app.get("/ping")
def ping():
    return {"status": "ok"}
