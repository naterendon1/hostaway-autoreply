import json
import logging
from fastapi import APIRouter, Request, Form
from fastapi.responses import JSONResponse
from slack_sdk.web import WebClient
from utils import (
    save_ai_feedback,
    save_learning_example,
    save_custom_response,
    notify_admin_of_custom_response
)

router = APIRouter()
slack_client = WebClient()

@router.post("/slack/actions")
async def slack_actions(request: Request):
    logging.info("🎯 /slack/actions endpoint hit!")
    form = await request.form()
    payload = json.loads(form["payload"])
    action_id = payload["actions"][0]["action_id"]
    user = payload.get("user", {}).get("username", "unknown")

    action_value = payload["actions"][0].get("value")
    metadata = json.loads(action_value)
    conv_id = metadata.get("conv_id")
    listing_id = metadata.get("listing_id")
    guest_msg = metadata.get("guest_message")
    ai_suggestion = metadata.get("ai_suggestion")
    guest_id = metadata.get("guest_id")
    reply_type = metadata.get("type")
    guest_name = metadata.get("guest_name")

    if action_id == "rate_up":
        save_ai_feedback(conv_id, guest_msg, ai_suggestion, rating="up", user=user)
    elif action_id == "rate_down":
        save_ai_feedback(conv_id, guest_msg, ai_suggestion, rating="down", user=user)
    elif action_id == "edit":
        return JSONResponse(
            content={
                "response_action": "push",
                "view": {
                    "type": "modal",
                    "callback_id": "edit_modal",
                    "title": {"type": "plain_text", "text": "Edit Reply"},
                    "submit": {"type": "plain_text", "text": "Save"},
                    "blocks": [
                        {
                            "type": "input",
                            "block_id": "edit_block",
                            "label": {"type": "plain_text", "text": "Update the AI reply"},
                            "element": {
                                "type": "plain_text_input",
                                "action_id": "edit_input",
                                "multiline": True,
                                "initial_value": ai_suggestion
                            }
                        }
                    ],
                    "private_metadata": json.dumps(metadata)
                }
            }
        )
    elif action_id == "write_own":
        return JSONResponse(
            content={
                "response_action": "push",
                "view": {
                    "type": "modal",
                    "callback_id": "write_modal",
                    "title": {"type": "plain_text", "text": "Write Your Own"},
                    "submit": {"type": "plain_text", "text": "Save"},
                    "blocks": [
                        {
                            "type": "input",
                            "block_id": "write_block",
                            "label": {"type": "plain_text", "text": "Write a better reply"},
                            "element": {
                                "type": "plain_text_input",
                                "action_id": "write_input",
                                "multiline": True
                            }
                        }
                    ],
                    "private_metadata": json.dumps(metadata)
                }
            }
        )
    return {"status": "ok"}

@router.post("/slack/interactive")
async def slack_interactive(request: Request):
    form = await request.form()
    payload = json.loads(form["payload"])
    callback_id = payload["view"]["callback_id"]
    metadata = json.loads(payload["view"].get("private_metadata", "{}"))
    state = payload["view"]["state"]["values"]

    reply_text = ""
    if "edit_block" in state:
        reply_text = state["edit_block"]["edit_input"]["value"]
        save_learning_example(metadata["listing_id"], metadata["guest_message"], reply_text)
    elif "write_block" in state:
        reply_text = state["write_block"]["write_input"]["value"]
        save_custom_response(metadata["listing_id"], metadata["guest_message"], reply_text)
        notify_admin_of_custom_response(metadata, reply_text)

    return {"response_action": "clear"}
