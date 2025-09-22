# path: assistant_core_smart.py
from __future__ import annotations

"""
assistant_core_smart.py
Thin wrapper around smart_intel.make_reply_smart for drop-in adoption.

Usage:
    from assistant_core_smart import generate_autoreply
    reply_text, meta = generate_autoreply(guest_message, context, history)

Contract:
    - Returns (reply_text: str, meta: dict)
    - No I/O, no DB, no Slack/Hostaway calls
    - Falls back to heuristics if OpenAI isn't configured (handled in smart_intel)
"""

from typing import Any, Dict, List, Optional, Tuple

from smart_intel import make_reply_smart

__all__ = ["generate_autoreply"]

History = List[Dict[str, Any]]  # terse alias for readability


def generate_autoreply(
    guest_message: str,
    context: Dict[str, Any],
    history: Optional[History] = None,
) -> Tuple[str, Dict[str, Any]]:
    """
    Drop-in wrapper. Use this in routes instead of your current composer to enable the new behavior.

    Returns:
        tuple[str, dict]: (reply_text, meta) — existing call sites can keep using the first element.
    """
    out = make_reply_smart(guest_message, context, history=history or [])
    return out["reply"], out
