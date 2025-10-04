# file: src/ai_engine.py
"""
AI Engine for Hostaway AutoReply
--------------------------------
Responsible for:
- Generating AI guest replies
- Summarizing entire conversation threads (from Hostaway, not Slack)
- Detecting guest mood
- Improving user-written or AI-suggested messages
- Rewriting tone (friendly, formal, professional)
"""

import os
import logging
import json
from typing import Dict, List, Optional, Tuple
from openai import OpenAI

# Initialize OpenAI client
client = OpenAI(api_key=os.getenv("OPENAI_API_KEY"))
MODEL = os.getenv("OPENAI_MODEL", "gpt-4o-mini")

DEFAULT_TONE = "clear, concise, informal and friendly"

# ---------------------------------------------------------------------
# Utility helpers
# ---------------------------------------------------------------------
def _format_history_for_prompt(messages: List[Dict[str, str]]) -> str:
    """Formats the last 10–15 messages into a readable string for the AI context."""
    history_str = ""
    for msg in messages[-15:]:
        role = msg.get("role", "guest").capitalize()
        text = msg.get("text", "").strip()
        if text:
            history_str += f"\n{role}: {text}"
    return history_str.strip()


def _call_openai(prompt: str, system: str) -> str:
    """Wrapper for OpenAI API call with consistent parameters and logging."""
    try:
        response = client.chat.completions.create(
            model=MODEL,
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": prompt},
            ],
            temperature=0.7,
            max_tokens=500,
        )
        return response.choices[0].message.content.strip()
    except Exception as e:
        logging.error(f"[AI_ENGINE] OpenAI call failed: {e}")
        return ""


# ---------------------------------------------------------------------
# Core AI functions
# ---------------------------------------------------------------------
def summarize_conversation(history: List[Dict[str, str]]) -> str:
    """Summarize full recent guest thread (not Slack conversation)."""
    if not history:
        return "No conversation history available."

    history_text = _format_history_for_prompt(history)
    system = (
        "You are a helpful assistant for a hospitality company. "
        "Summarize the guest's full conversation so far in 1–2 short sentences. "
        "Focus on key requests, issues, or questions, not the host's responses."
    )

    prompt = f"Conversation thread:\n{history_text}\n\nProvide a short, clear summary:"
    return _call_openai(prompt, system)


def detect_guest_mood(history: List[Dict[str, str]]) -> str:
    """Determine the guest's mood using message tone and phrasing."""
    if not history:
        return "Neutral"

    guest_msgs = [m["text"] for m in history if m.get("role") == "guest"]
    if not guest_msgs:
        return "Neutral"

    text = "\n".join(guest_msgs[-5:])
    system = (
        "You are an emotion analysis model for hospitality communication. "
        "Determine the guest's mood based on tone, punctuation, and phrasing. "
        "Choose one of: Happy, Excited, Curious, Neutral, Confused, Frustrated, Upset, Angry."
    )

    prompt = f"Guest messages:\n{text}\n\nMood:"
    return _call_openai(prompt, system)


def generate_reply(guest_message: str, context: Dict[str, any]) -> str:
    """Generate a natural, helpful response to the guest message."""
    listing = context.get("listing_info", {})
    reservation = context.get("reservation", {})
    nearby = context.get("nearby_places", [])
    guest_name = context.get("guest_name", "Guest")

    # Structured context for the AI
    system = (
        f"You are an expert vacation rental assistant that responds in a {DEFAULT_TONE} tone. "
        "You must always be polite, helpful, and natural-sounding, like a human host. "
        "Use the data provided, such as check-in time, amenities, and local attractions."
    )

    prompt = f"""
Guest name: {guest_name}
Message: {guest_message}

Listing info: {json.dumps(listing, indent=2)}
Reservation: {json.dumps(reservation, indent=2)}
Nearby recommendations: {json.dumps(nearby, indent=2)}

Please craft a short and friendly message back to the guest:
"""

    return _call_openai(prompt, system)


def improve_message_with_ai(user_input: str, original_suggestion: Optional[str] = None, context: Optional[Dict] = None) -> str:
    """
    Takes user's manual message or prompt and improves it for flow, tone, and readability.
    If `original_suggestion` is provided, AI uses it as a reference.
    """
    context_text = f"Context: {json.dumps(context or {}, indent=2)}" if context else ""
    system = (
        f"You are a writing enhancement assistant specializing in hospitality. "
        f"Rewrite the user's message to sound {DEFAULT_TONE}. Maintain the same meaning."
    )

    prompt = f"""
User message (or edit): {user_input}
Original suggestion: {original_suggestion or 'N/A'}
{context_text}

Return only the improved message.
"""
    return _call_openai(prompt, system)


def rewrite_tone(message: str, tone: str) -> str:
    """Rewrites a message with the requested tone (Friendly, Formal, Professional)."""
    tone = tone.lower()
    tone_desc = {
        "friendly": "warm, conversational, upbeat",
        "formal": "polite, professional, structured",
        "professional": "direct, courteous, efficient"
    }.get(tone, DEFAULT_TONE)

    system = (
        f"You are a tone rewriter. Rewrite the following message to match this style: {tone_desc}. "
        f"Keep all important details the same."
    )

    prompt = f"Message:\n{message}\n\nRewritten ({tone_desc} tone):"
    return _call_openai(prompt, system)


# ---------------------------------------------------------------------
# Combined workflow
# ---------------------------------------------------------------------
def process_conversation(guest_message: str, history: List[Dict[str, str]], context: Dict[str, any]) -> Dict[str, str]:
    """
    High-level pipeline: summarizes conversation, detects mood, and generates reply.
    Returns all components for use in Slack header and suggested response.
    """
    summary = summarize_conversation(history)
    mood = detect_guest_mood(history)
    reply = generate_reply(guest_message, context)

    return {
        "suggested_reply": reply,
        "summary": summary,
        "mood": mood
    }
