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

# --- Retrieval-augmented system prompt ---
SYSTEM_PROMPT_FIELD_SELECTION = (
    "You are a knowledgeable, friendly property host. When you receive a guest’s question, first decide which property details you need. "
    "If you need info, respond only with a comma-separated list of fields (e.g., wifiUsername, wifiPassword, bedroomsNumber). "
    "If you have all info you need, reply with \"ready\". Wait for the property info, then write your reply as if you know the house personally. "
    "Never mention “listing,” “database,” or where info came from—just answer as the host. Keep it warm, concise, and use the guest’s name."
)

SYSTEM_PROMPT_ANSWER = (
    "You are a knowledgeable, friendly property host. Write a warm, clear, helpful response to the guest using the property details provided. "
    "If a property detail is blank, say you'll check and follow up. Never mention you checked a listing or database. Use the guest’s name and property’s name in a natural, human way. "
    "Keep it concise, inviting, and use a conversational host tone."
)

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

    # --- Step 1: Let AI decide which fields are needed ---
    try:
        field_response = openai_client.chat.completions.create(
            model="gpt-4",
            messages=[
                {"role": "system", "content": SYSTEM_PROMPT_FIELD_SELECTION},
                {"role": "user", "content": f'Guest message: "{guest_msg}"'}
            ]
        ).choices[0].message.content.strip()
    except Exception as e:
        logging.error(f"❌ OpenAI field selection error: {e}")
        field_response = "ready"

    core_fields = {"name", "description", "city"}
    requested_fields = set()

    if field_response.lower() != "ready":
        requested_fields.update([f.strip() for f in field_response.split(",") if f.strip()])
    requested_fields = requested_fields.union(core_fields)

    listing_obj = fetch_hostaway_listing(listing_id) or {}
    listing = listing_obj.get("result", {})
    property_details = {k: listing.get(k, "") for k in requested_fields if k in listing}

    property_str = "\n".join([f"{k}: {v}" for k, v in property_details.items() if v])
    if not property_str:
        property_str = "(no extra details available)"

    convo = fetch_hostaway_conversation(conv_id) or {}
    msgs = convo.get("conversationMessages", [])
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
        "---\nWrite a warm, clear, and helpful reply as the host, using the info above. If a detail is missing, say you'll check and follow up. Do not mention 'listing' or 'database'."
    )

    try:
        response = openai_client.chat.completions.create(
            model="gpt-4",
            messages=[
                {"role": "system", "content": SYSTEM_PROMPT_ANSWER},
                {"role": "user", "content": ai_prompt}
            ]
        )
        ai_reply = clean_ai_reply(response.choices[0].message.content.strip())
    except Exception as e:
        logging.error(f"❌ OpenAI answer generation error: {e}")
        ai_reply = "(Error generating reply.)"

    # --- ONLY store IDs (not giant strings) in button values! ---
    button_meta_minimal = {
        "conv_id": conv_id,
        "listing_id": listing_id,
        "guest_id": guest_id,
        "type": communication_type,
        "guest_name": guest_name
    }
    # All context for modals is in private_metadata!
    modal_metadata = {
        **button_meta_minimal,
        "guest_message": guest_msg,
        "ai_suggestion": ai_reply,
    }

    # Button values now small and safe!
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
        # For modals, pass full context in private_metadata, not in button value!
        slack_client.chat_postMessage(
            channel=SLACK_CHANNEL,
            blocks=blocks,
            text="New message from guest",
            metadata={"private_metadata": json.dumps(modal_metadata)}  # <-- optional, can be used if needed
        )
    except Exception as e:
        logging.error(f"❌ Slack send error: {e}")

    return {"status": "ok"}

@app.get("/ping")
def ping():
    return {"status": "ok"}
