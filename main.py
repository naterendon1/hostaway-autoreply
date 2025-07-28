import os
import logging
import json
import re
from fastapi import FastAPI
from slack_interactivity import router as slack_router, needs_clarification
from pydantic import BaseModel
from openai import OpenAI
from utils import (
    fetch_hostaway_listing,
    fetch_hostaway_reservation,
    fetch_hostaway_conversation,
    get_cancellation_policy_summary,
    get_similar_learning_examples,
    get_property_info,
)

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

# --- The most important system prompt ---
SYSTEM_PROMPT_ANSWER = (
    "You are a helpful, informal, and friendly vacation rental host. "
    "Reply to guests as if texting a peer—clear, concise, and casual (think millennial tone). "
    "Don’t restate info the guest already sees. Only mention a property detail if it answers their question. "
    "Never say you’re checking or following up unless they *explicitly* ask about availability or something unknown. "
    "No formal greetings, no copy-paste listing descriptions, and keep it under 200 characters unless the question needs more. "
    "Use contractions, skip filler, and just answer what’s needed. Never restate the guest's message."
)

# --- Use this for ALL AI generations, including clarify/improve_with_ai ---
def make_ai_reply(prompt, system_prompt=SYSTEM_PROMPT_ANSWER):
    try:
        response = openai_client.chat.completions.create(
            model="gpt-4",
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": prompt}
            ]
        )
        return response.choices[0].message.content.strip()
    except Exception as e:
        logging.error(f"❌ OpenAI error: {e}")
        return "(Error generating reply.)"

def clean_ai_reply(reply: str):
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

    # --- Decide what fields to use, but ALWAYS add 'name', 'description', 'city' ---
    core_fields = {"name", "description", "city"}
    requested_fields = set(core_fields)

    # Optionally, you can make an AI call to pick extra fields based on the guest message if you wish
    # For brevity, we'll stick to core and only fetch extra as needed

    listing_obj = fetch_hostaway_listing(listing_id) or {}
    listing = listing_obj.get("result", {})
    property_details = {k: listing.get(k, "") for k in requested_fields if k in listing}

    property_str = "\n".join([f"{k}: {v}" for k, v in property_details.items() if v])
    if not property_str:
        property_str = "(no extra details available)"

    convo = fetch_hostaway_conversation(conv_id) or {}
    msgs = convo.get("conversationMessages", [])
    # Only the last 10, for context (optional, not shown to the guest)
    context = "\n".join([f"{'Guest' if m['isIncoming'] else 'Host'}: {m['body']}" for m in msgs[-MAX_THREAD_MESSAGES:]])

    prev_examples = get_similar_learning_examples(guest_msg, listing_id)
    prev_answer = ""
    if prev_examples and prev_examples[0][2]:
        prev_answer = f"Previously, you replied:\n\"{prev_examples[0][2]}\"\nUse this only as context.\n"

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
                {"type": "button", "text": {"type": "plain_text", "text": "📝 Write Your Own"}, "value": json.dumps({**button_meta_minimal, "action": "write_own"}), "action_id": "write_own"},
                {"type": "button", "text": {"type": "plain_text", "text": "🤔 Clarify"}, "value": json.dumps({**button_meta_minimal, "action": "clarify_request"}), "action_id": "clarify_request"}
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
