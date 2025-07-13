from fastapi import APIRouter, Request, Form
from fastapi.responses import JSONResponse
import json
import logging
import requests
import os

router = APIRouter()

HOSTAWAY_API_KEY = os.getenv("eyJ0eXAiOiJKV1QiLCJhbGciOiJSUzI1NiJ9.eyJhdWQiOiIxMzI5NiIsImp0aSI6IjhiN2I5OTA0MjNhYTkzYWVjYzQ1NmFmOTE2ODk5NWJmMGQyNmIyMmI2NDMwM2IxYjNmOGJkMTMyYjBiMjVhODU5YjYyMTc3NDI1ODY3NzU1IiwiaWF0IjoxNzUyNDQ1MzY0LjkyNjUwMSwibmJmIjoxNzUyNDQ1MzY0LjkyNjUwMywiZXhwIjoyMDY3OTc4MTY0LjkyNjUwNiwic3ViIjoiIiwic2NvcGVzIjpbImdlbmVyYWwiXSwic2VjcmV0SWQiOjY3MjA5fQ.pNkPVX97kOqQxea_3mFkow3CnNA6Zjwb08Bfbr4h_bbfrPuO2Qjk8SdLTVXA1mfDrKh6MuZPKqNEImssELVcg515dwIM8-4-JdplN7DBjxFgB7csqLkGDO1PpmgovycMhzR0eC8_be62FMRUQZYp5P4WDNYVvZsRUPf_JnggE2cPekcjW_KgaN64_XuEuPBudRxQXK-qiBIC8Fb1rz5nwIXKcOpXYuTYs4ijHx0tO31WIwJZFAkySIU3_qNTw81q2qiK73NGLZDo9m3-4YhQgKGMKUljvdfhh7sanQtm4dyAat6CmVhJhfgP5OVt3tQrQRieKi5F0zC1M9xvfqElQA
")  # Use the access token from the .env file
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
