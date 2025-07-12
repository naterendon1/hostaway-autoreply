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

# Load environment variables
load_dotenv()

# Set up FastAPI app and logging
app = FastAPI()
logging.basicConfig(level=logging.INFO)

# Set up OpenAI and API keys
client = OpenAI(api_key=os.getenv("OPENAI_API_KEY"))
SLACK_WEBHOOK_URL = os.getenv("SLACK_WEBHOOK_URL")
HOSTAWAY_API_KEY = os.getenv("HOSTAWAY_API_KEY")
HOSTAWAY_API_BASE = "https://api.hostaway.com/v1"

# Define Pydantic model for payload with Optional fields
class HostawayUnifiedWebhook(BaseModel):
    event: str
    entityId: int
    entityType: str
    data: dict

    # Optional fields in case they are missing from the payload
    body: Optional[str] = None
    listingName: Optional[str] = None
    date: Optional[str] = None

@app.post("/unified-webhook")
async def unified_webhook(payload: HostawayUnifiedWebhook):
    # Log the entire payload as a string to understand its structure
    logging.info(f"Received payload: {json.dumps(payload.dict(), indent=2)}")  # Log the entire payload

    if payload.event == "guestMessage" and payload.entityType == "message":
        guest_message = payload.data.get("body", "")
        listing_name = payload.data.get("listingName", "Guest
