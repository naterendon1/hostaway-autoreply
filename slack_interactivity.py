import os
import logging
import json
from fastapi import APIRouter, Request, BackgroundTasks
from fastapi.responses import JSONResponse
from utils import (
    send_reply_to_hostaway,
    fetch_hostaway_resource,
    store_learning_example,
    get_similar_learning_examples
)
from openai import OpenAI

# Setup logging if not already configured
logging.basicConfig(level=logging.INFO)

OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
openai_client = OpenAI(api_key=OPENAI_API_KEY)

router = APIRouter()

def clean_ai_reply(reply: str) -> str:
    bad_signoffs = [
        "Enjoy your meal", "Enjoy your meals", "Enjoy!", "Best,", "Best regards,", "Cheers,", "Sincerely,", "[Your Name]", "Best", "Sincerely"
    ]
    for signoff in bad_signoffs:
        if signoff in reply:
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
    return reply.strip().replace("  ", " ").rstrip(",. ")

SYSTEM_PROMPT = (
    "You are a vacation rental host for homes in Crystal Beach, TX, Austin, TX, Galveston, TX, and Georgetown, TX. "
    "Answer guest questions in a concise, informal, and polite way, always to-the-point. "
    "Never add extra information, suggestions, or local tips unless the guest asks for it. "
    "Do not include fluff, chit-chat, upsell, or overly friendly phrases. "
    "Never use sign-offs like 'Enjoy your meal', 'Enjoy your meals', 'Enjoy!', 'Best', or your name. Only use a simple closing like 'Let me know if you need anything else' or 'Let me know if you need more recommendations' if it’s natural for the situation, and leave it off entirely if the message already feels complete. "
    "Never use multi-line replies unless absolutely necessary—keep replies to a single paragraph with greeting and answer together. "
    "Greet the guest casually using their first name if known, then answer their question immediately. "
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
        return property_info
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
    return property_info

@router.post("/slack/actions")
async def slack_actions(request: Request, background_tasks: BackgroundTasks):
    logging.info("slack_actions endpoint called.")
    try:
        form = await request.form()
        logging.info(f"Form received: {form}")
        payload_raw = form.get("payload")
        logging.info(f"Payload raw: {payload_raw}")
        payload = json.loads(payload_raw)
        logging.info(f"Parsed payload: {json.dumps(payload, indent=2)}")
    except Exception as e:
        logging.error(f"Error parsing Slack payload: {e}")
        return JSONResponse({"text": f"Error parsing Slack payload: {e}"})

    actions = payload.get("actions", [])
    action = actions[0] if actions else {}
    action_id = action.get("action_id")
    logging.info(f"Action ID: {action_id}")

    if action_id == "write_own":
        logging.info("Handling 'write_own' button click.")
        # -- Test modal for diagnostics --
        modal = {
            "type": "modal",
            "title": {"type": "plain_text", "text": "Write Your Own (Test Modal)", "emoji": True},
            "submit": {"type": "plain_text", "text": "Send", "emoji": True},
            "close": {"type": "plain_text", "text": "Cancel", "emoji": True},
            "blocks": [
                {
                    "type": "section",
                    "text": {"type": "plain_text", "text": "If you see this, your modal opened! (real modal logic can go here)"}
                }
            ]
        }
        return JSONResponse({"response_action": "push", "view": modal})

    if action_id == "edit":
        logging.info("Handling 'edit' button click.")
        # -- Test modal for diagnostics --
        modal = {
            "type": "modal",
            "title": {"type": "plain_text", "text": "Edit (Test Modal)", "emoji": True},
            "submit": {"type": "plain_text", "text": "Send", "emoji": True},
            "close": {"type": "plain_text", "text": "Cancel", "emoji": True},
            "blocks": [
                {
                    "type": "section",
                    "text": {"type": "plain_text", "text": "If you see this, your edit modal opened! (real modal logic can go here)"}
                }
            ]
        }
        return JSONResponse({"response_action": "push", "view": modal})

    # -- Optional: log other actions for further debugging --
    logging.info(f"Unhandled action_id: {action_id}. Full payload: {json.dumps(payload, indent=2)}")
    return JSONResponse({"text": f"Action received: {action_id}"})
