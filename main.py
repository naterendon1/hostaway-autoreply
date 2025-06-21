from fastapi import FastAPI, Request
from pydantic import BaseModel
import os
import logging
from openai import OpenAI, OpenAIError
from slack_sdk.webhook import WebhookClient
from slack_sdk.errors import SlackApiError

# Configure logging
logging.basicConfig(level=logging.INFO)

app = FastAPI()
client = OpenAI(api_key=os.getenv("OPENAI_API_KEY"))

# Pydantic model for Hostaway webhook payload
class HostawayWebhook(BaseModel):
    id: int
    body: str
    listingName: str

@app.post("/hostaway-webhook")
async def receive_message(payload: HostawayWebhook):
    try:
        guest_message = payload.body
        listing_name = payload.listingName or "Guest"
        message_id = payload.id

        logging.info(f"📩 New guest message received: {guest_message}")

        # Build GPT prompt
        prompt = (
            f"You are a professional short-term rental manager. "
            f"A guest staying at '{listing_name}' sent this message:\n"
            f"\"{guest_message}\"\n\n"
            f"Write a warm, professional reply. Be friendly and helpful. Sign off politely."
        )

        # Generate AI reply
        try:
            response = client.chat.completions.create(
                model="gpt-3.5-turbo",
                messages=[
                    {"role": "system", "content": "You are a helpful, friendly vacation rental host."},
                    {"role": "user", "content": prompt}
                ]
            )
            ai_reply = response.choices[0].message.content.strip()
        except OpenAIError as e:
            logging.error(f"❌ OpenAI error: {e}")
            ai_reply = "(Error generating reply with OpenAI.)"

        # Build Slack message
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

        # Send to Slack
        slack_url = os.getenv("SLACK_WEBHOOK_URL")
        if slack_url:
            try:
                webhook = WebhookClient(slack_url)
                webhook.send(**slack_message)
                logging.info("✅ Sent message to Slack.")
            except SlackApiError as e:
                logging.error(f"❌ Slack error: {e.response['error']}")
        else:
            logging.warning("⚠️ SLACK_WEBHOOK_URL not set. Skipping Slack notification.")

        return {"status": "sent_to_slack"}

    except Exception as e:
        logging.exception("🔥 Unexpected error in /hostaway-webhook")
        return {"status": "error", "detail": str(e)}
