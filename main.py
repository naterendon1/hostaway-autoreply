import logging
from fastapi import FastAPI, Request
from slack_interactivity import router as slack_router
from utils import (
    send_reply_to_hostaway,
    fetch_hostaway_resource,
    store_learning_example,
    get_similar_learning_examples,
    store_clarification_log,
    get_property_info,          # <-- THIS FIXES YOUR ERROR
    fetch_hostaway_listing
)
# (No need to import re, os, json, WebClient, or OpenAI here—keep those in the files where you actually use them.)

logging.basicConfig(level=logging.INFO)

app = FastAPI()

# Mount your Slack interactivity router
app.include_router(slack_router)

@app.post("/unified-webhook")
async def unified_webhook(request: Request):
    payload = await request.json()
    logging.info(f"📬 Webhook received: {payload}")

    data = payload.get("data", {})
    conversation_id = data.get("conversationId")
    listing_id = data.get("listingMapId")
    guest_message = data.get("body")
    reservation_id = data.get("reservationId")

    # --- EXAMPLE: using get_property_info correctly ---
    fields_needed = ["propertyType", "bedrooms", "bathrooms"]
    listing_result = fetch_hostaway_listing(listing_id)
    property_info = get_property_info(listing_result, fields_needed)

    # Your business logic here...
    logging.info(f"Property Info: {property_info}")

    return {"status": "ok"}

@app.get("/")
async def health():
    return {"status": "ok"}

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
