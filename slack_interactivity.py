from fastapi import APIRouter, Request, Form
from fastapi.responses import JSONResponse
import json
import logging
import requests
import os

router = APIRouter()

# Environment variables
HOSTAWAY_API_KEY = os.getenv("HOSTAWAY_ACCESS_TOKEN")  # Ensure this matches your curl token
HOSTAWAY_API_BASE = "https://api.hostaway.com/v1"

@router.post("/slack-interactivity")
async def slack_action(request: Request):
    form_data = await request.form()
    payload = json.loads(form_data["payload"])
    action = payload["actions"][0]
    action_type = action["name"]
    callback_id = payload["callback_id"]  # Renamed for clarity (may be conversation_id)

    if action_type == "approve":
        reply = action["value"]
        success = send_reply_to_hostaway(callback_id, reply)  # Pass callback_id directly
        if success:
            return JSONResponse({"text": "✅ Reply approved and sent to guest."})
        else:
            return JSONResponse({"text": "❌ Failed to send reply to Hostaway."})

    elif action_type == "write_own":
        return JSONResponse({
            "text": "📝 Please compose your message below.",
            "attachments": [
                {
                    "callback_id": callback_id,  # Use callback_id here too
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


def send_reply_to_hostaway(conversation_id: str, reply_text: str) -> bool:
    url = f"{HOSTAWAY_API_BASE}/conversations/{conversation_id}/messages"
    headers = {
        "Authorization": f"Bearer {HOSTAWAY_API_KEY}",
        "Content-Type": "application/json",
    }
    payload = {
        "body": reply_text,
        "isIncoming": 0,  # Must be 0 for host-to-guest
        "communicationType": "email"  # or "sms"
    }
    response = requests.post(url, headers=headers, json=payload)
    if response.status_code != 200:
        logging.error(f"Hostaway API Error: {response.text}")  # Log detailed error
        return False
    return True
