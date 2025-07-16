from fastapi import APIRouter, Request, Form
from fastapi.responses import JSONResponse
import json
import logging
import requests
import os

router = APIRouter()

HOSTAWAY_API_KEY = os.getenv("HOSTAWAY_ACCESS_TOKEN")
HOSTAWAY_API_BASE = "https://api.hostaway.com/v1"

@router.post("/slack-interactivity")
async def slack_action(request: Request):
    form_data = await request.form()
    payload = json.loads(form_data["payload"])
    action = payload["actions"][0]
    action_type = action["name"]
    callback_id = payload["callback_id"]

    if action_type == "approve":
        reply = action["value"]
        success = send_reply_to_hostaway(callback_id, reply)
        if success:
            return JSONResponse({"text": "✅ Reply approved and sent to guest."})
        else:
            return JSONResponse({"text": "❌ Failed to send reply to Hostaway."})

    elif action_type == "write_own":
        return JSONResponse({
            "text": "📝 Please compose your message below.",
            "attachments": [
                {
                    "callback_id": callback_id,
                    "fallback": "Compose your reply",
                    "color": "#3AA3E3",
                    "attachment_type": "default",
                    "actions": [
                        {"name": "back", "text": "🔙 Back", "type": "button", "value": "back"},
                        {"name": "improve", "text": "✏️ Improve with AI", "type": "button", "value": "improve"},
                        {"name": "send", "text": "📨 Send", "type": "button", "value": "send"}
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
    "Accept": "application/json",
    "Cache-Control": "no-cache"
}
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
        logging.error(f"❌ HTTPError sending reply: {e.response.status_code} - {e.response.text}")
        return False
    except Exception as e:
        logging.error(f"❌ Unexpected error: {str(e)}")
        return False
