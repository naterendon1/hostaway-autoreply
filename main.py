from fastapi import FastAPI, Request, Form
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from typing import Optional
import os
import requests
import json
import logging
from slack_sdk.webhook import WebhookClient
from openai import OpenAI

logging.basicConfig(level=logging.INFO)

HOSTAWAY_CLIENT_ID = os.getenv("HOSTAWAY_CLIENT_ID")
HOSTAWAY_CLIENT_SECRET = os.getenv("HOSTAWAY_CLIENT_SECRET")
HOSTAWAY_API_BASE = "https://api.hostaway.com/v1"
SLACK_WEBHOOK_URL = os.getenv("SLACK_WEBHOOK_URL")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")

if not HOSTAWAY_CLIENT_ID or not HOSTAWAY_CLIENT_SECRET:
    logging.error("❌ Missing Hostaway credentials")

app = FastAPI()
client = OpenAI(api_key=OPENAI_API_KEY)

class HostawayUnifiedWebhook(BaseModel):
    object: str
    event: str
    accountId: int
    data: dict
    body: Optional[str] = None
    listingName: Optional[str] = None
    date: Optional[str] = None

@app.get("/ping")
def ping():
    return {"status": "ok"}

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
        logging.info(f"📦 Reservation: {json.dumps(res, indent=2)}")
        result = res.get("result", {}) if res else {}
        guest_name = result.get("guestName", guest_name)
        check_in = result.get("startDate", check_in)
        check_out = result.get("endDate", check_out)
        guest_count = result.get("numberOfGuests", guest_count)
        if not listing_map_id:
            listing_map_id = result.get("listingId")

    if listing_map_id:
        listing = fetch_hostaway_resource("listings", listing_map_id)
        logging.info(f"📦 Listing: {json.dumps(listing, indent=2)}")
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
        response = client.chat.completions.create(
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

    header = f"*New {readable_communication}* from *{guest_name}* at *{listing_name}*\nDates: *{check_in} → {check_out}*\nGuests: *{guest_count}* | Status: *{reservation_status}*"

    slack_message = {
        "text": header + f"\n\n>{guest_message}\n\n*Suggested Reply:*\n>{ai_reply}",
        "attachments": [
            {
                "callback_id": str(conversation_id),
                "fallback": "Choose a response",
                "color": "#3AA3E3",
                "attachment_type": "default",
                "actions": [
                    {
                        "name": "approve",
                        "text": "✅ Approve",
                        "type": "button",
                        "value": json.dumps({"reply": ai_reply, "type": communication_type}),
                        "style": "primary"
                    },
                    {
                        "name": "write_own",
                        "text": "📝 Write Your Own",
                        "type": "button",
                        "value": str(conversation_id)
                    }
                ]
            }
        ]
    }

    try:
        webhook = WebhookClient(SLACK_WEBHOOK_URL)
        webhook.send(**slack_message)
        logging.info("✅ Slack message sent.")
    except Exception as e:
        logging.error(f"❌ Slack send error: {e}")

    return {"status": "ok"}

@app.post("/slack-interactivity")
async def slack_action(request: Request):
    logging.info("📩 Slack interactivity received")
    form_data = await request.form()
    payload = json.loads(form_data["payload"])
    logging.info(f"📦 Slack Payload: {json.dumps(payload, indent=2)}")

    action = payload["actions"][0]
    action_type = action["name"]
    conversation_id = payload.get("callback_id")

    if action_type == "approve" and conversation_id:
        value_data = json.loads(action["value"])
        reply = value_data.get("reply")
        communication_type = value_data.get("type", "channel")
        success = send_reply_to_hostaway(conversation_id, reply, communication_type)
        return JSONResponse({
            "text": f"✅ Sent to guest:\n>{reply}" if success else "❌ Failed to send reply.",
            "replace_original": True
        })

    elif action_type == "write_own":
        return JSONResponse({
            "text": "📝 Please compose your message below.",
            "attachments": [
                {
                    "callback_id": str(conversation_id),
                    "fallback": "Write custom message",
                    "color": "#3AA3E3",
                    "attachment_type": "default",
                    "actions": [
                        {"name": "back", "text": "🔙 Back", "type": "button", "value": "back"},
                        {"name": "improve", "text": "✏️ Improve with AI", "type": "button", "value": "improve"},
                        {"name": "send", "text": "📨 Send", "type": "button", "value": "send", "style": "primary"}
                    ]
                }
            ]
        })

    return JSONResponse({"text": "⚠️ Unknown Slack action."})

def get_hostaway_access_token() -> Optional[str]:
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

def fetch_hostaway_resource(resource: str, resource_id: int) -> Optional[dict]:
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
