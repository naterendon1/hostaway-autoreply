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
from openai import OpenAI
import threading

from fastapi import APIRouter, Request, Header, HTTPException
from fastapi.responses import JSONResponse

from src.slack_client import (
    client as slack_client,
    open_edit_modal,
    send_hostaway_reply,
)
from src.ai_engine import generate_reply_with_tone, improve_message_with_ai

openai_client = OpenAI(api_key=os.getenv("OPENAI_API_KEY")) if os.getenv("OPENAI_API_KEY") else None

def clean_ai_reply(text: str, guest_msg: str) -> str:
    """Clean up AI-generated reply"""
    text = text.strip()

    # Remove greeting if it starts with one
    greetings = ["hi", "hello", "hey", "dear"]
    first_word = text.split()[0].lower().rstrip(",!") if text.split() else ""
    if first_word in greetings:
        if "." in text:
            text = ".".join(text.split(".")[1:]).strip()
        elif "\n" in text:
            text = "\n".join(text.split("\n")[1:]).strip()

    # Remove sign-offs
    sign_offs = ["best", "regards", "sincerely", "thanks", "thank you", "cheers"]
    lines = text.split("\n")
    if len(lines) > 1:
        last_line = lines[-1].lower()
        if any(s in last_line for s in sign_offs):
            lines = lines[:-1]
            text = "\n".join(lines).strip()

    return text


def sanitize_ai_reply(text: str, guest_msg: str) -> str:
    """Additional sanitization of AI reply"""
    # Remove excessive line breaks
    while "\n\n\n" in text:
        text = text.replace("\n\n\n", "\n\n")

    # Remove emojis
    emoji_chars = ["😊", "👍", "🏠", "🎉", "✨", "💯", "🙏", "❤️", "⭐"]
    for emoji in emoji_chars:
        text = text.replace(emoji, "")

    return text.strip()


def _background_improve_and_update(
    view_id: str,
    hash_value: Optional[str],
    meta: dict,
    edited_text: str,
    coach_prompt_text: Optional[str],
    guest_name: str,
    guest_msg: str,
):
    """Background thread to improve text with OpenAI and update modal"""

    logging.info(f"[Background] Starting improvement for conversationId: {meta.get('conv_id') or meta.get('conversationId')}")

    improved = edited_text
    error_message = None

    if not openai_client:
        error_message = "OpenAI key not configured; showing your original text."
        logging.warning("[Background] OpenAI client not configured")
    else:
        # NEW SYSTEM PROMPT - More flexible and instruction-following
        sys = """You are an expert assistant helping vacation rental hosts craft perfect guest replies.

Your job is to improve the draft reply based on the user's coaching instructions (if provided).

If coaching instructions are provided, follow them EXACTLY. The host knows what they want.

Common requests might include:
- Changing the tone (more formal, casual, friendly, professional, apologetic, etc.)
- Adding or removing specific information
- Making it shorter or more detailed
- Completely rewriting with different approach
- Fixing factual errors or wrong assumptions

IMPORTANT RULES:
1. Read the ENTIRE guest message - don't introduce topics they didn't ask about
2. If guest mentions trash, accessibility, parking, check-in/out, or codes, focus on that
3. Only include dining/local recommendations if guest explicitly asks
4. Keep the meaning, improve tone and brevity
5. NO greetings, NO sign-offs, NO emojis
6. Style: concise, casual, easy to understand
7. Preserve important details like dates, prices, check-in times, policies unless asked to change them"""

        # Build user prompt with coaching instructions prominently featured
        if coach_prompt_text and coach_prompt_text.strip():
            user = f"""Guest message:
{guest_msg}

Current draft reply:
{edited_text}

HOST'S IMPROVEMENT INSTRUCTIONS:
{coach_prompt_text.strip()}

Please rewrite the reply following the host's instructions exactly. Return ONLY the rewritten reply."""
        else:
            user = f"""Guest message:
{guest_msg}

Current draft reply (to improve, not to lengthen):
{edited_text}

Please improve this reply to be clearer, more concise, and more natural. Return ONLY the improved reply."""

        try:
            logging.info("[Background] Calling OpenAI API...")
            if coach_prompt_text:
                logging.info(f"[Background] With instructions: {coach_prompt_text[:100]}...")

            response = openai_client.chat.completions.create(
                model="gpt-4o-mini",
                messages=[
                    {"role": "system", "content": sys},
                    {"role": "user", "content": user},
                ],
                temperature=0.7,
                max_tokens=500
            )

            improved = (response.choices[0].message.content or "").strip()
            logging.info(f"[Background] OpenAI response received (length={len(improved)})")

            improved = clean_ai_reply(improved, guest_msg)
            improved = sanitize_ai_reply(improved, guest_msg)

            logging.info(f"[Background] Cleaned improved text: {improved[:100]}...")

        except Exception as e:
            logging.error(f"[Background] OpenAI error: {e}", exc_info=True)
            error_message = f"Error improving with AI: {str(e)}"

    # Create new metadata with previous draft saved
    new_meta = {
        **meta,
        "previous_draft": edited_text,
        "improving": False,
        "coach_prompt": coach_prompt_text or ""
    }

    logging.info(f"[Background] New meta conversationId: {new_meta.get('conv_id') or new_meta.get('conversationId')}")

    # Build header
    header_text = (
        f"*✉️ Message from {guest_name}*\n"
        f"🏡 *Property:* {new_meta.get('property_name', 'Unknown')}\n"
        f"📅 *Dates:* {new_meta.get('check_in', 'N/A')} → {new_meta.get('check_out', 'N/A')}\n"
        f"👥 *Guests:* {new_meta.get('guest_count', '?')} | *Status:* {new_meta.get('status', 'N/A')}\n"
    )

    # Build modal blocks with improved text
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
                "initial_value": improved,
            },
        },
        {
            "type": "input",
            "block_id": "coach_prompt_block",
            "optional": True,
            "label": {"type": "plain_text", "text": "Improve with AI Instructions (optional)"},
            "element": {
                "type": "plain_text_input",
                "action_id": "coach_prompt",
                "multiline": True,
                "placeholder": {
                    "type": "plain_text",
                    "text": "Examples: 'Make more apologetic' • 'Too long, 2 sentences max' • 'Add check-in is at 3pm' • 'More professional tone' • 'Completely rewrite to..'"
                },
                **({"initial_value": coach_prompt_text} if coach_prompt_text else {})
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
                        "conv_id": new_meta.get("conv_id") or new_meta.get("conversationId"),
                        "guest_message": guest_msg[:800]
                    }),
                },
            ],
        },
    ]

    # Add Undo button if we have previous draft
    if new_meta.get("previous_draft"):
        blocks.append({
            "type": "actions",
            "block_id": "undo_actions",
            "elements": [
                {
                    "type": "button",
                    "text": {"type": "plain_text", "text": "↩️ Undo AI"},
                    "action_id": "undo_ai",
                    "value": json.dumps({"previous_draft": new_meta["previous_draft"]}),
                }
            ]
        })

    # Add error message if any
    if error_message:
        blocks = [
            {"type": "section", "text": {"type": "mrkdwn", "text": f":warning: *{error_message}*"}}
        ] + blocks

    # Build final view
    final_view = {
        "type": "modal",
        "callback_id": "edit_modal_submit",
        "title": {"type": "plain_text", "text": "AI Improved Reply", "emoji": True},
        "submit": {"type": "plain_text", "text": "Send", "emoji": True},
        "close": {"type": "plain_text", "text": "Cancel", "emoji": True},
        "private_metadata": pack_private_meta(new_meta),
        "blocks": blocks,
    }

    # Update the modal
    if not slack_client:
        logging.error("[Background] Slack client not initialized")
        return

    try:
        logging.info("[Background] Updating modal with improved text...")
        try:
            # Try with hash first for optimistic locking
            if hash_value:
                resp = slack_client.views_update(view_id=view_id, hash=hash_value, view=final_view)
            else:
                resp = slack_client.views_update(view_id=view_id, view=final_view)

            if not resp.get("ok"):
                logging.error(f"[Background] views_update failed: {resp.get('error')}")

        except Exception as e:
            # Hash conflict or other error - retry without hash
            error_msg = str(e)
            if "hash_conflict" in error_msg:
                logging.warning("[Background] Hash conflict detected, retrying without hash...")
            else:
                logging.error(f"[Background] views_update error: {e}")

            # Retry without hash to force update
            slack_client.views_update(view_id=view_id, view=final_view)
            logging.info("[Background] Modal updated successfully after retry")

    except Exception as e:
        logging.error(f"[Background] Failed to update modal after all retries: {e}", exc_info=True)

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
    "conv_id", "conversationId", "listing_id", "guest_id", "guest_name", "guest_message",
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
    logging.info(f"[_open_edit_modal] conv_id in data: {data.get('conv_id')}")
    logging.info(f"[_open_edit_modal] conversationId in data: {data.get('conversationId')}")
    logging.info(f"[_open_edit_modal] meta in data: {data.get('meta')}")

    # Add fingerprint for deduplication
    container = payload.get("container", {}) or {}
    channel_id = container.get("channel_id") or (payload.get("channel") or {}).get("id")
    message_ts = container.get("message_ts") or (payload.get("message") or {}).get("ts")

    data["fingerprint"] = f"{channel_id}|{message_ts}|{data.get('conv_id') or data.get('conversationId','')}|{uuid.uuid4()}"
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
    import threading

    try:
        # Extract current data
        view = payload.get("view", {})
        view_id = view["id"]
        hash_value = view.get("hash")
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

        # Get metadata
        meta_str = view.get("private_metadata", "{}")
        logging.info(f"[Slack] Raw private_metadata: {meta_str[:200]}...")

        try:
            meta = json.loads(meta_str) if meta_str else {}
        except json.JSONDecodeError as e:
            logging.error(f"[Slack] Failed to parse private_metadata: {e}")
            meta = {}

        # Extract conversationId (using conv_id key)
        conversation_id = meta.get("conv_id") or meta.get("conversationId")
        guest_name = meta.get("guest_name", "Guest")
        guest_message = meta.get("guest_message", "")

        logging.info(f"[Slack] Extracted conversationId: {conversation_id}")
        logging.info(f"[Slack] Full metadata keys: {list(meta.keys())}")
        logging.info(f"[Slack] Full metadata: {meta}")

        if not conversation_id:
            logging.error("[Slack] No conversationId found in metadata!")
            logging.error(f"[Slack] conv_id value: {meta.get('conv_id')}, conversationId value: {meta.get('conversationId')}")

            # Try to show error to user
            try:
                error_view = {
                    "type": "modal",
                    "callback_id": "edit_modal_submit",
                    "title": {"type": "plain_text", "text": "Error"},
                    "close": {"type": "plain_text", "text": "Close"},
                    "blocks": [
                        {
                            "type": "section",
                            "text": {"type": "mrkdwn", "text": "❌ *Error: No conversation ID found*\n\nThis message cannot be improved because the conversation ID is missing. This might be a test message or draft that hasn't been linked to a Hostaway conversation yet."}
                        }
                    ],
                }
                slack_client.views_update(view_id=view_id, hash=hash_value, view=error_view)
            except Exception as e:
                logging.error(f"[Slack] Failed to show error modal: {e}")

            return JSONResponse({"ok": True})

        # Update modal to show "Improving..." immediately
        improving_view = {
            "type": "modal",
            "callback_id": "edit_modal_submit",
            "title": {"type": "plain_text", "text": "Improving...", "emoji": True},
            "submit": {"type": "plain_text", "text": "Send", "emoji": True},
            "close": {"type": "plain_text", "text": "Cancel", "emoji": True},
            "private_metadata": meta_str,
            "blocks": [
                {
                    "type": "section",
                    "text": {"type": "mrkdwn", "text": "✨ *Improving with AI...* This may take a few seconds."}
                }
            ],
        }

        try:
            slack_client.views_update(view_id=view_id, hash=hash_value, view=improving_view)
        except Exception as e:
            logging.error(f"[Slack] Error showing improving state: {e}")

        # Start background thread (pass None for hash since we just updated the modal)
        logging.info("[Slack] Starting background improvement thread...")
        threading.Thread(
            target=_background_improve_and_update,
            args=(view_id, None, meta, current_text, coach_prompt, guest_name, guest_message),
            daemon=True
        ).start()

        # Return immediate acknowledgment
        return JSONResponse({"ok": True})

    except Exception as e:
        logging.error(f"[Slack] Error in improve_with_ai: {e}", exc_info=True)
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
                "label": {"type": "plain_text", "text": "Improve with AI Instructions (optional)"},
                "element": {
                    "type": "plain_text_input",
                    "action_id": "coach_prompt",
                    "multiline": True,
                    "placeholder": {
                        "type": "plain_text",
                        "text": "Examples: 'Make more apologetic' • 'Too long, 2 sentences max' • 'Add check-in is at 3pm' • 'More professional tone' • 'Completely rewrite to..'"
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
                        "value": json.dumps({"conv_id": meta.get("conv_id") or meta.get("conversationId"), "guest_message": guest_message}),
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

        conversation_id = data.get("conv_id") or data.get("conversationId")
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
        conversation_id = meta.get("conv_id") or meta.get("conversationId")

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
