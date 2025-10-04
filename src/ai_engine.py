# file: src/ai_engine.py
import logging
import os
from openai import OpenAI

OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
client = OpenAI(api_key=OPENAI_API_KEY) if OPENAI_API_KEY else None

def generate_reply(guest_message: str, context: dict) -> str:
    """
    Generate a helpful and concise reply for a guest message.
    """
    if not client:
        logging.warning("OpenAI API key missing; returning fallback reply.")
        return "Thanks for reaching out! Could you please clarify your request?"
    
    sys = (
        "You are an assistant for a vacation rental host. "
        "Read the guest message carefully and generate a short, friendly, and informative reply. "
        "No greetings or sign-offs. Be concise and direct."
    )

    user = (
        f"Guest message:\n{guest_message}\n\n"
        f"Context:\n{context}\n\n"
        "Reply to the guest accordingly. Output only the reply text."
    )

    try:
        resp = client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[{"role":"system","content":sys},{"role":"user","content":user}],
            temperature=0.4,
        )
        return (resp.choices[0].message.content or "").strip()
    except Exception as e:
        logging.error(f"[ai_engine] Error generating reply: {e}")
        return "Got it! Let me check and get back to you shortly."


def improve_message_with_ai(message: str, guest_message: str, tone: str = "friendly") -> str:
    """
    Improve or adjust tone of an existing message.
    """
    if not client:
        return message

    sys = (
        f"You are an AI editor for hospitality messages. Rewrite the message to be {tone}, "
        "concise, clear, and polite. Do not add greetings or sign-offs."
    )
    user = f"Guest message: {guest_message}\n\nOriginal reply:\n{message}"

    try:
        resp = client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[{"role":"system","content":sys},{"role":"user","content":user}],
            temperature=0.4,
        )
        return (resp.choices[0].message.content or "").strip()
    except Exception as e:
        logging.error(f"[ai_engine] improve_message_with_ai error: {e}")
        return message


def rewrite_tone(text: str, tone: str = "friendly") -> str:
    """
    Lightly rephrase a message to adjust tone.
    """
    if not client:
        return text
    sys = (
        f"You rewrite text to be {tone}, natural, and professional but warm. "
        "Do not add new content or greetings."
    )
    user = text
    try:
        resp = client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[{"role":"system","content":sys},{"role":"user","content":user}],
            temperature=0.3,
        )
        return (resp.choices[0].message.content or "").strip()
    except Exception as e:
        logging.error(f"[ai_engine] rewrite_tone error: {e}")
        return text


def analyze_conversation_thread(messages: list) -> dict:
    """
    Analyze a list of conversation messages and summarize key context.
    Used by message_handler.py.
    """
    if not client:
        return {
            "summary": "No AI available",
            "sentiment": "neutral",
            "topics": [],
        }

    sys = (
        "You are an assistant analyzing a guest-host message thread. "
        "Summarize the guest's main concerns or requests in 1-2 sentences, "
        "detect overall sentiment (positive, neutral, or negative), "
        "and list key topics as short keywords. "
        "Respond in JSON only with fields: summary, sentiment, topics."
    )
    text_blob = "\n\n".join(
        f"{m.get('sender','guest')}: {m.get('text','')}" for m in messages if isinstance(m, dict)
    )
    user = f"Conversation thread:\n{text_blob}\n\nReturn JSON only."

    try:
        resp = client.chat.completions.create(
            model="gpt-4o-mini",
            response_format={"type":"json_object"},
            messages=[{"role":"system","content":sys},{"role":"user","content":user}],
            temperature=0,
        )
        return resp.choices[0].message.parsed or {}
    except Exception as e:
        logging.error(f"[ai_engine] analyze_conversation_thread error: {e}")
        return {
            "summary": "Unable to analyze conversation.",
            "sentiment": "neutral",
            "topics": [],
        }
