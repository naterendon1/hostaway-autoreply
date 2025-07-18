# main.py
from fastapi import FastAPI
from slack_interactivity import router as slack_router
from pydantic import BaseModel
import os
import requests
import logging
import json
from openai import OpenAI

logging.basicConfig(level=logging.INFO)

HOSTAWAY_CLIENT_ID = os.getenv("HOSTAWAY_CLIENT_ID")
HOSTAWAY_CLIENT_SECRET = os.getenv("HOSTAWAY_CLIENT_SECRET")
HOSTAWAY_API_BASE = "https://api.hostaway.com/v1"
SLACK_CHANNEL = os.getenv("SLACK_CHANNEL")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")

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

    if reservation_id:
        res = fetch_hostaway_resource("reservations", reservation_id)
        result = res.get("result", {}) if res else {}
        guest_name = result.get("guestName", guest_name)
        check_in = result.get("startDate", check_in)
        check_out = result.get("endDate", check_out)
        guest_count = result.get("numberOfGuests", guest_count)
        if not listing_map_id:
            listing_map_id = result.get("listingId")

    if listing_map_id:
        listing = fetch_hostaway_resource("listings", listing_map_id)
        result = listing.get("result", {}) if listing else {}
        listing_name = result.get("name", listing_name)

    readable_communication = {
        "channel": "Channel Message",
        "email": "Email",
        "sms": "SMS",
        "whatsapp": "WhatsApp",
        "airbnb": "Airbnb",
    }.get(communication_type, communication_type.capitalize())

    prompt = f"""You are a professional short-term rental manager. A guest sent this message:
{guest_message}

Write a warm, professional reply. Be friendly and helpful. Use a tone that is informal, concise, and polite. Don't include a signoff."""

    try:
        response = openai_client.chat.completions.create(
            model="gpt-4",
            messages=[
                {"role": "system", "content": "You are a helpful, friendly vacation rental host."},
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

    # Slack Block Kit with 3 actions (buttons)
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
                    "text": {"type": "plain_text", "text": "✏️ Improve with AI"},
                    "value": json.dumps({"reply": ai_reply, "conv_id": conversation_id, "type": communication_type}),
                    "action_id": "improve"
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
    slack_client = WebClient(token=os.getenv("SLACK_BOT_TOKEN"))
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

def get_hostaway_access_token() -> str:
    url = f"{HOSTAWAY_API_BASE}/accessTokens"
    data = {
        "grant_type": "client_credentials",
        "client_id": HOSTAWAY_CLIENT_ID,
        "client_secret": HOSTAWAY_CLIENT_SECRET,
        "scope": "general"
    }
    try:
        r = requests.post(url, data=data, headers={"Content-Type": "application/x-www-form-urlencoded"})
        r.raise_for_status()
        return r.json().get("access_token")
    except Exception as e:
        logging.error(f"❌ Token error: {e}")
        return None

def fetch_hostaway_resource(resource: str, resource_id: int):
    token = get_hostaway_access_token()
    if not token:
        return None
    url = f"{HOSTAWAY_API_BASE}/{resource}/{resource_id}"
    try:
        r = requests.get(url, headers={"Authorization": f"Bearer {token}"})
        r.raise_for_status()
        return r.json()
    except Exception as e:
        logging.error(f"❌ Fetch {resource} error: {e}")
        return None

def send_reply_to_hostaway(conversation_id: str, reply_text: str, communication_type: str = "email") -> bool:
    token = get_hostaway_access_token()
    if not token:
        return False
    url = f"{HOSTAWAY_API_BASE}/conversations/{conversation_id}/messages"
    payload = {
        "body": reply_text,
        "isIncoming": 0,
        "communicationType": communication_type
    }
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json"
    }
    try:
        r = requests.post(url, headers=headers, json=payload)
        r.raise_for_status()
        logging.info(f"✅ Sent to Hostaway: {r.text}")
        return True
    except Exception as e:
        logging.error(f"❌ Send error: {e}")
        return False
