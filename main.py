import os
import logging
import json
import re
from fastapi import FastAPI
from slack_interactivity import router as slack_router
from pydantic import BaseModel
from openai import OpenAI
from utils import (
    fetch_hostaway_resource,
    fetch_hostaway_listing,
    fetch_hostaway_reservation,
    fetch_hostaway_conversation,
    get_cancellation_policy_summary,
    get_similar_learning_examples,
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

def determine_needed_fields(guest_message: str):
    core_listing_fields = {"summary", "amenities", "houseManual", "type", "name"}
    extra_fields = set()
    text = guest_message.lower()
    if any(keyword in text for keyword in [
        "how far", "distance", "close", "how close", "near", "nearest", "proximity", "from", "to", "airport", "downtown", "center", "stadium"
    ]):
        extra_fields.update({"address", "city", "zipcode", "state"})
    if "parking" in text or "car" in text or "vehicle" in text:
        extra_fields.update({"parking", "amenities", "houseManual"})
    if "price" in text or "cost" in text or "fee" in text or "rate" in text:
        extra_fields.update({"price", "cleaningFee", "securityDepositFee", "currencyCode"})
    if "cancel" in text or "refund" in text:
        extra_fields.update({"cancellationPolicy", "cancellationPolicyId"})
    if any(x in text for x in ["wifi", "internet", "tv", "cable", "smart", "stream", "netflix"]):
        extra_fields.update({"amenities", "houseManual", "wifiUsername", "wifiPassword"})
    if any(x in text for x in ["bed", "sofa", "couch", "sleep", "bedroom"]):
        extra_fields.update({"bedroomsNumber", "bedsNumber", "guestBathroomsNumber"})
    if "guest" in text or "person" in text or "max" in text or "limit" in text or "occupancy" in text:
        extra_fields.update({"personCapacity", "maxChildrenAllowed", "maxInfantsAllowed", "maxPetsAllowed", "guestsIncluded"})
    if "pet" in text or "dog" in text or "cat" in text or "animal" in text:
        extra_fields.update({"maxPetsAllowed", "amenities", "houseRules"})
    return core_listing_fields.union(extra_fields)

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
    "You're a helpful vacation rental host. Always respond casually, briefly, and to the point. "
    "Use the guest's first name if known. "
    "Use only the provided property and reservation info — do not guess. "
    "If something is missing, say you'll check and follow up. "
    "No chit-chat, no extra tips, no sign-offs."
)

class HostawayUnifiedWebhook(BaseModel):
    object: str
    event: str
    accountId: int
    data: dict
    body: str = None
    listingName: str = None
    date: str = None

def get_property_info(listing_result, fields_needed):
    lines = []
    for field in fields_needed:
        val = listing_result.get(field, "")
        if not val:
            continue
        if isinstance(val, (list, dict)):
            val = json.dumps(val)
        lines.append(f"{field}: {val}")
    return "\n".join(lines)

@app.post("/unified-webhook")
async def unified_webhook(payload: HostawayUnifiedWebhook):
    logging.info(f"📬 Webhook received: {json.dumps(payload.dict(), indent=2)}")
    if payload.event != "message.received" or payload.object != "conversationMessage":
        return {"status": "ignored"}

    guest_message = payload.data.get("body", "")
    conversation_id = payload.data.get("conversationId")
    communication_type = payload.data.get("communicationType", "channel")
    reservation_id = payload.data.get("reservationId")
    listing_map_id = payload.data.get("listingMapId")
    guest_id = payload.data.get("userId", "")

    guest_name = "Guest"
    guest_first_name = "Guest"
    check_in = "N/A"
    check_out = "N/A"
    guest_count = "N/A"
    reservation_status = payload.data.get("status", "Unknown").capitalize()

    reservation_obj = fetch_hostaway_reservation(reservation_id) if reservation_id else None
    reservation_result = reservation_obj.get("result", {}) if reservation_obj else {}

    if reservation_result:
        guest_name = reservation_result.get("guestName", guest_name)
        guest_first_name = reservation_result.get("guestFirstName", guest_first_name)
        check_in = reservation_result.get("arrivalDate", check_in)
        check_out = reservation_result.get("departureDate", check_out)
        guest_count = reservation_result.get("numberOfGuests", guest_count)
        if not listing_map_id:
            listing_map_id = reservation_result.get("listingId")
        if not guest_id:
            guest_id = reservation_result.get("guestId", "")

    fields_needed = determine_needed_fields(guest_message)
    listing_obj = fetch_hostaway_listing(listing_map_id) if listing_map_id else None
    listing_result = {}
    if listing_obj:
        raw_result = listing_obj.get("result", {})
        listing_result = {k: v for k, v in raw_result.items() if k in fields_needed}

    listing_name = listing_result.get("name", "Unknown listing")
    property_type = get_property_type(listing_result)

    conversation_obj = fetch_hostaway_conversation(conversation_id) if conversation_id else None
    thread_messages = []
    if conversation_obj and conversation_obj.get("conversationMessages"):
        thread_messages = conversation_obj["conversationMessages"]

    thread_context = ""
    if thread_messages:
        last_msgs = thread_messages[-MAX_THREAD_MESSAGES:]
        thread_context = "Conversation history:\n"
        for msg in last_msgs:
            who = "Guest" if msg.get("isIncoming") else "Host"
            body = msg.get("body", "")
            thread_context += f"{who}: {body}\n"
    else:
        logging.warning(f"[Hostaway AutoResponder] No thread messages found for conversation_id={conversation_id}")

    property_info = get_property_info(listing_result, fields_needed)
    similar_examples = get_similar_learning_examples(guest_message, listing_map_id)
    prev_answer = ""
    if (
        similar_examples and
        isinstance(similar_examples[0], (list, tuple)) and
        len(similar_examples[0]) >= 3 and
        similar_examples[0][2]
    ):
        prev_answer = (
            "Previously, you (the host) replied to a similar guest question about this property:\n"
            f"\"{similar_examples[0][2]}\"\n"
            "Use this previous reply for context only. Write a new answer in your own words.\n"
        )

    cancellation_context = get_cancellation_policy_summary(listing_result, reservation_result)

    prompt = (
        f"{thread_context}\n"
        f"Guest's latest message: \"{guest_message}\"\n"
        f"{prev_answer}\n"
        f"Listing info:\n{property_info}\n"
        f"Reservation info:\n{json.dumps(reservation_result)}\n"
        f"Cancellation policy: {cancellation_context}\n"
        "\n---\n"
        "Write a reply using the information above. "
        "Reference property or reservation details where helpful. "
        "Do not guess or invent anything. If information is missing, say you'll follow up."
    )

    try:
        response = openai_client.chat.completions.create(
            model="gpt-4",
            messages=[
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": prompt}
            ]
        )
        ai_reply = response.choices[0].message.content.strip()
        ai_reply = clean_ai_reply(ai_reply, property_type)
    except Exception as e:
        logging.error(f"❌ OpenAI error: {e}")
        ai_reply = "(Error generating reply.)"

    header = (
        f"*New {communication_type.capitalize()}* from *{guest_first_name}*\n"
        f"Dates: *{check_in} → {check_out}*\n"
        f"Guests: *{guest_count}* | Status: *{reservation_status}*"
    )

    slack_button_payload = {
        "conv_id": conversation_id,
        "listing_id": listing_map_id,
        "guest_message": guest_message,
        "ai_suggestion": ai_reply,
        "type": communication_type,
        "guest_id": guest_id
    }

    blocks = [
        {
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": f"*Listing:* {listing_name} ({property_type})"
            }
        },
        {
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": header
            }
        },
        {
            "type": "section",
            "text": {"type": "mrkdwn", "text": f"> {guest_message}"}
        },
        {
            "type": "section",
            "text": {"type": "mrkdwn", "text": f"*Suggested Reply:*\n>{ai_reply}"}
        },
        {
            "type": "actions",
            "elements": [
                {
                    "type": "button",
                    "text": {"type": "plain_text", "text": "✅ Send"},
                    "value": json.dumps({**slack_button_payload, "reply": ai_reply}),
                    "action_id": "send"
                },
                {
                    "type": "button",
                    "text": {"type": "plain_text", "text": "✏️ Edit"},
                    "value": json.dumps({**slack_button_payload, "draft": ai_reply}),
                    "action_id": "edit"
                },
                {
                    "type": "button",
                    "text": {"type": "plain_text", "text": "📝 Write Your Own"},
                    "value": json.dumps(slack_button_payload),
                    "action_id": "write_own"
                }
            ]
        }
    ]

    from slack_sdk.web import WebClient
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
