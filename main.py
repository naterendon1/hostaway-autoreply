from fastapi import FastAPI
from slack_interactivity import router as slack_router
from pydantic import BaseModel
import os
import logging
import json
from openai import OpenAI
from utils import fetch_hostaway_resource
from db import init_db, save_custom_response, get_similar_response

logging.basicConfig(level=logging.INFO)

HOSTAWAY_CLIENT_ID = os.getenv("HOSTAWAY_CLIENT_ID")
HOSTAWAY_CLIENT_SECRET = os.getenv("HOSTAWAY_CLIENT_SECRET")
HOSTAWAY_API_BASE = "https://api.hostaway.com/v1"
SLACK_CHANNEL = os.getenv("SLACK_CHANNEL")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
SLACK_BOT_TOKEN = os.getenv("SLACK_BOT_TOKEN")

app = FastAPI()
app.include_router(slack_router)

init_db()

openai_client = OpenAI(api_key=OPENAI_API_KEY)

system_prompt = (
    "You are a highly knowledgeable, friendly vacation rental host for homes in Crystal Beach, TX, Austin, TX, Galveston, TX, and Georgetown, TX. "
    "Use an informal, chill tone. Always greet the guest using their first name at the beginning of your reply, unless you are already in an active back-and-forth. "
    "Never invent information. If you do not know the answer from the listing, house manual, or available info, say something like 'Let me get back to you on that!' "
    "If the guest asks about restaurants or attractions, use the property address for context and suggest nearby options. "
    "You know these Texas towns and their attractions inside and out, but don't make up specific house rules, amenities, or details you can't confirm. "
    "If a guest is only inquiring about dates or making a request, always check the calendar to confirm availability before agreeing. "
    "For early check-in or late check-out, check availability first, then mention a fee. "
    "Answer amenity or house detail questions directly, with no fluff. "
    "Refund requests outside the cancellation policy: explain refunds are only possible if the dates rebook. "
    "If a guest cancels for an emergency, show empathy and refer to Airbnb’s or the platform's extenuating circumstances policy. "
    "Contact instructions: guests can reach out via platform messenger, call, or text. "
    "Summarize amenities if asked what's included. "
    "For extra guests/visitors: only registered guests are allowed unless pre-approved. "
    "Maintain a helpful, problem-solving, fast, clear attitude. "
    "For pets: the property is not pet-friendly, ESAs are not allowed, but service animals are welcome by law. "
    "Remind guests to respect neighbors, follow noise rules, and clean up after themselves—especially outdoors. "
    "For local nightlife, give relaxed, nearby suggestions based on the address. "
    "For parking: specify vehicles allowed and where to park (driveways, etc.). "
    "For tech/amenity questions (WiFi, TV, grill, etc.), give quick, direct instructions. "
    "For complaints/issues, apologize first, then offer a fast solution or fix timeline. "
)

class HostawayUnifiedWebhook(BaseModel):
    object: str
    event: str
    accountId: int
    data: dict
    body: str = None
    listingName: str = None
    date: str = None

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

    guest_name = "Guest"
    guest_first_name = "Guest"
    check_in = "N/A"
    check_out = "N/A"
    guest_count = "N/A"
    listing_name = "Unknown"
    reservation_status = payload.data.get("status", "Unknown").capitalize()

    # --- Fetch Hostaway reservation (for guest name, dates, etc) ---
    if reservation_id:
        res = fetch_hostaway_resource("reservations", reservation_id)
        result = res.get("result", {}) if res else {}
        logging.info(f"Reservation data: {json.dumps(result, indent=2)}")
        guest_name = result.get("guestName", guest_name)
        guest_first_name = result.get("guestFirstName", guest_first_name)
        check_in = result.get("arrivalDate", check_in)
        check_out = result.get("departureDate", check_out)
        guest_count = result.get("numberOfGuests", guest_count)
        if not listing_map_id:
            listing_map_id = result.get("listingId")

    # --- Fetch listing and build property info for AI prompt ---
    property_info = ""
    address = city = zipcode = summary = amenities_str = ""
    house_manual = ""
    if listing_map_id:
        listing = fetch_hostaway_resource("listings", listing_map_id)
        result = listing.get("result", {}) if listing else {}
        logging.info(f"Listing full data: {json.dumps(result, indent=2)}")
        listing_name = result.get("name", listing_name)
        address = result.get("address", "")
        city = result.get("city", "")
        zipcode = result.get("zip", "")
        summary = result.get("summary", "")
        house_manual = result.get("houseManual", "")
        amenities = result.get("amenities", "")
        if isinstance(amenities, list):
            amenities_str = ", ".join(amenities)
        elif isinstance(amenities, dict):
            amenities_str = ", ".join([k for k, v in amenities.items() if v])
        else:
            amenities_str = str(amenities)

        property_info = (
            f"Property Address: {address}, {city} {zipcode}\n"
            f"Summary: {summary}\n"
            f"House Manual: {house_manual}\n"
            f"Amenities: {amenities_str}\n"
        )

    # **NEW: Try to find a custom response**
    custom_response = get_similar_response(listing_map_id, guest_message)
    extra_custom = ""
    if custom_response:
        extra_custom = f"\n---\nPast answer for this listing to a similar guest question:\n{custom_response}"

    readable_communication = {
        "channel": "Channel Message",
        "email": "Email",
        "sms": "SMS",
        "whatsapp": "WhatsApp",
        "airbnb": "Airbnb",
        "vrbo": "VRBO",
        "bookingcom": "Booking.com",
    }.get(communication_type, communication_type.capitalize())

    prompt = (
        f"Guest first name: {guest_first_name}\n"
        f"A guest sent this message:\n{guest_message}\n\n"
        f"Property info:\n{property_info}\n"
        f"{extra_custom}\n"
        "If you do not know the answer to a question from the above info, DO NOT make up an answer. Instead, reply that you'll follow up with more info."
        "If the guest asks about the local area, you can use the address above for context and suggest nearby restaurants, shops, or attractions."
    )

    try:
        response = openai_client.chat.completions.create(
            model="gpt-4o",
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": prompt}
            ],
        )
        ai_reply = response.choices[0].message.content.strip()
    except Exception as e:
        logging.error(f"❌ OpenAI error: {e}")
        ai_reply = "(Error generating reply.)"

    header = (
        f"*New {readable_communication}* from *{guest_name}* at *{listing_name}*\n"
        f"Dates: *{check_in} → {check_out}*\n"
        f"Guests: *{guest_count}* | Status: *{reservation_status}*"
    )

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
                    "value": json.dumps({"reply": ai_reply, "conv_id": conversation_id, "type": communication_type}),
                    "action_id": "send"
                },
                {
                    "type": "button",
                    "text": {"type": "plain_text", "text": "✏️ Edit"},
                    "value": json.dumps({
                        "draft": ai_reply,
                        "conv_id": conversation_id,
                        "type": communication_type,
                    }),
                    "action_id": "edit"
                },
                {
                    "type": "button",
                    "text": {"type": "plain_text", "text": "📝 Write Your Own"},
                    "value": json.dumps({"conv_id": conversation_id, "type": communication_type}),
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
