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
    # Add/adjust types here as needed
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
    # Remove unwanted sign-offs
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
    # Remove address/city (unless guest asked for it)
    address_patterns = [
        r"(the )?house at [\d]+ [^,]+, [A-Za-z ]+",
        r"\d{3,} [A-Za-z0-9 .]+, [A-Za-z ]+",
        r"at [\d]+ [\w .]+, [\w ]+"
    ]
    for pattern in address_patterns:
        reply = re.sub(pattern, f"the {property_type}", reply, flags=re.IGNORECASE)
    # Remove property name references like "at Cozy 2BR..." or "at Remodeled 4BR/2BA"
    reply = re.sub(r"at [A-Za-z0-9 ,/\-\(\)\']+", f"at the {property_type}", reply, flags=re.IGNORECASE)
    # Remove leading/trailing or double spaces, trailing commas/periods
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

def get_property_info(listing_id):
    property_info = ""
    if not listing_id:
        return property_info, {}
    listing = fetch_hostaway_resource("listings", listing_id)
    result = listing.get("result", {}) if listing else {}
    address = result.get("address", "")
    city = result.get("city", "")
    zipcode = result.get("zip", "")
    summary = result.get("summary", "")
    amenities = result.get("amenities", "")
    house_manual = result.get("houseManual", "")
    if isinstance(amenities, list):
        amenities_str = ", ".join(amenities)
    elif isinstance(amenities, dict):
        amenities_str = ", ".join([k for k, v in amenities.items() if v])
    else:
        amenities_str = str(amenities)
    property_info = (
        f"Property Address: {address}, {city} {zipcode}\n"
        f"Summary: {summary}\n"
        f"Amenities: {amenities_str}\n"
        f"House Manual: {house_manual[:800]}{'...' if len(house_manual) > 800 else ''}\n"
    )
    return property_info, result

@router.post("/slack/actions")
async def slack_actions(request: Request):
    form = await request.form()
    payload = json.loads(form.get("payload"))
    logging.info(f"Slack Interactivity Payload: {json.dumps(payload, indent=2)}")

    # --- Handle block_actions (button clicks) ---
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
            _, listing_result = get_property_info(listing_id)
            property_type = get_property_type(listing_result)
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
            _, listing_result = get_property_info(listing_id)
            property_type = get_property_type(listing_result)
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

        if action_id == "improve_with_ai":
            view = payload.get("view", {})
            state = view.get("state", {}).get("values", {})
            reply_block = state.get("reply_input", {})
            edited_text = None
            for v in reply_block.values():
                edited_text = v.get("value")
            meta = json.loads(view.get("private_metadata", "{}"))
            guest_message = meta.get("guest_message", "")
            listing_id = meta.get("listing_id", None)
            guest_id = meta.get("guest_id", None)
            ai_suggestion = meta.get("ai_suggestion", "")

            property_info, listing_result = get_property_info(listing_id)
            property_type = get_property_type(listing_result)
            similar_examples = get_similar_learning_examples(guest_message, listing_id)
            prev_answer = ""
            if similar_examples:
                prev_answer = f"Previously, you (the host) replied to a similar guest question about this property: \"{similar_examples[0][2]}\". Use this as a guide if it fits.\n"

            # Pass all info available (from message payload if needed)
            extra_meta = "\n".join([f"{k}: {v}" for k, v in meta.items() if k not in [
                "guest
