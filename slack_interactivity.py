import os
import logging
import json
import datetime
from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from slack_sdk import WebClient
from utils import (
    send_reply_to_hostaway,
    fetch_hostaway_resource,
    store_learning_example,
    get_similar_learning_examples,
    clean_ai_reply,
)

from slack_sdk import WebClient
import logging

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
    """
    Updates the original Slack message (with the AI suggestion) to show the actual sent reply,
    removes action buttons, and displays a 'Reply sent!' confirmation.

    Args:
        slack_bot_token: Bot token for authentication.
        channel: Slack channel ID where the original message was posted.
        ts: Timestamp of the original Slack message.
        guest_name: Name of the guest.
        guest_msg: The guest's message.
        sent_reply: The actual reply that was sent to the guest.
        communication_type: Type of message (email, channel, etc).
        check_in: Check-in date.
        check_out: Check-out date.
        guest_count: Number of guests.
        status: Reservation status.
        detected_intent: The AI-classified intent.
    """

    slack_client = WebClient(token=slack_bot_token)

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
        {
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": f"> {guest_msg}"
            }
        },
        {
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": f"*Sent Reply:*\n>{sent_reply}"
            }
        },
        {
            "type": "context",
            "elements": [
                {
                    "type": "mrkdwn",
                    "text": f"*Intent:* `{detected_intent}`"
                }
            ]
        },
        {
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": ":white_check_mark: *Reply sent to guest!*"
            }
        }
    ]

    try:
        slack_client.chat_update(
            channel=channel,
            ts=ts,
            blocks=blocks,
            text="Reply sent to guest!"
        )
    except Exception as e:
        logging.error(f"❌ Failed to update Slack message with sent reply: {e}")

from openai import OpenAI
from utils import get_modal_blocks

logging.basicConfig(level=logging.INFO)
router = APIRouter()

SLACK_BOT_TOKEN = os.getenv("SLACK_BOT_TOKEN")
slack_client = WebClient(token=SLACK_BOT_TOKEN)
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
openai_client = OpenAI(api_key=OPENAI_API_KEY)

def add_undo_button(blocks, meta):
    """Adds an Undo AI button if previous_draft exists in meta."""
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

@router.post("/slack/actions")
async def slack_actions(request: Request):
    logging.info("🎯 /slack/actions endpoint hit!")
    form = await request.form()
    payload_raw = form.get("payload")
    if not payload_raw:
        logging.error("No payload found in Slack actions request!")
        return JSONResponse({})

    payload = json.loads(payload_raw)
    logging.info(f"Slack Interactivity Payload: {json.dumps(payload, indent=2)}")

    if payload.get("type") == "block_actions":
        action = payload["actions"][0]
        action_id = action.get("action_id")
        trigger_id = payload.get("trigger_id")
        user = payload.get("user", {})
        user_id = user.get("id", "")

        def get_meta_from_action(action):
            return json.loads(action["value"]) if "value" in action else {}

        # --- SEND ---
        if action_id == "send":
            meta = get_meta_from_action(action)
            reply = meta.get("reply", meta.get("ai_suggestion", "(No reply provided.)"))
            conv_id = meta.get("conv_id")
            communication_type = meta.get("type", "email")
            channel = meta.get("channel") or os.getenv("SLACK_CHANNEL")
            ts = meta.get("ts") or payload.get("message", {}).get("ts")

            if not reply or not conv_id:
                return JSONResponse({"text": "Missing reply or conversation ID."})

            try:
                success = send_reply_to_hostaway(conv_id, reply, communication_type)
            except Exception as e:
                logging.error(f"Slack SEND error: {e}")
                success = False

            # --- Update the Slack message, replacing the buttons with "Reply sent" ---
            if ts and channel:
                original_blocks = payload.get("message", {}).get("blocks", [])
                new_blocks = [block for block in original_blocks if block.get("type") != "actions"]
                new_blocks.append({
                    "type": "section",
                    "text": {
                        "type": "mrkdwn",
                        "text": ":white_check_mark: *Message sent!*" if success else ":x: *Failed to send reply.*"
                    }
                })
                try:
                    slack_client.chat_update(
                        channel=channel,
                        ts=ts,
                        blocks=new_blocks,
                        text="Message sent!" if success else "Failed to send reply."
                    )
                except Exception as e:
                    logging.error(f"Slack chat_update error: {e}")

            # --- Close the modal after send ---
            return JSONResponse({
                "response_action": "clear"
            })

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
                    draft_text="",
                    action_id="write_own",
                    checkbox_checked=checkbox_checked
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
                draft_text=ai_suggestion,
                action_id="edit",
                checkbox_checked=checkbox_checked
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

        # --- IMPROVE WITH AI ---
        if action_id == "improve_with_ai":
            view = payload.get("view", {})
            state = view.get("state", {}).get("values", {})
            reply_block = state.get("reply_input", {})
            edited_text = next((v.get("value") for v in reply_block.values() if v.get("value")), "")

            prev_checkbox = False
            state_save = state.get("save_answer_block", {})
            if "save_answer" in state_save and state_save["save_answer"].get("selected_options"):
                prev_checkbox = True

            meta = json.loads(view.get("private_metadata", "{}"))
            guest_name = meta.get("guest_name", "Guest")
            guest_msg = meta.get("guest_message", "")
            previous_draft = edited_text

            prompt = (
                "Take this guest message reply and improve it. "
                "Make it clear, modern, friendly, concise like a mmillennial, and make it make sense. "
                "Do not add extra content or use emojis. Only return the improved version.\n\n"
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
                logging.error(f"OpenAI error in 'improve_with_ai': {e}")
                improved = previous_draft
                error_message = f"Error improving with AI: {str(e)}"

            # Always pre-fill with improved, preserve checkbox, add "undo" to metadata if wanted
            new_meta = {
                **meta,
                "previous_draft": previous_draft,
                "checkbox_checked": prev_checkbox
            }
            blocks = get_modal_blocks(
                guest_name,
                guest_msg,
                draft_text=improved,
                action_id="edit",
                checkbox_checked=prev_checkbox
            )
            blocks = add_undo_button(blocks, new_meta)
            if error_message:
                blocks = [{
                    "type": "section",
                    "text": {"type": "mrkdwn", "text": f":warning: *{error_message}*"}
                }] + blocks

            new_modal = {
                "type": "modal",
                "title": {"type": "plain_text", "text": "AI Improved Reply", "emoji": True},
                "submit": {"type": "plain_text", "text": "Send", "emoji": True},
                "close": {"type": "plain_text", "text": "Cancel", "emoji": True},
                "private_metadata": json.dumps(new_meta),
                "blocks": blocks
            }

            try:
                slack_client.views_push(trigger_id=trigger_id, view=new_modal)
            except Exception as e:
                logging.error(f"Slack views_push error: {e}")

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
                draft_text=previous_draft,
                action_id="edit",
                checkbox_checked=checkbox_checked
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
            slack_client.views_push(trigger_id=trigger_id, view=modal)
            return JSONResponse({})

    # --- Modal submission handler (edit/send/write own) ---
    if payload.get("type") == "view_submission":
        view = payload.get("view", {})
        state = view.get("state", {}).get("values", {})
        meta = json.loads(view.get("private_metadata", "{}"))

        # Get reply text
        reply_text = None
        for block in state.values():
            if "reply" in block:
                reply_text = block["reply"]["value"]

        # Get checkbox state
        save_for_next_time = False
        for block in state.values():
            if "save_answer" in block and block["save_answer"].get("selected_options"):
                save_for_next_time = True

        conv_id = meta.get("conv_id") or meta.get("conversation_id")
        communication_type = meta.get("type", "email")
        guest_message = meta.get("guest_message", "")
        ai_suggestion = meta.get("ai_suggestion", "")

        try:
            send_reply_to_hostaway(conv_id, reply_text, communication_type)
            if save_for_next_time:
                listing_id = meta.get("listing_id")
                guest_id = meta.get("guest_id")
                store_learning_example(guest_message, ai_suggestion, reply_text, listing_id, guest_id)
        except Exception as e:
            logging.error(f"Slack regular send error: {e}")

        # --- Close the modal after send, and send message sent to Slack ---
        # (Slack already updates the thread as in the SEND block above.)
        return JSONResponse({
            "response_action": "clear"
        })

    return JSONResponse({"status": "ok"})
