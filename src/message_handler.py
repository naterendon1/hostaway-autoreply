import os
import json
import logging
from typing import Dict, Any, List
from datetime import datetime

from fastapi import APIRouter, Request
from slack_sdk import WebClient

# Local imports
from src.api_client import (
    fetch_hostaway_reservation,
    fetch_hostaway_listing,
    fetch_hostaway_conversation,
)
from src.ai_assistant import generate_reply, analyze_conversation_thread
from src.db import already_processed, mark_processed, log_ai_exchange
from src.places import should_fetch_local_recs, build_local_recs

# --- Setup ---
message_handler_bp = APIRouter()
SLACK_BOT_TOKEN = os.getenv("SLACK_BOT_TOKEN", "")
SLACK_CHANNEL = os.getenv("SLACK_CHANNEL", "")
client = WebClient(token=SLACK_BOT_TOKEN) if SLACK_BOT_TOKEN else None

logging.basicConfig(level=logging.INFO)


# -------------------------------------------------------------------
# 🔹 Unified Webhook Endpoint
# -------------------------------------------------------------------
@message_handler_bp.post("/unified-webhook")
async def unified_webhook(request: Request):
    payload = await request.json()
    logging.info(f"🧩 DEBUG WEBHOOK PAYLOAD:\n{json.dumps(payload, indent=2)}")

    if payload.get("object") != "conversationMessage" or payload.get("event") != "message.received":
        return {"status": "ignored"}

    data = payload.get("data", {})
    event_key = f"{data.get('id')}:{data.get('conversationId')}"
    if already_processed(event_key):
        return {"status": "duplicate"}

    guest_message = data.get("body", "")
    if not guest_message:
        mark_processed(event_key)
        return {"status": "ignored"}

    conv_id = data.get("conversationId")
    reservation_id = data.get("reservationId")
    listing_id = data.get("listingMapId")

    # -------------------------------------------------------------------
    # Fetch reservation + listing + conversation context
    # -------------------------------------------------------------------
    reservation = fetch_hostaway_reservation(reservation_id) or {}
    res_data = reservation.get("result", {})
    listing = fetch_hostaway_listing(listing_id) or {}
    listing_data = listing.get("result", {})
    conversation = fetch_hostaway_conversation(conv_id) or {}
    messages = conversation.get("result", {}).get("conversationMessages", [])

    # -------------------------------------------------------------------
    # Extract details
    # -------------------------------------------------------------------
    guest_name = res_data.get("guestFirstName") or res_data.get("guestName") or "Guest"
    check_in = res_data.get("arrivalDate")
    check_out = res_data.get("departureDate")
    guest_count = res_data.get("numberOfGuests") or res_data.get("adults") or "?"
    status = res_data.get("status", "unknown").capitalize()
    platform = res_data.get("channelName") or "Hostaway"
    property_name = listing_data.get("name") or "Unnamed Property"
    property_address = listing_data.get("address") or "Unknown Address"
    lat, lng = listing_data.get("lat"), listing_data.get("lng")

    # -------------------------------------------------------------------
    # AI: Analyze mood + summary using Assistants API
    # -------------------------------------------------------------------
    try:
        mood, summary = analyze_conversation_thread(str(conv_id), messages)
    except Exception as e:
        logging.error(f"[AI] analyze_conversation_thread failed: {e}")
        mood, summary = "Neutral", "Summary unavailable."

    # -------------------------------------------------------------------
    # AI: Generate reply using Assistants API (maintains conversation memory)
    # -------------------------------------------------------------------
    context = {
        "guest_name": guest_name,
        "check_in": check_in,
        "check_out": check_out,
        "guest_count": guest_count,
        "property_name": property_name,
        "property_address": property_address,
        "status": status,
        "platform": platform,
    }
    ai_reply = generate_smart_reply(str(conv_id), guest_message, enhanced_context)

    # Log exchange
    log_ai_exchange(
        conversation_id=str(conv_id),
        guest_message=guest_message,
        ai_suggestion=ai_reply,
        intent="general",
    )

    # -------------------------------------------------------------------
    # Optional: Nearby Recommendations
    # -------------------------------------------------------------------
    nearby_places = []
    if should_fetch_local_recs(guest_message):
        try:
            nearby_places = build_local_recs(lat, lng, guest_message)
        except Exception as e:
            logging.warning(f"[places] Failed to build local recs: {e}")

    # -------------------------------------------------------------------
    # Format Slack Message (emoji-rich)
    # -------------------------------------------------------------------
    checkin_fmt = (
        datetime.strptime(check_in, "%Y-%m-%d").strftime("%b %d")
        if check_in else "N/A"
    )
    checkout_fmt = (
        datetime.strptime(check_out, "%Y-%m-%d").strftime("%b %d")
        if check_out else "N/A"
    )

    header_text = (
        f"*✉️ Message from {guest_name}*\n"
        f"🏡 *Property:* {property_name} — {property_address}\n"
        f"📅 *Dates:* {checkin_fmt} → {checkout_fmt}\n"
        f"👥 *Guests:* {guest_count} | Status: *{status}* | Platform: *{platform}*\n"
        f"🧠 *Mood:* {mood}\n"
        f"📝 *Summary:* {summary}\n\n"
        f"💬 *Guest Message:*\n{guest_message}"
    )

    suggestion_text = f"💡 *Suggested Reply:*\n{ai_reply}"

    # Add local recs (optional)
    if nearby_places:
        recs_lines = []
        for p in nearby_places[:3]:
            line = f"• {p['name']} ({p['type']})"
            # Add travel time if available
            if p.get('travel_time'):
                line += f" - {p['travel_time']} away"
            elif p.get('distance'):
                line += f" - {p['distance']} away"
            recs_lines.append(line)
        recs_text = "\n".join(recs_lines)
        suggestion_text += f"\n\n📍 *Nearby Recommendations:*\n{recs_text}"

    blocks = [
        {"type": "section", "text": {"type": "mrkdwn", "text": header_text}},
        {"type": "divider"},
        {"type": "section", "text": {"type": "mrkdwn", "text": suggestion_text}},
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
                        "reply_text": ai_reply,
                        "guest_message": guest_message,
                    }),
                },
                {
                    "type": "button",
                    "text": {"type": "plain_text", "text": "Edit"},
                    "action_id": "open_edit_modal",
                    "value": json.dumps({
                        "guest_name": guest_name,
                        "guest_message": guest_message,
                        "draft_text": ai_reply,
                    }),
                },
                {
                    "type": "button",
                    "text": {"type": "plain_text", "text": "Send Guest Portal"},
                    "action_id": "send_guest_portal",
                    "value": json.dumps({
                        "conversation_id": conv_id,
                        "guest_portal_url": res_data.get("guestPortalUrl"),
                        "status": status,
                    }),
                },
            ],
        },
    ]

    # -------------------------------------------------------------------
    # Post to Slack
    # -------------------------------------------------------------------
    if client and SLACK_CHANNEL:
        try:
            client.chat_postMessage(channel=SLACK_CHANNEL, blocks=blocks, text="New guest message")
            logging.info(f"✅ Posted conversation {conv_id} to Slack")
        except Exception as e:
            logging.error(f"[Slack] Failed to post: {e}")
    else:
        logging.warning("⚠️ Slack client or channel not configured.")

    mark_processed(event_key)
    return {"status": "ok"}
