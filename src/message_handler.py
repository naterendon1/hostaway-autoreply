# file: src/message_handler.py
"""
Slack Interactivity Handler
---------------------------
Handles Slack button clicks, modal submissions, and tone/improvement actions.
"""

import os
import json
import logging
from fastapi import APIRouter, Request, HTTPException
from fastapi.responses import JSONResponse

from src.slack_client import (
    open_edit_modal,
    handle_tone_rewrite,
    handle_improve_with_ai,
)
from src.api_client import send_reply_to_hostaway
from src.ai_engine import improve_message_with_ai

router = APIRouter()


# -----------------------------------------------------
# SLACK INTERACTIVITY ENDPOINT
# -----------------------------------------------------
@router.post("/interactivity")
async def handle_slack_interactivity(request: Request):
    """Handles button clicks, tone changes, and modal submissions from Slack."""
    try:
        form_data = await request.form()
        payload_str = form_data.get("payload")
        if not payload_str:
            raise HTTPException(status_code=400, detail="Missing payload")

        payload = json.loads(payload_str)
        logging.info(f"[SLACK] Event received: {payload.get('type')}")

        event_type = payload.get("type")

        # 🟩 BUTTON CLICK HANDLERS
        if event_type == "block_actions":
            actions = payload.get("actions", [])
            trigger_id = payload.get("trigger_id")
            user_id = payload.get("user", {}).get("id")

            for action in actions:
                action_id = action.get("action_id")
                value = action.get("value")

                # Send Message to Hostaway
                if action_id == "send":
                    data = json.loads(value)
                    conv_id = data.get("conv_id")
                    reply = data.get("reply")
                    if not conv_id or not reply:
                        continue
                    success = send_reply_to_hostaway(conv_id, reply)
                    logging.info(f"[SLACK] Sent reply to Hostaway (success={success})")
                    return JSONResponse({"text": "✅ Message sent to guest."})

                # Open Edit Modal
                elif action_id == "open_edit_modal":
                    open_edit_modal(trigger_id, json.loads(value))
                    return JSONResponse({"text": "Opening edit modal..."})

                # Tone Rewrite Buttons
                elif action_id in ("rewrite_friendly", "rewrite_formal", "rewrite_professional"):
                    handle_tone_rewrite(action_id, value, trigger_id)
                    return JSONResponse({"text": f"Applied tone rewrite: {action_id}"})

                # Send Guest Portal
                elif action_id == "send_guest_portal":
                    data = json.loads(value)
                    portal_url = data.get("guest_portal_url")
                    conv_id = data.get("conv_id")
                    if not portal_url:
                        return JSONResponse({"text": "⚠️ No guest portal available."})
                    success = send_reply_to_hostaway(conv_id, f"Here’s your guest portal link: {portal_url}")
                    return JSONResponse({"text": "✅ Guest portal sent."})

                # Improve with AI (from within modal)
                elif action_id == "improve_with_ai":
                    handle_improve_with_ai(value, trigger_id)
                    return JSONResponse({"text": "✨ Improved message displayed."})

        # 🟦 MODAL SUBMISSIONS
        elif event_type == "view_submission":
            view = payload.get("view", {})
            state_values = view.get("state", {}).get("values", {})
            conv_id = None
            guest_message = ""
            reply_text = ""

            # Extract the edited message text
            for block_id, block in state_values.items():
                if "reply_text" in block:
                    reply_text = block["reply_text"]["value"]

            # Extract contextual metadata
            private_metadata = view.get("private_metadata")
            if private_metadata:
                try:
                    meta = json.loads(private_metadata)
                    conv_id = meta.get("conv_id")
                    guest_message = meta.get("guest_message", "")
                except Exception:
                    pass

            # If user pressed “Save”, send the edited message back to Hostaway
            if reply_text and conv_id:
                success = send_reply_to_hostaway(conv_id, reply_text)
                logging.info(f"[SLACK] Edited message sent (success={success})")
                return JSONResponse({"response_action": "clear"})

            return JSONResponse({"text": "Message saved."})

        # 🟨 OTHER INTERACTIONS
        else:
            logging.info(f"[SLACK] Ignored event type: {event_type}")
            return JSONResponse({"text": "Ignored."})

    except Exception as e:
        logging.error(f"[SLACK] Interactivity error: {e}")
        raise HTTPException(status_code=500, detail=str(e))


# -----------------------------------------------------
# IMPROVE MESSAGE ENDPOINT (Direct API)
# -----------------------------------------------------
@router.post("/improve")
async def improve_message(request: Request):
    """Endpoint for directly improving a message using AI (outside Slack UI)."""
    try:
        body = await request.json()
        text = body.get("text", "")
        meta = body.get("meta", {})
        if not text:
            raise HTTPException(status_code=400, detail="Missing text")
        improved = improve_message_with_ai(text, None, meta)
        return {"improved_text": improved}
    except Exception as e:
        logging.error(f"[AI Improve] Error: {e}")
        raise HTTPException(status_code=500, detail=str(e))
