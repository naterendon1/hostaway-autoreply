from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from typing import Optional
import os
import requests
import json
import logging
from dotenv import load_dotenv
from slack_sdk.webhook import WebhookClient
from openai import OpenAI

# Load environment variables
load_dotenv()

# Set up FastAPI app and logging
app = FastAPI()
logging.basicConfig(level=logging.INFO)

# Set up OpenAI and API keys
client = OpenAI(api_key=os.getenv("OPENAI_API_KEY"))
SLACK_WEBHOOK_URL = os.getenv("SLACK_WEBHOOK_URL")
HOSTAWAY_API_KEY = os.getenv("HOSTAWAY_API_KEY")
HOSTAWAY_API_BASE = "https://api.hostaway.com/v1"

# Define Pydantic model for payload with Optional fields
class HostawayUnifiedWebhook(BaseModel):
    event: str
    entityId: int
    entityType: str
    data: dict

    # Optional fields in case they are missing from the payload
    body: Optional[str] = None
    listingName: Optional[str] = None
    date: Optional[str] = None

@app.post("/unified-webhook")
async def unified_webhook(payload: HostawayUnifiedWebhook):
    # Log the entire payload as a string to understand its structure
    logging.info(f"Received payload: {json.dumps(payload.dict(), indent=2)}")  # Log the entire payload

    if payload.event == "message.received":
        guest_message = payload.data.get("body", "")
        message_id = payload.data.get("id", "")
        
        logging.info(f"📩 New guest message received: {guest_message}")
        
        # Generate reply using OpenAI
        prompt = f"""You are a professional short-term rental manager. A guest sent this message:
        {guest_message}

        Write a warm, professional reply."""
        
        try:
            response = client.chat.completions.create(
                model="gpt-4",
                messages=[{"role": "system", "content": "You are a friendly vacation rental host."},
                          {"role": "user", "content": prompt}]
            )
            ai_reply = response.choices[0].message.content.strip()
            logging.info(f"AI Reply: {ai_reply}")
        except Exception as e:
            logging.error(f"❌ OpenAI error: {str(e)}")
            ai_reply = "Error generating reply."
        
        slack_message = {
            "text": f"New message: {guest_message}\nSuggested Reply: {ai_reply}",
            "attachments": [
                {
                    "callback_id": str(message_id),
                    "fallback": "You are unable to choose a response",
                    "actions": [
                        {"name": "approve", "text": "✅ Approve", "type": "button", "value": ai_reply},
                        {"name": "write_own", "text": "📝 Write Your Own", "type": "button", "value": str(message_id)}
                    ]
                }
            ]
        }

        try:
            webhook = WebhookClient(SLACK_WEBHOOK_URL)
            response = webhook.send(**slack_message)
            logging.info(f"Slack response status: {response.status_code}")
        except Exception as e:
            logging.error(f"❌ Failed to send Slack message: {str(e)}")
    
    return {"status": "ok"}

@app.post("/slack-interactivity")
async def slack_action(request: Request):
    form_data = await request.form()
    payload = json.loads(form_data["payload"])
    action = payload["actions"][0]
    action_type = action["name"]
    message_id = int(payload["callback_id"])
    
    if action_type == "approve":
        reply = action["value"]
        logging.info(f"Reply approved: {reply}")
        send_reply_to_hostaway(message_id, reply)
        return JSONResponse({"text": "✅ Reply approved and sent."})

def send_reply_to_hostaway(message_id: int, reply_text: str):
    url = f"{HOSTAWAY_API_BASE}/messages/{message_id}/reply"
    headers = {
        "Authorization": f"Bearer {HOSTAWAY_API_KEY}",
        "Content-Type": "application/json"
    }
    payload = {"body": reply_text}
    
    logging.info(f"Sending reply to Hostaway: {payload}")
    
    try:
        r = requests.post(url, headers=headers, json=payload)
        r.raise_for_status()
        logging.info("✅ Reply sent successfully.")
    except requests.exceptions.HTTPError as e:
        logging.error(f"❌ Failed to send reply: {e.response.status_code} {e.response.text}")
