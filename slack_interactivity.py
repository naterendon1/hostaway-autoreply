import os
import logging
import json
import re
from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from slack_sdk import WebClient
from utils import (
    send_reply_to_hostaway,
    store_learning_example,
    get_similar_learning_examples,
    store_clarification_log,
)
from openai import OpenAI

logging.basicConfig(level=logging.INFO)
router = APIRouter()

SLACK_BOT_TOKEN = os.getenv("SLACK_BOT_TOKEN")
slack_client = WebClient(token=SLACK_BOT_TOKEN)
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
openai_client = OpenAI(api_key=OPENAI_API_KEY)

def clean_ai_reply(reply: str, property_type="home"):
    bad_signoffs = ["Enjoy!", "Best,", "Cheers,", "Sincerely,", "[Your Name]"]
    for signoff in bad_signoffs:
        reply = reply.replace(signoff, "")
    lines = reply.split('\n')
    reply = ' '.join([l.strip() for l in lines if not l.strip().lower().startswith(tuple(s.lower().replace(",", "") for s in bad_signoffs))])
    return re.sub(r"\s+", " ", reply).strip(",. ")

def needs_clarification(reply: str) -> bool:
    return any(phrase in reply.lower() for phrase in [
        "i'm not sure", "i don't know", "let me check", "can't find", "need to verify"
    ])

def ask_host_for_clarification(guest_msg, metadata, trigger_id):
    slack_client.views_open(
        trigger_id=trigger_id,
        view={
            "type": "modal",
            "title": {"type": "plain_text", "text": "Need Your Help"},
            "submit": {"type": "plain_text", "text": "Submit"},
            "close": {"type": "plain_text", "text": "Cancel"},
            "private_metadata": json.dumps(metadata),
            "blocks": [
                {
                    "type": "section",
                    "text": {"type": "mrkdwn", "text": f"Guest asked: *{guest_msg}*\nCan you help clarify the answer?"}
                },
                {
                    "type": "input",
                    "block_id": "clarify_input",
                    "label": {"type": "plain_text", "text": "Clarification:"},
                    "element": {"type": "plain_text_input", "action_id": "clarify_text", "multiline": True}
                },
                {
                    "type": "input",
                    "block_id": "clarify_tag",
                    "label": {"type": "plain_text", "text": "Tags (wifi, parking...)"},
                    "element": {"type": "plain_text_input", "action_id": "clarify_tag_input"}
                }
            ]
        }
    )

@router.post("/slack/actions")
async def slack_actions(request: Request):
    form = await request.form()
    payload = json.loads(form.get("payload"))
    logging.info(f"Slack Payload: {json.dumps(payload, indent=2)}")

    if payload.get("type") == "block_actions":
        action = payload["actions"][0]
        action_id = action.get("action_id")
        trigger_id = payload.get("trigger_id")
        view = payload.get("view", {})
        user = payload["user"]
        meta = json.loads(view.get("private_metadata", "{}"))

        if action_id == "improve_with_ai":
            state = view.get("state", {}).get("values", {})
            reply_block = state.get("reply_input", {})
            edited_text = next(iter(reply_block.values()), {}).get("value", "")

            prompt = (
                "Improve the guest message reply. Make it clear, concise, polite. "
                "Fix grammar or awkward phrasing, but don't add info.\n\n"
                f"{edited_text}"
            )

            try:
                response = openai_client.chat.completions.create(
                    model="gpt-4",
                    messages=[
                        {"role": "system", "content": "You help hosts polish replies to guests."},
                        {"role": "user", "content": prompt}
                    ]
                )
                improved = response.choices[0].message.content.strip()
            except Exception as e:
                logging.error(f"OpenAI improvement failed: {e}")
                improved = "(Error improving message.)"

            try:
                slack_client.views_update(
                    view_id=view.get("id"),
                    hash=view.get("hash"),
                    view={
                        "type": "modal",
                        "title": {"type": "plain_text", "text": "Improved Reply"},
                        "submit": {"type": "plain_text", "text": "Send"},
                        "close": {"type": "plain_text", "text": "Cancel"},
                        "private_metadata": json.dumps(meta),
                        "blocks": [
                            {
                                "type": "input",
                                "block_id": "reply_input",
                                "label": {"type": "plain_text", "text": "Your improved reply:"},
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
                                        "text": {"type": "plain_text", "text": ":rocket: Improve with AI"}
                                    }
                                ]
                            }
                        ]
                    }
                )
            except Exception as e:
                logging.error(f"Slack view update failed: {e}")

            return JSONResponse({})

    return JSONResponse({"status": "ignored"})
    
        # "Clarify for AI" - open the clarify modal
        if action_id == "clarify_submission":
            view = payload.get("view", {})
            state = view.get("state", {}).get("values", {})
            meta = json.loads(view.get("private_metadata", "{}"))
            guest_msg = meta.get("guest_message", "")
            ask_host_for_clarification(
                guest_msg=guest_msg,
                metadata=meta,
                trigger_id=trigger_id
            )
            return JSONResponse({})

    # Handle view_submission (when user submits manual reply or clarification)
    if payload.get("type") == "view_submission":
        view = payload.get("view", {})
        state = view.get("state", {}).get("values", {})
        meta = json.loads(view.get("private_metadata", "{}"))

        # Clarification modal submission
        if "clarify_input" in state:
            clarify_block = state.get("clarify_input", {})
            clarification_text = list(clarify_block.values())[0].get("value")
            tag_block = state.get("clarify_tag", {})
            clarification_tag = list(tag_block.values())[0].get("value")

            guest_msg = meta.get("guest_message", "")
            listing_id = meta.get("listing_id")
            guest_id = meta.get("guest_id")
            conversation_id = meta.get("conv_id") or meta.get("conversation_id")

            store_clarification_log(
                conversation_id=conversation_id,
                guest_message=guest_msg,
                clarification=clarification_text,
                tags=[clarification_tag] if clarification_tag else []
            )

            improved = generate_reply_with_clarification(guest_msg, clarification_text)
            store_learning_example(
                guest_message=guest_msg,
                ai_suggestion="",  # Optionally store prior AI suggestion
                user_reply=improved,
                listing_id=listing_id,
                guest_id=guest_id
            )

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

        # Manual reply submission or edit (handle as needed)
        if "reply_input" in state:
            reply_block = state.get("reply_input", {})
            reply_text = list(reply_block.values())[0].get("value")

            conv_id = meta.get("conv_id") or meta.get("conversation_id")
            communication_type = meta.get("type", "email")

            if not conv_id or not reply_text:
                logging.warning("Missing conversation ID or reply text.")
                return JSONResponse({"response_action": "errors"})

            success = send_reply_to_hostaway(conv_id, reply_text, communication_type)
            if success:
                logging.info(f"✅ Reply sent from modal for conv_id={conv_id}")
            else:
                logging.error(f"❌ Failed to send reply for conv_id={conv_id}")

            return JSONResponse({
                "response_action": "clear"
            })
