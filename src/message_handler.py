from fastapi import APIRouter, Request
import logging
from src.api_client import (
    fetch_hostaway_reservation,
    fetch_hostaway_listing,
    fetch_hostaway_conversation,
)
from src.slack_client import post_message_to_slack
from src.ai_engine import generate_reply, analyze_conversation_thread

message_handler_bp = APIRouter()


@message_handler_bp.post("/unified-webhook")
async def unified_webhook(request: Request):
    try:
        payload = await request.json()
    except Exception:
        payload = {}

    event = payload.get("event")
    obj = payload.get("object")
    data = payload.get("data") or {}

    if event != "message.received" or obj != "conversationMessage":
        return {"status": "ignored"}

    guest_message = data.get("body", "").strip()
    if not guest_message:
        return {"status": "ignored"}

    conversation_id = data.get("conversationId")
    reservation_id = data.get("reservationId")
    listing_id = data.get("listingMapId")

    # --- Always fetch conversation details ---
    conversation = fetch_hostaway_conversation(conversation_id) or {}
    convo_data = (
        conversation.get("result")
        or conversation.get("data")
        or conversation
        or {}
    )

    # If reservation or listing missing, get them from conversation
    if not reservation_id:
        reservation_id = convo_data.get("reservationId")
    if not listing_id:
        listing_id = convo_data.get("listingId")

    # --- Fetch reservation + listing data ---
    reservation = fetch_hostaway_reservation(reservation_id) or {}
    listing = fetch_hostaway_listing(listing_id) or {}

    res_data = (
        reservation.get("result")
        or reservation.get("data")
        or reservation
        or {}
    )
    listing_data = (
        listing.get("result")
        or listing.get("data")
        or listing
        or {}
    )

    messages = convo_data.get("conversationMessages", []) or []
    thread = [
        {"role": "guest" if m.get("isIncoming") else "host", "text": m.get("body")}
        for m in messages
        if m.get("body")
    ]

    # --- AI Analysis ---
    mood, summary = await analyze_conversation_thread(thread)

    ai_context = {
        "guest_name": res_data.get("guestFirstName", "Guest"),
        "check_in_date": res_data.get("arrivalDate"),
        "check_out_date": res_data.get("departureDate"),
        "listing_info": listing_data,
        "conversation_history": thread,
    }

    ai_suggestion = generate_reply(guest_message, ai_context)

    # --- Build metadata for Slack header ---
    guest_photo = res_data.get("guest", {}).get("pictureLarge") or res_data.get("guestPicture")
    meta = {
        "conv_id": conversation_id,
        "guest_name": res_data.get("guestFirstName", "Guest"),
        "property_name": listing_data.get("name") or listing_data.get("address"),
        "property_address": listing_data.get("address"),
        "check_in": res_data.get("arrivalDate"),
        "check_out": res_data.get("departureDate"),
        "guest_count": res_data.get("numberOfGuests"),
        "status": res_data.get("status", "N/A"),
        "platform": res_data.get("channelId", "Hostaway"),
        "guest_message": guest_message,
        "guest_photo": guest_photo,
        "guest_portal_url": res_data.get("guestPortalUrl"),
        "mood": mood,
        "summary": summary,
    }

    # --- Send to Slack ---
    post_message_to_slack(guest_message, ai_suggestion, meta, mood, summary)
    return {"status": "ok"}
