from __future__ import annotations
import os
import re
import json
import logging
from typing import Any, Dict, List, Optional
from datetime import datetime, date

try:
    from openai import OpenAI
except ImportError:
    OpenAI = None  # type: ignore

log = logging.getLogger(__name__)

# ---- Configuration ----
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "")
OPENAI_MODEL = os.getenv("OPENAI_MODEL", "gpt-4o")
USE_LEARNING = True  # Turn off if you don't want saved answers

# ---- Utilities ----

def _today() -> date:
    return datetime.utcnow().date()

def _norm(text: str) -> str:
    return re.sub(r"\s+", " ", text.strip().lower())

def _strip_placeholders(text: str) -> str:
    text = re.sub(r"\[[^\]]+\]", "", text)
    text = re.sub(r"\{[^}]+\}", "", text)
    return re.sub(r"\s{2,}", " ", text).strip()

# ---- Main Reply Generator ----

def generate_reply(message: str, context: Dict[str, Any]) -> str:
    """
    Main function to generate a reply to a guest message.
    Uses OpenAI GPT-4o with fallback logic and optional learning.
    """
    message = message.strip()
    if not message:
        return "Can you share a bit more? I’ll help however I can."

    # 1. Check for saved answer (learning memory)
    if USE_LEARNING:
        saved = _check_memory(message, context)
        if saved:
            return _finalize_reply(saved, context)

    # 2. Try OpenAI draft
    ai_draft = _llm_reply(message, context)
    if ai_draft:
        return _finalize_reply(ai_draft, context)

    # 3. Fallback reply
    return _finalize_reply("Happy to help — just let me know what you need!", context)

# ---- OpenAI Call ----

def _llm_reply(message: str, context: Dict[str, Any]) -> str:
    if not (OpenAI and OPENAI_API_KEY):
        return ""
    try:
        client = OpenAI(api_key=OPENAI_API_KEY)
        system_prompt = _load_prompt("system_reply.txt")
        user_payload = _build_prompt_payload(message, context)
        response = client.chat.completions.create(
            model=OPENAI_MODEL,
            temperature=0.2,
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_payload},
            ],
        )
        return (response.choices[0].message.content or "").strip()
    except Exception as e:
        log.warning(f"[llm_reply_error] {e}")
        return ""

# ---- Prompt Construction ----

def _build_prompt_payload(message: str, context: Dict[str, Any]) -> str:
    payload = {
        "message": message,
        "guest_name": context.get("guest_name"),
        "check_in": context.get("check_in_date"),
        "check_out": context.get("check_out_date"),
        "listing_info": context.get("listing_info", {}),
        "reservation": context.get("reservation", {}),
        "history": context.get("history", []),
    }
    return json.dumps(payload, ensure_ascii=False, indent=2)

def _load_prompt(filename: str) -> str:
    path = os.path.join("prompts", filename)
    try:
        with open(path, "r", encoding="utf-8") as f:
            return f.read()
    except Exception:
        return "You are a helpful assistant for vacation rental guests. Be friendly, brief, and accurate."

# ---- Learning Memory Stub ----

def _check_memory(message: str, context: Dict[str, Any]) -> Optional[str]:
    """
    TODO: Implement your saved-reply logic here (from SQLite or JSON)
    Return a saved string reply if a match is found.
    """
    return None  # Placeholder for now

# ---- Final Touches ----

def _finalize_reply(text: str, context: Dict[str, Any]) -> str:
    """
    Cleans up and formats the reply text before returning.
    """
    if not text:
        return "Let me know how I can help!"
    text = _strip_placeholders(text)
    if not re.match(r"^(hi|hey|hello)\b", text, re.I):
        text = "Hey! " + text
    if not re.search(r"[.!?]$", text):
        text += "."
    return text
