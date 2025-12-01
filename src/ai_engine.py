# file: src/ai_engine.py
import os
import logging
from typing import List, Dict, Any
from openai import OpenAI
from openai import AsyncOpenAI
import os

openai_client = AsyncOpenAI(api_key=os.getenv("OPENAI_API_KEY"))

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
def improve_message_with_ai(
    text: str,
    user_instructions: str,
    context: Dict[str, Any]
) -> str:
    """
    Improve an existing message based on specific user instructions.

    Args:
        text: The draft reply to improve
        user_instructions: What the user wants changed (tone, content, style, complete rewrite, etc.)
        context: Additional context like guest_message, conversation_thread, property_info, etc.

    Returns:
        The improved message based on user instructions
    """
    if not client:
        return text

    # Build conversation context if available
    guest_message = context.get("guest_message", "")
    conversation_thread = context.get("conversation_thread", [])

    # Format conversation history if it exists (last 5 messages for context)
    thread_text = ""
    if conversation_thread and len(conversation_thread) > 0:
        thread_text = "Previous conversation:\n" + "\n".join(
            [f"{m.get('sender', 'Guest')}: {m.get('body', '')}"
             for m in conversation_thread[-5:]]
        ) + "\n\n"

    system_prompt = """You are an expert assistant helping hosts craft perfect guest replies.

Your job is to take a draft message and modify it EXACTLY as the user requests. The user knows what they want - follow their instructions precisely.

Common requests might include:
- Changing the tone (more formal, casual, friendly, professional, apologetic, etc.)
- Adding or removing specific information
- Making it shorter or more detailed
- Completely rewriting with different approach
- Fixing factual errors or wrong assumptions
- Adjusting formality level
- Adding or removing emojis
- Being more direct or more diplomatic

Always preserve important details like dates, prices, check-in times, and policies unless specifically asked to change them.
Keep the message natural and conversational unless asked otherwise."""

    user_prompt = f"""{thread_text}Most recent guest message:
"{guest_message}"

Current draft reply:
{text}

USER'S IMPROVEMENT REQUEST:
{user_instructions}

Please rewrite the reply following the user's instructions exactly:"""

    try:
        resp = client.chat.completions.create(
            model=OPENAI_MODEL,
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            temperature=0.7,  # Slightly higher for more creative rewrites
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
async def analyze_conversation_thread(thread: list):
    """Analyzes a guest conversation and returns (mood, summary)."""
    try:
        # Build a structured conversation transcript
        conversation_text = "\n".join(
            [f"{m.get('sender', 'Guest')}: {m.get('body', '')}" for m in thread]
        )

        # 🔹 Await the async call correctly
        response = await openai_client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[
                {"role": "system", "content": "You are an assistant analyzing Airbnb guest conversations."},
                {"role": "user", "content": f"Analyze this conversation and provide:\n1. The guest's mood (e.g., happy, confused, frustrated).\n2. A short summary.\n\nConversation:\n{conversation_text}"}
            ],
            max_tokens=300,
        )

        # Extract the model response text safely
        text = response.choices[0].message.content.strip()

        # Very simple split logic
        mood, summary = "Neutral", text
        if "Mood:" in text and "Summary:" in text:
            try:
                mood = text.split("Mood:")[1].split("Summary:")[0].strip()
                summary = text.split("Summary:")[1].strip()
            except Exception:
                pass

        return mood, summary

    except Exception as e:
        logging.error(f"[ai_engine] analyze_conversation_thread failed: {e}")
        return "Neutral", "No summary available."
