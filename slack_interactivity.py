# slack_interactivity.py

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
import json
import logging
import os

from utils import send_reply_to_hostaway, store_learning_example

from slack_sdk.web import WebClient

SLACK_BOT_TOKEN = os.getenv("SLACK_BOT_TOKEN")
slack_client = WebClient(token=SLACK_BOT_TOKEN)

router = APIRouter()

@router.post("/slack/actions")
async def slack_actions(request: Request):
    payload = await request.form()
    data = json.loads(payload["payload"])

    # Distinguish between block_actions and view_submission
    action_type = data.get("type")

    # --- Button Clicks ---
    if action_type == "block_actions":
        action = data["actions"][0]
        action_id = action["action_id"]

        # Extract channel and thread_ts if you want to use threads
        channel_id = data.get("channel", {}).get("id")
        thread_ts = data.get("message", {}).get("ts")

        if action_id == "send":
            val = json.loads(action["value"])
            reply = val["reply"]
            conv_id = val["conv_id"]
            comm_type = val.get("type", "email")

            success = send_reply_to_hostaway(conv_id, reply, comm_type)
            # Update Slack message with feedback
            slack_client.chat_postMessage(
                channel=channel_id,
                text="✅ Reply sent to guest!",
                thread_ts=thread_ts
            )
            return JSONResponse({"ok": True})

        elif action_id == "edit":
            val = json.loads(action["value"])
            draft = val["draft"]
            conv_id = val["conv_id"]
            comm_type = val.get("type", "email")
            listing_id = val.get("listing_id")
            guest_message = val.get("guest_message")
            ai_suggestion = draft  # The suggestion is the draft here
            guest_id = val.get("guest_id")

            # Open a Slack modal for editing
            slack_client.views_open(
                trigger_id=data["trigger_id"],
                view={
                    "type": "modal",
                    "callback_id": "edit_reply_modal",
                    "private_metadata": json.dumps({
                        "conv_id": conv_id,
                        "listing_id": listing_id,
                        "guest_message": guest_message,
                        "ai_suggestion": ai_suggestion,
                        "comm_type": comm_type,
                        "guest_id": guest_id,
                        "thread_ts": thread_ts
                    }),
                    "title": {"type": "plain_text", "text": "Edit Reply"},
                    "submit": {"type": "plain_text", "text": "Send"},
                    "close": {"type": "plain_text", "text": "Cancel"},
                    "blocks": [
                        {
                            "type": "input",
                            "block_id": "reply_input",
                            "label": {"type": "plain_text", "text": "Your reply:"},
                            "element": {
                                "type": "plain_text_input",
                                "action_id": "reply",
                                "initial_value": draft,
                                "multiline": True
                            }
                        }
                    ]
                }
            )
            return JSONResponse({"ok": True})

        elif action_id == "write_own":
            val = json.loads(action["value"])
            conv_id = val["conv_id"]
            comm_type = val.get("type", "email")
            listing_id = val.get("listing_id")
            guest_message = val.get("guest_message")
            ai_suggestion = val.get("ai_suggestion", "")
            guest_id = val.get("guest_id")

            # Open a Slack modal for writing your own reply
            slack_client.views_open(
                trigger_id=data["trigger_id"],
                view={
                    "type": "modal",
                    "callback_id": "write_own_modal",
                    "private_metadata": json.dumps({
                        "conv_id": conv_id,
                        "listing_id": listing_id,
                        "guest_message": guest_message,
                        "ai_suggestion": ai_suggestion,
                        "comm_type": comm_type,
                        "guest_id": guest_id,
                        "thread_ts": thread_ts
                    }),
                    "title": {"type": "plain_text", "text": "Write Your Own Reply"},
                    "submit": {"type": "plain_text", "text": "Send"},
                    "close": {"type": "plain_text", "text": "Cancel"},
                    "blocks": [
                        {
                            "type": "input",
                            "block_id": "reply_input",
                            "label": {"type": "plain_text", "text": "Your reply:"},
                            "element": {
                                "type": "plain_text_input",
                                "action_id": "reply",
                                "initial_value": "",
                                "multiline": True
                            }
                        }
                    ]
                }
            )
            return JSONResponse({"ok": True})

    # --- Modal Submissions (Edit or Write Your Own) ---
    elif action_type == "view_submission":
        view = data["view"]
        private_metadata = json.loads(view.get("private_metadata", "{}"))
        user_reply = view["state"]["values"]["reply_input"]["reply"]["value"]

        conv_id = private_metadata.get("conv_id")
        listing_id = private_metadata.get("listing_id")
        guest_message = private_metadata.get("guest_message", "")
        ai_suggestion = private_metadata.get("ai_suggestion", "")
        comm_type = private_metadata.get("comm_type", "email")
        guest_id = private_metadata.get("guest_id")
        thread_ts = private_metadata.get("thread_ts")
        channel_id = data.get("user", {}).get("id")  # Fallback, you might want to get actual channel elsewhere

        # Send to Hostaway
        send_reply_to_hostaway(conv_id, user_reply, comm_type)

        # Store for learning
        try:
            store_learning_example(
                guest_message=guest_message,
                ai_suggestion=ai_suggestion,
                user_reply=user_reply,
                listing_id=listing_id,
                guest_id=guest_id
            )
            logging.info("✅ Learning example stored")
        except Exception as e:
            logging.error(f"❌ Error storing learning example: {e}")

        # Optionally update Slack thread or send confirmation
        if thread_ts and channel_id:
            try:
                slack_client.chat_postMessage(
                    channel=channel_id,
                    text="✅ Your reply was sent and saved for future learning!",
                    thread_ts=thread_ts
                )
            except Exception as e:
                logging.error(f"❌ Slack message update error: {e}")

        return JSONResponse({"response_action": "clear"})

    return JSONResponse({"ok": True})
