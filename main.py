from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel
import os
import requests
import json
import logging
from dotenv import load_dotenv
from slack_sdk.webhook import WebhookClient
from openai import OpenAI

load_dotenv()

app = FastAPI()
logging.basicConfig(level=logging.INFO)

client = OpenAI(api_key=os.getenv("OPENAI_API_KEY"))
SLACK_WEBHOOK_URL = os.getenv("SLACK_WEBHOOK_URL")
HOSTAWAY_API_KEY = os.getenv("HOSTAWAY_API_KEY")
HOSTAWAY_API_BASE = "https://api.hostaway.com/v1"

class HostawayUnifiedWebhook(BaseModel):
    event: str
    entityId: int
    entityType: str
    data: dict

@app.post("/unified-webhook")
async def unified_webhook(payload: HostawayUnifiedWebhook):
    logging.info(f"Received payload: {payload}")
    if payload.event == "guestMessage" and payload.entityType == "message":
        guest_message = payload.data.get("body", "")
        listing_name = payload.data.get("listingName", "Guest")
        message_id = payload.entityId

        logging.info(f"📩 New guest message received: {guest_message}")

        prompt = f"""You are a professional short-term rental manager. A guest staying at '{listing_name}' sent this message:
{guest_message}

Write a warm, professional reply. Be friendly and helpful. Use a tone that is informal, concise, and polite. Don’t include a signoff."""

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

        slack_message = {
            "text": f"*New Guest Message for {listing_name}:*\n>{guest_message}\n\n*Suggested Reply:*\n>{ai_reply}",
            "attachments": [
                {
                    "callback_id": str(message_id),
                    "fallback": "You are unable to choose a response",
                    "color": "#3AA3E3",
                    "attachment_type": "default",
                    "actions": [
                        {
                            "name": "approve",
                            "text": "✅ Approve",
                            "type": "button",
                            "value": ai_reply
                        },
                        {
                            "name": "write_own",
                            "text": "📝 Write Your Own",
                            "type": "button",
                            "value": str(message_id)
                        }
                    ]
                }
            ]
        }

        webhook = WebhookClient(SLACK_WEBHOOK_URL)
        webhook.send(**slack_message)

    return {"status": "ok"}  # This line should be inside the function

@app.post("/slack-interactivity")
async def slack_action(request: Request):
    form_data = await request.form()
    payload = json.loads(form_data["payload"])
    action = payload["actions"][0]
    action_type = action["name"]
    message_id = int(payload["callback_id"])

    if action_type == "approve":
        reply = action["value"]
        send_reply_to_hostaway(message_id, reply)
        return JSONResponse({"text": "✅ Reply approved and sent."})

    elif action_type == "write_own":
        return JSONResponse({
            "text": "📝 Please compose your message below.",
            "attachments": [
                {
                    "callback_id": str(message_id),
                    "fallback": "Compose your reply",
                    "color": "#3AA3E3",
                    "attachment_type": "default",
                    "actions": [
                        {
                            "name": "back",
                            "text": "🔙 Back",
                            "type": "button",
                            "value": "back"
                        },
                        {
                            "name": "improve",
                            "text": "✏️ Improve with AI",
                            "type": "button",
                            "value": "improve"
                        },
                        {
                            "name": "send",
                            "text": "📨 Send",
                            "type": "button",
                            "value": "send"
                        }
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

def send_reply_to_hostaway(message_id: int, reply_text: str):
    url = f"{HOSTAWAY_API_BASE}/messages/{message_id}/reply"
    headers = {
        "Authorization": f"Bearer {HOSTAWAY_API_KEY}",
        "Content-Type": "application/json"
    }
    payload = {"body": reply_text}

    logging.info(f"🕒 Sending reply to Hostaway for message ID {message_id}")
    try:
        r = requests.post(url, headers=headers, json=payload)
        r.raise_for_status()
        logging.info("✅ Reply sent successfully.")
    except requests.exceptions.HTTPError as e:
        logging.error(f"❌ Failed to send reply: {e.response.status_code} {e.response.text}")
