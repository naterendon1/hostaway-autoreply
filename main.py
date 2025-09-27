
# ✅ FULL main.py with Smart AI, Slack buttons, real Hostaway data
import os
import json
import logging
from typing import Optional, Dict, Any, List
from fastapi import FastAPI, Request
from pydantic import BaseModel
from slack_sdk import WebClient
from smart_intel import generate_reply
from utils import (
    fetch_hostaway_listing,
    fetch_hostaway_reservation,
    fetch_hostaway_conversation,
)
from amenities_index import AmenitiesIndex
from db import (
    already_processed,
    mark_processed,
    log_message_event,
    log_ai_exchange,
)

app = FastAPI()

# ENV
SLACK_BOT_TOKEN = os.getenv("SLACK_BOT_TOKEN", "")
SLACK_CHANNEL = os.getenv("SLACK_CHANNEL", "")
MAX_THREAD_MESSAGES = 10

logging.basicConfig(level=logging.INFO)

# Slack helpers
def post_to_slack(blocks: List[Dict[str, Any]], text: str = "New guest message") -> bool:
    if not SLACK_CHANNEL or not SLACK_BOT_TOKEN:
        logging.error("Slack config missing")
        return False
    client = WebClient(token=SLACK_BOT_TOKEN)
    try:
        client.chat_postMessage(channel=SLACK_CHANNEL, blocks=blocks, text=text)
        return True
    except Exception as e:
        logging.error(f"Slack error: {e}")
        return False

# Webhook payload model
class HostawayUnifiedWebhook(BaseModel):
    object: str
    event: str
    accountId: int
    data: dict
    body: Optional[str] = None
    listingName: Optional[str] = None
    date: Optional[str] = None

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
    guest_id = data.get("userId")
    communication_type = data.get("communicationType", "channel")

    # Fetch data
    reservation = fetch_hostaway_reservation(reservation_id) or {}
    res_data = reservation.get("result", {})
    guest_name = res_data.get("guestFirstName", "Guest")
    check_in = res_data.get("arrivalDate")
    check_out = res_data.get("departureDate")
    guest_count = res_data.get("numberOfGuests", "N/A")
    total_price = res_data.get("totalPrice")
    guest_portal_url = res_data.get("guestPortalUrl", "")
    currency = res_data.get("currency", "USD")

    conversation = fetch_hostaway_conversation(conv_id) or {}
    messages = conversation.get("result", {}).get("conversationMessages", [])[-MAX_THREAD_MESSAGES:]
    history = [{"role": "guest" if m.get("isIncoming") else "host", "text": m.get("body", "")} for m in messages if m.get("body")]

    listing = fetch_hostaway_listing(listing_id) or {}
    listing_data = listing.get("result", {})
    amenities = AmenitiesIndex(listing_data)

    # Address
    addr_raw = listing_data.get("address", "N/A")
    if isinstance(addr_raw, dict):
        address = ", ".join([str(addr_raw.get(k, "")) for k in ["address", "city", "state", "zip", "country"] if addr_raw.get(k)])
    else:
        address = str(addr_raw)

    context = {
        "guest_name": guest_name,
        "check_in_date": check_in,
        "check_out_date": check_out,
        "listing_info": listing_data,
        "reservation": res_data,
        "history": history
    }

    # AI reply
    ai_reply = generate_reply(guest_message, context)
    log_ai_exchange(
        conversation_id=str(conv_id),
        guest_message=guest_message,
        ai_suggestion=ai_reply,
        intent="general"
    )

    # Slack block formatting
    checkin_fmt = check_in or "N/A"
    checkout_fmt = check_out or "N/A"
    price_str = f"${float(total_price):,.2f}" if total_price else "N/A"

    header_text = (
        f"*{communication_type.title()} message* from *{guest_name}*
"
        f"Property: *{address}*
"
        f"Dates: *{checkin_fmt} → {checkout_fmt}*
"
        f"Guests: *{guest_count}* | Res: *{res_data.get('status', 'N/A')}* | Price: *{price_str}*"
    )

    button_meta = {
        "conv_id": conv_id,
        "listing_id": listing_id,
        "guest_id": guest_id,
        "guest_name": guest_name,
        "guest_message": guest_message,
        "ai_suggestion": ai_reply,
        "check_in": check_in,
        "check_out": check_out,
        "guest_count": guest_count,
        "status": res_data.get("status"),
        "channel_pretty": communication_type.title(),
        "property_address": address,
        "price": price_str,
        "guest_portal_url": guest_portal_url
    }

    actions = [
        {"type": "button", "text": {"type": "plain_text", "text": "✅ Send"}, "value": json.dumps({**button_meta, "action": "send"}), "action_id": "send"},
        {"type": "button", "text": {"type": "plain_text", "text": "✏️ Edit"}, "value": json.dumps({**button_meta, "action": "edit"}), "action_id": "edit"},
    ]

    if guest_portal_url:
        actions.append({
            "type": "button",
            "style": "primary",
            "text": {"type": "plain_text", "text": "🔗 Send guest portal"},
            "value": json.dumps({**button_meta, "action": "send_guest_portal"}),
            "action_id": "send_guest_portal"
        })

    blocks = [
        {"type": "section", "text": {"type": "mrkdwn", "text": header_text}},
        {"type": "section", "text": {"type": "mrkdwn", "text": f"> {guest_message}"}},
        {"type": "section", "text": {"type": "mrkdwn", "text": f"*Suggested Reply:*
{ai_reply}"}},
        {"type": "actions", "elements": actions}
    ]

    post_to_slack(blocks)
    mark_processed(event_key)
    return {"status": "ok"}

@app.get("/ping")
def ping():
    return {"status": "ok"}
