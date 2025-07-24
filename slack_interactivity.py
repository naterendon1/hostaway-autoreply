import os
import logging
import json
import re
from fastapi import FastAPI
from slack_interactivity import router as slack_router
from pydantic import BaseModel
from openai import OpenAI
from utils import (
    fetch_hostaway_resource,
    fetch_hostaway_listing,
    fetch_hostaway_reservation,
    fetch_hostaway_conversation,
    get_cancellation_policy_summary,
    get_similar_learning_examples,
    store_learning_example,
    store_clarification_log
)

logging.basicConfig(level=logging.INFO)

HOSTAWAY_CLIENT_ID = os.getenv("HOSTAWAY_CLIENT_ID")
HOSTAWAY_CLIENT_SECRET = os.getenv("HOSTAWAY_CLIENT_SECRET")
HOSTAWAY_API_BASE = "https://api.hostaway.com/v1"
SLACK_CHANNEL = os.getenv("SLACK_CHANNEL")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
SLACK_BOT_TOKEN = os.getenv("SLACK_BOT_TOKEN")

app = FastAPI()
app.include_router(slack_router)

openai_client = OpenAI(api_key=OPENAI_API_KEY)

MAX_THREAD_MESSAGES = 10

SYSTEM_PROMPT = (
    "You're a helpful vacation rental host. Always respond casually, briefly, and to the point. "
    "Use the guest's first name if known. "
    "Use only the provided property and reservation info — do not guess. "
    "If something is missing, say you'll check and follow up. "
    "No chit-chat, no extra tips, no sign-offs."
)

class HostawayUnifiedWebhook(BaseModel):
    object: str
    event: str
    accountId: int
    data: dict
    body: str = None
    listingName: str = None
    date: str = None

def determine_needed_fields(guest_message: str):
    core_listing_fields = {"summary", "amenities", "houseManual", "type", "name"}
    extra_fields = set()
    text = guest_message.lower()
    if any(keyword in text for keyword in [
        "how far", "distance", "close", "how close", "near", "nearest", "proximity", "from", "to", "airport", "downtown", "center", "stadium"
    ]):
        extra_fields.update({"address", "city", "zipcode", "state"})
    if "parking" in text or "car" in text or "vehicle" in text:
        extra_fields.update({"parking", "amenities", "houseManual"})
    if "price" in text or "cost" in text or "fee" in text or "rate" in text:
        extra_fields.update({"price", "cleaningFee", "securityDepositFee", "currencyCode"})
    if "cancel" in text or "refund" in text:
        extra_fields.update({"cancellationPolicy", "cancellationPolicyId"})
    if any(x in text for x in ["wifi", "internet", "tv", "cable", "smart", "stream", "netflix"]):
        extra_fields.update({"amenities", "houseManual", "wifiUsername", "wifiPassword"})
    if any(x in text for x in ["bed", "sofa", "couch", "sleep", "bedroom"]):
        extra_fields.update({"bedroomsNumber", "bedsNumber", "guestBathroomsNumber"})
    if "guest" in text or "person" in text or "max" in text or "limit" in text or "occupancy" in text:
        extra_fields.update({"personCapacity", "maxChildrenAllowed", "maxInfantsAllowed", "maxPetsAllowed", "guestsIncluded"})
    if "pet" in text or "dog" in text or "cat" in text or "animal" in text:
        extra_fields.update({"maxPetsAllowed", "amenities", "houseRules"})
    return core_listing_fields.union(extra_fields)

def get_property_type(listing_result):
    prop_type = (listing_result.get("type") or "").lower()
    name = (listing_result.get("name") or "").lower()
    for t in ["house", "cabin", "condo", "apartment", "villa", "bungalow", "cottage", "suite"]:
        if t in prop_type:
            return t
        if t in name:
            return t
    return "home"

def clean_ai_reply(reply: str, property_type="home"):
    bad_signoffs = [
        "Enjoy your meal", "Enjoy your meals", "Enjoy!", "Best,", "Best regards,", "Cheers,", "Sincerely,", "[Your Name]", "Best", "Sincerely"
    ]
    for signoff in bad_signoffs:
        reply = reply.replace(signoff, "")
    lines = reply.split('\n')
    filtered_lines = []
    for line in lines:
        stripped = line.strip()
        if any(stripped.startswith(s.replace(",", "")) for s in ["Best", "Cheers", "Sincerely"]):
            continue
        if "[Your Name]" in stripped:
            continue
        filtered_lines.append(line)
    reply = ' '.join(filtered_lines)
    address_patterns = [
        r"(the )?house at [\d]+ [^,]+, [A-Za-z ]+",
        r"\d{3,} [A-Za-z0-9 .]+, [A-Za-z ]+",
        r"at [\d]+ [\w .]+, [\w ]+"
    ]
    for pattern in address_patterns:
        reply = re.sub(pattern, f"the {property_type}", reply, flags=re.IGNORECASE)
    reply = re.sub(r"at [A-Za-z0-9 ,/\-\(\)\']+", f"at the {property_type}", reply, flags=re.IGNORECASE)
    reply = ' '.join(reply.split())
    reply = reply.strip().replace(" ,", ",").replace(" .", ".")
    return reply.rstrip(",. ")

@app.get("/ping")
def ping():
    return {"status": "ok"}
