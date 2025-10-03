# path: assistant_core_smart.py
from __future__ import annotations
import os
import re
import logging
from typing import Any, Dict, List, Optional, Tuple
from openai import OpenAI

OPENAI_MODEL = os.getenv("OPENAI_MODEL", "gpt-4o-mini")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
_client = OpenAI(api_key=OPENAI_API_KEY) if OPENAI_API_KEY else None

# ---------------------------- helpers ----------------------------

def _history_to_lines(history: List[Dict[str, Any]]) -> List[str]:
    lines: List[str] = []
    for h in history[-20:]:
        role = h.get("role") or "guest"
        txt = (h.get("text") or "").strip()
        if not txt:
            continue
        prefix = "Guest:" if role == "guest" else "Host:"
        lines.append(f"{prefix} {txt}")
    return lines

def _v(d: Dict[str, Any], *keys: str, default: Any = None) -> Any:
    cur: Any = d
    for k in keys:
        if not isinstance(cur, dict):
            return default
        cur = cur.get(k)
    return cur if cur is not None else default

def _intish(x: Any) -> Optional[int]:
    try:
        i = int(str(x).strip())
        return i
    except Exception:
        return None

def _compose_property_facts(meta_for_ai: Dict[str, Any]) -> Dict[str, Any]:
    """Collect normalized numeric facts for easy use + a friendly summary line."""
    details = _v(meta_for_ai, "property_details", default={}) or {}
    amx = _v(meta_for_ai, "amenities_index", default={}) or {}

    bedrooms = _intish(details.get("bedrooms") or amx.get("bedrooms") or _v(amx, "counts", "bedrooms"))
    beds = _intish(details.get("beds") or amx.get("beds") or _v(amx, "counts", "beds"))
    bathrooms = _intish(details.get("bathrooms") or amx.get("bathrooms") or _v(amx, "counts", "bathrooms"))
    max_guests = _intish(details.get("max_guests") or amx.get("max_guests") or _v(amx, "limits", "max_guests"))

    check_in_start = details.get("check_in_start")
    check_in_end = details.get("check_in_end")
    check_out_time = details.get("check_out_time")
    wifi_name = details.get("wifi_username") or _v(amx, "wifi", "username")
    wifi_pass = details.get("wifi_password") or _v(amx, "wifi", "password")

    summary_bits = []
    if bedrooms: summary_bits.append(f"{bedrooms} bedroom" + ("s" if bedrooms != 1 else ""))
    if beds: summary_bits.append(f"{beds} bed" + ("s" if beds != 1 else ""))
    if bathrooms: summary_bits.append(f"{bathrooms} bath" + ("s" if bathrooms != 1 else ""))
    if max_guests: summary_bits.append(f"up to {max_guests} guests")
    summary = ", ".join(summary_bits) if summary_bits else ""

    return {
        "bedrooms": bedrooms,
        "beds": beds,
        "bathrooms": bathrooms,
        "max_guests": max_guests,
        "check_in_start": check_in_start,
        "check_in_end": check_in_end,
        "check_out_time": check_out_time,
        "wifi_username": wifi_name,
        "wifi_password": wifi_pass,
        "summary": summary,
        "amenities_index": amx,
    }

_KIDS_WORDS = re.compile(r"\b(kid|kids|child|children|playground|park|play\s?area)\b", re.I)
_BED_ASK = re.compile(r"\b(bed|beds|bedroom|bedrooms)\b", re.I)
_HAS_NUMBER_NEAR_BED = re.compile(r"\b\d+\s*(bed|beds|bedroom|bedrooms)\b", re.I)
_PLACEHOLDER = re.compile(r"\[[^\]]+\]|\{[^}]+\}", re.M)
_EMPTY_BULLET = re.compile(r"(?m)^\s*[-•]\s*$")
_GENERIC_BEDS = re.compile(r"\b(has|have)\s+beds\b\.?", re.I)

def _strip_placeholders_and_empty_bullets(text: str) -> str:
    t = _PLACEHOLDER.sub("", text or "")
    # Drop empty bullets or bullets that became empty after stripping
    t = _EMPTY_BULLET.sub("", t)
    # Collapse multiple blank lines
    t = re.sub(r"\n{3,}", "\n\n", t).strip()
    return t

def _inject_concrete_facts(reply: str, guest_msg: str, facts: Dict[str, Any]) -> str:
    """If guest asked for beds/bedrooms and reply missed numbers, add a crisp sentence."""
    out = reply.strip()
    asked_beds = bool(_BED_ASK.search(guest_msg or ""))
    has_number = bool(_HAS_NUMBER_NEAR_BED.search(out))
    if asked_beds and not has_number:
        beds = facts.get("beds")
        bedrooms = facts.get("bedrooms")
        if beds and bedrooms:
            sentence = f"The home has {beds} beds across {bedrooms} bedrooms."
        elif beds:
            sentence = f"The home has {beds} beds."
        elif bedrooms:
            sentence = f"The home has {bedrooms} bedrooms."
        else:
            sentence = "I’ll confirm the exact bed setup and get right back to you."
        # Prepend if the reply already says “has beds” generically
        if GENERIC_BEDS.search(out):
            out = GENERIC_BEDS.sub(sentence, out, count=1)
        else:
            # Insert as first sentence if the reply never stated it concretely
            out = sentence + (" " if out else "") + out
    return out.strip()

def _clip_lines_no_empty_bullets(text: str) -> str:
    # Remove any bullet lines that are just "-" after other filters
    lines = [ln for ln in (text or "").splitlines() if not _EMPTY_BULLET.match(ln)]
    # Trim trailing punctuation spacing
    cleaned = "\n".join(lines).replace(" ,", ",").replace(" .", ".").strip()
    return cleaned

def _build_nearby_snippet(meta_for_ai: Dict[str, Any], guest_msg: str) -> str:
    items = _v(meta_for_ai, "nearby", "items", default=[]) or []
    if not items:
        return ""
    # Keep it short; only if guest asked kid stuff we bias to parks/playgrounds
    wants_kids = bool(_KIDS_WORDS.search(guest_msg or ""))
    picks: List[str] = []
    for it in items:
        name = (it.get("name") or "").strip()
        if not name:
            continue
        # If kids-focused, prefer items tagged as park/playground if present
        types = " ".join([str(t) for t in (it.get("types") or [])]).lower()
        if wants_kids and not any(k in types for k in ("park", "playground")):
            # still allow if we don't have enough
            pass
        approx = it.get("approx_distance") or it.get("approx_time")
        if approx:
            picks.append(f"- {name} (~{approx})")
        else:
            picks.append(f"- {name}")
        if len(picks) >= 3:
            break
    return "\n".join(picks)

# ---------------------------- main smart path ----------------------------

def make_reply_smart(
    guest_message: str,
    meta_for_ai: Dict[str, Any],
    history: Optional[List[Dict[str, Any]]] = None,
    reservation_obj: Optional[Dict[str, Any]] = None,
    listing_obj: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """
    Smart path with strict no-placeholder + amenity-first prompting and post-fix.
    Returns {reply, intent, meta_used}.
    """
    history = history or []
    reservation = (reservation_obj or {}).get("result") or reservation_obj or {}
    listing = (listing_obj or {}).get("result") or listing_obj or {}

    facts = _compose_property_facts(meta_for_ai)
    nearby_snippet = _build_nearby_snippet(meta_for_ai, guest_message)

    # System: warm, concrete, no placeholders/empty bullets, never invent facts.
    system = (
        "You are a friendly, helpful vacation rental property manager.\n"
        "Tone: warm, conversational, concise. Sound like a real human host.\n"
        "Rules:\n"
        "• Use concrete facts from the provided context (listing/reservation/amenities).\n"
        "• Never write placeholders like [insert …] or template braces {like this}.\n"
        "• Never output empty bullets. If you list items, ensure each bullet has content.\n"
        "• If a requested fact is missing from context, say you'll confirm and follow up soon—do not guess.\n"
        "• Answer all parts of the guest’s latest message in 1–3 short sentences (use bullets only if listing multiple options).\n"
        "• Be hospitable and specific; avoid corporate phrases.\n"
    )

    # Build a compact user content with the concrete facts up front.
    # Keep this brief to avoid swamping the model.
    prop_summary = facts.get("summary")
    pd = meta_for_ai.get("property_details") or {}
    lines = []
    if prop_summary:
        lines.append(f"Property summary: {prop_summary}.")
    if pd.get("check_in_start") or pd.get("check_in_end"):
        ci = f"{pd.get('check_in_start','')}-{pd.get('check_in_end','')}".strip("-")
        if ci:
            lines.append(f"Check-in window: {ci}.")
    if pd.get("check_out_time"):
        lines.append(f"Check-out: {pd.get('check_out_time')}.")
    if facts.get("wifi_username") or facts.get("wifi_password"):
        w = []
        if facts.get("wifi_username"): w.append(f"SSID: {facts['wifi_username']}")
        if facts.get("wifi_password"): w.append(f"Pass: {facts['wifi_password']}")
        lines.append("Wi-Fi " + " | ".join(w) + ".")

    if nearby_snippet:
        lines.append("Nearby highlights:\n" + nearby_snippet)

    # Conversation thread (newest last) for context
    thread_lines = _history_to_lines(history)
    thread_block = "\n".join(thread_lines) if thread_lines else ""

    # A tiny hint: if guest asks kids stuff
    extra_hint = ""
    if _KIDS_WORDS.search(guest_message or ""):
        extra_hint = "If they asked about kids activities, prefer parks/playgrounds within walking distance."

    user = (
        (f"{thread_block}\n\n" if thread_block else "")
        + ("Context facts:\n" + "\n".join(lines) + "\n\n" if lines else "")
        + "Guest's latest message:\n"
        + f"\"{(guest_message or '').strip()}\"\n\n"
        + "Write a brief, helpful reply using the facts above. "
          "Do NOT repeat the guest verbatim. "
          "No placeholders. No empty bullets. No emojis. "
          "If you don't see the exact fact, say you'll confirm.\n"
        + (extra_hint if extra_hint else "")
    )

    reply_text = ""
    if _client:
        try:
            resp = _client.chat.completions.create(
                model=OPENAI_MODEL,
                temperature=0.3,
                messages=[{"role": "system", "content": system}, {"role": "user", "content": user}],
            )
            reply_text = (resp.choices[0].message.content or "").strip()
        except Exception as e:
            logging.error(f"[smart] OpenAI error: {e}")

    if not reply_text:
        # Fallback line—still hospitable
        reply_text = "Happy to help—tell me exactly what you need and I’ll confirm details for you right away."

    # Post-process: strip placeholders/empty bullets, inject missing numbers if asked.
    reply_text = _strip_placeholders_and_empty_bullets(reply_text)
    reply_text = _inject_concrete_facts(reply_text, guest_message, facts)
    reply_text = _clip_lines_no_empty_bullets(reply_text)

    # Choose an intent to return if caller passed one; default 'other'
    intent = (meta_for_ai.get("intent") or meta_for_ai.get("detected_intent") or "other").lower()
    return {"reply": reply_text, "intent": intent, "meta_used": meta_for_ai}

def generate_autoreply(
    guest_message: str,
    context: Dict[str, Any],
    history: Optional[List[Dict[str, Any]]] = None
) -> Tuple[str, Dict[str, Any]]:
    """
    Backward-compatible wrapper.
    """
    out = make_reply_smart(guest_message, context, history=history or [])
    return out["reply"], out
