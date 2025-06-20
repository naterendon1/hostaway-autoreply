from fastapi import FastAPI, Request
import json
import requests
import os
from dotenv import load_dotenv

load_dotenv()

app = FastAPI()

HOSTAWAY_API_KEY = os.getenv("HOSTAWAY_API_KEY")
HOSTAWAY_API_BASE = "https://api.hostaway.com/v1"

@app.post("/slack-interactivity")
async def slack_action(request: Request):
    payload = await request.form()
    action_payload = json.loads(payload["payload"])
    actions = action_payload["actions"]
    action = actions[0]
    message_id = int(action_payload["callback_id"])

    if action["name"] == "approve":
        reply = action["value"]
        send_reply_to_hostaway(message_id, reply)
        return {"text": "✅ Reply approved and sent."}

    return {"text": "❌ Reply rejected."}


def send_reply_to_hostaway(message_id: int, reply_text: str):
    url = f"{HOSTAWAY_API_BASE}/messages/{message_id}/reply"
    headers = {
        "Authorization": f"Bearer {HOSTAWAY_API_KEY}",
        "Content-Type": "application/json"
    }
    payload = {"body": reply_text}
    r = requests.post(url, headers=headers, json=payload)
    r.raise_for_status()
