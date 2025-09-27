# file: smart_intel.py
import logging
from typing import Dict, Any, List
from openai import OpenAI
import os

OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
openai_client = OpenAI(api_key=OPENAI_API_KEY) if OPENAI_API_KEY else None

def _format_nearby_places(nearby_places: List[Dict[str, Any]]) -> str:
    """Turn Google Places API results into a friendly string for the LLM."""
    if not nearby_places:
        return ""
    parts = []
    for category in nearby_places:
        label = category.get("label") or "Nearby"
        places = category.get("places") or []
        if not places:
            continue
        items = []
        for p in places:
            name = p.get("name")
            vicinity = p.get("vicinity")
            rating = p.get("rating")
            distance = p.get("distance_text")
            duration = p.get("duration_text")
            if rating:
                items.append(f"- {name} ({vicinity}, {rating}⭐, {distance or '?'} / {duration or '?'})")
            else:
                items.append(f"- {name} ({vicinity}, {distance or '?'} / {duration or '?'})")
        if items:
            parts.append(f"{label}:\n" + "\n".join(items))
    return "\n\n".join(parts)


def generate_reply(guest_message: str, context: Dict[str, Any]) -> str:
    """
    Use OpenAI to generate a smart reply based on:
      - guest message
      - reservation details
      - listing details
      - nearby places (Google Places API)
    """
    if not openai_client:
        logging.warning("OpenAI API key not set; returning fallback reply.")
        return "Hi! Thanks for your message. I’ll get back to you shortly."

    guest_name = context.get("guest_name", "Guest")
    listing_info = context.get("listing_info", {})
    reservation = context.get("reservation", {})
    history = context.get("history", [])
    nearby_places = context.get("nearby_places", [])

    # Basic property details
    name = listing_info.get("name", "the property")
    beds = listing_info.get("beds")
    bedrooms = listing_info.get("bedrooms")
    bathrooms = listing_info.get("bathrooms")
    check_in = context.get("check_in_date")
    check_out = context.get("check_out_date")

    # Format guest conversation history (only last few messages)
    history_text = ""
    if history:
        lines = []
        for h in history[-5:]:
            who = "Guest" if h["role"] == "guest" else "Host"
            lines.append(f"{who}: {h['text']}")
        history_text = "\n".join(lines)

    # Format nearby places
    nearby_text = _format_nearby_places(nearby_places)

    # Compose system and user prompts
    system_prompt = (
        "You are a helpful assistant for a vacation rental host. "
        "You write clear, concise, accurate replies to guests. "
        "Use ONLY the information provided below. "
        "If the guest asks about beds, bedrooms, bathrooms, WiFi, check-in/out times, etc., "
        "use the listing details provided. "
        "If the guest asks about local recommendations, use the provided nearby places list. "
        "Do not hallucinate or make up details. "
        "Do not include greetings or sign-offs unless it's an email. "
        "Write in a friendly, conversational tone but concise."
    )

    user_prompt = (
        f"Guest message from {guest_name}:\n"
        f"{guest_message}\n\n"
        f"Reservation details:\nCheck-in: {check_in}, Check-out: {check_out}\n\n"
        f"Listing details:\n"
        f"- Property name: {name}\n"
        f"- Beds: {beds}\n"
        f"- Bedrooms: {bedrooms}\n"
        f"- Bathrooms: {bathrooms}\n\n"
    )

    if nearby_text:
        user_prompt += f"Nearby places you can suggest if relevant:\n{nearby_text}\n\n"

    if history_text:
        user_prompt += f"Conversation history:\n{history_text}\n\n"

    user_prompt += (
        "Write a concise, accurate reply addressing the guest's question(s). "
        "Only use the information given. "
        "If a detail is missing, say you’ll check and follow up rather than guessing. "
        "Do not include sign-offs."
    )

    try:
        resp = openai_client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            temperature=0.3,
        )
        reply = (resp.choices[0].message.content or "").strip()
        return reply
    except Exception as e:
        logging.error(f"OpenAI error: {e}")
        return "Thanks for your message! I’ll get back to you shortly."
