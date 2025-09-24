# path: ai/prompt_builder.py
from __future__ import annotations
from typing import Any, Dict, Iterable, List, Optional
from datetime import datetime
import json

# ──────────────────────────────────────────────────────────────────────────────
# Friendly, guest-first system prompt
# ──────────────────────────────────────────────────────────────────────────────
ENHANCED_SYSTEM_PROMPT = """
You are the friendly property manager for a vacation rental. You genuinely want guests to have an amazing stay.

TONE: Conversational, warm, helpful; never corporate. Like texting a guest you care about.
STYLE:
- Use natural contractions (you'll, we've, that's).
- Short and clear: 1–4 sentences unless instructions/steps are required.
- Confirm concrete details when relevant (bedrooms, beds, check-in/out, address fragments, amenities).
- Offer the next step concisely when action is needed (one step or a short list).
- Match property personality if provided (brand voice / quirks).

AVOID:
- Emojis or formal sign-offs.
- Corporate phrases (“per our policy”, “we apologize for any inconvenience”).
- Placeholders like “[insert …]” or “{something}”. If unknown, say what you *can* do or can check.

OUTPUT:
- A single guest-ready reply text, preserving any factual commitments in the context.
- Do not invent unprovided facts. Prefer “I can confirm that for you” over placeholders.
"""

# ──────────────────────────────────────────────────────────────────────────────
# Builders for each context section
# ──────────────────────────────────────────────────────────────────────────────

def _fmt_pair(k: str, v: Any) -> str:
    return f"{k}: {v}"

def _kv_if(d: Dict[str, Any], *keys: str) -> List[str]:
    out: List[str] = []
    for k in keys:
        v = d.get(k)
        if v not in (None, "", [], {}):
            out.append(_fmt_pair(k, v))
    return out

def build_examples_section(similar_examples: Iterable[Iterable[str]] | None) -> str:
    """Format prior Q&A examples for the prompt (max 3)."""
    if not similar_examples:
        return ""
    lines = ["Previous similar guest Q&A:"]
    for ex in list(similar_examples)[:3]:
        # Your schema: (guest_message, ai_suggestion, user_reply)
        q = str(ex[0])[:220].replace("\n", " ").strip()
        a = str(ex[2])[:220].replace("\n", " ").strip()
        lines.append(f"Q: {q}")
        lines.append(f"A: {a}")
    return "\n" + "\n".join(lines)

def build_thread_section(thread_msgs: List[str] | None) -> str:
    """Prior thread as dialogue, newest last."""
    if not thread_msgs:
        return ""
    text = "\nConversation history (newest last):\n"
    for msg in thread_msgs:
        text += f"{msg}\n"
    return text.rstrip()

def build_reservation_section(reservation: Optional[Dict[str, Any]]) -> str:
    """Key reservation fields."""
    if not reservation:
        return ""
    fields = ["guestFirstName", "arrivalDate", "departureDate", "numberOfGuests", "status"]
    parts = _kv_if(reservation, *fields)
    return ("\nReservation: " + ", ".join(parts)) if parts else ""

def build_listing_section(listing: Optional[Dict[str, Any]]) -> str:
    """
    Show listing highlights from Hostaway (fallback if amenities_index/property_details
    are not provided by the caller's meta).
    """
    if not listing:
        return ""
    result = listing.get("result", listing)  # tolerate either shape
    name = result.get("name") or result.get("externalListingName") or "Listing"
    # Prefer Hostaway canonical numeric fields when present
    hl_parts = _kv_if(
        result,
        "bedroomsNumber", "bedsNumber", "bathroomsNumber",
        "personCapacity", "roomType", "bathroomType",
        "checkInTimeStart", "checkInTimeEnd", "checkOutTime",
        "wifiUsername", "wifiPassword",
    )
    addr = result.get("address")
    addr_line = ""
    if isinstance(addr, dict):
        bits = [addr.get("address"), addr.get("city"), addr.get("state"), addr.get("zip"), addr.get("country")]
        addr_line = ", ".join([str(b).strip() for b in bits if b]) or ""
    elif isinstance(addr, str):
        addr_line = addr
    amen = result.get("listingAmenities") or result.get("amenities")
    if isinstance(amen, list) and amen:
        # Amenity list can be IDs; we pass the count to avoid hallucinating names.
        hl_parts.append(f"amenities_count={len(amen)}")
    base = f"\nListing: {name}"
    if addr_line:
        base += f"\nAddress (partial): {addr_line}"
    if hl_parts:
        base += "\nListing highlights: " + ", ".join(hl_parts)
    return base

def build_property_details_section(property_details: Optional[Dict[str, Any]]) -> str:
    if not property_details:
        return ""
    ordered = [
        "bedrooms","beds","bathrooms","max_guests","square_meters",
        "room_type","bathroom_type","check_in_start","check_in_end","check_out_time",
        "wifi_username","wifi_password","cancellation_policy",
    ]
    parts = [f"{k}: {property_details[k]}" for k in ordered if property_details.get(k) not in (None, "", [])]
    return ("\nProperty details: " + ", ".join(parts)) if parts else ""

def build_amenities_index_section(amenities_index: Optional[Dict[str, Any]]) -> str:
    """
    The normalized amenity index (from your AmenitiesIndex.to_api()).
    We serialize a concise JSON block so the model has exact fields to reference.
    """
    if not amenities_index:
        return ""
    # Keep it compact but structured
    compact = json.dumps(amenities_index, ensure_ascii=False)
    return "\nAmenities index (normalized JSON): " + compact

def build_calendar_section(calendar_summary: Optional[str]) -> str:
    return f"\nCalendar Info: {calendar_summary}" if calendar_summary else ""

def build_intent_section(intent: Optional[str]) -> str:
    return f"\nIntent: {intent or 'other'}"

def build_voice_section(core_identity: Optional[Dict[str, Any]]) -> str:
    if not isinstance(core_identity, dict):
        return ""
    voice = core_identity.get("voice") or core_identity.get("brand") or core_identity.get("tagline")
    if not voice:
        return ""
    return f"\nProperty voice: {voice}"

# ──────────────────────────────────────────────────────────────────────────────
# Public builder
# ──────────────────────────────────────────────────────────────────────────────

def build_full_prompt(
    guest_message: str,
    thread_msgs: List[str] | None,
    reservation: Dict[str, Any] | None,
    listing: Dict[str, Any] | None,
    calendar_summary: Optional[str],
    intent: Optional[str],
    similar_examples: Iterable[Iterable[str]] | None,
    meta_for_ai: Optional[Dict[str, Any]] = None,
    extra_instructions: Optional[str] = None,
) -> Dict[str, str]:
    """
    Returns a dict with:
      - system: ENHANCED_SYSTEM_PROMPT
      - user:   composed user prompt including listing+amenities+reservation context

    NOTE: This is designed to be fed to OpenAI *without* additional boilerplate.
    """
    meta = meta_for_ai or {}
    # Pull richer sources if caller provided them
    property_details = meta.get("property_details") or {}
    amenities_index = meta.get("amenities_index") or {}
    core_identity = meta.get("core_identity") or {}

    user = (
        build_examples_section(similar_examples)
        + build_thread_section(thread_msgs or [])
        + build_reservation_section(reservation or {})
        + build_property_details_section(property_details)
        + build_amenities_index_section(amenities_index)
        + build_listing_section(listing or {})
        + build_calendar_section(calendar_summary)
        + build_voice_section(core_identity)
        + build_intent_section(intent)
        + f"\n\nGuest’s latest message: \"{(guest_message or '').strip()}\"\n"
        + "---\n"
        + "Write a brief, factual, and warm reply to the most recent guest message using ALL provided context. "
          "Preserve concrete facts (beds, bedrooms, amenities, codes, times) from the context. "
          "Never output placeholders (e.g., [insert …])—if a specific fact is not present, say you'll confirm it. "
          "No emojis, no sign-offs. If a next step exists, state it clearly in one short sentence or a short list."
    )

    if extra_instructions:
        user += f"\n{extra_instructions.strip()}"

    return {"system": ENHANCED_SYSTEM_PROMPT.strip(), "user": user.strip()}
