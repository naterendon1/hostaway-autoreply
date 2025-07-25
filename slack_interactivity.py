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
    bad_signoffs = [
        "Enjoy your meal", "Enjoy your meals", "Enjoy!", "Best,", "Best regards,", "Cheers,", "Sincerely,", "[Your Name]", "Best", "Sincerely"
    ]
    for signoff in bad_signoffs:
        reply = reply.replace(signoff, "")
    lines = reply.split('\n')
    filtered_lines = []
    for line in lines:
        stripped = line.strip()
        if any(stripped.lower().startswith(s.lower().replace(",", "")) for s in ["Best", "Cheers", "Sincerely"]):
            continue
        if "[Your Name]" in stripped:
            continue
        filtered_lines.append(line)
    reply = ' '.join(filtered_lines)
    address_patterns = [
        r"(at\s+)?\d{3,} [A-Za-z0-9 .]+, [A-Za-z ]+",
        r"(the\s+)?house at [\d]+ [^,]+, [A-Za-z ]+",
        r"at [\d]+ [\w .]+, [\w ]+"
    ]
    for pattern in address_patterns:
        reply = re.sub(pattern, f"at the {property_type}", reply, flags=re.IGNORECASE)
    reply = ' '.join(reply.split())
    reply = reply.strip().replace(" ,", ",").replace(" .", ".")
    return reply.rstrip(",. ")

def needs_clarification(reply: str) -> bool:
    return any(phrase in reply.lower() for phrase in [
        "i'm not sure", "i don't know", "let me check", "can't find that info",
        "need to verify", "need to ask", "unsure"
    ])

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
                {"role": "system", "content": "You are a warm and professional vacation rental assistant. Your tone is clear, helpful, and friendly. Avoid sounding robotic. Be specific and natural."},
                {"role": "user", "content": prompt}
            ]
        )
        return response.choices[0].message.content.strip()
    except Exception as e:
        logging.error(f"Clarify AI generation failed: {e}")
        return "(Error generating response from clarification.)"

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

        # "Write Your Own Reply" - open the manual reply modal
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
                            },
                            {
                                "type": "button",
                                "action_id": "clarify_submission",
                                "text": {"type": "plain_text", "text": ":question: Clarify for AI", "emoji": True}
                            }
                        ]
                    }
                ]
            }
            slack_client.views_open(trigger_id=trigger_id, view=modal)
            return JSONResponse({})

        # "Improve with AI" - rewrite the reply using GPT, update the modal in place
        if action_id == "improve_with_ai":
            logging.info("🚀 Improve with AI clicked.")
            view = payload.get("view", {})
            state = view.get("state", {}).get("values", {})
            reply_block = state.get("reply_input", {})
            edited_text = None
            for v in reply_block.values():
                if v.get("value") is not None:
                    edited_text = v.get("value")
            logging.info(f"User's draft text: {edited_text}")

            prompt = (
                "Make the following message as clear, concise, and informal as possible. "
                "Ensure it makes sense, and do not add any extra information. Only return the improved message.\n\n"
                f"Original message:\n{edited_text}"
            )

            try:
                response = openai_client.chat.completions.create(
                    model="gpt-4",
                    messages=[
                        {"role": "system", "content": "You are a helpful assistant for editing guest replies."},
                        {"role": "user", "content": prompt}
                    ]
                )
                improved = response.choices[0].message.content.strip()
                logging.info(f"Improved reply from AI: {improved}")
            except Exception as e:
                logging.error(f"OpenAI error in 'improve_with_ai': {e}")
                improved = "(Error generating improved reply.)"

            try:
                slack_client.views_update(
                    view_id=view.get("id"),
                    hash=view.get("hash"),
                    view={
                        "type": "modal",
                        "title": {"type": "plain_text", "text": "Improved Reply", "emoji": True},
                        "submit": {"type": "plain_text", "text": "Send", "emoji": True},
                        "close": {"type": "plain_text", "text": "Cancel", "emoji": True},
                        "private_metadata": view.get("private_metadata"),
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
                )
            except Exception as e:
                logging.error(f"Failed to update improved modal: {e}")
            return JSONResponse({})

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
            # Optionally, post reply to Hostaway or elsewhere here
            logging.info(f"Manual reply submitted: {reply_text}")
            # You may want to add code to send the reply to Hostaway or log it

            return JSONResponse({"text": "Reply sent!"})

    # Fallback response
    return JSONResponse({"text": "Action received."})

