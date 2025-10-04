# file: src/message_handler.py

import os
import json
import logging
from typing import Any, Dict, Optional, Tuple, List

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, PlainTextResponse

from src.api_client import fetch_hostaway_listing, fetch_hostaway_reservation, fetch_hostaway_conversation
from src.slack_client import post_slack_card
from src.ai_engine import generate_ai_reply
from db import already_processed, mark_processed, log_ai_exchange
from utils import (
    make_suggested_reply,
    extract_destination_from_message,
    get_distance_drive_time,
    route_message,
    should_fetch_local_recs,
    build_local_recs
)

router = APIRouter()
logger = logging.getLogger(__name__)


# ---------------- Helpers ----------------
def _safe_get(d: Dict[str, Any], *keys, default=None):
    cur = d
    for k in keys:
        if not isinstance(cur, dict):
            return default
        cur = cur.get(k)
        if cur is None:
            return default
    return cur


def _fmt_date(d: Optional[str]) -> str:
    if not d:
        return "N/A"
    from datetime import datetime
    for fmt in ("%Y-%m-%d", "%Y-%m-%dT%H:%M:%S%z", "%Y-%m-%dT%H:%M:%S", "%m/%d/%Y"):
        try:
            return datetime.strptime(d, fmt).strftime("%m-%d-%Y")
        except Exception:
            continue
    return d


async def _read_hostaway_payload(request: Request) -> Dict[str, Any]:
    """Robust parser for Hostaway webhooks."""
    try:
        body = await request.json()
        if isinstance(body, dict):
            return body
    except Exception:
        pass
    raw = await request.body()
    if raw:
        s = raw.decode("utf-8", "ignore").strip()
        if s.startswith("{") and s.endswith("}"):
            try:
                return json.loads(s)
            except Exception:
                pass
    return {}


# ---------------- Webhook Route ----------------
@router.post("/unified-webhook")
async def unified_webhook(request: Request):
    payload = await _read_hostaway_payload(request)
    if not payload:
        logger.warning("[webhook] empty/invalid payload accepted (ignored).")
        return {"status": "ignored"}

    event = payload.get("event")
    obj = payload.get("object")
    data = payload.get("data") or {}

    if event != "message.received" or obj != "conversationMessage":
        return {"status": "ignored"}

    event_key = f"{obj}:{event}:{data.get('id') or data.get('conversationId')}"
    if already_processed(event_key):
        return {"status": "duplicate"}

    guest_message = data.get("body") or payload.get("body") or ""
    if not guest_message:
        mark_processed(event_key)
        return {"status": "ignored"}

    conv_id = data.get("conversationId")
    reservation_id = data.get("reservationId")
    listing_id = data.get("listingMapId")

    # --- Reservation & Guest Info ---
    reservation = fetch_hostaway_reservation(reservation_id) or {}
    res_data = reservation.get("result", {}) or {}
    guest_name = res_data.get("guestFirstName") or res_data.get("guest", {}).get("firstName") or "Guest"
    check_in = res_data.get("arrivalDate")
    check_out = res_data.get("departureDate")

    # --- Conversation History ---
    conversation = fetch_hostaway_conversation(conv_id) or {}
    messages = conversation.get("result", {}).get("conversationMessages", [])[-10:]
    history = [
        {"role": ("guest" if m.get("isIncoming") else "host"), "text": m.get("body", "")}
        for m in messages if m.get("body")
    ]

    # --- Listing Info ---
    listing = fetch_hostaway_listing(listing_id) or {}
    listing_data = listing.get("result", {}) or {}

    nearby_places: List[Dict[str, Any]] = []
    lat = listing_data.get("lat")
    lng = listing_data.get("lng")
    if should_fetch_local_recs(guest_message) and lat and lng:
        try:
            nearby_places = build_local_recs(lat, lng, guest_message)
        except Exception as e:
            logger.warning(f"[recs] nearby recs failed: {e}")

    # --- AI Context ---
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

    ai_context = {
        "guest_name": guest_name,
        "check_in_date": check_in,
        "check_out_date": check_out,
        "listing_info": structured_listing_info,
        "reservation": res_data,
        "history": history,
        "nearby_places": nearby_places,
    }

    ai_reply = ""
    try:
        ai_reply = generate_ai_reply(guest_message, ai_context)
    except Exception as e:
        logger.warning(f"[AI] generate_ai_reply failed: {e}")
    if not ai_reply:
        ai_reply, _ = make_suggested_reply(guest_message, {
            "location": {"lat": lat, "lng": lng},
            "listing": listing_data,
            "reservation": {"arrivalDate": check_in, "departureDate": check_out},
        })

    try:
        log_ai_exchange(
            conversation_id=str(conv_id),
            guest_message=guest_message,
            ai_suggestion=ai_reply,
            intent=(route_message(guest_message) or {}).get("primary_intent", "general"),
        )
    except Exception:
        pass

    # --- Slack Meta ---
    checkin_fmt = _fmt_date(check_in)
    checkout_fmt = _fmt_date(check_out)

    price = res_data.get("grandTotalPrice") or res_data.get("totalPrice") or res_data.get("price") or "N/A"
    try:
        price_str = f"${float(str(price)):,.2f}"
    except Exception:
        price_str = "$N/A"

    channel_map = {
        2018: "Airbnb", 2002: "Vrbo", 2005: "Booking.com", 2007: "Expedia",
        2009: "Vrbo (iCal)", 2010: "Vrbo (iCal)", 2000: "Direct", 2013: "Booking Engine",
        2015: "Custom iCal", 2016: "Tripadvisor (iCal)", 2017: "WordPress", 2019: "Marriott",
        2020: "Partner", 2021: "GDS", 2022: "Google",
    }
    platform = channel_map.get(res_data.get("channelId"), "Unknown")
    guest_count = res_data.get("numberOfGuests") or res_data.get("adults") or "?"

    meta = {
        "conv_id": conv_id,
        "guest_name": guest_name,
        "property_address": listing_data.get("address") or "Unknown Address",
        "property_name": listing_data.get("name"),
        "check_in": checkin_fmt,
        "check_out": checkout_fmt,
        "guest_count": guest_count,
        "status": res_data.get("status", "N/A"),
        "price_str": price_str,
        "platform": platform,
        "listing_id": listing_id,
        "reservation_id": reservation_id,
        "type": "email",
        "guest_portal_url": res_data.get("guestPortalUrl") or res_data.get("portalUrl"),
    }

    post_slack_card(guest_message, ai_reply, meta)

    mark_processed(event_key)
    return {"status": "ok"}
