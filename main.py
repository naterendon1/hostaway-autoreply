import os
import json
import logging
from typing import Optional, Dict, Any, List

from fastapi import FastAPI, Request
from pydantic import BaseModel
from slack_sdk import WebClient
from slack_sdk.errors import SlackApiError

from smart_intel import generate_reply
from utils import (
    fetch_hostaway_listing,
    fetch_hostaway_reservation,
    fetch_hostaway_conversation
)
from db import already_processed, mark_processed, log_message_event, log_ai_exchange

# ---------- Setup ----------
app = FastAPI()
logging.basicConfig(level=logging.INFO)

SLACK_BOT_TOKEN = os.getenv("SLACK_BOT_TOKEN", "")
SLACK_CHANNEL = os.getenv("SLACK_CHANNEL", "")
MAX_THREAD_MESSAGES = 10

slack_client = WebClient(token=SLACK_BOT_TOKEN)

# ---------- Models ----------
class HostawayUnifiedWebhook(BaseModel):
    object: str
    event: str
    accountId: int
    data: dict
    body: Optional[str] = None
    listingName: Optional[str] = None
    date: Optional[str] = None


# ---------- Slack Helper ----------
def post_to_slack(blocks: List[Dict[str, Any]], text: str = "New guest message") -> bool:
    if not SLACK_CHANNEL or not SLACK_BOT_TOKEN:
        logging.error("Slack config missing")
        return False
    try:
        slack_client.chat_postMessage(channel=SLACK_CHANNEL, blocks=blocks, text=text)
        return True
    except SlackApiError as e:
        logging.error(f"Slack error: {e.response['error']}")
        return False
    except Exception as e:
        logging.error(f"Slack unknown error: {e}")
        return False


# ---------- Main Webhook ----------
@app.post("/unified-webhook")
async def unified_webhook(payload: HostawayUnifiedWebhook):
    if payload.event != "message.received" or payload.object != "conversationMessage":
        return {"status": "ignored"}

    data = payload.data
    event_key = f"{payload.object}:{payload.event}:{data.get('id') or data.get('conversationId')}"

    if already_processed(event_key):
        return {"status": "duplicate"}

    guest_message = data.get("body", "")
    if not guest_message:
        mark_processed(event_key)
        return {"status": "ignored"}

    conv_id = data.get("conversationId")
    reservation_id = data.get("reservationId")
    listing_id = data.get("listingMapId")
    communication_type = data.get("type", "Message").lower()

    # ---------- Context ----------
    reservation = fetch_hostaway_reservation(reservation_id) or {}
    res_data = reservation.get("result", {})

    guest_name = res_data.get("guestFirstName", "Guest")
    guest_count = res_data.get("numberOfGuests", "N/A")
    check_in = res_data.get("arrivalDate")
    check_out = res_data.get("departureDate")
    price_str = res_data.get("payoutAmount", "N/A")

    listing = fetch_hostaway_listing(listing_id) or {}
    listing_data = listing.get("result", {})
    address_info = listing_data.get("address", {})
    if isinstance(address_info, dict):
        address = address_info.get("address1", "Unknown address")
    elif isinstance(address_info, str):
        address = address_info
    else:
        address = "Unknown address"

    conversation = fetch_hostaway_conversation(conv_id) or {}
    messages = conversation.get("result", {}).get("conversationMessages", [])[-MAX_THREAD_MESSAGES:]
    history = [
        {"role": "guest" if m.get("isIncoming") else "host", "text": m.get("body", "")}
        for m in messages if m.get("body")
    ]

    checkin_fmt = check_in or "?"
    checkout_fmt = check_out or "?"

    context = {
        "guest_name": guest_name,
        "check_in_date": checkin_fmt,
        "check_out_date": checkout_fmt,
        "guest_count": guest_count,
        "listing_info": listing_data,
        "reservation": res_data,
        "history": history
    }

    # ---------- AI Suggestion ----------
    ai_reply = generate_reply(guest_message, context)
    log_ai_exchange(
        conversation_id=str(conv_id),
        guest_message=guest_message,
        ai_suggestion=ai_reply,
        intent="general"
    )

    # ---------- Slack Blocks ----------
    summary_text = f"""
*{communication_type.title()} message* from *{guest_name}!*
Property: *{address}*
Dates: *{checkin_fmt} → {checkout_fmt}*
Guests: *{guest_count}* | Res: *{res_data.get('status', 'N/A')}* | Price: *{price_str}*
""".strip()

    blocks = [
        {"type": "section", "text": {"type": "mrkdwn", "text": summary_text}},
        {"type": "section", "text": {"type": "mrkdwn", "text": f"*Message:* {guest_message}"}},
        {"type": "section", "text": {"type": "mrkdwn", "text": f"*Suggested Reply:*\n{ai_reply}"}},
        {
            "type": "actions",
            "elements": [
                {"type": "button", "text": {"type": "plain_text", "text": "✅ Send"}, "action_id": "send_ai_reply", "style": "primary"},
                {"type": "button", "text": {"type": "plain_text", "text": "✏️ Edit"}, "action_id": "edit_ai_reply"},
                {"type": "button", "text": {"type": "plain_text", "text": "🔗 Send Guest Portal"}, "action_id": "send_portal_link"},
            ]
        }
    ]

    post_to_slack(blocks)
    mark_processed(event_key)
    return {"status": "ok"}


@app.get("/ping")
def ping():
    return {"status": "ok"}
