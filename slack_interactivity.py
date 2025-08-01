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
from openai import OpenAI

logging.basicConfig(level=logging.INFO)
router = APIRouter()

SLACK_BOT_TOKEN = os.getenv("SLACK_BOT_TOKEN")
slack_client = WebClient(token=SLACK_BOT_TOKEN)
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
openai_client = OpenAI(api_key=OPENAI_API_KEY)

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
                # Get the original message blocks
                original_blocks = payload.get("message", {}).get("blocks", [])
                # Remove the actions block(s)
                new_blocks = [block for block in original_blocks if block.get("type") != "actions"]
                # Add a confirmation section at the end
                new_blocks.append({
                    "type": "section",
                    "text": {
                        "type": "mrkdwn",
                        "text": ":white_check_mark: *Reply sent to guest!*" if success else ":x: *Failed to send reply.*"
                    }
                })
                try:
                    slack_client.chat_update(
                        channel=channel,
                        ts=ts,
                        blocks=new_blocks,
                        text="Reply sent to guest!" if success else "Failed to send reply."
                    )
                except Exception as e:
                    logging.error(f"Slack chat_update error: {e}")

            return JSONResponse({"text": "Reply sent to guest!" if success else "Failed to send reply to guest."})

        # --- WRITE OWN ---
        if action_id == "write_own":
            meta = get_meta_from_action(action)
            guest_name = meta.get("guest_name", "Guest")
            guest_msg = meta.get("guest_message", "(Message unavailable)")
            modal = {
                "type": "modal",
                "title": {"type": "plain_text", "text": "Write Your Reply", "emoji": True},
                "submit": {"type": "plain_text", "text": "Send", "emoji": True},
                "close": {"type": "plain_text", "text": "Cancel", "emoji": True},
                "private_metadata": json.dumps(meta),
                "blocks": [
                    {
                        "type": "section",
                        "block_id": "guest_message_section",
                        "text": {"type": "mrkdwn", "text": f"*Guest*: {guest_name}\n*Message*: {guest_msg}"}
                    },
                    {
                        "type": "input",
                        "block_id": "reply_input",
                        "label": {"type": "plain_text", "text": "Your reply:", "emoji": True},
                        "element": {
                            "type": "plain_text_input",
                            "action_id": "reply",
                            "multiline": True
                        }
                    },
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
                    }
                ]
            }
            slack_client.views_open(trigger_id=trigger_id, view=modal)
            return JSONResponse({})

        # --- EDIT ---
        if action_id == "edit":
            meta = get_meta_from_action(action)
            guest_name = meta.get("guest_name", "Guest")
            guest_msg = meta.get("guest_message", "(Message unavailable)")
            ai_suggestion = meta.get("draft", meta.get("ai_suggestion", ""))
            modal = {
                "type": "modal",
                "title": {"type": "plain_text", "text": "Edit AI Reply", "emoji": True},
                "submit": {"type": "plain_text", "text": "Send", "emoji": True},
                "close": {"type": "plain_text", "text": "Cancel", "emoji": True},
                "private_metadata": json.dumps(meta),
                "blocks": [
                    {
                        "type": "section",
                        "block_id": "guest_message_section",
                        "text": {"type": "mrkdwn", "text": f"*Guest*: {guest_name}\n*Message*: {guest_msg}"}
                    },
                    {
                        "type": "input",
                        "block_id": "reply_input",
                        "label": {"type": "plain_text", "text": "Edit below:", "emoji": True},
                        "element": {
                            "type": "plain_text_input",
                            "action_id": "reply",
                            "multiline": True,
                            "initial_value": ai_suggestion
                        }
                    },
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
                    }
                ]
            }
            # Use views_push if already in a modal, else views_open
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

            meta = json.loads(view.get("private_metadata", "{}"))
            guest_name = meta.get("guest_name", "Guest")
            guest_msg = meta.get("guest_message", "")

            prompt = (
                "Take this guest message reply and improve it. "
                "Make it clear, modern, friendly, and concise. "
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
            except Exception as e:
                logging.error(f"OpenAI error in 'improve_with_ai': {e}")
                improved = "(Error generating improved message.)"

            new_modal = {
                "type": "modal",
                "title": {"type": "plain_text", "text": "AI Improved Reply", "emoji": True},
                "submit": {"type": "plain_text", "text": "Send", "emoji": True},
                "close": {"type": "plain_text", "text": "Cancel", "emoji": True},
                "private_metadata": view.get("private_metadata"),
                "blocks": [
                    {
                        "type": "section",
                        "block_id": "guest_message_section",
                        "text": {"type": "mrkdwn", "text": f"*Guest*: {guest_name}\n*Message*: {guest_msg}"}
                    },
                    {
                        "type": "input",
                        "block_id": "reply_input",
                        "label": {"type": "plain_text", "text": "Your improved reply:", "emoji": True},
                        "element": {
                            "type": "plain_text_input",
                            "action_id": "reply",
                            "multiline": True,
                            "initial_value": improved
                        }
                    },
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
                    {
                        "type": "context",
                        "elements": [
                            {
                                "type": "plain_text",
                                "text": f"Last AI improvement: {datetime.datetime.now().isoformat()}"
                            }
                        ]
                    }
                ]
            }

            try:
                slack_client.views_push(trigger_id=trigger_id, view=new_modal)
            except Exception as e:
                logging.error(f"Slack views_push error: {e}")

            return JSONResponse({})

    # --- Modal submission handler (edit/send/write own) ---
    if payload.get("type") == "view_submission":
        view = payload.get("view", {})
        state = view.get("state", {}).get("values", {})
        meta = json.loads(view.get("private_metadata", "{}"))

        if "reply_input" in state:
            reply_text = next(iter(state["reply_input"].values())).get("value")
            conv_id = meta.get("conv_id") or meta.get("conversation_id")
            communication_type = meta.get("type", "email")
            try:
                send_reply_to_hostaway(conv_id, reply_text, communication_type)
            except Exception as e:
                logging.error(f"Slack regular send error: {e}")
            return JSONResponse({"response_action": "clear"})

    return JSONResponse({"status": "ok"})
