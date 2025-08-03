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

class HostawayUnifiedWebhook(BaseModel):
    object: str
    event: str
    accountId: int
    data: dict
    body: str = None
    listingName: str = None
    date: str = None

### --------- AI SYSTEM PROMPT: HUMAN, HELPFUL, USE CONTEXT, LEARN ---------
SYSTEM_PROMPT_ANSWER = (
    "You are a smart, friendly human host for a vacation rental property. "
    "Respond as if you’re texting a guest you want to welcome back: clear, warm, and relaxed—but always accurate. "
    "Never sound robotic. Use natural, casual language. "
    "If the guest asks something about the house, always use any info available from amenities, house rules, calendar, or learning examples. "
    "If you’re not sure, say so, or ask for clarification, instead of guessing. "
    "Never restate the guest’s question. Don’t use emojis. "
    "Use the provided message thread (with roles labeled), reservation details, listing info, and any previous similar questions+answers to help construct the best possible reply. "
    "If a previous reply exists for a similar question, adapt and reuse as context—but ensure accuracy for THIS guest and THIS reservation. "
    "Keep answers under 240 characters unless more detail is needed."
)

def make_ai_reply(prompt, system_prompt=SYSTEM_PROMPT_ANSWER):
    try:
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
    conv_id = payload.data.get("conversationId")
    reservation_id = payload.data.get("reservationId")
    listing_id = payload.data.get("listingMapId")
    guest_id = payload.data.get("userId", "")
    communication_type = payload.data.get("communicationType", "channel")

    if not guest_msg:
        if payload.data.get("attachments"):
            logging.info("📷 Skipping image-only message.")
        else:
            logging.info("🧾 Empty message skipped.")
        return {"status": "ignored"}

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
        conversation_context.append(f"{sender}: {body}")
    context_str = "\n".join(conversation_context)

    # --- Surface learned answers if available ---
    prev_examples = get_similar_learning_examples(guest_msg, listing_id)
    prev_answer = ""
    if prev_examples and prev_examples[0][2]:
        prev_answer = (
            f"Reference answer to a similar question in the past:\n\"{prev_examples[0][2]}\"\n"
            f"Use it as context, but ensure accuracy for this guest.\n"
        )

    cancellation = get_cancellation_policy_summary({}, res)

    # -------------- CALENDAR INTENT & LIVE CHECK --------------
    calendar_keywords = [
        "available", "availability", "book", "open", "stay", "dates", "night", "reserve", "weekend",
        "extend", "extra night", "holiday", "christmas", "spring break", "july", "august", "september", "october",
        "november", "december", "january", "february", "march", "april", "may", "june"
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
                    f"Calendar checked for {start_date} to {end_date}. "
                    f"Available nights: {', '.join(available_days)}."
                )
                if unavailable_days:
                    calendar_summary += f" Unavailable: {', '.join(unavailable_days)}."
            else:
                calendar_summary = f"No available nights between {start_date} and {end_date}."
        else:
            calendar_summary = "Unable to retrieve calendar data for the requested dates."
    # ----------------------------------------------------------

    # --- Amenities summary for AI (optional: if you have get_listing_amenities in utils) ---
    try:
        from utils import get_listing_amenities
        amenities = get_listing_amenities(listing_id)
        amenities_str = f"Amenities: {', '.join(amenities)}" if amenities else ""
    except Exception:
        amenities_str = ""

    # -------------- AI PROMPT --------------
    ai_prompt = (
        f"Recent conversation (last 10 messages, newest last):\n"
        f"{context_str}\n\n"
        f"{prev_answer}"
        f"Reservation:\n{json.dumps(res)}\n"
        f"Cancellation: {cancellation}\n"
        f"Calendar: {calendar_summary}\n"
        f"{amenities_str}\n"
        "---\n"
        "Write the best possible reply for the latest guest message, using everything above as context. "
        "Sound human, friendly, clear, and helpful."
    )

    ai_reply = clean_ai_reply(make_ai_reply(ai_prompt))

    # --- Button/meta block only contains IDs! ---
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
