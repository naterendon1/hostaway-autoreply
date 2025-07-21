from fastapi import APIRouter, Request
import os
import logging
import json
from utils import send_reply_to_hostaway  # Your function to send messages to Hostaway
from db import save_custom_response  # From your db.py

router = APIRouter()

SLACK_SIGNING_SECRET = os.getenv("SLACK_SIGNING_SECRET")
SLACK_BOT_TOKEN = os.getenv("SLACK_BOT_TOKEN")

from slack_sdk.web import WebClient
slack_client = WebClient(token=SLACK_BOT_TOKEN)

@router.post("/slack/actions")
async def slack_actions(request: Request):
    payload = await request.form()
    payload = json.loads(payload["payload"])
    logging.info(f"Slack action payload: {json.dumps(payload, indent=2)}")

    action = payload["actions"][0]
    action_id = action["action_id"]
    value = json.loads(action["value"])
    channel_id = payload["channel"]["id"]
    thread_ts = payload["container"].get("thread_ts") or payload["container"].get("message_ts")
    message_blocks = payload.get("message", {}).get("blocks", [])
    guest_message = None
    listing_id = None

    # Try to extract guest message and listing_id from the Slack blocks, if possible
    for block in message_blocks:
        if block["type"] == "section" and block["text"]["type"] == "mrkdwn":
            text = block["text"]["text"]
            if text.startswith("> "):
                guest_message = text[2:]
            elif "at *" in text and "*" in text:
                # Example: "*New Airbnb* from *Robert Lawrence* at *King Bed, Fireplace. Sleeps 8*"
                pass  # Listing name is not strictly needed here
    # Get listing_id from the button value, if available
    listing_id = value.get("listing_id")

    # If not in value, try to pull from conversation ID by API (optional, add if needed)

    if action_id == "send":
        # This can be AI reply, or an edited/custom reply by the user
        reply_text = value.get("reply")
        conv_id = value.get("conv_id")
        msg_type = value.get("type")
        # Save to learning DB if this is an edited or custom reply
        if value.get("is_custom") or value.get("is_edit"):  # You can set these flags from your button payloads if needed
            if listing_id and guest_message and reply_text:
                save_custom_response(listing_id, guest_message, reply_text)
        # Actually send the message to Hostaway (update as needed)
        success = send_reply_to_hostaway(conv_id, reply_text, msg_type)
        # Respond in Slack thread
        if success:
            slack_client.chat_postMessage(
                channel=channel_id,
                thread_ts=thread_ts,
                text=":white_check_mark: Reply sent to Hostaway."
            )
        else:
            slack_client.chat_postMessage(
                channel=channel_id,
                thread_ts=thread_ts,
                text=":x: Error sending reply to Hostaway."
            )
        return {"ok": True}

    elif action_id == "edit":
        # When user clicks "Edit" on an AI reply, open a modal or post a new message with a text box pre-filled
        reply_draft = value.get("draft", "")
        # Use Slack modals for editing
        slack_client.views_open(
            trigger_id=payload["trigger_id"],
            view={
                "type": "modal",
                "title": {"type": "plain_text", "text": "Edit Reply"},
                "callback_id": "edit_reply_modal",
                "private_metadata": json.dumps({
                    "conv_id": value.get("conv_id"),
                    "listing_id": listing_id,
                    "guest_message": guest_message,
                    "type": value.get("type"),
                    "thread_ts": thread_ts,
                }),
                "blocks": [
                    {
                        "type": "input",
                        "block_id": "reply_input",
                        "label": {"type": "plain_text", "text": "Edit your reply:"},
                        "element": {
                            "type": "plain_text_input",
                            "action_id": "reply",
                            "multiline": True,
                            "initial_value": reply_draft
                        }
                    }
                ],
                "submit": {"type": "plain_text", "text": "Send"},
                "close": {"type": "plain_text", "text": "Cancel"}
            }
        )
        return {"ok": True}

    elif action_id == "write_own":
        # When user wants to write their own reply, open a modal with a blank text box
        slack_client.views_open(
            trigger_id=payload["trigger_id"],
            view={
                "type": "modal",
                "title": {"type": "plain_text", "text": "Write Your Own Reply"},
                "callback_id": "write_own_modal",
                "private_metadata": json.dumps({
                    "conv_id": value.get("conv_id"),
                    "listing_id": listing_id,
                    "guest_message": guest_message,
                    "type": value.get("type"),
                    "thread_ts": thread_ts,
                }),
                "blocks": [
                    {
                        "type": "input",
                        "block_id": "reply_input",
                        "label": {"type": "plain_text", "text": "Your reply:"},
                        "element": {
                            "type": "plain_text_input",
                            "action_id": "reply",
                            "multiline": True,
                            "initial_value": ""
                        }
                    }
                ],
                "submit": {"type": "plain_text", "text": "Send"},
                "close": {"type": "plain_text", "text": "Cancel"}
            }
        )
        return {"ok": True}

    return {"ok": False}


@router.post("/slack/interactivity")
async def slack_interactivity(request: Request):
    # Handles modals (edit & write your own) submissions
    payload = await request.form()
    payload = json.loads(payload["payload"])
    callback_id = payload["view"]["callback_id"]
    private_metadata = json.loads(payload["view"]["private_metadata"])
    reply_text = payload["view"]["state"]["values"]["reply_input"]["reply"]["value"]

    conv_id = private_metadata.get("conv_id")
    listing_id = private_metadata.get("listing_id")
    guest_message = private_metadata.get("guest_message")
    msg_type = private_metadata.get("type")
    thread_ts = private_metadata.get("thread_ts")
    channel_id = payload["user"]["id"] if not thread_ts else None

    # Save ALL edits or custom replies to the learning DB
    if listing_id and guest_message and reply_text:
        save_custom_response(listing_id, guest_message, reply_text)

    # Send to Hostaway
    success = send_reply_to_hostaway(conv_id, reply_text, msg_type)

    # Acknowledge in the original thread (if thread_ts is available)
    if success:
        slack_client.chat_postMessage(
            channel=private_metadata.get("channel_id"),
            thread_ts=thread_ts,
            text=":white_check_mark: Reply sent to Hostaway."
        )
    else:
        slack_client.chat_postMessage(
            channel=private_metadata.get("channel_id"),
            thread_ts=thread_ts,
            text=":x: Error sending reply to Hostaway."
        )

    return {"ok": True}
