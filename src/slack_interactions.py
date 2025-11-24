# file: src/slack_interactions.py
"""
Enhanced Slack interactions with:
- Proper modal handling (edit, improve with AI, send)
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

from fastapi import APIRouter, Request, Header, HTTPException, BackgroundTasks
from fastapi.responses import JSONResponse
from openai import OpenAI

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

# OpenAI client for AI improvements
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
openai_client = OpenAI(api_key=OPENAI_API_KEY) if OPENAI_API_KEY else None


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
    "conv_id", "conversation_id", "listing_id", "guest_id", "guest_name",
    "guest_message", "type", "status", "check_in", "check_out", "guest_count",
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


# -------------------- Helper: Extract Input Text --------------------
def _extract_input_text(state_values: dict, block_ids: list = None) -> str:
    """
    Extracts text input from modal state safely.
    Tries multiple block_ids in order: reply_input_ai, reply_input, or any input block.
    """
    if block_ids is None:
        block_ids = ["reply_input_ai", "reply_input"]
    
    # Try specified block IDs first
    for block_id in block_ids:
        block = state_values.get(block_id, {})
        for action_id, val in block.items():
            if isinstance(val, dict) and "value" in val:
                text = val.get("value", "").strip()
                if text:
                    return text
    
    # Fallback: search all blocks for any plain_text_input with a value
    for block in state_values.values():
        for val in block.values():
            if isinstance(val, dict) and val.get("type") == "plain_text_input":
                text = val.get("value", "").strip()
                if text:
                    return text
    
    return ""


# -------------------- Modal Building Blocks --------------------
def get_modal_blocks(
    guest_name: str,
    guest_msg: str,
    draft_text: str = "",
    checkbox_checked: bool = False,
    coach_prompt_initial: Optional[str] = None,
    input_block_id: str = "reply_input_ai",
    input_action_id: str = "reply_ai",
) -> list:
    """Build the blocks for the edit modal."""
    
    reply_block = {
        "type": "input",
        "block_id": input_block_id,
        "label": {"type": "plain_text", "text": "Edit your reply:", "emoji": True},
        "element": {
            "type": "plain_text_input",
            "action_id": input_action_id,
            "multiline": True,
        },
    }
    if draft_text:
        reply_block["element"]["initial_value"] = draft_text

    coach_block = {
        "type": "input",
        "block_id": "coach_prompt_block",
        "optional": True,
        "label": {"type": "plain_text", "text": "Coach the AI (optional)", "emoji": True},
        "element": {
            "type": "plain_text_input",
            "action_id": "coach_prompt",
            "multiline": True,
            "placeholder": {
                "type": "plain_text",
                "text": "Tell the AI how to adjust (e.g., 'make it shorter', 'add parking info')",
            },
        },
    }
    if coach_prompt_initial:
        coach_block["element"]["initial_value"] = coach_prompt_initial[:3000]

    return [
        {
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": f"*Guest:* {guest_name}\n*Message:* {guest_msg[:500]}"
            },
        },
        {"type": "divider"},
        reply_block,
        coach_block,
        {
            "type": "actions",
            "block_id": "improve_ai_block",
            "elements": [
                {
                    "type": "button",
                    "action_id": "improve_with_ai",
                    "text": {"type": "plain_text", "text": "✨ Improve with AI", "emoji": True},
                    "style": "primary",
                }
            ],
        },
    ]


def add_undo_button(blocks: list, has_previous_draft: bool) -> list:
    """Add undo button if there's a previous draft."""
    if has_previous_draft:
        blocks.append({
            "type": "actions",
            "block_id": "undo_block",
            "elements": [
                {
                    "type": "button",
                    "text": {"type": "plain_text", "text": "↩️ Undo AI", "emoji": True},
                    "action_id": "undo_ai",
                }
            ],
        })
    return blocks


# -------------------- Background: Improve with AI --------------------
def _background_improve_and_update(
    view_id: str,
    meta: dict,
    edited_text: str,
    coach_prompt_text: Optional[str],
    guest_name: str,
    guest_msg: str,
):
    """Background task to improve text with AI and update modal view."""
    logging.info(f"[AI] Starting improvement for view {view_id}")
    
    improved = edited_text
    error_message = None

    if not openai_client:
        error_message = "OpenAI not configured"
        logging.warning("[AI] OpenAI client not available")
    else:
        try:
            # Build AI prompt
            system_prompt = (
                "You improve guest message replies for a vacation rental host. "
                "Keep the meaning and intent, but improve tone and brevity. "
                "No greetings, no sign-offs, no emojis. "
                "Style: concise, casual, helpful."
            )
            
            user_prompt = f"""Guest message:
{guest_msg}

Current draft reply:
{edited_text}

Coach instructions: {coach_prompt_text or '(none)'}

Rewrite the reply to be better while following any coach instructions. Return ONLY the improved reply."""

            response = openai_client.chat.completions.create(
                model="gpt-4o-mini",
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_prompt},
                ],
                max_tokens=500,
                temperature=0.7,
            )
            
            improved = response.choices[0].message.content.strip()
            logging.info(f"[AI] Successfully improved text (length: {len(improved)})")
            
        except Exception as e:
            logging.error(f"[AI] Error improving with AI: {e}")
            error_message = f"AI improvement failed: {str(e)}"

    # Update metadata with previous draft for undo functionality
    new_meta = {
        **meta,
        "previous_draft": edited_text,
        "coach_prompt": coach_prompt_text or "",
    }

    # Build updated modal blocks
    blocks = get_modal_blocks(
        guest_name=guest_name,
        guest_msg=guest_msg,
        draft_text=improved,
        checkbox_checked=new_meta.get("checkbox_checked", False),
        coach_prompt_initial=coach_prompt_text or "",
    )
    
    # Add undo button since we now have a previous draft
    blocks = add_undo_button(blocks, has_previous_draft=True)
    
    # Add error message if present
    if error_message:
        blocks.insert(0, {
            "type": "section",
            "text": {"type": "mrkdwn", "text": f"⚠️ *{error_message}*"}
        })

    updated_view = {
        "type": "modal",
        "callback_id": "edit_modal_submit",
        "title": {"type": "plain_text", "text": "AI Improved Reply", "emoji": True},
        "submit": {"type": "plain_text", "text": "Send", "emoji": True},
        "close": {"type": "plain_text", "text": "Cancel", "emoji": True},
        "private_metadata": pack_private_meta(new_meta),
        "blocks": blocks,
    }

    # Update the view
    if not slack_client:
        logging.error("[Slack] Slack client not available")
        return
    
    try:
        result = slack_client.views_update(
            view_id=view_id,
            view=updated_view
        )
        if result.get("ok"):
            logging.info(f"[Slack] Successfully updated view {view_id}")
        else:
            logging.error(f"[Slack] Failed to update view: {result.get('error')}")
    except Exception as e:
        logging.error(f"[Slack] Exception updating view: {e}")


# -------------------- Background: Send to Hostaway --------------------
def _background_send_to_hostaway(
    meta: dict,
    reply_text: str,
):
    """Background task to send reply to Hostaway and update Slack."""
    conv_id = meta.get("conv_id") or meta.get("conversation_id")
    channel = meta.get("channel")
    ts = meta.get("ts")
    
    logging.info(f"[Hostaway] Sending reply to conversation {conv_id}")
    
    try:
        # Send to Hostaway
        success = send_hostaway_reply(conv_id, reply_text)
        
        if success:
            logging.info(f"[Hostaway] Successfully sent reply to {conv_id}")
            
            # Post confirmation to Slack thread
            if slack_client and channel and ts:
                try:
                    slack_client.chat_postMessage(
                        channel=channel,
                        thread_ts=ts,
                        text=f"✅ Reply sent to guest:\n>{reply_text[:200]}{'...' if len(reply_text) > 200 else ''}"
                    )
                except Exception as e:
                    logging.error(f"[Slack] Failed to post confirmation: {e}")
        else:
            logging.error(f"[Hostaway] Failed to send reply to {conv_id}")
            
            # Post error to Slack thread
            if slack_client and channel and ts:
                try:
                    slack_client.chat_postMessage(
                        channel=channel,
                        thread_ts=ts,
                        text="❌ Failed to send reply to Hostaway. Please try again."
                    )
                except Exception as e:
                    logging.error(f"[Slack] Failed to post error: {e}")
                    
    except Exception as e:
        logging.error(f"[Hostaway] Exception sending reply: {e}")


# -------------------- Main Interactivity Endpoint --------------------
@router.post("/interactivity")
async def handle_slack_interaction(
    request: Request,
    background_tasks: BackgroundTasks,
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

    payload_type = payload.get("type")
    logging.info(f"[Slack] Received interaction type: {payload_type}")

    try:
        # ==================== BLOCK ACTIONS ====================
        if payload_type == "block_actions":
            actions = payload.get("actions", [])
            if not actions:
                return JSONResponse({"ok": True})
            
            action = actions[0]
            action_id = action.get("action_id")
            trigger_id = payload.get("trigger_id")
            
            logging.info(f"[Slack] Block action: {action_id}")

            # Get metadata from button value
            try:
                meta = json.loads(action.get("value", "{}"))
            except:
                meta = {}
            
            # Normalize conversation ID
            if "conversation_id" in meta and "conv_id" not in meta:
                meta["conv_id"] = meta["conversation_id"]
            
            # Get channel and timestamp from container
            container = payload.get("container", {}) or {}
            channel_id = container.get("channel_id") or (payload.get("channel") or {}).get("id")
            message_ts = container.get("message_ts") or (payload.get("message") or {}).get("ts")
            
            if channel_id:
                meta["channel"] = channel_id
            if message_ts:
                meta["ts"] = message_ts
            
            # Generate fingerprint if missing
            if not meta.get("fingerprint"):
                meta["fingerprint"] = f"{channel_id}|{message_ts}|{meta.get('conv_id', '')}|{uuid.uuid4()}"

            # ---------------- SEND REPLY ----------------
            if action_id in ("send", "send_reply"):
                logging.info("[Slack] Handling send_reply action")
                
                # Get reply text from metadata
                reply_text = meta.get("reply_text") or meta.get("draft_text") or meta.get("ai_suggestion", "")
                conv_id = meta.get("conv_id")
                
                if not reply_text or not conv_id:
                    logging.error(f"[Slack] Missing reply_text or conv_id: {meta}")
                    return JSONResponse({"ok": True})
                
                # Send in background
                background_tasks.add_task(_background_send_to_hostaway, meta, reply_text)
                
                # Post immediate acknowledgment to thread
                if slack_client and channel_id and message_ts:
                    try:
                        slack_client.chat_postMessage(
                            channel=channel_id,
                            thread_ts=message_ts,
                            text="⏳ Sending reply to guest..."
                        )
                    except Exception as e:
                        logging.error(f"[Slack] Failed to post ack: {e}")
                
                return JSONResponse({"ok": True})

            # ---------------- OPEN EDIT MODAL ----------------
            elif action_id in ("edit", "open_edit_modal"):
                logging.info("[Slack] Opening edit modal")
                
                guest_name = meta.get("guest_name", "Guest")
                guest_msg = meta.get("guest_message", "")
                draft_text = meta.get("draft_text") or meta.get("ai_suggestion", "")
                
                blocks = get_modal_blocks(
                    guest_name=guest_name,
                    guest_msg=guest_msg,
                    draft_text=draft_text,
                    coach_prompt_initial=meta.get("coach_prompt", ""),
                )
                
                modal = {
                    "type": "modal",
                    "callback_id": "edit_modal_submit",
                    "title": {"type": "plain_text", "text": "Edit AI Reply", "emoji": True},
                    "submit": {"type": "plain_text", "text": "Send", "emoji": True},
                    "close": {"type": "plain_text", "text": "Cancel", "emoji": True},
                    "private_metadata": pack_private_meta(meta),
                    "blocks": blocks,
                }
                
                if slack_client:
                    try:
                        slack_client.views_open(trigger_id=trigger_id, view=modal)
                        logging.info("[Slack] Edit modal opened successfully")
                    except Exception as e:
                        logging.error(f"[Slack] Failed to open modal: {e}")
                
                return JSONResponse({"ok": True})

            # ---------------- IMPROVE WITH AI ----------------
            elif action_id == "improve_with_ai":
                logging.info("[Slack] Handling improve_with_ai action")
                
                view = payload.get("view", {})
                view_id = view.get("id")
                
                if not view_id:
                    logging.error("[Slack] No view_id for improve_with_ai")
                    return JSONResponse({"ok": True})
                
                # Extract current text from modal state
                state_values = view.get("state", {}).get("values", {})
                edited_text = _extract_input_text(state_values)
                
                # Extract coach prompt
                coach_prompt_block = state_values.get("coach_prompt_block", {})
                coach_prompt_value = ""
                if "coach_prompt" in coach_prompt_block:
                    coach_prompt_value = coach_prompt_block["coach_prompt"].get("value", "")
                
                # Get metadata
                try:
                    meta = json.loads(view.get("private_metadata", "{}"))
                except:
                    meta = {}
                
                guest_name = meta.get("guest_name", "Guest")
                guest_msg = meta.get("guest_message", "")
                
                # Show loading state immediately
                loading_blocks = [
                    {
                        "type": "section",
                        "text": {"type": "mrkdwn", "text": "⏳ *Improving with AI...*"}
                    }
                ] + get_modal_blocks(
                    guest_name=guest_name,
                    guest_msg=guest_msg,
                    draft_text=edited_text,
                    coach_prompt_initial=coach_prompt_value,
                )
                
                loading_view = {
                    "type": "modal",
                    "callback_id": "edit_modal_submit",
                    "title": {"type": "plain_text", "text": "Improving...", "emoji": True},
                    "close": {"type": "plain_text", "text": "Cancel", "emoji": True},
                    "private_metadata": pack_private_meta(meta),
                    "blocks": loading_blocks,
                }
                
                # Start background improvement
                background_tasks.add_task(
                    _background_improve_and_update,
                    view_id,
                    meta,
                    edited_text,
                    coach_prompt_value,
                    guest_name,
                    guest_msg,
                )
                
                # Return loading view immediately
                return JSONResponse({
                    "response_action": "update",
                    "view": loading_view
                })

            # ---------------- UNDO AI ----------------
            elif action_id == "undo_ai":
                logging.info("[Slack] Handling undo_ai action")
                
                view = payload.get("view", {})
                try:
                    meta = json.loads(view.get("private_metadata", "{}"))
                except:
                    meta = {}
                
                previous_draft = meta.get("previous_draft", "")
                if not previous_draft:
                    logging.warning("[Slack] No previous draft to restore")
                    return JSONResponse({"ok": True})
                
                guest_name = meta.get("guest_name", "Guest")
                guest_msg = meta.get("guest_message", "")
                
                # Restore previous draft
                blocks = get_modal_blocks(
                    guest_name=guest_name,
                    guest_msg=guest_msg,
                    draft_text=previous_draft,
                    coach_prompt_initial=meta.get("coach_prompt", ""),
                )
                
                # Remove previous_draft from meta
                meta.pop("previous_draft", None)
                
                restored_view = {
                    "type": "modal",
                    "callback_id": "edit_modal_submit",
                    "title": {"type": "plain_text", "text": "Edit AI Reply", "emoji": True},
                    "submit": {"type": "plain_text", "text": "Send", "emoji": True},
                    "close": {"type": "plain_text", "text": "Cancel", "emoji": True},
                    "private_metadata": pack_private_meta(meta),
                    "blocks": blocks,
                }
                
                return JSONResponse({
                    "response_action": "update",
                    "view": restored_view
                })

            # ---------------- SEND GUEST PORTAL ----------------
            elif action_id == "send_guest_portal":
                logging.info("[Slack] Handling send_guest_portal action")
                
                conv_id = meta.get("conv_id")
                portal_url = meta.get("guest_portal_url")
                
                if not portal_url or not conv_id:
                    if slack_client and channel_id and message_ts:
                        slack_client.chat_postMessage(
                            channel=channel_id,
                            thread_ts=message_ts,
                            text="⚠️ Guest portal URL not available"
                        )
                    return JSONResponse({"ok": True})
                
                # Send portal link
                try:
                    success = send_hostaway_reply(
                        conv_id,
                        f"Here's your guest portal link: {portal_url}"
                    )
                    
                    if success and slack_client and channel_id and message_ts:
                        slack_client.chat_postMessage(
                            channel=channel_id,
                            thread_ts=message_ts,
                            text="✅ Guest portal link sent"
                        )
                except Exception as e:
                    logging.error(f"[Hostaway] Error sending portal: {e}")
                
                return JSONResponse({"ok": True})

        # ==================== VIEW SUBMISSION ====================
        elif payload_type == "view_submission":
            logging.info("[Slack] Handling view_submission")
            
            view = payload.get("view", {})
            callback_id = view.get("callback_id")
            
            if callback_id != "edit_modal_submit":
                return JSONResponse({"ok": True})
            
            # Extract reply text from modal
            state_values = view.get("state", {}).get("values", {})
            reply_text = _extract_input_text(state_values)
            
            # Get metadata
            try:
                meta = json.loads(view.get("private_metadata", "{}"))
            except:
                meta = {}
            
            conv_id = meta.get("conv_id") or meta.get("conversation_id")
            
            logging.info(f"[Slack] Modal submission - conv_id: {conv_id}, reply length: {len(reply_text)}")
            
            # Validate
            if not reply_text:
                return JSONResponse({
                    "response_action": "errors",
                    "errors": {"reply_input_ai": "Please enter a reply"}
                })
            
            if not conv_id:
                return JSONResponse({
                    "response_action": "errors",
                    "errors": {"reply_input_ai": "Missing conversation ID"}
                })
            
            # Send reply in background
            background_tasks.add_task(_background_send_to_hostaway, meta, reply_text)
            
            # Clear modal
            return JSONResponse({"response_action": "clear"})

        return JSONResponse({"ok": True})

    except Exception as e:
        logging.error(f"[Slack] Error handling interaction: {e}", exc_info=True)
        return JSONResponse({"error": "processing_error"}, status_code=500)
