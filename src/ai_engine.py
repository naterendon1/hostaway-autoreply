# file: src/ai_engine.py
import os
import logging
from typing import List, Dict, Any
from openai import OpenAI

OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
OPENAI_MODEL = os.getenv("OPENAI_MODEL", "gpt-4o-mini")
client = OpenAI(api_key=OPENAI_API_KEY) if OPENAI_API_KEY else None


# -------------------- Core Reply Generation --------------------
def generate_reply(guest_message: str, context: Dict[str, Any]) -> str:
    """Generate a friendly, concise reply to a guest message."""
    if not client:
        return "Thanks for reaching out! We'll get back to you shortly."

    sys_prompt = (
        "You are a friendly, concise assistant for a short-term rental host. "
        "Respond naturally and politely to guests. Be clear and conversational, "
        "avoid sounding robotic or overly formal."
    )

    user_prompt = f"""
Guest message:
{guest_message}

Context (may include property info, dates, etc.):
{context}

Write a helpful reply:
"""

    try:
        resp = client.chat.completions.create(
            model=OPENAI_MODEL,
            messages=[
                {"role": "system", "content": sys_prompt},
                {"role": "user", "content": user_prompt},
            ],
            temperature=0.7,
        )
        return resp.choices[0].message.content.strip()
    except Exception as e:
        logging.error(f"[ai_engine] generate_reply failed: {e}")
        return "Got it — let me double-check that for you!"


# -------------------- Improve Message --------------------
def improve_message_with_ai(text: str, context: Dict[str, Any]) -> str:
    """Improve an existing message with clarity and tone."""
    if not client:
        return text

    guest_message = context.get("guest_message", "")
    prompt = f"""
Guest said: "{guest_message}"

Your draft reply:
{text}

Improve this reply to sound more clear, concise, friendly, and natural.
Do not add greetings or sign-offs.
"""

    try:
        resp = client.chat.completions.create(
            model=OPENAI_MODEL,
            messages=[
                {"role": "system", "content": "You are an assistant editing guest replies for clarity and tone."},
                {"role": "user", "content": prompt},
            ],
            temperature=0.6,
        )
        return resp.choices[0].message.content.strip()
    except Exception as e:
        logging.error(f"[ai_engine] improve_message_with_ai failed: {e}")
        return text


# -------------------- Rewrite Tone --------------------
def rewrite_tone(text: str, tone: str = "friendly") -> str:
    """Rewrite a message to match a specific tone."""
    if not client:
        return text

    tone = tone.lower()
    sys = (
        f"You are rewriting hospitality messages. "
        f"Rewrite this text to be {tone}, natural, and professional. "
        "Keep meaning intact, remove any extra fluff or stiffness."
    )

    try:
        resp = client.chat.completions.create(
            model=OPENAI_MODEL,
            messages=[
                {"role": "system", "content": sys},
                {"role": "user", "content": text},
            ],
            temperature=0.5,
        )
        return resp.choices[0].message.content.strip()
    except Exception as e:
        logging.error(f"[ai_engine] rewrite_tone failed: {e}")
        return text


# -------------------- Generate Reply with Specific Tone --------------------
def generate_reply_with_tone(guest_message: str, tone: str, base_reply: str = None) -> str:
    """Rewrite an existing reply into a specific tone (friendly, formal, professional)."""
    if not client:
        return base_reply or "Thanks for your message!"

    sys_prompt = (
        f"You are rewriting guest replies to have a {tone} tone. "
        "Keep the response polite, natural, concise, and clear."
    )

    user_prompt = f"""
Guest message: {guest_message}

Original reply:
{base_reply or 'Thank you for reaching out!'}

Rewrite with a {tone} tone:
"""

    try:
        resp = client.chat.completions.create(
            model=OPENAI_MODEL,
            messages=[
                {"role": "system", "content": sys_prompt},
                {"role": "user", "content": user_prompt},
            ],
            temperature=0.6,
        )
        return resp.choices[0].message.content.strip()
    except Exception as e:
        logging.error(f"[ai_engine] generate_reply_with_tone failed: {e}")
        return base_reply or "Thank you for your message!"


# -------------------- Analyze Conversation Thread --------------------
dasync def analyze_conversation_thread(thread: list):
    """
    Analyze a guest conversation thread.
    Returns a tuple: (mood, summary)
    """
    try:
        prompt = (
            "Summarize the last 10–15 messages of this guest-host thread. "
            "Capture the tone (e.g., friendly, upset, neutral) and a concise summary of context."
        )
        messages = [{"role": "system", "content": prompt}]
        for msg in thread[-15:]:
            messages.append({"role": "user", "content": msg.get("message", "")})

        response = openai_client.chat.completions.create(
            model="gpt-4o-mini",
            messages=messages,
        )

        content = response.choices[0].message.content or ""
        # Extract a simple mood and summary
        if ":" in content:
            parts = content.split(":", 1)
            mood = parts[0].strip()
            summary = parts[1].strip()
        else:
            mood = "neutral"
            summary = content.strip()

        return mood, summary

    except Exception as e:
        logging.error(f"[ai_engine] analyze_conversation_thread failed: {e}")
        return "neutral", "Unable to summarize thread"
