from fastapi import FastAPI
from slack_interactivity import router as slack_router
from pydantic import BaseModel
import os
import logging
import json
import requests
from openai import OpenAI
from utils import fetch_hostaway_resource, get_hostaway_access_token

logging.basicConfig(level=logging.INFO)

HOSTAWAY_API_BASE = "https://api.hostaway.com/v1"
SLACK_CHANNEL = os.getenv("SLACK_CHANNEL")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
SLACK_BOT_TOKEN = os.getenv("SLACK_BOT_TOKEN")

app = FastAPI()
app.include_router(slack_router)

openai_client = OpenAI(api_key=OPENAI_API_KEY)

system_prompt = (
    "You are a highly knowledgeable, super-friendly vacation rental host for homes in Crystal Beach, TX, Austin, TX, Galveston, TX, and Georgetown, TX. "
    "Use an informal tone. Start the message by greeting the guest in the same sentence as the rest of the text—don’t break it into a separate line. Don’t include any sign-off at the end. "
    "If you receive an immediate message from the guest after sending one, don't use the greeting. "
    "You know these Texas towns and their attractions inside and out. "
    "Your tone is casual, millennial-friendly, concise, and never stuffy or overly formal. Always keep replies brief, friendly, and approachable—never robotic. "
    "If a guest is only inquiring about dates or making a request, always check the calendar to confirm availability before agreeing. If the guest already has a confirmed booking, do not check the calendar or mention availability—just answer their questions as they are already booked. "
    "For early check-in or late check-out requests, check if available first, then mention a fee applies. "
    "If asked about amenities or house details, reply directly and with no extra fluff. "
    "For refund requests outside the cancellation policy, politely explain that refunds are only possible if the dates rebook. "
    "If a guest cancels for an emergency, show empathy and refer to Airbnb’s extenuating circumstances policy or the relevant platform's version. "
    "If a guest asks how to contact you, let them know they can reach out via the platform messenger, call, or text. "
    "If guests ask about what's included, summarize the main amenities (full kitchen, laundry, outdoor spaces, etc). "
    "If asked about bringing extra people or visitors, remind them only registered guests are allowed unless approved in advance. "
    "Maintain a helpful, problem-solving attitude and aim for fast, clear solutions. "
    "If guests ask about bringing pets, explain that the property is not pet-friendly and ESAs are not allowed. Service animals are always welcome, as required by law. "
    "Remind guests to respect neighbors, follow noise rules, and clean up after themselves—especially outdoors. "
    "For local nightlife questions, give chill, nearby suggestions based on the property's area (bars, breweries, live music, etc). "
    "If guests ask about parking, clarify how many vehicles are allowed and where to park (driveways, not blocking neighbors, etc). "
    "For tech/amenity questions (WiFi, TV, grill, etc.), give quick, direct instructions. "
    "If a guest complains or reports an issue, always start with an apology, then offer a fast solution or explain the fix timeline. "
)

class HostawayUnifiedWebhook(BaseModel):
    object: str
    event: str
    accountId: int
    data: dict
    body: str = None
    listingName: str = None
    date: str = None

def extract_dates_openai(message: str):
    """Use OpenAI to extract start/end date from message if possible."""
    prompt = (
        f"Extract the check-in and check-out dates from this text: \"{message}\". "
        "If there is only one date, use it as check-in and check-out. "
        "Reply with a JSON in this format: {{\"start\": \"YYYY-MM-DD\", \"end\": \"YYYY-MM-DD\"}}. "
        "If no dates are found, reply with: {{\"start\": null, \"end\": null}}."
    )
    try:
        response = openai_client.chat.completions.create(
            model="gpt-4o",
            messages=[
                {"role": "user", "content": prompt}
            ]
        )
        content = response.choices[0].message.content
        data = json.loads(content)
        return data.get("start"), data.get("end")
    except Exception as e:
        logging.error(f"Date extraction failed: {e}")
        return None, None

def check_calendar_availability(listing_id, start_date, end_date):
    """Query Hostaway calendar for availability for a date range (inclusive)."""
    # Hostaway API docs: /listings/{listingId}/calendar?startDate=YYYY-MM-DD&endDate=YYYY-MM-DD
    token = get_hostaway_access_token()
    if not token:
        return None
    url = f"{HOSTAWAY_API_BASE}/listings/{listing_id}/calendar?startDate={start_date}&endDate={end_date}"
    try:
        r = requests.get(url, headers={"Authorization": f"Bearer {token}"})
        r.raise_for_status()
        data = r.json()
        # Hostaway calendar API returns a "result": { "calendar": [ ... ] }
        calendar = data.get("result", {}).get("calendar", [])
        # Check if ALL requested dates are available
        all_available = all(day.get("available", True) for day in calendar)
        return all_available
    except Exception as e:
        logging.error(f"Calendar check failed: {e}")
        return None

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
    check_in = "N/A"
    check_out = "N/A"
    guest_count = "N/A"
    listing_name = "Unknown"
    reservation_status = payload.data.get("status", "Unknown").capitalize()
    availability_info = ""

    # --- Fetch Hostaway reservation (for guest name, dates, etc) ---
    if reservation_id:
        res = fetch_hostaway_resource("reservations", reservation_id)
        result = res.get("result", {}) if res else {}
        logging.info(f"Reservation data: {json.dumps(result, indent=2)}")
        guest_name = result.get("guestName", guest_name)
        check_in = result.get("arrivalDate", check_in)
        check_out = result.get("departureDate", check_out)
        guest_count = result.get("numberOfGuests", guest_count)
        if not listing_map_id:
            listing_map_id = result.get("listingId")

    # --- Fetch listing and build property info for AI prompt ---
    property_info = ""
    if listing_map_id:
        listing = fetch_hostaway_resource("listings", listing_map_id)
        result = listing.get("result", {}) if listing else {}
        listing_name = result.get("name", listing_name)
        address = result.get("address", "")
        city = result.get("city", "")
        zipcode = result.get("zip", "")
        summary = result.get("summary", "")
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
            f"Amenities: {amenities_str}\n"
        )

    # -- If this is NOT a confirmed booking, check for availability question and do live calendar lookup --
    is_confirmed = (reservation_status.lower() == "confirmed")
    # Heuristic: If message contains 'available', 'availability', 'open', etc, attempt date extraction
    avail_triggers = ["available", "availability", "open", "free", "vacant"]
    if (not is_confirmed and any(word in guest_message.lower() for word in avail_triggers)):
        start_date, end_date = extract_dates_openai(guest_message)
        if start_date and end_date and listing_map_id:
            is_available = check_calendar_availability(listing_map_id, start_date, end_date)
            if is_available is True:
                availability_info = (
                    f"Good news! The dates you asked about ({start_date} to {end_date}) are available for booking."
                )
                check_in = start_date
                check_out = end_date
            elif is_available is False:
                availability_info = (
                    f"Sorry, the dates you requested ({start_date} to {end_date}) are already booked."
                )
                check_in = start_date
                check_out = end_date
            else:
                availability_info = (
                    "Sorry, I couldn't confirm availability due to a technical issue. Please check the calendar or try again."
                )

    readable_communication = {
        "channel": "Channel Message",
        "email": "Email",
        "sms": "SMS",
        "whatsapp": "WhatsApp",
        "airbnb": "Airbnb",
    }.get(communication_type, communication_type.capitalize())

    # Compose AI prompt
    prompt = (
        f"A guest sent this message:\n{guest_message}\n\n"
        f"Property info:\n{property_info}\n"
        f"{'AVAILABILITY CHECK: ' + availability_info if availability_info else ''}\n"
        "Respond according to your latest rules and tone, and use property info to make answers detailed and specific if appropriate."
    )

    try:
        response = openai_client.chat.completions.create(
            model="gpt-4o",
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": prompt}
            ]
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
