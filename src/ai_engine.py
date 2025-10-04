# file: src/ai_engine.py
import os
import logging
from typing import Dict, Any, List, Optional

import openai

# Environment setup
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "")
OPENAI_MODEL = os.getenv("OPENAI_MODEL", "gpt-4o-mini")

openai.api_key = OPENAI_API_KEY


# -------------------- Utility: Safe OpenAI Call --------------------
def _call_openai(prompt: str, temperature: float = 0.7) -> str:
    """Robust wrapper around OpenAI API to safely return text completions."""
    try:
        response = openai.chat.completions.create(
            model=OPENAI_MODEL,
            messages=[{"role": "user", "content": prompt}],
            temperature=temperature,
        )
        return response.choices[0].message["content"].strip()
    except Exception as e:
        logging.error(f"[AI Engine] OpenAI call failed: {e}")
        return ""


# -------------------- Generate Suggested Reply --------------------
def generate_reply(guest_message: str, context: Dict[str, Any]) -> str:
    """Main AI generation for suggested reply (keeps friendly, concise tone)."""

    listing = context.get("listing_info", {})
    guest_name = context.get("guest_name", "Guest")
    check_in = context.get("check_in_date", "")
    check_out = context.get("check_out_date", "")
    nearby = context.get("nearby_places", [])
    named_place = context.get("named_place")
    distance = context.get("distance")

    summary = summarize_conversation(context.get("history", []))
    mood = detect_guest_mood(context.get("history", []))

    prompt = f"""
You are a friendly and concise property assistant writing replies to guests staying at a short-term rental.

Guest name: {guest_name}
Guest mood (your interpretation): {mood}
Check-in: {check_in}, Check-out: {check_out}
Listing name: {listing.get('name')}
Address: {listing.get('address')}
Nearby notable places: {', '.join([p.get('name','') for p in nearby]) or 'N/A'}
Conversation so far: {summary}

Guest just said:
"{guest_message}"

Write a clear, concise, and friendly response. 
Be natural — conversational but not overly casual. 
If the guest asks for recommendations, use nearby or named places and note drive distance if available ({named_place}: {distance}).
End with a polite, helpful tone, never robotic.
    """

    reply = _call_openai(prompt, temperature=0.8)
    if not reply:
        reply = "Thanks so much for your message! Let me get that info for you right away."

    return reply


# -------------------- Summarize Conversation --------------------
def summarize_conversation(history: List[Dict[str, str]]) -> str:
    """Summarizes the full guest thread (not Slack messages)."""
    if not history:
        return "No prior messages from the guest."

    combined = "\n".join([f"{m['role'].title()}: {m['text']}" for m in history[-15:]])
    prompt = f"""
Summarize the following guest-host conversation in 2–3 sentences.
Focus on what the guest wants, any key topics, and the overall flow.

Conversation:
{combined}
    """

    summary = _call_openai(prompt, temperature=0.4)
    return summary or "Summary unavailable."


# -------------------- Detect Guest Mood --------------------
def detect_guest_mood(history: List[Dict[str, str]]) -> str:
    """Determines emotional tone of the guest from recent messages."""
    guest_msgs = [m["text"] for m in history if m["role"] == "guest"]
    if not guest_msgs:
        return "Neutral"

    recent_text = " ".join(guest_msgs[-5:])
    prompt = f"""
Determine the guest's emotional tone from their recent messages.
Examples: Calm, Curious, Excited, Frustrated, Polite, Grateful, Anxious, Upset, Neutral.

Guest messages:
{recent_text}

Return a single tone word only.
    """

    mood = _call_openai(prompt, temperature=0.3)
    return mood or "Neutral"


# -------------------- Improve Message with AI --------------------
def improve_message_with_ai(text: str, context: Dict[str, Any]) -> str:
    """Improves a manually written or AI-suggested reply."""
    guest_name = context.get("guest_name", "Guest")
    guest_message = context.get("guest_message", "")
    tone = "friendly and concise"

    prompt = f"""
You are improving a message to a rental guest.
Keep tone {tone}. Simplify and improve flow if needed.

Guest message:
"{guest_message}"

Draft reply:
"{text}"

Return the improved version only (no notes or explanations).
    """

    improved = _call_openai(prompt, temperature=0.7)
    return improved or text


# -------------------- Generate Reply with Specific Tone --------------------
def generate_reply_with_tone(guest_message: str, tone: str, base_reply: Optional[str] = None) -> str:
    """Rewrites an existing message into a specific tone (friendly/formal)."""
    base = base_reply or "Thank you for your message."

    prompt = f"""
Rewrite this message to sound more {tone}, while keeping it concise and warm.
Maintain natural, polite phrasing and avoid stiffness.

Original message:
{base}

Guest said:
{guest_message}

Rewritten message:
    """

    rewritten = _call_openai(prompt, temperature=0.7)
    return rewritten or base
