import os
import logging
import json
import re
from fastapi import FastAPI
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from slack_interactivity import router as slack_router
from utils import (
    fetch_hostaway_resource,
    get_similar_learning_examples
)
from openai import OpenAI

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

class HostawayUnifiedWebhook(BaseModel):
    object: str
    event: str
    accountId: int
    data: dict
    body: str = None
    listingName: str = None
    date: str = None

def get_property_type(listing_result):
    prop_type = (listing_result.get("type") or "").lower()
    name = (listing_result.get("name") or "").lower()
    for t in ["house", "cabin", "condo", "apartment", "villa", "bungalow", "cottage", "suite"]:
        if t in prop_type or t in name:
            return t
    return "home"

def summarize_listing(listing):
    fields = [
        "name", "propertyTypeId", "description", "houseRules", "address", "city", "zipcode", "bedroomsNumber",
        "bedsNumber", "bathroomsNumber", "personCapacity", "maxPetsAllowed", "minNights", "maxNights", "cleaningFee",
        "instantBookable", "cancellationPolicy", "wifiUsername", "wifiPassword", "listingAmenities", "listingBedTypes",
        "checkInTimeStart", "checkInTimeEnd", "checkOutTime"
    ]
    result = listing.get("result", {}) if listing else {}
    summary = {field: result.get(field) for field in fields}
    summary["property_type"] = get_property_type(result)
    return summary, result

def summarize_reservation(res):
    fields = [
        "id", "listingMapId", "channelId", "channelName", "guestName", "guestFirstName", "guestLastName",
        "numberOfGuests", "arrivalDate", "departureDate", "status", "totalPrice", "currency", "isInstantBooked"
    ]
    result = res.get("result", {}) if res else {}
    summary = {field: result.get(field) for field in fields}
    return summary, result

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
        r"at [\d]+ [\w .]+, [\w ]+",
        r"at [A-Za-z0-9 ,/\-\(\)\']+"  # property names
    ]
    for pattern in address_patterns:
        reply = re.sub(pattern, f"the {property_type}", reply, flags=re.IGNORECASE)
    reply = ' '.join(reply.split())
    reply = reply.strip().replace(" ,", ",").replace(" .", ".")
    return reply.rstrip(",. ")

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

    guest_first_name = "Guest"
    check_in = "N/A"
    check_out = "N/A"
    guest_count = "N/A"
    listing_name = "Unknown"
    reservation_status = payload.data.get("status", "Unknown").capitalize()

    # --- Listing and Reservation Objects ---
    listing = fetch_hostaway_resource("listings", listing_map_id) if listing_map_id else {}
    listing_summary, listing_result = summarize_listing(listing)
    property_type = listing_summary.get("property_type", "home")
    logging.info(f"Listing summary: {json.dumps(listing_summary, indent=2)}")

    res = fetch_hostaway_resource("reservations", reservation_id) if reservation_id else {}
    res_summary, res_result = summarize_reservation(res)
    logging.info(f"Reservation summary: {json.dumps(res_summary, indent=2)}")

    if res_summary.get("guestFirstName"):
        guest_first_name = res_summary.get("guestFirstName")
    if res_summary.get("arrivalDate"):
        check_in = res_summary.get("arrivalDate")
    if res_summary.get("departureDate"):
        check_out = res_summary.get("departureDate")
    if res_summary.get("numberOfGuests"):
        guest_count = res_summary.get("numberOfGuests")
    if not guest_id and res_result.get("guestId"):
        guest_id = res_result.get("guestId")
    if listing_summary.get("name"):
        listing_name = listing_summary.get("name")
    if res_summary.get("status"):
        reservation_status = res_summary.get("status").capitalize()

    # Give AI *all* info
    similar_examples = get_similar_learning_examples(guest_message, listing_map_id)
    prev_answer = ""
    if similar_examples:
        prev_answer = f"Previously, you (the host) replied to a similar guest question about this property: \"{similar_examples[0][2]}\". Use this as a guide if it fits.\n"

    readable_communication = {
        "channel": "Channel Message",
        "email": "Email",
        "sms": "SMS",
        "whatsapp": "WhatsApp",
        "airbnb": "Airbnb",
        "vrbo": "VRBO",
        "bookingcom": "Booking.com",
    }.get(communication_type, communication_type.capitalize())

    # Compose prompt with *full context*
    prompt = (
        f"{prev_answer}"
        f"Guest message:\n{guest_message}\n\n"
        f"Reservation info:\n{json.dumps(res_summary, indent=2)}\n"
        f"Listing info:\n{json.dumps(listing_summary, indent=2)}\n"
        "Respond according to your latest rules and tone, and use listing/reservation info to make answers specific and accurate."
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
        f"*New {readable_communication}* from *{guest_first_name}* at *{listing_name}*\n"
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
        {"type": "section", "text": {"type": "mrkdwn", "text": header}},
        {"type": "section", "text": {"type": "mrkdwn", "text": f"> {guest_message}"}},
        {"type": "section", "text": {"type": "mrkdwn", "text": f"*Suggested Reply:*\n>{ai_reply}"}},
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
