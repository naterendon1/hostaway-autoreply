import os
import logging
import json
import hmac
import hashlib
import time

from fastapi import APIRouter, Request, Header, HTTPException, BackgroundTasks
from fastapi.responses import JSONResponse
from slack_sdk import WebClient
from slack_sdk.errors import SlackApiError
from openai import OpenAI
from utils import (
    send_reply_to_hostaway,
    fetch_hostaway_resource,
    store_learning_example,
    get_similar_learning_examples,
    clean_ai_reply,
)

logging.basicConfig(level=logging.INFO)
router = APIRouter()

SLACK_BOT_TOKEN = os.getenv("SLACK_BOT_TOKEN")
slack_client = WebClient(token=SLACK_BOT_TOKEN)
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
openai_client = OpenAI(api_key=OPENAI_API_KEY)
SLACK_SIGNING_SECRET = os.getenv("SLACK_SIGNING_SECRET")


def verify_slack_signature(request_body: str, slack_signature: str, slack_request_timestamp: str) -> bool:
    if not SLACK_SIGNING_SECRET:
        raise RuntimeError("Missing SLACK_SIGNING_SECRET")

    if not slack_request_timestamp or abs(time.time() - int(slack_request_timestamp)) > 60 * 5:
        return False

    basestring = f"v0:{slack_request_timestamp}:{request_body}".encode("utf-8")
    my_signature = "v0=" + hmac.new(
        SLACK_SIGNING_SECRET.encode("utf-8"),
        basestring,
        hashlib.sha256
    ).hexdigest()
    return hmac.compare_digest(my_signature, slack_signature or "")


def get_modal_blocks(
    guest_name,
    guest_msg,
    action_id,
    draft_text: str = "",
    checkbox_checked: bool = False,
    input_block_id: str = "reply_input",
    input_action_id: str = "reply",
):
    reply_block = {
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

    learning_checkbox_option = {
        "text": {"type": "plain_text", "text": "Save this answer for next time", "emoji": True},
        "value": "save"
    }
    learning_checkbox = {
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

    return [
        {
            "type": "section",
            "block_id": "guest_message_section",
            "text": {"type": "mrkdwn", "text": f"*Guest*: {guest_name}\n*Message*: {guest_msg}"}
        },
        reply_block,
        {
            "type": "actions",
            "block_id": "improve_ai_block",
            "elements": [
                {
                    "type": "button",
                    "action_id": "improve_with_ai",
                    "text": {"type": "plain_text", "text": "Improve with AI", "emoji": True}
                }
            ]
        },
        learning_checkbox
    ]


def update_slack_message_with_sent_reply(
    slack_bot_token,
    channel,
    ts,
    guest_name,
    guest_msg,
    sent_reply,
    communication_type,
    check_in,
    check_out,
    guest_count,
    status,
    detected_intent
):
    _client = WebClient(token=slack_bot_token)
    blocks = [
        {
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": (
                    f"*New {communication_type.capitalize()}* from *{guest_name}*\n"
                    f"Dates: *{check_in} → {check_out}*\n"
                    f"Guests: *{guest_count}* | Status: *{status}*"
                )
            }
        },
        {"type": "section", "text": {"type": "mrkdwn", "text": f"> {guest_msg}"}},
        {"type": "section", "text": {"type": "mrkdwn", "text": f"*Sent Reply:*\n>{sent_reply}"}},
        {"type": "context", "elements": [{"type": "mrkdwn", "text": f"*Intent:* `{detected_intent}`"}]},
        {"type": "section", "text": {"type": "mrkdwn", "text": ":white_check_mark: *Reply sent to guest!*"}}
    ]
    try:
        _client.chat_update(channel=channel, ts=ts, blocks=blocks, text="Reply sent to guest!")
    except Exception as e:
        logging.error(f"❌ Failed to update Slack message with sent reply: {e}")
def add_undo_button(blocks, meta):
    if "previous_draft" in meta and meta["previous_draft"]:
        blocks.append({
            "type": "actions",
            "elements": [
                {
                    "type": "button",
                    "text": {"type": "plain_text", "text": "Undo AI", "emoji": True},
                    "value": json.dumps(meta),
                    "action_id": "undo_ai"
                }
            ]
        })
    return blocks


def _background_improve_and_update(view_id, hash_value, meta, edited_text, guest_name, guest_msg):
    prompt = (
        "Take this guest message reply and improve it. "
        "Make it clear, modern, informal, concise, natural and make it make sense. "
        "Do not add extra content or use emojis. Only return the improved version.\n\n"
        "Give this the tone of a direct-response marketer who’s done $10M in sales."
        f"{edited_text}"
    )
    try:
        response = openai_client.chat.completions.create(
            model="gpt-4",
            timeout=15,
            messages=[
                {"role": "system", "content": "You are a helpful assistant for editing guest replies. Be clear, modern, friendly, and concise. No emojis."},
                {"role": "user", "content": prompt}
            ]
        )
        improved = clean_ai_reply(response.choices[0].message.content.strip())
        error_message = None
    except Exception as e:
        logging.error(f"OpenAI error in background 'improve_with_ai': {e}")
        improved = edited_text
        error_message = f"Error improving with AI: {str(e)}"

    new_meta = {**meta, "previous_draft": edited_text, "improving": False}
    blocks = get_modal_blocks(
        guest_name,
        guest_msg,
        action_id="edit",
        draft_text=improved,
        checkbox_checked=new_meta.get("checkbox_checked", False),
        input_block_id="reply_input_ai",
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

    try:
        resp = slack_client.views_update(view_id=view_id, hash=hash_value, view=final_view)
        logging.info(f"views_update (final) resp: {resp}")
        if not resp.get("ok"):
            err = resp.get("error")
            logging.error(f"views_update (final) ok=false: {err}")
            if err in {"hash_conflict", "not_found", "view_not_found"}:
                resp2 = slack_client.views_update(view_id=view_id, view=final_view)
                logging.info(f"views_update (final) retry-no-hash resp: {resp2}")
                if not resp2.get("ok"):
                    logging.error(f"views_update (final) retry-no-hash ok=false: {resp2.get('error')}")
    except Exception as e:
        logging.error(f"views_update (final) exception: {e}")
        try:
            resp2 = slack_client.views_update(view_id=view_id, view=final_view)
            logging.info(f"views_update (final) exception retry-no-hash resp: {resp2}")
            if not resp2.get("ok"):
                logging.error(f"views_update (final) exception retry-no-hash ok=false: {resp2.get('error')}")
        except Exception as e2:
            logging.error(f"views_update (final) second exception: {e2}")


@router.post("/slack/actions")
async def slack_actions(
    request: Request,
    background_tasks: BackgroundTasks,
    x_slack_signature: str = Header(None, alias="X-Slack-Signature"),
    x_slack_request_timestamp: str = Header(None, alias="X-Slack-Request-Timestamp")
):
    raw_body_bytes = await request.body()
    raw_body = raw_body_bytes.decode("utf-8") if raw_body_bytes else ""

    if not verify_slack_signature(raw_body, x_slack_signature, x_slack_request_timestamp):
        raise HTTPException(status_code=401, detail="Invalid Slack signature or timestamp.")

    form = await request.form()
    payload_raw = form.get("payload")
    if not payload_raw:
        logging.error("Missing payload from Slack.")
        raise HTTPException(status_code=400, detail="Missing payload from Slack.")
    payload = json.loads(payload_raw)

    logging.info("🎯 /slack/actions hit")
    logging.info(f"Slack Interactivity Payload: {json.dumps(payload, indent=2)}")
    if payload.get("type") == "block_actions":
        action = payload["actions"][0]
        action_id = action.get("action_id")
        trigger_id = payload.get("trigger_id")
        user = payload.get("user", {})
        user_id = user.get("id", "")

        def get_meta_from_action(_action):
            return json.loads(_action["value"]) if "value" in _action else {}

        # All logic for each action_id is already covered earlier

    if payload.get("type") == "view_submission":
        view = payload.get("view", {})
        state = view.get("state", {}).get("values", {})
        meta = json.loads(view.get("private_metadata", "{}") or "{}")

        reply_text = None
        for block_id, block in state.items():
            if "reply_ai" in block:
                reply_text = block["reply_ai"]["value"]
                break
            if "reply" in block:
                reply_text = block["reply"]["value"]
                break

        save_for_next_time = False
        for block in state.values():
            if "save_answer" in block and block["save_answer"].get("selected_options"):
                save_for_next_time = True

        conv_id = meta.get("conv_id") or meta.get("conversation_id")
        communication_type = meta.get("type", "email")
        guest_message = meta.get("guest_message", "")
        ai_suggestion = meta.get("ai_suggestion", "")
        channel = meta.get("channel") or os.getenv("SLACK_CHANNEL")
        ts = meta.get("ts")

        try:
            send_reply_to_hostaway(conv_id, reply_text, communication_type)
            if channel and ts:
                update_slack_message_with_sent_reply(
                    slack_bot_token=SLACK_BOT_TOKEN,
                    channel=channel,
                    ts=ts,
                    guest_name=meta.get("guest_name", "Guest"),
                    guest_msg=guest_message,
                    sent_reply=reply_text,
                    communication_type=communication_type,
                    check_in=meta.get("check_in", "N/A"),
                    check_out=meta.get("check_out", "N/A"),
                    guest_count=meta.get("guest_count", "N/A"),
                    status=meta.get("status", "Unknown"),
                    detected_intent=meta.get("detected_intent", "Unknown"),
                )
            if save_for_next_time:
                listing_id = meta.get("listing_id")
                guest_id = meta.get("guest_id")
                store_learning_example(guest_message, ai_suggestion, reply_text, listing_id, guest_id)
        except Exception as e:
            logging.error(f"Slack regular send error: {e}")

        return JSONResponse({"response_action": "clear"})

    return JSONResponse({"status": "ok"})

                # --- WRITE OWN ---
        if action_id == "write_own":
            meta = get_meta_from_action(action)
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
                    guest_name,
                    guest_msg,
                    action_id="write_own",
                    draft_text="",
                    checkbox_checked=checkbox_checked,
                    input_block_id="reply_input",
                    input_action_id="reply",
                )
            }
            slack_client.views_open(trigger_id=trigger_id, view=modal)
            return JSONResponse({})

        # --- EDIT ---
        if action_id == "edit":
            meta = get_meta_from_action(action)
            guest_name = meta.get("guest_name", "Guest")
            guest_msg = meta.get("guest_message", "(Message unavailable)")
            ai_suggestion = meta.get("draft", meta.get("ai_suggestion", ""))
            checkbox_checked = meta.get("checkbox_checked", False)
            modal_blocks = get_modal_blocks(
                guest_name,
                guest_msg,
                action_id="edit",
                draft_text=ai_suggestion,
                checkbox_checked=checkbox_checked,
                input_block_id="reply_input",
                input_action_id="reply",
            )
            modal_blocks = add_undo_button(modal_blocks, meta)
            modal = {
                "type": "modal",
                "title": {"type": "plain_text", "text": "Edit AI Reply", "emoji": True},
                "submit": {"type": "plain_text", "text": "Send", "emoji": True},
                "close": {"type": "plain_text", "text": "Cancel", "emoji": True},
                "private_metadata": json.dumps(meta),
                "blocks": modal_blocks
            }
            container = payload.get("container", {})
            try:
                if container.get("type") == "message":
                    slack_client.views_open(trigger_id=trigger_id, view=modal)
                    logging.info("Opened modal with views_open.")
                else:
                    slack_client.views_push(trigger_id=trigger_id, view=modal)
                    logging.info("Pushed modal with views_push.")
            except Exception as e:
                logging.error(f"Slack modal error: {e}")
            return JSONResponse({})

        # --- IMPROVE WITH AI (Immediate API update -> capture hash -> async finalize) ---
        if action_id == "improve_with_ai":
            view = payload.get("view", {})
            view_id = view.get("id")
            if not view_id:
                logging.error("No view_id on improve_with_ai payload")
                return JSONResponse({})

            # Read current typed text from state
            state = view.get("state", {}).get("values", {})
            reply_block = state.get("reply_input", {})
            edited_text = next((v.get("value") for v in reply_block.values() if v.get("value")), "")

            # Checkbox state
            state_save = state.get("save_answer_block", {})
            checkbox_checked = False
            if "save_answer" in state_save and state_save["save_answer"].get("selected_options"):
                checkbox_checked = True

            # Private metadata + debounce
            meta = json.loads(view.get("private_metadata", "{}") or "{}")
            if meta.get("improving"):
                logging.info("Improve clicked while already improving; ignoring.")
                return JSONResponse({})
            guest_name = meta.get("guest_name", "Guest")
            guest_msg = meta.get("guest_message", "")

            # Build a "loading" view preserving input + text (same IDs)
            loading_meta = {**meta, "improving": True, "checkbox_checked": checkbox_checked}
            loading_blocks = [
                {"type": "section", "text": {"type": "mrkdwn", "text": ":hourglass_flowing_sand: Improving your reply…"}}
            ] + get_modal_blocks(
                guest_name,
                guest_msg,
                action_id="edit",
                draft_text=edited_text,
                checkbox_checked=checkbox_checked,
                input_block_id="reply_input",     # keep original IDs so text remains visible
                input_action_id="reply",
            )
            loading_view = {
                "type": "modal",
                "title": {"type": "plain_text", "text": "Improving…", "emoji": True},
                "submit": {"type": "plain_text", "text": "Send", "emoji": True},
                "close": {"type": "plain_text", "text": "Cancel", "emoji": True},
                "private_metadata": json.dumps(loading_meta),
                "blocks": loading_blocks
            }

            # Update via API immediately using current hash; CAPTURE & LOG response (no response_action)
            current_hash = view.get("hash")
            try:
                resp = slack_client.views_update(view_id=view_id, hash=current_hash, view=loading_view)
                logging.info(f"views_update (loading) resp: {resp}")
                if not resp.get("ok"):
                    err = resp.get("error")
                    logging.error(f"views_update (loading) returned ok=false: {err}")
                    # Fallback once without hash so user still sees loading
                    resp2 = slack_client.views_update(view_id=view_id, view=loading_view)
                    logging.info(f"views_update (loading) fallback resp: {resp2}")
                    if not resp2.get("ok"):
                        logging.error(f"views_update (loading) fallback ok=false: {resp2.get('error')}")
                        return JSONResponse({})
                new_hash = resp.get("view", {}).get("hash") or resp.get("hash")
            except Exception as e:
                logging.error(f"views_update (loading) exception: {e}")
                # Final fallback: try without hash and keep going
                try:
                    resp2 = slack_client.views_update(view_id=view_id, view=loading_view)
                    logging.info(f"views_update (loading) exception-fallback resp: {resp2}")
                    new_hash = resp2.get("view", {}).get("hash") or resp2.get("hash")
                    if not resp2.get("ok"):
                        logging.error(f"views_update (loading) exception-fallback ok=false: {resp2.get('error')}")
                        return JSONResponse({})
                except Exception as e2:
                    logging.error(f"views_update (loading) second exception: {e2}")
                    return JSONResponse({})

            # Background: call OpenAI + final update using fresh hash
            background_tasks.add_task(
                _background_improve_and_update,
                view_id,
                new_hash,
                loading_meta,
                edited_text,
                guest_name,
                guest_msg,
            )

            # IMPORTANT: return empty JSON — no response_action from block_action
            return JSONResponse({})

        # --- UNDO AI IMPROVEMENT ---
        if action_id == "undo_ai":
            meta = get_meta_from_action(action)
            guest_name = meta.get("guest_name", "Guest")
            guest_msg = meta.get("guest_message", "")
            previous_draft = meta.get("previous_draft", "")
            checkbox_checked = meta.get("checkbox_checked", False)
            blocks = get_modal_blocks(
                guest_name,
                guest_msg,
                action_id="edit",
                draft_text=previous_draft,
                checkbox_checked=checkbox_checked,
                input_block_id="reply_input",    # back to original IDs
                input_action_id="reply",
            )
            blocks = add_undo_button(blocks, meta)
            modal = {
                "type": "modal",
                "title": {"type": "plain_text", "text": "Edit Your Reply", "emoji": True},
                "submit": {"type": "plain_text", "text": "Send", "emoji": True},
                "close": {"type": "plain_text", "text": "Cancel", "emoji": True},
                "private_metadata": json.dumps(meta),
                "blocks": blocks
            }
            # Use push/open depending on container type
            container = payload.get("container", {})
            try:
                if container.get("type") == "message":
                    slack_client.views_open(trigger_id=trigger_id, view=modal)
                else:
                    slack_client.views_push(trigger_id=trigger_id, view=modal)
            except Exception as e:
                logging.error(f"Slack views push/open error: {e}")
            return JSONResponse({})

    # -------------------- view_submission handler --------------------
    if payload.get("type") == "view_submission":
        view = payload.get("view", {})
        state = view.get("state", {}).get("values", {})
        meta = json.loads(view.get("private_metadata", "{}") or "{}")

        # Get reply text: prefer AI field if present, else original
        reply_text = None
        for block_id, block in state.items():
            if "reply_ai" in block:
                reply_text = block["reply_ai"]["value"]
                break
            if "reply" in block:
                reply_text = block["reply"]["value"]
                break

        # Get checkbox state
        save_for_next_time = False
        for block in state.values():
            if "save_answer" in block and block["save_answer"].get("selected_options"):
                save_for_next_time = True

        conv_id = meta.get("conv_id") or meta.get("conversation_id")
        communication_type = meta.get("type", "email")
        guest_message = meta.get("guest_message", "")
        ai_suggestion = meta.get("ai_suggestion", "")
        channel = meta.get("channel") or os.getenv("SLACK_CHANNEL")
        ts = meta.get("ts")

        try:
            send_reply_to_hostaway(conv_id, reply_text, communication_type)
            # Update Slack thread with actual sent reply
            if channel and ts:
                update_slack_message_with_sent_reply(
                    slack_bot_token=SLACK_BOT_TOKEN,
                    channel=channel,
                    ts=ts,
                    guest_name=meta.get("guest_name", "Guest"),
                    guest_msg=guest_message,
                    sent_reply=reply_text,
                    communication_type=communication_type,
                    check_in=meta.get("check_in", "N/A"),
                    check_out=meta.get("check_out", "N/A"),
                    guest_count=meta.get("guest_count", "N/A"),
                    status=meta.get("status", "Unknown"),
                    detected_intent=meta.get("detected_intent", "Unknown"),
                )
            if save_for_next_time:
                listing_id = meta.get("listing_id")
                guest_id = meta.get("guest_id")
                store_learning_example(guest_message, ai_suggestion, reply_text, listing_id, guest_id)
        except Exception as e:
            logging.error(f"Slack regular send error: {e}")

        return JSONResponse({"response_action": "clear"})

    # Fallback
    return JSONResponse({"status": "ok"})




