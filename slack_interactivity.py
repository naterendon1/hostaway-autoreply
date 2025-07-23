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

def get_property_type(listing_result):
    prop_type = (listing_result.get("type") or "").lower()
    name = (listing_result.get("name") or "").lower()
    for t in ["house", "cabin", "condo", "apartment", "villa", "bungalow", "cottage", "suite"]:
        if t in prop_type:
            return t
        if t in name:
            return t
    return "home"

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

SYSTEM_PROMPT = (
    "You are a vacation rental host for homes in Crystal Beach, TX, Austin, TX, Galveston, TX, and Georgetown, TX. "
    "Answer guest questions in a concise, informal, and polite way, always to-the-point. "
    "Never add extra information, suggestions, or local tips unless the guest asks for it. "
    "Do not include fluff, chit-chat, upsell, or overly friendly phrases. "
    "Never use sign-offs like 'Enjoy your meal', 'Enjoy your meals', 'Enjoy!', 'Best', or your name. Only use a simple closing like 'Let me know if you need anything else' or 'Let me know if you need more recommendations' if it’s natural for the situation, and leave it off entirely if the message already feels complete. "
    "Never use multi-line replies unless absolutely necessary—keep replies to a single paragraph with greeting and answer together. "
    "Greet the guest casually using their first name if known, then answer their question immediately. "
    "Never include the property’s address, city, zip, or property name in your answer unless the guest specifically asks for it. Instead, refer to it as 'the house', 'the condo', or the appropriate property type. "
    "If you don’t know the answer, say you’ll check and get back to them. "
    "If a guest is only inquiring about dates or making a request, always check the calendar to confirm availability before agreeing. "
    "If the guest already has a confirmed booking, do not check the calendar or mention availability—just answer their questions directly. "
    "For early check-in or late check-out requests, check if available first, then mention a fee applies. "
    "For refund requests outside the cancellation policy, politely explain that refunds are only possible if the dates rebook. "
    "If a guest cancels for an emergency, show empathy and refer to Airbnb’s extenuating circumstances policy or the relevant platform's version. "
    "For amenity/house details, answer directly with no extra commentary. "
    "For parking, clarify how many vehicles are allowed and where to park (driveways, not blocking neighbors, etc). "
    "For tech/amenity questions (WiFi, TV, grill, etc.), give quick, direct instructions. "
    "If you have a previously saved answer for this question and house, use that wording if appropriate. "
    "Always be helpful and accurate, but always brief."
)

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
            listing_id = meta.get("listing_id", None)
            _, listing_result = {}, {}
            property_type = "home"
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
            listing_id = meta.get("listing_id", None)
            _, listing_result = {}, {}
            property_type = "home"
            draft = clean_ai_reply(meta.get("draft", ""), property_type)
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

        # ----- Improved "improve_with_ai" handler -----
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
            meta = json.loads(view.get("private_metadata", "{}"))
            guest_message = meta.get("guest_message", "")
            listing_id = meta.get("listing_id", None)
            guest_id = meta.get("guest_id", None)
            ai_suggestion = meta.get("ai_suggestion", "")

            property_type = "home"
            similar_examples = get_similar_learning_examples(guest_message, listing_id)
            prev_answer = ""
            if similar_examples:
                prev_answer = f"Previously, you (the host) replied to a similar guest question about this property: \"{similar_examples[0][2]}\". Use this as a guide if it fits.\n"

            prompt = (
                f"{prev_answer}"
                f"A guest sent this message:\n{guest_message}\n\n"
                "Here is my draft reply (below). Please improve it for clarity, conciseness, and polite, informal tone, "
                "but do NOT invent information that isn't in the property details or prior answers. "
                f"\nDraft reply: {edited_text}\n"
                "Respond ONLY with the improved reply."
            )
            logging.info(f"Prompt sent to OpenAI:\n{prompt}")

            try:
                response = openai_client.chat.completions.create(
                    model="gpt-4",
                    messages=[
                        {"role": "system", "content": SYSTEM_PROMPT},
                        {"role": "user", "content": prompt}
                    ]
                )
                improved = clean_ai_reply(response.choices[0].message.content.strip(), property_type)
                logging.info(f"Improved reply from AI: {improved}")
            except Exception as e:
                logging.error(f"OpenAI error in 'improve_with_ai': {e}")
                improved = "(Error generating improved reply.)"

            # Defensive: update input block with improved reply
            import copy
            new_modal = copy.deepcopy(view)
            updated = False
            for block in new_modal["blocks"]:
                if block["type"] == "input" and block.get("block_id") == "reply_input":
                    if "element" in block:
                        block["element"]["initial_value"] = improved
                        updated = True

            if not updated:
                logging.error("No reply_input block found in modal for update!")

            logging.info("Returning modal update to Slack: %s", json.dumps(new_modal))
            return JSONResponse({"response_action": "update", "view": new_modal})

        # ----- End improve_with_ai handler -----

        if action_id == "send":
            meta = get_meta_from_action(action)
            conv_id = meta.get("conv_id")
            listing_id = meta.get("listing_id", None)
            property_type = "home"
            reply = clean_ai_reply(meta.get("reply", ""), property_type)
            type_ = meta.get("type")
            guest_message = meta.get("guest_message", "")
            guest_id = meta.get("guest_id", None)
            ai_suggestion = reply
            store_learning_example(guest_message, ai_suggestion, reply, listing_id, guest_id)
            send_ok = send_reply_to_hostaway(conv_id, reply, type_)
            msg = ":white_check_mark: AI reply sent and saved!" if send_ok else ":warning: Failed to send."
            return JSONResponse({"text": msg})

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
        property_type = "home"
        guest_message = meta.get("guest_message", "")
        type_ = meta.get("type")
        guest_id = meta.get("guest_id")
        ai_suggestion = meta.get("ai_suggestion", "")

        clean_reply = clean_ai_reply(user_text, property_type)
        store_learning_example(guest_message, ai_suggestion, clean_reply, listing_id, guest_id)
        send_ok = send_reply_to_hostaway(conv_id, clean_reply, type_)
        msg = ":white_check_mark: Your reply was sent and saved for future learning!" if send_ok else ":warning: Failed to send."
        done_modal = {
            "type": "modal",
            "title": {"type": "plain_text", "text": "Done!", "emoji": True},
            "close": {"type": "plain_text", "text": "Close", "emoji": True},
            "blocks": [
                {"type": "section", "text": {"type": "mrkdwn", "text": msg}}
            ]
        }
        return JSONResponse({"response_action": "update", "view": done_modal})

    return JSONResponse({"text": "Action received."})
