from fastapi import APIRouter, Request, Form
from fastapi.responses import JSONResponse
import json
import logging
import requests
import os

router = APIRouter()

# Directly access the environment variable set in Render
HOSTAWAY_API_KEY = os.getenv("HOSTAWAY_ACCESS_TOKEN")  # This will use the Render environment variable
HOSTAWAY_API_BASE = "https://api.hostaway.com/v1"

@router.post("/slack-interactivity")
async def slack_action(request: Request):
    form_data = await request.form()
    payload = json.loads(form_data["payload"])
    action = payload["actions"][0]
    action_type = action["name"]
    reservation_id = int(payload["callback_id"])

    if action_type == "approve":
        reply = action["value"]
        send_reply_to_hostaway(reservation_id, reply)
        return JSONResponse({"text": "✅ Reply approved and sent to guest."})

    elif action_type == "write_own":
        return JSONResponse({
            "text": "📝 Please compose your message below.",
            "attachments": [
                {
                    "callback_id": str(reservation_id),
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
    url = f"{HOSTAWAY_API_BASE}/conversations/{message_id}/messages"
    headers = {
        "Authorization": f"Bearer {HOSTAWAY_API_KEY}",
        "Content-Type": "application/json"
    }
    payload = {"body": reply_text}

    logging.info(f"📬 Sending reply to Hostaway (message ID: {message_id})")
    r = requests.post(url, headers=headers, json=payload)

    if r.status_code != 200:
        logging.error(f"❌ Hostaway reply failed: {r.status_code} - {r.text}")
    else:
        logging.info("✅ Hostaway reply sent successfully.")
