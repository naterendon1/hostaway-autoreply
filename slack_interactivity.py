import os
import logging
import json
import re
import datetime
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

# --- Tag Options ---
TAG_OPTIONS = [
    "wifi", "checkin", "parking", "pets", "kitchen", "checkout", "address", "directions", "cancellation",
    "bedrooms", "bathrooms", "view", "beach", "pool", "hot tub", "laundry", "tv", "streaming", "grill", "quiet hours",
    "neighbors", "amenities", "rules", "children", "infants", "accessibility", "security", "safety", "heat/ac",
    "extra guests", "noise", "privacy", "local area", "distance", "fees", "deposit", "cleaning", "essentials",
    "coffee", "host contact", "special requests", "breakfast", "supplies", "wifi password"
]

def clean_ai_reply(reply: str, property_type="home"):
    bad_signoffs = [
        "Enjoy your meal", "Enjoy your meals", "Enjoy!", "Best,", "Best regards,",
        "Cheers,", "Sincerely,", "[Your Name]", "Best", "Sincerely"
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
                {"role": "system", "content": "You are a warm and professional vacation rental assistant. Your tone is clear, helpful, and friendly."},
                {"role": "user", "content": prompt}
            ]
        )
        return response.choices[0].message.content.strip()
    except Exception as e:
        logging.error(f"Clarify AI generation failed: {e}")
        return "(Error generating response from clarification.)"

def tag_options_block():
    return {
        "type": "input",
        "block_id": "clarify_tag_block",
        "element": {
            "type": "multi_static_select",
            "action_id": "clarify_tag_input",
            "placeholder": {"type": "plain_text", "text": "Choose tag(s)"},
            "options": [
                {
                    "text": {"type": "plain_text", "text": tag},
                    "value": tag
                } for tag in TAG_OPTIONS
            ]
        },
        "label": {"type": "plain_text", "text": "Tags (choose as many as apply)"}
    }

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
        user = payload.get("user", {})
        user_id = user.get("id", "")

        def get_meta_from_action(action):
            return json.loads(action["value"]) if "value" in action else {}

        # --- SEND / WRITE OWN / EDIT (unchanged) ---
        # ... (your existing logic here) ...

        # --- CLARIFY / RETRY WITH CLARIFICATION ---
        if action_id in ("clarify_request", "retry_with_clarification"):
            meta = get_meta_from_action(action)
            guest_name = meta.get("guest_name", "Guest")
            guest_message = meta.get("guest_message", "(Message unavailable)")
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
                        "text": {"type": "mrkdwn", "text": f"*Guest*: {guest_name}\n*Message*: {guest_message}\n*AI Suggested*: {ai_suggestion}"}
                    },
                    {
                        "type": "input",
                        "block_id": "clarify_input",
                        "label": {"type": "plain_text", "text": "Explain to the AI what it should know to answer better. (e.g. 'There IS a king bed, and there IS a grill, but it's charcoal only.')", "emoji": True},
                        "element": {
                            "type": "plain_text_input",
                            "action_id": "clarify_text",
                            "multiline": True
                        }
                    },
                    tag_options_block()
                ]
            }
            slack_open_or_push(payload, trigger_id, modal)
            return JSONResponse({})

        # --- IMPROVE WITH AI (unchanged) ---
        # ... (your existing logic here) ...

    # --- CLARIFY MODAL SUBMISSION HANDLER (Retry logic) ---
    if payload.get("type") == "view_submission":
        view = payload.get("view", {})
        state = view.get("state", {}).get("values", {})
        meta = json.loads(view.get("private_metadata", "{}"))

        # Clarification modal submission (for AI learning & retry)
        if "clarify_input" in state:
            clarification_text = next(iter(state["clarify_input"].values())).get("value")
            tag_block = state.get("clarify_tag_block", {})
            selected_tags = []
            for block in tag_block.values():
                selected_tags = block.get("selected_options", [])
                if selected_tags:
                    selected_tags = [t['value'] for t in selected_tags]
                    break

            guest_msg = meta.get("guest_message", "")
            listing_id = meta.get("listing_id")
            guest_id = meta.get("guest_id")
            conversation_id = meta.get("conv_id") or meta.get("conversation_id")

            store_clarification_log(conversation_id, guest_msg, clarification_text, selected_tags)
            improved = generate_reply_with_clarification(guest_msg, clarification_text)
            store_learning_example(guest_msg, "", improved, listing_id, guest_id)

            # Modal updates: Show improved reply, allow retry or send
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
                            "type": "section",
                            "block_id": "guest_message_section",
                            "text": {"type": "mrkdwn", "text": f"*Guest*: {meta.get('guest_name','Guest')}\n*Message*: {guest_msg}\n*Your clarification:* {clarification_text}\n*Tags:* {', '.join(selected_tags)}"}
                        },
                        {
                            "type": "input",
                            "block_id": "reply_input",
                            "label": {"type": "plain_text", "text": "AI Improved Reply:", "emoji": True},
                            "element": {
                                "type": "plain_text_input",
                                "action_id": "reply",
                                "multiline": True,
                                "initial_value": improved
                            }
                        },
                        {
                            "type": "actions",
                            "block_id": "actions_block",
                            "elements": [
                                {
                                    "type": "button",
                                    "action_id": "retry_with_clarification",
                                    "text": {"type": "plain_text", "text": "Retry with Clarification", "emoji": True}
                                },
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

        # Regular reply submission handler
        if "reply_input" in state:
            reply_text = next(iter(state["reply_input"].values())).get("value")
            conv_id = meta.get("conv_id") or meta.get("conversation_id")
            communication_type = meta.get("type", "email")
            send_reply_to_hostaway(conv_id, reply_text, communication_type)
            return JSONResponse({"response_action": "clear"})

    return JSONResponse({"status": "ok"})
