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
# Slack Block Builders
# ---------------------------------------------------------------------
def _build_header_block(meta: Dict[str, Any], summary: Optional[str] = None, mood: Optional[str] = None) -> Dict[str, Any]:
    """Generates the Slack header block with guest + listing info."""
    header_text = (
        f"*✉️ Message from {meta.get('guest_name','Guest')}*\n"
        f"🏡 *Property:* {meta.get('property_name') or meta.get('property_address','Unknown')}*\n"
        f"📅 *Dates:* {meta.get('check_in','N/A')} → {meta.get('check_out','N/A')}\n"
        f"👥 *Guests:* {meta.get('guest_count','?')} | *Status:* {meta.get('status','N/A')} | "
        f"*Platform:* {meta.get('platform','Unknown')}*\n"
    )

    if mood:
        header_text += f"\n*🧠 Mood:* {mood}"
    if summary:
        header_text += f"\n*📝 Summary:* {summary}"

    guest_photo = meta.get("guest_photo")

    if guest_photo:
        return {
            "type": "section",
            "text": {"type": "mrkdwn", "text": header_text},
            "accessory": {
                "type": "image",
                "image_url": guest_photo,
                "alt_text": f"Photo of {meta.get('guest_name','Guest')}",
            },
        }
    else:
        return {"type": "section", "text": {"type": "mrkdwn", "text": header_text}}


def build_message_blocks(meta: Dict[str, Any], ai_result: Dict[str, str]) -> list:
    """Constructs Slack message blocks for the guest message + AI suggestion card."""
    guest_message = meta.get("guest_message", "")
    ai_suggestion = ai_result.get("suggested_reply", "")
    summary = ai_result.get("summary", "Summary unavailable.")
    mood = ai_result.get("mood", "Neutral")

    header_block = _build_header_block(meta, summary, mood)

    blocks = [
        header_block,
        {"type": "divider"},
        {
            "type": "section",
            "text": {"type": "mrkdwn", "text": f"💬 *Guest Message:*\n>{guest_message}"},
        },
        {"type": "divider"},
        {
            "type": "section",
            "text": {"type": "mrkdwn", "text": f"💡 *Suggested Reply:*\n{ai_suggestion}"},
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
                        "meta": meta
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
        {
            "type": "actions",
            "block_id": "tone_buttons",
            "elements": [
                {"type": "button", "text": {"type": "plain_text", "text": "Friendly Tone"},
                 "action_id": "rewrite_friendly", "value": json.dumps({"reply": ai_suggestion})},
                {"type": "button", "text": {"type": "plain_text", "text": "Formal Tone"},
                 "action_id": "rewrite_formal", "value": json.dumps({"reply": ai_suggestion})},
                {"type": "button", "text": {"type": "plain_text", "text": "Professional Tone"},
                 "action_id": "rewrite_professional", "value": json.dumps({"reply": ai_suggestion})},
            ],
        },
    ]
    return blocks


def build_edit_modal(payload: Dict[str, Any]) -> Dict[str, Any]:
    """Creates the modal that opens when 'Edit / Improve' is clicked."""
    meta = payload.get("meta", {})
    guest_name = payload.get("guest_name", "Guest")
    guest_message = payload.get("guest_message", "")
    draft_text = payload.get("draft_text", "")

    header_block = _build_header_block(meta)

    modal = {
        "type": "modal",
        "callback_id": "edit_modal_submit",
        "title": {"type": "plain_text", "text": "Edit Reply"},
        "submit": {"type": "plain_text", "text": "Send"},
        "close": {"type": "plain_text", "text": "Cancel"},
        "private_metadata": json.dumps(meta),
        "blocks": [
            header_block,
            {"type": "divider"},
            {
                "type": "input",
                "block_id": "reply_input",
                "element": {
                    "type": "plain_text_input",
                    "action_id": "reply_text",
                    "multiline": True,
                    "initial_value": draft_text,
                },
                "label": {"type": "plain_text", "text": "Edit your message"},
            },
            {
                "type": "actions",
                "block_id": "improve_ai_actions",
                "elements": [
                    {"type": "button",
                     "text": {"type": "plain_text", "text": "✨ Improve with AI"},
                     "action_id": "improve_with_ai",
                     "value": json.dumps({"draft_text": draft_text, "meta": meta})},
                ],
            },
        ],
    }
    return modal


# ---------------------------------------------------------------------
# Slack Actions
# ---------------------------------------------------------------------
def post_message_to_slack(meta: Dict[str, Any], ai_result: Dict[str, Any]):
    """Posts the main AI card to Slack."""
    try:
        guest_message = meta.get("guest_message", "")
        ai_suggestion = ai_result.get("suggested_reply", "")
        summary = ai_result.get("summary")
        mood = ai_result.get("mood")

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


def fetch_hostaway_reservation(reservation_id: int):
    if not reservation_id:
        return {}
    url = f"{HOSTAWAY_API_BASE}/reservations/{reservation_id}"
    headers = {"Authorization": f"Bearer {HOSTAWAY_ACCESS_TOKEN}"}
    try:
        return requests.get(url, headers=headers, timeout=10).json()
    except Exception as e:
        logging.error(f"[fetch_hostaway_reservation] {e}")
        return {}


def fetch_hostaway_listing(listing_id: int):
    if not listing_id:
        return {}
    url = f"{HOSTAWAY_API_BASE}/listings/{listing_id}"
    headers = {"Authorization": f"Bearer {HOSTAWAY_ACCESS_TOKEN}"}
    try:
        return requests.get(url, headers=headers, timeout=10).json()
    except Exception as e:
        logging.error(f"[fetch_hostaway_listing] {e}")
        return {}


def fetch_hostaway_conversation(conversation_id: int):
    if not conversation_id:
        return {}
    url = f"{HOSTAWAY_API_BASE}/conversations/{conversation_id}"
    headers = {"Authorization": f"Bearer {HOSTAWAY_ACCESS_TOKEN}"}
    try:
        return requests.get(url, headers=headers, timeout=10).json()
    except Exception as e:
        logging.error(f"[fetch_hostaway_conversation] {e}")
        return {}
