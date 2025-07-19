from fastapi import FastAPI
from slack_interactivity import router as slack_router
from pydantic import BaseModel
import os
import logging
import json
from openai import OpenAI
from utils import fetch_hostaway_resource

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

# --- SYSTEM PROMPT, as before ---
system_prompt = (
    "You are a highly knowledgeable, super-friendly vacation rental host for homes in Crystal Beach, TX, Austin, TX, Galveston, TX, and Georgetown, TX. "
    "You know these Texas towns and their attractions inside and out. "
    "Your tone is casual, millennial-friendly, concise, and never stuffy or overly formal. Always keep replies brief, friendly, and approachable—never robotic. "
    "Only double-check calendar availability before confirming requests. If a message comes from a confirmed booking, no need to check dates, they're already booked. "
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

    # --- Fetch Hostaway reservation (for guest name, dates, etc) ---
    if reservation_id:
        res = fetch_hostaway_resource("reservations", reservation_id)
        result = res.get("result", {}) if res else {}
        guest_name = result.get("guestName", guest_name)
        check_in = result.get("startDate", check_in)
        check_out = result.get("endDate", check_out)
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
        # amenities may be a list, dict, or string. Adjust as needed.
        amenities = result.get("amenities", "")
        # Format amenities for AI (list or string)
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

    readable_communication = {
        "channel": "Channel Message",
        "email": "Email",
        "sms": "SMS",
        "whatsapp": "WhatsApp",
        "airbnb": "Airbnb",
    }.get(communication_type, communication_type.capitalize())

    # --- Compose AI prompt with property info ---
    prompt = (
        f"A guest sent this message:\n{guest_message}\n\n"
        f"Property info:\n{property_info}\n"
        "Respond according to your latest rules and tone, and use property info to make answers detailed and specific if appropriate."
    )

    try:
        response = openai_client.chat.completions.create(
            model="gpt-4",
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
