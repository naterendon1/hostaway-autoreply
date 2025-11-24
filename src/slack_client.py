# file: src/slack_client.py
"""
Slack Client for Hostaway AutoReply
-----------------------------------
- Post message card with Edit/Improve button
- Open edit modal
- Build edit modal (with pruned metadata)
- Send reply to Hostaway
"""

import os
import json
import logging
import requests
from typing import Any, Dict, Optional
from datetime import datetime

from slack_sdk import WebClient
from slack_sdk.errors import SlackApiError

# If you need AI helpers here, import from ai_engine ONLY (no imports from slack_interactions)
from src.ai_engine import improve_message_with_ai, rewrite_tone  # ok to keep if used

# --- Environment ---
SLACK_BOT_TOKEN = os.getenv("SLACK_BOT_TOKEN", "")
SLACK_CHANNEL = os.getenv("SLACK_CHANNEL", "")
HOSTAWAY_API_BASE = os.getenv("HOSTAWAY_API_BASE", "https://api.hostaway.com/v1")
HOSTAWAY_ACCESS_TOKEN = os.getenv("HOSTAWAY_ACCESS_TOKEN")

client = WebClient(token=SLACK_BOT_TOKEN)

# --- Helpers ---
def _fmt_date(d: Optional[str]) -> str:
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

def _prune_meta(meta: Dict[str, Any]) -> Dict[str, Any]:
    """Whitelist only small, needed fields to stay within Slack limits."""
    pruned = {
        "conv_id": meta.get("conv_id"),
        "guest_name": meta.get("guest_name", "Guest"),
        "guest_message": (meta.get("guest_message") or "")[:900],  # keep small
        "property_name": meta.get("property_name"),
        "property_address": meta.get("property_address"),
        "check_in": _fmt_date(meta.get("check_in")),
        "check_out": _fmt_date(meta.get("check_out")),
        "guest_count": _fmt_int(meta.get("guest_count"), "N/A"),
        "status": (meta.get("status") or "Unknown"),
        "platform": _pretty_platform(meta),
    }
    return {k: v for k, v in pruned.items() if v is not None}

# --- Blocks ---
def _build_header_block(meta: Dict[str, Any], summary: Optional[str] = None, mood: Optional[str] = None) -> Dict[str, Any]:
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

    block = {"type": "section", "text": {"type": "mrkdwn", "text": header_text}}
    if meta.get("guest_photo"):
        block["accessory"] = {
            "type": "image",
            "image_url": meta["guest_photo"],
            "alt_text": f"Photo of {guest_name}",
        }
    return block

def build_message_blocks(meta: Dict[str, Any], ai_result: Dict[str, str]) -> list:
    guest_message = meta.get("guest_message", "")
    ai_suggestion = ai_result.get("suggested_reply", "")
    summary = ai_result.get("summary", "Summary unavailable.")
    mood = ai_result.get("mood", "Neutral")

    header_block = _build_header_block(meta, summary, mood)
    small_meta = _prune_meta({**meta, "guest_message": guest_message})

    blocks = [
        {"type": "header", "text": {"type": "plain_text", "text": "Guest Message", "emoji": True}},
        header_block,
        {"type": "divider"},
        {"type": "section", "text": {"type": "mrkdwn", "text": f"> {guest_message}"}},
        {"type": "divider"},
        {"type": "section", "text": {"type": "mrkdwn", "text": f"*💡 Suggested Reply:*\n{ai_suggestion}"}},
        {
            "type": "actions",
            "block_id": "action_buttons",
            "elements": [
                {
                    "type": "button",
                    "text": {"type": "plain_text", "text": "Send"},
                    "style": "primary",
                    "action_id": "send",
                    "value": json.dumps({"conv_id": small_meta.get("conv_id"), "reply": ai_suggestion})[:1900],
                },
                {
                    "type": "button",
                    "text": {"type": "plain_text", "text": "Edit / Improve"},
                    "action_id": "open_edit_modal",
                    "value": json.dumps({
                        "guest_name": small_meta.get("guest_name", "Guest"),
                        "guest_message": small_meta.get("guest_message", ""),
                        "draft_text": ai_suggestion,
                        "conv_id": small_meta.get("conv_id"),
                        "meta": small_meta,
                    })[:1900],
                },
                {
                    "type": "button",
                    "text": {"type": "plain_text", "text": "Send Guest Portal"},
                    "action_id": "send_guest_portal",
                    "value": json.dumps({"conv_id": small_meta.get("conv_id"), "guest_portal_url": meta.get("guest_portal_url")})[:1900],
                },
            ],
        },
    ]
    return blocks

def post_message_to_slack(guest_message: str, ai_suggestion: str, meta: Dict[str, Any], mood: Optional[str] = None, summary: Optional[str] = None):
    try:
        ai_result = {"suggested_reply": ai_suggestion, "summary": summary, "mood": mood}
        meta = {**meta, "guest_message": guest_message}
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

def open_edit_modal(trigger_id: str, payload: Dict[str, Any]):
    """Open the edit modal. Payload is already pruned."""
    try:
        modal = build_edit_modal(payload)
        client.views_open(trigger_id=trigger_id, view=modal)
    except SlackApiError as e:
        logging.error(f"[SLACK] Failed to open modal: {e}")

def build_edit_modal(payload: Dict[str, Any]) -> Dict[str, Any]:
    """
    Build a Slack modal for editing the AI reply.
    Keeps fields small; private_metadata <= 3000 chars by pruning.
    """
    meta = _prune_meta(payload.get("meta", {}))
    guest_name = payload.get("guest_name", meta.get("guest_name", "Guest"))
    guest_message = payload.get("guest_message", meta.get("guest_message", ""))
    draft_text = payload.get("draft_text", meta.get("draft_text", ""))
    
    header_text = (
        f"*✉️ Message from {guest_name}*\n"
        f"🏡 *Property:* {_pretty_property(meta)}\n"
        f"📅 *Dates:* {meta.get('check_in', 'N/A')} → {meta.get('check_out', 'N/A')}\n"
        f"👥 *Guests:* {meta.get('guest_count', '?')} | *Status:* {meta.get('status', 'N/A')}"
    )
    
    # Get conv_id from either top-level payload or nested meta
    conv_id = payload.get("conv_id") or meta.get("conv_id") or payload.get("conversation_id") or meta.get("conversation_id")
    
    pm = {
        "conv_id": conv_id,
        "guest_name": guest_name,
        "guest_message": guest_message[:900],
        "property_name": meta.get("property_name"),
        "property_address": meta.get("property_address"),
        "check_in": meta.get("check_in"),
        "check_out": meta.get("check_out"),
        "guest_count": meta.get("guest_count"),
        "status": meta.get("status"),
        "platform": meta.get("platform"),
    }
    private_metadata = json.dumps(pm)[:2900]
    
    modal = {
        "type": "modal",
        "callback_id": "edit_modal_submit",
        "title": {"type": "plain_text", "text": "Edit AI Reply"},
        "submit": {"type": "plain_text", "text": "Send"},
        "close": {"type": "plain_text", "text": "Cancel"},
        "private_metadata": private_metadata,
        "blocks": [
            {"type": "section", "text": {"type": "mrkdwn", "text": header_text}},
            {"type": "divider"},
            {
                "type": "input",
                "block_id": "reply_input",
                "label": {"type": "plain_text", "text": "Edit your reply"},
                "element": {
                    "type": "plain_text_input",
                    "action_id": "reply_text",
                    "multiline": True,
                    "initial_value": (draft_text or "")[:2800],
                },
            },
            {
                "type": "input",
                "block_id": "coach_prompt_block",
                "optional": True,
                "label": {"type": "plain_text", "text": "Coach the AI (optional)"},
                "element": {
                    "type": "plain_text_input",
                    "action_id": "coach_prompt",
                    "multiline": True,
                    "placeholder": {
                        "type": "plain_text",
                        "text": "Tell AI how to adjust (e.g., 'be more formal', 'mention parking')"
                    }
                }
            },
            {
                "type": "actions",
                "block_id": "improve_ai_actions",
                "elements": [
                    {
                        "type": "button",
                        "text": {"type": "plain_text", "text": "✨ Improve with AI"},
                        "action_id": "improve_with_ai",
                        # CHANGED: Include conv_id and guest_message for context
                        "value": json.dumps({
                            "conv_id": conv_id,
                            "guest_message": guest_message[:800],
                            "draft_text": (draft_text or "")[:800]
                        })[:1500],
                    },
                ],
            },
        ],
    }
    return modal

def send_hostaway_reply(conversation_id: int, message: str) -> bool:
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
