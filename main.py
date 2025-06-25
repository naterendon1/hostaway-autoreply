from fastapi import FastAPI, Request
from pydantic import BaseModel
import os
import logging
from openai import OpenAI
from slack_sdk.webhook import WebhookClient

app = FastAPI()
client = OpenAI(api_key=os.getenv("OPENAI_API_KEY"))

logging.basicConfig(level=logging.INFO)

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

    prompt = f"""You are a professional short-term rental manager. A guest staying at '{listing_name}' sent this message:
"{guest_message}"

Write a warm, professional reply. Be friendly and helpful. No sign-off needed."""

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
        logging.error(f"❌ OpenAI error: {e}")
        ai_reply = "(Error generating reply with OpenAI.)"

    # Send message to Slack
    webhook = WebhookClient(os.getenv("SLACK_WEBHOOK_URL"))
    webhook.send(text=f"*New Guest Message for {listing_name}:*\n>{guest_message}\n\n*Suggested Reply:*\n>{ai_reply}")

    return {"status": "sent_to_slack"}
