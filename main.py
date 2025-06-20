from fastapi import FastAPI, Request
from pydantic import BaseModel
import requests
import openai
import os
from dotenv import load_dotenv
from slack_sdk.webhook import WebhookClient

load_dotenv()

app = FastAPI()

# Env variables
HOSTAWAY_API_KEY = os.getenv("HOSTAWAY_API_KEY")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
SLACK_WEBHOOK_URL = os.getenv("SLACK_WEBHOOK_URL")
SLACK_INTERACT_URL = os.getenv("SLACK_INTERACT_URL")

openai.api_key = OPENAI_API_KEY


class HostawayMessage(BaseModel):
    id: int
    body: str
    guest: dict


def generate_reply(guest_message: str, guest_name: str):
    prompt = f"""You are a professional short-term rental manager. A guest named {guest_name} sent this message:
"{guest_message}"

Write a friendly, helpful reply in a professional tone. Sign off politely."""

    response = openai.ChatCompletion.create(
        model="gpt-4",
        messages=[
            {"role": "system", "content": "You are a helpful, friendly vacation rental host."},
            {"role": "user", "content": prompt}
        ]
    )
    return response.choices[0].message.content.strip()


@app.post("/hostaway-webhook")
async def receive_message(request: Request):
    body = await request.body()
    print("🔥 RAW PAYLOAD RECEIVED:", body.decode())

    try:
        json_data = await request.json()
        print("✅ PARSED JSON:", json_data)
    except Exception as e:
        print("❌ FAILED TO PARSE JSON:", e)
        raise HTTPException(status_code=400, detail="Invalid JSON")

    return {"status": "test"}

    ai_reply = generate_reply(guest_message, guest_name)

    slack_message = {
        "text": f"*New Guest Message from {guest_name}:*\n>{guest_message}\n\n*Suggested Reply:*\n>{ai_reply}",
        "attachments": [
            {
                "text": "Do you want to approve this response?",
                "callback_id": f"{message_id}",
                "actions": [
                    {
                        "name": "approve",
                        "text": "✅ Approve",
                        "type": "button",
                        "value": ai_reply
                    },
                    {
                        "name": "reject",
                        "text": "❌ Reject",
                        "type": "button",
                        "value": "reject"
                    }
                ]
            }
        ]
    }

    webhook = WebhookClient(SLACK_WEBHOOK_URL)
    webhook.send(**slack_message)

    return {"status": "sent_to_slack"}
