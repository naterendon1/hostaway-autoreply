# path: smart_intel.py
from __future__ import annotations
import os, re, json, logging
from typing import Any, Dict, List, Optional
from datetime import datetime, date

try:
    from openai import OpenAI
except Exception:
    OpenAI = None  # type: ignore

log = logging.getLogger(__name__)

OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "")
MODEL_ROUTER = os.getenv("OPENAI_MODEL_ROUTER", "gpt-4o-mini")
MODEL_REPLY  = os.getenv("OPENAI_MODEL_REPLY",  "gpt-4o-mini")

def _today() -> date: return datetime.utcnow().date()
def _norm(s: str) -> str: return re.sub(r"\s+", " ", (s or "").strip().lower())
def _strip_placeholders(s: str) -> str:
    s = re.sub(r"\[[^\]]+\]", "", s)
    s = re.sub(r"\{[^}]+\}", "", s)
    return re.sub(r"\s{2,}", " ", s).strip()

def _chat_json(system: str, user: str) -> Dict[str, Any]:
    if not (OpenAI and OPENAI_API_KEY): return {}
    try:
        cli = OpenAI(api_key=OPENAI_API_KEY)
        r = cli.chat.completions.create(
            model=MODEL_ROUTER,
            messages=[{"role":"system","content":system},{"role":"user","content":user}],
            response_format={"type":"json_object"},
            temperature=0,
        )
        return json.loads(r.choices[0].message.content or "{}")
    except Exception as e:
        log.warning(f"[openai-json] {e}"); return {}

def _chat_text(system: str, user: str) -> str:
    if not (OpenAI and OPENAI_API_KEY): return ""
    try:
        cli = OpenAI(api_key=OPENAI_API_KEY)
        r = cli.chat.completions.create(
            model=MODEL_REPLY,
            messages=[{"role":"system","content":system},{"role":"user","content":user}],
            temperature=0.2,
        )
        return (r.choices[0].message.content or "").strip()
    except Exception as e:
        log.warning(f"[openai-text] {e}"); return ""

# ---------- Intent ----------
def _parse_date(d: Any) -> Optional[date]:
    if isinstance(d, date): return d
    if isinstance(d, str):
        try: return datetime.fromisoformat(d).date()
        except Exception: return None
    return None

def analyze_guest_intent(message: str, context: Dict[str, Any]) -> Dict[str, Any]:
    sys = ("Analyze a vacation-rental guest message. Return JSON: "
           "primary_intent, urgency (high|medium|low), requires_calendar_check (bool).")
    user = json.dumps({
        "message": message or "",
        "check_in": str(_parse_date(context.get('check_in') or context.get('check_in_date')) or ""),
        "current_date": str(_today()),
        "reservation_status": context.get("reservation_status") or context.get("status"),
    }, ensure_ascii=False)
    data = _chat_json(sys, user)
    if data:
        return {
            "primary_intent": str(data.get("primary_intent","other")).lower() or "other",
            "urgency": str(data.get("urgency","low")).lower() if str(data.get("urgency","low")).lower() in {"high","medium","low"} else "low",
            "requires_calendar_check": bool(data.get("requires_calendar_check", False)),
        }
    t = _norm(message)
    pi = ("inquire_about_amenities" if any(w in t for w in ("amenit","bed","bath","wifi","parking","pool","kid","walk")) else "other")
    return {"primary_intent": pi, "urgency": "low", "requires_calendar_check": False}

# ---------- Context ----------
def determine_guest_journey_stage(meta: Dict[str, Any]) -> str:
    ci, co = _parse_date(meta.get("check_in") or meta.get("check_in_date")), _parse_date(meta.get("check_out") or meta.get("check_out_date"))
    today = _today()
    if not ci: return "unknown"
    if today < ci: return "pre_arrival"
    if co and ci <= today <= co: return "during_stay"
    if co and today > co: return "post_checkout"
    return "unknown"

def enhanced_context(guest_message: str, history: List[Dict[str, Any]], meta: Dict[str, Any]) -> Dict[str, Any]:
    base = dict(meta or {})
    base["journey_stage"] = determine_guest_journey_stage(meta)
    base["last_guest_message"] = guest_message
    return base

# ---------- Amenities access ----------
def _amen_payload(ctx: Dict[str, Any]) -> Dict[str, Any]:
    ai = ctx.get("amenities_index") or {}
    if not isinstance(ai, dict):
        return {"amenities":{}, "amenity_labels":{}, "meta":{}, "bed_types":{}, "images":[], "custom_fields":{}}
    return {
        "amenities": ai.get("amenities") or {},
        "amenity_labels": ai.get("amenity_labels") or {},
        "meta": ai.get("meta") or {},
        "bed_types": ai.get("bed_types") or {},
        "images": ai.get("images") or [],
        "custom_fields": ai.get("custom_fields") or {},
    }

def _fmt_yes_no(v: Optional[bool]) -> str:
    if v is True: return "Yes"
    if v is False: return "No"
    return "I don’t have that info"

def _kv_fmt(label: str, value: Any) -> str:
    if value is None or value == "":
        return f"{label}: I don’t have that info"
    if isinstance(value, bool):
        return f"{label}: {_fmt_yes_no(value)}"
    return f"{label}: {value}"

# ---------- Universal QA over facts ----------
def _search_facts(message: str, ctx: Dict[str, Any]) -> Optional[str]:
    t = _norm(message)
    am = _amen_payload(ctx)
    meta, ams, labels = am["meta"], am["amenities"], am["amenity_labels"]

    # 1) Structured common fields
    keys_order = [
        ("bedroomsNumber","Bedrooms"), ("bedsNumber","Beds"), ("bathroomsNumber","Bathrooms"),
        ("personCapacity","Maximum occupancy"), ("guestsIncluded","Guests included"),
        ("check_in_start","Check-in start"), ("check_in_end","Check-in end"), ("check_out_time","Check-out"),
        ("cancellationPolicy","Cancellation policy"),
        ("wifiUsername","Wi-Fi SSID"), ("wifiPassword","Wi-Fi password"),
        ("roomType","Room type"), ("bathroomType","Bathroom type"),
        ("squareMeters","Size (m²)"),
    ]
    answers: List[str] = []
    for k, label in keys_order:
        if any(w in t for w in _norm(label).split()) or re.search(rf"\b{_norm(label).split()[0]}\b", t):
            val = meta.get(k) if k in meta else ctx.get("property_details",{}).get(k)
            if k in ("check_in_start","check_in_end","check_out_time"):
                val = meta.get(k) or (ctx.get("property_details") or {}).get(k)
            if val is not None and val != "":
                answers.append(_kv_fmt(label, val))

    # 2) Try amenity yes/no via fuzzy label hit
    amen_triggers = ["wifi","parking","pool","hot tub","jacuzzi","spa","ac","air conditioning","heater","heating","kitchen","washer","dryer","dishwasher","tv","pets","gym","elevator","balcony","grill","bbq","crib","ev charger","charging"]
    if any(w in t for w in amen_triggers):
        for key, present in ams.items():
            lab = labels.get(key, key.replace("_"," ").title())
            if any(w in t for w in _norm(lab).split()):
                answers.append(_kv_fmt(lab, bool(present)))

    # 3) Bed types expansion
    if any(w in t for w in ("bed","beds","bed type","king","queen","sofa","bunk")) and am["bed_types"]:
        bt = ", ".join(f"{qty}× {name}" for name, qty in am["bed_types"].items())
        answers.append(f"Bed setup: {bt}")

    # 4) Custom fields surfaced on demand
    if am["custom_fields"]:
        for k, v in am["custom_fields"].items():
            if _norm(k) in t:
                answers.append(_kv_fmt(k.replace("_"," ").title(), v))

    # 5) Fuzzy fallback over meta keys/values
    if not answers:
        # lightweight term match
        q_toks = [tok for tok in re.split(r"[^a-z0-9]+", t) if tok]
        candidates: List[str] = []
        for k, v in meta.items():
            txt = f"{k} {v}".lower()
            score = sum(1 for tok in q_toks if tok in txt)
            if score >= 2 or (score == 1 and len(k) <= 24):
                label = k.replace("_"," ").replace("Number","").title()
                candidates.append(_kv_fmt(label, v))
                if len(candidates) >= 3:
                    break
        answers.extend(candidates)

    if not answers:
        return None
    return _hospitalify(" ".join(answers))

# ---------- Tone ----------
def _hospitalify(text: str) -> str:
    s = _strip_placeholders(text)
    if not s:
        return "Thanks for reaching out! Happy to help with any details you need."
    if not re.search(r"[.!?]$", s):
        s += "."
    return s + " Anything else I can clarify?"

# ---------- System prompt ----------
ENHANCED_SYSTEM_PROMPT = """You are a vacation rental host assistant.

STYLE:
- Warm, hospitable, concise (1–3 sentences). Sound like a thoughtful property manager.

FACTS-ONLY:
- Use ONLY facts provided in FACTS_JSON (Hostaway fields, amenities, bed types, policies, images, custom fields).
- If a fact is missing, say you don’t have that info and offer to check. Do NOT guess. Do NOT use placeholders.
"""

def compose_reply(guest_message: str, ctx: Dict[str, Any], intent_info: Dict[str, Any]) -> str:
    t = _norm(guest_message)

    # Post-checkout gratitude
    if re.search(r"\b(thank you|thanks|appreciate)\b", t) or re.search(r"\b(locked|lockbox|checked out|check-?out)\b", t):
        if ctx.get("journey_stage") in {"during_stay","post_checkout"} or "check out" in t or "locked" in t:
            return "Thanks so much for staying with us—hosting you was a pleasure! Safe travels, and I’m here if anything comes up."

    # UNIVERSAL FACT ANSWERS (covers all Hostaway fields + amenities)
    direct = _search_facts(guest_message, ctx)
    if direct:
        return direct

    # LLM fallback with facts (still safe)
    facts = {
        "intent": intent_info,
        "journey_stage": ctx.get("journey_stage"),
        "property_details": ctx.get("property_details"),
        "amenities_index": ctx.get("amenities_index"),
        "nearby": ctx.get("nearby"),
    }
    user = (
        f"GUEST_MESSAGE:\n{guest_message}\n\n"
        f"FACTS_JSON:\n{json.dumps(facts, ensure_ascii=False)}\n\n"
        "Write ONLY the reply. Use concrete facts from FACTS_JSON. Never use placeholders."
    )
    draft = _chat_text(ENHANCED_SYSTEM_PROMPT, user)
    return _hospitalify(draft or "")

def make_reply_smart(guest_message: str, meta_context: Dict[str, Any], history: Optional[List[Dict[str, Any]]] = None) -> Dict[str, Any]:
    history = history or []
    ctx = enhanced_context(guest_message, history, meta_context or {})
    intent = analyze_guest_intent(guest_message, ctx)
    reply = compose_reply(guest_message, ctx, intent)
    return {"reply": reply, "intent": intent["primary_intent"], "urgency": intent["urgency"], "confidence": 0.9 if reply else 0.3}
