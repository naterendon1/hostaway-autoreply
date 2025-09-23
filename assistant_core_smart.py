# path: assistant_core_smart.py
from __future__ import annotations
from typing import Any, Dict, List, Optional, Tuple
from smart_intel import make_reply_smart

__all__ = ["generate_autoreply"]
History = List[Dict[str, Any]]

def generate_autoreply(
    guest_message: str,
    context: Dict[str, Any],
    history: Optional[History] = None,
) -> Tuple[str, Dict[str, Any]]:
    out = make_reply_smart(guest_message, context, history=history or [])
    return out["reply"], out
