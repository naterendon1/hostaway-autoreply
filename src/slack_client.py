# file: src/slack_client.py
import os
import json
import logging
from typing import Dict, Any, List, Optional

from slack_sdk import WebClient
from slack_sdk.errors import SlackApiError

# Initialize Slack client
SLACK_BOT_TOKEN = os.getenv("SLACK_BOT_TOKEN", "")
SLACK_CHANNEL = os.getenv("SLACK_CHANNEL", "")
slack_client = WebClient(token=SLACK_BOT_TOKEN) if SLACK_BOT_TOKEN else None


# ---------------- Helper: Build context/photo section ----------------
def _build_guest_context(meta: Dict[str, Any]) -> Dict[str, Any]:
    """Builds the Slack context block with guest photo and info."""
    elements = []

    # Add guest profile photo (if available)
    guest_photo_url = meta.get("guest_photo_url")
    if guest_photo_url:
        elements.append({
            "type": "image",
            "image_url": guest_photo_url,
            "alt_text": f"{meta.get('guest_name', 'Guest')} photo"
        })

    # Add platform and name
    guest_name = meta.get("guest_name", "Guest")
    platform = meta.get("platform", "Unknown")
    elements.append({
        "type": "mrkdwn",
        "text": f"*{guest_name}* via *{platform}*"
    })

    # Optional: Add perceived mood (if available from AI)
    if meta.get("guest_mood"):
        mood = meta["guest_mood"]
        emoji = "🙂" if mood == "neutral" else "😃" if mood == "happy" else "😟"
        elements.append({"type": "mrkdwn", "text": f"*Mood:* {emoji} {mood.title()}"})

    return {"type": "context", "elements": elements}


# ---------------- Helper: Build header block ----------------
def _build_header_block(meta: Dict[str, Any]) -> Dict[str, Any]:
    """Builds the main header with property and reservation details."""
    property_name = meta.get("property_name", "Unknown Property")
    property_address = meta.get("property_address", "Unknown Address")
    check_in = meta.get("check_in", "N/A")
    check_out = meta.get("check_out", "N/A")
    guest_count = meta.get("guest_count", "?")
    price_str = meta.get("price_str", "$N/A")
    status = meta.get("status", "N/A")

    header_text = (
        f"🏡 *{property_name}*\n"
        f"📍 {property_address}\n"
        f"📅 *{check_in} → {check_out}* | 👥 *{guest_count} guests* | 💵 {price_str} | {status}"
    )

    # Add optional conversation summary from AI
    if meta.get("conversation_summary"):
        header_text += f"\n\n🗒️ *Conversation Summary:*\n_{meta['conversation_summary']}_"

    return {"type": "section", "text": {"type": "mrkdwn", "text": header_text}}


# ---------------- Helper: Build action buttons ----------------
def _build_action_buttons(meta: Dict[str, Any], guest_message: str, ai_suggestion: str) -> Dict[str, Any]:
    """Builds interactive buttons for Slack message cards."""
    conv_id = meta.get("conv_id")

    send_payload = {
        "conv_id": conv_id,
        "reply_text": ai_suggestion,
        "guest_message": guest_message,
        "type": meta.get("type", "email"),
    }

    edit_payload = {
        "conv_id": conv_id,
        "guest_message": guest_message,
        "draft_text": ai_suggestion,
        **{k: meta.get(k) for k in (
            "guest_name", "listing_id", "reservation_id",
            "check_in", "check_out", "guest_count",
            "property_address", "property_name"
        )}
    }

    portal_payload = {
        "conv_id": conv_id,
        "guest_portal_url": meta.get("guest_portal_url"),
        "status": meta.get("status", "").lower(),
        "type": meta.get("type", "email"),
    }

    tone_payloads = {
        "friendlier": {**send_payload, "tone": "friendlier"},
        "formal": {**send_payload, "tone": "formal"},
    }

    return {
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
                "text": {"type": "plain_text", "text": "More Friendly"},
                "action_id": "adjust_tone_friendlier",
                "value": json.dumps(tone_payloads["friendlier"]),
            },
            {
                "type": "button",
                "text": {"type": "plain_text", "text": "More Formal"},
                "action_id": "adjust_tone_formal",
                "value": json.dumps(tone_payloads["formal"]),
            },
            {
                "type": "button",
                "text": {"type": "plain_text", "text": "Send Guest Portal"},
                "action_id": "send_guest_portal",
                "value": json.dumps(portal_payload),
            }
        ],
    }


# ---------------- Public: Post Slack card ----------------
def post_guest_message_to_slack(
    meta: Dict[str, Any],
    guest_message: str,
    ai_suggestion: str
) -> Optional[Dict[str, Any]]:
    """Sends a styled Slack card summarizing guest info and AI reply."""

    if not slack_client or not SLACK_CHANNEL:
        logging.warning("Slack not configured correctly; skipping post.")
        return None

    try:
        # Build all sections
        blocks: List[Dict[str, Any]] = [
            _build_guest_context(meta),
            _build_header_block(meta),
            {"type": "section", "text": {"type": "mrkdwn", "text": f"💬 *Guest Message:*\n{guest_message}"}},
            {"type": "section", "text": {"type": "mrkdwn", "text": f"💡 *Suggested Reply:*\n{ai_suggestion}"}},
            _build_action_buttons(meta, guest_message, ai_suggestion),
        ]

        response = slack_client.chat_postMessage(
            channel=SLACK_CHANNEL,
            blocks=blocks,
            text="New guest message received"
        )

        return response.data if hasattr(response, "data") else response

    except SlackApiError as e:
        logging.error(f"[Slack] Message post failed: {e.response.data if hasattr(e, 'response') else e}")
        return None
