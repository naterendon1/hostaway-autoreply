# file: src/ai_engine.py
"""
AI Engine for Hostaway AutoReply
--------------------------------
Handles:
- Generating AI replies
- Summarizing guest conversations
- Detecting guest mood
- Rewriting tone
- Improving existing replies
"""

import os
import logging
from typing import Dict, Any, List, Optional
from openai import OpenAI

# ---------------------------------------------------------------------
# Environment & OpenAI Setup
# ---------------------------------------------------------------------
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "")
OPENAI_MODEL = os.getenv("OPENAI_MODEL", "gpt-4o-mini")
client = OpenAI(api_key=OPENAI_API_KEY)

# ---------------------------------------------------------------------
# Utility: Safe OpenAI Call
# ---------------------------------------------------------------------
def _call_openai(prompt: str, temperature: float = 0.7) -> str:
    """Safely call OpenAI and return a string response."""
    try:
        response = client.chat.completions.create(
            model=OPENAI_MODEL,
            messages=[{"role": "user", "content": prompt}],
            temperature=temperature,
        )
        return response.choices[0].message.content.strip()
    except Exception as e:
        logging.error(f"[AI Engine] OpenAI call failed: {e}")
        return ""

# ---------------------------------------------------------------------
# Generate AI Reply
# ---------------------------------------------------------------------
def generate_reply(guest_message: str, context: Dict[str, Any]) -> str:
    """Generates a concise, friendly AI reply."""
    listing = context.get("listing_info", {})
    guest_name = context.get("guest_name", "Guest")
    check_in = context.get("check_in_date", "")
    check_out = context.get("check_out_date", "")

    summary = summarize_conversation(context.get("conversation_history", []))
    mood = detect_guest_mood(context.get("conversation_history", []))

    prompt = f"""
You are a friendly and concise hospitality assistant responding to a guest.

Guest: {guest_name}
Check-in: {check_in}, Check-out: {check_out}
Listing: {listing.get('name')}
Address: {listing.get('address')}
Mood: {mood}
Conversation Summary: {summary}

Guest said:
"{guest_message}"

Write a helpful, natural reply that sounds warm and professional.
Avoid sounding robotic or repetitive.
"""

    reply = _call_openai(prompt, temperature=0.8)
    return reply or "Thanks for your message! I’ll check that right away."

# ---------------------------------------------------------------------
# Conversation Summarization
# ---------------------------------------------------------------------
def summarize_conversation(history: List[Dict[str, str]]) -> str:
    """Summarizes guest-host conversation in 2–3 sentences."""
    if not history:
        return "No previous messages."

    combined = "\n".join(f"{m['role'].title()}: {m['text']}" for m in history[-15:])
    prompt = f"""
Summarize the following guest-host conversation in 2–3 sentences.

{combined}
"""
    summary = _call_openai(prompt, temperature=0.4)
    return summary or "Summary unavailable."

# ---------------------------------------------------------------------
# Detect Guest Mood
# ---------------------------------------------------------------------
def detect_guest_mood(history: List[Dict[str, str]]) -> str:
    """Infers the guest’s emotional tone."""
    guest_msgs = [m["text"] for m in history if m["role"] == "guest"]
    if not guest_msgs:
        return "Neutral"

    recent_text = " ".join(guest_msgs[-5:])
    prompt = f"""
Determine the emotional tone of the following guest messages.
Examples: Calm, Curious, Excited, Frustrated, Polite, Grateful, Upset.

Messages:
{recent_text}

Return a single descriptive word only.
"""
    mood = _call_openai(prompt, temperature=0.3)
    return mood or "Neutral"

# ---------------------------------------------------------------------
# Improve Message with AI
# ---------------------------------------------------------------------
def improve_message_with_ai(text: str, context: Optional[Dict[str, Any]] = None) -> str:
    """Refines and improves a reply for clarity, tone, and style."""
    guest_message = (context or {}).get("guest_message", "")
    tone = "friendly and concise"

    prompt = f"""
You are improving a rental guest reply.
Keep it {tone}, clear, natural, and professional.

Guest message:
"{guest_message}"

Draft reply:
"{text}"

Return only the improved reply.
"""
    improved = _call_openai(prompt, temperature=0.7)
    return improved or text

# ---------------------------------------------------------------------
# Generate Reply with Specific Tone
# ---------------------------------------------------------------------
def generate_reply_with_tone(guest_message: str, tone: str, base_reply: Optional[str] = None) -> str:
    """Rewrites a message into a specific tone."""
    base = base_reply or "Thank you for your message."
    prompt = f"""
Rewrite this reply to sound more {tone}, while keeping it natural and concise.

Original reply:
{base}

Guest said:
{guest_message}

Rewritten message:
"""
    rewritten = _call_openai(prompt, temperature=0.7)
    return rewritten or base

# ---------------------------------------------------------------------
# Rewrite Tone (used by Slack)
# ---------------------------------------------------------------------
def rewrite_tone(original_text: str, tone: str) -> str:
    """
    Rewrites a message into a new tone (friendly, formal, professional, concise).
    """
    tone = tone.lower().strip()
    supported_tones = ["friendly", "formal", "professional", "concise"]
    if tone not in supported_tones:
        tone = "friendly"

    system_prompt = (
        "You are a professional hospitality AI assistant. "
        "Rewrite the message in the specified tone without changing meaning or facts. "
        "Keep it human, warm, and clear."
    )
    user_prompt = f"Rewrite the following in a {tone} tone:\n\n{original_text}"

    try:
        response = client.chat.completions.create(
            model=OPENAI_MODEL,
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            temperature=0.7,
            max_tokens=500,
        )
        return response.choices[0].message.content.strip()
    except Exception as e:
        logging.error(f"[AI Engine] rewrite_tone failed: {e}")
        return f"[{tone.capitalize()} tone] {original_text}"
