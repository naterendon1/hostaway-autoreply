# file: src/slack_interactions.py
import os
import json
import uuid
import logging
from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from src.slack_client import (
    client as slack_client,  # Slack WebClient
    build_edit_modal,
    open_edit_modal,
    send_hostaway_reply,
)
from src.ai_engine import generate_reply_with_tone, improve_message_with_ai

router = APIRouter()
slack_interactions_bp = router  # alias if main.py imports this

SLACK_SIGNING_SECRET = os.getenv("SLACK_SIGNING_SECRET", "")


@router.post("/interactivity")
async def handle_slack_interaction(request: Request):
    """
    Slack interactivity endpoint.
    - block_actions (buttons): ACK 200 and use Web API (views.update/push)
    - view_submission (Send in modal): respond with response_action (clear/errors)
    """
    try:
        form_data = await request.form()
        payload = json.loads(form_data.get("payload", "{}"))
    except Exception as e:
        logging.error(f"[Slack] Invalid payload: {e}")
        return JSONResponse({"error": "invalid_payload"}, status_code=400)

    # Modal submission first (Send inside modal)
    if payload.get("type") == "view_submission":
        return await _handle_modal_submit(payload)

    action_id = _get_action_id(payload)
    logging.info(f"[Slack] Action received: {action_id}")

    try:
        if action_id == "open_edit_modal":
            return await _open_edit_modal(payload)
        if action_id in ("send", "send_reply", "send_guest_portal"):
            return await _send_reply(payload, action_id)
        if "rewrite_" in action_id or action_id in ("adjust_tone_friendlier", "adjust_tone_formal"):
            return await _adjust_tone(payload, action_id)
        if action_id == "improve_with_ai":
            return await _improve_with_ai(payload)

        logging.warning(f"[Slack] Unhandled action: {action_id}")
        return JSONResponse({"ok": True})
    except Exception as e:
        logging.error(f"[Slack] Error handling action {action_id}: {e}")
        return JSONResponse({"error": "processing_error"}, status_code=500)


# ---------------- Helpers ----------------

def _get_action_id(payload: dict) -> str:
    """Return action_id for block_actions; empty otherwise."""
    try:
        actions = payload.get("actions", [])
        if actions and isinstance(actions, list):
            return actions[0].get("action_id", "") or ""
    except Exception:
        pass
    return ""


def _extract_input_text(state_values: dict) -> str:
    """Return the first 'value' found in modal input elements."""
    try:
        for block in state_values.values():
            for val in block.values():
                if isinstance(val, dict) and "value" in val:
                    return val["value"]
    except Exception:
        pass
    return ""


# ---------------- Actions ----------------

async def _open_edit_modal(payload: dict):
    """Open the edit modal from a message button."""
    trigger_id = payload.get("trigger_id")
    action = (payload.get("actions") or [{}])[0]
    try:
        data = json.loads(action.get("value", "{}") or "{}")
    except Exception:
        data = {}

    try:
        open_edit_modal(trigger_id, data)
        return JSONResponse({"ok": True})
    except Exception as e:
        logging.error(f"[Slack] Failed to open edit modal: {e}")
        return JSONResponse({"error": "modal_open_failed"}, status_code=500)


async def _improve_with_ai(payload: dict):
    """
    Improve text and update the current modal via views.update.
    WHY: block_actions can't be updated with response_action; must use Web API.
    Also rotate input IDs so Slack re-renders initial_value instead of preserving old entry.
    """
    try:
        # Extract current text from the modal state
        values = payload.get("view", {}).get("state", {}).get("values", {})
        current_text = _extract_input_text(values)

        # Parse action value (may contain a stale draft_text; we prefer current_text)
        action = (payload.get("actions") or [{}])[0]
        try:
            action_data = json.loads(action.get("value", "{}") or "{}")
        except Exception:
            action_data = {}

        # Improve wording
        improved_text = improve_message_with_ai(
            current_text or action_data.get("draft_text", ""),
            {}
        ) or "I refined your message slightly for clarity and friendliness!"

        # Carry pruned metadata from private_metadata
        try:
            meta = json.loads(payload["view"].get("private_metadata", "{}") or "{}")
        except Exception:
            meta = {}

        # Rotate input IDs to force Slack to refresh the input content
        ids_suffix = "_" + uuid.uuid4().hex[:6]
        clean_modal = build_edit_modal({
            "meta": meta,
            "guest_name": meta.get("guest_name", "Guest"),
            "guest_message": meta.get("guest_message", ""),
            "draft_text": improved_text,
            "ids_suffix": ids_suffix,
        })

        # Use Web API to update the visible view (include hash to avoid races)
        view_id = payload["view"]["id"]
        view_hash = payload["view"].get("hash")  # optional but recommended
        if view_hash:
            slack_client.views_update(view_id=view_id, hash=view_hash, view=clean_modal)
        else:
            slack_client.views_update(view_id=view_id, view=clean_modal)

        # Ack
        return JSONResponse({"ok": True})
    except Exception as e:
        logging.error(f"[Slack] Improve with AI failed: {e}")
        return JSONResponse({"error": "improve_failed"}, status_code=500)


async def _adjust_tone(payload: dict, action_id: str):
    """Adjust tone from a message button (posts preview to channel)."""
    try:
        action = (payload.get("actions") or [{}])[0]
        try:
            data = json.loads(action.get("value", "{}") or "{}")
        except Exception:
            data = {}

        tone = (
            "friendly" if "friendly" in action_id else
            "formal" if "formal" in action_id else
            "professional"
        )
        reply_text = data.get("reply_text") or data.get("reply") or ""
        guest_message = data.get("guest_message", "")
        improved_text = generate_reply_with_tone(guest_message, tone, base_reply=reply_text)

        slack_client.chat_postMessage(
            channel=os.getenv("SLACK_CHANNEL"),
            text=f"✨ *Tone adjusted to {tone.title()}:*\n{improved_text}",
        )
        return JSONResponse({"ok": True})
    except Exception as e:
        logging.error(f"[Slack] Tone adjustment failed: {e}")
        return JSONResponse({"error": "tone_failed"}, status_code=500)


async def _send_reply(payload: dict, action_id: str):
    """Send reply (from a message-level button, not modal submit)."""
    try:
        action = (payload.get("actions") or [{}])[0]
        try:
            data = json.loads(action.get("value", "{}") or "{}")
        except Exception:
            data = {}

        conv_id = data.get("conv_id") or data.get("conversation_id")
        reply_text = data.get("reply_text") or data.get("reply") or ""
        if not conv_id or not reply_text:
            raise ValueError("Missing conversation ID or message")

        send_hostaway_reply(conversation_id=conv_id, message=reply_text)
        slack_client.chat_postMessage(
            channel=os.getenv("SLACK_CHANNEL"),
            text=f"✅ Message sent to guest: \n>{reply_text}",
        )
        return JSONResponse({"ok": True})
    except Exception as e:
        logging.error(f"[Slack] Send reply failed: {e}")
        return JSONResponse({"error": "send_failed"}, status_code=500)


# ---------------- Modal Submit ----------------

async def _handle_modal_submit(payload: dict):
    """User pressed 'Send' inside modal → send to Hostaway and clear modal."""
    try:
        values = payload.get("view", {}).get("state", {}).get("values", {})
        reply_text = _extract_input_text(values)
        meta = json.loads(payload["view"].get("private_metadata", "{}") or "{}")
        conv_id = meta.get("conv_id")

        if conv_id and reply_text:
            ok = send_hostaway_reply(conv_id, reply_text)
            if not ok:
                # Keep modal open with inline field error
                return JSONResponse({
                    "response_action": "errors",
                    "errors": {"reply_input": "Failed to send to Hostaway. Try again."}
                })
        # Close modal on success (or if empty)
        return JSONResponse({"response_action": "clear"})
    except Exception as e:
        logging.error(f"[Slack] Modal submission failed: {e}")
        return JSONResponse({"error": "modal_submit_failed"}, status_code=500)
