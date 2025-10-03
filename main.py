# file: main.py
import os
import json
import logging
from typing import Any, Dict, Optional, Tuple, List
from smart_intel import generate_reply, _smart_generate_reply

from fastapi import FastAPI, APIRouter, Request, HTTPException
from fastapi.responses import JSONResponse, PlainTextResponse

# ---------------- Env & logging ----------------
logging.basicConfig(level=logging.INFO)
app = FastAPI()

SLACK_BOT_TOKEN = os.getenv("SLACK_BOT_TOKEN", "")
SLACK_CHANNEL = os.getenv("SLACK_CHANNEL", "")

# ---------------- Slack SDK (optional at import time) ----------------
try:
    from slack_sdk import WebClient
    from slack_sdk.errors import SlackApiError
except Exception:
    WebClient = None
    SlackApiError = Exception

slack_client = WebClient(token=SLACK_BOT_TOKEN) if (SLACK_BOT_TOKEN and WebClient) else None

# ---------------- Local modules (all optional, with safe fallbacks) ----------------
try:
    from utils import (
        make_suggested_reply,
        fetch_hostaway_listing,
        fetch_hostaway_reservation,
        fetch_hostaway_conversation,
        extract_destination_from_message,
        get_distance_drive_time,
        route_message,
        send_reply_to_hostaway,
    )
except Exception:
    def make_suggested_reply(*args, **kwargs): return ("Thanks for reaching out—happy to help!", "general")
    def fetch_hostaway_listing(*args, **kwargs): return {}
    def fetch_hostaway_reservation(*args, **kwargs): return {}
    def fetch_hostaway_conversation(*args, **kwargs): return {}
    def extract_destination_from_message(*args, **kwargs): return None
    def get_distance_drive_time(*args, **kwargs): return ""
    def route_message(*args, **kwargs): return {"primary_intent": "other"}
    def send_reply_to_hostaway(*args, **kwargs): return False

try:
    from db import already_processed, mark_processed, log_ai_exchange
except Exception:
    def already_processed(key: str) -> bool: return False
    def mark_processed(key: str) -> None: return None
    def log_ai_exchange(*args, **kwargs): return None

try:
    from places import text_search_place, get_drive_distance_duration
except Exception:
    text_search_place = None
    get_drive_distance_duration = None

try:
    from places import build_local_recs, should_fetch_local_recs
except Exception:
    def build_local_recs(*args, **kwargs): return []
    def should_fetch_local_recs(*args, **kwargs): return False

try:
    from slack_interactivity import router as slack_router
except Exception:
    slack_router = APIRouter()

app.include_router(slack_router, prefix="/slack")

# ---------------- Small helpers ----------------
def _safe_get(d: Dict[str, Any], *keys, default=None):
    cur = d
    for k in keys:
        if not isinstance(cur, dict):
            return default
        cur = cur.get(k)
        if cur is None:
            return default
    return cur

def _fmt_date(d: Optional[str]) -> str:
    if not d:
        return "N/A"
    from datetime import datetime
    for fmt in ("%Y-%m-%d", "%Y-%m-%dT%H:%M:%S%z", "%Y-%m-%dT%H:%M:%S", "%m/%d/%Y"):
        try:
            return datetime.strptime(d, fmt).strftime("%m-%d-%Y")
        except Exception:
            continue
    return d

def _extract_listing_context_from_payload(data: Dict[str, Any]) -> Dict[str, Any]:
    ctx: Dict[str, Any] = {"address": None, "city": None, "state": None, "zipcode": None, "lat": None, "lng": None, "name": None}
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

def _resolve_named_place_and_distance(listing_ctx: Dict[str, Any], guest_text: str) -> Tuple[Optional[str], Optional[Dict[str, Any]]]:
    lat = listing_ctx.get("lat"); lng = listing_ctx.get("lng")
    if lat is None or lng is None:
        return None, None
    dest_query = extract_destination_from_message(guest_text) or ""
    if not dest_query:
        return None, None
    if text_search_place and get_drive_distance_duration:
        try:
            city = listing_ctx.get("city"); state = listing_ctx.get("state")
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
            logging.info(f"[distance] robust path failed, fallback. err={e}")
    try:
        dm_text = get_distance_drive_time(float(lat), float(lng), dest_query)
        import re
        miles = None; minutes = None
        m = re.search(r"\(([\d\.]+)\s*mi\)", dm_text)
        if m:
            miles = float(m.group(1))
        m2 = re.search(r"about\s+(\d+)\s*min", dm_text, re.I)
        if m2:
            minutes = int(m2.group(1))
        dist = {"miles": miles, "minutes": minutes} if (miles or minutes) else None
        return dest_query, dist
    except Exception:
        return dest_query, None

def _post_initial_slack_card(
    *, channel: str, guest_message: str, ai_suggestion: str, meta: Dict[str, Any]
) -> Optional[Dict[str, Any]]:
    if not slack_client or not channel:
        logging.warning("Slack not configured; skipping card.")
        return None

    header_text = (
        f"*✉️ Message from {meta.get('guest_name','Guest')}*\n"
        f"🏡 *Property:* {meta.get('property_address','Unknown Address')}\n"
        f"📅 *Dates:* {meta.get('check_in','N/A')} → {meta.get('check_out','N/A')}\n"
        f"👥 *Guests:* {meta.get('guest_count','?')} | Res: *{meta.get('status','N/A')}* | "
        f"Price: *{meta.get('price_str','$N/A')}* | Platform: *{meta.get('platform','Unknown')}*\n\n"
        f"{guest_message}"
    )

    # Make button payloads robust: include BOTH old and new key names
    send_payload = {
        "conv_id": meta.get("conv_id"),
        "conversation_id": meta.get("conv_id"),
        "reply": ai_suggestion,
        "reply_text": ai_suggestion,
        "guest_message": guest_message,
        "type": meta.get("type", "email"),
    }
    edit_payload = {
        "guest_name": meta.get("guest_name", "Guest"),
        "guest_message": guest_message,
        "draft_text": ai_suggestion,
        # add the missing conv_id so modal can send
        "conv_id": meta.get("conv_id"),
        # carry useful context
        "listing_id": meta.get("listing_id"),
        "reservation_id": meta.get("reservation_id"),
        "status": meta.get("status"),
        "check_in": meta.get("check_in"),
        "check_out": meta.get("check_out"),
        "guest_count": meta.get("guest_count"),
        "property_address": meta.get("property_address"),
        "property_name": meta.get("property_name"),
        "type": meta.get("type", "email"),
    }
    portal_payload = {
        "conv_id": meta.get("conv_id"),
        "status": (meta.get("status") or "").lower(),
        "guest_portal_url": meta.get("guest_portal_url"),
        "type": meta.get("type", "email"),
    }

    blocks: List[Dict[str, Any]] = [
        {"type": "section", "text": {"type": "mrkdwn", "text": header_text}},
        {"type": "section", "text": {"type": "mrkdwn", "text": f"💡 *Suggested Reply:*\n{ai_suggestion}"}},
        {
            "type": "actions",
            "block_id": "action_buttons",
            "elements": [
                {
                    "type": "button",
                    "text": {"type": "plain_text", "text": "Send"},
                    "style": "primary",
                    "action_id": "send",  # support new id
                    "value": json.dumps(send_payload)
                },
                {
                    "type": "button",
                    "text": {"type": "plain_text", "text": "Edit"},
                    "action_id": "open_edit_modal",
                    "value": json.dumps(edit_payload)
                },
                {
                    "type": "button",
                    "text": {"type": "plain_text", "text": "Send Guest Portal"},
                    "action_id": "send_guest_portal",
                    "value": json.dumps(portal_payload)
                }
            ]
        }
    ]

    try:
        resp = slack_client.chat_postMessage(channel=channel, blocks=blocks, text="New guest message")
        return resp.data if hasattr(resp, "data") else resp
    except SlackApiError as e:
        logging.error(f"[slack] chat_postMessage failed: {e.response.data if hasattr(e,'response') else e}")
        return None

# ---------------- Tolerant Hostaway payload reader ----------------
async def _read_hostaway_payload(request: Request) -> Dict[str, Any]:
    try:
        body = await request.json()
        if isinstance(body, dict):
            return body
    except Exception:
        pass
    raw = await request.body()
    if raw:
        s = raw.decode("utf-8", "ignore").strip()
        if s.startswith("{") and s.endswith("}"):
            try:
                return json.loads(s)
            except Exception:
                pass
    try:
        form = await request.form()
        for key in ("payload", "data", "event", "body"):
            if key in form and form[key]:
                val = form[key]
                if isinstance(val, (str, bytes)):
                    if isinstance(val, bytes):
                        val = val.decode("utf-8", "ignore")
                    val = val.strip()
                    if val.startswith("{"):
                        try:
                            return json.loads(val)
                        except Exception:
                            try:
                                inner = json.loads(json.loads(val))
                                if isinstance(inner, dict):
                                    return inner
                            except Exception:
                                pass
        if len(form.keys()) == 1:
            only_key = list(form.keys())[0]
            val = form[only_key]
            if isinstance(val, str) and val.strip().startswith("{"):
                try:
                    return json.loads(val)
                except Exception:
                    pass
    except Exception:
        pass
    return {}

# ---------------- Routes ----------------
@app.get("/")
async def root():
    return {"ok": True, "service": "hostaway-autoreply"}

@app.get("/ping")
async def ping():
    return PlainTextResponse("ok")

@app.get("/healthz")
async def healthz():
    def present(name: str) -> str:
        v = os.getenv(name)
        return "SET" if v and len(v) > 2 else "MISSING"

    checks = {
        "SLACK_BOT_TOKEN": present("SLACK_BOT_TOKEN"),
        "SLACK_CHANNEL": "SET" if SLACK_CHANNEL else "MISSING",
        "OPENAI_API_KEY": present("OPENAI_API_KEY"),
        "GOOGLE_PLACES_API_KEY": present("GOOGLE_PLACES_API_KEY"),
        "HOSTAWAY_CLIENT_ID": present("HOSTAWAY_CLIENT_ID"),
        "HOSTAWAY_CLIENT_SECRET": present("HOSTAWAY_CLIENT_SECRET"),
    }
    status = 200 if not [k for k, v in checks.items() if v == "MISSING"] else 500
    return JSONResponse({"status": "ok" if status == 200 else "missing_env", "checks": checks}, status_code=status)

@app.post("/unified-webhook")
async def unified_webhook(request: Request):
    payload = await _read_hostaway_payload(request)
    if not payload:
        logging.warning("[webhook] empty/invalid payload accepted (ignored).")
        return {"status": "ignored"}

    event = payload.get("event")
    obj = payload.get("object")
    data = payload.get("data") or {}

    if event != "message.received" or obj != "conversationMessage":
        return {"status": "ignored"}

    event_key = f"{obj}:{event}:{data.get('id') or data.get('conversationId')}"
    if already_processed(event_key):
        return {"status": "duplicate"}

    guest_message = data.get("body") or payload.get("body") or ""
    if not guest_message:
        mark_processed(event_key)
        return {"status": "ignored"}

    conv_id = data.get("conversationId")
    reservation_id = data.get("reservationId")
    listing_id = data.get("listingMapId")

    reservation = fetch_hostaway_reservation(reservation_id) or {}
    res_data = reservation.get("result", {}) or {}
    guest_name = res_data.get("guestFirstName") or res_data.get("guest", {}).get("firstName") or "Guest"
    check_in = res_data.get("arrivalDate")
    check_out = res_data.get("departureDate")

    conversation = fetch_hostaway_conversation(conv_id) or {}
    messages = conversation.get("result", {}).get("conversationMessages", [])[-10:]
    history = [
        {"role": ("guest" if m.get("isIncoming") else "host"), "text": m.get("body", "")}
        for m in messages if m.get("body")
    ]

    listing = fetch_hostaway_listing(listing_id) or {}
    listing_data = listing.get("result", {}) or {}

    nearby_places: List[Dict[str, Any]] = []
    lat = listing_data.get("lat")
    lng = listing_data.get("lng")
    if should_fetch_local_recs(guest_message) and lat and lng:
        try:
            nearby_places = build_local_recs(lat, lng, guest_message)
        except Exception as e:
            logging.warning(f"[recs] nearby recs failed: {e}")

    listing_ctx = {
        "lat": lat, "lng": lng,
        "city": listing_data.get("city"), "state": listing_data.get("state"),
        "address": listing_data.get("address"), "name": listing_data.get("name"),
    }
    place_name, distance = _resolve_named_place_and_distance(listing_ctx, guest_message)

    structured_listing_info = {
        "name": listing_data.get("name"),
        "address": listing_data.get("address"),
        "bedrooms": listing_data.get("bedroomsNumber"),
        "beds": listing_data.get("bedsNumber"),
        "bathrooms": listing_data.get("bathroomsNumber"),
        "amenities": listing_data.get("listingAmenities", []),
        "bed_types": listing_data.get("listingBedTypes", []),
        "check_in_time": listing_data.get("checkInTimeStart"),
        "check_out_time": listing_data.get("checkOutTime"),
        "wifi_username": listing_data.get("wifiUsername"),
        "wifi_password": listing_data.get("wifiPassword"),
        "latitude": listing_data.get("lat"),
        "longitude": listing_data.get("lng"),
        "description": listing_data.get("description"),
        "house_rules": listing_data.get("houseRules"),
    }

    ai_context = {
        "guest_name": guest_name,
        "check_in_date": check_in,
        "check_out_date": check_out,
        "listing_info": structured_listing_info,
        "reservation": res_data,
        "history": history,
        "nearby_places": nearby_places,
    }
    if place_name and distance:
        ai_context["named_place"] = place_name
        ai_context["distance"] = distance

    ai_reply = ""
    try:
        if generate_reply:
            ai_reply = generate_reply(guest_message, ai_context) or ""
    except Exception as e:
        logging.warning(f"[AI] generate_reply failed: {e}")
    if not ai_reply:
        try:
            ai_reply, _ = make_suggested_reply(guest_message, {
                "location": {"lat": lat, "lng": lng},
                "listing": listing_ctx,
                "reservation": {"arrivalDate": check_in, "departureDate": check_out},
                "distance": distance,
                "named_place": place_name,
            })
        except Exception:
            ai_reply = "Thanks for reaching out—happy to help. I’ll get you the details shortly."

    try:
        log_ai_exchange(
            conversation_id=str(conv_id),
            guest_message=guest_message,
            ai_suggestion=ai_reply,
            intent=(route_message(guest_message) or {}).get("primary_intent", "general"),
        )
    except Exception:
        pass

    checkin_fmt = _fmt_date(check_in)
    checkout_fmt = _fmt_date(check_out)

    price = res_data.get("grandTotalPrice") or res_data.get("totalPrice") or res_data.get("price") or "N/A"
    try:
        price_float = float(str(price))
        price_str = f"${price_float:,.2f}"
    except Exception:
        price_str = "$N/A"

    channel_map = {
        2018: "Airbnb", 2002: "Vrbo", 2005: "Booking.com", 2007: "Expedia",
        2009: "Vrbo (iCal)", 2010: "Vrbo (iCal)", 2000: "Direct", 2013: "Booking Engine",
        2015: "Custom iCal", 2016: "Tripadvisor (iCal)", 2017: "WordPress", 2019: "Marriott",
        2020: "Partner", 2021: "GDS", 2022: "Google",
    }
    platform = channel_map.get(res_data.get("channelId"), "Unknown")
    guest_count = res_data.get("numberOfGuests") or res_data.get("adults") or "?"

    meta = {
        "conv_id": conv_id,
        "guest_name": guest_name,
        "property_address": listing_data.get("address") or "Unknown Address",
        "property_name": listing_data.get("name"),
        "check_in": checkin_fmt,
        "check_out": checkout_fmt,
        "guest_count": guest_count,
        "status": res_data.get("status", "N/A"),
        "price_str": price_str,
        "platform": platform,
        "listing_id": listing_id,
        "reservation_id": reservation_id,
        "type": "email",
        "guest_portal_url": res_data.get("guestPortalUrl") or res_data.get("portalUrl"),
    }

    if SLACK_CHANNEL:
        _post_initial_slack_card(
            channel=SLACK_CHANNEL,
            guest_message=guest_message,
            ai_suggestion=ai_reply,
            meta=meta,
        )
    else:
        logging.warning("SLACK_CHANNEL not set; skipping Slack post.")

    mark_processed(event_key)
    return {"status": "ok"}

# ---------------- Local dev runner ----------------
if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=int(os.getenv("PORT", "5000")))
