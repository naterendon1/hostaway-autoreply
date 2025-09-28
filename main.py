import os
import json
import logging
from typing import Optional, Dict, Any, List
from slack_sdk import WebClient
from datetime import datetime

from fastapi import FastAPI
from pydantic import BaseModel

from slack_interactivity import router as slack_router
from smart_intel import generate_reply
from utils import fetch_hostaway_listing, fetch_hostaway_reservation, fetch_hostaway_conversation
from db import already_processed, mark_processed, log_ai_exchange
from places import build_local_recs, should_fetch_local_recs  # ✅ NEW

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

    # Fetch context from Hostaway
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

    # ✅ Google-powered nearby places
    lat = listing_data.get("lat")
    lng = listing_data.get("lng")
    nearby_places = []
    if should_fetch_local_recs(guest_message):
        nearby_places = build_local_recs(lat, lng, guest_message)

    # Context for AI
    context = {
        "guest_name": guest_name,
        "check_in_date": check_in,
        "check_out_date": check_out,
        "listing_info": structured_listing_info,
        "reservation": res_data,
        "history": history,
        "nearby_places": nearby_places  # ✅ Pass to AI
    }

    # Generate AI reply
    ai_reply = generate_reply(guest_message, context)
    log_ai_exchange(
        conversation_id=str(conv_id),
        guest_message=guest_message,
        ai_suggestion=ai_reply,
        intent="general"
    )

    # Format Slack message
    checkin_fmt = datetime.strptime(check_in, "%Y-%m-%d").strftime("%m-%d-%Y") if check_in else "N/A"
    checkout_fmt = datetime.strptime(check_out, "%Y-%m-%d").strftime("%m-%d-%Y") if check_out else "N/A"

    price = res_data.get("grandTotalPrice") or res_data.get("totalPrice") or res_data.get("price") or "N/A"
    price_str = f"${float(price):,.2f}" if isinstance(price, (int, float, str)) and str(price).replace('.', '', 1).isdigit() else "$N/A"

    guest_count = res_data.get("numberOfGuests") or res_data.get("adults") or "?"
    # --- Platform Name Mapping ---
    channel_map = {
        2018: "Airbnb",
        2002: "Vrbo",
        2005: "Booking.com",
        2007: "Expedia",
        2009: "Vrbo (iCal)",
        2010: "Vrbo (iCal)",
        2000: "Direct",
        2013: "Booking Engine",
        2015: "Custom iCal",
        2016: "Tripadvisor (iCal)",
        2017: "WordPress",
        2019: "Marriott",
        2020: "Partner",
        2021: "GDS",
        2022: "Google",
    }
    channel_id = res_data.get("channelId")
    platform = channel_map.get(channel_id, "Unknown")

    # Use address instead of property name
    property_address = listing_data.get("address") or "Unknown Address"

    blocks = [
        {
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": (
                    f"*✉️ Message from {guest_name}*\n"
                    f"🏡 *Property:* {property_address}\n"
                    f"📅 *Dates:* {checkin_fmt} → {checkout_fmt}\n"
                    f"👥 *Guests:* {guest_count} | Res: *{res_data.get('status', 'N/A')}* | "
                    f"Price: *{price_str}* | Platform: *{platform}*\n\n"
                    f"{guest_message}"  # <-- no label like "Message:"
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
                    "action_id": "send_reply",
                    "value": json.dumps({
                        "conversation_id": conv_id,
                        "reply_text": ai_reply
                    })
                },
                {
                    "type": "button",
                    "text": {"type": "plain_text", "text": "Edit"},
                    "action_id": "open_edit_modal",
                    "value": json.dumps({
                        "guest_name": guest_name,
                        "guest_message": guest_message,
                        "draft_text": ai_reply
                    })
                },
                {
                    "type": "button",
                    "text": {"type": "plain_text", "text": "Send Guest Portal"},
                    "action_id": "send_guest_portal",
                    "value": json.dumps({
                        "conversation_id": conv_id
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
