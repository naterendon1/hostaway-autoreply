# file: main.py

import os
import json
import logging
from typing import Optional, Dict, Any, List
from datetime import datetime

from fastapi import FastAPI
from pydantic import BaseModel
from slack_sdk import WebClient
from slack_sdk.errors import SlackApiError

# Local modules
from slack_interactivity import router as slack_router
from smart_intel import generate_reply
from utils import (
    fetch_hostaway_listing,
    fetch_hostaway_reservation,
    fetch_hostaway_conversation,
)
from places import build_local_recs, should_fetch_local_recs

# Optional DB helpers (no-op fallbacks if not present)
try:
    from db import already_processed, mark_processed, log_ai_exchange  # type: ignore
except Exception:  # pragma: no cover
    def already_processed(_k: str) -> bool: return False  # type: ignore
    def mark_processed(_k: str) -> None: pass            # type: ignore
    def log_ai_exchange(*_args, **_kwargs) -> None: pass # type: ignore

# ---------------- Config ----------------
logging.basicConfig(level=logging.INFO)

SLACK_BOT_TOKEN = os.getenv("SLACK_BOT_TOKEN")
ENV = os.getenv("ENV", "dev")

# Use SLACK_CHANNEL_ID if present; otherwise accept legacy SLACK_CHANNEL
DEFAULT_SLACK_CHANNEL_ID = (
    os.getenv("SLACK_CHANNEL_ID")
    or os.getenv("SLACK_CHANNEL")
)

logging.info(
    f"Boot: ENV={ENV!r} SLACK_CHANNEL_ID={DEFAULT_SLACK_CHANNEL_ID!r} "
    f"(token set={bool(SLACK_BOT_TOKEN)})"
)

slack_client: Optional[WebClient] = WebClient(token=SLACK_BOT_TOKEN) if SLACK_BOT_TOKEN else None

# ---------------- App ----------------
app = FastAPI(title="Hostaway Auto-Reply")

# ---------------- Models ----------------
class HostawayUnifiedWebhook(BaseModel):
    object: str
    event: str
    data: Dict[str, Any]

# ---------------- Helpers ----------------
def _pretty_date(dt_str: Optional[str]) -> Optional[str]:
    if not dt_str:
        return None
    try:
        dt = datetime.fromisoformat(dt_str.replace("Z", "+00:00"))
        return dt.strftime("%b %d, %Y")
    except Exception:
        return dt_str

def _fallback_channel_for_listing(_listing: Optional[Dict[str, Any]]) -> Optional[str]:
    """Route to a single default channel for now."""
    return DEFAULT_SLACK_CHANNEL_ID

def _post_to_slack(blocks: List[Dict[str, Any]], channel: Optional[str] = None, thread_ts: Optional[str] = None) -> None:
    if not slack_client:
        logging.warning("No SLACK_BOT_TOKEN configured; skipping Slack post.")
        return
    channel_id = channel or DEFAULT_SLACK_CHANNEL_ID
    logging.info(f"Posting to Slack channel_id={channel_id!r} (override={channel!r})")
    if not channel_id:
        logging.warning("No Slack channel available; set SLACK_CHANNEL_ID (or SLACK_CHANNEL).")
        return
    try:
        slack_client.chat_postMessage(
            channel=channel_id,
            text="New guest message",
            blocks=blocks,
            thread_ts=thread_ts,
        )
    except SlackApiError as e:
        logging.error(f"Slack post error: {e}")

def _build_action_row(meta: Dict[str, Any], ai_reply: str) -> Dict[str, Any]:
    """
    Matches slack_interactivity.py handlers:
      send              -> conv_id + reply/ai_suggestion
      edit              -> draft
      send_guest_portal -> conv_id (+ optional guest_portal_url/status)
    """
    base_meta = {
        "conv_id": meta.get("conv_id"),
        "guest_message": meta.get("guest_message", ""),
        "guest_name": meta.get("guest_name", "Guest"),
        "listing_id": meta.get("listing_id"),
        "guest_id": meta.get("guest_id"),
        "status": meta.get("status"),
        "type": meta.get("type", "email"),
        "check_in": meta.get("check_in"),
        "check_out": meta.get("check_out"),
        "location": meta.get("location"),
        "property_address": meta.get("property_address"),
        "sent_label": meta.get("sent_label", "message sent"),
    }

    send_value = dict(base_meta);  send_value["ai_suggestion"] = ai_reply
    edit_value = dict(base_meta);  edit_value["draft"] = ai_reply
    portal_value = dict(base_meta)
    if meta.get("guest_portal_url"):
        portal_value["guest_portal_url"] = meta["guest_portal_url"]

    return {
        "type": "actions",
        "block_id": "action_buttons",
        "elements": [
            {
                "type": "button",
                "action_id": "send",  # ✅ correct
                "text": {"type": "plain_text", "text": "Send", "emoji": True},
                "style": "primary",
                "value": json.dumps(send_value, ensure_ascii=False),
            },
            {
                "type": "button",
                "action_id": "edit",  # ✅ correct
                "text": {"type": "plain_text", "text": "Edit", "emoji": True},
                "value": json.dumps(edit_value, ensure_ascii=False),
            },
            {
                "type": "button",
                "action_id": "send_guest_portal",
                "text": {"type": "plain_text", "text": "Send Guest Portal", "emoji": True},
                "value": json.dumps(portal_value, ensure_ascii=False),
            },
        ],
    }

def _build_message_blocks(guest_message: str, ai_reply: str, meta: Dict[str, Any]) -> List[Dict[str, Any]]:
    header_text = meta.get("channel_pretty") or meta.get("property_name") or "New guest message"
    guest_name = meta.get("guest_name", "Guest")
    check_in = _pretty_date(meta.get("check_in"))
    check_out = _pretty_date(meta.get("check_out"))

    context_bits = []
    if guest_name: context_bits.append(f"*Guest:* {guest_name}")
    if check_in or check_out: context_bits.append(f"*Stay:* {check_in or '?'} → {check_out or '?'}")
    if meta.get("guest_count"): context_bits.append(f"*Guests:* {meta['guest_count']}")
    if meta.get("status"): context_bits.append(f"*Status:* {meta['status']}")
    if meta.get("property_name"): context_bits.append(f"*Listing:* {meta['property_name']}")

    blocks: List[Dict[str, Any]] = [
        {"type": "header", "text": {"type": "plain_text", "text": header_text[:150], "emoji": True}},
        {
            "type": "section",
            "text": {"type": "mrkdwn", "text": f"*:email: Message from {guest_name}*\n> {guest_message[:2500]}"},
        },
        {"type": "divider"},
        {
            "type": "section",
            "text": {"type": "mrkdwn", "text": f":bulb: *Suggested Reply:*\n{ai_reply[:3500]}"},
        },
    ]
    if context_bits:
        blocks.insert(1, {"type": "context", "elements": [{"type": "mrkdwn", "text": " • ".join(context_bits)[:300]}]})
    blocks.append(_build_action_row(meta, ai_reply))
    return blocks

def _get_listing_lat_lng(listing: Dict[str, Any]) -> (Optional[float], Optional[float]):
    # Hostaway typically uses latitude/longitude on the listing or inside 'address'
    lat = listing.get("latitude") or (listing.get("address") or {}).get("lat")
    lng = listing.get("longitude") or (listing.get("address") or {}).get("lng")
    try:
        return (float(lat), float(lng)) if lat is not None and lng is not None else (None, None)
    except Exception:
        return (None, None)

def _collect_context_from_hostaway(conversation_id: int) -> Dict[str, Any]:
    """
    Pull conversation, reservation, and listing context to enrich the Slack card and AI reply.
    """
    conversation = fetch_hostaway_conversation(conversation_id) or {}
    reservation_id = conversation.get("reservationId") or conversation.get("reservation_id")
    reservation = fetch_hostaway_reservation(reservation_id) if reservation_id else {}
    listing_id = reservation.get("listingId") or conversation.get("listingId") or conversation.get("listing_id")
    listing = fetch_hostaway_listing(listing_id) if listing_id else {}

    guest_name = (
        (conversation.get("guest") or {}).get("fullName")
        or (conversation.get("guest") or {}).get("firstName")
        or (reservation.get("guest") or {}).get("fullName")
        or "Guest"
    )

    # Compact message history for AI context
    history = []
    for msg in (conversation.get("messages") or []):
        role = "guest" if (msg.get("senderType") == "guest") else "host"
        history.append({"role": role, "text": msg.get("body", "")})

    # Lat/Lng (if available) for local recs / distance
    lat, lng = _get_listing_lat_lng(listing)

    meta: Dict[str, Any] = {
        "conv_id": conversation_id,
        "listing_id": listing_id,
        "guest_id": (reservation.get("guest") or {}).get("id") or conversation.get("guestId"),
        "guest_name": guest_name,
        "status": (reservation.get("status") or reservation.get("reservationStatus") or conversation.get("status")),
        "type": "email",
        "check_in": reservation.get("checkInDate") or reservation.get("checkIn"),
        "check_out": reservation.get("checkOutDate") or reservation.get("checkOut"),
        "guest_count": reservation.get("numberOfGuests") or reservation.get("guestCount"),
        "channel_pretty": (conversation.get("channelName") or listing.get("name") or "New guest message"),
        "property_name": listing.get("name"),
        "property_address": (listing.get("address") or {}).get("address1"),
        "latitude": lat,
        "longitude": lng,
    }

    # If any portal URL exists, pass it through for the "send_guest_portal" handler
    for k in ("guestPortalUrl", "guest_portal_url", "portalUrl"):
        if reservation.get(k):
            meta["guest_portal_url"] = reservation.get(k)
            break

    # Local recs bundle only when the guest asks about local things AND we have coordinates
    nearby_bundle: List[Dict[str, Any]] = []
    # (We’ll decide using the guest_message later inside the webhook.)

    ai_ctx: Dict[str, Any] = {
        "guest_name": guest_name,
        "listing_info": listing or {},
        "reservation": reservation or {},
        "history": history[-8:],  # keep prompt small
        "nearby_places": nearby_bundle,  # may be replaced below once we see the message
        "property_address": meta["property_address"],
        "latitude": lat,
        "longitude": lng,
    }
    return meta | {"_ai_ctx": ai_ctx}

# ---------------- Webhooks ----------------
@app.post("/unified-webhook")
async def unified_webhook(payload: HostawayUnifiedWebhook):
    """
    Expect Hostaway-style events:
      object: "conversationMessage"
      event:  "message.received"
    """
    if payload.event != "message.received" or payload.object != "conversationMessage":
        return {"status": "ignored"}

    data = payload.data or {}
    conv_id = data.get("conversationId") or data.get("conversation_id")
    message_id = data.get("id") or data.get("messageId")
    guest_message = data.get("body", "") or data.get("message", "")

    # Dedupe
    event_key = f"{payload.object}:{payload.event}:{message_id or conv_id}"
    if already_processed(event_key):
        return {"status": "duplicate"}

    if not conv_id or not guest_message:
        mark_processed(event_key)
        return {"status": "no-content"}

    # Collect context
    meta = _collect_context_from_hostaway(int(conv_id))
    meta["guest_message"] = guest_message

    # Inject local recs bundle if the message is about local stuff and we have coords
    lat = meta.get("latitude")
    lng = meta.get("longitude")
    ai_ctx = meta.get("_ai_ctx", {})
    if should_fetch_local_recs(guest_message) and lat is not None and lng is not None:
        ai_ctx["nearby_places"] = build_local_recs(lat, lng, guest_message)

    # Generate AI suggestion (sync)
    ai_reply = generate_reply(guest_message, ai_ctx) if guest_message else "Thanks for your message! I’ll get back to you shortly."

    # Optional log
    try:
        log_ai_exchange(
            conversation_id=str(conv_id),
            question=guest_message,
            ai_answer=ai_reply,
            listing_id=str(meta.get("listing_id") or ""),
            guest_id=str(meta.get("guest_id") or ""),
        )
    except Exception:
        pass

    # Build & post Slack card
    blocks = _build_message_blocks(guest_message, ai_reply, meta)
    _post_to_slack(blocks, channel=_fallback_channel_for_listing(None))

    mark_processed(event_key)
    return {"status": "ok"}

@app.get("/ping")
def ping():
    return {"status": "ok", "env": ENV}

# Simple runtime check for what the process actually sees
@app.get("/debug/slack")
def debug_slack():
    return {"SLACK_CHANNEL_ID": DEFAULT_SLACK_CHANNEL_ID, "env": ENV}

# Mount Slack interactivity routes (/slack/actions, etc.)
app.include_router(slack_router, prefix="/slack")
