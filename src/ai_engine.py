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
def analyze_conversation_thread(messages: List[Dict[str, str]]) -> Dict[str, Any]:
    """Summarize a guest-host conversation and detect mood."""
    if not client:
        return {"summary": "AI unavailable", "sentiment": "neutral", "topics": []}

    conversation_text = "\n".join(
        [f"{m.get('role', 'guest')}: {m.get('text', '')}" for m in messages if m.get("text")]
    )

    sys_prompt = (
        "You are analyzing guest-host chat threads for a vacation rental. "
        "Summarize what the guest wants in 1–2 sentences, infer their mood "
        "(e.g. polite, happy, confused, upset), and extract main topics. "
        "Return JSON with fields: summary, sentiment, topics."
    )

    try:
        resp = client.chat.completions.create(
            model=OPENAI_MODEL,
            response_format={"type": "json_object"},
            messages=[
                {"role": "system", "content": sys_prompt},
                {"role": "user", "content": conversation_text},
            ],
            temperature=0,
        )
        return resp.choices[0].message.parsed or {}
    except Exception as e:
        logging.error(f"[ai_engine] analyze_conversation_thread failed: {e}")
        return {"summary": "Unable to analyze.", "sentiment": "neutral", "topics": []}
