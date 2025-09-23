# path: ai_switch.py
from __future__ import annotations
import os
import logging
from typing import Any, Dict, Optional, Tuple

# Legacy composer (your existing path)
# Adjust the import below to match where your legacy function lives:
from utils import make_suggested_reply  # current implementation

# Smart composer (new)
SMART_AUTOREPLY = os.getenv("SMART_AUTOREPLY", "0") in {"1","true","True","yes","YES"}
SHADOW_MODE     = os.getenv("SHADOW_MODE", "1") in {"1","true","True","yes","YES"}

try:
    if SMART_AUTOREPLY:
        from assistant_core_smart import generate_autoreply  # new path
    else:
        generate_autoreply = None  # type: ignore
except Exception:
    SMART_AUTOREPLY = False
    generate_autoreply = None  # type: ignore

log = logging.getLogger(__name__)

# Optional: hook to record comparison analytics. No-op by default.
def record_shadow_event(
    conversation_id: Optional[str],
    guest_message: str,
    legacy_reply: str,
    legacy_intent: str,
    smart_reply: Optional[str],
    smart_meta: Optional[Dict[str, Any]],
) -> None:
    try:
        log.info({
            "evt": "autoreply_shadow",
            "conv_id": conversation_id,
            "legacy": {"intent": legacy_intent, "reply_len": len(legacy_reply)},
            "smart": {
                "intent": (smart_meta or {}).get("intent"),
                "urgency": (smart_meta or {}).get("urgency"),
                "confidence": ((smart_meta or {}).get("validation") or {}).get("confidence_score"),
                "reply_len": len(smart_reply or ""),
            },
        })
    except Exception:
        pass

def get_ai_reply(
    guest_message: str,
    context_for_reply: Dict[str, Any],
    *,
    history: Optional[list[dict]] = None,
    meta_for_ai: Optional[Dict[str, Any]] = None,
    conversation_id: Optional[str] = None,
) -> Tuple[str, str]:
    """
    Returns (final_reply, detected_intent).
    - Legacy-only by default.
    - If SMART_AUTOREPLY=1 and SHADOW_MODE=1: logs smart result but returns legacy.
    - If SMART_AUTOREPLY=1 and SHADOW_MODE=0: returns smart reply.
    """
    # 1) legacy path (always compute)
    legacy_reply, legacy_intent = make_suggested_reply(guest_message, context_for_reply)

    # 2) smart path (optional)
    smart_reply_text: Optional[str] = None
    smart_meta: Optional[Dict[str, Any]] = None

    if SMART_AUTOREPLY and generate_autoreply:
        try:
            merged_ctx = {**(meta_for_ai or {}), **(context_for_reply or {})}
            smart_reply_text, smart_meta = generate_autoreply(guest_message, merged_ctx, history=history or [])
        except Exception as e:
            log.warning(f"[ai_switch] smart path failed: {e}")

    # 3) shadow logging
    if SMART_AUTOREPLY and SHADOW_MODE:
        record_shadow_event(
            conversation_id=conversation_id,
            guest_message=guest_message,
            legacy_reply=legacy_reply,
            legacy_intent=legacy_intent,
            smart_reply=smart_reply_text,
            smart_meta=smart_meta,
        )

    # 4) choose output
    if SMART_AUTOREPLY and not SHADOW_MODE and smart_reply_text:
        return smart_reply_text, (smart_meta or {}).get("intent", legacy_intent)
    return legacy_reply, legacy_intent
