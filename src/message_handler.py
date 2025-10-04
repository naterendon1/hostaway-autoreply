# file: src/message_handler.py
import logging
from fastapi import APIRouter, Request
from typing import Dict, Any, List

from src.api_client import (
    fetch_hostaway_reservation,
    fetch_hostaway_listing,
    fetch_hostaway_conversation,
)
from src.slack_client import post_message_to_slack
from src.ai_engine import generate_reply, analyze_conversation_thread
from src.config import config

message_handler_bp = APIRouter()

# -------------------- Unified Webhook Endpoint --------------------
@message_handler_bp.post("/unified-webhook")
async def unified_webhook(request: Request):
    """
    Handles Hostaway unified webhooks and routes guest messages into Slack.
    Includes AI mood & summary analysis for header.
    """
    try:
        payload = await request.json()
    except Exception:
        payload = {}

    event = payload.get("event")
    obj = payload.get("object")
    data = payload.get("data") or {}

    # Filter only relevant messages
    if event != "message.received" or obj != "conversationMessage":
        return {"status": "ignored"}

    guest_message = data.get("body", "").strip()
    if not guest_message:
        return {"status": "ignored"}

    conversation_id = data.get("conversationId")
    reservation_id = data.get("reservationId")
    listing_id = data.get("listingMapId")

    # -------------------- Fetch Hostaway Context --------------------
    reservation = fetch_hostaway_reservation(reservation_id)
    res_data = (reservation or {}).get("result", {}) or {}

    guest_name = (
        res_data.get("guestFirstName")
        or res_data.get("guest", {}).get("firstName")
        or "Guest"
    )
    check_in = res_data.get("arrivalDate")
    check_out = res_data.get("departureDate")
    guest_count = res_data.get("numberOfGuests") or res_data.get("adults") or "?"
    platform = res_data.get("channelId", "Unknown")

    listing = fetch_hostaway_listing(listing_id)
    listing_data = (listing or {}).get("result", {}) or {}

    # -------------------- Fetch Conversation History --------------------
    conversation = fetch_hostaway_conversation(conversation_id)
    messages = (
        (conversation or {}).get("result", {}).get("conversationMessages", [])
        or []
    )

    # Prepare conversation thread for AI analysis
    thread = [
        {"role": "guest" if m.get("isIncoming") else "host", "text": m.get("body")}
        for m in messages
        if m.get("body")
    ]

    # Limit to last 10–15 messages for summary
    thread = thread[-15:]

    # -------------------- AI Mood & Summary --------------------
    mood, summary = None, None
    try:
        mood, summary = analyze_conversation_thread(thread)
    except Exception as e:
        logging.warning(f"[AI] analyze_conversation_thread failed: {e}")

    # -------------------- AI Suggested Reply --------------------
    ai_context = {
        "guest_name": guest_name,
        "check_in_date": check_in,
        "check_out_date": check_out,
        "listing_info": listing_data,
        "reservation": res_data,
        "conversation_history": thread,
    }

    ai_suggestion = generate_reply(guest_message, ai_context)

    # -------------------- Price, Photo & Meta --------------------
    price = res_data.get("grandTotalPrice") or res_data.get("totalPrice") or "N/A"
    try:
        price_float = float(str(price))
        price_str = f"${price_float:,.2f}"
    except Exception:
        price_str = "$N/A"

    guest_photo = None
    guest_obj = res_data.get("guest", {})
    if isinstance(guest_obj, dict):
        guest_photo = guest_obj.get("pictureUrl") or guest_obj.get("photo")

    # Channel name mapping (Hostaway channel IDs)
    channel_map = {
        2018: "Airbnb",
        2002: "Vrbo",
        2005: "Booking.com",
        2007: "Expedia",
        2000: "Direct",
        2022: "Google",
    }
    platform_name = channel_map.get(res_data.get("channelId"), "Unknown")

    meta = {
        "conv_id": conversation_id,
        "guest_name": guest_name,
        "property_name": listing_data.get("name"),
        "property_address": listing_data.get("address"),
        "check_in": check_in,
        "check_out": check_out,
        "guest_count": guest_count,
        "status": res_data.get("status", "N/A"),
        "price_str": price_str,
        "platform": platform_name,
        "listing_id": listing_id,
        "reservation_id": reservation_id,
        "guest_portal_url": res_data.get("guestPortalUrl")
        or res_data.get("portalUrl"),
        "guest_photo": guest_photo,
    }

    # -------------------- Post to Slack --------------------
    slack_response = post_message_to_slack(
        guest_message=guest_message,
        ai_suggestion=ai_suggestion,
        meta=meta,
        mood=mood,
        summary=summary,
    )

    if slack_response:
        logging.info(f"[Slack] Message posted successfully for {guest_name}")
        return {"status": "ok"}
    else:
        logging.warning("[Slack] Message failed to post.")
        return {"status": "failed"}
