import os
import json
import logging
from typing import Optional, Dict, Any, List
from datetime import datetime
from places import build_local_recs, should_fetch_local_recs


from fastapi import FastAPI
from pydantic import BaseModel
from slack_sdk import WebClient

from smart_intel import generate_reply
from utils import fetch_hostaway_listing, fetch_hostaway_reservation, fetch_hostaway_conversation
from db import already_processed, mark_processed, log_ai_exchange
from slack_interactivity import router as slack_router

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

    structured_listing_info = {
        "name": listing_data.get("name"),
        "address": listing_data.get("address"),
        "bedrooms": listing_data.get("bedroomsNumber"),
        "beds": listing_data.get("bedsNumber"),
        "bathrooms": listing_data.get("bathroomsNumber"),
        "amenities": listing_data.get("listingAmenities", []),
        "bed_types": listing_data.get("listingBedTypes", []),
        "check_in_time": listing_data.get("checkInTimeStart"),
        "check_out_time": listing_data.get("checkOutTime"),
        "wifi_username": listing_data.get("wifiUsername"),
        "wifi_password": listing_data.get("wifiPassword"),
        "latitude": listing_data.get("lat"),
        "longitude": listing_data.get("lng"),
        "description": listing_data.get("description"),
        "house_rules": listing_data.get("houseRules"),
    }

    # Google-powered nearby places
    lat = listing_data.get("lat")
    lng = listing_data.get("lng")

    nearby_places = []
    if should_fetch_local_recs(guest_message):
        nearby_places = build_local_recs(lat, lng, guest_message)

    context = {
        "guest_name": guest_name,
        "check_in_date": check_in,
        "check_out_date": check_out,
        "listing_info": structured_listing_info,
        "reservation": res_data,
        "history": history,
        "nearby_places": nearby_places
    }

    ai_reply = generate_reply(guest_message, context)
    log_ai_exchange(
        conversation_id=str(conv_id),
        guest_message=guest_message,
        ai_suggestion=ai_reply,
        intent="general"
    )

    # Format Slack display values
    checkin_fmt = datetime.strptime(check_in, "%Y-%m-%d").strftime("%m-%d-%Y") if check_in else "N/A"
    checkout_fmt = datetime.strptime(check_out, "%Y-%m-%d").strftime("%m-%d-%Y") if check_out else "N/A"

    price = res_data.get("grandTotalPrice") or res_data.get("totalPrice") or res_data.get("price") or "N/A"
    price_str = f"${float(price):,.2f}" if isinstance(price, (int, float, str)) and str(price).replace('.', '', 1).isdigit() else "$N/A"

    guest_count = res_data.get("numberOfGuests") or res_data.get("adults") or "?"
    platform = res_data.get("platform", "Unknown")
    property_name = listing_data.get("name") or listing_data.get("internalListingName") or "Unknown Property"

    meta_payload = {
        "conv_id": conv_id,
        "guest_message": guest_message,
        "guest_name": guest_name,
        "reply": ai_reply,
        "ai_suggestion": ai_reply,
        "type": "email",
        "status": res_data.get("status", "N/A"),
        "check_in": checkin_fmt,
        "check_out": checkout_fmt,
        "guest_count": guest_count,
        "channel_pretty": platform,
        "property_address": structured_listing_info["address"],
        "listing_id": listing_id,
        "guest_id": res_data.get("guestId"),
    }

    blocks = [
        {
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": (
                    f"*✉️ Message from {guest_name}*\n"
                    f"🏡 *Property:* {property_name}\n"
                    f"📅 *Dates:* {checkin_fmt} → {checkout_fmt}\n"
                    f"👥 *Guests:* {guest_count} | Res: *{res_data.get('status', 'N/A')}* | "
                    f"Price: *{price_str}* | Platform: *{platform}*\n\n"
                    f"💬 *Message:* {guest_message}"
                )
            }
        },
        {
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": f"💡 *Suggested Reply:*\n{ai_reply}"
            }
        },
        {
            "type": "actions",
            "block_id": "action_buttons",
            "elements": [
                {
                    "type": "button",
                    "text": {"type": "plain_text", "text": "Send"},
                    "style": "primary",
                    "action_id": "send",  # matches slack_interactivity.py
                    "value": json.dumps(meta_payload)
                },
                {
                    "type": "button",
                    "text": {"type": "plain_text", "text": "Edit"},
                    "action_id": "edit",  # matches slack_interactivity.py
                    "value": json.dumps(meta_payload)
                },
                {
                    "type": "button",
                    "text": {"type": "plain_text", "text": "Send Guest Portal"},
                    "action_id": "send_guest_portal",  # matches slack_interactivity.py
                    "value": json.dumps({
                        "conv_id": conv_id,
                        "guest_portal_url": res_data.get("guestPortalUrl"),
                        "status": res_data.get("status", "N/A"),
                        "channel": SLACK_CHANNEL,
                    })
                }
            ]
        }
    ]

    post_to_slack(blocks)
    mark_processed(event_key)
    return {"status": "ok"}

@app.get("/ping")
def ping():
    return {"status": "ok"}

# ✅ include Slack interactivity router
app.include_router(slack_router, prefix="/slack")
