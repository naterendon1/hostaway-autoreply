# file: smart_intel.py

import os
import json
import logging
from datetime import datetime, timezone
from typing import Dict, Any, Optional, Tuple, List, Union

try:
    from openai import OpenAI
    _client = OpenAI(api_key=os.getenv("OPENAI_API_KEY"))
    _has_client = True
except Exception:
    _client = None
    _has_client = False

OPENAI_MODEL = os.getenv("OPENAI_MODEL", "gpt-4o-mini")

# Generic place + distance helpers (no hardcoded venues)
from places import (
    text_search_place,                   # NEW: resolves any free-text destination
    get_drive_distance_duration,         # accepts address or (lat,lng) for origin/dest
)

# ---------- Utilities ----------

def _parse_iso_date(s: Optional[str]):
    if not s:
        return None
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00"))
    except Exception:
        return None

def _infer_phase(check_in: Optional[str], check_out: Optional[str], now: Optional[datetime] = None) -> str:
    """
    pre_arrival | in_stay | post_stay | unknown
    """
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

def _pick_origin(ctx: Dict[str, Any]) -> Optional[Union[str, Tuple[float, float]]]:
    """
    Choose best available origin: address string, else (lat,lng), else None.
    """
    addr = ctx.get("property_address") \
        or ((ctx.get("listing_info") or {}).get("address") or {}).get("address1")
    lat = ctx.get("latitude"); lng = ctx.get("longitude")
    if addr:
        return addr
    if isinstance(lat, (int, float)) and isinstance(lng, (int, float)):
        return (float(lat), float(lng))
    return None

def _collect_core_facts(ctx: Dict[str, Any]) -> Dict[str, Any]:
    """
    Lift everything the writer might use; do not assume any specific Hostaway schema.
    """
    reservation = ctx.get("reservation") or {}
    listing = ctx.get("listing_info") or {}
    address1 = (listing.get("address") or {}).get("address1") or ctx.get("property_address")

    check_in = reservation.get("checkInDate") or reservation.get("checkIn") or ctx.get("check_in")
    check_out = reservation.get("checkOutDate") or reservation.get("checkOut") or ctx.get("check_out")

    # common listing facts (best-effort)
    def _num(x):
        try: return int(float(x))
        except Exception: return None

    facts = {
        "guest_name": ctx.get("guest_name"),
        "property_name": listing.get("name") or ctx.get("property_name"),
        "property_address": address1,
        "latitude": ctx.get("latitude"),
        "longitude": ctx.get("longitude"),
        "check_in": check_in,
        "check_out": check_out,
        "guest_count": ctx.get("guest_count") or reservation.get("numberOfGuests"),
        "status": ctx.get("status") or reservation.get("status"),
        "beds": _num(listing.get("beds") or listing.get("numberOfBeds") or listing.get("bedCount")),
        "bedrooms": _num(listing.get("bedrooms") or listing.get("numberOfBedrooms") or listing.get("bedroomsNumber")),
        "bathrooms": _num(listing.get("bathrooms") or listing.get("numberOfBathrooms") or listing.get("bathroomsNumber")),
        "city": (listing.get("address") or {}).get("city") or ctx.get("city"),
        "state": (listing.get("address") or {}).get("state") or (listing.get("address") or {}).get("province") or ctx.get("state"),
        "nearby_places": (ctx.get("nearby_places") or [])[:3],  # compact bundle if present
    }
    facts["phase"] = _infer_phase(check_in, check_out)
    return facts

# ---------- Prompts ----------

PLANNER_SYSTEM = """You are a planner that extracts user intent and entities from a guest message for a short-term rental host.
Return ONLY valid JSON with these top-level keys:

{
  "wants_distance": boolean,
  "destinations": [ {"text": string} ],           // zero or more free-text destination names found in the message
  "wants_recommendations": boolean,               // e.g., local things to do, kid-friendly, restaurants, etc.
  "info_questions": [ "bedrooms" | "bathrooms" | "beds" | "check_in" | "check_out" | "guest_count" | "address" ],
  "clarifications": [ string ]                    // at most 1 concise clarification if the message is ambiguous
}

Rules:
- Do not guess. If the user asked "how far", include "wants_distance": true and put what they wrote into "destinations" (even if vague like "downtown").
- If they ask about the home (e.g., bedrooms), add a matching token to "info_questions".
- If they ask for nearby activities, set "wants_recommendations": true.
- If nothing is ambiguous, "clarifications" should be [].
"""

# --- find WRITER_SYSTEM in smart_intel.py and replace it with this ---
WRITER_SYSTEM = """You are an expert guest-messaging assistant for short-term rentals.

Use the provided facts exactly. Be brief, warm, and helpful:
- 1–3 sentences. At most one short follow-up question if essential.
- Respect stay phase (pre_arrival, in_stay, post_stay). If post_stay, use past tense and thank them.
- If distance facts exist, include miles and minutes succinctly.
- If bedrooms/bathrooms/beds exist and the guest asked, answer directly.
- If local recommendations are provided, suggest up to 2–3 good picks, including minutes if present.
- Never invent prices, addresses, or policies.
- If a critical fact is missing and no distance was computed, ask for exactly that one piece.
- No greetings, no sign-offs, no emojis.

Output only the final message to the guest (no JSON)."""


# ---------- Public entrypoint ----------

def generate_reply(guest_message: str, ctx: Dict[str, Any]) -> str:
    """
    Two-pass flow:
      1) Ask the model to plan: extract intents and destination strings (no hardcoding).
      2) Resolve destinations via Google (text search + distance) using address or (lat,lng).
      3) Ask the model to write the final reply using all computed facts.
    """
    core = _collect_core_facts(ctx)

    # ----- Pass 1: Planner -----
    planner_input = {
        "guest_message": guest_message,
        "available_facts": {k: v for k, v in core.items() if v is not None}
    }
    plan = {
        "wants_distance": False,
        "destinations": [],
        "wants_recommendations": False,
        "info_questions": [],
        "clarifications": [],
    }

    try:
        if _has_client and _client:
            p = _client.chat.completions.create(
                model=OPENAI_MODEL,
                temperature=0.0,
                messages=[
                    {"role": "system", "content": PLANNER_SYSTEM},
                    {"role": "user", "content": json.dumps(planner_input)}
                ],
                response_format={"type": "json_object"},
            )
            plan = json.loads(p.choices[0].message.content or "{}")
    except Exception as e:
        logging.warning(f"Planner call failed, falling back: {e}")

    # ----- Resolve destinations (generic) -----
    distances: List[Dict[str, Any]] = []
    origin = _pick_origin(ctx)

    if plan.get("wants_distance") and origin is not None:
        dest_items = plan.get("destinations") or []
        # Use city/state to bias search if present
        bias_lat = core.get("latitude") if isinstance(core.get("latitude"), (int, float)) else None
        bias_lng = core.get("longitude") if isinstance(core.get("longitude"), (int, float)) else None
        city = core.get("city"); state = core.get("state")

        for d in dest_items:
            dest_text = (d or {}).get("text")
            if not dest_text:
                continue
            # 1) Resolve free-text destination to a place (lat/lng + name/address)
            place = text_search_place(
                query=dest_text,
                bias_lat=bias_lat,
                bias_lng=bias_lng,
                city=city,
                state=state,
            )
            if not place:
                continue

            # 2) Compute distance using best available origin
            try:
                if isinstance(origin, tuple):
                    dist = get_drive_distance_duration(origin, (place["lat"], place["lng"]))
                else:
                    # origin is an address string
                    dist = get_drive_distance_duration(origin, place["formatted_address"] or place["name"])
            except Exception as e:
                logging.warning(f"Distance calc failed for {dest_text}: {e}")
                dist = None

            if dist:
                distances.append({
                    "to_name": place["name"],
                    "to_address": place["formatted_address"],
                    "distance_text": dist.get("distance_text"),
                    "duration_text": dist.get("duration_text"),
                })

    # ----- Build writer facts -----
    writer_facts = {k: v for k, v in core.items() if v is not None}
    if distances:
        writer_facts["distances"] = distances

    # If user asked bedrooms/baths/beds, keep those visible
    asked = set(plan.get("info_questions") or [])
    if "bedrooms" in asked and core.get("bedrooms") is None:
        writer_facts["missing_bedrooms"] = True
    if "bathrooms" in asked and core.get("bathrooms") is None:
        writer_facts["missing_bathrooms"] = True
    if "beds" in asked and core.get("beds") is None:
        writer_facts["missing_beds"] = True

    # If they want recommendations and you computed nearby bundle earlier
    if plan.get("wants_recommendations") and core.get("nearby_places"):
        writer_facts["nearby_places"] = core["nearby_places"]

    # If planner thought a single clarification is essential and we still have zero distance results,
    # allow the writer to ask for that one missing item.
    clarifications = plan.get("clarifications") or []
    if clarifications and not distances:
        writer_facts["planner_clarification"] = clarifications[0]

    # ----- Pass 2: Writer -----
    writer_input = {
        "guest_message": guest_message,
        "facts": writer_facts
    }

    try:
        if _has_client and _client:
            r = _client.chat.completions.create(
                model=OPENAI_MODEL,
                temperature=0.3,
                messages=[
                    {"role": "system", "content": WRITER_SYSTEM},
                    {"role": "user", "content": json.dumps(writer_input)}
                ],
            )
            return (r.choices[0].message.content or "").strip()
    except Exception as e:
        logging.warning(f"Writer call failed: {e}")

    # Safe fallback
    if core.get("phase") == "post_stay":
        return "Thanks so much for staying with us—glad to hear everything went well! Safe travels home."
    return "Thanks for your message! I’ll take a look and follow up shortly."
