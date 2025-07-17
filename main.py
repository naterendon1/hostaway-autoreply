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

if not HOSTAWAY_CLIENT_ID or not HOSTAWAY_CLIENT_SECRET:
    logging.error("❌ HOSTAWAY_CLIENT_ID or HOSTAWAY_CLIENT_SECRET not set.")

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
        listing_name = payload.data.get("listingName", "Unknown")
        conversation_id = payload.data.get("conversationId")
        message_id = payload.data.get("id")

        # Extract reservation info
        reservation_status = payload.data.get("status", "Unknown").capitalize()
        guest_name = payload.data.get("guestName", "Guest")
        check_in = payload.data.get("startDate", "N/A")
        check_out = payload.data.get("endDate", "N/A")
        guest_count = payload.data.get("numberOfGuests", "N/A")

        logging.info(f"📩 New guest message received: {guest_message}")

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

        header_text = f"*New Guest Message* from *{guest_name}* at *{listing_name}*  \nDates: *{check_in} → {check_out}*  \nGuests: *{guest_count}* | Status: *{reservation_status}*"

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
                            "value": ai_reply,
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

@app.post("/slack-interactivity")
async def slack_action(request: Request):
    form_data = await request.form()
    payload = json.loads(form_data["payload"])

    logging.info(f"Received Slack payload: {json.dumps(payload, indent=2)}")

    action = payload["actions"][0]
    action_type = action["name"]
    conversation_id = payload.get("callback_id")

    if action_type == "approve" and conversation_id:
        reply = action["value"]
        success = send_reply_to_hostaway(conversation_id, reply)

        if success:
            return JSONResponse({
                "text": f"✅ Reply sent to guest:\n\n>{reply}",
                "replace_original": True
            })
        else:
            return JSONResponse({
                "text": "❌ Failed to send reply to Hostaway. Please check:\n1. API key permissions\n2. Conversation still exists\n3. Correct endpoint URL",
                "replace_original": True
            })

    elif action_type == "write_own":
        return JSONResponse({
            "text": "📝 Please compose your message below.",
            "attachments": [
                {
                    "callback_id": str(conversation_id),
                    "fallback": "Compose your reply",
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

    elif action_type == "back":
        return JSONResponse({"text": "🔙 Returning to original options. (Feature coming soon)"})
    elif action_type == "improve":
        return JSONResponse({"text": "✏️ Improve with AI feature coming soon."})
    elif action_type == "send":
        return JSONResponse({"text": "📨 Send functionality coming soon."})

    return JSONResponse({"text": "⚠️ Unknown action"})

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

def send_reply_to_hostaway(conversation_id: str, reply_text: str) -> bool:
    access_token = get_hostaway_access_token()
    if not access_token:
        return False

    url = f"{HOSTAWAY_API_BASE}/conversations/{conversation_id}/messages"
    headers = {
        "Authorization": f"Bearer {access_token}",
        "Cache-Control": "no-cache",
        "Content-Type": "application/json",
        "Accept": "application/json"
    }
    payload = {
        "body": reply_text,
        "isIncoming": 0,
        "communicationType": "email"
    }

    logging.info(f"🕒 Attempting to send reply to Hostaway for conversation {conversation_id}")
    logging.debug(f"Full request URL: {url}")
    logging.debug(f"Request headers: {headers}")
    logging.debug(f"Request payload: {payload}")

    try:
        response = requests.post(url, headers=headers, json=payload)
        response.raise_for_status()
        logging.info(f"✅ Successfully sent reply. Response: {response.text}")
        return True

    except requests.exceptions.HTTPError as e:
        error_detail = {
            "status_code": e.response.status_code,
            "response_text": e.response.text,
            "request_url": url,
            "request_headers": headers,
            "request_payload": payload
        }
        logging.error(f"❌ HTTP error sending reply: {json.dumps(error_detail, indent=2)}")

        if e.response.status_code == 404:
            logging.error("🔍 404 Not Found - Possible issues:")
            logging.error(f"1. Invalid conversation ID: {conversation_id}")
            logging.error(f"2. Incorrect endpoint URL: {url}")
            logging.error("3. Missing required parameters in payload")
        elif e.response.status_code == 403:
            logging.error("🔒 403 Forbidden - Please verify:")
            logging.error("1. Token not valid or expired")
            logging.error("2. Client credentials incorrect")

        return False

    except Exception as e:
        logging.error(f"❌ Unexpected error sending reply: {str(e)}")
        return False
