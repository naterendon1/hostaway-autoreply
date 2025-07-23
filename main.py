import os
import logging
import json
import re
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from openai import OpenAI

from slack_interactivity import router as slack_router
from utils import (
    fetch_hostaway_resource,
    fetch_hostaway_fields,
    get_similar_learning_examples
)

logging.basicConfig(level=logging.INFO)

OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
openai_client = OpenAI(api_key=OPENAI_API_KEY)

app = FastAPI()
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)
app.include_router(slack_router)

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

# ---- STEP 1: Determine needed fields via AI ---
def get_fields_needed(guest_message):
    probe_prompt = (
        "A guest sent this message:\n"
        f"{guest_message}\n"
        "What specific property or reservation fields do you need to answer this question? "
        "Respond ONLY with a comma-separated list of field names (e.g. wifiPassword, checkInTime, petsAllowed, amenities, houseManual, etc.). "
        "If no fields are needed, reply with 'none'."
    )
    try:
        resp = openai_client.chat.completions.create(
            model="gpt-4",
            messages=[{"role": "system", "content": "You are an assistant helping to determine what data is needed for short guest questions."},
                      {"role": "user", "content": probe_prompt}]
        )
        fields_line = resp.choices[0].message.content.strip()
        fields = [f.strip() for f in re.split("[,\\n]", fields_line) if f.strip() and f.strip().lower() != "none"]
        return fields
    except Exception as e:
        logging.error(f"❌ OpenAI fields-needed error: {e}")
        return []

def clean_ai_reply(reply: str, property_type="home"):
    bad_signoffs = [
        "Enjoy your meal", "Enjoy your meals", "Enjoy!", "Best,", "Best regards,", "Cheers,", "Sincerely,", "[Your Name]", "Best", "Sincerely"
    ]
    for signoff in bad_signoffs:
        reply = reply.replace(signoff, "")
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
    reservation_status = payload.data.get("status", "Unknown").capitalize()

    if reservation_id:
        res = fetch_hostaway_resource("reservations", reservation_id)
        result = res.get("result", {}) if res else {}
        guest_first_name = result.get("guestFirstName", guest_first_name)
        check_in = result.get("arrivalDate", check_in)
        check_out = result.get("departureDate", check_out)
        guest_count = result.get("numberOfGuests", guest_count)
        if not listing_map_id:
            listing_map_id = result.get("listingId")
        if not guest_id:
            guest_id = result.get("guestId", "")

    # --- 1. Find what fields are needed to answer this specific guest question ---
    fields_needed = get_fields_needed(guest_message)
    logging.info(f"Fields needed for answer: {fields_needed}")

    # --- 2. Fetch ONLY the needed fields ---
    property_info = {}
    reservation_info = {}
    if listing_map_id and fields_needed:
        prop_fields = [f for f in fields_needed if f in (
            "wifiPassword", "wifiUsername", "houseManual", "checkInTimeStart", "checkInTimeEnd",
            "checkOutTime", "petPolicy", "amenities", "propertyType", "parkingInfo", "summary", "name"
        )]
        if prop_fields:
            property_info = fetch_hostaway_fields("listings", listing_map_id, prop_fields) or {}

    if reservation_id and fields_needed:
        res_fields = [f for f in fields_needed if f in (
            "arrivalDate", "departureDate", "numberOfGuests", "status", "confirmationCode"
        )]
        if res_fields:
            reservation_info = fetch_hostaway_fields("reservations", reservation_id, res_fields) or {}

    # --- 3. Compose AI prompt and get final answer ---
    prop_type = property_info.get("propertyType") or "home"
    similar_examples = get_similar_learning_examples(guest_message, listing_map_id)
    prev_answer = ""
    if similar_examples:
        prev_answer = f"Previously, you (the host) replied to a similar guest question about this property: \"{similar_examples[0][2]}\". Use this as a guide if it fits.\n"

    prompt = (
        f"{prev_answer}"
        f"A guest sent this message:\n{guest_message}\n\n"
        f"Reservation info: {reservation_info}\n"
        f"Property info: {property_info}\n"
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
        ai_reply = clean_ai_reply(ai_reply, prop_type)
    except Exception as e:
        logging.error(f"❌ OpenAI error: {e}")
        ai_reply = "(Error generating reply.)"

    # You can now send the reply back via Slack, Hostaway, etc.
    logging.info(f"🤖 AI reply: {ai_reply}")

    return {"status": "ok", "reply": ai_reply}
