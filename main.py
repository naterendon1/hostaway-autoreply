import os
import logging
from fastapi import FastAPI, Request
from slack_sdk import WebClient
from slack_interactivity import router as slack_router
from utils import (
    fetch_hostaway_listing,
    get_property_info
)

logging.basicConfig(level=logging.INFO)

app = FastAPI()
app.include_router(slack_router)

# Configure Slack client
SLACK_BOT_TOKEN = os.getenv("SLACK_BOT_TOKEN")
slack_client = WebClient(token=SLACK_BOT_TOKEN)
SLACK_CHANNEL_ID = os.getenv("SLACK_CHANNEL_ID")  # Set in your environment

@app.post("/unified-webhook")
async def unified_webhook(request: Request):
    payload = await request.json()
    logging.info(f"📬 Webhook received: {payload}")

    data = payload.get("data", {})
    guest_message = data.get("body")
    listing_id = data.get("listingMapId")
    conversation_id = data.get("conversationId")

    # Example: Get listing property info
    fields_needed = ["propertyType", "bedrooms", "bathrooms"]
    listing_result = fetch_hostaway_listing(listing_id)
    property_info = get_property_info(listing_result, fields_needed)

    # Send a message to Slack when a new Hostaway message comes in
    if guest_message and SLACK_CHANNEL_ID:
        slack_text = (
            f"*New Guest Message in Hostaway*\n"
            f"*Listing:* {listing_id}\n"
            f"*Property:* {property_info}\n"
            f"*Conversation ID:* {conversation_id}\n"
            f"*Message:*\n>{guest_message}"
        )
        slack_client.chat_postMessage(channel=SLACK_CHANNEL_ID, text=slack_text)
        logging.info("✅ Sent message to Slack.")

    return {"status": "ok"}

@app.get("/")
async def health():
    return {"status": "ok"}

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
