from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from typing import Optional
import os
import requests
import json
import logging
from dotenv import load_dotenv
from slack_sdk.webhook import WebhookClient
from openai import OpenAI
import os
import logging

logging.info(f"Hostaway Access Token: {os.getenv('HOSTAWAY_ACCESS_TOKEN')}")

# Load environment variables
load_dotenv()

# Set up FastAPI app and logging
app = FastAPI()
logging.basicConfig(level=logging.INFO)

# Set up OpenAI and API keys
client = OpenAI(api_key=os.getenv("OPENAI_API_KEY"))
SLACK_WEBHOOK_URL = os.getenv("SLACK_WEBHOOK_URL")
HOSTAWAY_API_KEY = os.getenv("HOSTAWAY_ACCESS_TOKEN")  # Using access token instead of API key
HOSTAWAY_API_BASE = "https://api.hostaway.com/v1"

# Define Pydantic model for payload with Optional fields
class HostawayUnifiedWebhook(BaseModel):
    object: str
    event: str
    accountId: int
    data: dict
    body: Optional[str] = None
    listingName: Optional[str] = None
    date: Optional[str] = None

@app.get("/")
def read_root():
    return {"message": "Welcome to the Hostaway Auto Reply Service!"}

@app.post("/unified-webhook")
async def unified_webhook(payload: HostawayUnifiedWebhook):
    # Log the entire payload
    logging.info(f"Received payload: {json.dumps(payload.dict(), indent=2)}")
    
    if payload.event == "message.received" and payload.object == "conversationMessage":
        guest_message = payload.data.get("body", "")
        listing_name = payload.data.get("listingName", "Guest")
        conversation_id = payload.data.get("conversationId", None)
        message_id = payload.data.get("id", None)  # Get the actual message ID

        logging.info(f"📩 New guest message received: {guest_message}")

        # Prepare prompt for OpenAI to generate a reply
        prompt = f"""You are a professional short-term rental manager. A guest sent this message:
{guest_message}

Write a warm, professional reply. Be friendly and helpful. Use a tone that is informal, concise, and polite. Don't include a signoff."""

        try:
            # Generate reply using OpenAI
            response = client.chat.completions.create(
                model="gpt-4",
                messages=[
                    {"role": "system", "content": "You are a helpful, friendly vacation rental host."},
                    {"role": "user", "content": prompt}
                ]
            )
            ai_reply = response.choices[0].message.content.strip()
        except Exception as e:
            logging.error(f"❌ OpenAI error: {str(e)}")
            ai_reply = "(Error generating reply with OpenAI.)"

        # Prepare Slack message with the generated reply
        slack_message = {
            "text": f"*New Guest Message for {listing_name}:*\n>{guest_message}\n\n*Suggested Reply:*\n>{ai_reply}",
            "attachments": [
                {
                    "callback_id": str(conversation_id),
                    "fallback": "You are unable to choose a response",
                    "color": "#3AA3E3",
                    "attachment_type": "default",
                    "actions": [
                        {
                            "name": "approve",
                            "text": "✅ Approve",
                            "type": "button",
                            "value": ai_reply,
                            "style": "primary"
                        },
                        {
                            "name": "write_own",
                            "text": "📝 Write Your Own",
                            "type": "button",
                            "value": str(conversation_id)
                        }
                    ]
                }
            ]
        }

        # Send the message to Slack
        try:
            webhook = WebhookClient(SLACK_WEBHOOK_URL)
            webhook.send(**slack_message)
            logging.info("✅ Slack message sent successfully.")
        except Exception as e:
            logging.error(f"❌ Failed to send Slack message: {str(e)}")

    return {"status": "ok"}

@app.post("/slack-interactivity")
async def slack_action(request: Request):
    form_data = await request.form()
    payload = json.loads(form_data["payload"])

    # Log the entire payload to inspect its structure
    logging.info(f"Received Slack payload: {json.dumps(payload, indent=2)}")

    action = payload["actions"][0]
    action_type = action["name"]
    conversation_id = payload.get("callback_id")

    # Handle different action types
    if action_type == "approve" and conversation_id:
        reply = action["value"]
        
        # Directly attempt to send the message
        success = send_reply_to_hostaway(conversation_id, reply)
        
        if success:
            return JSONResponse({
                "text": f"✅ Reply sent to guest:\n\n>{reply}",
                "replace_original": True
            })
        else:
            return JSONResponse({
                "text": "❌ Failed to send reply to Hostaway. Please check:\n1. API key permissions\n2. Conversation still exists\n3. Correct endpoint URL",
                "replace_original": True
            })

    elif action_type == "write_own":
        return JSONResponse({
            "text": "📝 Please compose your message below.",
            "attachments": [
                {
                    "callback_id": str(conversation_id),
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
                            "value": "send",
                            "style": "primary"
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
    """Send a reply to Hostaway's messaging system"""
    url = f"{HOSTAWAY_API_BASE}/conversations/{conversation_id}/messages"
    headers = {
        "Authorization": f"Bearer {HOSTAWAY_ACCESS_TOKEN}",
        "Cache-Control": "no-cache",
        "Content-Type": "application/json",
        "Accept": "application/json"
    }
    payload = {
        "body": reply_text,
        "isIncoming": 0,  # 0 = host to guest (outgoing)
        "communicationType": "email"  # Added based on Hostaway API requirements
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
        error_detail = {
            "status_code": e.response.status_code,
            "response_text": e.response.text,
            "request_url": url,
            "request_headers": headers,
            "request_payload": payload
        }
        logging.error(f"❌ HTTP error sending reply: {json.dumps(error_detail, indent=2)}")
        
        if e.response.status_code == 404:
            logging.error("🔍 404 Not Found - Possible issues:")
            logging.error(f"1. Invalid conversation ID: {conversation_id}")
            logging.error(f"2. Incorrect endpoint URL: {url}")
            logging.error("3. Missing required parameters in payload")
        elif e.response.status_code == 403:
            logging.error("🔒 403 Forbidden - Please verify:")
            logging.error("1. Your API key has 'messages:write' permission")
            logging.error("2. Your API key is valid and not expired")
            logging.error("3. Your server IP is whitelisted if required")
        
        return False
        
    except Exception as e:
        logging.error(f"❌ Unexpected error sending reply: {str(e)}")
        return False
