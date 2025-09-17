# file: slack_interactivity.py
import os
import logging
import json
import hmac
import hashlib
import time
import sqlite3
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Request, Header, HTTPException, BackgroundTasks
from fastapi.responses import JSONResponse
from slack_sdk import WebClient
from slack_sdk.errors import SlackApiError
from openai import OpenAI

from utils import (
    send_reply_to_hostaway,
    store_learning_example,  # legacy helper kept for backwards compatibility
    clean_ai_reply,
    sanitize_ai_reply,
)
from places import should_fetch_local_recs, build_local_recs

# --- NEW: analytics hook
from db import record_event

logging.basicConfig(level=logging.INFO)
router = APIRouter()

# --- Slack / OpenAI clients
SLACK_BOT_TOKEN = os.getenv("SLACK_BOT_TOKEN", "")
SLACK_SIGNING_SECRET = os.getenv("SLACK_SIGNING_SECRET", "")
slack_client = WebClient(token=SLACK_BOT_TOKEN) if SLACK_BOT_TOKEN else None

OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
openai_client = OpenAI(api_key=OPENAI_API_KEY) if OPENAI_API_KEY else None

LEARNING_DB_PATH = os.getenv("LEARNING_DB_PATH", "learning.db")

if not SLACK_BOT_TOKEN:
    logging.warning("SLACK_BOT_TOKEN is not set; Slack operations will fail in production.")


# -------------------- Security: Slack Signature Verify --------------------
def verify_slack_signature(
    request_body: str,
    slack_signature: Optional[str],
    slack_request_timestamp: Optional[str],
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

    base_string = f"v0:{slack_request_timestamp}:{request_body}".encode("utf-8")
    my_signature = "v0=" + hmac.new(
        SLACK_SIGNING_SECRET.encode("utf-8"),
        base_string,
        hashlib.sha256,
    ).hexdigest()
    return hmac.compare_digest(my_signature, slack_signature)


# -------------------- Small helpers --------------------
# Hostaway "booked/confirmed" statuses
CONFIRMED_STATUSES = {"new", "modified"}

def is_booking_confirmed(status: Optional[str]) -> bool:
    """
    Accepts either raw Hostaway status ('new', 'modified', ...) or your pretty
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


# ---------------- Private metadata packing (avoid 3KB limit) --------------------
MAX_PRIVATE_BYTES = 2800  # safety margin under Slack's ~3KB limit
PRIVATE_META_KEYS = {
    "conv_id", "listing_id", "guest_id", "guest_name", "guest_message",
    "type", "status", "check_in", "check_out", "guest_count",
    "channel", "ts", "detected_intent", "channel_pretty", "property_address",
    "sent_label", "checkbox_checked", "coach_prompt", "location"  # keep lat/lng small if present
}

def pack_private_meta(meta: Dict[str, Any]) -> str:
    """
    Keep only essential keys and ensure the JSON stays under Slack's private_metadata limit.
    """
    thin = {k: meta.get(k) for k in PRIVATE_META_KEYS if k in meta}
    s = json.dumps(thin, ensure_ascii=False)
    if len(s.encode("utf-8")) <= MAX_PRIVATE_BYTES:
        return s

    # Last resort: trim potentially long strings
    for k in ("property_address", "guest_message"):
        if k in thin and isinstance(thin[k], str):
            thin[k] = thin[k][:800]
            s = json.dumps(thin, ensure_ascii=False)
            if len(s.encode("utf-8")) <= MAX_PRIVATE_BYTES:
                break
    # If somehow still too large, slice bytes as final guard
    enc = s.encode("utf-8")
    if len(enc) > MAX_PRIVATE_BYTES:
        enc = enc[:MAX_PRIVATE_BYTES]
        try:
            s = enc.decode("utf-8", errors="ignore")
        except Exception:
            s = "{}"
    return s


# ---------------- Places injection ----------------
def inject_local_recs(meta: Dict[str, Any], guest_msg_override: Optional[str] = None) -> Dict[str, Any]:
    """
    Populate meta["local_recs_api"] with live POIs based on lat/lng and a qualifying guest message.
    Safe no-op on any failure. Note: this is NOT serialized into private_metadata.
    """
    try:
        lat = None
        lng = None
        guest_msg = guest_msg_override
        # Prefer nested location dict if present
        loc = meta.get("location") if isinstance(meta, dict) else None
        if isinstance(loc, dict):
            lat = loc.get("lat")
            lng = loc.get("lng")
        # Fallbacks if lat/lng are at top-level
        if lat is None and isinstance(meta, dict):
            lat = meta.get("lat")
        if lng is None and isinstance(meta, dict):
            lng = meta.get("lng")
        if guest_msg is None and isinstance(meta, dict):
            guest_msg = (meta.get("guest_message") or "")

        local_recs_api: List[Dict[str, Any]] = []
        if lat is not None and lng is not None and should_fetch_local_recs(guest_msg or ""):
            local_recs_api = build_local_recs(lat, lng, guest_msg or "")
        meta["local_recs_api"] = local_recs_api
    except Exception as e:
        logging.warning(f"[interactivity] Local recs fetch failed: {e}")
        try:
            meta["local_recs_api"] = []
        except Exception:
            pass
    return meta


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

    header_text = (
        f"*{channel_label} message* from *{guest_name}*\n"
        f"Property: *{addr}*\n"
        f"Dates: *{check_in} → {check_out}*\n"
        f"Guests: *{guest_count}* | Status: *{status}*"
    )

    blocks = [
        {"type": "section", "text": {"type": "mrkdwn", "text": header_text}},
        {"type": "section", "text": {"type": "mrkdwn", "text": f"> {guest_msg}"}},
        {"type": "section", "text": {"type": "mrkdwn", "text": f"*Sent Reply:*\n>{sent_reply}"}},
        {"type": "context", "elements": ctx_elems},
        {"type": "section", "text": {"type": "mrkdwn", "text": f":white_check_mark: *{sent_label}*"}},
    ]

    try:
        _client.chat_update(channel=channel, ts=ts, blocks=blocks, text="Reply sent to guest!")
    except SlackApiError as e:
        logging.error(
            f"❌ Failed to update Slack message with sent reply: {getattr(e, 'response', {}).data if hasattr(e, 'response') else e}"
        )


# --------- Modal building blocks ----------
def get_modal_blocks(
    guest_name: str,
    guest_msg: str,
    action_id: str,
    draft_text: str = "",
    checkbox_checked: bool = False,
    input_block_id: str = "reply_input",
    input_action_id: str = "reply",
    coach_prompt_initial: Optional[str] = None,
) -> List[Dict[str, Any]]:
    """
    Builds a modal that contains:
      - The guest message (read-only)
      - A multiline text box with your current draft (or empty)
      - A "coach the AI" prompt box (optional, used by 'Improve with AI')
      - A checkbox to save for learning
    """
    reply_block: Dict[str, Any] = {
        "type": "input",
        "block_id": input_block_id,
        "label": {"type": "plain_text", "text": ("Your reply:" if action_id == "write_own" else "Edit below:"), "emoji": True},
        "element": {
            "type": "plain_text_input",
            "action_id": input_action_id,
            "multiline": True,
        },
    }
    if draft_text:
        reply_block["element"]["initial_value"] = draft_text

    coach_block: Dict[str, Any] = {
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
                "text": "Tell the AI how to tweak this reply (e.g., 'offer pest control, not cleaners').",
            },
        },
    }
    if coach_prompt_initial:
        coach_block["element"]["initial_value"] = coach_prompt_initial[:3000]

    learning_checkbox_option = {
        "text": {"type": "plain_text", "text": "Save this answer for next time", "emoji": True},
        "value": "save",
    }
    learning_checkbox: Dict[str, Any] = {
        "type": "input",
        "block_id": "save_answer_block",
        "element": {
            "type": "checkboxes",
            "action_id": "save_answer",
            "options": [learning_checkbox_option],
        },
        "label": {"type": "plain_text", "text": "Learning", "emoji": True},
        "optional": True,
    }
    if checkbox_checked:
        learning_checkbox["element"]["initial_options"] = [learning_checkbox_option]

    return [
        {
            "type": "section",
            "block_id": "guest_message_section",
            "text": {"type": "mrkdwn", "text": f"*Guest*: {guest_name}\n*Message*: {guest_msg}"},
        },
        reply_block,
        coach_block,
        {
            "type": "actions",
            "block_id": "improve_ai_block",
            "elements": [
                {
                    "type": "button",
                    "action_id": "improve_with_ai",
                    "text": {"type": "plain_text", "text": "Improve with AI", "emoji": True},
                }
            ],
        },
        learning_checkbox,
    ]


def add_undo_button(blocks: List[Dict[str, Any]], meta: Dict[str, Any]) -> List[Dict[str, Any]]:
    if meta.get("previous_draft"):
        blocks.append(
            {
                "type": "actions",
                "elements": [
                    {
                        "type": "button",
                        "text": {"type": "plain_text", "text": "Undo AI", "emoji": True},
                        "value": json.dumps(meta),
                        "action_id": "undo_ai",
                    }
                ],
            }
        )
    return blocks


# ---------------- Background: improve + final views.update (with conditional hash) ----------------
def _background_improve_and_update(
    view_id: str,
    hash_value: Optional[str],
    meta: dict,
    edited_text: str,
    coach_prompt_text: Optional[str],
    guest_name: str,
    guest_msg: str,
):
    """
    Uses OpenAI to improve the draft, guided by an optional coach prompt.
    """
    improved = edited_text
    error_message = None

    if not openai_client:
        error_message = "OpenAI key not configured; showing your original text."
    else:
        # Build a precise instruction that references both the guest message and the coach prompt.
        sys = (
            "You edit messages for a vacation-rental host. "
            "Keep meaning, improve tone and brevity. No greetings, no sign-offs, no emojis. "
            "Style: concise, casual, informal, easy to understand."
        )
        user = (
            "Guest message:\n"
            f"{guest_msg}\n\n"
            "Current draft reply (to improve, not to lengthen):\n"
            f"{edited_text}\n\n"
            "Coach prompt (host's instruction to adjust the reply):\n"
            f"{(coach_prompt_text or '').strip() or '(none)'}\n\n"
            "Rewrite the reply to satisfy the coach prompt if present, keep the same intent, and stay concise. "
            "Return ONLY the rewritten reply."
        )
        try:
            response = openai_client.chat.completions.create(
                model="gpt-4o-mini",
                messages=[
                    {"role": "system", "content": sys},
                    {"role": "user", "content": user},
                ],
            )
            improved = clean_ai_reply((response.choices[0].message.content or "").strip())
        except Exception as e:
            logging.error(f"OpenAI error in background 'improve_with_ai': {e}")
            error_message = f"Error improving with AI: {str(e)}"

    # Keep the previous draft so "Undo AI" works
    new_meta = {**meta, "previous_draft": edited_text, "improving": False, "coach_prompt": coach_prompt_text or ""}

    blocks = get_modal_blocks(
        guest_name,
        guest_msg,
        action_id="edit",
        draft_text=improved,
        checkbox_checked=new_meta.get("checkbox_checked", False),
        input_block_id="reply_input_ai",  # Force Slack to refill initial_value
        input_action_id="reply_ai",
        coach_prompt_initial=coach_prompt_text or "",
    )
    blocks = add_undo_button(blocks, new_meta)
    if error_message:
        blocks = [{"type": "section", "text": {"type": "mrkdwn", "text": f":warning: *{error_message}*"}}] + blocks

    final_view = {
        "type": "modal",
        "title": {"type": "plain_text", "text": "AI Improved Reply", "emoji": True},
        "submit": {"type": "plain_text", "text": "Send", "emoji": True},
        "close": {"type": "plain_text", "text": "Cancel", "emoji": True},
        "private_metadata": pack_private_meta(new_meta),
        "blocks": blocks,
    }

    if not slack_client:
        return

    try:
        if hash_value:
            resp = slack_client.views_update(view_id=view_id, hash=hash_value, view=final_view)
        else:
            resp = slack_client.views_update(view_id=view_id, view=final_view)
        if not resp.get("ok"):
            logging.error(f"views_update (final) ok=false: {resp.get('error')}")
            # Fallback without hash
            try:
                slack_client.views_update(view_id=view_id, view=final_view)
            except Exception as e2:
                logging.error(f"views_update (final) fallback exception: {e2}")
    except Exception as e:
        logging.error(f"views_update (final) exception: {e}")
        try:
            slack_client.views_update(view_id=view_id, view=final_view)
        except Exception as e2:
            logging.error(f"views_update (final) second exception: {e2}")


# ---------------- Background: send to Hostaway + update Slack ----------------
def _ensure_feedback_tables(conn: sqlite3.Connection) -> None:
    cur = conn.cursor()
    cur.execute("""
        CREATE TABLE IF NOT EXISTS ai_feedback (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            conversation_id TEXT,
            question TEXT,
            ai_answer TEXT,
            rating TEXT,
            reason TEXT,
            user TEXT,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP
        )
    """)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS learning_examples (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            intent TEXT,
            question TEXT,
            answer TEXT,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP
        )
    """)
    conn.commit()


def _insert_feedback_row(row: Dict[str, Any]) -> None:
    conn = sqlite3.connect(LEARNING_DB_PATH)
    conn.row_factory = sqlite3.Row
    _ensure_feedback_tables(conn)
    cur = conn.cursor()
    cur.execute("""
        INSERT INTO ai_feedback (conversation_id, question, ai_answer, rating, reason, user)
        VALUES (:conversation_id, :question, :ai_answer, :rating, :reason, :user)
    """, row)
    conn.commit()
    conn.close()


def _insert_learning_example(question: str, answer: str, intent: str = "") -> None:
    if not (question and answer):
        return
    conn = sqlite3.connect(LEARNING_DB_PATH)
    conn.row_factory = sqlite3.Row
    _ensure_feedback_tables(conn)
    cur = conn.cursor()
    cur.execute("""
        INSERT INTO learning_examples (intent, question, answer)
        VALUES (?, ?, ?)
    """, (intent or "", question, answer))
    conn.commit()
    conn.close()


def _background_send_and_update(meta: dict, reply_text: str):
    # sanitize reply (best-effort)
    try:
        reply_text = sanitize_ai_reply(reply_text, meta.get("guest_message", ""))
    except Exception:
        pass

    # Send to Hostaway (actual delivery)
    ok = False
    try:
        conv_id = meta.get("conv_id")
        comm_type = meta.get("type", "email")
        if conv_id:
            ok = bool(send_reply_to_hostaway(conv_id, reply_text, comm_type))
    except Exception as e:
        logging.error(f"Hostaway send error: {e}")
        ok = False

    # ---- NEW analytics hook (right after computing ok) ----
    try:
        record_event(
            "slack",
            "send" if ok else "send.failed",
            conversation_id=str(meta.get("conv_id") or ""),
            reservation_id=str(meta.get("reservation_id") or ""),
            listing_id=str(meta.get("listing_id") or ""),
            guest_id=str(meta.get("guest_id") or ""),
            user_id="",  # Slack user id not reliably available here
            intent=meta.get("detected_intent"),
            text=reply_text,
            payload={"saved_for_learning": bool(meta.get("saved_for_learning"))},
        )
    except Exception as e:
        logging.error(f"analytics send: {e}")
    # -------------------------------------------------------

    # Update Slack UI message
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
                text="Failed to send reply.",
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
    ptype = payload.get("type")

    # ---------- Block actions ----------
    if ptype == "block_actions":
        action = payload["actions"][0]
        action_id = action.get("action_id")
        trigger_id = payload.get("trigger_id")
        container = payload.get("container", {}) or {}
        channel_id = container.get("channel_id") or (payload.get("channel") or {}).get("id")
        message_ts = container.get("message_ts") or (payload.get("message") or {}).get("ts")
        user_id = (payload.get("user") or {}).get("id")

        def get_meta_from_action(_action: Dict[str, Any]) -> dict:
            try:
                return json.loads(_action.get("value") or "{}")
            except Exception:
                return {}

        # --- FEEDBACK: 👍 Useful ---
        if action_id == "rate_up":
            meta = get_meta_from_action(action)
            try:
                _insert_feedback_row({
                    "conversation_id": str(meta.get("conv_id") or ""),
                    "question": meta.get("guest_message") or "",
                    "ai_answer": meta.get("ai_suggestion") or "",
                    "rating": "up",
                    "reason": "",
                    "user": user_id or "",
                })
            except Exception as e:
                logging.error(f"insert feedback up failed: {e}")

            # ---- NEW analytics hook ----
            try:
                record_event(
                    "slack",
                    "rate_up",
                    conversation_id=str(meta.get("conv_id") or ""),
                    listing_id=str(meta.get("listing_id") or ""),
                    guest_id=str((meta.get("guest_id") or "")),
                    user_id=user_id or "",
                    rating="up",
                    intent=meta.get("detected_intent"),
                    text=meta.get("ai_suggestion") or "",
                )
            except Exception as e:
                logging.error(f"analytics rate_up: {e}")
            # ----------------------------

            # Best-effort ephemeral ack
            try:
                if slack_client and channel_id and user_id:
                    slack_client.chat_postEphemeral(channel=channel_id, user=user_id, text="Thanks for the feedback 👍")
            except Exception as e:
                logging.debug(f"ephemeral ack failed: {e}")
            return JSONResponse({"ok": True})

            # (no view to update)

        # --- FEEDBACK: 👎 Needs work (open reason modal) ---
        if action_id == "rate_down":
            meta = get_meta_from_action(action)
            private_meta = json.dumps({
                "conv_id": meta.get("conv_id"),
                "guest_message": meta.get("guest_message"),
                "ai_suggestion": meta.get("ai_suggestion"),
                "detected_intent": meta.get("detected_intent"),
                "channel_id": channel_id,
            })
            view = {
                "type": "modal",
                "callback_id": "rate_down_modal",
                "private_metadata": private_meta,
                "title": {"type": "plain_text", "text": "Feedback"},
                "submit": {"type": "plain_text", "text": "Submit"},
                "close": {"type": "plain_text", "text": "Cancel"},
                "blocks": [
                    {
                        "type": "input",
                        "block_id": "reason_block",
                        "label": {"type": "plain_text", "text": "What was wrong?"},
                        "element": {
                            "type": "plain_text_input",
                            "action_id": "reason",
                            "multiline": True,
                            "placeholder": {"type": "plain_text", "text": "E.g., tone off, incorrect policy, missed intent..."},
                        }
                    },
                    {
                        "type": "input",
                        "optional": True,
                        "block_id": "improved_block",
                        "label": {"type": "plain_text", "text": "Your improved reply (optional)"},
                        "element": {
                            "type": "plain_text_input",
                            "action_id": "improved",
                            "multiline": True,
                        }
                    },
                ],
            }
            try:
                if slack_client:
                    slack_client.views_open(trigger_id=trigger_id, view=view)
            except SlackApiError as e:
                logging.error(f"views_open failed: {e.response.data if hasattr(e, 'response') else e}")
            return JSONResponse({})

        # --- SEND ---
        if action_id == "send":
            meta = get_meta_from_action(action)
            # Ensure channel/ts are present for later update
            if channel_id and not meta.get("channel"):
                meta["channel"] = channel_id
            if message_ts and not meta.get("ts"):
                meta["ts"] = message_ts

            # Try to enrich meta with local recs before send (harmless if unused)
            inject_local_recs(meta)

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
                            "blocks": [
                                {
                                    "type": "section",
                                    "text": {"type": "mrkdwn", "text": ":hourglass: Sending your message..."},
                                }
                            ],
                            "close": {"type": "plain_text", "text": "Close", "emoji": True},
                        },
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

            inject_local_recs(meta)

            guest_name = meta.get("guest_name", "Guest")
            guest_msg = meta.get("guest_message", "(Message unavailable)")
            checkbox_checked = meta.get("checkbox_checked", False)
            coach_prompt_initial = meta.get("coach_prompt", "")

            modal = {
                "type": "modal",
                "title": {"type": "plain_text", "text": "Write Your Reply", "emoji": True},
                "submit": {"type": "plain_text", "text": "Send", "emoji": True},
                "close": {"type": "plain_text", "text": "Cancel", "emoji": True},
                "private_metadata": pack_private_meta(meta),
                "blocks": get_modal_blocks(
                    guest_name,
                    guest_msg,
                    action_id="write_own",
                    draft_text="",
                    checkbox_checked=checkbox_checked,
                    input_block_id="reply_input",
                    input_action_id="reply",
                    coach_prompt_initial=coach_prompt_initial,
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

            inject_local_recs(meta)

            guest_name = meta.get("guest_name", "Guest")
            guest_msg = meta.get("guest_message", "(Message unavailable)")
            ai_suggestion = meta.get("draft", meta.get("ai_suggestion", ""))
            checkbox_checked = meta.get("checkbox_checked", False)
            coach_prompt_initial = meta.get("coach_prompt", "")

            modal_blocks = get_modal_blocks(
                guest_name,
                guest_msg,
                action_id="edit",
                draft_text=ai_suggestion,
                checkbox_checked=checkbox_checked,
                input_block_id="reply_input",
                input_action_id="reply",
                coach_prompt_initial=coach_prompt_initial,
            )
            modal_blocks = add_undo_button(modal_blocks, meta)
            modal = {
                "type": "modal",
                "title": {"type": "plain_text", "text": "Edit AI Reply", "emoji": True},
                "submit": {"type": "plain_text", "text": "Send", "emoji": True},
                "close": {"type": "plain_text", "text": "Cancel", "emoji": True},
                "private_metadata": pack_private_meta(meta),
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
                    logging.error(
                        f"Slack modal error: {getattr(e, 'response', {}).data if hasattr(e, 'response') else e}"
                    )
            return JSONResponse({})

        # --- IMPROVE WITH AI ---
        if action_id == "improve_with_ai":
            view = payload.get("view") or {}
            container = payload.get("container") or {}
            # Be lenient: sometimes Slack only gives container.view_id
            view_id = view.get("id") or container.get("view_id")
            if not view_id:
                logging.error("No view_id on improve_with_ai payload")
                return JSONResponse({})

            # Read current typed text from common text inputs (robust scan)
            edited_text = ""
            state_values = (view.get("state") or {}).get("values") or {}
            # Specific IDs first
            for key in ("reply_input_ai", "reply_input"):
                block = state_values.get(key, {})
                if block:
                    for v in block.values():
                        if isinstance(v, dict) and v.get("value"):
                            edited_text = v["value"]
                            break
                if edited_text:
                    break
            # Fallback scan
            if not edited_text:
                for block in state_values.values():
                    for action_obj in block.values():
                        if isinstance(action_obj, dict) and action_obj.get("type") == "plain_text_input":
                            if action_obj.get("value"):
                                edited_text = action_obj["value"]
                                break
                    if edited_text:
                        break

            # Read the coach prompt (optional)
            coach_prompt_value = ""
            cp_block = state_values.get("coach_prompt_block", {})
            if "coach_prompt" in cp_block and isinstance(cp_block["coach_prompt"], dict):
                coach_prompt_value = (cp_block["coach_prompt"].get("value") or "").strip()

            # Checkbox state
            checkbox_checked = False
            state_save = state_values.get("save_answer_block", {})
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

            # Ensure fresh local recs in meta (uses the latest typed message if provided)
            guest_name = meta.get("guest_name", "Guest")
            guest_msg = meta.get("guest_message", "")
            inject_local_recs(meta, guest_msg_override=guest_msg)

            # Build loading view (and set improving flag)
            loading_meta = {
                **meta,
                "improving": True,
                "checkbox_checked": checkbox_checked,
                "coach_prompt": coach_prompt_value,
            }
            loading_blocks = [
                {"type": "section", "text": {"type": "mrkdwn", "text": ":hourglass_flowing_sand: Improving your reply…"}},
            ] + get_modal_blocks(
                guest_name,
                guest_msg,
                action_id="edit",
                draft_text=edited_text or "",
                checkbox_checked=checkbox_checked,
                input_block_id="reply_input",
                input_action_id="reply",
                coach_prompt_initial=coach_prompt_value,
            )
            loading_view = {
                "type": "modal",
                "title": {"type": "plain_text", "text": "Improving…", "emoji": True},
                "submit": {"type": "plain_text", "text": "Send", "emoji": True},
                "close": {"type": "plain_text", "text": "Cancel", "emoji": True},
                "private_metadata": pack_private_meta(loading_meta),
                "blocks": loading_blocks,
            }

            # Schedule the real improvement in the background (we won't rely on hash here)
            background_tasks.add_task(
                _background_improve_and_update,
                view_id,
                None,  # don't rely on hash; background will handle fallbacks
                loading_meta,
                edited_text or "",
                coach_prompt_value,
                guest_name,
                guest_msg,
            )

            # Instantly swap the modal to “Improving…” via the interaction response
            return JSONResponse({
                "response_action": "update",
                "view": loading_view
            })

        # --- UNDO AI ---
        if action_id == "undo_ai":
            meta = get_meta_from_action(action)
            guest_name = meta.get("guest_name", "Guest")
            guest_msg = meta.get("guest_message", "")
            previous_draft = meta.get("previous_draft", "")
            checkbox_checked = meta.get("checkbox_checked", False)
            coach_prompt_initial = meta.get("coach_prompt", "")

            inject_local_recs(meta)

            blocks = get_modal_blocks(
                guest_name,
                guest_msg,
                action_id="edit",
                draft_text=previous_draft,
                checkbox_checked=checkbox_checked,
                input_block_id="reply_input",
                input_action_id="reply",
                coach_prompt_initial=coach_prompt_initial,
            )
            blocks = add_undo_button(blocks, meta)
            modal = {
                "type": "modal",
                "title": {"type": "plain_text", "text": "Edit Your Reply", "emoji": True},
                "submit": {"type": "plain_text", "text": "Send", "emoji": True},
                "close": {"type": "plain_text", "text": "Cancel", "emoji": True},
                "private_metadata": pack_private_meta(meta),
                "blocks": blocks,
            }
            if slack_client:
                try:
                    if container.get("type") == "message":
                        slack_client.views_open(trigger_id=trigger_id, view=modal)
                    else:
                        slack_client.views_push(trigger_id=trigger_id, view=modal)
                except SlackApiError as e:
                    logging.error(
                        f"Slack views push/open error: {getattr(e, 'response', {}).data if hasattr(e, 'response') else e}"
                    )
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
            status = (meta.get("status") or "").lower()  # pretty 'New' -> 'new' is OK
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

    # ---------- View submission (modal "Send" OR feedback modal) ----------
    if ptype == "view_submission":
        view = payload.get("view", {}) or {}
        callback_id = view.get("callback_id") or ""

        # Feedback modal submit
        if callback_id == "rate_down_modal":
            state = view.get("state", {}).get("values", {}) or {}
            private_meta = {}
            try:
                private_meta = json.loads(view.get("private_metadata") or "{}")
            except Exception:
                private_meta = {}

            reason = ((state.get("reason_block") or {}).get("reason") or {}).get("value") or ""
            improved = ((state.get("improved_block") or {}).get("improved") or {}).get("value") or ""
            user_id = (payload.get("user") or {}).get("id") or ""

            guest_message = private_meta.get("guest_message") or ""
            ai_suggestion = private_meta.get("ai_suggestion") or ""
            conv_id = private_meta.get("conv_id")
            try:
                _insert_feedback_row({
                    "conversation_id": str(conv_id or ""),
                    "question": guest_message,
                    "ai_answer": ai_suggestion,
                    "rating": "down",
                    "reason": reason.strip(),
                    "user": user_id,
                })
            except Exception as e:
                logging.error(f"insert feedback down failed: {e}")

            # ---- NEW analytics hook ----
            try:
                record_event(
                    "slack",
                    "rate_down",
                    conversation_id=str(conv_id or ""),
                    listing_id="",
                    guest_id="",
                    user_id=user_id,
                    rating="down",
                    reason=reason.strip() or None,
                    intent=private_meta.get("detected_intent"),
                    text=ai_suggestion or "",
                )
            except Exception as e:
                logging.error(f"analytics rate_down: {e}")
            # ----------------------------

            if improved.strip():
                try:
                    _insert_learning_example(guest_message, improved.strip(), intent=private_meta.get("detected_intent") or "")
                except Exception as e:
                    logging.error(f"insert learning example failed: {e}")

            return JSONResponse({"response_action": "clear"})

        # --- Normal reply modal submission (Send) ---
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
                reply_text = block["reply"]["value"]
                break

        # Optional coach prompt (to save with learning)
        coach_prompt_value: Optional[str] = None
        cp_block = state.get("coach_prompt_block", {})
        if "coach_prompt" in cp_block and isinstance(cp_block["coach_prompt"], dict):
            coach_prompt_value = (cp_block["coach_prompt"].get("value") or "").strip() or None

        if not reply_text or not meta.get("conv_id"):
            return JSONResponse(
                {
                    "response_action": "errors",
                    "errors": {"reply_input": "Please enter a reply (and make sure we have a conversation id)."},
                }
            )

        # Save “learn for next time” checkbox
        save_for_next_time = False
        save_block = state.get("save_answer_block", {})
        if "save_answer" in save_block and save_block["save_answer"].get("selected_options"):
            save_for_next_time = True
        meta["saved_for_learning"] = bool(save_for_next_time)

        # Ensure meta has local recs (for richer learning context)
        inject_local_recs(meta)

        if save_for_next_time:
            # 1) Keep your legacy store (optional, unchanged)
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

            # 2) New richer table: learning_examples (simple)
            try:
                conn = sqlite3.connect(LEARNING_DB_PATH)
                _ensure_feedback_tables(conn)
                cur = conn.cursor()
                cur.execute(
                    "INSERT INTO learning_examples (intent, question, answer) VALUES (?, ?, ?)",
                    (meta.get("detected_intent") or "other", (meta.get("guest_message") or "")[:4000], reply_text[:8000]),
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
