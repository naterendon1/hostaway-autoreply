# file: src/ai_engine.py
import os
import logging
from typing import Dict, Any, List, Optional
import openai

OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "")
OPENAI_MODEL = os.getenv("OPENAI_MODEL", "gpt-4o-mini")
openai.api_key = OPENAI_API_KEY

def _call_openai(prompt: str, temperature: float = 0.7) -> str:
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

def generate_reply(guest_message: str, context: Dict[str, Any]) -> str:
    listing = context.get("listing_info", {})
    guest_name = context.get("guest_name", "Guest")
    check_in = context.get("check_in_date", "")
    check_out = context.get("check_out_date", "")
    summary = summarize_conversation(context.get("conversation_history", []))
    mood = detect_guest_mood(context.get("conversation_history", []))

    prompt = f"""
You are a friendly and concise property assistant replying to a guest.

Guest: {guest_name}
Mood: {mood}
Check-in: {check_in} | Check-out: {check_out}
Listing: {listing.get('name')}
Conversation summary: {summary}

Guest says: "{guest_message}"

Reply in a clear, warm, concise way.
    """
    return _call_openai(prompt, 0.8) or "Thanks for your message! Happy to help."

def summarize_conversation(history: List[Dict[str, str]]) -> str:
    if not history:
        return "No previous messages."
    combined = "\n".join([f"{m['role']}: {m['text']}" for m in history[-10:]])
    prompt = f"Summarize this guest conversation in 2–3 lines:\n{combined}"
    return _call_openai(prompt, 0.4) or "Summary unavailable."

def detect_guest_mood(history: List[Dict[str, str]]) -> str:
    guest_msgs = [m["text"] for m in history if m["role"] == "guest"]
    if not guest_msgs:
        return "Neutral"
    prompt = f"Identify guest tone (e.g. calm, polite, frustrated) from:\n{guest_msgs[-3:]}"
    return _call_openai(prompt, 0.3) or "Neutral"

def improve_message_with_ai(text: str, context: Dict[str, Any]) -> str:
    guest_message = context.get("guest_message", "")
    prompt = f"Improve this reply for clarity and friendliness:\nGuest: {guest_message}\nDraft: {text}"
    return _call_openai(prompt, 0.7) or text

def generate_reply_with_tone(guest_message: str, tone: str, base_reply: Optional[str] = None) -> str:
    base = base_reply or "Thank you for your message."
    prompt = f"Rewrite this to sound more {tone}, keeping it concise:\n{base}\nGuest: {guest_message}"
    return _call_openai(prompt, 0.7) or base

def analyze_conversation_thread(messages: list) -> tuple:
    """Return (mood, summary)"""
    try:
        combined_text = "\n".join([m.get("text", "") for m in messages[-10:]])
        prompt = f"""
Summarize and detect tone:
{combined_text}

Respond in JSON:
{{"summary": "...", "mood": "..."}}
        """
        result = _call_openai(prompt, 0.5)
        import json
        data = json.loads(result)
        return data.get("mood", "Neutral"), data.get("summary", "No summary.")
    except Exception as e:
        logging.error(f"[AI] analyze_conversation_thread failed: {e}")
        return "Neutral", "Summary unavailable."
