from fastapi import FastAPI, Request
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

app = FastAPI()

client = OpenAI(api_key=os.getenv("OPENAI_API_KEY"))
SLACK_WEBHOOK_URL = os.getenv("SLACK_WEBHOOK_URL")

class HostawayUnifiedWebhook(BaseModel):
    object: str
    event: str
    accountId: int
    data: dict
    body: Optional[str] = None
    listingName: Optional[str] = None
    date: Optional[str] = None

@app.get("/")
def read_root():
    return {"message": "Welcome to the Hostaway Auto Reply Service!"}

@app.post("/unified-webhook")
async def unified_webhook(payload: HostawayUnifiedWebhook):
    logging.info(f"Received payload: {json.dumps(payload.dict(), indent=2)}")

    if payload.event == "message.received" and payload.object == "conversationMessage":
        guest_message = payload.data.get("body", "")
        conversation_id = payload.data.get("conversationId")
        message_id = payload.data.get("id")
        communication_type = payload.data.get("communicationType") or "channel"
        reservation_id = payload.data.get("reservationId")
        listing_map_id = payload.data.get("listingMapId")

        guest_name = "Guest"
        check_in = "N/A"
        check_out = "N/A"
        guest_count = "N/A"
        listing_name = "Unknown"
        reservation_status = payload.data.get("status", "Unknown").capitalize()

        if reservation_id:
            res = fetch_reservation_details(reservation_id)
            if res:
                logging.info(f"📦 Reservation API response: {json.dumps(res, indent=2)}")
                res_data = res.get("result", res)
                guest_name = res_data.get("guestName", guest_name)
                check_in = res_data.get("startDate", check_in)
                check_out = res_data.get("endDate", check_out)
                guest_count = res_data.get("numberOfGuests", guest_count)
                if not listing_map_id:
                    listing_map_id = res_data.get("listingId")

        if listing_map_id:
            listing = fetch_listing_details(listing_map_id)
            if listing:
                logging.info(f"📦 Listing API response: {json.dumps(listing, indent=2)}")
                listing_data = listing.get("result", listing)
                listing_name = listing_data.get("name", listing_name)

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
            logging.error(f"❌ OpenAI error: {str(e)}")
            ai_reply = "(Error generating reply with OpenAI.)"

        header_text = f"*New {communication_type.capitalize()}* from *{guest_name}* at *{listing_name}*  \nDates: *{check_in} → {check_out}*  \nGuests: *{guest_count}* | Status: *{reservation_status}*"

        slack_message = {
            "text": header_text + f"\n\n>{guest_message}\n\n*Suggested Reply:*\n>{ai_reply}",
            "attachments": [
                {
                    "callback_id": str(conversation_id),
                    "fallback": "You are unable to choose a response",
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
            logging.info("✅ Slack message sent successfully.")
        except Exception as e:
            logging.error(f"❌ Failed to send Slack message: {str(e)}")

    return {"status": "ok"}

def get_hostaway_access_token() -> Optional[str]:
    url = f"{HOSTAWAY_API_BASE}/accessTokens"
    payload = {
        "grant_type": "client_credentials",
        "client_id": HOSTAWAY_CLIENT_ID,
        "client_secret": HOSTAWAY_CLIENT_SECRET,
        "scope": "general"
    }
    headers = {"Content-Type": "application/x-www-form-urlencoded"}

    try:
        response = requests.post(url, data=payload, headers=headers)
        response.raise_for_status()
        token_data = response.json()
        return token_data.get("access_token")
    except Exception as e:
        logging.error(f"❌ Failed to retrieve Hostaway access token: {e}")
        return None

def fetch_reservation_details(reservation_id: int) -> Optional[dict]:
    access_token = get_hostaway_access_token()
    if not access_token:
        return None

    url = f"{HOSTAWAY_API_BASE}/reservations/{reservation_id}"
    headers = {
        "Authorization": f"Bearer {access_token}",
        "Accept": "application/json"
    }

    try:
        response = requests.get(url, headers=headers)
        response.raise_for_status()
        return response.json()
    except Exception as e:
        logging.error(f"❌ Failed to fetch reservation details: {e}")
        return None

def fetch_listing_details(listing_id: int) -> Optional[dict]:
    access_token = get_hostaway_access_token()
    if not access_token:
        return None

    url = f"{HOSTAWAY_API_BASE}/listings/{listing_id}"
    headers = {
        "Authorization": f"Bearer {access_token}",
        "Accept": "application/json"
    }

    try:
        response = requests.get(url, headers=headers)
        response.raise_for_status()
        return response.json()
    except Exception as e:
        logging.error(f"❌ Failed to fetch listing details: {e}")
        return None
