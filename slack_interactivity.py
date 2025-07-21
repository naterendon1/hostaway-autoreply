from fastapi import APIRouter, Request
import os
import logging
import json
from slack_sdk.web import WebClient
from utils import send_reply_to_hostaway, store_learning_example

router = APIRouter()
SLACK_BOT_TOKEN = os.getenv("SLACK_BOT_TOKEN")
slack_client = WebClient(token=SLACK_BOT_TOKEN)

@router.post("/slack/actions")
async def slack_actions(request: Request):
    payload = await request.form()
    payload = json.loads(payload["payload"])

    # All action payloads should contain channel and thread_ts (from button value or modal metadata)
    payload_type = payload.get("type")
    user_id = payload.get("user", {}).get("id")
    team_id = payload.get("team", {}).get("id")
    logging.info(f"Slack action payload: {json.dumps(payload, indent=2)}")

    # --- Handle BUTTON CLICKS ---
    if payload_type == "block_actions":
        action = payload["actions"][0]
        action_id = action.get("action_id")
        value = json.loads(action.get("value", "{}"))
        channel = payload["channel"]["id"]
        thread_ts = payload.get("message", {}).get("ts")
        guest_message = None

        # Try to get guest message for learning or context (if available)
        for block in payload["message"].get("blocks", []):
            if block.get("block_id", "").startswith("guest_msg") or "> " in block.get("text", {}).get("text", ""):
                guest_message = block.get("text", {}).get("text", "").replace("> ", "")

        # --- SEND Button: send to Hostaway, confirm in Slack thread ---
        if action_id == "send":
            reply = value["reply"]
            conv_id = value.get("conv_id")
            comm_type = value.get("type")
            guest_id = value.get("guest_id")
            listing_id = value.get("listing_id")
            send_success = send_reply_to_hostaway(conv_id, reply, comm_type)
            # You could also log this as a "learning" event here
            if send_success:
                slack_client.chat_postMessage(
                    channel=channel,
                    text=":white_check_mark: Reply sent to guest.",
                    thread_ts=thread_ts
                )
            else:
                slack_client.chat_postMessage(
                    channel=channel,
                    text=":x: Failed to send reply to guest.",
                    thread_ts=thread_ts
                )
            return {"ok": True}

        # --- EDIT Button: open a modal with the draft for editing ---
        elif action_id == "edit":
            draft = value.get("draft", "")
            conv_id = value.get("conv_id")
            comm_type = value.get("type")
            guest_id = value.get("guest_id")
            listing_id = value.get("listing_id")
            slack_client.views_open(
                trigger_id=payload["trigger_id"],
                view={
                    "type": "modal",
                    "callback_id": "edit_reply_modal",
                    "title": {"type": "plain_text", "text": "Edit Reply"},
                    "submit": {"type": "plain_text", "text": "Send"},
                    "close": {"type": "plain_text", "text": "Cancel"},
                    "private_metadata": json.dumps({
                        "conv_id": conv_id,
                        "type": comm_type,
                        "channel": channel,
                        "thread_ts": thread_ts,
                        "guest_message": guest_message,
                        "guest_id": guest_id,
                        "listing_id": listing_id,
                    }),
                    "blocks": [
                        {
                            "type": "input",
                            "block_id": "reply_input",
                            "element": {
                                "type": "plain_text_input",
                                "action_id": "reply",
                                "initial_value": draft,
                                "multiline": True
                            },
                            "label": {"type": "plain_text", "text": "Your reply:"}
                        }
                    ]
                }
            )
            return {"ok": True}

        # --- WRITE OWN: open a blank modal for writing custom reply ---
        elif action_id == "write_own":
            conv_id = value.get("conv_id")
            comm_type = value.get("type")
            guest_id = value.get("guest_id")
            listing_id = value.get("listing_id")
            slack_client.views_open(
                trigger_id=payload["trigger_id"],
                view={
                    "type": "modal",
                    "callback_id": "write_own_modal",
                    "title": {"type": "plain_text", "text": "Write Your Own Reply"},
                    "submit": {"type": "plain_text", "text": "Send"},
                    "close": {"type": "plain_text", "text": "Cancel"},
                    "private_metadata": json.dumps({
                        "conv_id": conv_id,
                        "type": comm_type,
                        "channel": channel,
                        "thread_ts": thread_ts,
                        "guest_message": guest_message,
                        "guest_id": guest_id,
                        "listing_id": listing_id,
                    }),
                    "blocks": [
                        {
                            "type": "input",
                            "block_id": "reply_input",
                            "element": {
                                "type": "plain_text_input",
                                "action_id": "reply",
                                "multiline": True
                            },
                            "label": {"type": "plain_text", "text": "Your reply:"}
                        }
                    ]
                }
            )
            return {"ok": True}

    # --- Handle MODAL SUBMISSIONS ---
    elif payload_type == "view_submission":
        view = payload["view"]
        callback_id = view["callback_id"]
        state_values = view["state"]["values"]
        reply = ""
        for block in state_values.values():
            reply = block.get("reply", {}).get("value", "")

        metadata = json.loads(view.get("private_metadata", "{}"))
        conv_id = metadata.get("conv_id")
        comm_type = metadata.get("type")
        channel = metadata.get("channel")
        thread_ts = metadata.get("thread_ts")
        guest_message = metadata.get("guest_message")
        guest_id = metadata.get("guest_id")
        listing_id = metadata.get("listing_id")

        # Send reply to Hostaway
        send_success = send_reply_to_hostaway(conv_id, reply, comm_type)

        # Learn from user's custom reply (write_own or edit)
        store_learning_example(
            guest_message=guest_message,
            ai_suggestion=None,
            user_reply=reply,
            listing_id=listing_id,
            guest_id=guest_id,
        )

        if send_success:
            slack_client.chat_postMessage(
                channel=channel,
                text=":white_check_mark: Reply sent to guest.",
                thread_ts=thread_ts
            )
        else:
            slack_client.chat_postMessage(
                channel=channel,
                text=":x: Failed to send reply to guest.",
                thread_ts=thread_ts
            )
        return {"response_action": "clear"}  # Closes modal

    return {"ok": True}

# --- Slack Events (noop or challenge handler) ---
@router.post("/slack/events")
async def slack_events(request: Request):
    payload = await request.json()
    # If this is a Slack URL verification (first time config), reply with the challenge
    if payload.get("type") == "url_verification":
        return {"challenge": payload.get("challenge")}
    # Optionally process events here, or just return ok
    return {"ok": True}
