import os
import logging
import json
import re
from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from slack_sdk import WebClient
from utils import (
    send_reply_to_hostaway,
    fetch_hostaway_resource,
    store_learning_example,
    get_similar_learning_examples,
    store_clarification_log
)
from openai import OpenAI

logging.basicConfig(level=logging.INFO)
router = APIRouter()

SLACK_BOT_TOKEN = os.getenv("SLACK_BOT_TOKEN")
slack_client = WebClient(token=SLACK_BOT_TOKEN)
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
openai_client = OpenAI(api_key=OPENAI_API_KEY)

# === AI Clarification Trigger ===
def needs_clarification(reply: str) -> bool:
    return any(phrase in reply.lower() for phrase in [
        "i'm not sure", "i don't know", "let me check", "can't find that info",
        "need to verify", "need to ask", "unsure"
    ])

# === Ask Host Modal ===
def ask_host_for_clarification(guest_msg, metadata, trigger_id):
    slack_client.views_open(
        trigger_id=trigger_id,
        view={
            "type": "modal",
            "title": {"type": "plain_text", "text": "Need Your Help", "emoji": True},
            "submit": {"type": "plain_text", "text": "Submit", "emoji": True},
            "close": {"type": "plain_text", "text": "Cancel", "emoji": True},
            "private_metadata": json.dumps(metadata),
            "blocks": [
                {
                    "type": "section",
                    "text": {"type": "mrkdwn", "text": f"The guest asked: *{guest_msg}*\nI couldn't confidently answer this. Can you help me out?"}
                },
                {
                    "type": "input",
                    "block_id": "clarify_input",
                    "label": {"type": "plain_text", "text": "What should I tell the guest?", "emoji": True},
                    "element": {
                        "type": "plain_text_input",
                        "action_id": "clarify_text",
                        "multiline": True
                    }
                },
                {
                    "type": "input",
                    "block_id": "clarify_tag",
                    "label": {"type": "plain_text", "text": "Tag this clarification (e.g. wifi, parking, etc)", "emoji": True},
                    "element": {
                        "type": "plain_text_input",
                        "action_id": "clarify_tag_input",
                        "multiline": False
                    }
                }
            ]
        }
    )

# === Apply Clarification ===
def generate_reply_with_clarification(guest_msg, host_clarification):
    prompt = (
        "A guest asked a question, and the host provided clarification. Based on both, write a helpful, clear reply.\n\n"
        f"Guest: {guest_msg}\n"
        f"Host clarification: {host_clarification}\n"
        "Reply:"
    )
    try:
        response = openai_client.chat.completions.create(
            model="gpt-4",
            messages=[
                {"role": "system", "content": "You are a helpful vacation rental assistant."},
                {"role": "user", "content": prompt}
            ]
        )
        return response.choices[0].message.content.strip()
    except Exception as e:
        logging.error(f"Clarify AI generation failed: {e}")
        return "(Error generating response from clarification.)"

# === Main Slack Interactivity ===
@router.post("/slack/actions")
async def slack_actions(request: Request):
    form = await request.form()
    payload = json.loads(form.get("payload"))
    logging.info(f"Slack Interactivity Payload: {json.dumps(payload, indent=2)}")

    if payload.get("type") == "block_actions":
        action = payload["actions"][0]
        action_id = action.get("action_id")
        trigger_id = payload.get("trigger_id")
        user = payload["user"]
        user_id = user.get("id")
        logging.info(f"Slack action: {action_id} by {user_id}")

        def get_meta_from_action(action):
            return json.loads(action["value"]) if "value" in action else {}

        if action_id == "write_own":
            meta = get_meta_from_action(action)
            modal = {
                "type": "modal",
                "title": {"type": "plain_text", "text": "Write Your Own Reply", "emoji": True},
                "submit": {"type": "plain_text", "text": "Send", "emoji": True},
                "close": {"type": "plain_text", "text": "Cancel", "emoji": True},
                "private_metadata": json.dumps(meta),
                "blocks": [
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
                                "text": {"type": "plain_text", "text": ":rocket: Improve with AI", "emoji": True}
                            }
                        ]
                    }
                ]
            }
            slack_client.views_open(trigger_id=trigger_id, view=modal)
            return JSONResponse({})

        if action_id == "clarify_submission":
            pass  # handled below via view_submission

    if payload.get("type") == "view_submission":
        view = payload.get("view", {})
        state = view.get("state", {}).get("values", {})
        meta = json.loads(view.get("private_metadata", "{}"))

        if "clarify_input" in state:
            clarify_block = state.get("clarify_input", {})
            clarification_text = list(clarify_block.values())[0].get("value")
            tag_block = state.get("clarify_tag", {})
            clarification_tag = list(tag_block.values())[0].get("value")
            guest_msg = meta.get("guest_message", "")
            listing_id = meta.get("listing_id")
            guest_id = meta.get("guest_id")
            store_clarification_log(guest_msg, listing_id, guest_id, clarification_tag)
            improved = generate_reply_with_clarification(guest_msg, clarification_text)
            return JSONResponse({
                "response_action": "update",
                "view": {
                    "type": "modal",
                    "title": {"type": "plain_text", "text": "Improved Reply", "emoji": True},
                    "submit": {"type": "plain_text", "text": "Send", "emoji": True},
                    "close": {"type": "plain_text", "text": "Cancel", "emoji": True},
                    "private_metadata": json.dumps(meta),
                    "blocks": [
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
                                    "text": {"type": "plain_text", "text": ":rocket: Improve with AI", "emoji": True}
                                }
                            ]
                        }
                    ]
                }
            })

    return JSONResponse({"text": "Action received."})
