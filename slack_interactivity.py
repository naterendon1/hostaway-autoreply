from pathlib import Path

main_py_code = """
from fastapi import FastAPI, Request, Form
from fastapi.responses import JSONResponse
from pydantic import BaseModel
import json
import os
import requests
import logging
from dotenv import load_dotenv
from openai import OpenAI
from slack_sdk.webhook import WebhookClient

load_dotenv()

app = FastAPI()
logging.basicConfig(level=logging.INFO)

client = OpenAI(api_key=os.getenv("OPENAI_API_KEY"))
SLACK_WEBHOOK_URL = os.getenv("SLACK_WEBHOOK_URL")
HOSTAWAY_API_KEY = os.getenv("HOSTAWAY_API_KEY")
HOSTAWAY_API_BASE = "https://api.hostaway.com/v1"

class HostawayWebhook(BaseModel):
    id: int
    body: str
    listingName: str
    guestName: str = "Guest"
    checkIn: str = ""
    checkOut: str = ""

@app.post("/hostaway-webhook")
async def receive_message(payload: HostawayWebhook):
    guest_message = payload.body
    listing_name = payload.listingName or "Unknown Listing"
    guest_name = payload.guestName or "Guest"
    check_in = payload.checkIn or "?"
    check_out = payload.checkOut or "?"
    message_id = payload.id

    logging.info(f"📩 New guest message from {guest_name}: {guest_message}")

    # Ask for clarification if needed
    known_questions = ["early check-in", "fireworks", "pets", "check out", "wifi"]
    if any(q in guest_message.lower() for q in known_questions):
        ask_me_text = f"The guest asked: \"{guest_message}\"\n\nThis question needs host input. Please provide the correct information so I can craft a response."
        return JSONResponse({"text": ask_me_text})

    prompt = f\"\"\"You are a professional short-term rental manager. A guest named {guest_name} staying at '{listing_name}' from {check_in} to {check_out} sent this message:
\"{guest_message}\"

Write a warm, professional reply. Be friendly and helpful. Use a tone that is informal, concise, and polite. Don’t include a signoff.\"\"\"

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
        "text": f"*New Guest Message from {guest_name} for {listing_name} ({check_in} - {check_out}):*\\n>{guest_message}\\n\\n*Suggested Reply:*\\n>{ai_reply}",
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

    return {"status": "sent_to_slack"}

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
        return JSONResponse({"text": "🔙 Returning to original options."})

    return JSONResponse({"text": "⚠️ Unknown action"})

def send_reply_to_hostaway(message_id: int, reply_text: str):
    url = f"{HOSTAWAY_API_BASE}/messages/{message_id}/reply"
    headers = {
        "Authorization": f"Bearer {HOSTAWAY_API_KEY}",
        "Content-Type": "application/json"
    }
    payload = {"body": reply_text}
    r = requests.post(url, headers=headers, json=payload)
    r.raise_for_status()
"""

# Save to /mnt/data for user access
path = Path("/mnt/data/main.py")
path.write_text(main_py_code)
path
