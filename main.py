import os
import logging
import json
from fastapi import FastAPI
from slack_interactivity import router as slack_router
from pydantic import BaseModel
from openai import OpenAI
from utils import (
    fetch_hostaway_listing,
    fetch_hostaway_reservation,
    fetch_hostaway_conversation,
    get_cancellation_policy_summary,
    get_similar_learning_examples,
    clean_ai_reply,
    extract_date_range_from_message,
    fetch_hostaway_calendar,
    is_date_available,
    next_available_dates,
    detect_intent,
    get_property_location,
    search_google_places,
    detect_place_type,
)

logging.basicConfig(level=logging.INFO)

REQUIRED_ENV_VARS = [
    "HOSTAWAY_CLIENT_ID",
    "HOSTAWAY_CLIENT_SECRET",
    "OPENAI_API_KEY",
    "SLACK_CHANNEL",
    "SLACK_BOT_TOKEN"
]
missing = [v for v in REQUIRED_ENV_VARS if not os.getenv(v)]
if missing:
    raise RuntimeError(f"Missing required environment variables: {missing}")

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

# For Render/CLI runs
if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host="0.0.0.0", port=int(os.getenv("PORT", 8000)))

class HostawayUnifiedWebhook(BaseModel):
    object: str
    event: str
    accountId: int
    data: dict
    body: str = None
    listingName: str = None
    date: str = None

SYSTEM_PROMPT_ANSWER = (
    "You are a helpful, human, and context-aware vacation rental host. "
    "Reply to the guest in a friendly, concise text message, as if you were texting from your phone. "
    "Do NOT repeat what the guest just said or already confirmed—only reply with new, helpful info if needed. "
    "If the guest already gave the answer, simply acknowledge or skip a reply unless clarification is needed. "
    "Do NOT add greetings or sign-offs. "
    "Always use the prior conversation (thread), reservation info, and calendar. "
    "Don’t invent facts. "
    "If the guest confirms something, you can just say 'Great, thanks for confirming!' or say nothing if no reply is needed. "
    "Replies are sent to the guest as-is. No emojis."
)

def make_ai_reply(prompt, system_prompt=SYSTEM_PROMPT_ANSWER):
    try:
        logging.info(f"Prompt length: {len(prompt)} characters")
        response = openai_client.chat.completions.create(
            model="gpt-4",
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": prompt}
            ],
            timeout=20
        )
        return response.choices[0].message.content.strip()
    except Exception as e:
        logging.error(f"❌ OpenAI error: {e}")
        return "(Error generating reply.)"

@app.post("/unified-webhook")
async def unified_webhook(payload: HostawayUnifiedWebhook):
    logging.info(f"📬 Webhook received: {json.dumps(payload.dict(), indent=2)}")
    if payload.event != "message.received" or payload.object != "conversationMessage":
        return {"status": "ignored"}

    guest_msg = payload.data.get("body", "")
    if not guest_msg:
        if payload.data.get("attachments"):
            logging.info("📷 Skipping image-only message.")
        else:
            logging.info("🧾 Empty message skipped.")
        return {"status": "ignored"}

    # Intent detection (classification)
    detected_intent = detect_intent(guest_msg)
    logging.info(f"[INTENT] Detected message intent: {detected_intent}")

    conv_id = payload.data.get("conversationId")
    reservation_id = payload.data.get("reservationId")
    listing_id = payload.data.get("listingMapId")
    guest_id = payload.data.get("userId", "")
    communication_type = payload.data.get("communicationType", "channel")

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

    # --- Fetch message thread for full context ---
    convo_obj = fetch_hostaway_conversation(conv_id) or {}
    msgs = []
    if "result" in convo_obj and "conversationMessages" in convo_obj["result"]:
        msgs = convo_obj["result"]["conversationMessages"]

    # Only last N messages, and total char cap (for token safety)
    conversation_context = []
    for m in msgs[-MAX_THREAD_MESSAGES:]:
        sender = "Guest" if m.get("isIncoming") else "Host"
        body = m.get("body", "")
        if not body:
            continue
        conversation_context.append(f"{sender}: {body}")
    context_str = "\n".join(conversation_context)

    # Limit conversation context chars
    max_context_chars = 1200
    if len(context_str) > max_context_chars:
        context_str = context_str[-max_context_chars:]

    prev_examples = get_similar_learning_examples(guest_msg, listing_id)
    prev_answer = ""
    if prev_examples and prev_examples[0][2]:
        prev_answer = f"Previously, you replied:\n\"{prev_examples[0][2]}\"\nUse this only as context.\n"

    cancellation = get_cancellation_policy_summary({}, res)

    # --- CALENDAR INTENT & LIVE CHECK ---
    calendar_keywords = [
        "available", "availability", "book", "open", "stay", "dates", "night", "reserve", "weekend",
        "extend", "extra night", "holiday", "christmas", "spring break", "july", "august", "september", "october",
        "november", "december", "january", "february", "march", "april", "may", "june", "thanksgiving"
    ]
    should_check_calendar = any(word in guest_msg.lower() for word in calendar_keywords)
    calendar_summary = ""

    if should_check_calendar:
        start_date, end_date = extract_date_range_from_message(guest_msg, res)
        calendar_days = fetch_hostaway_calendar(listing_id, start_date, end_date)
        if calendar_days:
            available_days = [d["date"] for d in calendar_days if ("isAvailable" in d and d["isAvailable"]) or (d.get("status") == "available")]
            unavailable_days = [d["date"] for d in calendar_days if not (("isAvailable" in d and d["isAvailable"]) or (d.get("status") == "available"))]
            if available_days:
                calendar_summary = (
                    f"For {start_date} to {end_date}: Available nights: {', '.join(available_days)}."
                )
                if unavailable_days:
                    calendar_summary += f" Unavailable: {', '.join(unavailable_days)}."
            else:
                calendar_summary = f"No available nights between {start_date} and {end_date}."
        else:
            calendar_summary = "Calendar data not available for these dates."
    else:
        calendar_summary = "No calendar check for this inquiry."

    # --- GOOGLE PLACES DYNAMIC RECS ---
    place_type, keyword = detect_place_type(guest_msg)
    local_recs = ""
    if place_type:
        lat, lng = get_property_location(fetch_hostaway_listing(listing_id), reservation)
        if lat and lng:
            places = search_google_places(keyword, lat, lng, type_hint=place_type)
            if places:
                local_recs = (
                    f"Nearby {keyword}s from Google Maps for this property:\n"
                    + "\n".join([f"- {p['name']} ({p.get('rating','N/A')}) – {p['address']}" for p in places[:3]])
                )
            else:
                logging.info(f"[PLACES] No results for '{keyword}' at {lat},{lng}")
                local_recs = f"No {keyword}s found nearby from Google Maps."
        else:
            local_recs = "Sorry, couldn't determine the property location for recommendations."
            logging.warning("[PLACES] No lat/lng for property.")

    # --- Reservation/listing context trimming ---
    important_res_fields = [
        'arrivalDate', 'departureDate', 'numberOfGuests', 'guestFirstName',
        'guestLastName', 'status', 'totalPrice', 'cancellationPolicy', 'listingId'
    ]
    res_trimmed = {k: res[k] for k in important_res_fields if k in res}

    # Listing fields, but don't blow up tokens
    listing_trimmed = {}
    listing_obj = fetch_hostaway_listing(listing_id)
    if listing_obj and 'result' in listing_obj and isinstance(listing_obj['result'], dict):
        lres = listing_obj['result']
        listing_trimmed = {
            "name": lres.get("name"),
            "address": lres.get("address"),
            "propertyType": lres.get("propertyType"),
            "bedrooms": lres.get("bedrooms"),
            "bathrooms": lres.get("bathrooms"),
            "maxGuests": lres.get("maxGuests"),
            "amenities": lres.get("amenities")[:5] if lres.get("amenities") else [],
        }

    # Limit recs and context total length
    if local_recs and len(local_recs) > 800:
        local_recs = local_recs[:800] + "..."

    # --- AI PROMPT CONSTRUCTION (PATCHED/TRIMMED) ---
    ai_prompt = (
        f"Here is the conversation thread so far (newest last):\n"
        f"{context_str}\n"
        f"Reservation Info (important fields only):\n{json.dumps(res_trimmed)}\n"
        f"Listing Info (important fields only):\n{json.dumps(listing_trimmed)}\n"
        f"Calendar Info: {calendar_summary}\n"
        f"{local_recs}\n"
        f"Intent: {detected_intent}\n"
        f"{prev_answer}\n"
        "---\n"
        "Write a brief, human reply to the most recent guest message above, using the full context. "
        "Do NOT repeat what the guest just said or already confirmed. "
        "Never add a greeting or a sign-off. Only answer the specific question, if possible."
    )

    ai_reply = clean_ai_reply(make_ai_reply(ai_prompt))

    # --- SLACK BLOCK CONSTRUCTION ---
    button_meta_minimal = {
        "conv_id": conv_id,
        "listing_id": listing_id,
        "guest_id": guest_id,
        "type": communication_type,
        "guest_name": guest_name,
        "guest_message": guest_msg,
        "ai_suggestion": ai_reply,
        "check_in": check_in,
        "check_out": check_out,
        "guest_count": guest_count,
        "status": status,
        "detected_intent": detected_intent,
        "channel": SLACK_CHANNEL
    }

    blocks = [
        {"type": "section", "text": {"type": "mrkdwn", "text": f"*New {communication_type.capitalize()}* from *{guest_name}*\nDates: *{check_in} → {check_out}*\nGuests: *{guest_count}* | Status: *{status}*"}},
        {"type": "section", "text": {"type": "mrkdwn", "text": f"> {guest_msg}"}},
        {"type": "section", "text": {"type": "mrkdwn", "text": f"*Suggested Reply:*\n>{ai_reply}"}},
        {
            "type": "context",
            "elements": [
                {"type": "mrkdwn", "text": f"*Intent:* {detected_intent}"}
            ]
        },
        {
            "type": "actions",
            "elements": [
                {"type": "button", "text": {"type": "plain_text", "text": "✅ Send"}, "value": json.dumps({**button_meta_minimal, "action": "send"}), "action_id": "send"},
                {"type": "button", "text": {"type": "plain_text", "text": "✏️ Edit"}, "value": json.dumps({**button_meta_minimal, "action": "edit"}), "action_id": "edit"},
                {"type": "button", "text": {"type": "plain_text", "text": "📝 Write Your Own"}, "value": json.dumps({**button_meta_minimal, "action": "write_own"}), "action_id": "write_own"}
            ]
        }
    ]

    from slack_sdk import WebClient
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
