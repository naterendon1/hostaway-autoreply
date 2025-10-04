# file: src/slack_interactions.py
import os
import json
import logging
from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from src.api_client import send_hostaway_reply
from src.slack_client import slack_client, _build_guest_context, _build_header_block
from src.ai_engine import generate_reply_with_tone, improve_message_with_ai

router = APIRouter()

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

    if action_id == "open_edit_modal":
        return await _open_edit_modal(payload)
    elif action_id in ("send", "send_guest_portal"):
        return await _send_reply(payload, action_id)
    elif action_id in ("adjust_tone_friendlier", "adjust_tone_formal"):
        return await _adjust_tone(payload, action_id)
    elif action_id == "improve_with_ai":
        return await _improve_with_ai(payload)

    logging.warning(f"[Slack] Unhandled action: {action_id}")
    return JSONResponse({"ok": True})


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

    guest_name = data.get("guest_name", "Guest")
    guest_message = data.get("guest_message", "")
    draft_text = data.get("draft_text", "")
    meta = data

    try:
        view = {
            "type": "modal",
            "callback_id": "edit_modal",
            "title": {"type": "plain_text", "text": "Edit Reply"},
            "submit": {"type": "plain_text", "text": "Send"},
            "close": {"type": "plain_text", "text": "Cancel"},
            "blocks": [
                _build_guest_context(meta),
                _build_header_block(meta),
                {
                    "type": "section",
                    "text": {"type": "mrkdwn", "text": f"*Guest Message:*\n{guest_message}"}
                },
                {
                    "type": "input",
                    "block_id": "reply_input",
                    "element": {
                        "type": "plain_text_input",
                        "action_id": "reply_text",
                        "multiline": True,
                        "initial_value": draft_text,
                    },
                    "label": {"type": "plain_text", "text": "Your reply"},
                },
                {
                    "type": "actions",
                    "block_id": "modal_actions",
                    "elements": [
                        {
                            "type": "button",
                            "text": {"type": "plain_text", "text": "Improve with AI"},
                            "action_id": "improve_with_ai",
                            "value": json.dumps(meta),
                        }
                    ],
                },
            ],
        }

        slack_client.views_open(trigger_id=trigger_id, view=view)
        return JSONResponse({"ok": True})

    except Exception as e:
        logging.error(f"[Slack] Failed to open edit modal: {e}")
        return JSONResponse({"error": "modal_open_failed"}, status_code=500)


# ---------------- Improve with AI ----------------
async def _improve_with_ai(payload: dict):
    """Handles 'Improve with AI' button — rewrites text in modal."""
    try:
        action = payload.get("actions", [{}])[0]
        data = json.loads(action.get("value", "{}"))
        user_input = payload.get("view", {}).get("state", {}).get("values", {})
        current_text = _extract_input_text(user_input)

        improved_text = improve_message_with_ai(current_text, data)
        if not improved_text:
            improved_text = "I refined your message slightly for clarity and friendliness!"

        # Update modal with new AI-improved text
        slack_client.views_update(
            view_id=payload["view"]["id"],
            hash=payload["view"]["hash"],
            view={
                **payload["view"],
                "blocks": [
                    b if b.get("block_id") != "reply_input" else {
                        **b,
                        "element": {
                            **b["element"],
                            "initial_value": improved_text
                        }
                    }
                    for b in payload["view"]["blocks"]
                ]
            },
        )
        return JSONResponse({"ok": True})

    except Exception as e:
        logging.error(f"[Slack] Improve with AI failed: {e}")
        return JSONResponse({"error": "improve_failed"}, status_code=500)


# ---------------- Adjust Tone ----------------
async def _adjust_tone(payload: dict, action_id: str):
    """Adjusts AI tone to more formal or friendlier."""
    try:
        action = payload.get("actions", [{}])[0]
        data = json.loads(action.get("value", "{}"))

        tone = "formal" if "formal" in action_id else "friendlier"
        reply_text = data.get("reply_text", "")
        guest_message = data.get("guest_message", "")

        improved_text = generate_reply_with_tone(guest_message, tone, base_reply=reply_text)

        # Send ephemeral Slack message (or replace block text)
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

        conv_id = data.get("conv_id")
        reply_text = data.get("reply_text", "")

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
