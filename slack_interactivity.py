import os
import logging
import json
import datetime
from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from slack_sdk import WebClient
from utils import (
    send_reply_to_hostaway,
    store_learning_example,
    store_clarification_log,
)
from openai import OpenAI

logging.basicConfig(level=logging.INFO)
router = APIRouter()

SLACK_BOT_TOKEN = os.getenv("SLACK_BOT_TOKEN")
slack_client = WebClient(token=SLACK_BOT_TOKEN)
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
openai_client = OpenAI(api_key=OPENAI_API_KEY)

CLARIFY_SYSTEM_PROMPT = (
    "You are a friendly, clear, and modern property host. Respond to guests as if you’re texting a friend. "
    "Keep replies casual, brief, and human—never robotic or overly formal. "
    "No sign-offs. Use contractions and speak in a warm, welcoming, millennial tone. "
    "Focus on being helpful and getting straight to the point."
)

TAGS = [
    "wifi", "parking", "checkin", "checkout", "bed type", "pets", "kitchen", "grill", "beach",
    "pool", "cleaning", "early checkin", "late checkout", "fee", "deposit", "amenities", "house rules",
    "tv", "streaming", "hot tub", "cancellation", "location", "views", "privacy", "accessibility", "security", "key code", "noise", "events", "supplies", "climate", "instructions", "other"
]

def clean_ai_reply(reply: str, property_type="home"):
    for signoff in ["Enjoy!", "Best,", "Best regards,", "Cheers,", "Sincerely,", "[Your Name]", "Best", "Sincerely"]:
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
    reply = ' '.join(reply.split())
    reply = reply.strip().replace(" ,", ",").replace(" .", ".")
    return reply.rstrip(",. ")

def needs_clarification(reply: str) -> bool:
    return any(phrase in reply.lower() for phrase in [
        "i'm not sure", "i don't know", "let me check", "can't find that info",
        "need to verify", "need to ask", "unsure"
    ])

def generate_reply_with_clarification(guest_msg, host_clarification):
    prompt = (
        "A guest asked a question. The host explained key facts for the AI. Write a new guest reply, using this info.\n\n"
        f"Guest: {guest_msg}\n"
        f"Host explanation: {host_clarification}\n"
        "Reply:"
    )
    try:
        response = openai_client.chat.completions.create(
            model="gpt-4",
            messages=[
                {"role": "system", "content": CLARIFY_SYSTEM_PROMPT},
                {"role": "user", "content": prompt}
            ]
        )
        return response.choices[0].message.content.strip()
    except Exception as e:
        logging.error(f"Clarify AI generation failed: {e}")
        return "(Error generating response from clarification.)"

def slack_open_or_push(payload, trigger_id, modal):
    container = payload.get("container", {})
    if container.get("type") == "message":
        slack_client.views_open(trigger_id=trigger_id, view=modal)
        logging.info("Opened modal with views_open.")
    else:
        slack_client.views_push(trigger_id=trigger_id, view=modal)
        logging.info("Pushed modal with views_push.")

@router.post("/slack/actions")
async def slack_actions(request: Request):
    logging.info("🎯 /slack/actions endpoint hit!")
    form = await request.form()
    payload_raw = form.get("payload")
    if not payload_raw:
        logging.error("No payload found in Slack actions request!")
        return JSONResponse({})

    payload = json.loads(payload_raw)
    logging.info(f"Slack Interactivity Payload: {json.dumps(payload, indent=2)}")

    if payload.get("type") == "block_actions":
        action = payload["actions"][0]
        action_id = action.get("action_id")
        trigger_id = payload.get("trigger_id")

        def get_meta_from_action(action):
            return json.loads(action["value"]) if "value" in action else {}

        # --- SEND, WRITE OWN, EDIT --- (unchanged, omitted for brevity; same as above)

        # --- CLARIFY ---
        if action_id == "clarify_request":
            meta = get_meta_from_action(action)
            guest_name = meta.get("guest_name", "Guest")
            guest_message = meta.get("guest_message", "")
            ai_suggestion = meta.get("ai_suggestion", "")
            modal = {
                "type": "modal",
                "title": {"type": "plain_text", "text": "Clarify for AI", "emoji": True},
                "submit": {"type": "plain_text", "text": "Teach AI", "emoji": True},
                "close": {"type": "plain_text", "text": "Cancel", "emoji": True},
                "private_metadata": json.dumps(meta),
                "blocks": [
                    {
                        "type": "section",
                        "block_id": "guest_message_section",
                        "text": {
                            "type": "mrkdwn",
                            "text": f"*Guest*: {guest_name}\n*Message*: {guest_message}\n*AI Suggested:*\n{ai_suggestion}\n\n*Explain to the AI (not guest):*\nWhat facts, rules, or info should the AI know to answer better?"
                        }
                    },
                    {
                        "type": "input",
                        "block_id": "clarify_input",
                        "label": {"type": "plain_text", "text": "Host explanation for AI", "emoji": True},
                        "element": {
                            "type": "plain_text_input",
                            "action_id": "clarify_text",
                            "multiline": True
                        }
                    },
                    {
                        "type": "input",
                        "block_id": "clarify_tag",
                        "label": {"type": "plain_text", "text": "Tags (choose all that apply)", "emoji": True},
                        "element": {
                            "type": "multi_static_select",
                            "action_id": "clarify_tag_input",
                            "placeholder": {"type": "plain_text", "text": "Select tags"},
                            "options": [
                                {"text": {"type": "plain_text", "text": tag}, "value": tag} for tag in TAGS
                            ]
                        }
                    },
                    {
                        "type": "actions",
                        "block_id": "clarify_actions",
                        "elements": [
                            {
                                "type": "button",
                                "action_id": "retry_response_with_clarification",
                                "text": {"type": "plain_text", "text": "Retry response with clarification", "emoji": True}
                            }
                        ]
                    }
                ]
            }
            slack_open_or_push(payload, trigger_id, modal)
            return JSONResponse({})

        # --- RETRY RESPONSE WITH CLARIFICATION ---
        if action_id == "retry_response_with_clarification":
            view = payload.get("view", {})
            state = view.get("state", {}).get("values", {})
            meta = json.loads(view.get("private_metadata", "{}"))

            clarification_text = next(iter(state["clarify_input"].values())).get("value", "")
            clarify_tags = state.get("clarify_tag", {}).get("clarify_tag_input", {}).get("selected_options", [])
            clarify_tags = [item["value"] for item in clarify_tags]

            guest_msg = meta.get("guest_message", "")
            listing_id = meta.get("listing_id")
            guest_id = meta.get("guest_id")
            conversation_id = meta.get("conv_id") or meta.get("conversation_id")

            # Save for AI learning as before
            store_clarification_log(conversation_id, guest_msg, clarification_text, clarify_tags)
            improved = generate_reply_with_clarification(guest_msg, clarification_text)
            store_learning_example(guest_msg, "", improved, listing_id, guest_id)

            # Push a new modal with the improved reply, ready to send
            return JSONResponse({
                "response_action": "update",
                "view": {
                    "type": "modal",
                    "title": {"type": "plain_text", "text": "AI New Reply", "emoji": True},
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
                        }
                    ]
                }
            })

    # --- Modal Submission: Normal Reply, Clarify, etc. ---
    if payload.get("type") == "view_submission":
        view = payload.get("view", {})
        state = view.get("state", {}).get("values", {})
        meta = json.loads(view.get("private_metadata", "{}"))

        # Normal reply send (from improved modal)
        if "reply_input" in state:
            reply_text = next(iter(state["reply_input"].values())).get("value")
            conv_id = meta.get("conv_id") or meta.get("conversation_id")
            communication_type = meta.get("type", "email")
            send_reply_to_hostaway(conv_id, reply_text, communication_type)
            return JSONResponse({"response_action": "clear"})

    return JSONResponse({"status": "ok"})
