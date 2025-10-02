# file: smart_intel.py

import os
import json
import logging
from datetime import datetime, timezone
from typing import Dict, Any, Optional, Tuple, List, Union

try:
    from openai import OpenAI
    _client = OpenAI(api_key=os.getenv("OPENAI_API_KEY"))
    _has_client = True
except Exception:
    _client = None
    _has_client = False

OPENAI_MODEL = os.getenv("OPENAI_MODEL", "gpt-4o-mini")

# Generic place + distance helpers (no hardcoded venues)
from places import (
    text_search_place,                   # resolves any free-text destination
    get_drive_distance_duration,         # accepts address or (lat,lng) for origin/dest
)

# Hostaway + reply cleanup helpers
from utils import (
    get_calendar,
    calendar_window_is_available,
    derive_min_stay,
    price_details_v2,
    early_late_available,
    fetch_hostaway_reservation,
    clean_ai_reply,
    sanitize_ai_reply,
)


# --- add this helper ---
def _smart_generate_reply(generate_reply_fn, guest_message: str, ctx: Dict[str, Any]) -> str:
    """
    Call smart_intel.generate_reply safely regardless of version:
      - v1: generate_reply(guest_message, ctx)
      - v2: generate_reply(ctx, guest_message)
    Accepts either str or dict return values.
    """
    if not generate_reply_fn:
        return ""

    def _normalize(ret) -> str:
        if isinstance(ret, str):
            return ret
        if isinstance(ret, dict):
            # common keys your writer might return
            for k in ("reply", "text", "message"):
                if k in ret and isinstance(ret[k], str):
                    return ret[k]
        return ""

    # try v1
    try:
        out = generate_reply_fn(guest_message, ctx)
        s = _normalize(out)
        if s:
            return s
    except Exception:
        pass

    # try v2
    try:
        out = generate_reply_fn(ctx, guest_message)
        s = _normalize(out)
        if s:
            return s
    except Exception:
        pass

    return ""

# Optional: flexible date parsing
try:
    from dateutil import parser as _dtparse
except Exception:
    _dtparse = None  # fallback to ISO only


# ---------- Utilities ----------

def _parse_iso_date(s: Optional[str]):
    if not s:
        return None
    # Accept true ISO or RFC3339, fallback to dateutil if installed
    if _dtparse:
        try:
            return _dtparse.parse(s)
        except Exception:
            pass
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00"))
    except Exception:
        return None

def _coerce_date_str(s: Optional[str]) -> Optional[str]:
    if not s:
        return None
    dt = _parse_iso_date(s)
    return dt.date().isoformat() if dt else None

def _infer_phase(check_in: Optional[str], check_out: Optional[str], now: Optional[datetime] = None) -> str:
    """
    pre_arrival | in_stay | post_stay | unknown
    """
    now = now or datetime.now(timezone.utc)
    ci = _parse_iso_date(check_in)
    co = _parse_iso_date(check_out)
    if not ci or not co:
        return "unknown"
    if now.date() < ci.date():
        return "pre_arrival"
    if ci.date() <= now.date() <= co.date():
        return "in_stay"
    if now.date() > co.date():
        return "post_stay"
    return "unknown"

def _pick_origin(ctx: Dict[str, Any]) -> Optional[Union[str, Tuple[float, float]]]:
    """
    Choose best available origin: address string, else (lat,lng), else None.
    """
    addr = ctx.get("property_address") \
        or ((ctx.get("listing_info") or {}).get("address") or {}).get("address1")
    lat = ctx.get("latitude"); lng = ctx.get("longitude")
    if addr:
        return addr
    if isinstance(lat, (int, float)) and isinstance(lng, (int, float)):
        return (float(lat), float(lng))
    return None

def _collect_core_facts(ctx: Dict[str, Any]) -> Dict[str, Any]:
    """
    Lift everything the writer might use; do not assume any specific Hostaway schema.
    """
    reservation = ctx.get("reservation") or {}
    listing = ctx.get("listing_info") or {}
    address1 = (listing.get("address") or {}).get("address1") or ctx.get("property_address")

    # Accept multiple field names
    check_in = reservation.get("arrivalDate") or reservation.get("checkInDate") or reservation.get("checkIn") or ctx.get("check_in")
    check_out = reservation.get("departureDate") or reservation.get("checkOutDate") or reservation.get("checkOut") or ctx.get("check_out")

    def _num(x):
        try: return int(float(x))
        except Exception: return None

    facts = {
        "guest_name": ctx.get("guest_name"),
        "property_name": listing.get("name") or ctx.get("property_name"),
        "property_address": address1,
        "latitude": ctx.get("latitude"),
        "longitude": ctx.get("longitude"),
        "check_in": check_in,
        "check_out": check_out,
        "guest_count": ctx.get("guest_count") or reservation.get("numberOfGuests"),
        "status": (ctx.get("status") or reservation.get("status")),
        "beds": _num(listing.get("beds") or listing.get("numberOfBeds") or listing.get("bedCount")),
        "bedrooms": _num(listing.get("bedrooms") or listing.get("numberOfBedrooms") or listing.get("bedroomsNumber")),
        "bathrooms": _num(listing.get("bathrooms") or listing.get("numberOfBathrooms") or listing.get("bathroomsNumber")),
        "city": (listing.get("address") or {}).get("city") or ctx.get("city"),
        "state": (listing.get("address") or {}).get("state") or (listing.get("address") or {}).get("province") or ctx.get("state"),
        "nearby_places": (ctx.get("nearby_places") or [])[:3],
        # IDs for Hostaway calls
        "listing_id": ctx.get("listing_id") or reservation.get("listingMapId"),
        "reservation_id": reservation.get("id") or ctx.get("reservation_id"),
        "conversation_id": ctx.get("conversation_id"),
    }
    facts["phase"] = _infer_phase(check_in, check_out)
    return facts


# ---------- Prompts ----------

PLANNER_SYSTEM = """You are a planner that extracts user intent and entities from a guest message for a short-term rental host.
Return ONLY valid JSON with these top-level keys:

{
  "wants_availability": boolean,
  "wants_price_quote": boolean,
  "dates": {"start": "YYYY-MM-DD" | null, "end": "YYYY-MM-DD" | null},
  "guests": int | null,
  "wants_distance": boolean,
  "destinations": [ {"text": string} ],
  "wants_recommendations": boolean,
  "info_questions": [ "bedrooms" | "bathrooms" | "beds" | "check_in" | "check_out" | "guest_count" | "address" ],
  "clarifications": []
}

Rules:
- If they mention booking, availability, specific dates, or rates/price: set the appropriate flags.
- Extract explicit or relative dates if present; otherwise nulls.
- Extract guest count if present (adults+children combined).
- If they ask “how far” or travel time → wants_distance true; place the raw target text in destinations.
- If they ask for local things to do/places → wants_recommendations true.
- If nothing is ambiguous, clarifications must be [].
"""

WRITER_SYSTEM = """You are an expert guest-messaging assistant for short-term rentals.
Use ONLY the provided facts. No greetings, no sign-offs, no emojis. No questions.

Priorities:
1) If availability facts exist, say Available/Not available; include min-stay if provided.
2) If price quote exists, give total and 1–2 key components (plain words).
3) If upsells exist, state whether early check-in / late check-out is possible and fees if provided.
4) If distance facts exist, include miles and minutes succinctly.
5) If the guest asked about bedrooms/bathrooms/beds and you have values, answer directly.
6) If local recommendations exist, list up to 2–3 concise options.

If a critical fact is missing, state what’s needed in a single short clause (no question mark).
Output only the final message text.
"""


# ---------- Public entrypoint ----------

def generate_reply(guest_message: str, ctx: Dict[str, Any]) -> str:
    """
    Two-pass flow:
      1) Planner: extract intents, dates, guests, destinations.
      2) Hostaway: availability, price v2, upsells.
      3) Places: resolve destinations + distances using address/(lat,lng).
      4) Writer: compose decisive reply (no clarifying questions).
    """
    core = _collect_core_facts(ctx)

    # ----- Pass 1: Planner -----
    planner_input = {
        "guest_message": guest_message,
        "available_facts": {k: v for k, v in core.items() if v is not None}
    }
    plan = {
        "wants_availability": False,
        "wants_price_quote": False,
        "dates": {"start": None, "end": None},
        "guests": None,
        "wants_distance": False,
        "destinations": [],
        "wants_recommendations": False,
        "info_questions": [],
        "clarifications": [],
    }

    try:
        if _has_client and _client:
            p = _client.chat.completions.create(
                model=OPENAI_MODEL,
                temperature=0.0,
                messages=[
                    {"role": "system", "content": PLANNER_SYSTEM},
                    {"role": "user", "content": json.dumps(planner_input)}
                ],
                response_format={"type": "json_object"},
            )
            plan = json.loads(p.choices[0].message.content or "{}")
    except Exception as e:
        logging.warning(f"Planner call failed, falling back: {e}")

    # Normalize dates/guests
    start = _coerce_date_str(((plan.get("dates") or {}).get("start"))) or None
    end   = _coerce_date_str(((plan.get("dates") or {}).get("end"))) or None
    guests = plan.get("guests") or core.get("guest_count") or 1
    try:
        guests = int(guests)
    except Exception:
        guests = 1

    listing_id = core.get("listing_id")
    reservation_id = core.get("reservation_id")

    # ----- Hostaway: Availability & Price (if requested and data present) -----
    writer_facts: Dict[str, Any] = {k: v for k, v in core.items() if v is not None}

    if plan.get("wants_availability") and listing_id and start and end:
        try:
            days = get_calendar(listing_id, start, end, 0)
            is_open, window = calendar_window_is_available(days, start, end)
            min_stay = derive_min_stay(window)
            writer_facts["availability"] = {
                "is_open": bool(is_open),
                "min_stay": min_stay,
                "start": start,
                "end": end
            }
        except Exception as e:
            logging.warning(f"[availability] {e}")

    if plan.get("wants_price_quote") and listing_id and start and end:
        try:
            priced = price_details_v2(listing_id, start, end, int(guests))
            if priced and "totalPrice" in priced:
                comps = priced.get("components") or []
                key = []
                for c in comps:
                    title = (c.get("title") or c.get("name") or "").lower()
                    if any(k in title for k in ("base", "cleaning", "tax")) and len(key) < 2:
                        key.append({"title": c.get("title") or c.get("name"), "amount": c.get("total")})
                writer_facts["price_quote"] = {
                    "total": priced["totalPrice"],
                    "components": key,
                    "start": start, "end": end, "guests": int(guests)
                }
        except Exception as e:
            logging.warning(f"[pricing] {e}")

    # ----- Hostaway: Upsells (early/late) if reservation exists -----
    if listing_id and reservation_id:
        try:
            rj = fetch_hostaway_reservation(int(reservation_id)) or {}
            r = (rj.get("result") or {})
            ups = early_late_available(listing_id, r.get("arrivalDate"), r.get("departureDate"))
            if ups:
                writer_facts["upsells"] = {
                    "early_checkin_ok": bool(ups.get("early_checkin_ok")),
                    "late_checkout_ok": bool(ups.get("late_checkout_ok")),
                    "early_fee": os.getenv("EARLY_CHECKIN_FEE") or 0,
                    "late_fee": os.getenv("LATE_CHECKOUT_FEE") or 0,
                }
        except Exception as e:
            logging.debug(f"[upsells] {e}")

    # ----- Resolve distances (your original logic, kept) -----
    distances: List[Dict[str, Any]] = []
    origin = _pick_origin(ctx)

    if plan.get("wants_distance") and origin is not None:
        dest_items = plan.get("destinations") or []
        bias_lat = core.get("latitude") if isinstance(core.get("latitude"), (int, float)) else None
        bias_lng = core.get("longitude") if isinstance(core.get("longitude"), (int, float)) else None
        city = core.get("city"); state = core.get("state")

        for d in dest_items:
            dest_text = (d or {}).get("text")
            if not dest_text:
                continue
            place = text_search_place(
                query=dest_text,
                bias_lat=bias_lat,
                bias_lng=bias_lng,
                city=city,
                state=state,
            )
            if not place:
                continue

            try:
                if isinstance(origin, tuple):
                    dist = get_drive_distance_duration(origin, (place["lat"], place["lng"]))
                else:
                    dist = get_drive_distance_duration(origin, place["formatted_address"] or place["name"])
            except Exception as e:
                logging.warning(f"Distance calc failed for {dest_text}: {e}")
                dist = None

            if dist:
                distances.append({
                    "to_name": place["name"],
                    "to_address": place["formatted_address"],
                    "distance_text": dist.get("distance_text"),
                    "duration_text": dist.get("duration_text"),
                })

    if distances:
        writer_facts["distances"] = distances

    # Asked fields (bed/bath/beds) surfaced if present
    asked = set(plan.get("info_questions") or [])
    if "bedrooms" in asked and core.get("bedrooms") is None:
        writer_facts["missing_bedrooms"] = True
    if "bathrooms" in asked and core.get("bathrooms") is None:
        writer_facts["missing_bathrooms"] = True
    if "beds" in asked and core.get("beds") is None:
        writer_facts["missing_beds"] = True

    # Clarification only as a declarative clause, and only if we computed nothing else
    clarifications = plan.get("clarifications") or []
    if clarifications and not (writer_facts.get("availability") or writer_facts.get("price_quote") or writer_facts.get("distances")):
        writer_facts["planner_clarification"] = clarifications[0]

    # ----- Pass 2: Writer -----
    writer_input = {
        "guest_message": guest_message,
        "facts": writer_facts
    }

    try:
        if _has_client and _client:
            r = _client.chat.completions.create(
                model=OPENAI_MODEL,
                temperature=0.2,
                messages=[
                    {"role": "system", "content": WRITER_SYSTEM},
                    {"role": "user", "content": json.dumps(writer_input, ensure_ascii=False)}
                ],
            )
            final = (r.choices[0].message.content or "").strip()
        else:
            final = ""
    except Exception as e:
        logging.warning(f"Writer call failed: {e}")
        final = ""

    # Deterministic fallbacks to avoid questions
    if not final and writer_facts.get("availability"):
        a = writer_facts["availability"]
        status = "Available" if a.get("is_open") else "Not available"
        extra = f"; minimum stay {a['min_stay']} nights" if a.get("min_stay") else ""
        final = f"{status} for {a.get('start')}–{a.get('end')}{extra}."
    if not final and writer_facts.get("price_quote"):
        pq = writer_facts["price_quote"]
        parts = ", ".join(f"{c['title']}: {c['amount']}" for c in (pq.get("components") or [])[:2])
        final = f"Total for {pq['start']}–{pq['end']} for {pq['guests']} guest(s) is {pq['total']}. {parts}".strip()
    if not final and writer_facts.get("distances"):
        d0 = writer_facts["distances"][0]
        final = f"{d0['to_name']} is about {d0.get('duration_text')} by car ({d0.get('distance_text')})."
    if not final and writer_facts.get("upsells"):
        u = writer_facts["upsells"]
        bits = []
        if u.get("early_checkin_ok"):
            bits.append(f"Early check-in available (fee {u.get('early_fee')}).")
        if u.get("late_checkout_ok"):
            bits.append(f"Late check-out available (fee {u.get('late_fee')}).")
        if bits:
            final = " ".join(bits)

    if not final:
        final = "I’ve shared the applicable details for your dates and request."

    # Final cleanup: tone/length guardrails
    final = clean_ai_reply(final, guest_message)
    final = sanitize_ai_reply(final, guest_message)
    return final
