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
    "If a guest sends a very brief or vague message (such as 'just following up?' or 'any update?'), use the previous conversation history to infer what they are referring to. If you cannot infer the topic, politely ask the guest to clarify their request. "
    "Always be helpful and accurate, but always brief."
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
    """Return property info string for prompt. Only includes needed fields."""
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

    # Fetch reservation for context
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

    # Determine needed fields for the listing, based on guest question
    fields_needed = determine_needed_fields(guest_message)
    listing_obj = fetch_hostaway_listing(listing_map_id) if listing_map_id else None
    listing_result = {}
    if listing_obj:
        # Only keep needed fields if full listing was fetched
        raw_result = listing_obj.get("result", {})
        listing_result = {k: v for k, v in raw_result.items() if k in fields_needed}

    # --- New: Listing name/type for Slack block ---
    listing_name = listing_result.get("name", "Unknown listing")
    property_type = get_property_type(listing_result)

    # Message thread for this conversation
    conversation_obj = fetch_hostaway_conversation(conversation_id) if conversation_id else None
    thread_messages = []
    if conversation_obj and conversation_obj.get("conversationMessages"):
        thread_messages = conversation_obj["conversationMessages"]

    # Add conversation thread context (always include last N messages)
    thread_context = ""
    if thread_messages:
        last_msgs = thread_messages[-5:]
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
    # --- NEW SAFE BLOCK for similar_examples ---
    if (
        similar_examples and
        isinstance(similar_examples[0], (list, tuple)) and
        len(similar_examples[0]) >= 3 and
        similar_examples[0][2]
    ):
        prev_answer = (
            "Previously, you (the host) replied to a similar guest question about this property:\n"
            f"\"{similar_examples[0][2]}\"\n"
            "Use this previous reply for *context only*; do not copy it verbatim. Write a new, clear answer in your own words if possible.\n"
        )

    # Add cancellation policy context
    cancellation_context = get_cancellation_policy_summary(listing_result, reservation_result)

    prompt = (
        f"{prev_answer}"
        f"{thread_context}"
        f"A guest sent this message:\n{guest_message}\n\n"
        f"Property info:\n{property_info}\n"
        f"Reservation context:\n{json.dumps(reservation_result)}\n"
        f"Cancellation: {cancellation_context}\n"
        f"Respond according to your latest rules and tone, and use all information above if needed."
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

    # --- Updated Slack blocks: Listing block at top ---
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
