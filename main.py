import os
import logging
import json
import re
from fastapi import FastAPI
from slack_interactivity import router as slack_router
from pydantic import BaseModel
from openai import OpenAI
from utils import (
    fetch_hostaway_listing,
    fetch_hostaway_reservation,
    fetch_hostaway_conversation,
    get_cancellation_policy_summary,
    get_similar_learning_examples,
    get_property_info,
    clean_ai_reply,
)

logging.basicConfig(level=logging.INFO)

# --- ENVIRONMENT VARIABLE CHECKS ---
REQUIRED_ENV_VARS = [
    "HOSTAWAY_CLIENT_ID",
    "HOSTAWAY_CLIENT_SECRET",
    "OPENAI_API_KEY",
    "SLACK_CHANNEL",
    "SLACK_BOT_TOKEN"
]
missing = [v for v in REQUIRED_ENV_VARS if not os.getenv(v)]
if missing:
    raise RuntimeError(f"Missing required environment variables: {missing}")

HOSTAWAY_CLIENT_ID = os.getenv("HOSTAWAY_CLIENT_ID")
HOSTAWAY_CLIENT_SECRET = os.getenv("HOSTAWAY_CLIENT_SECRET")
HOSTAWAY_API_BASE = "https://api.hostaway.com/v1"
SLACK_CHANNEL = os.getenv("SLACK_CHANNEL")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
SLACK_BOT_TOKEN = os.getenv("SLACK_BOT_TOKEN")

app = FastAPI()
app.include_router(slack_router)

openai_client = OpenAI(api_key=OPENAI_API_KEY)
MAX_THREAD_MESSAGES = 10  # how many to include for context

class HostawayUnifiedWebhook(BaseModel):
    object: str
    event: str
    accountId: int
    data: dict
    body: str = None
    listingName: str = None
    date: str = None

# --- System prompt: clear, modern, friendly, concise, but not forced millennial ---
SYSTEM_PROMPT_ANSWER = (
    "You are a helpful, friendly vacation rental host. "
    "Reply as if texting a peer—modern, clear, and informal, but professional. "
    "Avoid emojis, do not restate the guest's message, and keep replies concise (preferably under 200 characters unless necessary). "
    "Mention property details only if they directly answer the question. "
    "Don't use copy-paste listing descriptions or formal closings. "
    "Never say you're checking or following up unless the guest asks for something unknown. "
    "Only answer what's needed, no filler."
)

def make_ai_reply(prompt, system_prompt=SYSTEM_PROMPT_ANSWER):
    try:
        response = openai_client.chat.completions.create(
            model="gpt-4",
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": prompt}
            ],
            timeout=20  # Always set timeout
        )
        return response.choices[0].message.content.strip()
    except Exception as e:
        logging.error(f"❌ OpenAI error: {e}")
        return "(Error generating reply.)"

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
            logging.info("🧾 Empty message skipped.")
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

    core_fields = {"name", "description", "city"}
    requested_fields = set(core_fields)

    listing_obj = fetch_hostaway_listing(listing_id) or {}
    listing = listing_obj.get("result", {})
    property_details = {k: listing.get(k, "") for k in requested_fields if k in listing}

    property_str = "\n".join([f"{k}: {v}" for k, v in property_details.items() if v])
    if not property_str:
        property_str = "(no extra details available)"

    # --- Fetch message thread for full context ---
    convo_obj = fetch_hostaway_conversation(conv_id) or {}
    msgs = []
    if "result" in convo_obj and "conversationMessages" in convo_obj["result"]:
        msgs = convo_obj["result"]["conversationMessages"]
    # Compose conversation thread (last N messages), newest last
    conversation_context = []
    for m in msgs[-MAX_THREAD_MESSAGES:]:
        sender = "Guest" if m.get("isIncoming") else "Host"
        body = m.get("body", "")
        if not body:
            continue
        conversation_context.append(f"{sender}: {body}")
    context_str = "\n".join(conversation_context)

    prev_examples = get_similar_learning_examples(guest_msg, listing_id)
    prev_answer = ""
    if prev_examples and prev_examples[0][2]:
        prev_answer = f"Previously, you replied:\n\"{prev_examples[0][2]}\"\nUse this only as context.\n"

    cancellation = get_cancellation_policy_summary(listing, res)

    # ...after loading guest_msg, reservation (res), listing_id, check_in, check_out, etc.

calendar_summary = ""  # Default: nothing

# Define extension/availability keywords
extension_keywords = [
    "extend", "stay longer", "add night", "extra night", "stay an extra", 
    "another night", "check out late", "can we stay", "can I stay", "available", "availability"
]

# Simple extension/availability intent detection
if any(kw in guest_msg.lower() for kw in extension_keywords):
    # Attempt to fetch the NEXT day after checkout for extension request
    try:
        req_date = (datetime.strptime(check_out, "%Y-%m-%d") + timedelta(days=1)).strftime("%Y-%m-%d")
    except Exception:
        req_date = check_out  # fallback
    calendar_json = fetch_hostaway_calendar(listing_id, req_date, req_date)
    if calendar_json:
        available = is_date_available(calendar_json, req_date)
        if available:
            calendar_summary = f"The night of {req_date} is available if you'd like to extend."
        else:
            calendar_summary = f"Sorry, the night of {req_date} is already booked."
    else:
        calendar_summary = "Calendar information is currently unavailable."
else:
    calendar_summary = ""

# Pass this info to your AI prompt:
ai_prompt = (
    f"Guest name: {guest_name}\n"
    f"Guest message: \"{guest_msg}\"\n"
    f"{prev_answer}"
    f"Property details:\n{property_str}\n"
    f"Reservation Info:\n{json.dumps(res)}\n"
    f"Cancellation: {cancellation}\n"
    f"Calendar Info: {calendar_summary}\n"  # <<<<<<<<<<
    "---\nWrite a reply to the guest. Answer based on all context above. Do not confirm dates unless calendar info above confirms it."
)


    # --- Improved AI prompt with thread context ---
    ai_prompt = (
        f"Here is the most recent message thread with a guest (newest last):\n"
        f"{context_str}\n\n"
        f"{prev_answer}"
        f"Property details:\n{property_str}\n"
        f"Reservation Info:\n{json.dumps(res)}\n"
        f"Cancellation: {cancellation}\n"
        "---\nWrite a clear, modern, friendly, concise reply to the most recent guest message."
    )

    ai_reply = clean_ai_reply(make_ai_reply(ai_prompt))

    # --- Button/meta block only contains IDs! ---
    button_meta_minimal = {
        "conv_id": conv_id,
        "listing_id": listing_id,
        "guest_id": guest_id,
        "type": communication_type,
        "guest_name": guest_name,
        "guest_message": guest_msg,  # Pass for modals!
        "ai_suggestion": ai_reply,
    }

    blocks = [
        {"type": "section", "text": {"type": "mrkdwn", "text": f"*Listing:* {listing.get('name', 'Unknown listing')}" }},
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
