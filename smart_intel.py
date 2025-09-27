from __future__ import annotations
import os
import logging
from typing import Any, Dict
from datetime import datetime
import re

try:
    from openai import OpenAI
except ImportError:
    OpenAI = None  # type: ignore

log = logging.getLogger(__name__)

# --- Config ---
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "")
OPENAI_MODEL = os.getenv("OPENAI_MODEL", "gpt-4o")

# --- Main reply entry point ---

def generate_reply(message: str, context: Dict[str, Any]) -> str:
    """
    Generate a warm, helpful reply using GPT-4o and listing context.
    """
    if not message.strip():
        return "Could you share a bit more so I can help?"

    # Compose full prompt
    prompt = _compose_prompt(message, context)

    # Run OpenAI
    try:
        client = OpenAI(api_key=OPENAI_API_KEY)
        response = client.chat.completions.create(
            model=OPENAI_MODEL,
            temperature=0.6,
            messages=[
                {"role": "system", "content": _system_prompt()},
                {"role": "user", "content": prompt},
            ],
        )
        text = (response.choices[0].message.content or "").strip()
        return _finalize_reply(text)
    except Exception as e:
        log.warning(f"[generate_reply] OpenAI error: {e}")
        return "Thanks for reaching out! Let me know how I can help."

# --- Prompt building ---

def _system_prompt() -> str:
    return (
        "You're a helpful, friendly assistant for a vacation rental host. "
        "Your job is to reply kindly, clearly, and informatively to guest questions. "
        "Be warm, brief, and personal. If you don’t know something, offer to find out."
    )

def _compose_prompt(message: str, context: Dict[str, Any]) -> str:
    guest_name = context.get("guest_name") or "the guest"
    check_in = context.get("check_in_date") or "N/A"
    check_out = context.get("check_out_date") or "N/A"
    listing = context.get("listing_info", {})
    location = listing.get("location", "an amazing place")
    beds = listing.get("beds", "some comfy beds")
    highlights = listing.get("highlights", "")
    activities = listing.get("activities", "")

    return (
        f"Guest message:\n{message.strip()}\n\n"
        f"Guest name: {guest_name}\n"
        f"Check-in: {check_in}, Check-out: {check_out}\n"
        f"Location: {location}\n"
        f"Beds: {beds}\n"
        f"Highlights: {highlights}\n"
        f"Activities nearby: {activities}\n\n"
        "Write a friendly, helpful reply."
    )

# --- Final cleanup ---

def _finalize_reply(text: str) -> str:
    text = _strip_placeholders(text)
    if not re.match(r"^(hi|hey|hello)\b", text, re.I):
        text = "Hey! " + text
    if not text.endswith((".", "!", "?")):
        text += "."
    return text

def _strip_placeholders(text: str) -> str:
    text = re.sub(r"\[[^\]]+\]", "", text)
    text = re.sub(r"\{[^}]+\}", "", text)
    return re.sub(r"\s{2,}", " ", text).strip()
