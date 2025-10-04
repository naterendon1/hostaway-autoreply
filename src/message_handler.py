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

    reservation = fetch_hostaway_reservation(reservation_id)
    res_data = (reservation or {}).get("result", {}) or {}

    guest_name = res_data.get("guestFirstName") or "Guest"
    listing = fetch_hostaway_listing(listing_id)
    listing_data = (listing or {}).get("result", {}) or {}

    conversation = fetch_hostaway_conversation(conversation_id)
    messages = (conversation or {}).get("result", {}).get("conversationMessages", []) or []
    thread = [{"role": "guest" if m.get("isIncoming") else "host", "text": m.get("body")} for m in messages if m.get("body")]

    mood, summary = await analyze_conversation_thread(thread)

    ai_context = {
        "guest_name": guest_name,
        "check_in_date": res_data.get("arrivalDate"),
        "check_out_date": res_data.get("departureDate"),
        "listing_info": listing_data,
        "conversation_history": thread,
    }

    ai_suggestion = generate_reply(guest_message, ai_context)

    meta = {
        "conv_id": conversation_id,
        "guest_name": guest_name,
        "property_name": listing_data.get("name"),
        "check_in": res_data.get("arrivalDate"),
        "check_out": res_data.get("departureDate"),
        "guest_count": res_data.get("numberOfGuests"),
        "platform": res_data.get("channelId", "Unknown"),
        "guest_message": guest_message,
        "mood": mood,
        "summary": summary,
    }

    post_message_to_slack(meta, {"suggested_reply": ai_suggestion, "summary": summary, "mood": mood})
    return {"status": "ok"}
