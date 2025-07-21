from fastapi import APIRouter, Request
import os
import json
import logging
from slack_sdk.web import WebClient

router = APIRouter()

SLACK_BOT_TOKEN = os.getenv("SLACK_BOT_TOKEN")
SLACK_CHANNEL = os.getenv("SLACK_CHANNEL")
slack_client = WebClient(token=SLACK_BOT_TOKEN)

# -- OPTIONAL: Set up your learning DB here, e.g., SQLite, dict, or file

@router.post("/slack/actions")
async def slack_actions(request: Request):
    # Slack sends urlencoded form data: key=payload
    form_data = await request.form()
    payload = json.loads(form_data["payload"])
    payload_type = payload.get("type")
    user_id = payload.get("user", {}).get("id")
    user_name = payload.get("user", {}).get("username")
    team_id = payload.get("team", {}).get("id")

    # 1. Button interactions (Send, Edit, Write Your Own)
    if payload_type == "block_actions":
        action = payload["actions"][0]
        action_id = action["action_id"]
        value = json.loads(action["value"])

        # -- "Send" button
        if action_id == "send":
            reply = value["reply"]
            conv_id = value["conv_id"]
            msg_type = value["type"]
            # Send reply to Hostaway here (implement your API logic)
            # Optionally acknowledge in Slack
            slack_client.chat_postMessage(
                channel=payload["channel"]["id"],
                text=f":white_check_mark: Reply sent to guest!",
                thread_ts=payload["container"]["message_ts"]
            )
            return {"response_action": "clear"}

        # -- "Edit" button (show modal with prefilled text)
        elif action_id == "edit":
            draft = value["draft"]
            conv_id = value["conv_id"]
            msg_type = value["type"]
            # You can add guest_id, listing_id if available in `value`
            slack_client.views_open(
                trigger_id=payload["trigger_id"],
                view={
                    "type": "modal",
                    "callback_id": "edit_modal",
                    "title": {"type": "plain_text", "text": "Edit Reply"},
                    "submit": {"type": "plain_text", "text": "Send"},
                    "close": {"type": "plain_text", "text": "Cancel"},
                    "private_metadata": json.dumps(value),
                    "blocks": [
                        {
                            "type": "input",
                            "block_id": "reply_input",
                            "label": {"type": "plain_text", "text": "Edit your reply:"},
                            "element": {
                                "type": "plain_text_input",
                                "action_id": "reply",
                                "initial_value": draft,
                                "multiline": True,
                            },
                        }
                    ]
                }
            )
            return {"response_action": "clear"}

        # -- "Write Your Own" button (blank modal)
        elif action_id == "write_own":
            conv_id = value["conv_id"]
            msg_type = value["type"]
            # You can add guest_id, listing_id if available in `value`
            slack_client.views_open(
                trigger_id=payload["trigger_id"],
                view={
                    "type": "modal",
                    "callback_id": "write_own_modal",
                    "title": {"type": "plain_text", "text": "Write Your Own Reply"},
                    "submit": {"type": "plain_text", "text": "Send"},
                    "close": {"type": "plain_text", "text": "Cancel"},
                    "private_metadata": json.dumps(value),
                    "blocks": [
                        {
                            "type": "input",
                            "block_id": "reply_input",
                            "label": {"type": "plain_text", "text": "Your reply:"},
                            "element": {
                                "type": "plain_text_input",
                                "action_id": "reply",
                                "multiline": True,
                            },
                        }
                    ]
                }
            )
            return {"response_action": "clear"}

    # 2. Modal submission (edit or write_own)
    elif payload_type == "view_submission":
        callback_id = payload["view"]["callback_id"]
        values = payload["view"]["state"]["values"]
        reply = ""
        for block in values.values():
            reply = block.get("reply", {}).get("value", "")
            if reply:
                break

        # Retrieve conversation and context info
        metadata = json.loads(payload["view"].get("private_metadata", "{}"))
        conv_id = metadata.get("conv_id")
        msg_type = metadata.get("type")
        guest_id = metadata.get("guest_id")     # Add guest_id to value for ultra-personal learning
        listing_id = metadata.get("listing_id") # Add listing_id to value for learning by house
        guest_message = metadata.get("guest_message", "")

        # Learn: Save (guest_id, listing_id, guest_message, reply) for future answers
        # -- TODO: Insert into DB or your learning method here
        # Example: learn_from_user(guest_id, listing_id, guest_message, reply)

        # Send reply to Hostaway
        # TODO: Implement your Hostaway API call here

        # Confirm to Slack in thread (optional)
        if "thread_ts" in metadata and "channel" in metadata:
            slack_client.chat_postMessage(
                channel=metadata["channel"],
                text=":white_check_mark: Your reply was sent!",
                thread_ts=metadata["thread_ts"]
            )

        return {"response_action": "clear"}

    # Unknown or unhandled types
    else:
        logging.warning(f"Unhandled Slack action type: {payload_type}")
        return {"status": "ignored"}
