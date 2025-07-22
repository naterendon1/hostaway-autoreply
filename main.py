from fastapi import FastAPI
from slack_interactivity import router as slack_router
from pydantic import BaseModel
import os
import logging
import json
import re
from openai import OpenAI
from utils import fetch_hostaway_resource, get_similar_learning_examples

from datetime import datetime, timedelta

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
    "Never include the property’s address, city, or zip in your answer unless the guest specifically asks for it. "
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

def clean_ai_reply(reply: str) -> str:
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
    # Remove any address/city if not explicitly asked
    address_patterns = [
        r"(the )?house at [\d]+ [^,]+, [A-Za-z ]+",
        r"\d{3,} [A-Za-z0-9 .]+, [A-Za-z ]+",
        r"at [\d]+ [\w .]+, [\w ]+"
    ]
    for pattern in address_patterns:
        reply = re.sub(pattern, "the house", reply, flags=re.IGNORECASE)
    reply = ' '.join(reply.split())
    reply = reply.strip().replace(" ,", ",").replace(" .", ".")
    return reply.rstrip(",. ")

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

def get_property_type(listing_result: dict):
    # Looks for property type in Hostaway API response
    type_ = listing_result.get("type", "")
    if not type_:
        # fallback: try to guess from name or summary
        name = listing_result.get("name", "").lower()
        summary = listing_result.get("summary", "").lower()
        for prop in ["house", "condo", "cabin", "villa", "apartment", "studio", "townhome"]:
            if prop in name or prop in summary:
                return prop
        return "property"
    return type_.lower()

def is_availability_question(message: str) -> bool:
    patterns = [
        r"(are|is) (these|those|the) dates available",
        r"can i book (these|those|the) dates",
        r"(do you have|any) availability",
        r"are you available (on|for|during)",
        r"(is|are) the (house|property|place|condo|cabin|apartment|listing) (available|free)",
        r"available for (these|those|the) dates",
    ]
    message = message.lower()
    return any(re.search(p, message) for p in patterns)

def check_availability(listing_id, arrival, departure):
    calendar = fetch_hostaway_resource("calendar", listing_id)
    if not calendar or "result" not in calendar:
        return None  # Can't check
    date = datetime.strptime(arrival, "%Y-%m-%d")
    end_date = datetime.strptime(departure, "%Y-%m-%d")
    while date < end_date:
        day_str = date.strftime("%Y-%m-%d")
        day = next((d for d in calendar['result'] if d.get("date") == day_str), None)
        if not day or not day.get("available", True):
            return False
        date += timedelta(days=1)
    return True

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
    listing_name = "Unknown"
    reservation_status = payload.data.get("status", "Unknown").capitalize()

    if reservation_id:
        res = fetch_hostaway_resource("reservations", reservation_id)
        result = res.get("result", {}) if res else {}
        guest_name = result.get("guestName", guest_name)
        guest_first_name = result.get("guestFirstName", guest_first_name)
        check_in = result.get("arrivalDate", check_in)
        check_out = result.get("departureDate", check_out)
        guest_count = result.get("numberOfGuests", guest_count)
        if not listing_map_id:
            listing_map_id = result.get("listingId")
        if not guest_id:
            guest_id = result.get("guestId", "")

    property_info = get_property_info(listing_map_id)
    listing = fetch_hostaway_resource("listings", listing_map_id) if listing_map_id else {}
    listing_result = listing.get("result", {}) if listing else {}
    listing_name = listing_result.get("name", listing_name)
    property_type = get_property_type(listing_result)

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

    # ---- New: Handle direct date-availability questions using calendar ----
    if (
        is_availability_question(guest_message)
        and listing_map_id
        and check_in != "N/A"
        and check_out != "N/A"
        and reservation_status == "Sent"
    ):
        availability = check_availability(listing_map_id, check_in, check_out)
        if availability is None:
            ai_reply = f"Sorry, I wasn't able to check the calendar right now. I'll check and get back to you soon."
        elif availability:
            ai_reply = f"Yes, the {property_type} is available for your requested dates."
        else:
            ai_reply = f"Sorry, the {property_type} is already booked for those dates."
        ai_reply = clean_ai_reply(ai_reply)
    else:
        prompt = (
            f"{prev_answer}"
            f"A guest sent this message:\n{guest_message}\n\n"
            f"Property info:\n{property_info}\n"
            f"Conversation meta:\n"
            f"Property type: {property_type}\n"
            f"Dates: {check_in} to {check_out}\n"
            f"Guest name: {guest_first_name}\n"
            f"Guest count: {guest_count}\n"
            f"Reservation status: {reservation_status}\n"
            f"Communication type: {readable_communication}\n"
            "Respond according to your latest rules and tone, and use property info to make answers detailed and specific if appropriate."
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
            ai_reply = clean_ai_reply(ai_reply)
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
