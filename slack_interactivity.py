# slack_interactivity.py

import os
import json
from fastapi import APIRouter, Request
from slack_sdk import WebClient
from slack_sdk.signature import SignatureVerifier
from utils import (
    store_learning_example,
    store_ai_feedback,
)

router = APIRouter()

SLACK_SIGNING_SECRET = os.getenv("SLACK_SIGNING_SECRET")
SLACK_BOT_TOKEN = os.getenv("SLACK_BOT_TOKEN")
slack_client = WebClient(token=SLACK_BOT_TOKEN)
verifier = SignatureVerifier(signing_secret=SLACK_SIGNING_SECRET)

@router.post("/slack/interactivity")
async def handle_interactivity(request: Request):
    body = await request.body()
    if not verifier.is_valid_request(body, request.headers):
        return {"status": "invalid request"}

    payload = json.loads(request.form()['payload'])
    action = payload.get("actions", [{}])[0]
    action_id = action.get("action_id")
    value = json.loads(action.get("value", "{}"))
    user = payload.get("user", {}).get("username", "unknown")
    response_url = payload.get("response_url")

    conv_id = value.get("conv_id")
    guest_msg = value.get("guest_message", "")
    ai_reply = value.get("ai_suggestion", "")
    action_type = value.get("action")

    if action_id == "send":
        slack_client.chat_postMessage(channel=value["conv_id"], text=ai_reply)
    elif action_id == "edit":
        slack_client.chat_postMessage(channel=value["conv_id"], text="Please edit and resend manually.")
    elif action_id == "write_own":
        slack_client.chat_postMessage(channel=value["conv_id"], text="You chose to write your own response.")
    elif action_id == "rate_up":
        store_ai_feedback(conv_id, guest_msg, ai_reply, "up", user)
        slack_client.chat_postMessage(channel=value["conv_id"], text="👍 Thanks for the feedback!")
    elif action_id == "rate_down":
        store_ai_feedback(conv_id, guest_msg, ai_reply, "down", user)
        slack_client.chat_postMessage(channel=value["conv_id"], text="👎 Thanks, we'll try to improve!")

    return {"status": "ok"}
