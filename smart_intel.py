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
            temperature=0.3,
        )
        return (r.choices[0].message.content or "").strip()
    except Exception as e:
        log.warning(f"[openai-text] {e}"); return ""

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

def determine_guest_journey_stage(meta: Dict[str, Any]) -> str:
    ci, co = _parse_date(meta.get("check_in") or meta.get("check_in_date")), _parse_date(meta.get("check_out") or meta.get("check_out_date"))
    today = _today()
    if not ci: return "unknown"
    if today < ci: return "pre_arrival"
    if co and ci <= today <= co: return "during_stay"
    if co and today > co: return "post_checkout"
    return "unknown"

def _persona_from_ctx(ctx: Dict[str, Any]) -> Dict[str, Any]:
    core = (ctx.get("core_identity") or {})
    return {
        "brand_voice": core.get("brand_voice") or "friendly, attentive, genuinely helpful property manager",
        "greeting": core.get("greeting") or "Hey",
        "signoff": core.get("signoff") or None,
        "donts": core.get("donts") or ["no placeholders"],
        "dos": core.get("dos") or ["be warm", "be specific", "offer help briefly"],
    }

def _style_for_stage(stage: str) -> Dict[str, Any]:
    if stage == "pre_arrival":
        return {"tone":"excited & helpful", "close":"If you need anything before you arrive, just shout."}
    if stage == "during_stay":
        return {"tone":"responsive & solution-focused", "close":"Anything else I can sort out?"}
    if stage == "post_checkout":
        return {"tone":"grateful & welcoming", "close":"We loved hosting you—hope to welcome you back!"}
    return {"tone":"warm & concise", "close":"Happy to help with anything else."}

def enhanced_context(guest_message: str, history: List[Dict[str, Any]], meta: Dict[str, Any]) -> Dict[str, Any]:
    base = dict(meta or {})
    stage = determine_guest_journey_stage(meta)
    base["journey_stage"] = stage
    base["last_guest_message"] = guest_message
    base["persona"] = _persona_from_ctx(base)
    base["style"] = _style_for_stage(stage)
    return base

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

def _search_facts(message: str, ctx: Dict[str, Any]) -> Optional[str]:
    t = _norm(message)
    am = _amen_payload(ctx)
    meta, ams, labels = am["meta"], am["amenities"], am["amenity_labels"]

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
        anchor = _norm(label).split()[0]
        if anchor in t or _norm(k) in t:
            val = meta.get(k) if k in meta else (ctx.get("property_details",{}) or {}).get(k)
            if k in ("check_in_start","check_in_end","check_out_time"):
                val = meta.get(k) or (ctx.get("property_details") or {}).get(k)
            if val is not None and val != "":
                answers.append(_kv_fmt(label, val))

    if any(w in t for w in ("wifi","parking","pool","hot tub","jacuzzi","spa","ac","air conditioning","heater","heating",
                             "kitchen","washer","dryer","dishwasher","tv","pets","gym","elevator","balcony","grill","bbq","crib","ev charger","charging")):
        for key, present in ams.items():
            lab = labels.get(key, key.replace("_"," ").title())
            if any(tok in t for tok in _norm(lab).split()):
                answers.append(_kv_fmt(lab, bool(present)))

    if any(w in t for w in ("bed","beds","bed type","king","queen","sofa","bunk")) and am["bed_types"]:
        bt = ", ".join(f"{qty}× {name}" for name, qty in am["bed_types"].items())
        answers.append(f"Bed setup: {bt}")

    if am["custom_fields"]:
        for k, v in am["custom_fields"].items():
            if _norm(k) in t:
                answers.append(_kv_fmt(k.replace("_"," ").title(), v))

    if not answers:
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
    return _hospitalify(" ".join(answers), ctx)

def _hospitalify(text: str, ctx: Dict[str, Any]) -> str:
    s = _strip_placeholders(text) or "Happy to help with any details you need."
    persona = ctx.get("persona") or {}
    style = ctx.get("style") or {}
    greeting = persona.get("greeting") or "Hey"
    close = style.get("close") or "Happy to help with anything else."
    s = s.strip()
    if not re.match(r"^(hi|hey|hello)\b", s, re.I):
        s = f"{greeting}! " + s
    if not re.search(r"[.!?]$", s):
        s += "."
    return f"{s} {close}"

def get_enhanced_system_prompt(persona: dict | None = None, stage: str = "unknown") -> str:
    brand_voice = (persona or {}).get("brand_voice") or "friendly, attentive, genuinely helpful property manager"
    greeting    = (persona or {}).get("greeting") or "Hey"
    stage_line = {
        "pre_arrival":  "Tone hint: excited and helpful before arrival.",
        "during_stay":  "Tone hint: responsive and solution-focused during the stay.",
        "post_checkout":"Tone hint: grateful and welcoming after checkout.",
    }.get(stage, "Tone hint: warm, concise, human.")
    return f"""You are the friendly property manager for a vacation rental. You genuinely want guests to have an amazing stay and feel welcomed.

BRAND VOICE:
- Subtly reflect this voice: {brand_voice}
- Use a brief natural greeting like "{greeting}" when it helps warmth.

{stage_line}

TONE & STYLE:
- Conversational, helpful, warm but not overly casual; like texting a friend who is great at hospitality.
- Use contractions naturally (you'll, we've, that's).
- Include brief courtesy phrases when they fit.
- Be solution-focused when guests have issues.
- Show enthusiasm for helping guests enjoy their stay.
- Keep it short: 1–3 sentences total unless details are truly necessary.

FACTS-ONLY & SAFETY:
- Use ONLY facts provided in FACTS_JSON. If a fact is missing, say you don’t have it and offer to check—do not guess.
- Never output placeholders like [insert …] or {{…}}.
- If the guest asks about amenities/policies, quote specifics from the facts when available.

AVOID:
- Corporate language or policy-speak.
- Excessive apologizing; one brief apology only if there is an actual issue.
- Emojis (unless the property’s brand explicitly uses them and it fits the situation).
- Overly long responses.

OUTPUT:
- Reply with the message text only (no labels, no metadata). Keep it human, specific, and actionable, and end with a concise offer to help if appropriate.
"""

def compose_reply(guest_message: str, ctx: Dict[str, Any], intent_info: Dict[str, Any]) -> str:
    t = _norm(guest_message)
    if re.search(r"\b(thank you|thanks|appreciate)\b", t) or re.search(r"\b(locked|lockbox|checked out|check-?out)\b", t):
        if ctx.get("journey_stage") in {"during_stay","post_checkout"} or "check out" in t or "locked" in t:
            return _hospitalify("Thanks so much for staying with us—hosting you was a pleasure!", ctx)

    direct = _search_facts(guest_message, ctx)
    if direct:
        return direct

    facts = {
        "intent": intent_info,
        "journey_stage": ctx.get("journey_stage"),
        "property_details": ctx.get("property_details"),
        "amenities_index": ctx.get("amenities_index"),
        "nearby": ctx.get("nearby"),
    }
    style_json = {"persona": ctx.get("persona"), "style": ctx.get("style")}
    user = (
        f"GUEST_MESSAGE:\n{guest_message}\n\n"
        f"FACTS_JSON:\n{json.dumps(facts, ensure_ascii=False)}\n\n"
        f"STYLE_JSON:\n{json.dumps(style_json, ensure_ascii=False)}\n\n"
        "Write only the reply text, in a warm, human tone that matches STYLE_JSON and uses concrete facts. No placeholders."
    )
    sys_prompt = get_enhanced_system_prompt(ctx.get("persona"), ctx.get("journey_stage"))
    draft = _chat_text(sys_prompt, user)
    return _hospitalify(draft or "", ctx)

def make_reply_smart(guest_message: str, meta_context: Dict[str, Any], history: Optional[List[Dict[str, Any]]] = None) -> Dict[str, Any]:
    history = history or []
    ctx = enhanced_context(guest_message, history, meta_context or {})
    intent = analyze_guest_intent(guest_message, ctx)
    reply = compose_reply(guest_message, ctx, intent)
    return {"reply": reply, "intent": intent["primary_intent"], "urgency": intent["urgency"], "confidence": 0.9 if reply else 0.3}
