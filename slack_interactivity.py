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

# Dropdown tag options
CLARIFY_TAG_OPTIONS = [
    "wifi", "parking", "checkin", "checkout", "pets", "kids", "beds", "grill", "amenities",
    "pool", "security", "tv", "kitchen", "access", "cleaning", "fees", "view", "distance",
    "capacity", "rules", "damage deposit", "cancellation", "house manual", "location",
    "noise", "late checkout", "early checkin", "beach", "lake", "mountain", "city", "hot tub",
    "ac", "heating", "coffee", "laundry", "linens", "directions", "transport", "discount", "events",
    "guests", "infants", "children", "supplies", "appliances"
]
CLARIFY_TAG_SLACK_OPTIONS = [
    {"text": {"type": "plain_text", "text": tag}, "value": tag} for tag in CLARIFY_TAG_OPTIONS
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
        "A guest asked a question about a vacation rental property, and the host explained to the AI some extra facts, rules, or details to answer better. "
        "Using the guest's question and the host's explanation, write a warm, clear, brief response for the guest. "
        "Do not mention that the info was just clarified—just answer as if you knew it all along.\n\n"
        f"Guest: {guest_msg}\n"
        f"Host's explanation for AI: {host_clarification}\n"
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

        # --- SEND ---
        if action_id == "send":
            meta = get_meta_from_action(action)
            reply = meta.get("reply", "(No reply provided here; should be provided by modal.)")
            conv_id = meta.get("conv_id")
            communication_type = meta.get("type", "email")
            if not reply or not conv_id:
                return JSONResponse({"text": "Missing reply or conversation ID."})
            success = send_reply_to_hostaway(conv_id, reply, communication_type)
            return JSONResponse({"text": "Reply sent to guest!" if success else "Failed to send reply to guest."})

        # --- WRITE OWN ---
        if action_id == "write_own":
            meta = get_meta_from_action(action)
            guest_name = meta.get("guest_name", "Guest")
            guest_msg = meta.get("guest_message", "(Message unavailable)")
            modal = {
                "type": "modal",
                "title": {"type": "plain_text", "text": "Write Your Reply", "emoji": True},
                "submit": {"type": "plain_text", "text": "Send", "emoji": True},
                "close": {"type": "plain_text", "text": "Cancel", "emoji": True},
                "private_metadata": json.dumps(meta),
                "blocks": [
                    {
                        "type": "section",
                        "block_id": "guest_message_section",
                        "text": {"type": "mrkdwn", "text": f"*Guest*: {guest_name}\n*Message*: {guest_msg}"}
                    },
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

        # --- EDIT ---
        if action_id == "edit":
            meta = get_meta_from_action(action)
            guest_name = meta.get("guest_name", "Guest")
            guest_msg = meta.get("guest_message", "(Message unavailable)")
            ai_suggestion = meta.get("draft", meta.get("ai_suggestion", ""))
            modal = {
                "type": "modal",
                "title": {"type": "plain_text", "text": "Edit AI Reply", "emoji": True},
                "submit": {"type": "plain_text", "text": "Send", "emoji": True},
                "close": {"type": "plain_text", "text": "Cancel", "emoji": True},
                "private_metadata": json.dumps(meta),
                "blocks": [
                    {
                        "type": "section",
                        "block_id": "guest_message_section",
                        "text": {"type": "mrkdwn", "text": f"*Guest*: {guest_name}\n*Message*: {guest_msg}"}
                    },
                    {
                        "type": "input",
                        "block_id": "reply_input",
                        "label": {"type": "plain_text", "text": "Edit below:", "emoji": True},
                        "element": {
                            "type": "plain_text_input",
                            "action_id": "reply",
                            "multiline": True,
                            "initial_value": ai_suggestion
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
            slack_open_or_push(payload, trigger_id, modal)
            return JSONResponse({})

        # --- CLARIFY ---
        if action_id == "clarify_request":
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
                        "label": {"type": "plain_text", "text": "Explain to the AI (not guest):\nWhat facts, rules, or info should the AI know to answer better?", "emoji": True},
                        "element": {
                            "type": "plain_text_input",
                            "action_id": "clarify_text",
                            "multiline": True,
                            "placeholder": {"type": "plain_text", "text": "e.g. There IS a king bed. Grill is charcoal only. One-night stays must leave by 10am."}
                        }
                    },
                    {
                        "type": "input",
                        "block_id": "clarify_tag",
                        "label": {"type": "plain_text", "text": "Select one or more tags for this clarification", "emoji": True},
                        "element": {
                            "type": "multi_static_select",
                            "action_id": "clarify_tag_input",
                            "placeholder": {"type": "plain_text", "text": "Choose tags..."},
                            "options": CLARIFY_TAG_SLACK_OPTIONS
                        }
                    }
                ]
            }
            slack_open_or_push(payload, trigger_id, modal)
            return JSONResponse({})

        # --- IMPROVE WITH AI ---
        if action_id == "improve_with_ai":
            view = payload.get("view", {})
            state = view.get("state", {}).get("values", {})
            reply_block = state.get("reply_input", {})
            edited_text = next((v.get("value") for v in reply_block.values() if v.get("value")), "")

            meta = json.loads(view.get("private_metadata", "{}"))
            guest_name = meta.get("guest_name", "Guest")
            guest_msg = meta.get("guest_message", "")

            logging.info(f"Improve with AI clicked. view_id: {view.get('id')}, hash: {view.get('hash')}")

            prompt = (
                "Take this guest message reply and improve it. "
                "Make it clear, concise, polite, informal, and ensure it makes sense. "
                "Do not add extra content. Return only the improved version.\n\n"
                f"{edited_text}"
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
            except Exception as e:
                logging.error(f"OpenAI error in 'improve_with_ai': {e}")
                improved = "(Error generating improved message.)"

            new_modal = {
                "type": "modal",
                "title": {"type": "plain_text", "text": "AI Improved Reply", "emoji": True},
                "submit": {"type": "plain_text", "text": "Send", "emoji": True},
                "close": {"type": "plain_text", "text": "Cancel", "emoji": True},
                "private_metadata": view.get("private_metadata"),
                "blocks": [
                    {
                        "type": "section",
                        "block_id": "guest_message_section",
                        "text": {"type": "mrkdwn", "text": f"*Guest*: {guest_name}\n*Message*: {guest_msg}"}
                    },
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
                    },
                    {
                        "type": "context",
                        "elements": [
                            {
                                "type": "plain_text",
                                "text": f"Last AI improvement: {datetime.datetime.now().isoformat()}"
                            }
                        ]
                    }
                ]
            }

            slack_client.views_push(trigger_id=trigger_id, view=new_modal)
            logging.info("Slack views_push sent new AI modal.")

            return JSONResponse({})

    # --- CLARIFY MODAL SUBMISSION HANDLER ---
    if payload.get("type") == "view_submission":
        view = payload.get("view", {})
        state = view.get("state", {}).get("values", {})
        meta = json.loads(view.get("private_metadata", "{}"))

        # Clarification modal submission (for AI learning & retry with AI!)
        if "clarify_input" in state:
            clarification_text = next(iter(state["clarify_input"].values())).get("value")
            tag_element = next(iter(state["clarify_tag"].values()))
            selected_tags = tag_element.get("selected_options", [])
            clarification_tags = [tag['value'] for tag in selected_tags]
            guest_msg = meta.get("guest_message", "")
            listing_id = meta.get("listing_id")
            guest_id = meta.get("guest_id")
            conversation_id = meta.get("conv_id") or meta.get("conversation_id")

            store_clarification_log(conversation_id, guest_msg, clarification_text, clarification_tags)
            improved = generate_reply_with_clarification(guest_msg, clarification_text)
            store_learning_example(guest_msg, "", improved, listing_id, guest_id)

            return JSONResponse({
                "response_action": "update",
                "view": {
                    "type": "modal",
                    "title": {"type": "plain_text", "text": "AI Revised Reply", "emoji": True},
                    "submit": {"type": "plain_text", "text": "Send", "emoji": True},
                    "close": {"type": "plain_text", "text": "Cancel", "emoji": True},
                    "private_metadata": json.dumps(meta),
                    "blocks": [
                        {
                            "type": "section",
                            "block_id": "guest_message_section",
                            "text": {"type": "mrkdwn", "text": f"*Guest*: {meta.get('guest_name', 'Guest')}\n*Message*: {guest_msg}"}
                        },
                        {
                            "type": "input",
                            "block_id": "reply_input",
                            "label": {"type": "plain_text", "text": "AI revised reply (edit or send):", "emoji": True},
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

        # Regular reply submission handler
        if "reply_input" in state:
            reply_text = next(iter(state["reply_input"].values())).get("value")
            conv_id = meta.get("conv_id") or meta.get("conversation_id")
            communication_type = meta.get("type", "email")
            send_reply_to_hostaway(conv_id, reply_text, communication_type)
            return JSONResponse({"response_action": "clear"})

    return JSONResponse({"status": "ok"})
