import os
import logging
import json
from fastapi import FastAPI
from slack_interactivity import router as slack_router
from pydantic import BaseModel
from openai import OpenAI
from utils import (
    build_full_prompt,
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

class HostawayUnifiedWebhook(BaseModel):
    object: str
    event: str
    accountId: int
    data: dict
    body: str = None
    listingName: str = None
    date: str = None

SYSTEM_PROMPT = (
    "You are a real human vacation rental host, not a bot. "
    "Reply to guest messages as if you're texting a friend: short, direct, warm, clear, and like a millennial. "
    "Avoid all formal or corporate phrases—never say things like 'Thank you for your message', 'Let me know if you have any other questions', 'Best regards', or 'I hope this helps'. "
    "Never repeat what the guest said or already confirmed. "
    "No greetings, no sign-offs. "
    "Don't use emojis or exclamation marks unless totally natural. "
    "If the guest confirms something, just say 'Great, thanks for confirming!' or nothing. "
    "Only add details if they're genuinely useful. "
    "Keep replies short, natural, and modern—no long explanations or lists. "
    "BAD:\n'Thank you for your message. Your check-in is at 3pm. Let me know if you have any other questions.'\n"
    "GOOD:\n'Check-in's at 3pm. Let me know if you need anything else.'\n"
    "BEST:\n'Yep! Check-in's at 3pm. If you want to drop bags early, just let me know.'"
)

def make_ai_reply(prompt, system_prompt=SYSTEM_PROMPT):
    try:
        response = openai_client.chat.completions.create(
            model="gpt-4",
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": prompt}
            ],
            timeout=20,
            temperature=0.7
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

    # --- Intent detection
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

    # --- Fetch message thread for context ---
    MAX_THREAD_MESSAGES = 8
    TRIMMED_MSG_LEN = 300
    def trim_msg(m):
        return m[:TRIMMED_MSG_LEN] + ("…" if len(m) > TRIMMED_MSG_LEN else "")

    convo_obj = fetch_hostaway_conversation(conv_id) or {}
    msgs = []
    if "result" in convo_obj and "conversationMessages" in convo_obj["result"]:
        msgs = convo_obj["result"]["conversationMessages"]
    conversation_context = []
    for m in msgs[-MAX_THREAD_MESSAGES:]:
        sender = "Guest" if m.get("isIncoming") else "Host"
        body = m.get("body", "")
        if not body:
            continue
        conversation_context.append(f"{sender}: {trim_msg(body)}")

    prev_examples = get_similar_learning_examples(guest_msg, listing_id)
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

    # --- Fetch listing info for AI context ---
    listing = fetch_hostaway_listing(listing_id) or {}

    # --- GOOGLE PLACES DYNAMIC RECS ---
    place_type, keyword = detect_place_type(guest_msg)
    local_recs = ""
    if place_type:
        lat, lng = get_property_location(listing, reservation)
        if lat and lng:
            places = search_google_places(keyword, lat, lng, type_hint=place_type)
            if places:
                local_recs = (
                    f"Nearby {keyword}s from Google Maps for this property:\n"
                    + "\n".join([f"- {p['name']} ({p.get('rating','N/A')}) – {p['address']}" for p in places])
                )
            else:
                logging.info(f"[PLACES] No results for '{keyword}' at {lat},{lng}")
                local_recs = f"No {keyword}s found nearby from Google Maps."
        else:
            local_recs = "Sorry, couldn't determine the property location for recommendations."
            logging.warning("[PLACES] No lat/lng for property.")

    # --- AI PROMPT CONSTRUCTION ---
    ai_prompt = build_full_prompt(
        guest_message=guest_msg,
        thread_msgs=conversation_context,
        reservation=res,
        listing=listing,
        calendar_summary=calendar_summary,
        intent=detected_intent,
        similar_examples=prev_examples,
        extra_instructions=local_recs if local_recs else None
    )

    # --- AI Completion ---
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
    }

    blocks = [
        {"type": "section", "text": {"type": "mrkdwn", "text": f"*New {communication_type.capitalize()}* from *{guest_name}*\nDates: *{check_in} → {check_out}*\nGuests: *{guest_count}* | Status: *{status}*"}},
        {"type": "section", "text": {"type": "mrkdwn", "text": f"> {guest_msg}"}},
        {"type": "section", "text": {"type": "mrkdwn", "text": f"*Suggested Reply:*\n>{ai_reply}"}},
        {
            "type": "context",
            "elements": [
                {"type": "mrkdwn", "text": f"*Intent:* `{detected_intent}`"}
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
