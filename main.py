import os
import json
import logging
from typing import Any, Dict, Optional, Tuple, List

from fastapi import FastAPI, APIRouter, Request, HTTPException, Form
from fastapi.responses import JSONResponse, PlainTextResponse

# --- Local imports (match your current repo structure) ---
# Utils: Hostaway, Slack, AI helpers you already have
from utils import (
    make_suggested_reply,
    send_reply_to_hostaway,
    fetch_hostaway_listing,
    fetch_hostaway_reservation,
    fetch_hostaway_conversation,
    extract_destination_from_message,
    resolve_place_textsearch,           # legacy text search (kept for compatibility)
    get_distance_drive_time,            # legacy DM text path (kept)
)

# Places: newer, more reliable Google helpers (bias + retries)
try:
    from places import text_search_place, get_drive_distance_duration
except Exception:
    text_search_place = None
    get_drive_distance_duration = None

# Slack signature + simple blocks
try:
    from slack_interactivity import verify_slack_signature, build_sent_block
except Exception:
    verify_slack_signature = None
    def build_sent_block(title: str, body: str) -> list:
        return [{"type": "header", "text": {"type": "plain_text", "text": title}},
                {"type": "section", "text": {"type": "mrkdwn", "text": body}}]

# Optional Slack notifier (channel-based)
try:
    from utils import slack_notify
except Exception:
    def slack_notify(*args, **kwargs):
        return None

# Optional smarter writer
try:
    from smart_intel import generate_reply  # your improved planner/writer
except Exception:
    generate_reply = None

# ------------------------------------------------------------------

logging.basicConfig(level=logging.INFO)
app = FastAPI()
router = APIRouter()

SLACK_SIGNING_SECRET = os.getenv("SLACK_SIGNING_SECRET", "")

# ---------------------------- Utilities ----------------------------

def _safe_get(d: Dict[str, Any], *keys, default=None):
    cur = d
    for k in keys:
        if not isinstance(cur, dict):
            return default
        cur = cur.get(k)
        if cur is None:
            return default
    return cur

def _extract_listing_context_from_payload(data: Dict[str, Any]) -> Dict[str, Any]:
    """
    Extract address/city/state/lat/lng directly from webhook payload.
    If missing, try to fetch by listingMapId using your existing utils.fetch_hostaway_listing.
    This keeps your current field names but also checks common alternates per Hostaway docs.
    """
    ctx: Dict[str, Any] = {
        "address": None, "city": None, "state": None, "zipcode": None, "lat": None, "lng": None
    }

    # Try typical shapes seen in Hostaway events
    listing = data.get("listing") or data.get("property") or {}
    for k in ("address", "city", "state", "zipcode", "lat", "lng", "street"):
        v = listing.get(k)
        if v not in (None, ""):
            # Keep address if only street is present
            if k == "street" and not ctx["address"]:
                ctx["address"] = v
            elif k in ctx:
                ctx[k] = v

    # Some payloads flatten fields at the top level (docs allow this)
    for k in ("address", "city", "state", "zipcode", "lat", "lng", "street"):
        if ctx.get("address") is None and k == "street" and data.get("street"):
            ctx["address"] = data.get("street")
        elif ctx.get(k) in (None, "") and data.get(k) not in (None, ""):
            ctx[k] = data.get(k)

    # If still missing coords, try listingMapId (very common)
    listing_id = data.get("listingMapId") or _safe_get(data, "reservation", "listingMapId")
    if (not ctx.get("lat") or not ctx.get("lng")) and listing_id:
        try:
            fetched = fetch_hostaway_listing(int(listing_id))
            fetched_result = (fetched or {}).get("result") or {}
            for k in ("address", "city", "state", "zipcode", "lat", "lng", "street"):
                fv = fetched_result.get(k)
                if k == "street" and fv and not ctx.get("address"):
                    ctx["address"] = fv
                elif k in ctx and fv not in (None, ""):
                    ctx[k] = fv
        except Exception as e:
            logging.info(f"[ctx] listing fetch failed for id={listing_id}: {e}")

    # If address still empty but city/state exist, synthesize a light display
    if not ctx.get("address"):
        if ctx.get("city") or ctx.get("state"):
            ctx["address"] = ", ".join([p for p in [ctx.get("city"), ctx.get("state")] if p]) or None

    return ctx

def _normalize_named_place(raw: Optional[str]) -> Optional[str]:
    if not raw:
        return None
    t = raw.strip()
    # Common case: “heb center” → “H-E-B Center”
    if t.lower().replace(".", "").strip().startswith("heb center"):
        return "H-E-B Center"
    return t

def _resolve_named_place_and_distance(
    listing_ctx: Dict[str, Any], guest_text: str
) -> Tuple[Optional[str], Optional[Dict[str, Any]]]:
    """
    Uses your improved places.* path if available; otherwise falls back to your legacy text matrix path.
    Returns (place_name, {"miles": float, "minutes": int} | None)
    """
    # Need coords to bias search + compute distance
    lat = listing_ctx.get("lat")
    lng = listing_ctx.get("lng")
    city = listing_ctx.get("city")
    state = listing_ctx.get("state")
    if lat is None or lng is None:
        return None, None

    # Pull a destination from the guest message
    dest_query = extract_destination_from_message(guest_text) or ""
    dest_query = _normalize_named_place(dest_query)
    if not dest_query:
        return None, None

    # Prefer the robust helpers from places.py if present
    if text_search_place and get_drive_distance_duration:
        try:
            place = text_search_place(dest_query, float(lat), float(lng), city, state)
            if not place:
                return dest_query, None
            loc = (place.get("geometry") or {}).get("location") or {}
            dlat, dlng = loc.get("lat"), loc.get("lng")
            if dlat is None or dlng is None:
                return place.get("name") or dest_query, None
            dist = get_drive_distance_duration((float(lat), float(lng)), (float(dlat), float(dlng)))
            return (place.get("name") or dest_query), dist
        except Exception as e:
            logging.info(f"[distance] robust path failed, will try legacy. err={e}")

    # Legacy text path (kept for compatibility if places.py not available)
    try:
        # Your legacy utils.get_distance_drive_time returns a string like:
        #   "<dest> is about <dur> by car (<dist>)."
        # We can pass this string into AI, but also try to parse miles/mins lightly.
        dm_text = get_distance_drive_time(float(lat), float(lng), dest_query)
        # Try to extract miles & minutes for smarter AI
        miles = None
        minutes = None
        # e.g., "about 18 mins by car (12.3 mi)"
        import re
        m = re.search(r"\(([\d\.]+)\s*mi\)", dm_text)
        if m:
            miles = float(m.group(1))
        m2 = re.search(r"about\s+(\d+)\s*min", dm_text, re.I)
        if m2:
            minutes = int(m2.group(1))
        dist = {"miles": miles, "minutes": minutes} if (miles or minutes) else None
        return dest_query, dist
    except Exception as e:
        logging.info(f"[distance] legacy path failed: {e}")
        return dest_query, None

def _kid_friendly_recs(listing_ctx: Dict[str, Any], guest_text: str) -> Optional[List[Dict[str, Any]]]:
    """
    Lightweight “kids” detector + nearby picks if you have _nearby in places.py.
    Otherwise returns None (no-op).
    """
    t = (guest_text or "").lower()
    if not any(k in t for k in ("kid", "kids", "children", "child", "family")):
        return None
    lat = listing_ctx.get("lat")
    lng = listing_ctx.get("lng")
    if lat is None or lng is None:
        return None
    try:
        from places import _nearby  # optional helper in your file
    except Exception:
        return None

    picks: List[Dict[str, Any]] = []
    try:
        # Start with playgrounds near the listing
        results = _nearby(float(lat), float(lng), keyword="playground", radius=6000, max_results=6)
        # Fallbacks if too thin
        if len(results) < 2:
            results += _nearby(float(lat), float(lng), keyword="zoo", radius=15000, max_results=3)
        if len(results) < 2:
            results += _nearby(float(lat), float(lng), keyword="aquarium", radius=15000, max_results=3)
        if len(results) < 2:
            results += _nearby(float(lat), float(lng), keyword="trampoline park", radius=15000, max_results=3)

        seen = set()
        for r in results:
            name = r.get("name")
            if not name:
                continue
            key = name.lower()
            if key in seen:
                continue
            seen.add(key)
            picks.append({"name": name, "maps_url": r.get("maps_url")})
            if len(picks) >= 3:
                break
        return picks or None
    except Exception:
        return None

def _compose_ai_context(base_ctx: Dict[str, Any], named_place, distance, kid_recs):
    ctx = dict(base_ctx)  # shallow copy
    if named_place:
        ctx["named_place"] = named_place
    if distance:
        ctx["distance"] = distance  # {"miles": X, "minutes": Y}
    if kid_recs:
        ctx["kid_recs"] = kid_recs
    return ctx

# ----------------------------- Routes -----------------------------

@app.get("/")
async def root():
    return {"ok": True, "service": "hostaway-autoresponder"}

@app.get("/healthz")
async def healthz():
    def present(name: str) -> str:
        v = os.getenv(name)
        return "SET" if v and len(v) > 3 else "MISSING"

    checks = {
        "SLACK_SIGNING_SECRET": present("SLACK_SIGNING_SECRET"),
        "SLACK_BOT_TOKEN": present("SLACK_BOT_TOKEN"),
        "OPENAI_API_KEY": present("OPENAI_API_KEY"),
        "OPENAI_MODEL": os.getenv("OPENAI_MODEL") or "default",
        "GOOGLE_PLACES_API_KEY": present("GOOGLE_PLACES_API_KEY"),
        "GOOGLE_DISTANCE_MATRIX_API_KEY": os.getenv("GOOGLE_DISTANCE_MATRIX_API_KEY") and "SET" or "MISSING/using PLACES key",
        "HOSTAWAY_CLIENT_ID": present("HOSTAWAY_CLIENT_ID"),
        "HOSTAWAY_CLIENT_SECRET": present("HOSTAWAY_CLIENT_SECRET"),
    }
    hints = []
    if checks["GOOGLE_PLACES_API_KEY"] == "MISSING":
        hints.append("Set GOOGLE_PLACES_API_KEY for place search.")
    if checks["HOSTAWAY_CLIENT_ID"] == "MISSING" or checks["HOSTAWAY_CLIENT_SECRET"] == "MISSING":
        hints.append("Set HOSTAWAY_CLIENT_ID and HOSTAWAY_CLIENT_SECRET for Hostaway messaging.")
    if checks["SLACK_SIGNING_SECRET"] == "MISSING":
        hints.append("Set SLACK_SIGNING_SECRET so Slack events/actions verify.")

    status = 200 if not [k for k, v in checks.items() if v == "MISSING"] else 500
    return JSONResponse({"status": "ok" if status == 200 else "missing_env", "checks": checks, "hints": hints}, status_code=status)

# ---------------- Slack endpoints (keep prior behavior) ----------------

@app.post("/slack/events")
async def slack_events(request: Request):
    # Slack URL verification challenge
    try:
        body = await request.json()
        if body.get("type") == "url_verification":
            return JSONResponse({"challenge": body.get("challenge")})
    except Exception:
        body = {}

    # Verify signature on raw body
    raw = await request.body()
    ts = request.headers.get("X-Slack-Request-Timestamp", "")
    sig = request.headers.get("X-Slack-Signature", "")
    if verify_slack_signature:
        if not verify_slack_signature(ts, sig, raw):
            raise HTTPException(status_code=403, detail="invalid slack signature")

    # You can expand event handling here as needed
    return JSONResponse({"ok": True})

@app.post("/slack/actions")
async def slack_actions(request: Request):
    # Slack sends application/x-www-form-urlencoded with "payload"
    raw = await request.body()
    ts = request.headers.get("X-Slack-Request-Timestamp", "")
    sig = request.headers.get("X-Slack-Signature", "")
    if verify_slack_signature:
        if not verify_slack_signature(ts, sig, raw):
            raise HTTPException(status_code=403, detail="invalid slack signature")

    form = await request.form()
    payload = form.get("payload")
    try:
        payload = json.loads(payload) if payload else {}
    except Exception:
        payload = {}

    # If you embed channel/ts in private_metadata, you can update the card here
    # meta = json.loads(payload.get("view", {}).get("private_metadata") or "{}")
    return JSONResponse({"ok": True})

# --------------- Hostaway unified webhook (primary entry) ---------------

@app.post("/hostaway/webhook")
async def hostaway_webhook(request: Request):
    """
    Handles new incoming guest messages (conversation webhook).
    Builds context, resolves requested destination (if any), computes distance,
    and sends a concise, on-topic AI reply back to Hostaway.
    """
    try:
        data = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="invalid json")

    guest_message = (data.get("message") or data.get("body") or "").strip()
    if not guest_message:
        raise HTTPException(status_code=400, detail="missing guest message")

    # Conversation / reservation identifiers (names vary by event)
    conversation_id = data.get("conversationId") or data.get("conversation_id") or _safe_get(data, "conversation", "id")
    if not conversation_id:
        raise HTTPException(status_code=400, detail="missing conversation id")

    # Try to capture reservation context for dates/guests
    res = data.get("reservation") or {}
    if not res and data.get("reservationId"):
        try:
            fetched_res = fetch_hostaway_reservation(int(data["reservationId"]))
            res = (fetched_res or {}).get("result") or fetched_res or {}
        except Exception:
            res = {}

    # Build listing context from payload; if missing lat/lng, _extract handles fetch by listingMapId
    listing_ctx = _extract_listing_context_from_payload(data)

    # Try to resolve a named destination + compute distance (miles/minutes)
    place_name, distance = _resolve_named_place_and_distance(listing_ctx, guest_message)

    # Optional: kid-friendly picks if guest asked
    kid_recs = _kid_friendly_recs(listing_ctx, guest_message)

    # Prepare context for AI
    base_ctx: Dict[str, Any] = {
        "phase": data.get("phase") or "in_stay",
        "guest_message": guest_message,
        "listing": listing_ctx,  # {address, city, state, lat, lng}
        "reservation": {
            "adults": res.get("adults"),
            "children": res.get("children"),
            "arrivalDate": res.get("arrivalDate"),
            "departureDate": res.get("departureDate"),
        },
    }
    ctx = _compose_ai_context(base_ctx, place_name, distance, kid_recs)

    # Generate reply
    reply_text = ""
    try:
        if generate_reply:
            reply_text = generate_reply(ctx, guest_message)
        if not reply_text:
            # fall back to your existing simpler suggester
            reply_text, _intent = make_suggested_reply(guest_message, {
                "location": {"lat": listing_ctx.get("lat"), "lng": listing_ctx.get("lng")},
                "listing": listing_ctx,
                "reservation": base_ctx["reservation"],
                "distance": distance,
                "named_place": place_name,
                "kid_recs": kid_recs,
            })
    except Exception as e:
        logging.warning(f"[AI] generation failed: {e}")
        # Hard fallback if everything else fails
        parts = []
        if place_name and distance and (distance.get("miles") or distance.get("minutes")):
            mm = []
            if distance.get("miles") is not None:
                mm.append(f"{distance['miles']} miles")
            if distance.get("minutes") is not None:
                mm.append(f"{distance['minutes']} minutes")
            parts.append(f"{place_name} is about {' / '.join(mm)} from the home.")
        if kid_recs:
            names = ", ".join(r["name"] for r in kid_recs[:3])
            parts.append(f"For kids nearby, popular spots include {names}.")
        reply_text = " ".join(parts) or "Happy to help with directions and kid-friendly ideas nearby."

    # Send reply to Hostaway
    ok = send_reply_to_hostaway(str(conversation_id), reply_text, communication_type="email")
    if not ok:
        raise HTTPException(status_code=502, detail="failed to send reply to hostaway")

    # Optional: Slack confirmation card
    try:
        body = f"*Conversation:* `{conversation_id}`\n*Message:*\n{reply_text}"
        slack_notify("Guest reply sent ✅", blocks=build_sent_block("Sent reply to guest", body))
    except Exception:
        pass

    return JSONResponse({"ok": True})

# If you prefer mounting an APIRouter, uncomment:
# app.include_router(router)

# ---------------------- Run local (Render uses Gunicorn) ----------------------
if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=int(os.getenv("PORT", "5000")))
