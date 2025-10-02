# file: main.py
import os
import json
import logging
from typing import Any, Dict, Optional, Tuple, List

from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import JSONResponse
from starlette.responses import PlainTextResponse  # ✅ fixes /ping NameError

# Slack SDK (used only to post the initial card)
try:
    from slack_sdk import WebClient
    from slack_sdk.errors import SlackApiError
except Exception:
    WebClient = None
    SlackApiError = Exception

# --- Local imports ---
from utils import (
    make_suggested_reply,
    fetch_hostaway_listing,
    fetch_hostaway_reservation,
    extract_destination_from_message,
    resolve_place_textsearch,      # legacy (kept)
    get_distance_drive_time,       # legacy (kept)
    route_message,                 # quick intent tag for header
)

# Improved Google helpers (bias + robust distance). Optional.
try:
    from places import text_search_place, get_drive_distance_duration
except Exception:
    text_search_place = None
    get_drive_distance_duration = None

# Slack interactivity (events + actions live there). Mounted at /slack
try:
    from slack_interactivity import (
        router as slack_router,
        build_rich_header_blocks,   # rich header for the Slack card
    )
except Exception as e:
    slack_router = None
    logging.warning(f"Slack interactivity router not available: {e}")

    def build_rich_header_blocks(**kwargs):
        meta = kwargs.get("meta", {}) or {}
        guest_msg = kwargs.get("guest_msg", "")
        sent_reply = kwargs.get("sent_reply")
        lines = [
            {"type": "header", "text": {"type": "plain_text", "text": "Guest Message", "emoji": True}},
            {"type": "section", "text": {"type": "mrkdwn", "text": f"*Guest:* {meta.get('guest_name','Guest')}"}},
            {"type": "section", "text": {"type": "mrkdwn", "text": f"> {guest_msg}"}},
        ]
        if sent_reply is not None:
            lines += [{"type": "section", "text": {"type": "mrkdwn", "text": f"*Sent Reply:*\n>{sent_reply}"}}]
        return lines

# Optional smarter writer
try:
    from smart_intel import generate_reply
except Exception:
    generate_reply = None

logging.basicConfig(level=logging.INFO)
app = FastAPI()

# ✅ Slack must call /slack/events and /slack/actions
if slack_router:
    app.include_router(slack_router, prefix="/slack")

SLACK_BOT_TOKEN = os.getenv("SLACK_BOT_TOKEN", "")
SLACK_CHANNEL = os.getenv("SLACK_CHANNEL", "")  # channel ID to post the initial card
slack_client = WebClient(token=SLACK_BOT_TOKEN) if (SLACK_BOT_TOKEN and WebClient) else None


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
    If missing, try to fetch by listingMapId using fetch_hostaway_listing.
    """
    ctx: Dict[str, Any] = {
        "address": None, "city": None, "state": None, "zipcode": None, "lat": None, "lng": None, "name": None
    }

    listing = data.get("listing") or data.get("property") or {}
    for k in ("address", "city", "state", "zipcode", "lat", "lng", "street", "name"):
        v = listing.get(k)
        if v not in (None, ""):
            if k == "street" and not ctx["address"]:
                ctx["address"] = v
            elif k in ctx:
                ctx[k] = v

    for k in ("address", "city", "state", "zipcode", "lat", "lng", "street", "name"):
        if ctx.get("address") is None and k == "street" and data.get("street"):
            ctx["address"] = data.get("street")
        elif ctx.get(k) in (None, "") and data.get(k) not in (None, ""):
            ctx[k] = data.get(k)

    listing_id = data.get("listingMapId") or _safe_get(data, "reservation", "listingMapId")
    if (ctx.get("lat") is None or ctx.get("lng") is None or not ctx.get("name")) and listing_id:
        try:
            fetched = fetch_hostaway_listing(int(listing_id))
            fetched_result = (fetched or {}).get("result") or {}
            for k in ("address", "city", "state", "zipcode", "lat", "lng", "street", "name"):
                fv = fetched_result.get(k)
                if k == "street" and fv and not ctx.get("address"):
                    ctx["address"] = fv
                elif k in ctx and fv not in (None, ""):
                    ctx[k] = fv
        except Exception as e:
            logging.info(f"[ctx] listing fetch failed for id={listing_id}: {e}")

    if not ctx.get("address"):
        if ctx.get("city") or ctx.get("state"):
            ctx["address"] = ", ".join([p for p in [ctx.get("city"), ctx.get("state")] if p]) or None

    return ctx

def _normalize_named_place(raw: Optional[str]) -> Optional[str]:
    if not raw:
        return None
    t = raw.strip()
    if t.lower().replace(".", "").strip().startswith("heb center"):
        return "H-E-B Center"
    return t

def _resolve_named_place_and_distance(listing_ctx: Dict[str, Any], guest_text: str) -> Tuple[Optional[str], Optional[Dict[str, Any]]]:
    lat = listing_ctx.get("lat")
    lng = listing_ctx.get("lng")
    city = listing_ctx.get("city")
    state = listing_ctx.get("state")
    if lat is None or lng is None:
        return None, None

    dest_query = extract_destination_from_message(guest_text) or ""
    dest_query = _normalize_named_place(dest_query)
    if not dest_query:
        return None, None

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

    try:
        dm_text = get_distance_drive_time(float(lat), float(lng), dest_query)
        import re
        miles = None
        minutes = None
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

def _compose_ai_context(base_ctx: Dict[str, Any], named_place, distance) -> Dict[str, Any]:
    ctx = dict(base_ctx)
    if named_place:
        ctx["named_place"] = named_place
    if distance:
        ctx["distance"] = distance
    return ctx

def _detect_intent_label(guest_message: str) -> str:
    try:
        r = route_message(guest_message)
        return r.get("primary_intent", "other")
    except Exception:
        return "other"

# ------------- Slack helpers: build initial card with actions -------------

def _action_button(action_id: str, text: str, meta: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "type": "button",
        "action_id": action_id,
        "text": {"type": "plain_text", "text": text, "emoji": True},
        "value": json.dumps(meta),
    }

def _action_row(meta: Dict[str, Any]) -> List[Dict[str, Any]]:
    elems = [
        _action_button("edit", "Edit", meta),
        _action_button("write_own", "Write own", meta),
        _action_button("send", "Send", meta),
    ]
    if meta.get("guest_portal_url") or meta.get("guestPortalUrl"):
        elems.append(_action_button("send_guest_portal", "Send guest portal", meta))
    return [{"type": "actions", "elements": elems}]

def _feedback_row(meta: Dict[str, Any]) -> List[Dict[str, Any]]:
    elems = [
        _action_button("rate_up", "👍 Useful", meta),
        _action_button("rate_down", "👎 Needs work", meta),
    ]
    return [{"type": "actions", "elements": elems}]

def _post_initial_slack_card(
    *,
    channel: str,
    guest_message: str,
    ai_suggestion: str,
    meta: Dict[str, Any],
    detected_intent: Optional[str],
) -> Optional[Dict[str, Any]]:
    if not slack_client or not channel:
        return None

    header_blocks = build_rich_header_blocks(
        meta=meta,
        guest_msg=guest_message,
        sent_reply=None,
        detected_intent=detected_intent,
        sent_label=None,
        saved_for_learning=False,
    )
    suggestion_block = [{
        "type": "section",
        "text": {"type": "mrkdwn", "text": f"*AI suggestion:*\n>{ai_suggestion}"},
    }]

    blocks = header_blocks + [{"type": "divider"}] + suggestion_block + _action_row(meta) + _feedback_row(meta)

    try:
        resp = slack_client.chat_postMessage(channel=channel, text="New guest message", blocks=blocks)
        return resp.data if hasattr(resp, "data") else resp
    except SlackApiError as e:
        logging.error(f"[slack] post card failed: {e.response.data if hasattr(e,'response') else e}")
        return None


# ----------------------------- Routes -----------------------------

@app.get("/")
async def root():
    return {"ok": True, "service": "hostaway-autoresponder"}

# ✅ Render health probe hits GET /ping — must return 200
@app.get("/ping")
async def ping():
    return PlainTextResponse("ok")

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
        "SLACK_CHANNEL": "SET" if SLACK_CHANNEL else "MISSING",
    }
    hints = []
    if checks["GOOGLE_PLACES_API_KEY"] == "MISSING":
        hints.append("Set GOOGLE_PLACES_API_KEY for place search.")
    if checks["HOSTAWAY_CLIENT_ID"] == "MISSING" or checks["HOSTAWAY_CLIENT_SECRET"] == "MISSING":
        hints.append("Set HOSTAWAY_CLIENT_ID and HOSTAWAY_CLIENT_SECRET for Hostaway messaging.")
    if checks["SLACK_CHANNEL"] == "MISSING":
        hints.append("Set SLACK_CHANNEL to post initial Slack cards.")

    status = 200 if not [k for k, v in checks.items() if v == "MISSING"] else 500
    return JSONResponse({"status": "ok" if status == 200 else "missing_env", "checks": checks, "hints": hints}, status_code=status)

# --------------- Hostaway unified webhook ---------------

@app.post("/hostaway/webhook")
async def hostaway_webhook(request: Request):
    """
    Handles new incoming guest messages (conversation webhook).
    Builds context, computes distance if applicable, generates a *suggested* reply,
    and posts a Slack card (rich header + actions). Sending to guest happens via Slack actions.
    """
    try:
        data = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="invalid json")

    guest_message = (data.get("message") or data.get("body") or "").strip()
    if not guest_message:
        raise HTTPException(status_code=400, detail="missing guest message")

    conversation_id = data.get("conversationId") or data.get("conversation_id") or _safe_get(data, "conversation", "id")
    if not conversation_id:
        raise HTTPException(status_code=400, detail="missing conversation id")

    # Reservation context (for header)
    res = data.get("reservation") or {}
    if not res and data.get("reservationId"):
        try:
            fetched_res = fetch_hostaway_reservation(int(data["reservationId"]))
            res = (fetched_res or {}).get("result") or fetched_res or {}
        except Exception:
            res = {}

    # Listing context (for header + distances)
    listing_ctx = _extract_listing_context_from_payload(data)

    # Optional “how far” distance add-on
    place_name, distance = _resolve_named_place_and_distance(listing_ctx, guest_message)

    # Prepare AI ctx for generate_reply (smart_intel) – (guest_message, ctx)
    base_ctx: Dict[str, Any] = {
        "guest_name": data.get("guest", {}).get("firstName") or data.get("guestName") or "Guest",
        "listing_info": {
            "name": listing_ctx.get("name"),
            "address": {"address1": listing_ctx.get("address"), "city": listing_ctx.get("city"), "state": listing_ctx.get("state")},
            "bedrooms": data.get("listing", {}).get("bedrooms"),
            "bathrooms": data.get("listing", {}).get("bathrooms"),
            "beds": data.get("listing", {}).get("beds"),
        },
        "latitude": listing_ctx.get("lat"),
        "longitude": listing_ctx.get("lng"),
        "reservation": {
            "checkInDate": res.get("arrivalDate") or res.get("checkInDate"),
            "checkOutDate": res.get("departureDate") or res.get("checkOutDate"),
            "numberOfGuests": res.get("numberOfGuests") or res.get("guests") or res.get("adults"),
            "status": res.get("status"),
        },
        "nearby_places": [],
        "city": listing_ctx.get("city"),
        "state": listing_ctx.get("state"),
    }
    if distance and place_name:
        base_ctx["named_place"] = place_name
        base_ctx["distance"] = distance

    # Generate *suggested* reply (do not auto-send here)
    ai_suggestion = ""
    try:
        if generate_reply:
            ai_suggestion = generate_reply(guest_message, base_ctx) or ""
        if not ai_suggestion:
            ai_suggestion, _ = make_suggested_reply(guest_message, {
                "location": {"lat": listing_ctx.get("lat"), "lng": listing_ctx.get("lng")},
                "listing": listing_ctx,
                "reservation": base_ctx["reservation"],
                "distance": distance,
                "named_place": place_name,
            })
    except Exception as e:
        logging.warning(f"[AI] generation failed: {e}")
        ai_suggestion = "Got it—here’s a concise reply ready to send."

    # Build metadata for Slack action handlers (values read by slack_interactivity.py)
    def _intent():
        try:
            r = route_message(guest_message)
            return r.get("primary_intent", "other")
        except Exception:
            return "other"

    intent = _intent()
    meta: Dict[str, Any] = {
        "conv_id": conversation_id,
        "listing_id": data.get("listingMapId") or _safe_get(data, "reservation", "listingMapId") or "",
        "guest_id": data.get("guest", {}).get("id") or data.get("guestId") or "",
        "guest_name": base_ctx["guest_name"],
        "guest_message": guest_message,
        "ai_suggestion": ai_suggestion,
        "type": (data.get("channel") or data.get("communicationType") or "email"),
        "status": (res.get("status") or data.get("status") or "unknown"),
        "check_in": base_ctx["reservation"]["checkInDate"] or "N/A",
        "check_out": base_ctx["reservation"]["checkOutDate"] or "N/A",
        "guest_count": base_ctx["reservation"]["numberOfGuests"] or "N/A",
        "channel_pretty": data.get("source") or data.get("platform") or data.get("channelName") or None,
        "property_address": listing_ctx.get("address"),
        "property_name": listing_ctx.get("name"),
        "guest_portal_url": res.get("guestPortalUrl") or res.get("guest_portal_url"),
        "location": {"lat": listing_ctx.get("lat"), "lng": listing_ctx.get("lng")},
        "detected_intent": intent,
        # UI labels
        "sent_label": "message sent",
        "checkbox_checked": False,
        "coach_prompt": "",
    }

    if not SLACK_CHANNEL:
        logging.warning("SLACK_CHANNEL not set; skipping Slack post.")
    else:
        _post_initial_slack_card(
            channel=SLACK_CHANNEL,
            guest_message=guest_message,
            ai_suggestion=ai_suggestion,
            meta=meta,
            detected_intent=intent,
        )

    # Hostaway only needs a 200; sending to guest is handled via Slack “Send”.
    return JSONResponse({"ok": True, "posted_to_slack": bool(SLACK_CHANNEL)})

# ---------------------- Run local ----------------------
if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=int(os.getenv("PORT", "5000")))
