# main.py
import os
import logging
import json
import re
from fastapi import FastAPI, Request
from slack_interactivity import router as slack_router
from pydantic import BaseModel
from openai import OpenAI
from utils import (
    fetch_hostaway_listing,
    fetch_hostaway_reservation,
    get_cancellation_policy_summary,
    get_similar_learning_examples,
    make_ai_reply,
    store_ai_feedback
)
from db import get_similar_response, init_db

logging.basicConfig(level=logging.INFO)

HOSTAWAY_CLIENT_ID = os.getenv("HOSTAWAY_CLIENT_ID")
HOSTAWAY_CLIENT_SECRET = os.getenv("HOSTAWAY_CLIENT_SECRET")
HOSTAWAY_API_BASE = "https://api.hostaway.com/v1"
SLACK_CHANNEL = os.getenv("SLACK_CHANNEL")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
SLACK_BOT_TOKEN = os.getenv("SLACK_BOT_TOKEN")

app = FastAPI()
app.include_router(slack_router)

openai_client = OpenAI(api_key=OPENAI_API_KEY)
MAX_THREAD_MESSAGES = 10

class HostawayUnifiedWebhook(BaseModel):
    object: str
    event: str
    accountId: int
    data: dict
    body: str = None
    listingName: str = None
    date: str = None

def strip_emojis(text: str) -> str:
    text = re.sub(r':[a-zA-Z0-9_+-]+:', '', text)
    text = re.sub(r'[^\w\s,.!?\'\"-]', '', text)
    return text

def clean_ai_reply(reply: str) -> str:
    bad_signoffs = ["Enjoy your meal", "Enjoy your meals", "Enjoy!", "Best,", "Best regards,", "Cheers,", "Sincerely,", "[Your Name]", "Best", "Sincerely"]
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
    reply = reply.rstrip(",. ")
    reply = strip_emojis(reply)
    return reply

@app.on_event("startup")
def startup_event():
    init_db()

@app.post("/unified-webhook")
async def unified_webhook(payload: HostawayUnifiedWebhook):
    logging.info(f"📬 Webhook received: {json.dumps(payload.dict(), indent=2)}")
    if payload.event != "message.received" or payload.object != "conversationMessage":
        return {"status": "ignored"}

    guest_msg = payload.data.get("body", "")
    conv_id = payload.data.get("conversationId")
    reservation_id = payload.data.get("reservationId")
    listing_id = payload.data.get("listingMapId")
    guest_id = payload.data.get("userId", "")
    communication_type = payload.data.get("communicationType", "channel")

    if not guest_msg:
        if payload.data.get("attachments"):
            logging.info("📷 Skipping image-only message.")
        else:
            logging.info("🗞 Empty message skipped.")
        return {"status": "ignored"}

    reservation = fetch_hostaway_reservation(reservation_id) or {}
    res = reservation.get("result", {})
    guest_name = res.get("guestFirstName", "Guest")
    check_in = res.get("arrivalDate", "N/A")
    check_out = res.get("departureDate", "N/A")
    guest_count = res.get("numberOfGuests", "N/A")
    status = payload.data.get("status", "Unknown").capitalize()

    if not listing_id:
        listing_id = res.get("listingId")
    if not guest_id:
        guest_id = res.get("guestId", "")

    listing_obj = fetch_hostaway_listing(listing_id) or {}
    listing = listing_obj.get("result", {})

    core_fields = {"name", "description", "city"}
    property_details = {k: listing.get(k, "") for k in core_fields}
    property_str = "\n".join([f"{k}: {v}" for k, v in property_details.items() if v]) or "(no extra details available)"

    prev_examples = get_similar_learning_examples(guest_msg, listing_id)
    prev_answer = f"Previously, you replied:\n\"{prev_examples[0][2]}\"\nUse this only as context.\n" if prev_examples and prev_examples[0][2] else ""

    cancellation = get_cancellation_policy_summary(listing, res)
    ai_prompt = (
        f"Guest name: {guest_name}\n"
        f"Guest message: \"{guest_msg}\"\n"
        f"{prev_answer}"
        f"Property details:\n{property_str}\n"
        f"Reservation Info:\n{json.dumps(res)}\n"
        f"Cancellation: {cancellation}\n"
        "---\nWrite a reply to the guest. Remember: clear, concise, informal, millennial tone. No listing details unless needed. No restating guest's message."
    )

    custom_response = get_similar_response(listing_id, guest_msg)
    ai_reply = clean_ai_reply(custom_response) if custom_response else clean_ai_reply(make_ai_reply(ai_prompt, previous_examples=prev_examples))

    button_meta_minimal = {
        "conv_id": conv_id,
        "listing_id": listing_id,
        "guest_id": guest_id,
        "type": communication_type,
        "guest_name": guest_name,
        "guest_message": guest_msg,
        "ai_suggestion": ai_reply,
    }

    blocks = [
        {"type": "section", "text": {"type": "mrkdwn", "text": f"*Listing:* {listing.get('name', 'Unknown listing')}"}},
        {"type": "section", "text": {"type": "mrkdwn", "text": f"*New {communication_type.capitalize()}* from *{guest_name}*\nDates: *{check_in} → {check_out}*\nGuests: *{guest_count}* | Status: *{status}*"}},
        {"type": "section", "text": {"type": "mrkdwn", "text": f"> {guest_msg}"}},
        {"type": "section", "text": {"type": "mrkdwn", "text": f"*Suggested Reply:*\n>{ai_reply}"}},
        {
            "type": "actions",
            "elements": [
                {"type": "button", "text": {"type": "plain_text", "text": "✅ Send"}, "value": json.dumps({**button_meta_minimal, "action": "send"}), "action_id": "send"},
                {"type": "button", "text": {"type": "plain_text", "text": "✏️ Edit"}, "value": json.dumps({**button_meta_minimal, "action": "edit"}), "action_id": "edit"},
                {"type": "button", "text": {"type": "plain_text", "text": "📝 Write Your Own"}, "value": json.dumps({**button_meta_minimal, "action": "write_own"}), "action_id": "write_own"}
            ]
        },
        {
            "type": "actions",
            "elements": [
                {"type": "button", "text": {"type": "plain_text", "text": "👍 Helpful"}, "value": json.dumps({**button_meta_minimal, "rating": "up"}), "action_id": "rate_up"},
                {"type": "button", "text": {"type": "plain_text", "text": "👎 Unhelpful"}, "value": json.dumps({**button_meta_minimal, "rating": "down"}), "action_id": "rate_down"}
            ]
        }
    ]

    from slack_sdk import WebClient
    slack_client = WebClient(token=SLACK_BOT_TOKEN)
    try:
        slack_client.chat_postMessage(
            channel=SLACK_CHANNEL,
            blocks=blocks,
            text="New message from guest"
        )
    except Exception as e:
        logging.error(f"❌ Slack send error: {e}")

    return {"status": "ok"}

@app.get("/ping")
def ping():
    return {"status": "ok"}

@app.get("/")
def root():
    return {"status": "ok", "message": "Auto-Reply API is running"}
