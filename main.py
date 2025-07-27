import os
import logging
import json
import re
from fastapi import FastAPI
from slack_interactivity import router as slack_router, needs_clarification
from pydantic import BaseModel
from openai import OpenAI
from utils import (
    fetch_hostaway_resource,
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

def determine_needed_fields(guest_message: str):
    core = {"summary", "amenities", "houseManual", "type", "name"}
    extra = set()
    text = guest_message.lower()
    if any(x in text for x in ["how far", "distance", "close", "from", "to", "airport", "center"]):
        extra.update({"address", "city", "zipcode", "state"})
    if any(x in text for x in ["parking", "car", "vehicle"]):
        extra.update({"parking", "houseManual"})
    if any(x in text for x in ["price", "cost", "fee", "rate"]):
        extra.update({"price", "cleaningFee", "securityDepositFee", "currencyCode"})
    if "cancel" in text or "refund" in text:
        extra.update({"cancellationPolicy", "cancellationPolicyId"})
    if any(x in text for x in ["wifi", "internet", "tv", "netflix"]):
        extra.update({"amenities", "wifiUsername", "wifiPassword"})
    if any(x in text for x in ["bed", "sofa", "couch"]):
        extra.update({"bedroomsNumber", "bedsNumber"})
    if "guest" in text or "person" in text:
        extra.update({"personCapacity", "maxChildrenAllowed", "maxInfantsAllowed", "maxPetsAllowed", "guestsIncluded"})
    if "pet" in text or "dog" in text or "cat" in text:
        extra.update({"maxPetsAllowed", "amenities", "houseRules"})
    return core.union(extra)

def get_property_type(listing_result):
    prop = (listing_result.get("type") or "").lower()
    name = (listing_result.get("name") or "").lower()
    for t in ["house", "cabin", "condo", "apartment", "villa"]:
        if t in prop or t in name:
            return t
    return "home"

def clean_ai_reply(reply: str, property_type="home"):
    for s in ["Best,", "Cheers,", "Sincerely,", "Enjoy!", "[Your Name]"]:
        reply = reply.replace(s, "")
    lines = reply.split('\n')
    filtered = [line for line in lines if not any(sign in line for sign in ["Best", "Cheers", "Sincerely", "[Your Name]"])]
    reply = ' '.join(filtered)
    reply = re.sub(r"\d{3,} [\w .]+, [\w ]+", f"at the {property_type}", reply)
    reply = re.sub(r"at [\d]+ [\w .]+", f"at the {property_type}", reply)
    return reply.strip(" ,.").replace(" ,", ",").replace(" .", ".")

SYSTEM_PROMPT = (
    "You're a helpful vacation rental host. Reply casually and briefly using available info. "
    "Don't guess. If unsure, say you'll follow up. No sign-offs."
)

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

    fields = determine_needed_fields(guest_msg)
    listing_obj = fetch_hostaway_listing(listing_id) or {}
    listing = listing_obj.get("result", {})
    listing_info = get_property_info(listing_obj, fields)
    property_type = get_property_type(listing)
    listing_name = listing.get("name", "Unknown listing")

    convo = fetch_hostaway_conversation(conv_id) or {}
    msgs = convo.get("conversationMessages", [])
    context = "\n".join([f"{'Guest' if m['isIncoming'] else 'Host'}: {m['body']}" for m in msgs[-MAX_THREAD_MESSAGES:]])

    examples = get_similar_learning_examples(guest_msg, listing_id)
    prev_answer = ""
    if examples and examples[0][2]:
        prev_answer = f"Previously, you replied:\n\"{examples[0][2]}\"\nUse this only as context.\n"

    cancellation = get_cancellation_policy_summary(listing, res)

    prompt = (
        f"{context}\nGuest: \"{guest_msg}\"\n{prev_answer}"
        f"Listing Info:\n{listing_info}\n"
        f"Reservation Info:\n{json.dumps(res)}\n"
        f"Cancellation: {cancellation}\n"
        "---\nWrite a reply using only the data above. Don't guess. If unsure, say you'll check."
    )

    try:
        response = openai_client.chat.completions.create(
            model="gpt-4",
            messages=[
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": prompt}
            ]
        )
        ai_reply = clean_ai_reply(response.choices[0].message.content.strip(), property_type)
    except Exception as e:
        logging.error(f"❌ OpenAI error: {e}")
        ai_reply = "(Error generating reply.)"

    # <-- HERE IS THE META INCLUDING guest_name!
    payload_meta = {
        "conv_id": conv_id,
        "listing_id": listing_id,
        "guest_message": guest_msg,
        "ai_suggestion": ai_reply,
        "type": communication_type,
        "guest_id": guest_id,
        "guest_name": guest_name,  # <--- added here!
    }

    if needs_clarification(ai_reply):
        # Optionally: Pass guest_name to clarification logic here if needed.
        # ask_host_for_clarification(guest_msg, payload_meta, trigger_id=None)
        return {"status": "clarification_requested"}

    header = f"*New {communication_type.capitalize()}* from *{guest_name}*\nDates: *{check_in} → {check_out}*\nGuests: *{guest_count}* | Status: *{status}*"
    blocks = [
        {"type": "section", "text": {"type": "mrkdwn", "text": f"*Listing:* {listing_name} ({property_type})"}},
        {"type": "section", "text": {"type": "mrkdwn", "text": header}},
        {"type": "section", "text": {"type": "mrkdwn", "text": f"> {guest_msg}"}},
        {"type": "section", "text": {"type": "mrkdwn", "text": f"*Suggested Reply:*\n>{ai_reply}"}},
        {
            "type": "actions",
            "elements": [
                {"type": "button", "text": {"type": "plain_text", "text": "✅ Send"}, "value": json.dumps({**payload_meta, "reply": ai_reply}), "action_id": "send"},
                {"type": "button", "text": {"type": "plain_text", "text": "✏️ Edit"}, "value": json.dumps({**payload_meta, "draft": ai_reply}), "action_id": "edit"},
                {"type": "button", "text": {"type": "plain_text", "text": "📝 Write Your Own"}, "value": json.dumps(payload_meta), "action_id": "write_own"},
                {"type": "button", "text": {"type": "plain_text", "text": "🤔 Clarify"}, "value": json.dumps(payload_meta), "action_id": "clarify_request"}
            ]
        }
    ]

    from slack_sdk import WebClient
    slack_client = WebClient(token=SLACK_BOT_TOKEN)
    try:
        slack_client.chat_postMessage(channel=SLACK_CHANNEL, blocks=blocks, text="New message from guest")
    except Exception as e:
        logging.error(f"❌ Slack send error: {e}")

    return {"status": "ok"}

@app.get("/ping")
def ping():
    return {"status": "ok"}
