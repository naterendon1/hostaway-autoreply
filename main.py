from fastapi import FastAPI, Request
from pydantic import BaseModel
import os
import logging
import json
import requests
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

# --------------------------------------
# Slack Interactivity Handler (Approve button)
# --------------------------------------

@app.post("/slack-interactivity")
async def handle_slack_button(request: Request):
    form_data = await request.form()
    payload = json.loads(form_data["payload"])
    action = payload["actions"][0]
    message_id = int(payload["callback_id"])
    action_value = action["value"]

    logging.info(f"🟢 Slack button clicked: {action['name']} for message {message_id}")

    if action["name"] == "approve":
        return post_reply_to_hostaway(message_id, action_value)
    else:
        return {"text": "❌ Rejected. No reply sent."}

def post_reply_to_hostaway(message_id: int, reply_text: str):
    url = f"https://api.hostaway.com/v1/messages/{message_id}/reply"
    headers = {
        "Authorization": f"Bearer {os.getenv('HOSTAWAY_API_KEY')}",
        "Content-Type": "application/json"
    }
    payload = {"body": reply_text}

    try:
        res = requests.post(url, headers=headers, json=payload)
        res.raise_for_status()
        logging.info(f"✅ Sent reply to Hostaway message {message_id}")
        return {"text": "✅ Reply sent to guest!"}
    except requests.exceptions.RequestException as e:
        logging.error(f"❌ Failed to send reply to Hostaway: {e}")
        return {"text": f"❌ Failed to send: {e}"}
