# path: main.py
import os
import time
import logging
import json
import sqlite3
from typing import List, Dict, Optional, Any
from datetime import date, datetime

from fastapi import FastAPI, Depends, HTTPException, Header, Query, Body
from pydantic import BaseModel

from smart_intel import generate_reply

from slack_sdk import WebClient
from slack_sdk.errors import SlackApiError

from places import should_fetch_local_recs, build_local_recs
from slack_interactivity import router as slack_router
from utils import (
    fetch_hostaway_listing,
    fetch_hostaway_reservation,
    fetch_hostaway_conversation,
)

from db import init_db as db_init
from db import (
    get_slack_thread as db_get_slack_thread,
    upsert_slack_thread as db_upsert_slack_thread,
    note_guest,
    already_processed,
    mark_processed,
    log_message_event,
    log_ai_exchange,
)

# ---------- App & logging ----------
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()
logging.basicConfig(level=getattr(logging, LOG_LEVEL, logging.INFO))
app = FastAPI()
app.include_router(slack_router, prefix="/slack")

# ---------- Env & config ----------
LEARNING_DB_PATH = os.getenv("LEARNING_DB_PATH", "learning.db")
ADMIN_TOKEN = os.getenv("ADMIN_TOKEN", "dev-token")
SHOW_NEW_GUEST_TAG = os.getenv("SHOW_NEW_GUEST_TAG", "0") in ("1", "true", "True", "yes", "YES")
DEBUG_CONVERSATION = os.getenv("DEBUG_CONVERSATION", "0") in ("1", "true", "True", "yes", "YES")
SLACK_SKIP_READS = os.getenv("SLACK_SKIP_READS", "1") in ("1", "true", "True", "yes", "YES")

REQUIRED_ENV_VARS = [
    "HOSTAWAY_CLIENT_ID",
    "HOSTAWAY_CLIENT_SECRET",
    "OPENAI_API_KEY",
    "SLACK_CHANNEL",
    "SLACK_BOT_TOKEN",
    "SLACK_SIGNING_SECRET",
]
_missing = [v for v in REQUIRED_ENV_VARS if not os.getenv(v)]
if _missing:
    raise RuntimeError(f"Missing required environment variables: {_missing}")

MAX_THREAD_MESSAGES = 10

# ---------- Slack helpers ----------
_SLACK_CHANNEL_ID: Optional[str] = None

def _hint_is_id(hint: str) -> bool:
    return bool(hint) and hint[0] in ("C", "G") and len(hint) > 8

def _ensure_bot_in_channel(client: WebClient, channel_id: str) -> None:
    try:
        client.conversations_join(channel=channel_id)
    except SlackApiError:
        pass

def _post_to_slack(client: WebClient, channel_hint: str, blocks: List[Dict[str, Any]], text: str) -> bool:
    chan_id = (_SLACK_CHANNEL_ID or (channel_hint or "").strip())
    if not _hint_is_id(chan_id):
        logging.error(
            "SLACK_CHANNEL must be a Slack channel ID (starts with 'C' or 'G'). Current=%r.",
            channel_hint,
        )
        return False
    try:
        client.chat_postMessage(channel=chan_id, blocks=blocks, text=text)
        return True
    except SlackApiError as e:
        err = getattr(e, "response", {}).data.get("error") if hasattr(e, "response") else None
        if err == "not_in_channel":
            _ensure_bot_in_channel(client, chan_id)
            try:
                client.chat_postMessage(channel=chan_id, blocks=blocks, text=text)
                return True
            except SlackApiError as e2:
                logging.error(f"Slack retry failed: {getattr(e2, 'response', {}).data if hasattr(e2,'response') else e2}")
                return False
        if err == "channel_not_found":
            logging.error("Slack error channel_not_found.")
            return False
        if err == "is_archived":
            logging.error("Slack channel is archived.")
            return False
        if err == "rate_limited":
            retry_after = 1
            try: retry_after = int(e.response.headers.get("Retry-After","1"))
            except Exception: pass
            time.sleep(max(retry_after, 1))
            try:
                client.chat_postMessage(channel=chan_id, blocks=blocks, text=text)
                return True
            except SlackApiError as e3:
                logging.error(f"Slack rate-limit retry failed: {getattr(e3,'response', {}).data if hasattr(e3,'response') else e3}")
                return False
        logging.error(f"Slack send error: {getattr(e, 'response', {}).data if hasattr(e, 'response') else e}")
        return False
    except Exception as e:
        logging.error(f"Unexpected Slack error: {e}")
        return False

# ---------- Optional legacy Slack threading ----------
def _get_thread_ts(conv_id: Optional[int | str]) -> Optional[str]:
    if not conv_id:
        return None
    rec = db_get_slack_thread(str(conv_id))
    return rec["ts"] if rec else None

def _set_thread_ts(conv_id: Optional[int | str], ts: str) -> None:
    if not conv_id or not ts:
        return
    channel = os.getenv("SLACK_CHANNEL") or ""
    db_upsert_slack_thread(str(conv_id), channel, ts)

# ---------- Helpers ----------
CHANNEL_ID_MAP = {
    2018: "Airbnb (Official)", 2002: "HomeAway", 2005: "Booking.com", 2007: "Expedia",
    2009: "HomeAway (iCal)", 2010: "Vrbo", 2000: "Direct", 2013: "Booking Engine",
    2015: "Custom iCal", 2016: "TripAdvisor (iCal)", 2017: "WordPress", 2019: "Marriott",
    2020: "Partner", 2021: "GDS", 2022: "Google",
}
COMM_TYPE_MAP = {
    "airbnb": "Airbnb", "airbnbofficial": "Airbnb (Official)", "vrbo": "Vrbo",
    "bookingcom": "Booking.com", "direct": "Direct", "expedia": "Expedia",
    "channel": "Channel", "email": "Email",
}
RES_STATUS_ALLOWED = {
    "new": "New", "modified": "Modified", "cancelled": "Cancelled", "ownerstay": "Owner Stay",
    "pending": "Pending", "awaitingpayment": "Awaiting Payment", "declined": "Declined",
    "expired": "Expired", "inquiry": "Inquiry", "inquirypreapproved": "Inquiry (Preapproved)",
    "inquirydenied": "Inquiry (Denied)", "inquirytimedout": "Inquiry (Timed Out)",
    "inquirynotpossible": "Inquiry (Not Possible)",
}
BOOKED_STATUSES = {"new", "modified"}

def format_us_date(d: str | None) -> str:
    if not d:
        return "N/A"
    s = str(d).strip()
    for fmt in ("%Y-%m-%d", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M:%S"):
        try:
            dt = datetime.strptime(s[:19], fmt)
            return dt.strftime("%m/%d/%Y")
        except Exception:
            pass
    return s

def format_price(amount, currency: str | None = "USD") -> str:
    try:
        val = float(amount)
    except (TypeError, ValueError):
        return "—"
    cur = (currency or "USD").upper()
    symbol = "$" if cur == "USD" else f"{cur} "
    return f"{symbol}{val:,.2f}"

def pretty_res_status(s: str | None) -> str:
    if not s:
        return "Unknown"
    key = str(s).strip().lower().replace("_", "").replace("-", "")
    return RES_STATUS_ALLOWED.get(key, s.capitalize())

def pretty_status(s: str | None) -> str:
    if not isinstance(s, str):
        return "Unknown"
    m = s.strip().replace("_", " ").replace("-", " ")
    return m[:1].upper() + m[1:] if m else "Unknown"

def channel_label_from(channel_id: Optional[int], communication_type: Optional[str]) -> str:
    if isinstance(channel_id, int) and channel_id in CHANNEL_ID_MAP:
        name = CHANNEL_ID_MAP[channel_id]
        if "vrbo" in name.lower():
            return "Vrbo"
        if "airbnb" in name.lower():
            return "Airbnb"
        return name
    if communication_type:
        key = str(communication_type).lower().strip()
        return COMM_TYPE_MAP.get(key, key.capitalize())
    return "Channel"

def trip_phase(check_in: str | None, check_out: str | None) -> str:
    try:
        ci = date.fromisoformat(str(check_in)) if check_in else None
        co = date.fromisoformat(str(check_out)) if check_out else None
    except Exception:
        return "unknown"
    today = date.today()
    if ci and today < ci:
        return "upcoming"
    if ci and co and ci <= today <= co:
        return "during"
    if co and today > co:
        return "past"
    return "unknown"

def extract_access_details(listing_obj: Optional[Dict], reservation_obj: Optional[Dict]) -> Dict[str, Optional[str]]:
    def _get(d: Dict[str, Any], *keys: str) -> Optional[str]:
        for k in keys:
            v = d.get(k)
            if isinstance(v, str) and v.strip():
                return v.strip()
        return None
    listing = (listing_obj or {}).get("result") or {}
    reservation = (reservation_obj or {}).get("result") or {}
    code = _get(reservation, "doorCode", "door_code", "accessCode", "checkInCode", "entryCode") \
        or _get(listing, "doorCode", "door_code", "accessCode", "checkInCode", "entryCode")
    arrival = _get(reservation, "arrivalInstructions", "checkInInstructions", "houseManual", "welcomeMessage") \
        or _get(listing, "arrivalInstructions", "checkInInstructions", "houseManual", "welcomeMessage")
    return {"door_code": code, "arrival_instructions": arrival}

def extract_pet_policy(listing_obj: Optional[Dict]) -> Dict[str, Optional[bool]]:
    listing = (listing_obj or {}).get("result") or {}
    pets_allowed = listing.get("petsAllowed") if "petsAllowed" in listing else None
    rules_blob = ""
    for k in ("rules", "houseRules", "description", "summary"):
        v = listing.get(k)
        if isinstance(v, str):
            rules_blob += " " + v.lower()
    if pets_allowed is None and rules_blob:
        if "no pets" in rules_blob or "pets not allowed" in rules_blob:
            pets_allowed = False
        elif "pets allowed" in rules_blob or "pet friendly" in rules_blob:
            pets_allowed = True
    return {"pets_allowed": pets_allowed, "pet_fee": None, "pet_deposit_refundable": None}

def _safe_read_json(path: str) -> Dict:
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        return {}
    except Exception as e:
        logging.error(f"Failed to read JSON at {path}: {e}")
        return {}

def load_listing_config(listing_id: Optional[int | str]) -> Dict:
    if not listing_id:
        return _safe_read_json("config/listings/default.json")
    by_id = _safe_read_json(f"config/listings/{listing_id}.json")
    return by_id or _safe_read_json("config/listings/default.json")

def apply_listing_config_to_meta(meta: Dict, cfg: Dict) -> Dict:
    out = dict(meta)
    prof = dict(out.get("property_profile") or {})
    if isinstance(cfg.get("property_profile"), dict):
        prof.update({k: v for k, v in cfg["property_profile"].items() if v is not None})
    out["property_profile"] = prof
    pol = dict(out.get("policies") or {})
    if isinstance(cfg.get("policies"), dict):
        pol.update({k: v for k, v in cfg["policies"].items() if v is not None})
    if "pets_allowed" in cfg:
        pol["pets_allowed"] = cfg.get("pets_allowed")
    out["policies"] = pol
    acc = dict(out.get("access") or {})
    if isinstance(cfg.get("access_and_arrival"), dict):
        for k, v in cfg["access_and_arrival"].items():
            if v is not None:
                acc[k] = v
    out["access"] = acc
    if isinstance(cfg.get("house_rules"), dict): out["house_rules"] = cfg["house_rules"]
    if isinstance(cfg.get("amenities_and_quirks"), dict): out["amenities_and_quirks"] = cfg["amenities_and_quirks"]
    if isinstance(cfg.get("safety_and_emergencies"), dict): out["safety_and_emergencies"] = cfg["safety_and_emergencies"]
    if isinstance(cfg.get("core_identity"), dict): out["core_identity"] = cfg["core_identity"]
    if isinstance(cfg.get("upsells"), dict): out["upsells"] = cfg["upsells"]
    return out

# ---------- Admin ----------
def require_admin(x_admin_token: str | None = Header(None, alias="X-Admin-Token"), token: str | None = Query(None)):
    supplied = x_admin_token or token
    if supplied != ADMIN_TOKEN:
        raise HTTPException(status_code=401, detail="Unauthorized")

@app.get("/admin/slack-diagnose", dependencies=[Depends(require_admin)])
def slack_diagnose():
    hint = (os.getenv("SLACK_CHANNEL", "") or "").strip()
    res: Dict[str, Any] = {"hint": hint, "skip_reads": SLACK_SKIP_READS, "cached_id": _SLACK_CHANNEL_ID}
    if _SLACK_CHANNEL_ID and _hint_is_id(_SLACK_CHANNEL_ID):
        res["resolved_id"] = _SLACK_CHANNEL_ID
        res["note"] = "Using channel ID; no read scopes required."
    else:
        res["error"] = "Set SLACK_CHANNEL to the Channel ID (starts with C/G)."
    return res

@app.post("/admin/slack-ping", dependencies=[Depends(require_admin)])
def admin_slack_ping(channel: Optional[str] = Query(None), text: str = Body("hostaway-autoreply ping")):
    token = os.getenv("SLACK_BOT_TOKEN", "")
    if not token:
        raise HTTPException(status_code=500, detail="SLACK_BOT_TOKEN missing")
    chan = (channel or _SLACK_CHANNEL_ID or os.getenv("SLACK_CHANNEL", "")).strip()
    if not _hint_is_id(chan):
        raise HTTPException(status_code=400, detail="Provide a channel ID. Set SLACK_CHANNEL to the Channel ID.")
    client = WebClient(token=token)
    try:
        resp = client.chat_postMessage(channel=chan, text=text)
        return {"ok": resp.get("ok"), "channel": chan, "ts": resp.get("ts")}
    except SlackApiError as e:
        data = e.response.data if hasattr(e, "response") else {"error": str(e)}
        return {"ok": False, "channel": chan, **data}

@app.get("/learning", dependencies=[Depends(require_admin)])
def list_learning(limit: int = 100) -> List[Dict]:
    conn = sqlite3.connect(LEARNING_DB_PATH); conn.row_factory = sqlite3.Row
    conn.execute("""
        CREATE TABLE IF NOT EXISTS learning_examples (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            intent TEXT, question TEXT, answer TEXT, coach_prompt TEXT, created_at TEXT
        )
    """)
    rows = conn.execute(
        "SELECT id, intent, question, answer, coach_prompt, created_at "
        "FROM learning_examples ORDER BY id DESC LIMIT ?", (limit,)
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]

@app.get("/feedback", dependencies=[Depends(require_admin)])
def list_feedback(limit: int = 100) -> List[Dict]:
    conn = sqlite3.connect(LEARNING_DB_PATH); conn.row_factory = sqlite3.Row
    conn.execute("""
        CREATE TABLE IF NOT EXISTS ai_feedback (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            conversation_id TEXT, question TEXT, ai_answer TEXT,
            rating TEXT, reason TEXT, user TEXT,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP
        )
    """)
    rows = conn.execute(
        "SELECT id, conversation_id, question, ai_answer, rating, reason, user, created_at "
        "FROM ai_feedback ORDER BY id DESC LIMIT ?", (limit,)
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]

@app.get("/feedback/summary", dependencies=[Depends(require_admin)])
def feedback_summary():
    conn = sqlite3.connect(LEARNING_DB_PATH); conn.row_factory = sqlite3.Row
    conn.execute("""
        CREATE TABLE IF NOT EXISTS ai_feedback (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            conversation_id TEXT, question TEXT, ai_answer TEXT,
            rating TEXT, reason TEXT, user TEXT,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP
        )
    """)
    rows = conn.execute("""
        SELECT rating, COUNT(*) AS n FROM ai_feedback GROUP BY rating ORDER BY n DESC
    """).fetchall()
    top_reasons = conn.execute("""
        SELECT reason, COUNT(*) AS n
        FROM ai_feedback
        WHERE rating = 'down' AND reason IS NOT NULL AND reason <> ''
        GROUP BY reason ORDER BY n DESC LIMIT 10
    """).fetchall()
    conn.close()
    return {
        "counts": [{"rating": r["rating"], "count": r["n"]} for r in rows],
        "top_reasons": [{"reason": r["reason"], "count": r["n"]} for r in top_reasons],
    }

@app.get("/feedback/export.csv", dependencies=[Depends(require_admin)])
def feedback_export_csv():
    from fastapi.responses import PlainTextResponse
    conn = sqlite3.connect(LEARNING_DB_PATH); conn.row_factory = sqlite3.Row
    rows = conn.execute("""
        SELECT id, conversation_id, question, ai_answer, rating, reason, user, created_at
        FROM ai_feedback ORDER BY id DESC
    """).fetchall()
    conn.close()
    header = "id,conversation_id,question,ai_answer,rating,reason,user,created_at"
    def _csv(s):
        if s is None: return ""
        s = str(s).replace('"','""'); return f'"{s}"'
    body = "\n".join(
        ",".join([
            str(r["id"]), _csv(r["conversation_id"]), _csv(r["question"]), _csv(r["ai_answer"]),
            _csv(r["rating"]), _csv(r["reason"]), _csv(r["user"]), _csv(r["created_at"]),
        ]) for r in rows
    )
    return PlainTextResponse("\n".join([header, body]), media_type="text/csv")

# ---------- Webhook ----------
class HostawayUnifiedWebhook(BaseModel):
    object: str
    event: str
    accountId: int
    data: dict
    body: str | None = None
    listingName: str | None = None
    date: str | None = None

@app.post("/unified-webhook")
async def unified_webhook(payload: HostawayUnifiedWebhook):
    payload_dict = payload.model_dump() if hasattr(payload, "model_dump") else payload.dict()
    logging.info("📬 Webhook received (keys): %s", list(payload_dict.keys()))

    if payload.event != "message.received" or payload.object != "conversationMessage":
        return {"status": "ignored"}

    d = payload.data or {}
    ev_core = d.get("id") or d.get("hash") or d.get("channelThreadMessageId") \
        or d.get("conversationId") or d.get("reservationId") or ""
    event_key = f"{payload.object}:{payload.event}:{ev_core}"
    if already_processed(event_key):
        return {"status": "duplicate"}

    guest_msg = d.get("body", "")
    if not guest_msg:
        if d.get("attachments"):
            logging.info("📷 Skipping image-only message.")
        else:
            logging.info("🧾 Empty message skipped.")
        mark_processed(event_key); return {"status": "ignored"}

    conv_id = d.get("conversationId")
    reservation_id = d.get("reservationId")
    listing_id = d.get("listingMapId")
    guest_id = d.get("userId", "")
    communication_type = d.get("communicationType", "channel")
    channel_id = d.get("channelId")

    try:
        log_message_event(
            direction="inbound",
            provider="hostaway",
            conversation_id=str(conv_id) if conv_id else None,
            reservation_id=str(reservation_id) if reservation_id else None,
            listing_id=str(listing_id) if listing_id else None,
            guest_id=str(guest_id) if guest_id else None,
            channel=channel_label_from(channel_id, communication_type),
            payload={"guest_message": guest_msg, "raw": d},
        )
    except Exception as e:
        logging.warning(f"message logging failed: {e}")

    reservation = fetch_hostaway_reservation(reservation_id) or {}
    res = reservation.get("result", {}) or {}

    guest_name = res.get("guestFirstName", "Guest")
    guest_email = res.get("guestEmail") or None

    convo_obj = fetch_hostaway_conversation(conv_id) or {}
    if DEBUG_CONVERSATION:
        try: logging.info("[DEBUG] Full conversation object: %s", json.dumps(convo_obj, indent=2))
        except Exception: pass
    convo_res = convo_obj.get("result", {}) or {}
    if convo_res: logging.info("✅ Conversation %s fetched with messages.", convo_res.get("id"))
    guest_photo = convo_res.get("recipientPicture") or convo_res.get("guestPicture") or res.get("guestPicture") or None
    if not guest_email:
        guest_email = convo_res.get("guestEmail") or convo_res.get("recipientEmail")

    returning_tag = ""
    if guest_email:
        try: seen_count = note_guest(guest_email.strip().lower())
        except Exception as e: logging.error(f"note_guest failed: {e}"); seen_count = 1
        if seen_count > 1: returning_tag = " • Returning guest!"
        elif SHOW_NEW_GUEST_TAG and seen_count == 1: returning_tag = " • New guest!"

    check_in = res.get("arrivalDate", "N/A")
    check_out = res.get("departureDate", "N/A")
    guest_count = res.get("numberOfGuests", "N/A")
    raw_status = (res.get("status") or "").strip().lower()
    res_status_pretty = pretty_res_status(raw_status)
    total_price_str = format_price(res.get("totalPrice"), (res.get("currency") or "USD"))
    guest_portal_url = (res.get("guestPortalUrl") or "").strip() or None
    show_portal_button = bool(guest_portal_url and raw_status in BOOKED_STATUSES)
    msg_status = pretty_status(d.get("status") or "sent")
    channel_pretty = channel_label_from(channel_id, communication_type)

    # ---------- LISTING & AMENITIES ----------
    listing_obj = fetch_hostaway_listing(listing_id)
    listing_result = (listing_obj or {}).get("result", {}) or {}

    try:
        from amenities_index import AmenitiesIndex
        amen_index = AmenitiesIndex(listing_result)
    except Exception as e:
        logging.warning("AmenitiesIndex unavailable: %s", e)
        class _NullIdx:
            def value(self, k, default=None): return default
            def to_api(self): return {}
        amen_index = _NullIdx()

    addr_raw = listing_result.get("address") or "Address unavailable"
    if isinstance(addr_raw, dict):
        property_address = (
            ", ".join(str(addr_raw.get(k, "")).strip()
                      for k in ["address","city","state","zip","country"] if addr_raw.get(k)) or "Address unavailable"
        )
    else:
        property_address = str(addr_raw)

    lat = listing_result.get("latitude") or listing_result.get("lat")
    lng = listing_result.get("longitude") or listing_result.get("lng")
    listing_cfg = load_listing_config(listing_id)
    tz_name = listing_result.get("timeZone") or listing_result.get("timezone") \
        or (listing_cfg.get("property_profile") or {}).get("timezone") or None

    access = extract_access_details(listing_obj, reservation)
    pet_policy = extract_pet_policy(listing_obj)

    # Optional nearby recs (no max_results kw)
    nearby_recs = None
    try:
        if should_fetch_local_recs(guest_msg) and lat and lng:
            try:
                nearby_recs = build_local_recs({"lat": lat, "lng": lng})
            except TypeError:
                # alt signatures
                try:
                    nearby_recs = build_local_recs(lat, lng)
                except Exception:
                    nearby_recs = []
    except Exception as e:
        logging.warning("Nearby recs fetch failed: %s", e)

    property_profile = {"checkin_time":"4:00 PM","checkout_time":"11:00 AM"}
    policies = {
        "pets_allowed": pet_policy.get("pets_allowed"),
        "pet_fee": pet_policy.get("pet_fee"),
        "pet_deposit_refundable": pet_policy.get("pet_deposit_refundable"),
    }
    property_details = {
        "address": property_address,
        "bedrooms": amen_index.value("bedrooms"),
        "bathrooms": amen_index.value("bathrooms"),
        "beds": amen_index.value("beds"),
        "max_guests": amen_index.value("max_guests"),
        "square_meters": amen_index.value("square_meters"),
        "room_type": amen_index.value("room_type"),
        "bathroom_type": amen_index.value("bathroom_type"),
        "check_in_start": amen_index.value("check_in_start"),
        "check_in_end": amen_index.value("check_in_end"),
        "check_out_time": amen_index.value("check_out_time"),
        "cancellation_policy": amen_index.value("cancellation_policy"),
        "wifi_username": amen_index.value("wifi_username"),
        "wifi_password": amen_index.value("wifi_password"),
    }

    meta_for_ai: Dict[str, Any] = {
        "listing_id": (str(listing_id) if listing_id is not None else ""),
        "listing_map_id": listing_id,
        "reservation_id": reservation_id,
        "check_in": check_in if isinstance(check_in, str) else str(check_in),
        "check_out": check_out if isinstance(check_out, str) else str(check_out),
        "reservation_status": (res.get("status") or "").strip(),
        "timezone": tz_name,
        "property_profile": property_profile,
        "property_details": property_details,
        "amenities_index": amen_index.to_api(),
        "policies": policies,
        "access": access,
        "location": {"lat": lat, "lng": lng},
        "nearby": {"items": nearby_recs or []},
    }

# --- Build context for generate_reply ---
    context_dict = {
        "guest_name": guest_name,
        "check_in_date": check_in,
        "check_out_date": check_out,
        "listing_info": meta_for_ai,  # includes property, policies, etc.
        "reservation": res,
        "history": [
            {"role": "guest" if m.get("isIncoming") else "host", "text": m.get("body", "")}
            for m in (convo_res.get("conversationMessages") or [])[-MAX_THREAD_MESSAGES:]
            if m.get("body")
        ]
    }

# --- Generate AI reply using smart_intel.py ---
ai_reply = generate_reply(guest_msg, context_dict)
detected_intent = "general"  # Or null/empty for now unless you later add intent detection

try:
    log_ai_exchange(
        conversation_id=str(conv_id) if conv_id else None,
        guest_message=guest_msg,
        ai_suggestion=ai_reply,
        intent=detected_intent,
        meta={
            "listing_id": listing_id, "reservation_id": reservation_id,
            "policies": meta_for_ai.get("policies"), "access": meta_for_ai.get("access"),
            "timezone": meta_for_ai.get("timezone"),
            "property_details": property_details,
            "amenities_index_keys": list((meta_for_ai.get("amenities_index") or {}).keys()),
        },
    )
except Exception as e:
    logging.warning(f"ai exchange logging failed: {e}")

    us_check_in = format_us_date(check_in); us_check_out = format_us_date(check_out)
    phase = trip_phase(check_in, check_out)

    button_meta = {
        "conv_id": conv_id, "listing_id": listing_id, "guest_id": guest_id, "type": communication_type,
        "guest_name": guest_name, "guest_message": guest_msg, "ai_suggestion": ai_reply,
        "check_in": check_in, "check_out": check_out, "guest_count": guest_count,
        "status": res_status_pretty, "detected_intent": detected_intent,
        "channel_pretty": channel_pretty, "property_address": property_address,
        "price": total_price_str, "guest_portal_url": guest_portal_url,
        "location": {"lat": lat, "lng": lng},
    }
    logging.info("button_meta: %s", json.dumps(button_meta, indent=2))

    header_text = (
        f"*{channel_pretty} message* from *{guest_name}*"
        f"{' • Returning guest!' if guest_email and guest_email.strip() else ''}\n"
        f"Property: *{property_address}*\n"
        f"Dates: *{us_check_in} → {us_check_out}*\n"
        f"Guests: *{guest_count}* | Res: *{res_status_pretty}* | Price: *{total_price_str}*"
    )
    header_block: Dict[str, Any] = {"type":"section","text":{"type":"mrkdwn","text": header_text}}
    if (guest_photo or ""):
        header_block["accessory"] = {"type":"image","image_url": guest_photo,"alt_text": guest_name or "Guest photo"}

    actions_elements = [
        {"type":"button","text":{"type":"plain_text","text":"✅ Send"},"value":json.dumps({**button_meta,"action":"send"}),"action_id":"send"},
        {"type":"button","text":{"type":"plain_text","text":"✏️ Edit"},"value":json.dumps({**button_meta,"action":"edit"}),"action_id":"edit"},
    ]
    if guest_portal_url and show_portal_button:
        actions_elements.append(
            {"type":"button","style":"primary","text":{"type":"plain_text","text":"🔗 Send guest portal"},
             "value":json.dumps({**button_meta,"action":"send_guest_portal"}),"action_id":"send_guest_portal"}
        )
    rating_payload = {
        "conv_id": conv_id, "listing_id": listing_id, "guest_message": guest_msg,
        "ai_suggestion": ai_reply, "detected_intent": detected_intent,
    }
    actions_elements.extend([
        {"type":"button","text":{"type":"plain_text","text":"👍 Useful"},"value":json.dumps(rating_payload),"action_id":"rate_up"},
        {"type":"button","text":{"type":"plain_text","text":"👎 Needs work"},"style":"danger","value":json.dumps(rating_payload),"action_id":"rate_down"},
    ])

    blocks = [
        {"type":"section","text":{"type":"mrkdwn","text": header_text}},
        {"type":"section","text":{"type":"mrkdwn","text": f"> {guest_msg}"}},
        {"type":"section","text":{"type":"mrkdwn","text": f"*Suggested Reply:*\n>{ai_reply}"}},
        {"type":"context","elements":[{"type":"mrkdwn","text": f"*Intent:* {detected_intent}  •  *Trip:* {phase}  •  *Msg:* {msg_status}"}]},
        {"type":"actions","elements": actions_elements},
    ]

    slack_client = WebClient(token=os.getenv("SLACK_BOT_TOKEN"))
    slack_channel_hint = os.getenv("SLACK_CHANNEL", "")
    sent = _post_to_slack(slack_client, slack_channel_hint, blocks, "New guest message")
    if not sent:
        mark_processed(event_key)
        logging.error("Slack post failed; marked processed to avoid retries. Check SLACK_CHANNEL and bot membership.")
        return {"status": "ok"}

        mark_processed(event_key)
        return {"status": "ok"}

# ---------- Startup & health ----------
@app.on_event("startup")
def _startup() -> None:
    db_init()
    hint = (os.getenv("SLACK_CHANNEL") or "").strip()
    global _SLACK_CHANNEL_ID
    if _hint_is_id(hint):
        _SLACK_CHANNEL_ID = hint
        logging.info("Using SLACK_CHANNEL as ID: %s", _SLACK_CHANNEL_ID)
    else:
        if SLACK_SKIP_READS:
            logging.error("SLACK_CHANNEL is a name but reads are disabled. Set it to the Channel ID (C…/G…).")
        else:
            logging.info("SLACK_SKIP_READS is off; name-based resolution disabled in this build. Provide a Channel ID.")

@app.get("/ping")
def ping():
    return {"status": "ok"}
