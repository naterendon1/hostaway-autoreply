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
    get_similar_learning_examples
)
from openai import OpenAI

logging.basicConfig(level=logging.INFO)
router = APIRouter()

SLACK_BOT_TOKEN = os.getenv("SLACK_BOT_TOKEN")
slack_client = WebClient(token=SLACK_BOT_TOKEN)
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
openai_client = OpenAI(api_key=OPENAI_API_KEY)

def clean_ai_reply(reply: str, property_type="home"):
    bad_signoffs = [
        "Enjoy your meal", "Enjoy your meals", "Enjoy!", "Best,", "Best regards,", "Cheers,", "Sincerely,", "[Your Name]", "Best", "Sincerely"
    ]
    for signoff in bad_signoffs:
        reply = reply.replace(signoff, "")
    lines = reply.split('\n')
    filtered_lines = []
    for line in lines:
        stripped = line.strip()
        if any(stripped.startswith(s.replace(",", "")) for s in ["Best", "Cheers", "Sincerely"]):
            continue
        if "[Your Name]" in stripped:
            continue
        filtered_lines.append(line)
    reply = ' '.join(filtered_lines)
    address_patterns = [
        r"(the )?house at [\d]+ [^,]+, [A-Za-z ]+",
        r"\d{3,} [A-Za-z0-9 .]+, [A-Za-z ]+",
        r"at [\d]+ [\w .]+, [\w ]+"
    ]
    for pattern in address_patterns:
        reply = re.sub(pattern, f"the {property_type}", reply, flags=re.IGNORECASE)
    reply = re.sub(r"at [A-Za-z0-9 ,/\-\(\)\']+", f"at the {property_type}", reply, flags=re.IGNORECASE)
    reply = ' '.join(reply.split())
    reply = reply.strip().replace(" ,", ",").replace(" .", ".")
    return reply.rstrip(",. ")

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

        if action_id == "edit":
            meta = get_meta_from_action(action)
            draft = clean_ai_reply(meta.get("draft", ""), "home")
            modal = {
                "type": "modal",
                "title": {"type": "plain_text", "text": "Edit Reply", "emoji": True},
                "submit": {"type": "plain_text", "text": "Send", "emoji": True},
                "close": {"type": "plain_text", "text": "Cancel", "emoji": True},
                "private_metadata": json.dumps(meta),
                "blocks": [
                    {
                        "type": "input",
                        "block_id": "reply_input",
                        "label": {"type": "plain_text", "text": "Edit your reply:", "emoji": True},
                        "element": {
                            "type": "plain_text_input",
                            "action_id": "reply",
                            "multiline": True,
                            "initial_value": draft
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

        if action_id == "send":
            meta = get_meta_from_action(action)
            conv_id = meta.get("conv_id")
            listing_id = meta.get("listing_id", None)
            reply = clean_ai_reply(meta.get("reply", ""), "home")
            type_ = meta.get("type")
            guest_message = meta.get("guest_message", "")
            guest_id = meta.get("guest_id", None)
            ai_suggestion = reply
            store_learning_example(guest_message, ai_suggestion, reply, listing_id, guest_id)
            send_ok = send_reply_to_hostaway(conv_id, reply, type_)
            msg = ":white_check_mark: AI reply sent and saved!" if send_ok else ":warning: Failed to send."
            return JSONResponse({
                "response_action": "update",
                "view": {
                    "type": "modal",
                    "title": {"type": "plain_text", "text": "Done!", "emoji": True},
                    "close": {"type": "plain_text", "text": "Close", "emoji": True},
                    "blocks": [
                        {"type": "section", "text": {"type": "mrkdwn", "text": msg}}
                    ]
                }
            })

        return JSONResponse({"text": "Action received."})

    if payload.get("type") == "view_submission":
        view = payload.get("view", {})
        state = view.get("state", {}).get("values", {})
        reply_block = state.get("reply_input", {})
        user_text = None
        for v in reply_block.values():
            user_text = v.get("value")
        meta = json.loads(view.get("private_metadata", "{}"))
        conv_id = meta.get("conv_id")
        listing_id = meta.get("listing_id")
        guest_message = meta.get("guest_message", "")
        type_ = meta.get("type")
        guest_id = meta.get("guest_id")
        ai_suggestion = meta.get("ai_suggestion", "")

        clean_reply = clean_ai_reply(user_text, "home")
        store_learning_example(guest_message, ai_suggestion, clean_reply, listing_id, guest_id)
        send_ok = send_reply_to_hostaway(conv_id, clean_reply, type_)
        msg = ":white_check_mark: Your reply was sent!" if send_ok else ":warning: Failed to send."
        return JSONResponse({
            "response_action": "update",
            "view": {
                "type": "modal",
                "title": {"type": "plain_text", "text": "Done!", "emoji": True},
                "close": {"type": "plain_text", "text": "Close", "emoji": True},
                "blocks": [
                    {"type": "section", "text": {"type": "mrkdwn", "text": msg}}
                ]
            }
        })

    return JSONResponse({"text": "Action received."})
