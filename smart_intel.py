# path: smart_intel.py
from __future__ import annotations
import os, re, json, logging
from dataclasses import dataclass, field
from datetime import datetime, date
from functools import lru_cache
from typing import Any, Dict, List, Optional

try:
    from openai import OpenAI  # optional
except Exception:
    OpenAI = None  # type: ignore

log = logging.getLogger(__name__)
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "")
MODEL_ROUTER = os.getenv("OPENAI_MODEL_ROUTER", "gpt-4o-mini")
MODEL_REPLY  = os.getenv("OPENAI_MODEL_REPLY",  "gpt-4o-mini")

# -------- OpenAI helpers (optional) --------
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

# -------- tiny utils --------
def _today() -> date: return datetime.utcnow().date()
def _norm(x: str) -> str: return re.sub(r"\s+", " ", (x or "").strip().lower())

def _parse_date(d: Any) -> Optional[date]:
    if isinstance(d, date): return d
    if isinstance(d, str):
        try: return datetime.fromisoformat(d).date()
        except Exception: return None
    return None

def _days_until(d: Optional[date]) -> Optional[int]:
    if not d: return None
    return (d - _today()).days

# -------- 1) semantic intent --------
def analyze_guest_intent(message: str, context: Dict[str, Any]) -> Dict[str, Any]:
    sys = ("Analyze a vacation-rental guest message. Return ONLY JSON:"
           " primary_intent, urgency (high|medium|low), requires_calendar_check (bool), context_clues (list).")
    user = json.dumps({
        "message": message or "",
        "check_in": str(_parse_date(context.get("check_in_date")) or ""),
        "current_date": str(_today()),
        "reservation_status": context.get("status"),
    }, ensure_ascii=False)
    data = _chat_json(sys, user)
    if data:
        pi = str(data.get("primary_intent", "other")).lower()
        urg = str(data.get("urgency", "low")).lower()
        return {
            "primary_intent": pi or "other",
            "urgency": urg if urg in {"high","medium","low"} else "low",
            "requires_calendar_check": bool(data.get("requires_calendar_check", False)),
            "context_clues": data.get("context_clues") or [],
        }
    # heuristic fallback
    t = _norm(message)
    def has(*k): return any(kv in t for kv in k)
    ci_days = _days_until(_parse_date(context.get("check_in_date")))
    if   has("early check in","early check-in","drop bags"): pi = "early_check_in"
    elif has("late checkout","late check-out"):              pi = "late_check_out"
    elif has("code","lock","check in","check-in"):           pi = "check_in_help"
    elif has("trash","garbage","bin"):                       pi = "trash_help"
    elif has("accessib","wheelchair","elevator"):            pi = "accessibility"
    elif has("restaurant","eat","dinner","coffee","lockbox","thank you","thanks"):  # extra signals
        pi = "other"
    elif has("parking","garage"):                            pi = "parking"
    else:                                                    pi = "other"
    urg = "high" if has("no power","no heat","leak","gas","locked out") else (
          "high" if (ci_days is not None and ci_days <= 1 and pi in {"check_in_help","early_check_in"}) else "low")
    return {"primary_intent": pi, "urgency": urg, "requires_calendar_check": pi in {"early_check_in","late_check_out","booking_question"}, "context_clues": []}

# -------- 2) proactive suggestions --------
def should_proactively_offer_info(ctx: Dict[str, Any]) -> List[str]:
    ci = _parse_date(ctx.get("check_in_date"))
    days = _days_until(ci) if ci else None
    out: List[str] = []
    if days is not None and 1 <= days <= 2: out.append("arrival_instructions")
    if ctx.get("weather") in {"rain","storm"} and (days is None or days <= 3): out.append("indoor_activities")
    if ctx.get("has_complicated_parking"): out.append("parking_map")
    return out

# -------- 3) richer context --------
@lru_cache(maxsize=128)
def get_property_profile(listing_id: str) -> Dict[str, Any]:
    return {"listing_id": listing_id, "amenities": ["wifi","ac","parking"], "limitations": ["no_elevator"], "check_in_window": "4pm-10pm"}

def determine_guest_journey_stage(meta: Dict[str, Any]) -> str:
    ci, co = _parse_date(meta.get("check_in_date")), _parse_date(meta.get("check_out_date"))
    today = _today()
    if not ci: return "unknown"
    if today < ci: return "pre_arrival"
    if co and ci <= today <= co: return "during_stay"
    if co and today > co: return "post_checkout"
    return "unknown"

def _classify_guest_style(history: List[Dict[str, Any]]) -> str:
    last = (history or [{}])[-1]
    txt = _norm(last.get("text",""))
    if len(txt) <= 24 and "?" not in txt: return "terse"
    if "please" in txt or "pls" in txt:   return "polite"
    if any(w in txt for w in ("asap","urgent","now")): return "urgent"
    return "neutral"

def enhanced_context(guest_message: str, history: List[Dict[str, Any]], meta: Dict[str, Any]) -> Dict[str, Any]:
    base = dict(meta or {})
    base["journey_stage"] = determine_guest_journey_stage(meta)
    base["property_context"] = get_property_profile(str(meta.get("listing_id") or ""))
    base["guest_style"] = _classify_guest_style(history)
    base["proactive_suggestions"] = should_proactively_offer_info(base)
    base["last_guest_message"] = guest_message
    return base

# -------- 4) QA / validation --------
def _sem_rel(reply: str, guest_msg: str) -> bool:
    rt = set(re.findall(r"[a-z]{3,}", _norm(reply)))
    gt = set(re.findall(r"[a-z]{3,}", _norm(guest_msg)))
    if not rt or not gt: return True
    return (len(rt & gt) / max(1, len(gt))) >= 0.15

def _policy_issues(reply: str, ctx: Dict[str, Any]) -> List[str]:
    issues: List[str] = []
    if "early check in" in _norm(reply) and (ctx.get("property_context") or {}).get("check_in_window") == "4pm-10pm":
        if re.search(r"\b(before\s*4\s*pm|3[:]?00|2[:]?00)\b", reply, re.I): issues.append("unapproved_early_checkin_promise")
    if re.search(r"\b(compensation|refund)\b", reply, re.I) and not ctx.get("allow_comp"): issues.append("unapproved_compensation")
    return issues

def _tone_ok(reply: str, guest_style: Optional[str]) -> bool:
    if guest_style == "terse" and len(reply) > 350: return False
    if guest_style == "polite" and re.search(r"\b(asap|now)\b", reply, re.I): return False
    return True

def _confidence(reply: str, ctx: Dict[str, Any]) -> float:
    score = 0.5
    if len(reply) < 400: score += 0.1
    if _sem_rel(reply, ctx.get("last_guest_message","")): score += 0.2
    if not _policy_issues(reply, ctx): score += 0.1
    return round(max(0.0, min(1.0, score)), 2)

def validate_reply(reply: str, guest_msg: str, ctx: Dict[str, Any]) -> Dict[str, Any]:
    issues: List[str] = []
    if not _sem_rel(reply, guest_msg): issues.append("reply_off_topic")
    issues += _policy_issues(reply, ctx)
    if not _tone_ok(reply, ctx.get("guest_style")): issues.append("tone_mismatch")
    return {"is_valid": not issues, "issues": issues, "confidence_score": _confidence(reply, ctx)}

# -------- 5) prompt & compose --------
ENHANCED_SYSTEM_PROMPT = """You are a vacation rental host assistant.

CONTEXT:
- Use journey stage (pre-arrival, during stay, post-checkout)
- Respect reservation timing and property limitations

STYLE:
- Match guest formality; use contractions for casual tone
- Keep it concise (1–3 sentences unless steps are needed)

PRIORITIES: 1) Urgent/safety first 2) Answer specifically 3) Add only relevant context 4) Provide next steps
NEVER: Promise unavailable services; repeat the guest’s own words; use empty hospitality clichés
"""

def compose_reply(guest_message: str, ctx: Dict[str, Any], intent_info: Dict[str, Any]) -> str:
    # Gratitude/departure override → friendly thanks (prevents checkout nags)
    if re.search(r"\b(thank you|thanks|appreciate)\b", _norm(guest_message)) and \
       (ctx.get("journey_stage") == "post_checkout" or re.search(r"\b(locked|lockbox|checked out|check(ed)?-?out)\b", _norm(guest_message))):
        return "Thanks so much for staying with us—glad to host you! Safe travels, and if you have feedback, I’d love to hear it."

    facts = {"intent": intent_info, "proactive": ctx.get("proactive_suggestions"),
             "journey_stage": ctx.get("journey_stage"), "property": ctx.get("property_context")}
    user = (f"GUEST_MESSAGE:\n{guest_message}\n\n"
            f"FACTS_JSON:\n{json.dumps(facts, ensure_ascii=False)}\n\n"
            "Write ONLY the reply. Use bullets only for steps or multiple options.")
    reply = _chat_text(ENHANCED_SYSTEM_PROMPT, user)
    if reply: return reply
    # fallbacks
    pi = intent_info["primary_intent"]
    if   pi == "early_check_in": return "We’ll try to accommodate early check-in if the home is ready. I’ll confirm after today’s clean—worst case we can hold bags."
    elif pi == "check_in_help":  return "I can help with entry. Do you need the door code or parking details? I can resend the arrival guide."
    elif pi == "trash_help":     return "Trash pickup is early morning. Please use the cans by the driveway; tie extra bags and place beside the bins if needed."
    elif pi == "food_recs":      return "A couple nearby picks within 5–10 minutes. Tell me what you’re craving and I’ll tailor recs."
    else:                        return "Got it—happy to help. Share a bit more detail and I’ll point you the right way."

# -------- 6) top-level --------
def make_reply_smart(guest_message: str, meta_context: Dict[str, Any], history: Optional[List[Dict[str, Any]]] = None) -> Dict[str, Any]:
    history = history or []
    ctx = enhanced_context(guest_message, history, meta_context or {})
    intent_info = analyze_guest_intent(guest_message, ctx)
    raw = compose_reply(guest_message, ctx, intent_info)
    val = validate_reply(raw, guest_message, ctx)
    if not val["is_valid"] or val["confidence_score"] < 0.6:
        critique = f"Issues: {', '.join(val['issues']) or 'none'}. Improve relevance and stay within property limits."
        revised = _chat_text(ENHANCED_SYSTEM_PROMPT, f"{guest_message}\n\nCRITIQUE:\n{critique}\nRevise briefly.")
        if revised: raw = revised.strip(); val = validate_reply(raw, guest_message, ctx)
    return {
        "reply": raw,
        "intent": intent_info["primary_intent"],
        "urgency": intent_info["urgency"],
        "proactive": ctx.get("proactive_suggestions", []),
        "validation": val,
        "context_debug": {
            "journey_stage": ctx.get("journey_stage"),
            "guest_style": ctx.get("guest_style"),
            "requires_calendar_check": intent_info["requires_calendar_check"],
        },
    }
