# path: assistant_core_smart.py
from __future__ import annotations
import os
import logging
from typing import Any, Dict, List, Optional, Tuple
from openai import OpenAI

# New prompt builder (friendly host tone + amenities awareness)
from ai.prompt_builder import build_full_prompt

OPENAI_MODEL = os.getenv("OPENAI_MODEL", "gpt-4o-mini")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
_client = OpenAI(api_key=OPENAI_API_KEY) if OPENAI_API_KEY else None

def _history_to_lines(history: List[Dict[str, Any]]) -> List[str]:
    """
    Convert your history items [{role: 'guest'|'host', 'text': '...'}] into short lines.
    Newest last.
    """
    lines: List[str] = []
    for h in history[-20:]:
        role = h.get("role") or "guest"
        txt = (h.get("text") or "").strip()
        if not txt:
            continue
        prefix = "Guest:" if role == "guest" else "Host:"
        lines.append(f"{prefix} {txt}")
    return lines

def _safe_intent(meta: Dict[str, Any]) -> str:
    intent = (meta.get("intent") or meta.get("detected_intent") or "").strip().lower()
    return intent or "other"

def make_reply_smart(
    guest_message: str,
    meta_for_ai: Dict[str, Any],
    history: Optional[List[Dict[str, Any]]] = None,
    reservation_obj: Optional[Dict[str, Any]] = None,
    listing_obj: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """
    Smart path: builds a rich prompt (listing amenities + property details),
    drafts with OpenAI, and returns a structured result.
    """
    history = history or []

    # Pull optional raw objects if the caller passed them; otherwise expect meta to contain details
    reservation = (reservation_obj or {}).get("result") or reservation_obj or {}
    listing = (listing_obj or {}).get("result") or listing_obj or {}

    # Thread lines for the prompt
    thread_lines = _history_to_lines(history)

    # Build prompt with your new prompt builder
    prompt = build_full_prompt(
        guest_message=guest_message,
        thread_msgs=thread_lines,
        reservation=reservation,
        listing=listing,
        calendar_summary=None,  # add if you track a calendar summary
        intent=_safe_intent(meta_for_ai),
        similar_examples=None,  # or your learned examples
        meta_for_ai=meta_for_ai,
        extra_instructions=None,
    )

    reply_text = ""
    if _client:
        try:
            resp = _client.chat.completions.create(
                model=OPENAI_MODEL,
                temperature=0.3,
                messages=[
                    {"role": "system", "content": prompt["system"]},
                    {"role": "user", "content": prompt["user"]},
                ],
            )
            reply_text = (resp.choices[0].message.content or "").strip()
        except Exception as e:
            logging.error(f"[smart] OpenAI error: {e}")

    # Fallback if API unavailable
    if not reply_text:
        reply_text = "Happy to help—tell me exactly what you need and I’ll confirm details for you right away."

    # Choose an intent to return (meta may have been set by caller)
    intent = _safe_intent(meta_for_ai)
    return {"reply": reply_text, "intent": intent, "meta_used": meta_for_ai}

def generate_autoreply(
    guest_message: str,
    context: Dict[str, Any],
    history: Optional[List[Dict[str, Any]]] = None
) -> Tuple[str, Dict[str, Any]]:
    """
    Backward-compatible wrapper if some code imports this symbol.
    """
    out = make_reply_smart(guest_message, context, history=history or [])
    return out["reply"], out
