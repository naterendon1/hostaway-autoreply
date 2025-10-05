# file: src/message_handler.py
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
    """Handle incoming Hostaway conversation webhooks and forward to Slack with AI summary."""
    try:
        payload = await request.json()
    except Exception:
        payload = {}

    event = payload.get("event")
    obj = payload.get("object")
    data = payload.get("data") or {}

    # Ignore non-message events
    if event != "message.received" or obj != "conversationMessage":
        return {"status": "ignored"}

    guest_message = data.get("body", "").strip()
    if not guest_message:
        return {"status": "ignored"}

    # Extract key identifiers
    conversation_id = data.get("conversationId")
    reservation_id = data.get("reservationId")
    listing_id = data.get("listingMapId")

    # Fetch related Hostaway objects
    reservation = fetch_hostaway_reservation(reservation_id)
    res_data = (reservation or {}).get("result", {}) or {}

    listing = fetch_hostaway_listing(listing_id)
    listing_data = (listing or {}).get("result", {}) or {}

    conversation = fetch_hostaway_conversation(conversation_id)
    messages = (conversation or {}).get("result", {}).get("conversationMessages", []) or []

    # Build message thread for AI context
    thread = [
        {"role": "guest" if m.get("isIncoming") else "host", "text": m.get("body")}
        for m in messages if m.get("body")
    ]

    # Analyze conversation tone + summary
    mood, summary = await analyze_conversation_thread(thread)

    # Prepare AI context for suggestion
    guest_name = res_data.get("guestFirstName") or "Guest"
    ai_context = {
        "guest_name": guest_name,
        "check_in_date": res_data.get("arrivalDate"),
        "check_out_date": res_data.get("departureDate"),
        "listing_info": listing_data,
        "conversation_history": thread,
    }

    # Generate AI reply suggestion
    ai_suggestion = generate_reply(guest_message, ai_context)

    # ---------------------------------------------------------------------
    # ✅ Normalize Hostaway data for Slack (improved header support)
    # ---------------------------------------------------------------------
    meta = {
        "conv_id": conversation_id,
        "guest_name": f"{res_data.get('guestFirstName', '')} {res_data.get('guestLastName', '')}".strip() or "Guest",
        "guest_photo": res_data.get("guestAvatar") or listing_data.get("thumbnailUrl"),
        "property_name": listing_data.get("name"),
        "property_address": listing_data.get("address", {}).get("full", ""),
        "check_in": res_data.get("arrivalDate"),
        "check_out": res_data.get("departureDate"),
        "guest_count": res_data.get("numberOfGuests"),
        "platform": res_data.get("channelId", "Hostaway"),
        "status": res_data.get("status", "N/A"),
        "guest_message": guest_message,
        "mood": mood,
        "summary": summary,
        "guest_portal_url": res_data.get("guestPortalUrl"),
    }

    # ---------------------------------------------------------------------
    # ✅ Post full AI card to Slack (includes photo, mood, and summary)
    # ---------------------------------------------------------------------
    await post_message_to_slack(
        guest_message=meta["guest_message"],
        ai_suggestion=ai_suggestion,
        meta=meta,
        mood=mood,
        summary=summary,
    )

    return {"status": "ok"}
