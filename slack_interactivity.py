# file: slack_interactivity.py
import os
import logging
import json
import hmac
import hashlib
import time
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Request, Header, HTTPException, BackgroundTasks
from fastapi.responses import JSONResponse
from slack_sdk import WebClient
from slack_sdk.errors import SlackApiError
from openai import OpenAI

from utils import (
    send_reply_to_hostaway,
    store_learning_example,
    clean_ai_reply,
)

logging.basicConfig(level=logging.INFO)
router = APIRouter()

# --- Slack / OpenAI clients
SLACK_BOT_TOKEN = os.getenv("SLACK_BOT_TOKEN", "")
SLACK_SIGNING_SECRET = os.getenv("SLACK_SIGNING_SECRET", "")
slack_client = WebClient(token=SLACK_BOT_TOKEN) if SLACK_BOT_TOKEN else None

OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
openai_client = OpenAI(api_key=OPENAI_API_KEY) if OPENAI_API_KEY else None

if not SLACK_BOT_TOKEN:
    logging.warning("SLACK_BOT_TOKEN is not set; Slack operations will fail in production.")


# -------------------- Security: Slack Signature Verify --------------------
def verify_slack_signature(
    request_body: str,
    slack_signature: Optional[str],
    slack_request_timestamp: Optional[str]
) -> bool:
    """
    Verify Slack request signature. If no signing secret is configured, allow (dev mode).
    """
    if not SLACK_SIGNING_SECRET:
        return True  # dev-friendly for local/dev
    if not slack_request_timestamp or abs(time.time() - int(slack_request_timestamp)) > 60 * 5:
        return False
    if not slack_signature:
        return False

    basestring = f"v0:{slack_request_timestamp}:{request_body}".encode("utf-8")
    my_signature = "v0=" + hmac.new(
        SLACK_SIGNING_SECRET.encode("utf-8"),
        basestring,
        hashlib.sha256
    ).hexdigest()
    return hmac.compare_digest(my_signature, slack_signature)


# -------------------- Small helpers --------------------
# Hostaway "booked/confirmed" statuses (raw)
CONFIRMED_STATUSES = {"new", "modified"}

def is_booking_confirmed(status: Optional[str]) -> bool:
    """
    Accepts either raw Hostaway status ('new', 'modified', ...) or pretty
    versions ('New', 'Modified', ...). We lower/trim before comparing.
    """
    return (status or "").strip().lower() in CONFIRMED_STATUSES


def _post_thread_note(channel: Optional[str], ts: Optional[str], text: str) -> None:
    """Post a small note into the message thread (best-effort)."""
    if not slack_client or not channel or not ts:
        return
    try:
        slack_client.chat_postMessage(channel=channel, thread_ts=ts, text=text)
    except Exception as e:
        logging.error(f"Thread note failed: {e}")


def update_slack_message_with_sent_reply(
    slack_bot_token: Optional[str],
    channel: Optional[str],
    ts: Optional[str],
    guest_name: str,
    guest_msg: str,
    sent_reply: str,
    communication_type: Optional[str],
    check_in: str,
    check_out: str,
    guest_count: str | int,
    status: str,
    detected_intent: str,
    sent_label: str = "message sent",
    channel_pretty: Optional[str] = None,
    property_address: Optional[str] = None,
    saved_for_learning: bool = False,
) -> None:
    """Replace the original Slack message blocks with a 'Sent' confirmation layout."""
    if not slack_bot_token or not channel or not ts or not slack_client:
        logging.warning("Missing token/channel/ts for Slack chat_update; skipping header update.")
        return

    _client = WebClient(token=slack_bot_token)
    channel_label = channel_pretty or (communication_type.capitalize() if communication_type else "Channel")
    addr = property_address or "Address unavailable"
    ctx_elems = [{"type": "mrkdwn", "text": f"*Intent:* `{detected_intent}`"}]
    if saved_for_learning:
        ctx_elems.append({"type": "mrkdwn", "text": ":bookmark_tabs: Saved for AI learning"})

    blocks = [
        {"type": "section", "text": {"type": "mrkdwn", "text": (
            f"*{channel_label} message* from *{guest_name}*\n"
            f"Property: *{addr}*\n"
            f"Dates: *{check_in} → {check_out}*\n"
            f"Guests: *{guest_count}* | Status: *{status}*"
        )}},
        {"type": "section", "text": {"type": "mrkdwn", "text": f"> {guest_msg}"}},
        {"type": "section", "text": {"type": "mrkdwn", "text": f"*Sent Reply:*\n>{sent_reply}"}},
        {"type": "context", "elements": ctx_elems},
        {"type": "section", "text": {"type": "mrkdwn", "text": f":white_check_mark: *{sent_label}*"}},
    ]

    try:
        _client.chat_update(channel=channel, ts=ts, blocks=blocks, text="Reply sent to guest!")
    except SlackApiError as e:
        logging.error(f"❌ Failed to update Slack message with sent reply: {getattr(e, 'response', {}).data if hasattr(e, 'response') else e}")


# --------- Modal building blocks ----------
def get_modal_blocks(
    guest_name: str,
    guest_msg: str,
    action_id: str,
    draft_text: str = "",
    checkbox_checked: bool = False,
    input_block_id: str = "reply_input",
    input_action_id: str = "reply",
) -> List[Dict[str, Any]]:
    # 1) Guest message context (read-only)
    header_section: Dict[str, Any] = {
        "type": "section",
        "block_id": "guest_message_section",
        "text": {"type": "mrkdwn", "text": f"*Guest*: {guest_name}\n*Message*: {guest_msg}"},
    }

    # 2) Optional instruction for AI (host prompt to steer the rewrite)
    ai_prompt_block: Dict[str, Any] = {
        "type": "input",
        "block_id": "ai_prompt_block",
        "optional": True,
        "label": {"type": "plain_text", "text": "Instruction for AI (optional)", "emoji": True},
        "element": {
            "type": "plain_text_input",
            "action_id": "ai_prompt",
            "multiline": True,
            "placeholder": {"type": "plain_text", "text": "e.g., don’t offer a cleaning crew—propose pest control and next steps"},
        },
    }

    # 3) The editable reply box
    reply_block: Dict[str, Any] = {
        "type": "input",
        "block_id": input_block_id,
        "label": {"type": "plain_text", "text": "Your reply:" if action_id == "write_own" else "Edit below:", "emoji": True},
        "element": {
            "type": "plain_text_input",
            "action_id": input_action_id,
            "multiline": True,
        }
    }
    if draft_text:
        reply_block["element"]["initial_value"] = draft_text

    # 4) Improve with AI button row
    improve_row: Dict[str, Any] = {
        "type": "actions",
        "block_id": "improve_ai_block",
        "elements": [
            {"type": "button", "action_id": "improve_with_ai", "text": {"type": "plain_text", "text": "Improve with AI", "emoji": True}}
        ],
    }

    # 5) Optional learning checkbox
    learning_checkbox_option = {
        "text": {"type": "plain_text", "text": "Save this answer for next time", "emoji": True},
        "value": "save"
    }
    learning_checkbox: Dict[str, Any] = {
        "type": "input",
        "block_id": "save_answer_block",
        "element": {
            "type": "checkboxes",
            "action_id": "save_answer",
            "options": [learning_checkbox_option]
        },
        "label": {"type": "plain_text", "text": "Learning", "emoji": True},
        "optional": True
    }
    if checkbox_checked:
        learning_checkbox["element"]["initial_options"] = [learning_checkbox_option]

    return [header_section, ai_prompt_block, reply_block, improve_row, learning_checkbox]


def add_undo_button(blocks: List[Dict[str, Any]], meta: Dict[str, Any]) -> List[Dict[str, Any]]:
    if meta.get("previous_draft"):
        blocks.append({
            "type": "actions",
            "elements": [
                {"type": "button", "text": {"type": "plain_text", "text": "Undo AI", "emoji": True},
                 "value": json.dumps(meta), "action_id": "undo_ai"}
            ]
        })
    return blocks


# ---------------- Background: improve + final views.update (with host instruction) ----------------
def _background_improve_and_update(
    view_id: str,
    hash_value: Optional[str],
    meta: dict,
    edited_text: str,
    guest_name: str,
    guest_msg: str,
    custom_prompt: Optional[str] = None,  # <-- NEW
):
    improved = edited_text
    error_message = None

    # Build prompt
    host_instruction = (custom_prompt or "").strip()
    base_instructions = (
        "Rewrite the reply WITHOUT changing the core facts.\n"
        "Voice: concise, casual, informal, easy to understand. Use contractions.\n"
        "No greeting, no sign-off, no emojis, no corporate filler.\n"
        "Keep or tighten length. Avoid repeating what the guest said.\n\n"
    )
    user_payload = (
        f"Guest message:\n{guest_msg}\n\n"
        f"Current draft reply:\n{edited_text}\n\n"
    )
    if host_instruction:
        user_payload += f"Host instruction (override/steer the reply):\n{host_instruction}\n\n"
    user_payload += "Return ONLY the final message to the guest."

    if not openai_client:
        error_message = "OpenAI key not configured; showing your original text."
    else:
        try:
            response = openai_client.chat.completions.create(
                model="gpt-4o-mini",
                messages=[
                    {"role": "system", "content": "You write messages for a vacation-rental host. Follow style and safety. Never include greetings/sign-offs."},
                    {"role": "user", "content": base_instructions + user_payload}
                ]
            )
            improved = clean_ai_reply((response.choices[0].message.content or "").strip())
        except Exception as e:
            logging.error(f"OpenAI error in background 'improve_with_ai': {e}")
            error_message = f"Error improving with AI: {str(e)}"

    new_meta = {**meta, "previous_draft": edited_text, "improving": False}
    blocks = get_modal_blocks(
        guest_name,
        guest_msg,
        action_id="edit",
        draft_text=improved,
        checkbox_checked=new_meta.get("checkbox_checked", False),
        input_block_id="reply_input_ai",   # Force Slack to re-fill initial_value
        input_action_id="reply_ai",
    )
    blocks = add_undo_button(blocks, new_meta)
    if error_message:
        blocks = [{"type": "section", "text": {"type": "mrkdwn", "text": f":warning: *{error_message}*"}}] + blocks

    final_view = {
        "type": "modal",
        "title": {"type": "plain_text", "text": "AI Improved Reply", "emoji": True},
        "submit": {"type": "plain_text", "text": "Send", "emoji": True},
        "close": {"type": "plain_text", "text": "Cancel", "emoji": True},
        "private_metadata": json.dumps(new_meta),
        "blocks": blocks
    }

    if not slack_client:
        return

    try:
        resp = slack_client.views_update(view_id=view_id, hash=hash_value, view=final_view)
        if not resp.get("ok"):
            err = resp.get("error")
            logging.error(f"views_update (final) ok=false: {err}")
            if err in {"hash_conflict", "not_found", "view_not_found"}:
                resp2 = slack_client.views_update(view_id=view_id, view=final_view)
                if not resp2.get("ok"):
                    logging.error(f"views_update (final) retry-no-hash ok=false: {resp2.get('error')}")
    except Exception as e:
        logging.error(f"views_update (final) exception: {e}")
        try:
            resp2 = slack_client.views_update(view_id=view_id, view=final_view)
            if not resp2.get("ok"):
                logging.error(f"views_update (final) exception retry-no-hash ok=false: {resp2.get('error')}")
        except Exception as e2:
            logging.error(f"views_update (final) second exception: {e2}")


# ---------------- Background: send to Hostaway + update Slack ----------------
def _background_send_and_update(meta: dict, reply_text: str):
    try:
        ok = send_reply_to_hostaway(meta["conv_id"], reply_text, meta.get("type", "email"))
    except Exception as e:
        logging.error(f"Hostaway send error: {e}")
        ok = False

    channel = meta.get("channel") or os.getenv("SLACK_CHANNEL")
    ts = meta.get("ts")
    if not channel or not ts:
        logging.warning("Missing channel/ts for Slack chat_update; skipping header update.")
        return

    if ok:
        update_slack_message_with_sent_reply(
            slack_bot_token=SLACK_BOT_TOKEN,
            channel=channel,
            ts=ts,
            guest_name=meta.get("guest_name", "Guest"),
            guest_msg=meta.get("guest_message", ""),
            sent_reply=reply_text,
            communication_type=meta.get("type", "email"),
            check_in=meta.get("check_in", "N/A"),
            check_out=meta.get("check_out", "N/A"),
            guest_count=meta.get("guest_count", "N/A"),
            status=meta.get("status", "Unknown"),
            detected_intent=meta.get("detected_intent", "Unknown"),
            sent_label=meta.get("sent_label", "message sent"),
            channel_pretty=meta.get("channel_pretty"),
            property_address=meta.get("property_address"),
            saved_for_learning=bool(meta.get("saved_for_learning")),
        )
    else:
        if not slack_client:
            return
        try:
            slack_client.chat_update(
                channel=channel,
                ts=ts,
                blocks=[{"type": "section", "text": {"type": "mrkdwn", "text": ":x: *Failed to send reply.*"}}],
                text="Failed to send reply."
            )
        except Exception as e:
            logging.error(f"Slack chat_update error: {e}")


# ---------------------------- Events Endpoint ----------------------------
@router.post("/events")
async def slack_events(
    request: Request,
    x_slack_signature: Optional[str] = Header(None, alias="X-Slack-Signature"),
    x_slack_request_timestamp: Optional[str] = Header(None, alias="X-Slack-Request-Timestamp"),
):
    raw_body_bytes = await request.body()
    raw_body = raw_body_bytes.decode("utf-8") if raw_body_bytes else ""
    if not verify_slack_signature(raw_body, x_slack_signature, x_slack_request_timestamp):
        raise HTTPException(status_code=401, detail="Invalid Slack signature or timestamp.")
    payload = await request.json()
    if payload.get("type") == "url_verification":
        return JSONResponse({"challenge": payload.get("challenge")})
    return JSONResponse({"ok": True})


# ---------------------------- Interactivity Endpoint ----------------------------
@router.post("/actions")
async def slack_actions(
    request: Request,
    background_tasks: BackgroundTasks,
    x_slack_signature: Optional[str] = Header(None, alias="X-Slack-Signature"),
    x_slack_request_timestamp: Optional[str] = Header(None, alias="X-Slack-Request-Timestamp"),
    x_slack_retry_num: Optional[str] = Header(None, alias="X-Slack-Retry-Num"),
    x_slack_retry_reason: Optional[str] = Header(None, alias="X-Slack-Retry-Reason"),
):
    # Ignore Slack retries (we already processed the action)
    if x_slack_retry_num is not None:
        logging.info(f"Skipping retry #{x_slack_retry_num} ({x_slack_retry_reason}) for /slack/actions")
        return JSONResponse({"ok": True})

    raw_body_bytes = await request.body()
    raw_body = raw_body_bytes.decode("utf-8") if raw_body_bytes else ""
    if not verify_slack_signature(raw_body, x_slack_signature, x_slack_request_timestamp):
        raise HTTPException(status_code=401, detail="Invalid Slack signature or timestamp.")

    form = await request.form()
    payload_raw = form.get("payload")
    if not payload_raw:
        logging.error("Missing payload from Slack.")
        raise HTTPException(status_code=400, detail="Missing payload from Slack.")
    payload: Dict[str, Any] = json.loads(payload_raw)

    logging.info("🎯 /slack/actions hit")
    logging.info(f"Slack Interactivity Payload: {json.dumps(payload, indent=2)}")

    ptype = payload.get("type")

    # ---------- Block actions ----------
    if ptype == "block_actions":
        action = payload["actions"][0]
        action_id = action.get("action_id")
        trigger_id = payload.get("trigger_id")
        container = payload.get("container", {}) or {}
        channel_id = container.get("channel_id") or (payload.get("channel") or {}).get("id")
        message_ts = container.get("message_ts") or (payload.get("message") or {}).get("ts")

        def get_meta_from_action(_action: Dict[str, Any]) -> dict:
            try:
                return json.loads(_action.get("value") or "{}")
            except Exception:
                return {}

        # --- SEND ---
        if action_id == "send":
            meta = get_meta_from_action(action)
            # Ensure channel/ts are present for later update
            if channel_id and not meta.get("channel"):
                meta["channel"] = channel_id
            if message_ts and not meta.get("ts"):
                meta["ts"] = message_ts

            reply_text = meta.get("reply", meta.get("ai_suggestion", "")).strip()
            conv_id = meta.get("conv_id")
            if not reply_text or not conv_id:
                return JSONResponse({"text": "Missing reply or conversation ID."})

            # Optional: show "Sending…" modal if this came from a modal
            try:
                view_id = container.get("view_id") or (payload.get("container", {}) or {}).get("view_id")
                if view_id and slack_client:
                    slack_client.views_update(
                        view_id=view_id,
                        view={
                            "type": "modal",
                            "title": {"type": "plain_text", "text": "Sending...", "emoji": True},
                            "blocks": [{"type": "section", "text": {"type": "mrkdwn", "text": ":hourglass: Sending your message..."}}],
                            "close": {"type": "plain_text", "text": "Close", "emoji": True}
                        }
                    )
            except Exception as e:
                logging.error(f"Slack sending-modal update error: {e}")

            background_tasks.add_task(_background_send_and_update, meta, reply_text)
            return JSONResponse({"response_action": "clear"})

        # --- WRITE OWN ---
        if action_id == "write_own":
            meta = get_meta_from_action(action)
            if channel_id:
                meta["channel"] = channel_id
            if message_ts:
                meta["ts"] = message_ts
            meta["sent_label"] = "original message sent"

            guest_name = meta.get("guest_name", "Guest")
            guest_msg = meta.get("guest_message", "(Message unavailable)")
            checkbox_checked = meta.get("checkbox_checked", False)
            modal = {
                "type": "modal",
                "title": {"type": "plain_text", "text": "Write Your Reply", "emoji": True},
                "submit": {"type": "plain_text", "text": "Send", "emoji": True},
                "close": {"type": "plain_text", "text": "Cancel", "emoji": True},
                "private_metadata": json.dumps(meta),
                "blocks": get_modal_blocks(
                    guest_name, guest_msg, action_id="write_own",
                    draft_text="", checkbox_checked=checkbox_checked,
                    input_block_id="reply_input", input_action_id="reply",
                ),
            }
            if slack_client:
                slack_client.views_open(trigger_id=trigger_id, view=modal)
            return JSONResponse({})

        # --- EDIT ---
        if action_id == "edit":
            meta = get_meta_from_action(action)
            if channel_id:
                meta["channel"] = channel_id
            if message_ts:
                meta["ts"] = message_ts
            meta["sent_label"] = "edited message sent"

            guest_name = meta.get("guest_name", "Guest")
            guest_msg = meta.get("guest_message", "(Message unavailable)")
            ai_suggestion = meta.get("draft", meta.get("ai_suggestion", ""))
            checkbox_checked = meta.get("checkbox_checked", False)

            modal_blocks = get_modal_blocks(
                guest_name, guest_msg, action_id="edit",
                draft_text=ai_suggestion, checkbox_checked=checkbox_checked,
                input_block_id="reply_input", input_action_id="reply",
            )
            modal_blocks = add_undo_button(modal_blocks, meta)
            modal = {
                "type": "modal",
                "title": {"type": "plain_text", "text": "Edit AI Reply", "emoji": True},
                "submit": {"type": "plain_text", "text": "Send", "emoji": True},
                "close": {"type": "plain_text", "text": "Cancel", "emoji": True},
                "private_metadata": json.dumps(meta),
                "blocks": modal_blocks,
            }
            if slack_client:
                # If action came from a message, open; if from another modal, push
                try:
                    if container.get("type") == "message":
                        slack_client.views_open(trigger_id=trigger_id, view=modal)
                    else:
                        slack_client.views_push(trigger_id=trigger_id, view=modal)
                except SlackApiError as e:
                    logging.error(f"Slack modal error: {getattr(e, 'response', {}).data if hasattr(e, 'response') else e}")
            return JSONResponse({})

        # --- IMPROVE WITH AI (reads optional "Instruction for AI") ---
        if action_id == "improve_with_ai":
            view = payload.get("view", {}) or {}
            view_id = view.get("id")
            if not view_id:
                logging.error("No view_id on improve_with_ai payload")
                return JSONResponse({})

            # Read current typed text from either input id
            state = view.get("state", {}).get("values", {}) or {}
            edited_text = ""
            for key in ("reply_input_ai", "reply_input"):
                block = state.get(key, {})
                if block:
                    for v in block.values():
                        if isinstance(v, dict) and v.get("value"):
                            edited_text = v["value"]
                            break
                if edited_text:
                    break

            # NEW: read the optional "Instruction for AI"
            custom_prompt = ""
            ai_prompt_block = state.get("ai_prompt_block", {})
            if "ai_prompt" in ai_prompt_block and isinstance(ai_prompt_block["ai_prompt"], dict):
                custom_prompt = (ai_prompt_block["ai_prompt"].get("value") or "").strip()

            # Checkbox state (learning)
            state_save = state.get("save_answer_block", {})
            checkbox_checked = False
            if "save_answer" in state_save and state_save["save_answer"].get("selected_options"):
                checkbox_checked = True

            # Parse meta
            try:
                meta = json.loads(view.get("private_metadata", "{}") or "{}")
            except Exception:
                meta = {}
            if meta.get("improving"):
                logging.info("Improve clicked while already improving; ignoring.")
                return JSONResponse({})

            guest_name = meta.get("guest_name", "Guest")
            guest_msg = meta.get("guest_message", "")

            # Show loading view (and set improving flag)
            loading_meta = {**meta, "improving": True, "checkbox_checked": checkbox_checked}
            loading_blocks = [
                {"type": "section", "text": {"type": "mrkdwn", "text": ":hourglass_flowing_sand: Improving your reply…"}}
            ] + get_modal_blocks(
                guest_name, guest_msg, action_id="edit",
                draft_text=edited_text, checkbox_checked=checkbox_checked,
                input_block_id="reply_input", input_action_id="reply",
            )
            loading_view = {
                "type": "modal",
                "title": {"type": "plain_text", "text": "Improving…", "emoji": True},
                "submit": {"type": "plain_text", "text": "Send", "emoji": True},
                "close": {"type": "plain_text", "text": "Cancel", "emoji": True},
                "private_metadata": json.dumps(loading_meta),
                "blocks": loading_blocks,
            }

            if not slack_client:
                return JSONResponse({})

            current_hash = view.get("hash")
            try:
                resp = slack_client.views_update(view_id=view_id, hash=current_hash, view=loading_view)
                if not resp.get("ok"):
                    err = resp.get("error")
                    logging.error(f"views_update (loading) returned ok=false: {err}")
                    resp2 = slack_client.views_update(view_id=view_id, view=loading_view)
                    if not resp2.get("ok"):
                        logging.error(f"views_update (loading) fallback ok=false: {resp2.get('error')}")
                        return JSONResponse({})
                new_hash = (resp.get("view") or {}).get("hash") or resp.get("hash")
            except Exception as e:
                logging.error(f"views_update (loading) exception: {e}")
                try:
                    resp2 = slack_client.views_update(view_id=view_id, view=loading_view)
                    new_hash = (resp2.get("view") or {}).get("hash") or resp2.get("hash")
                    if not resp2.get("ok"):
                        logging.error(f"views_update (loading) exception-fallback ok=false: {resp2.get('error')}")
                        return JSONResponse({})
                except Exception as e2:
                    logging.error(f"views_update (loading) second exception: {e2}")
                    return JSONResponse({})

            background_tasks.add_task(
                _background_improve_and_update,
                view_id, new_hash, loading_meta, edited_text, guest_name, guest_msg, custom_prompt  # pass the prompt
            )
            return JSONResponse({})

        # --- UNDO AI ---
        if action_id == "undo_ai":
            meta = get_meta_from_action(action)
            guest_name = meta.get("guest_name", "Guest")
            guest_msg = meta.get("guest_message", "")
            previous_draft = meta.get("previous_draft", "")
            checkbox_checked = meta.get("checkbox_checked", False)
            blocks = get_modal_blocks(
                guest_name, guest_msg, action_id="edit",
                draft_text=previous_draft, checkbox_checked=checkbox_checked,
                input_block_id="reply_input", input_action_id="reply",
            )
            blocks = add_undo_button(blocks, meta)
            modal = {
                "type": "modal",
                "title": {"type": "plain_text", "text": "Edit Your Reply", "emoji": True},
                "submit": {"type": "plain_text", "text": "Send", "emoji": True},
                "close": {"type": "plain_text", "text": "Cancel", "emoji": True},
                "private_metadata": json.dumps(meta),
                "blocks": blocks,
            }
            if slack_client:
                try:
                    if container.get("type") == "message":
                        slack_client.views_open(trigger_id=trigger_id, view=modal)
                    else:
                        slack_client.views_push(trigger_id=trigger_id, view=modal)
                except SlackApiError as e:
                    logging.error(f"Slack views push/open error: {getattr(e, 'response', {}).data if hasattr(e, 'response') else e}")
            return JSONResponse({})

        # --- SEND GUEST PORTAL (confirmed bookings only) ---
        if action_id == "send_guest_portal":
            meta = get_meta_from_action(action)
            # Ensure channel/ts available for thread note feedback
            if channel_id and not meta.get("channel"):
                meta["channel"] = channel_id
            if message_ts and not meta.get("ts"):
                meta["ts"] = message_ts
            channel = meta.get("channel")
            ts = meta.get("ts")

            conv_id = meta.get("conv_id")
            communication_type = meta.get("type", "email")
            status = (meta.get("status") or "").lower()  # pretty 'New' -> 'new' ok
            url = meta.get("guest_portal_url") or meta.get("guestPortalUrl")

            if not url:
                _post_thread_note(channel, ts, "⚠️ No guest portal URL available on this reservation.")
                return JSONResponse({})

            if not is_booking_confirmed(status):
                _post_thread_note(channel, ts, "⚠️ Guest portal link is only available after the booking is confirmed.")
                return JSONResponse({})

            try:
                ok = send_reply_to_hostaway(conv_id, f"Here’s your guest portal link: {url}", communication_type)
                if ok:
                    _post_thread_note(channel, ts, "🔗 Guest portal link sent to guest.")
                else:
                    _post_thread_note(channel, ts, "⚠️ Failed to send guest portal link.")
            except Exception as e:
                logging.error(f"Guest portal send error: {e}")
                _post_thread_note(channel, ts, "⚠️ Failed to send guest portal link.")
            return JSONResponse({})

        # Unhandled action ids are no-ops
        return JSONResponse({})

    # ---------- View submission (modal "Send") ----------
    if ptype == "view_submission":
        view = payload.get("view", {}) or {}
        state = view.get("state", {}).get("values", {}) or {}

        try:
            meta = json.loads(view.get("private_metadata", "{}") or "{}")
        except Exception:
            meta = {}

        # Prefer improved field if present
        reply_text: Optional[str] = None
        for block_id, block in state.items():
            if "reply_ai" in block and isinstance(block["reply_ai"], dict) and block["reply_ai"].get("value"):
                reply_text = block["reply_ai"]["value"]
                break
            if "reply" in block and isinstance(block["reply"], dict) and block["reply"].get("value"):
                reply_text = block["reply"].get("value")
                break

        if not reply_text or not meta.get("conv_id"):
            return JSONResponse({
                "response_action": "errors",
                "errors": {"reply_input": "Please enter a reply (and make sure we have a conversation id)."}
            })

        # Save “learn for next time” checkbox
        save_for_next_time = False
        save_block = state.get("save_answer_block", {})
        if "save_answer" in save_block and save_block["save_answer"].get("selected_options"):
            save_for_next_time = True
        meta["saved_for_learning"] = bool(save_for_next_time)

        if save_for_next_time:
            try:
                store_learning_example(
                    meta.get("guest_message", ""),
                    meta.get("ai_suggestion", ""),
                    reply_text,
                    meta.get("listing_id"),
                    meta.get("guest_id"),
                )
            except Exception as e:
                logging.error(f"store_learning_example failed: {e}")

            # Also write into the simplified learning_examples table
            try:
                import sqlite3
                DB_PATH = os.getenv("LEARNING_DB_PATH", "learning.db")
                conn = sqlite3.connect(DB_PATH)
                cur = conn.cursor()
                cur.execute("""
                    CREATE TABLE IF NOT EXISTS learning_examples (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        intent TEXT,
                        question TEXT,
                        answer TEXT,
                        created_at TEXT DEFAULT CURRENT_TIMESTAMP
                    )
                """)
                guest_msg = (meta.get("guest_message") or "")[:2000]
                final_text = reply_text[:4000]
                cur.execute(
                    "INSERT INTO learning_examples (intent, question, answer) VALUES (?, ?, ?)",
                    ("other", guest_msg, final_text)
                )
                conn.commit()
                conn.close()
            except Exception as e:
                logging.error(f"learning_examples insert failed: {e}")

        # Ensure Slack update can happen
        container = payload.get("container", {}) or {}
        channel_id = container.get("channel_id") or (payload.get("channel") or {}).get("id")
        message_ts = container.get("message_ts") or (payload.get("message") or {}).get("ts")
        if channel_id and not meta.get("channel"):
            meta["channel"] = channel_id
        if message_ts and not meta.get("ts"):
            meta["ts"] = message_ts

        # Send + update (background)
        background_tasks.add_task(_background_send_and_update, meta, reply_text)
        return JSONResponse({"response_action": "clear"})

    # Default OK
    return JSONResponse({"ok": True})
