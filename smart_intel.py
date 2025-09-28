# file: smart_intel.py

import os
import re
import json
import logging
from datetime import datetime, timezone
from typing import Dict, Any, Optional

# OpenAI client (1.x)
try:
    from openai import OpenAI
    _openai_client = OpenAI(api_key=os.getenv("OPENAI_API_KEY"))
    _use_client = True
except Exception:
    # Fallback to raw HTTPS if needed later
    _openai_client = None
    _use_client = False

OPENAI_MODEL = os.getenv("OPENAI_MODEL", "gpt-4o-mini")

# Distance helper from places.py (single-destination)
from places import get_drive_distance_duration

# ---- Small, fast "reply planner" -------------------------------------------------

DISTANCE_PATTERNS = re.compile(
    r"\b(how\s+far|distance|how\s+long|drive\s*time|minutes?\s*(away|drive))\b",
    re.I,
)

def _parse_iso_date(s: Optional[str]) -> Optional[datetime]:
    if not s:
        return None
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00"))
    except Exception:
        return None

def infer_stay_phase(check_in: Optional[str], check_out: Optional[str], *, now: Optional[datetime] = None) -> str:
    """Return 'pre_arrival' | 'in_stay' | 'post_stay' | 'unknown'."""
    # Use server clock; your server is in UTC, but we only compare by date.
    now = now or datetime.now(timezone.utc)
    ci = _parse_iso_date(check_in)
    co = _parse_iso_date(check_out)

    if not ci or not co:
        return "unknown"
    if now.date() < ci.date():
        return "pre_arrival"
    if ci.date() <= now.date() <= co.date():
        return "in_stay"
    if now.date() > co.date():
        return "post_stay"
    return "unknown"

def detect_distance_intent(text: str) -> bool:
    return bool(DISTANCE_PATTERNS.search(text or ""))

def normalize_destination(text: str) -> Optional[str]:
    """
    Expand common venue names to canonical queries for Google Distance Matrix.
    Add any venues you care about here.
    """
    t = (text or "")
    if re.search(r"\bheb\b.*center\b", t, re.I):
        return "H-E-B Center at Cedar Park, TX"
    return None

# ---- Prompt scaffolding ----------------------------------------------------------

STYLE = """Tone: warm, concise, proactive, no fluff. Use 1–3 short sentences. One brief follow-up question at most. Never contradict known facts from context."""

def _few_shots() -> list[dict]:
    # Short, targeted behaviors the model should imitate
    return [
        # Distance intent with provided data
        {
            "role": "user",
            "content": json.dumps({
                "guest_message": "How far is the HEB center from this house? I’m going to a concert there.",
                "facts": {"phase":"pre_arrival","distance":{"distance_text":"12.4 mi","duration_text":"23 mins"}},
            })
        },
        {
            "role": "assistant",
            "content": "It’s about 12.4 miles—roughly a 23-minute drive, depending on traffic. I can send a route if you’d like."
        },
        # Post-stay thank-you
        {
            "role": "user",
            "content": json.dumps({
                "guest_message": "Thank you! We enjoyed our stay!",
                "facts": {"phase":"post_stay"}
            })
        },
        {
            "role": "assistant",
            "content": "I’m so glad you enjoyed your stay—thanks again for choosing us! Safe travels, and we’d love to host you again."
        },
        # Missing data
        {
            "role": "user",
            "content": json.dumps({
                "guest_message": "How far is the arena?",
                "facts": {"phase":"pre_arrival"},
                "missing": ["property_address","destination"]
            })
        },
        {
            "role": "assistant",
            "content": "Happy to help! Could you confirm the property address and the arena’s full name? I’ll check drive time right away."
        },
    ]

SYSTEM_TEMPLATE = """You are an expert guest-messaging assistant for short-term rentals.

Always:
- Read the guest message carefully and use the provided facts.
- Infer stay phase from dates using today's date: {today}.
  * pre_arrival: today < check-in
  * in_stay: check-in ≤ today ≤ check-out
  * post_stay: today > check-out
- If phase is post_stay, speak in past tense and thank them. Never say “enjoy the rest of your stay”.
- If the guest asks for distance/time and a distance fact is provided, include it succinctly.
- If address or destination is missing for a distance question, ask for exactly the missing fields (one concise question).
- If local recommendations are provided, optionally include 2–3 strong picks with distance/time (if present).
- Never invent prices, addresses, or policies. Be brief and useful.

{style}
"""

# ---- Public entrypoint -----------------------------------------------------------

def generate_reply(guest_message: str, ctx: Dict[str, Any]) -> str:
    """
    ctx may include:
      property_address, latitude, longitude,
      listing_info, reservation, history, nearby_places (from places.build_local_recs),
      guest_name, property_name, check_in, check_out, guest_count, status
    """
    # Extract facts from context
    property_address = ctx.get("property_address") \
        or ((ctx.get("listing_info") or {}).get("address") or {}).get("address1")

    check_in = ctx.get("reservation", {}).get("checkInDate") \
        or ctx.get("reservation", {}).get("checkIn") \
        or ctx.get("check_in")

    check_out = ctx.get("reservation", {}).get("checkOutDate") \
        or ctx.get("reservation", {}).get("checkOut") \
        or ctx.get("check_out")

    guest_name = ctx.get("guest_name")
    property_name = ctx.get("listing_info", {}).get("name") or ctx.get("property_name")
    guest_count = ctx.get("guest_count") or ctx.get("reservation", {}).get("numberOfGuests")
    status = ctx.get("status") or ctx.get("reservation", {}).get("status")
    nearby_places = ctx.get("nearby_places") or []

    # Compute stay phase
    phase = infer_stay_phase(check_in, check_out)

    # Distance intent
    distance = None
    missing = []
    if detect_distance_intent(guest_message):
        destination = normalize_destination(guest_message)
        if not property_address:
            missing.append("property_address")
        if not destination:
            missing.append("destination")
        if not missing:
            # Try single-destination drive time (address→place)
            distance = get_drive_distance_duration(property_address, destination)

    # Facts payload for the model
    facts: Dict[str, Any] = {
        "phase": phase,
        "property_name": property_name,
        "check_in": check_in,
        "check_out": check_out,
        "guest_name": guest_name,
        "guest_count": guest_count,
        "status": status,
    }
    if distance:
        facts["distance"] = distance
    if nearby_places:
        # Keep it light; model can pick 2–3 if useful
        facts["nearby_places"] = nearby_places[:3]

    user_payload = {
        "guest_message": guest_message,
        "facts": facts,
    }
    if missing:
        user_payload["missing"] = missing

    system = SYSTEM_TEMPLATE.format(today=datetime.now().date(), style=STYLE)
    messages = [{"role": "system", "content": system}] + _few_shots() + [
        {"role": "user", "content": json.dumps(user_payload)}
    ]

    # Call OpenAI
    try:
        if _use_client and _openai_client:
            resp = _openai_client.chat.completions.create(
                model=OPENAI_MODEL,
                messages=messages,
                temperature=0.3,
            )
            return (resp.choices[0].message.content or "").strip()
    except Exception as e:
        logging.warning(f"OpenAI call failed: {e}")

    # Fallback: minimal safe response if the model call fails
    # Use phase for a sensible default
    if phase == "post_stay":
        return "Thanks so much for staying with us—glad to hear everything went well! Safe travels home."
    return "Thanks for your message! I’ll take a look and follow up shortly."
