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
import yaml

# Load feature toggles
os.makedirs("config", exist_ok=True)
if not os.path.exists("config/feature_toggles.yaml"):
    with open("config/feature_toggles.yaml", "w") as f:
        f.write("")

load_dotenv()
app = FastAPI()
logging.basicConfig(level=logging.INFO)

client = OpenAI(api_key=os.getenv("OPENAI_API_KEY"))
SLACK_WEBHOOK_URL = os.getenv("SLACK_WEBHOOK_URL")
HOSTAWAY_API_KEY = os.getenv("HOSTAWAY_API_KEY")
HOSTAWAY_API_BASE = "https://api.hostaway.com/v1"

message_memory = {}

class HostawayWebhook(BaseModel):
    id: int
    body: str
    listingName: str

@app.post("/hostaway-webhook")
async def receive_message(payload: HostawayWebhook):
    guest_message = payload.body
    listing_name = payload.listingName or "Guest"
    message_id = payload.id

    logging.info(f"📩 New guest message received: {guest_message}")

    prompt = f\"""You are a professional short-term rental manager. A guest staying at '{listing_name}' sent this message:
\"{guest_message}\"

Write a warm, professional reply. Be friendly and helpful. Use a tone that is informal, concise, and polite. Don’t include a signoff.\"""

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

    message_memory[message_id] = {
        "guest_message": guest_message,
        "listing_name": listing_name,
        "ai_reply": ai_reply
    }

    slack_message = {
        "text": f"*New Guest Message for {listing_name}:*\n>{guest_message}\n\n*Suggested Reply:*\n>{ai_reply}",
        "attachments": [
            {
                "callback_id": str(message_id),
                "fallback": "You are unable to choose a response",
                "color": "#3AA3E3",
                "attachment_type": "default",
                "actions": [
                    {"name": "approve", "text": "✅ Approve", "type": "button", "value": ai_reply},
                    {"name": "reject", "text": "❌ Reject", "type": "button", "value": "reject"},
                    {"name": "improve", "text": "✏️ Improve with AI", "type": "button", "value": ai_reply},
                    {"name": "write_own", "text": "📝 Write Your Own", "type": "button", "value": str(message_id)}
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

    mem = message_memory.get(message_id, {})
    guest_message = mem.get("guest_message", "")
    listing_name = mem.get("listing_name", "Guest")
    ai_reply = mem.get("ai_reply", "(No stored reply)")

    if action_type == "approve":
        send_reply_to_hostaway(message_id, ai_reply)
        return JSONResponse({"text": "✅ Reply approved and sent."})

    elif action_type == "reject":
        return JSONResponse({"text": "❌ Reply rejected."})

    elif action_type == "improve":
        return JSONResponse({"text": "🔄 Improving message with AI... (feature coming soon)"})

    elif action_type == "write_own":
        return JSONResponse({
            "text": "📝 You can now write your own reply below.",
            "attachments": [
                {
                    "text": "",
                    "callback_id": str(message_id),
                    "actions": [
                        {"name": "back", "text": "🔙 Back", "type": "button", "value": "back"}
                    ]
                }
            ]
        })

    elif action_type == "back":
        return JSONResponse({
            "text": f"*New Guest Message for {listing_name}:*\n>{guest_message}\n\n*Suggested Reply:*\n>{ai_reply}",
            "attachments": [
                {
                    "callback_id": str(message_id),
                    "fallback": "You are unable to choose a response",
                    "color": "#3AA3E3",
                    "attachment_type": "default",
                    "actions": [
                        {"name": "approve", "text": "✅ Approve", "type": "button", "value": ai_reply},
                        {"name": "reject", "text": "❌ Reject", "type": "button", "value": "reject"},
                        {"name": "improve", "text": "✏️ Improve with AI", "type": "button", "value": ai_reply},
                        {"name": "write_own", "text": "📝 Write Your Own", "type": "button", "value": str(message_id)}
                    ]
                }
            ]
        })

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

# Save the file
output_path = Path("/mnt/data/main.py")
output_path.write_text(main_py_code)
output_path
