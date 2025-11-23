# file: src/slack_interactions.py
import os
import json
import logging
from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from src.slack_client import (
    client as slack_client,
    build_edit_modal,
    handle_tone_rewrite,
    handle_improve_with_ai,
    open_edit_modal,
    send_hostaway_reply,
)
from src.ai_engine import generate_reply_with_tone, improve_message_with_ai

router = APIRouter()
slack_interactions_bp = router

SLACK_SIGNING_SECRET = os.getenv("SLACK_SIGNING_SECRET", "")


# ---------------- Slack Event Router ----------------
@router.post("/interactivity")
async def handle_slack_interaction(request: Request):
    """Handles interactive Slack actions and modals."""
    try:
        form_data = await request.form()
        payload = json.loads(form_data.get("payload", "{}"))
    except Exception as e:
        logging.error(f"[Slack] Invalid payload: {e}")
        return JSONResponse({"error": "invalid_payload"}, status_code=400)

    action_id = _get_action_id(payload)
    logging.info(f"[Slack] Action received: {action_id}")

    try:
        if action_id == "open_edit_modal":
            return await _open_edit_modal(payload)
        elif action_id in ("send", "send_reply", "send_guest_portal"):
            return await _send_reply(payload, action_id)
        elif "rewrite_" in action_id or action_id in ("adjust_tone_friendlier", "adjust_tone_formal"):
            return await _adjust_tone(payload, action_id)
        elif action_id == "improve_with_ai":
            return await _improve_with_ai(payload)

        # Modal form submission
        if payload.get("type") == "view_submission":
            return await _handle_modal_submit(payload)

        logging.warning(f"[Slack] Unhandled action: {action_id}")
        return JSONResponse({"ok": True})

    except Exception as e:
        logging.error(f"[Slack] Error handling action {action_id}: {e}")
        return JSONResponse({"error": "processing_error"}, status_code=500)


# ---------------- Helpers ----------------
def _get_action_id(payload: dict) -> str:
    """Extracts action_id safely from any Slack payload type."""
    try:
        actions = payload.get("actions", [])
        if actions and isinstance(actions, list):
            return actions[0].get("action_id", "")
        elif payload.get("type") == "view_submission":
            return payload.get("callback_id", "")
    except Exception:
        pass
    return ""


# ---------------- Open Edit Modal ----------------
async def _open_edit_modal(payload: dict):
    """Opens an edit modal to modify AI's suggested reply."""
    trigger_id = payload.get("trigger_id")
    action = payload.get("actions", [{}])[0]
    data = json.loads(action.get("value", "{}"))

    try:
        open_edit_modal(trigger_id, data)
        return JSONResponse({"ok": True})
    except Exception as e:
        logging.error(f"[Slack] Failed to open edit modal: {e}")
        return JSONResponse({"error": "modal_open_failed"}, status_code=500)


# ---------------- Improve with AI ----------------
async def _improve_with_ai(payload: dict):
    """Rewrite current modal text to be clear, friendly, and easy to understand."""
    try:
        # Extract the current text from the modal state
        user_input = payload.get("view", {}).get("state", {}).get("values", {})
        current_text = _extract_input_text(user_input)

        # Call your AI to improve wording
        action = payload.get("actions", [{}])[0]
        data = json.loads(action.get("value", "{}")) if action else {}
        improved_text = improve_message_with_ai(current_text, data) or (
            "I refined your message slightly for clarity and friendliness!"
        )

        # Build a CLEAN modal — do NOT reuse Slack's incoming view as-is
        try:
            meta = json.loads(payload["view"].get("private_metadata", "{}"))
        except Exception:
            meta = {}

        clean_modal = build_edit_modal({
            "meta": meta,
            "guest_name": meta.get("guest_name", "Guest"),
            "guest_message": meta.get("guest_message", ""),
            "draft_text": improved_text,
        })

        # Only send allowed fields with views.update
        slack_client.views_update(
            view_id=payload["view"]["id"],
            hash=payload["view"].get("hash"),
            view=clean_modal,
        )
        return JSONResponse({"ok": True})
    except Exception as e:
        logging.error(f"[Slack] Improve with AI failed: {e}")
        return JSONResponse({"error": "improve_failed"}, status_code=500)

async def _handle_modal_submit(payload: dict):
    """User pressed 'Send' in the modal — post to Hostaway and close."""
    try:
        view_state = payload.get("view", {}).get("state", {}).get("values", {})
        reply_text = _extract_input_text(view_state)
        meta = json.loads(payload["view"].get("private_metadata", "{}"))
        conv_id = meta.get("conv_id")

        if conv_id and reply_text:
            ok = send_hostaway_reply(conv_id, reply_text)
            if not ok:
                logging.error("[Slack] Hostaway send failed; keeping modal open.")
                return JSONResponse({
                    "response_action": "errors",
                    "errors": {"reply_input": "Failed to send to Hostaway. Try again."}
                })
        # Clear modal on success
        return JSONResponse({"response_action": "clear"})
    except Exception as e:
        logging.error(f"[Slack] Modal submission failed: {e}")
        return JSONResponse({"error": "modal_submit_failed"}, status_code=500)


# ---------------- Adjust Tone ----------------
async def _adjust_tone(payload: dict, action_id: str):
    """Adjusts AI tone to more formal or friendlier."""
    try:
        action = payload.get("actions", [{}])[0]
        data = json.loads(action.get("value", "{}"))
        tone = (
            "friendly" if "friendly" in action_id else
            "formal" if "formal" in action_id else
            "professional"
        )

        reply_text = data.get("reply_text", "") or data.get("reply", "")
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


# ---------------- Send Hostaway Reply ----------------
async def _send_reply(payload: dict, action_id: str):
    """Sends reply to Hostaway and confirms to Slack."""
    try:
        action = payload.get("actions", [{}])[0]
        data = json.loads(action.get("value", "{}"))
        conv_id = data.get("conv_id") or data.get("conversation_id")
        reply_text = data.get("reply_text", "") or data.get("reply", "")

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
    """Handles final modal form submission (user presses 'Send')."""
    try:
        view_state = payload.get("view", {}).get("state", {}).get("values", {})
        reply_text = _extract_input_text(view_state)
        meta = json.loads(payload["view"].get("private_metadata", "{}"))
        conv_id = meta.get("conv_id")

        if conv_id and reply_text:
            send_hostaway_reply(conv_id, reply_text)
            slack_client.chat_postMessage(
                channel=os.getenv("SLACK_CHANNEL"),
                text=f"✅ Edited reply sent to guest:\n>{reply_text}",
            )
        return JSONResponse({"response_action": "clear"})
    except Exception as e:
        logging.error(f"[Slack] Modal submission failed: {e}")
        return JSONResponse({"error": "modal_submit_failed"}, status_code=500)


# ---------------- Internal: Extract Input ----------------
def _extract_input_text(state_values: dict) -> str:
    """Extracts text input from modal state safely."""
    try:
        for block in state_values.values():
            for val in block.values():
                if "value" in val:
                    return val["value"]
    except Exception:
        pass
    return ""
