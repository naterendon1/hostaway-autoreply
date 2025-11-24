# file: src/slack_interactions.py
"""
Enhanced Slack interactions with:
- Async processing for "Improve with AI"
- Slack signature verification
- Retry detection
- Coaching prompt
- Undo AI feature
- Better error handling
"""
import os
import json
import logging
import hmac
import hashlib
import time
import uuid
from typing import Optional, Dict, Any

from fastapi import APIRouter, Request, Header, HTTPException
from fastapi.responses import JSONResponse

from src.slack_client import (
    client as slack_client,
    open_edit_modal,
    send_hostaway_reply,
)
from src.ai_engine import generate_reply_with_tone, improve_message_with_ai

router = APIRouter()
slack_interactions_bp = router

SLACK_SIGNING_SECRET = os.getenv("SLACK_SIGNING_SECRET", "")
MAX_PRIVATE_BYTES = 2800  # Slack limit is 3000, leave buffer


# -------------------- Security: Slack Signature Verify --------------------
def verify_slack_signature(
    request_body: str,
    slack_signature: Optional[str],
    slack_request_timestamp: Optional[str],
) -> bool:
    """Verify Slack request signature to prevent unauthorized access."""
    if not SLACK_SIGNING_SECRET:
        logging.warning("SLACK_SIGNING_SECRET not set - signature verification disabled (dev mode)")
        return True

    if not slack_request_timestamp or abs(time.time() - int(slack_request_timestamp)) > 60 * 5:
        logging.warning("Slack request timestamp too old or missing")
        return False

    if not slack_signature:
        logging.warning("Slack signature missing")
        return False

    base_string = f"v0:{slack_request_timestamp}:{request_body}".encode("utf-8")
    my_signature = "v0=" + hmac.new(
        SLACK_SIGNING_SECRET.encode("utf-8"),
        base_string,
        hashlib.sha256,
    ).hexdigest()

    is_valid = hmac.compare_digest(my_signature, slack_signature)
    if not is_valid:
        logging.warning("Slack signature verification failed")

    return is_valid


# -------------------- Private Metadata Packing --------------------
PRIVATE_META_KEYS = {
    "conversationId", "listing_id", "guest_id", "guest_name", "guest_message",
    "type", "status", "check_in", "check_out", "guest_count",
    "channel", "ts", "detected_intent", "channel_pretty", "property_address",
    "property_name", "guest_portal_url", "reservation_id", "sent_label",
    "checkbox_checked", "coach_prompt", "location", "fingerprint",
    "previous_draft", "draft_text"
}


def pack_private_meta(meta: Dict[str, Any]) -> str:
    """Pack metadata to fit Slack's 3000-byte limit."""
    # Include only essential keys
    thin = {k: meta.get(k) for k in PRIVATE_META_KEYS if k in meta}

    # Try full encoding
    s = json.dumps(thin, ensure_ascii=False)
    if len(s.encode("utf-8")) <= MAX_PRIVATE_BYTES:
        return s

    # Truncate large fields if needed
    for k in ("property_address", "guest_message", "draft_text"):
        if k in thin and isinstance(thin[k], str) and len(thin[k]) > 800:
            thin[k] = thin[k][:800]
            s = json.dumps(thin, ensure_ascii=False)
            if len(s.encode("utf-8")) <= MAX_PRIVATE_BYTES:
                break

    # Final fallback - hard truncate
    enc = s.encode("utf-8")
    if len(enc) > MAX_PRIVATE_BYTES:
        enc = enc[:MAX_PRIVATE_BYTES]
        try:
            s = enc.decode("utf-8", errors="ignore")
        except Exception:
            s = "{}"

    return s


# -------------------- Slack Event Router ----------------
@router.post("/interactivity")
async def handle_slack_interaction(
    request: Request,
    x_slack_signature: Optional[str] = Header(None, alias="X-Slack-Signature"),
    x_slack_request_timestamp: Optional[str] = Header(None, alias="X-Slack-Request-Timestamp"),
    x_slack_retry_num: Optional[str] = Header(None, alias="X-Slack-Retry-Num"),
    x_slack_retry_reason: Optional[str] = Header(None, alias="X-Slack-Retry-Reason"),
):
    """Handles interactive Slack actions and modals."""

    # Skip retries - already processed
    if x_slack_retry_num is not None:
        logging.info(f"[Slack] Skipping retry #{x_slack_retry_num} ({x_slack_retry_reason})")
        return JSONResponse({"ok": True})

    # Get raw body for signature verification
    raw_body_bytes = await request.body()
    raw_body = raw_body_bytes.decode("utf-8") if raw_body_bytes else ""

    # Verify signature
    if not verify_slack_signature(raw_body, x_slack_signature, x_slack_request_timestamp):
        raise HTTPException(status_code=401, detail="Invalid Slack signature or timestamp")

    # Parse payload
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
        elif action_id == "improve_with_ai":
            return await _improve_with_ai(payload)
        elif action_id == "undo_ai":
            return await _undo_ai(payload)

        # Modal form submission
        if payload.get("type") == "view_submission":
            return await _handle_modal_submit(payload)

        logging.warning(f"[Slack] Unhandled action: {action_id}")
        return JSONResponse({"ok": True})

    except Exception as e:
        logging.error(f"[Slack] Error handling action {action_id}: {e}", exc_info=True)
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
    
    # ADD DEBUGGING
    raw_value = action.get("value", "{}")
    logging.info(f"[_open_edit_modal] Raw button value: {raw_value[:500]}")
    
    data = json.loads(raw_value)
    
    # ADD MORE DEBUGGING
    logging.info(f"[_open_edit_modal] Parsed data keys: {list(data.keys())}")
    logging.info(f"[_open_edit_modal] conversationId in data: {data.get('conversationId')}")
    logging.info(f"[_open_edit_modal] meta in data: {data.get('meta')}")
    
    # Add fingerprint for deduplication
    container = payload.get("container", {}) or {}
    channel_id = container.get("channel_id") or (payload.get("channel") or {}).get("id")
    message_ts = container.get("message_ts") or (payload.get("message") or {}).get("ts")

    data["fingerprint"] = f"{channel_id}|{message_ts}|{data.get('conversationId','')}|{uuid.uuid4()}"
    data["channel"] = channel_id
    data["ts"] = message_ts

    try:
        open_edit_modal(trigger_id, data)
        return JSONResponse({"ok": True})
    except Exception as e:
        logging.error(f"[Slack] Failed to open edit modal: {e}")
        return JSONResponse({"error": "modal_open_failed"}, status_code=500)

# ---------------- Improve with AI ----------------
async def _improve_with_ai(payload: dict):
    """Handles 'Improve with AI' button — rewrites text in modal."""
    import asyncio

    async def update_modal_view():
        """Async function to update the modal after acknowledgment."""
        try:
            # Extract current data
            action = payload.get("actions", [{}])[0]
            data = json.loads(action.get("value", "{}"))
            view = payload.get("view", {})
            view_state = view.get("state", {}).get("values", {})

            # Get current text from input
            current_text = _extract_input_text(view_state)

            # Get coaching prompt if provided
            coach_prompt = ""
            cp_block = view_state.get("coach_prompt_block", {})
            if "coach_prompt" in cp_block and isinstance(cp_block["coach_prompt"], dict):
                coach_prompt = (cp_block["coach_prompt"].get("value") or "").strip()

            logging.info(f"[Slack] Improving text: {current_text[:50]}...")
            if coach_prompt:
                logging.info(f"[Slack] With coaching: {coach_prompt[:50]}...")

            # Get metadata for full context
            meta_str = view.get("private_metadata", "{}")
            logging.info(f"[Slack] Raw private_metadata: {meta_str[:200]}...")
            
            try:
                meta = json.loads(meta_str) if meta_str else {}
            except json.JSONDecodeError as e:
                logging.error(f"[Slack] Failed to parse private_metadata: {e}")
                meta = {}
            
            # Extract conversationId from metadata or button value
            conversation_id = meta.get("conversationId") or data.get("conversationId")
            
            logging.info(f"[Slack] Extracted conversationId: {conversation_id}")
            logging.info(f"[Slack] Meta keys available: {list(meta.keys())}")
            
            if not conversation_id:
                logging.error("[Slack] No conversationId found in metadata or button value!")
                return
            
            # Build improved context with all necessary info
            improved_context = {
                "conversationId": conversation_id,
                "guest_message": data.get("guest_message") or meta.get("guest_message", ""),
                "guest_name": meta.get("guest_name", "Guest"),
                "property_name": meta.get("property_name"),
                "check_in": meta.get("check_in"),
                "check_out": meta.get("check_out"),
            }
            
            if coach_prompt:
                improved_context["coach_prompt"] = coach_prompt

            # Improve with AI
            improved_text = improve_message_with_ai(current_text, improved_context)
            if not improved_text:
                improved_text = current_text  # Fallback

            logging.info(f"[Slack] Improved text: {improved_text[:50]}...")

            # Preserve ALL existing metadata and add new fields
            updated_meta = meta.copy()
            updated_meta["previous_draft"] = current_text
            updated_meta["coach_prompt"] = coach_prompt
            updated_meta["conversationId"] = conversation_id  # Explicitly ensure conversationId is set

            logging.info(f"[Slack] Updated meta conversationId: {updated_meta.get('conversationId')}")

            # Rebuild modal with improved text
            guest_name = updated_meta.get("guest_name", "Guest")

            # Build header
            header_text = (
                f"*✉️ Message from {guest_name}*\n"
                f"🏡 *Property:* {updated_meta.get('property_name', 'Unknown')}\n"
                f"📅 *Dates:* {updated_meta.get('check_in', 'N/A')} → {updated_meta.get('check_out', 'N/A')}\n"
                f"👥 *Guests:* {updated_meta.get('guest_count', '?')} | *Status:* {updated_meta.get('status', 'N/A')}\n"
            )

            # Build modal blocks
            blocks = [
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
                        "initial_value": improved_text,
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
                        },
                        **({"initial_value": coach_prompt} if coach_prompt else {})
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
                            "value": json.dumps({
                                "conversationId": conversation_id,
                                "guest_message": updated_meta.get("guest_message", "")[:800]
                            }),
                        },
                    ],
                },
            ]

            # Add Undo button if we have a previous draft
            if updated_meta.get("previous_draft"):
                blocks.append({
                    "type": "actions",
                    "block_id": "undo_actions",
                    "elements": [
                        {
                            "type": "button",
                            "text": {"type": "plain_text", "text": "↩️ Undo AI"},
                            "action_id": "undo_ai",
                            "value": json.dumps({"previous_draft": updated_meta["previous_draft"]}),
                        }
                    ]
                })

            # Pack metadata
            packed_meta = pack_private_meta(updated_meta)
            logging.info(f"[Slack] Packed metadata length: {len(packed_meta)} bytes")

            # Update view
            updated_view = {
                "type": "modal",
                "callback_id": "edit_modal_submit",
                "title": {"type": "plain_text", "text": "Edit AI Reply"},
                "submit": {"type": "plain_text", "text": "Send"},
                "close": {"type": "plain_text", "text": "Cancel"},
                "private_metadata": packed_meta,
                "blocks": blocks,
            }

            current_view = payload["view"]
            logging.info(f"[Slack] Updating modal view {current_view['id']} with conversationId: {conversation_id}")
            result = slack_client.views_update(
                view_id=current_view["id"],
                hash=current_view.get("hash", ""),
                view=updated_view
            )
            logging.info(f"[Slack] Modal view updated: {result.get('ok', False)}")

        except Exception as e:
            logging.error(f"[Slack] Failed to update modal view: {e}", exc_info=True)

    # Start background task
    asyncio.create_task(update_modal_view())

    # Return immediate acknowledgment
    return JSONResponse({"ok": True})

# ---------------- Undo AI ----------------
async def _undo_ai(payload: dict):
    """Restores previous draft before AI improvement."""
    try:
        view = payload.get("view", {})
        meta = json.loads(view.get("private_metadata", "{}"))

        previous_draft = meta.get("previous_draft", "")
        if not previous_draft:
            logging.warning("[Slack] Undo requested but no previous draft found")
            return JSONResponse({"ok": True})

        guest_name = meta.get("guest_name", "Guest")
        guest_message = meta.get("guest_message", "")

        # Build header
        header_text = (
            f"*✉️ Message from {guest_name}*\n"
            f"🏡 *Property:* {meta.get('property_name', 'Unknown')}*\n"
            f"📅 *Dates:* {meta.get('check_in', 'N/A')} → {meta.get('check_out', 'N/A')}\n"
            f"👥 *Guests:* {meta.get('guest_count', '?')} | *Status:* {meta.get('status', 'N/A')}*\n"
        )

        # Restore previous draft
        blocks = [
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
                    "initial_value": previous_draft,
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
                    "placeholder": {"type": "plain_text", "text": "Tell AI how to adjust"}
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
                        "value": json.dumps({"conversationId": meta.get("conversationId"), "guest_message": guest_message}),
                    },
                ],
            },
        ]

        # Clear previous draft from metadata
        meta.pop("previous_draft", None)

        updated_view = {
            "type": "modal",
            "callback_id": "edit_modal_submit",
            "title": {"type": "plain_text", "text": "Edit AI Reply"},
            "submit": {"type": "plain_text", "text": "Send"},
            "close": {"type": "plain_text", "text": "Cancel"},
            "private_metadata": pack_private_meta(meta),
            "blocks": blocks,
        }

        slack_client.views_update(
            view_id=view["id"],
            hash=view.get("hash", ""),
            view=updated_view
        )

        logging.info("[Slack] Undo successful - restored previous draft")
        return JSONResponse({"ok": True})

    except Exception as e:
        logging.error(f"[Slack] Undo failed: {e}", exc_info=True)
        return JSONResponse({"error": "undo_failed"}, status_code=500)


# ---------------- Send Hostaway Reply ----------------
async def _send_reply(payload: dict, action_id: str):
    """Sends reply to Hostaway and confirms to Slack."""
    try:
        action = payload.get("actions", [{}])[0]
        raw_value = action.get("value", "{}")
        
        logging.info(f"[_send_reply] action_id: {action_id}")
        logging.info(f"[_send_reply] Raw action value: {raw_value[:500]}")

        data = json.loads(raw_value)

        logging.info(f"[_send_reply] Parsed data keys: {list(data.keys())}")
        
        conversation_id = data.get("conversationId")
        reply_text = data.get("reply_text", "") or data.get("reply", "")

        logging.info(f"[_send_reply] conversationId: {conversation_id}")
        logging.info(f"[_send_reply] reply_text length: {len(reply_text) if reply_text else 0}")


        if not conversation_id or not reply_text:
            logging.error(f"[_send_reply] Missing data - conversationId: {conversation_id}, reply_text: {bool(reply_text)}")
            raise ValueError("Missing conversation ID or message")

        send_hostaway_reply(conversation_id=conversation_id, message=reply_text)

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
        logging.info("[Slack] Processing modal submission...")

        # Extract view state and text
        view_state = payload.get("view", {}).get("state", {}).get("values", {})
        reply_text = _extract_input_text(view_state)

        # Extract metadata
        meta_str = payload.get("view", {}).get("private_metadata", "{}")
        meta = json.loads(meta_str) if meta_str else {}
        conversation_id = meta.get("conversationId")

        logging.info(f"[Slack] Modal submission - conversationId: {conversation_id}, reply_text length: {len(reply_text) if reply_text else 0}")

        # Validate
        if not conversation_id:
            logging.error("[Slack] No conversationId in modal submission")
            return JSONResponse({
                "response_action": "errors",
                "errors": {"reply_input": "Unable to send - missing conversation ID"}
            })

        if not reply_text or not reply_text.strip():
            logging.error("[Slack] No reply text in modal submission")
            return JSONResponse({
                "response_action": "errors",
                "errors": {"reply_input": "Please enter a message to send"}
            })

        # Send to Hostaway
        logging.info(f"[Slack] Sending message to Hostaway conversation {conversation_id}...")
        success = send_hostaway_reply(conversation_id, reply_text.strip())

        if success:
            # Post confirmation to Slack
            slack_client.chat_postMessage(
                channel=os.getenv("SLACK_CHANNEL"),
                text=f"✅ Edited reply sent to guest:\n>{reply_text}",
            )
            logging.info(f"[Slack] Modal message sent successfully to conversation {conversation_id}")

            # Clear modal
            return JSONResponse({"response_action": "clear"})
        else:
            logging.error(f"[Slack] Failed to send message to Hostaway for conversation {conversation_id}")
            return JSONResponse({
                "response_action": "errors",
                "errors": {"reply_input": "Failed to send message to Hostaway. Please try again."}
            })

    except json.JSONDecodeError as e:
        logging.error(f"[Slack] Failed to parse modal metadata: {e}")
        return JSONResponse({
            "response_action": "errors",
            "errors": {"reply_input": "Internal error - invalid modal data"}
        })
    except Exception as e:
        logging.error(f"[Slack] Modal submission failed: {e}", exc_info=True)
        return JSONResponse({
            "response_action": "errors",
            "errors": {"reply_input": f"Error: {str(e)}"}
        })


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
