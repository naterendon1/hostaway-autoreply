from fastapi import FastAPI, Request, HTTPException
from pydantic import BaseModel
import openai
import os
from slack_sdk.webhook import WebhookClient

app = FastAPI()
class HostawayWebhook(BaseModel):
    id: int
    body: str
    listingName: str

@app.post("/hostaway-webhook")
async def receive_message(payload: HostawayWebhook):
    guest_message = payload.body
    listing_name = payload.listingName or "Guest"
    message_id = payload.id

    # Generate reply using ChatGPT
    prompt = f"""You are a professional short-term rental manager. A guest staying at '{listing_name}' sent this message:
\"{guest_message}\"

Write a warm, professional reply. Be friendly and helpful. Sign off politely."""

    response = openai.ChatCompletion.create(
        model="gpt-4",
        messages=[
            {"role": "system", "content": "You are a helpful, friendly vacation rental host."},
            {"role": "user", "content": prompt}
        ]
    )

    ai_reply = response.choices[0].message.content.strip()

    # Send to Slack
    slack_message = {
        "text": f"*New Guest Message for {listing_name}:*\n>{guest_message}\n\n*Suggested Reply:*\n>{ai_reply}",
        "attachments": [
            {
                "text": "Do you want to approve this response?",
                "callback_id": str(message_id),
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

    webhook = WebhookClient(os.getenv("SLACK_WEBHOOK_URL"))
    webhook.send(**slack_message)

    return {"status": "sent_to_slack"}

