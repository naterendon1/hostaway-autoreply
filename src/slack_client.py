# file: src/slack_client.py
"""
Slack Client for Hostaway AutoReply
-----------------------------------
Handles:
- Posting structured message blocks (headers, guest photo, AI suggestions, buttons)
- Opening modals for message editing / improvement
- Processing tone rewrite and AI improvement interactions
"""

import os
import json
import logging
import requests
from typing import Any, Dict, Optional
from datetime import datetime
from slack_sdk import WebClient
from slack_sdk.errors import SlackApiError

from src.ai_engine import improve_message_with_ai, rewrite_tone

# ---------------------------------------------------------------------
# Environment setup
# ---------------------------------------------------------------------
SLACK_BOT_TOKEN = os.getenv("SLACK_BOT_TOKEN", "")
SLACK_SIGNING_SECRET = os.getenv("SLACK_SIGNING_SECRET", "")
SLACK_CHANNEL = os.getenv("SLACK_CHANNEL", "")
HOSTAWAY_API_BASE = os.getenv("HOSTAWAY_API_BASE", "https://api.hostaway.com/v1")
HOSTAWAY_ACCESS_TOKEN = os.getenv("HOSTAWAY_ACCESS_TOKEN")

client = WebClient(token=SLACK_BOT_TOKEN)


# ---------------------------------------------------------------------
# Formatting Helpers
# ---------------------------------------------------------------------
def _fmt_date(d: Optional[str]) -> str:
    """Formats a date like 2025-10-04T00:00:00Z → 2025-10-04."""
    if not d:
        return "N/A"
    try:
        if "T" in d:
            return datetime.fromisoformat(d.replace("Z", "+00:00")).date().isoformat()
        return d[:10]
    except Exception:
        return str(d)[:10] if len(str(d)) >= 10 else "N/A"


def _fmt_int(x: Any, default: str = "N/A") -> str:
    try:
        return str(int(x))
    except Exception:
        return default


def _pretty_property(meta: Dict[str, Any]) -> str:
    name = meta.get("property_name")
    addr = meta.get("property_address")
    if name and addr:
        return f"{name} — {addr}"
    return name or addr or "Property unavailable"


def _pretty_platform(meta: Dict[str, Any]) -> str:
    for k in ("channel_pretty", "platform", "channelName", "source"):
        v = meta.get(k)
        if v:
            return str(v).strip()
    return "Hostaway"


# ---------------------------------------------------------------------
# Slack Block Builders
# ---------------------------------------------------------------------
def _build_header_block(meta: Dict[str, Any], summary: Optional[str] = None, mood: Optional[str] = None) -> Dict[str, Any]:
    """Generates the Slack header block with guest, property, and booking info."""

    guest_name = meta.get("guest_name", "Guest")
    property_line = _pretty_property(meta)
    check_in = _fmt_date(meta.get("check_in"))
    check_out = _fmt_date(meta.get("check_out"))
    guests = _fmt_int(meta.get("guest_count"), "N/A")
    status = (meta.get("status") or "Unknown").title()
    platform = _pretty_platform(meta)
    conv_id = meta.get("conv_id")

    header_text = (
        f"*{platform}* · *{status}*\n"
        f"*{guest_name}* → *{property_line}*\n"
        f"*Dates:* {check_in} → {check_out} · *Guests:* {guests}"
    )

    if conv_id:
        header_text += f"\n*Conversation:* `{conv_id}`"

    if mood:
        header_text += f"\n*🧠 Mood:* {mood}"
    if summary:
        header_text += f"\n*📝 Summary:* {summary}"

    guest_photo = meta.get("guest_photo")
    block = {"type": "section", "text": {"type": "mrkdwn", "text": header_text}}

    if guest_photo:
        block["accessory"] = {
            "type": "image",
            "image_url": guest_photo,
            "alt_text": f"Photo of {guest_name}",
        }

    return block


def build_message_blocks(meta: Dict[str, Any], ai_result: Dict[str, str]) -> list:
    """Constructs Slack message blocks for the guest message + AI suggestion card."""
    guest_message = meta.get("guest_message", "")
    ai_suggestion = ai_result.get("suggested_reply", "")
    summary = ai_result.get("summary", "Summary unavailable.")
    mood = ai_result.get("mood", "Neutral")

    header_block = _build_header_block(meta, summary, mood)

    blocks = [
        {"type": "header", "text": {"type": "plain_text", "text": "Guest Message", "emoji": True}},
        header_block,
        {"type": "divider"},
        {
            "type": "section",
            "text": {"type": "mrkdwn", "text": f"> {guest_message}"},
        },
        {"type": "divider"},
        {
            "type": "section",
            "text": {"type": "mrkdwn", "text": f"*💡 Suggested Reply:*\n{ai_suggestion}"},
        },
        {
            "type": "actions",
            "block_id": "action_buttons",
            "elements": [
                {
                    "type": "button",
                    "text": {"type": "plain_text", "text": "Send"},
                    "style": "primary",
                    "action_id": "send",
                    "value": json.dumps({"conv_id": meta.get("conv_id"), "reply": ai_suggestion}),
                },
                {
                    "type": "button",
                    "text": {"type": "plain_text", "text": "Edit / Improve"},
                    "action_id": "open_edit_modal",
                    "value": json.dumps({
                        "guest_name": meta.get("guest_name", "Guest"),
                        "guest_message": guest_message,
                        "draft_text": ai_suggestion,
                        "conv_id": meta.get("conv_id"),
                        "meta": meta,
                    }),
                },
                {
                    "type": "button",
                    "text": {"type": "plain_text", "text": "Send Guest Portal"},
                    "action_id": "send_guest_portal",
                    "value": json.dumps({
                        "conv_id": meta.get("conv_id"),
                        "guest_portal_url": meta.get("guest_portal_url"),
                    }),
                },
            ],
        },
    ]
    return blocks


# ---------------------------------------------------------------------
# Slack Actions
# ---------------------------------------------------------------------
def post_message_to_slack(guest_message: str, ai_suggestion: str, meta: Dict[str, Any], mood: Optional[str] = None, summary: Optional[str] = None):
    """Posts the main AI card to Slack."""
    try:
        ai_result = {
            "suggested_reply": ai_suggestion,
            "summary": summary,
            "mood": mood,
        }
        meta["guest_message"] = guest_message
        blocks = build_message_blocks(meta, ai_result)
        client.chat_postMessage(
            channel=SLACK_CHANNEL,
            blocks=blocks,
            text=f"New guest message from {meta.get('guest_name','Guest')}",
        )
        return True
    except SlackApiError as e:
        logging.error(f"[SLACK] Failed to post message: {e}")
        return False


def handle_tone_rewrite(action_id: str, value: str, trigger_id: str):
    """Handles tone button presses."""
    try:
        data = json.loads(value)
        reply = data.get("reply", "")
        tone = "friendly" if "friendly" in action_id else "formal" if "formal" in action_id else "professional"
        new_reply = rewrite_tone(reply, tone)
        client.chat_postMessage(
            channel=SLACK_CHANNEL,
            text=f"*Rewritten ({tone.capitalize()} tone):*\n{new_reply}",
        )
        return new_reply
    except Exception as e:
        logging.error(f"[SLACK] Tone rewrite failed: {e}")
        return ""


def handle_improve_with_ai(action_value: str, trigger_id: str):
    """Handles the 'Improve with AI' button in the modal."""
    try:
        data = json.loads(action_value)
        draft_text = data.get("draft_text", "")
        meta = data.get("meta", {})
        improved = improve_message_with_ai(draft_text, meta)
        modal = {
            "type": "modal",
            "title": {"type": "plain_text", "text": "Improved Reply"},
            "close": {"type": "plain_text", "text": "Close"},
            "blocks": [
                {"type": "section", "text": {"type": "mrkdwn", "text": "*✨ Improved Message:*"}},
                {"type": "section", "text": {"type": "mrkdwn", "text": improved}},
            ],
        }
        client.views_open(trigger_id=trigger_id, view=modal)
        return improved
    except Exception as e:
        logging.error(f"[SLACK] Improve with AI failed: {e}")
        return ""


def open_edit_modal(trigger_id: str, payload: Dict[str, Any]):
    """Opens the edit modal when 'Edit / Improve' is clicked."""
    try:
        modal = build_edit_modal(payload)
        client.views_open(trigger_id=trigger_id, view=modal)
    except SlackApiError as e:
        logging.error(f"[SLACK] Failed to open modal: {e}")

# ---------------------------------------------------------------------
# Modal Builder (exported for interactivity)
# ---------------------------------------------------------------------
def build_edit_modal(payload: Dict[str, Any]) -> Dict[str, Any]:
    """
    Rebuilds the edit modal when 'Edit / Improve' is clicked in Slack.
    This version matches what slack_interactions.py expects.
    """
    meta = payload.get("meta", {})
    guest_name = payload.get("guest_name", meta.get("guest_name", "Guest"))
    guest_message = payload.get("guest_message", meta.get("guest_message", ""))
    draft_text = payload.get("draft_text", meta.get("draft_text", ""))

    header_text = (
        f"*✉️ Message from {guest_name}*\n"
        f"🏡 *Property:* {meta.get('property_name', 'Unknown')}*\n"
        f"📅 *Dates:* {meta.get('check_in', 'N/A')} → {meta.get('check_out', 'N/A')}\n"
        f"👥 *Guests:* {meta.get('guest_count', '?')} | *Status:* {meta.get('status', 'N/A')}*\n"
    )

    modal = {
        "type": "modal",
        "callback_id": "edit_modal_submit",
        "title": {"type": "plain_text", "text": "Edit AI Reply"},
        "submit": {"type": "plain_text", "text": "Send"},
        "close": {"type": "plain_text", "text": "Cancel"},
        "private_metadata": json.dumps(meta),
        "blocks": [
            {
                "type": "section",
                "text": {"type": "mrkdwn", "text": header_text},
            },
            {"type": "divider"},
            {
                "type": "input",
                "block_id": "reply_input",
                "label": {"type": "plain_text", "text": "Edit your reply"},
                "element": {
                    "type": "plain_text_input",
                    "action_id": "reply_text",
                    "multiline": True,
                    "initial_value": draft_text or "",
                },
            },
            {
                "type": "actions",
                "block_id": "improve_ai_actions",
                "elements": [
                    {
                        "type": "button",
                        "text": {"type": "plain_text", "text": "✨ Improve with AI"},
                        "action_id": "improve_with_ai",
                        "value": json.dumps({"draft_text": draft_text, "meta": meta}),
                    },
                ],
            },
        ],
    }
    return modal



# ---------------------------------------------------------------------
# Hostaway API Wrappers
# ---------------------------------------------------------------------
def send_hostaway_reply(conversation_id: int, message: str) -> bool:
    """Sends a reply to Hostaway conversation."""
    if not (HOSTAWAY_ACCESS_TOKEN and conversation_id and message):
        logging.warning("[send_hostaway_reply] Missing token, conversation_id, or message.")
        return False
    url = f"{HOSTAWAY_API_BASE}/conversations/{conversation_id}/messages"
    headers = {"Authorization": f"Bearer {HOSTAWAY_ACCESS_TOKEN}", "Content-Type": "application/json"}
    try:
        resp = requests.post(url, headers=headers, json={"body": message}, timeout=10)
        if resp.status_code == 200:
            logging.info(f"[Hostaway] Reply sent successfully to conversation {conversation_id}")
            return True
        logging.error(f"[Hostaway] Failed to send reply (status={resp.status_code}): {resp.text}")
        return False
    except Exception as e:
        logging.error(f"[Hostaway] Error sending reply: {e}")
        return False
