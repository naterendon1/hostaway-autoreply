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

# Optional dedupe/analytics utilities (keep if available in your repo)
try:
    from db import already_processed, mark_processed, log_ai_exchange  # type: ignore
except Exception:  # pragma: no cover
    def already_processed(_k: str) -> bool:  # fallback no-op
        return False
    def mark_processed(_k: str) -> None:
        pass
    def log_ai_exchange(*_args, **_kwargs) -> None:
        pass

# ---------- Config ----------
SLACK_BOT_TOKEN = os.getenv("SLACK_BOT_TOKEN")
DEFAULT_SLACK_CHANNEL = os.getenv("SLACK_CHANNEL_ID")  # fallback channel if none per-listing
ENV = os.getenv("ENV", "dev")

slack_client: Optional[WebClient] = WebClient(token=SLACK_BOT_TOKEN) if SLACK_BOT_TOKEN else None

# ---------- App ----------
app = FastAPI(title="Hostaway Auto-Reply")

# ---------- Models ----------
class HostawayUnifiedWebhook(BaseModel):
    # Minimal schema: adjust if your webhook adds more fields.
    object: str
    event: str
    data: Dict[str, Any]


# ---------- Helpers ----------
def _pretty_date(dt_str: Optional[str]) -> Optional[str]:
    if not dt_str:
        return None
    try:
        # Hostaway often uses ISO-like timestamps
        dt = datetime.fromisoformat(dt_str.replace("Z", "+00:00"))
        return dt.strftime("%b %d, %Y")
    except Exception:
        return dt_str


def _fallback_channel_for_listing(listing: Optional[Dict[str, Any]]) -> Optional[str]:
    """
    If you route Slack by property, add your mapping here.
    For now, we just honor DEFAULT_SLACK_CHANNEL.
    """
    return DEFAULT_SLACK_CHANNEL


def _post_to_slack(blocks: List[Dict[str, Any]], channel: Optional[str] = None, thread_ts: Optional[str] = None) -> None:
    if not slack_client:
        logging.warning("No SLACK_BOT_TOKEN configured; skipping Slack post.")
        return
    channel_id = channel or DEFAULT_SLACK_CHANNEL
    if not channel_id:
        logging.warning("No Slack channel available; set SLACK_CHANNEL_ID.")
        return
    try:
        slack_client.chat_postMessage(channel=channel_id, text="New guest message", blocks=blocks, thread_ts=thread_ts)
    except SlackApiError as e:
        logging.error(f"Slack post error: {e}")


def _build_action_row(meta: Dict[str, Any], ai_reply: str) -> Dict[str, Any]:
    """
    Build the Block Kit actions row with correct action_ids and expected metadata for the handlers in slack_interactivity.py.
    """
    base_meta = {
        # Required/used across handlers
        "conv_id": meta.get("conv_id"),
        "guest_message": meta.get("guest_message", ""),
        "listing_id": meta.get("listing_id"),
        "guest_id": meta.get("guest_id"),
        "guest_name": meta.get("guest_name", "Guest"),
        "status": meta.get("status"),         # e.g., booking status for portal check
        "type": meta.get("type", "email"),    # e.g., "email" or "sms" if you use it downstream
        "check_in": meta.get("check_in"),
        "check_out": meta.get("check_out"),
        "location": meta.get("location"),
        "property_address": meta.get("property_address"),
        # For post-send header label
        "sent_label": meta.get("sent_label", "message sent"),
    }

    # --- Send button (primary) ---
    # The "send" handler will (re)generate a reply based on guest_message + context.
    # We still include ai_suggestion as a fallback/reference.
    send_value = dict(base_meta)
    send_value["ai_suggestion"] = ai_reply

    # --- Edit button ---
    # The "edit" handler expects the draft under "draft" (or will fallback to ai_suggestion).
    edit_value = dict(base_meta)
    edit_value["draft"] = ai_reply

    # --- Guest Portal button ---
    # Handler checks status and guest_portal_url/guestPortalUrl. Provide when available.
    portal_value = dict(base_meta)
    if meta.get("guest_portal_url"):
        portal_value["guest_portal_url"] = meta["guest_portal_url"]

    return {
        "type": "actions",
        "block_id": "action_buttons",
        "elements": [
            {
                "type": "button",
                "action_id": "send",
                "text": {"type": "plain_text", "text": "Send", "emoji": True},
                "style": "primary",
                "value": json.dumps(send_value, ensure_ascii=False),
            },
            {
                "type": "button",
                "action_id": "edit",
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


def _build_message_blocks(
    guest_message: str,
    ai_reply: str,
    meta: Dict[str, Any],
) -> List[Dict[str, Any]]:
    """
    Build a simple, readable card with the guest message and AI suggestion,
    followed by the actions row wired to the right handler IDs.
    """
    header_text = meta.get("channel_pretty") or meta.get("property_name") or "New guest message"
    guest_name = meta.get("guest_name", "Guest")
    check_in = _pretty_date(meta.get("check_in"))
    check_out = _pretty_date(meta.get("check_out"))

    context_lines = []
    if guest_name:
        context_lines.append(f"*Guest:* {guest_name}")
    if check_in or check_out:
        context_lines.append(f"*Stay:* {check_in or '?'} → {check_out or '?'}")
    if meta.get("guest_count"):
        context_lines.append(f"*Guests:* {meta['guest_count']}")
    if meta.get("status"):
        context_lines.append(f"*Status:* {meta['status']}")
    if meta.get("property_name"):
        context_lines.append(f"*Listing:* {meta['property_name']}")

    blocks: List[Dict[str, Any]] = [
        {"type": "header", "text": {"type": "plain_text", "text": header_text[:150], "emoji": True}},
        {
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": f"*Guest message:*\n>{guest_message[:2500]}",
            },
        },
        {"type": "divider"},
        {
            "type": "section",
            "text": {"type": "mrkdwn", "text": f"*Suggested reply:*\n{ai_reply[:3500]}"},
        },
    ]

    if context_lines:
        blocks.insert(
            1,
            {"type": "context", "elements": [{"type": "mrkdwn", "text": " • ".join(context_lines)[:300]}]},
        )

    blocks.append(_build_action_row(meta, ai_reply))
    return blocks


def _collect_context_from_hostaway(conversation_id: int) -> Dict[str, Any]:
    """
    Pulls conversation, reservation, and listing data to build meta and a better AI reply context.
    """
    conversation = fetch_hostaway_conversation(conversation_id) or {}
    reservation_id = conversation.get("reservationId") or conversation.get("reservation_id")
    reservation = fetch_hostaway_reservation(reservation_id) if reservation_id else {}
    listing_id = reservation.get("listingId") or conversation.get("listingId") or conversation.get("listing_id")
    listing = fetch_hostaway_listing(listing_id) if listing_id else {}

    # Try to locate guest name/message and history
    guest_name = (
        (conversation.get("guest") or {}).get("fullName")
        or (conversation.get("guest") or {}).get("firstName")
        or (reservation.get("guest") or {}).get("fullName")
        or "Guest"
    )
    history = []
    for msg in (conversation.get("messages") or []):
        history.append(
            {"role": "guest" if (msg.get("senderType") == "guest") else "host", "text": msg.get("body", "")}
        )

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
    }

    # If reservation includes a portal url, pass it (the handler checks + enforces confirmed status)
    for k in ("guestPortalUrl", "guest_portal_url", "portalUrl"):
        if reservation.get(k):
            meta["guest_portal_url"] = reservation.get(k)
            break

    # Nearby places (optional)
    nearby = build_local_recs(listing) if should_fetch_local_recs(listing) else []

    context_for_ai: Dict[str, Any] = {
        "guest_name": guest_name,
        "listing_info": listing or {},
        "reservation": reservation or {},
        "history": history[-8:],  # keep prompt small
        "nearby_places": nearby or [],
    }
    return meta | {"_ai_ctx": context_for_ai}


# ---------- Webhook ----------
@app.post("/unified-webhook")
async def unified_webhook(payload: HostawayUnifiedWebhook):
    """
    Expecting Hostaway-style "message.received" events for conversation messages.
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

    # Pull context (listing/reservation/history, nearby places)
    meta = _collect_context_from_hostaway(int(conv_id))
    meta["guest_message"] = guest_message

    # Generate AI suggestion
    ai_ctx = meta.get("_ai_ctx", {})
    ai_reply = generate_reply(guest_message, ai_ctx) if guest_message else "Thanks for your message! I’ll get back to you shortly."

    # Log (optional)
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

    # Build + send Slack card (with fixed action_ids & value keys)
    blocks = _build_message_blocks(guest_message, ai_reply, meta)
    _post_to_slack(blocks, channel=_fallback_channel_for_listing(None))

    mark_processed(event_key)
    return {"status": "ok"}


@app.get("/ping")
def ping():
    return {"status": "ok", "env": ENV}


# Mount Slack interactivity routes (/slack/actions, etc.)
app.include_router(slack_router, prefix="/slack")
