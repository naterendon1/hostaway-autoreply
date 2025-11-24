# file: src/slack_interactions.py
"""
Enhanced Slack interactions with fully fixed modal handling:
- Proper lifecycle with views.open and views.update
- Correct block_id/action_id preservation
- Async processing for "Improve with AI"
- Undo and Send functions
- Signature verification
- Error recovery
"""

import os
import json
import logging
import hmac
import hashlib
import time
import uuid
from typing import Optional, Dict, Any

from fastapi import APIRouter, Request, Header, HTTPException, BackgroundTasks
from fastapi.responses import JSONResponse
from openai import OpenAI
from slack_sdk.errors import SlackApiError

from src.slack_client import (
    client as slack_client,
    send_hostaway_reply,
)
from src.ai_engine import improve_message_with_ai

router = APIRouter()
slack_interactions_bp = router

SLACK_SIGNING_SECRET = os.getenv("SLACK_SIGNING_SECRET", "")
SLACK_BOT_TOKEN = os.getenv("SLACK_BOT_TOKEN", "")
MAX_PRIVATE_BYTES = 2800  # Slack limit is 3000, leave buffer

OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
openai_client = OpenAI(api_key=OPENAI_API_KEY) if OPENAI_API_KEY else None

# -------------------- Slack Signature Verification --------------------
def verify_slack_signature(body: str, signature: Optional[str], timestamp: Optional[str]) -> bool:
    if not SLACK_SIGNING_SECRET:
        logging.warning("SLACK_SIGNING_SECRET not set. Verification disabled.")
        return True

    if not signature or not timestamp:
        return False

    if abs(time.time() - int(timestamp)) > 60 * 5:
        return False

    base = f"v0:{timestamp}:{body}".encode("utf-8")
    my_sig = "v0=" + hmac.new(SLACK_SIGNING_SECRET.encode("utf-8"), base, hashlib.sha256).hexdigest()
    return hmac.compare_digest(my_sig, signature)

# -------------------- Metadata Packing --------------------
PRIVATE_META_KEYS = {
    "conv_id", "conversation_id", "listing_id", "guest_id", "guest_name",
    "guest_message", "type", "status", "check_in", "check_out", "guest_count",
    "channel", "ts", "detected_intent", "channel_pretty", "property_address",
    "property_name", "guest_portal_url", "reservation_id", "sent_label",
    "checkbox_checked", "coach_prompt", "location", "fingerprint",
    "previous_draft", "draft_text"
}

def pack_private_meta(meta: Dict[str, Any]) -> str:
    thin = {k: meta.get(k) for k in PRIVATE_META_KEYS if k in meta}
    s = json.dumps(thin, ensure_ascii=False)
    if len(s.encode("utf-8")) <= MAX_PRIVATE_BYTES:
        return s

    for k in ("property_address", "guest_message", "draft_text"):
        if k in thin and isinstance(thin[k], str) and len(thin[k]) > 800:
            thin[k] = thin[k][:800]
            s = json.dumps(thin, ensure_ascii=False)
            if len(s.encode("utf-8")) <= MAX_PRIVATE_BYTES:
                break

    enc = s.encode("utf-8")
    if len(enc) > MAX_PRIVATE_BYTES:
        enc = enc[:MAX_PRIVATE_BYTES]
        s = enc.decode("utf-8", errors="ignore")
    return s

# -------------------- Extract Input Helper --------------------
def _extract_input_text(state_values: dict) -> str:
    """Extract plain_text_input value from modal."""
    for block in state_values.values():
        for val in block.values():
            if isinstance(val, dict) and "value" in val:
                text = val.get("value", "").strip()
                if text:
                    return text
    return ""

# -------------------- Modal Builder --------------------
def get_modal_blocks(
    guest_name: str,
    guest_msg: str,
    draft_text: str = "",
    coach_prompt_initial: Optional[str] = None,
    has_undo: bool = False
) -> list:
    """Constructs modal with input and AI actions."""
    reply_input_block = {
        "type": "input",
        "block_id": "reply_input_ai",
        "label": {"type": "plain_text", "text": "Edit your reply:"},
        "element": {
            "type": "plain_text_input",
            "action_id": "reply_ai",
            "multiline": True,
            "initial_value": draft_text or ""
        }
    }

    coach_block = {
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
                "text": "e.g. 'make it shorter', 'add parking info'"
            },
            "initial_value": coach_prompt_initial or ""
        },
    }

    blocks = [
        {
            "type": "section",
            "block_id": "guest_info",
            "text": {
                "type": "mrkdwn",
                "text": f"*Guest:* {guest_name}\n*Message:* {guest_msg[:500]}"
            },
        },
        {"type": "divider"},
        reply_input_block,
        coach_block,
        {
            "type": "actions",
            "block_id": "ai_actions",
            "elements": [
                {
                    "type": "button",
                    "action_id": "improve_with_ai",
                    "text": {"type": "plain_text", "text": "✨ Improve with AI"},
                    "style": "primary",
                }
            ]
        }
    ]

    if has_undo:
        blocks.append({
            "type": "actions",
            "block_id": "undo_block",
            "elements": [
                {
                    "type": "button",
                    "action_id": "undo_ai",
                    "text": {"type": "plain_text", "text": "↩️ Undo AI"},
                }
            ]
        })
    return blocks

# -------------------- Background AI Update --------------------
def _background_improve_and_update(view_id: str, meta: dict, edited_text: str,
                                   coach_prompt: Optional[str], guest_name: str, guest_msg: str):
    """Improves reply text via AI and updates the Slack modal view."""
    improved = edited_text
    error_message = None
    try:
        if not openai_client:
            raise ValueError("OpenAI API key not configured.")
        system_prompt = (
            "You improve guest message replies for a vacation rental host. "
            "Maintain meaning but improve tone, clarity, and brevity. "
            "No greetings, no sign-offs, no emojis. "
            "Style: concise, friendly, helpful."
        )
        user_prompt = f"""
Guest message:
{guest_msg}

Current draft:
{edited_text}

Coach instructions: {coach_prompt or '(none)'}

Improve the reply accordingly and return ONLY the improved text.
"""
        resp = openai_client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            temperature=0.7,
            max_tokens=500
        )
        improved = resp.choices[0].message.content.strip()
    except Exception as e:
        logging.error(f"[AI] Error improving with AI: {e}", exc_info=True)
        error_message = str(e)

    new_meta = {**meta, "previous_draft": edited_text, "coach_prompt": coach_prompt or ""}
    blocks = get_modal_blocks(
        guest_name=guest_name,
        guest_msg=guest_msg,
        draft_text=improved,
        coach_prompt_initial=coach_prompt or "",
        has_undo=True
    )

    if error_message:
        blocks.insert(0, {
            "type": "section",
            "text": {"type": "mrkdwn", "text": f"⚠️ *AI improvement failed:* {error_message}"}
        })

    view_update = {
        "type": "modal",
        "callback_id": "edit_modal_submit",
        "title": {"type": "plain_text", "text": "AI Improved Reply"},
        "submit": {"type": "plain_text", "text": "Send"},
        "close": {"type": "plain_text", "text": "Cancel"},
        "private_metadata": pack_private_meta(new_meta),
        "blocks": blocks,
    }

    try:
        slack_client.views_update(view_id=view_id, view=view_update)
    except SlackApiError as e:
        logging.error(f"[Slack] Failed to update modal: {e.response['error']}")

# -------------------- Background Send to Hostaway --------------------
def _background_send_to_hostaway(meta: dict, reply_text: str):
    """Send reply to Hostaway and confirm in Slack."""
    conv_id = meta.get("conv_id") or meta.get("conversation_id")
    channel = meta.get("channel")
    ts = meta.get("ts")

    logging.info(f"[Hostaway] Sending reply to conversation {conv_id}")
    try:
        success = send_hostaway_reply(conv_id, reply_text)
        if success:
            if slack_client and channel and ts:
                slack_client.chat_postMessage(
                    channel=channel,
                    thread_ts=ts,
                    text=f"✅ Reply sent to guest:\n>{reply_text[:200]}{'...' if len(reply_text) > 200 else ''}"
                )
        else:
            if slack_client and channel and ts:
                slack_client.chat_postMessage(
                    channel=channel,
                    thread_ts=ts,
                    text="❌ Failed to send reply to Hostaway."
                )
    except Exception as e:
        logging.error(f"[Hostaway] Exception sending reply: {e}", exc_info=True)

# -------------------- Slack Interactivity --------------------
@router.post("/interactivity")
async def handle_slack_interaction(
    request: Request,
    background_tasks: BackgroundTasks,
    x_slack_signature: Optional[str] = Header(None, alias="X-Slack-Signature"),
    x_slack_request_timestamp: Optional[str] = Header(None, alias="X-Slack-Request-Timestamp"),
    x_slack_retry_num: Optional[str] = Header(None, alias="X-Slack-Retry-Num"),
):
    """Main Slack interactivity entrypoint."""

    # Skip retries
    if x_slack_retry_num:
        return JSONResponse({"ok": True})

    body_bytes = await request.body()
    body_str = body_bytes.decode("utf-8")

    if not verify_slack_signature(body_str, x_slack_signature, x_slack_request_timestamp):
        raise HTTPException(status_code=401, detail="Invalid Slack signature")

    form = await request.form()
    payload = json.loads(form.get("payload", "{}"))
    payload_type = payload.get("type")
    logging.info(f"[Slack] Interaction: {payload_type}")

    # -------------------- BLOCK ACTIONS --------------------
    if payload_type == "block_actions":
        actions = payload.get("actions", [])
        if not actions:
            return JSONResponse({"ok": True})

        action = actions[0]
        action_id = action.get("action_id")
        trigger_id = payload.get("trigger_id")
        view = payload.get("view")
        view_id = view.get("id") if view else None

        try:
            meta = json.loads(action.get("value", "{}"))
        except Exception:
            meta = {}

        container = payload.get("container", {})
        channel_id = container.get("channel_id")
        message_ts = container.get("message_ts")

        meta["channel"] = channel_id
        meta["ts"] = message_ts
        meta["fingerprint"] = meta.get("fingerprint") or f"{channel_id}|{message_ts}|{uuid.uuid4()}"

        # ---- OPEN EDIT MODAL ----
        if action_id in ("edit", "open_edit_modal"):
            guest_name = meta.get("guest_name", "Guest")
            guest_msg = meta.get("guest_message", "")
            draft_text = meta.get("draft_text") or meta.get("ai_suggestion", "")

            modal_view = {
                "type": "modal",
                "callback_id": "edit_modal_submit",
                "title": {"type": "plain_text", "text": "Edit AI Reply"},
                "submit": {"type": "plain_text", "text": "Send"},
                "close": {"type": "plain_text", "text": "Cancel"},
                "notify_on_close": True,
                "private_metadata": pack_private_meta(meta),
                "blocks": get_modal_blocks(
                    guest_name, guest_msg, draft_text=draft_text, coach_prompt_initial=meta.get("coach_prompt")
                ),
            }

            try:
                slack_client.views_open(trigger_id=trigger_id, view=modal_view)
            except SlackApiError as e:
                logging.error(f"[Slack] Failed to open modal: {e.response['error']}")
            return JSONResponse({"ok": True})

        # ---- IMPROVE WITH AI ----
        elif action_id == "improve_with_ai" and view_id:
            state_values = view.get("state", {}).get("values", {})
            edited_text = _extract_input_text(state_values)
            coach_prompt = ""
            if "coach_prompt_block" in state_values:
                coach_prompt = state_values["coach_prompt_block"]["coach_prompt"].get("value", "")

            try:
                meta = json.loads(view.get("private_metadata", "{}"))
            except Exception:
                meta = {}

            guest_name = meta.get("guest_name", "Guest")
            guest_msg = meta.get("guest_message", "")

            loading_view = {
                "response_action": "update",
                "view": {
                    "type": "modal",
                    "callback_id": "edit_modal_submit",
                    "title": {"type": "plain_text", "text": "Improving..."},
                    "close": {"type": "plain_text", "text": "Cancel"},
                    "private_metadata": pack_private_meta(meta),
                    "blocks": [
                        {"type": "section", "text": {"type": "mrkdwn", "text": "⏳ *Improving with AI...*"}}
                    ] + get_modal_blocks(guest_name, guest_msg, draft_text=edited_text, coach_prompt_initial=coach_prompt)
                }
            }

            background_tasks.add_task(
                _background_improve_and_update, view_id, meta, edited_text, coach_prompt, guest_name, guest_msg
            )
            return JSONResponse(loading_view)

        # ---- UNDO AI ----
        elif action_id == "undo_ai":
            try:
                meta = json.loads(view.get("private_metadata", "{}"))
            except Exception:
                meta = {}
            prev = meta.get("previous_draft")
            if not prev:
                return JSONResponse({"ok": True})

            guest_name = meta.get("guest_name", "Guest")
            guest_msg = meta.get("guest_message", "")
            meta.pop("previous_draft", None)

            undo_view = {
                "response_action": "update",
                "view": {
                    "type": "modal",
                    "callback_id": "edit_modal_submit",
                    "title": {"type": "plain_text", "text": "Edit AI Reply"},
                    "submit": {"type": "plain_text", "text": "Send"},
                    "close": {"type": "plain_text", "text": "Cancel"},
                    "private_metadata": pack_private_meta(meta),
                    "blocks": get_modal_blocks(
                        guest_name, guest_msg, draft_text=prev, coach_prompt_initial=meta.get("coach_prompt", "")
                    )
                }
            }
            return JSONResponse(undo_view)

    # -------------------- VIEW SUBMISSION --------------------
    elif payload_type == "view_submission":
        view = payload.get("view", {})
        callback_id = view.get("callback_id")

        if callback_id != "edit_modal_submit":
            return JSONResponse({"ok": True})

        state_values = view.get("state", {}).get("values", {})
        reply_text = _extract_input_text(state_values)
        try:
            meta = json.loads(view.get("private_metadata", "{}"))
        except Exception:
            meta = {}

        conv_id = meta.get("conv_id") or meta.get("conversation_id")
        if not reply_text:
            return JSONResponse({
                "response_action": "errors",
                "errors": {"reply_input_ai": "Please enter a reply."}
            })
        if not conv_id:
            return JSONResponse({
                "response_action": "errors",
                "errors": {"reply_input_ai": "Missing conversation ID."}
            })

        background_tasks.add_task(_background_send_to_hostaway, meta, reply_text)
        return JSONResponse({"response_action": "clear"})

    return JSONResponse({"ok": True})
