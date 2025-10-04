# file: src/slack_client.py
import os
import json
import logging
from typing import Dict, Any, Optional, List
from slack_sdk import WebClient
from slack_sdk.errors import SlackApiError

SLACK_BOT_TOKEN = os.getenv("SLACK_BOT_TOKEN", "")
SLACK_CHANNEL = os.getenv("SLACK_CHANNEL", "")

slack_client = WebClient(token=SLACK_BOT_TOKEN) if SLACK_BOT_TOKEN else None

# ---------------- Helper functions for Slack message composition ----------------
def _build_guest_context(meta: dict) -> str:
    """
    Build a concise guest context summary used in Slack headers.
    """
    guest_name = meta.get("guest_name", "Guest")
    property_name = meta.get("property_name", "Unknown Property")
    address = meta.get("property_address", "")
    check_in = meta.get("check_in", "N/A")
    check_out = meta.get("check_out", "N/A")
    guest_count = meta.get("guest_count", "?")
    price_str = meta.get("price_str", "$N/A")
    platform = meta.get("platform", "Unknown")
    mood = meta.get("guest_mood", "Neutral 😐")
    summary = meta.get("conversation_summary", "No summary available yet.")

    return (
        f"*✉️ Message from {guest_name}*\n"
        f"🏡 *Property:* {property_name}\n"
        f"📍 {address}\n"
        f"📅 *{check_in} → {check_out}* | 👥 {guest_count} guests | 💰 {price_str} | 🌐 {platform}\n"
        f"🧠 *Mood:* {mood}\n"
        f"🗒️ *Conversation Summary:* {summary}"
    )


def _build_header_block(meta: dict) -> dict:
    """
    Returns a Slack Block Kit section block for the header.
    """
    header_text = _build_guest_context(meta)
    return {
        "type": "section",
        "text": {"type": "mrkdwn", "text": header_text},
        # Add image if guest uploaded one (Airbnb guests often attach photos)
        "accessory": {
            "type": "image",
            "image_url": meta.get("guest_image_url", "https://i.imgur.com/9M3K4sY.png"),
            "alt_text": meta.get("guest_name", "Guest"),
        },
    }



# -------------------- Helper: Post Slack Message --------------------
def post_message_to_slack(
    guest_message: str,
    ai_suggestion: str,
    meta: Dict[str, Any],
    mood: Optional[str] = None,
    summary: Optional[str] = None,
) -> Optional[Dict[str, Any]]:
    """
    Posts the main Slack message with header, guest info, summary, and buttons.
    """

    if not slack_client:
        logging.warning("Slack not configured; skipping message post.")
        return None

    guest_name = meta.get("guest_name", "Guest")
    property_name = meta.get("property_name", "Unknown Property")
    property_address = meta.get("property_address", "Unknown Address")
    check_in = meta.get("check_in", "N/A")
    check_out = meta.get("check_out", "N/A")
    guest_count = meta.get("guest_count", "?")
    status = meta.get("status", "N/A")
    price_str = meta.get("price_str", "$N/A")
    platform = meta.get("platform", "Unknown")
    guest_photo = meta.get("guest_photo")
    conversation_id = meta.get("conv_id")

    # -------------------- Header Composition --------------------
    header_text = (
        f"*✉️ Message from {guest_name}*"
        + (f" (Mood: *{mood}*)" if mood else "")
        + "\n"
        f"🏡 *Property:* {property_name}\n"
        f"📍 {property_address}\n"
        f"📅 *{check_in} → {check_out}*\n"
        f"👥 {guest_count} guests | Status: *{status}* | {price_str} | Platform: *{platform}*\n"
    )

    if summary:
        header_text += f"\n🗒️ *Conversation Summary:* {summary}\n"

    # -------------------- Build Action Buttons --------------------
    send_payload = {
        "conv_id": conversation_id,
        "reply": ai_suggestion,
        "guest_message": guest_message,
    }
    edit_payload = {
        **meta,
        "guest_message": guest_message,
        "draft_text": ai_suggestion,
    }
    portal_payload = {
        "conv_id": conversation_id,
        "guest_portal_url": meta.get("guest_portal_url"),
    }

    # Tone buttons payloads
    tone_buttons = [
        {
            "type": "button",
            "text": {"type": "plain_text", "text": "More Friendly"},
            "action_id": "tone_friendly",
            "value": json.dumps({"conv_id": conversation_id, "tone": "friendly", "guest_message": guest_message}),
        },
        {
            "type": "button",
            "text": {"type": "plain_text", "text": "More Formal"},
            "action_id": "tone_formal",
            "value": json.dumps({"conv_id": conversation_id, "tone": "formal", "guest_message": guest_message}),
        },
    ]

    # -------------------- Construct Blocks --------------------
    blocks: List[Dict[str, Any]] = []

    # Include guest photo if available (e.g. Airbnb profile picture)
    if guest_photo:
        blocks.append({
            "type": "image",
            "image_url": guest_photo,
            "alt_text": f"Photo of {guest_name}"
        })

    # Header
    blocks.append({
        "type": "section",
        "text": {"type": "mrkdwn", "text": header_text},
    })

    # Guest message
    blocks.append({
        "type": "section",
        "text": {"type": "mrkdwn", "text": f"💬 *Guest Message:*\n{guest_message}"},
    })

    # AI suggested reply
    blocks.append({
        "type": "section",
        "text": {"type": "mrkdwn", "text": f"💡 *Suggested Reply:*\n{ai_suggestion}"},
    })

    # Main buttons (Send / Edit / Portal)
    blocks.append({
        "type": "actions",
        "block_id": "action_buttons",
        "elements": [
            {
                "type": "button",
                "text": {"type": "plain_text", "text": "Send"},
                "style": "primary",
                "action_id": "send",
                "value": json.dumps(send_payload),
            },
            {
                "type": "button",
                "text": {"type": "plain_text", "text": "Edit"},
                "action_id": "open_edit_modal",
                "value": json.dumps(edit_payload),
            },
            {
                "type": "button",
                "text": {"type": "plain_text", "text": "Send Guest Portal"},
                "action_id": "send_guest_portal",
                "value": json.dumps(portal_payload),
            },
        ],
    })

    # Tone change buttons
    blocks.append({
        "type": "actions",
        "block_id": "tone_buttons",
        "elements": tone_buttons,
    })

    # -------------------- Send to Slack --------------------
    try:
        resp = slack_client.chat_postMessage(
            channel=SLACK_CHANNEL,
            blocks=blocks,
            text=f"Message from {guest_name} at {property_name}"
        )
        return resp.data if hasattr(resp, "data") else resp
    except SlackApiError as e:
        logging.error(f"[Slack] post_message_to_slack failed: {e.response['error']}")
        return None


# -------------------- Helper: Update Message --------------------
def update_message_in_slack(ts: str, channel: str, blocks: List[Dict[str, Any]]):
    """Allows the message header and blocks to stay consistent when updates occur."""
    try:
        slack_client.chat_update(channel=channel, ts=ts, blocks=blocks)
    except Exception as e:
        logging.error(f"[Slack] update_message_in_slack failed: {e}")
