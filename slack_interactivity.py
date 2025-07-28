import os
import logging
import json
import datetime
from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from slack_sdk import WebClient
from openai import OpenAI
from utils import (
    fetch_hostaway_listing,
    fetch_hostaway_reservation,
    fetch_hostaway_conversation,
    get_cancellation_policy_summary,
    get_similar_learning_examples,
    get_property_info,
    store_ai_feedback,
    send_reply_to_hostaway,
    store_clarification_log,
    store_learning_example
)

logging.basicConfig(level=logging.INFO)
router = APIRouter()

SLACK_BOT_TOKEN = os.getenv("SLACK_BOT_TOKEN")
slack_client = WebClient(token=SLACK_BOT_TOKEN)
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
openai_client = OpenAI(api_key=OPENAI_API_KEY)

# Tag options for clarify modal
CLARIFY_TAG_OPTIONS = [
    {"text": {"type": "plain_text", "text": "Check-in/Check-out"}, "value": "checkin_checkout"},
    {"text": {"type": "plain_text", "text": "Wifi"}, "value": "wifi"},
    {"text": {"type": "plain_text", "text": "Amenities"}, "value": "amenities"},
    {"text": {"type": "plain_text", "text": "Pet Policy"}, "value": "pet_policy"},
    {"text": {"type": "plain_text", "text": "Beds"}, "value": "beds"},
    {"text": {"type": "plain_text", "text": "Parking"}, "value": "parking"},
    {"text": {"type": "plain_text", "text": "Booking"}, "value": "booking"},
    {"text": {"type": "plain_text", "text": "Fees/Price"}, "value": "fees_price"},
    {"text": {"type": "plain_text", "text": "Cancellation"}, "value": "cancellation"},
    {"text": {"type": "plain_text", "text": "Location"}, "value": "location"},
    {"text": {"type": "plain_text", "text": "House Manual"}, "value": "house_manual"},
    {"text": {"type": "plain_text", "text": "House Rules"}, "value": "house_rules"},
    {"text": {"type": "plain_text", "text": "Minimum Stay"}, "value": "min_stay"},
    {"text": {"type": "plain_text", "text": "Accessibility"}, "value": "accessibility"},
    {"text": {"type": "plain_text", "text": "Golf Cart"}, "value": "golf_cart"},
    {"text": {"type": "plain_text", "text": "Pool"}, "value": "pool"},
    {"text": {"type": "plain_text", "text": "View"}, "value": "view"},
    {"text": {"type": "plain_text", "text": "Other"}, "value": "other"}
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
        "You are a clear, concise, and informal property host (millennial vibe). Use the info the host gave you to answer the guest's question, but keep it casual and straight to the point. Don't add fluff. Only use what the host explained.\n"
        f"Guest: {guest_msg}\n"
        f"Host's clarification/explanation to AI (not for guest!): {host_clarification}\n"
        "Reply:"
    )
    try:
        response = openai_client.chat.completions.create(
            model="gpt-4",
            timeout=15,  # Increase timeout for long explanations
            messages=[
                {"role": "system", "content": "Be concise, informal, and helpful. Write like a millennial host texting a guest."},
                {"role": "user", "content": prompt}
            ]
        )
        return clean_ai_reply(response.choices[0].message.content.strip())
    except Exception as e:
        logging.error(f"Clarify AI generation failed: {e}")
        return "(Error generating response from clarification.)"

def slack_open_or_push(payload, trigger_id, modal):
    container = payload.get("container", {})
    try:
        if container.get("type") == "message":
            slack_client.views_open(trigger_id=trigger_id, view=modal)
            logging.info("Opened modal with views_open.")
        else:
            slack_client.views_push(trigger_id=trigger_id, view=modal)
            logging.info("Pushed modal with views_push.")
    except Exception as e:
        logging.error(f"Slack modal error: {e}")

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
            reply = meta.get("reply", "(No reply provided.)")
            conv_id = meta.get("conv_id")
            communication_type = meta.get("type", "email")
            if not reply or not conv_id:
                return JSONResponse({"text": "Missing reply or conversation ID."})
            try:
                success = send_reply_to_hostaway(conv_id, reply, communication_type)
            except Exception as e:
                logging.error(f"Slack SEND error: {e}")
                return JSONResponse({"text": "Slack send failed."})
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
            guest_msg = meta.get("guest_message", "(Message unavailable)")
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
                            "text": f"*Guest*: {guest_name}\n*Message*: {guest_msg}\n\n*AI Suggested:*\n{ai_suggestion}\n\n*Explain to the AI (not guest):*\nWhat facts, rules, or info should the AI know to answer better?"
                        }
                    },
                    {
                        "type": "input",
                        "block_id": "clarify_input",
                        "label": {"type": "plain_text", "text": "Your clarification/explanation", "emoji": True},
                        "element": {
                            "type": "plain_text_input",
                            "action_id": "clarify_text",
                            "multiline": True
                        }
                    },
                    {
                        "type": "input",
                        "block_id": "clarify_tag",
                        "label": {"type": "plain_text", "text": "Tags (pick as many as apply)", "emoji": True},
                        "element": {
                            "type": "multi_static_select",
                            "action_id": "clarify_tag_input",
                            "placeholder": {"type": "plain_text", "text": "Choose tags"},
                            "options": CLARIFY_TAG_OPTIONS
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

            # Get guest context from private_metadata for display in improved modal
            meta = json.loads(view.get("private_metadata", "{}"))
            guest_name = meta.get("guest_name", "Guest")
            guest_msg = meta.get("guest_message", "")

            prompt = (
                "Take this guest message reply and improve it. "
                "Make it clear, concise, informal, and millennial. "
                "Do not add extra content. Only return the improved version.\n\n"
                f"{edited_text}"
            )
            try:
                response = openai_client.chat.completions.create(
                    model="gpt-4",
                    timeout=15,
                    messages=[
                        {"role": "system", "content": "You are a helpful assistant for editing guest replies. Be concise, informal, and millennial."},
                        {"role": "user", "content": prompt}
                    ]
                )
                improved = clean_ai_reply(response.choices[0].message.content.strip())
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

            try:
                slack_client.views_push(trigger_id=trigger_id, view=new_modal)
            except Exception as e:
                logging.error(f"Slack views_push error: {e}")

            return JSONResponse({})

    # --- CLARIFY MODAL SUBMISSION HANDLER ---
    if payload.get("type") == "view_submission":
        view = payload.get("view", {})
        state = view.get("state", {}).get("values", {})
        meta = json.loads(view.get("private_metadata", "{}"))

        # Clarification modal submission (for AI learning)
        if "clarify_input" in state:
            clarification_text = next(iter(state["clarify_input"].values())).get("value")
            clarify_tag_data = state.get("clarify_tag", {})
            clarify_tag_selected = []
            for v in clarify_tag_data.values():
                clarify_tag_selected = v.get("selected_options", [])
            tag_values = [t.get("value") for t in clarify_tag_selected]

            guest_msg = meta.get("guest_message", "")
            listing_id = meta.get("listing_id")
            guest_id = meta.get("guest_id")
            conversation_id = meta.get("conv_id") or meta.get("conversation_id")

            # Save clarification for learning
            store_clarification_log(conversation_id, guest_msg, clarification_text, tag_values)
            improved = generate_reply_with_clarification(guest_msg, clarification_text)
            store_learning_example(guest_msg, "", improved, listing_id, guest_id)

            # Modal: show improved reply and "Retry/Send" option
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
                            "text": {"type": "mrkdwn", "text": f"*Guest*: {meta.get('guest_name', 'Guest')}\n*Message*: {guest_msg}"}
                        },
                        {
                            "type": "input",
                            "block_id": "reply_input",
                            "label": {"type": "plain_text", "text": "AI reply (edit as needed):", "emoji": True},
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
            try:
                send_reply_to_hostaway(conv_id, reply_text, communication_type)
            except Exception as e:
                logging.error(f"Slack regular send error: {e}")
            return JSONResponse({"response_action": "clear"})

    return JSONResponse({"status": "ok"})

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

        def get_meta_from_action(action):
            return json.loads(action["value"]) if "value" in action else {}

        # 👍👎 Feedback Rating Handler
        if action_id in ["rate_up", "rate_down"]:
            meta = get_meta_from_action(action)
            rating = "up" if action_id == "rate_up" else "down"
            reply = meta.get("ai_suggestion", "")
            listing_id = meta.get("listing_id")
            guest_id = meta.get("guest_id")
            store_ai_feedback(reply, rating, listing_id, guest_id)
            return JSONResponse({"text": "📊 Thanks for your feedback!"})

    return JSONResponse({"status": "ok"})
