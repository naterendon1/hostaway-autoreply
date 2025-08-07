import os
import logging
import json
from fastapi import APIRouter, Request, BackgroundTasks
from fastapi.responses import JSONResponse
from slack_sdk import WebClient
from openai import OpenAI
from utils import (
    send_reply_to_hostaway,
    fetch_hostaway_resource,
    store_learning_example,
    get_similar_learning_examples,
    clean_ai_reply,
    # DO NOT import get_modal_blocks here (we patch it below)
)

logging.basicConfig(level=logging.INFO)
router = APIRouter()

SLACK_BOT_TOKEN = os.getenv("SLACK_BOT_TOKEN")
slack_client = WebClient(token=SLACK_BOT_TOKEN)
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
openai_client = OpenAI(api_key=OPENAI_API_KEY)

# --------- PATCHED MODAL BLOCKS (Inline helper, do not import from utils) ----------
def get_modal_blocks(guest_name, guest_msg, action_id, draft_text="", checkbox_checked=False):
    reply_block = {
        "type": "input",
        "block_id": "reply_input",
        "label": {"type": "plain_text", "text": "Your reply:" if action_id == "write_own" else "Edit below:", "emoji": True},
        "element": {
            "type": "plain_text_input",
            "action_id": "reply",
            "multiline": True,
        }
    }
    if action_id == "edit" and draft_text:
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
    # Patch: Only set initial_options if checked
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
# -------------------------------------------------------------------

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

# Background task to improve text and update the modal asynchronously
def _background_improve_and_update(view_id, meta, edited_text, guest_name, guest_msg):
    prompt = (
        "Take this guest message reply and improve it. "
        "Make it clear, modern, friendly, concise, and natural. "
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
        logging.error(f"OpenAI error in background 'improve_with_ai': {e}")
        improved = edited_text
        error_message = f"Error improving with AI: {str(e)}"

    new_meta = {
        **meta,
        "previous_draft": edited_text,
    }
    blocks = get_modal_blocks(
        guest_name,
        guest_msg,
        action_id="edit",
        draft_text=improved,
        checkbox_checked=meta.get("checkbox_checked", False)
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
        slack_client.views_update(view_id=view_id, view=new_modal)
    except Exception as e:
        logging.error(f"Slack views_update error: {e}")

@router.post("/slack/actions")
async def slack_actions(request: Request, background_tasks: BackgroundTasks):
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
            guest_name = meta.get("guest_name", "Guest")
            guest_msg = meta.get("guest_message", "(Message unavailable)")
            check_in = meta.get("check_in", "N/A")
            check_out = meta.get("check_out", "N/A")
            guest_count = meta.get("guest_count", "N/A")
            status = meta.get("status", "Unknown")
            detected_intent = meta.get("detected_intent", "Unknown")

            if not reply or not conv_id:
                return JSONResponse({"text": "Missing reply or conversation ID."})

            try:
                success = send_reply_to_hostaway(conv_id, reply, communication_type)
            except Exception as e:
                logging.error(f"Slack SEND error: {e}")
                success = False

            # --- Update Slack thread with *actual sent reply* and close modal ---
            if ts and channel and success:
                update_slack_message_with_sent_reply(
                    slack_bot_token=SLACK_BOT_TOKEN,
                    channel=channel,
                    ts=ts,
                    guest_name=guest_name,
                    guest_msg=guest_msg,
                    sent_reply=reply,
                    communication_type=communication_type,
                    check_in=check_in,
                    check_out=check_out,
                    guest_count=guest_count,
                    status=status,
                    detected_intent=detected_intent
                )
            elif ts and channel and not success:
                try:
                    # Just show error in the thread
                    slack_client.chat_update(
                        channel=channel,
                        ts=ts,
                        blocks=[
                            {
                                "type": "section",
                                "text": {
                                    "type": "mrkdwn",
                                    "text": ":x: *Failed to send reply.*"
                                }
                            }
                        ],
                        text="Failed to send reply."
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
                    action_id="write_own",
                    draft_text="",
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
                action_id="edit",
                draft_text=ai_suggestion,
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
            view_id = view.get("id")
            state = view.get("state", {}).get("values", {})
            reply_block = state.get("reply_input", {})
            edited_text = next((v.get("value") for v in reply_block.values() if v.get("value")), "")

            state_save = state.get("save_answer_block", {})
            checkbox_checked = False
            if "save_answer" in state_save and state_save["save_answer"].get("selected_options"):
                checkbox_checked = True

            meta = json.loads(view.get("private_metadata", "{}"))
            guest_name = meta.get("guest_name", "Guest")
            guest_msg = meta.get("guest_message", "")

            # Immediately update modal to show working state
            working_blocks = [
                {"type": "section", "text": {"type": "mrkdwn", "text": ":hourglass_flowing_sand: Improving your reply..."}}
            ] + get_modal_blocks(
                guest_name,
                guest_msg,
                action_id="edit",
                draft_text=edited_text,
                checkbox_checked=checkbox_checked
            )
            working_modal = {
                "type": "modal",
                "title": {"type": "plain_text", "text": "Improving Reply", "emoji": True},
                "submit": {"type": "plain_text", "text": "Send", "emoji": True},
                "close": {"type": "plain_text", "text": "Cancel", "emoji": True},
                "private_metadata": json.dumps({**meta, "checkbox_checked": checkbox_checked}),
                "blocks": working_blocks
            }

            # Kick off background improvement and update
            background_tasks.add_task(
                _background_improve_and_update,
                view_id,
                {**meta, "checkbox_checked": checkbox_checked},
                edited_text,
                guest_name,
                guest_msg,
            )

            return JSONResponse({
                "response_action": "update",
                "view": working_modal
            })

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
        channel = meta.get("channel") or os.getenv("SLACK_CHANNEL")
        ts = meta.get("ts")

        try:
            send_reply_to_hostaway(conv_id, reply_text, communication_type)
            # Update the Slack message thread with the ACTUAL sent reply
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

        return JSONResponse({
            "response_action": "clear"
        })

    return JSONResponse({"status": "ok"})
