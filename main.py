import os
import json
import logging
from typing import Optional, Dict, Any, List

from fastapi import FastAPI, Request
from pydantic import BaseModel
from slack_sdk import WebClient

from smart_intel import generate_reply
from utils import fetch_hostaway_listing, fetch_hostaway_reservation, fetch_hostaway_conversation
from db import already_processed, mark_processed, log_message_event, log_ai_exchange

app = FastAPI()

SLACK_BOT_TOKEN = os.getenv("SLACK_BOT_TOKEN", "")
SLACK_CHANNEL = os.getenv("SLACK_CHANNEL", "")
MAX_THREAD_MESSAGES = 10

logging.basicConfig(level=logging.INFO)

# ---------- Data Models ----------
class HostawayUnifiedWebhook(BaseModel):
    object: str
    event: str
    accountId: int
    data: dict
    body: Optional[str] = None
    listingName: Optional[str] = None
    date: Optional[str] = None

# ---------- Slack Helpers ----------
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

# ---------- Webhook Endpoint ----------
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

    # Fetch context
    reservation = fetch_hostaway_reservation(reservation_id) or {}
    res_data = reservation.get("result", {})
    guest_name = res_data.get("guestFirstName", "Guest")
    check_in = res_data.get("arrivalDate")
    check_out = res_data.get("departureDate")

    conversation = fetch_hostaway_conversation(conv_id) or {}
    messages = conversation.get("result", {}).get("conversationMessages", [])[-MAX_THREAD_MESSAGES:]
    history = [{"role": "guest" if m.get("isIncoming") else "host", "text": m.get("body", "")} for m in messages if m.get("body")]

    listing = fetch_hostaway_listing(listing_id) or {}
    listing_data = listing.get("result", {})

    context = {
        "guest_name": guest_name,
        "check_in_date": check_in,
        "check_out_date": check_out,
        "listing_info": listing_data,
        "reservation": res_data,
        "history": history
    }

    ai_reply = generate_reply(guest_message, context)
    log_ai_exchange(
        conversation_id=str(conv_id),
        guest_message=guest_message,
        ai_suggestion=ai_reply,
        intent="general"
    )

    # Slack message
    blocks = [
        {"type": "section", "text": {"type": "mrkdwn", "text": f"*{guest_name}*: {guest_message}"}},
        {"type": "section", "text": {"type": "mrkdwn", "text": f"*Suggested Reply:*\n{ai_reply}"}}
    ]
    post_to_slack(blocks)

    mark_processed(event_key)
    return {"status": "ok"}

@app.get("/ping")
def ping():
    return {"status": "ok"}
