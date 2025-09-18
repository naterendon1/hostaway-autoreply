# path: main.py
import os
import time
import logging
import json
import sqlite3
from typing import List, Dict, Optional, Any
from datetime import date, datetime

from fastapi import FastAPI, Depends, HTTPException, Header, Query
from pydantic import BaseModel

# Local helpers
from places import should_fetch_local_recs, build_local_recs
from slack_interactivity import router as slack_router
from assistant_core import compose_reply as ac_compose
from utils import (
    fetch_hostaway_listing,
    fetch_hostaway_reservation,
    fetch_hostaway_conversation,
    clean_ai_reply,
)

# DB functions
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

# ---- App & logging ----
logging.basicConfig(level=logging.INFO)
app = FastAPI()
app.include_router(slack_router, prefix="/slack")

@app.on_event("startup")
def _startup() -> None:
    db_init()  # ensure tables on boot

# ---- Env & config ----
LEARNING_DB_PATH = os.getenv("LEARNING_DB_PATH", "learning.db")
ADMIN_TOKEN = os.getenv("ADMIN_TOKEN", "dev-token")
SHOW_NEW_GUEST_TAG = os.getenv("SHOW_NEW_GUEST_TAG", "0") in ("1", "true", "True", "yes", "YES")

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

# ---------- Slack threading (optional legacy) ----------
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
    2018: "Airbnb (Official)",
    2002: "HomeAway",
    2005: "Booking.com",
    2007: "Expedia",
    2009: "HomeAway (iCal)",
    2010: "Vrbo",
    2000: "Direct",
    2013: "Booking Engine",
    2015: "Custom iCal",
    2016: "TripAdvisor (iCal)",
    2017: "WordPress",
    2019: "Marriott",
    2020: "Partner",
    2021: "GDS",
    2022: "Google",
}
COMM_TYPE_MAP = {
    "airbnb": "Airbnb",
    "airbnbofficial": "Airbnb (Official)",
    "vrbo": "Vrbo",
    "bookingcom": "Booking.com",
    "direct": "Direct",
    "expedia": "Expedia",
    "channel": "Channel",
    "email": "Email",
}
RES_STATUS_ALLOWED = {
    "new": "New",
    "modified": "Modified",
    "cancelled": "Cancelled",
    "ownerstay": "Owner Stay",
    "pending": "Pending",
    "awaitingpayment": "Awaiting Payment",
    "declined": "Declined",
    "expired": "Expired",
    "inquiry": "Inquiry",
    "inquirypreapproved": "Inquiry (Preapproved)",
    "inquirydenied": "Inquiry (Denied)",
    "inquirytimedout": "Inquiry (Timed Out)",
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
            continue
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
    code = (
        _get(reservation, "doorCode", "door_code", "accessCode", "checkInCode", "entryCode")
        or _get(listing, "doorCode", "door_code", "accessCode", "checkInCode", "entryCode")
    )
    arrival_instructions = (
        _get(reservation, "arrivalInstructions", "checkInInstructions", "houseManual", "welcomeMessage")
        or _get(listing, "arrivalInstructions", "checkInInstructions", "houseManual", "welcomeMessage")
    )
    return {"door_code": code, "arrival_instructions": arrival_instructions}

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
    if by_id:
        return by_id
    return _safe_read_json("config/listings/default.json")

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

    if isinstance(cfg.get("house_rules"), dict):
        out["house_rules"] = cfg["house_rules"]
    if isinstance(cfg.get("amenities_and_quirks"), dict):
        out["amenities_and_quirks"] = cfg["amenities_and_quirks"]
    if isinstance(cfg.get("safety_and_emergencies"), dict):
        out["safety_and_emergencies"] = cfg["safety_and_emergencies"]
    if isinstance(cfg.get("core_identity"), dict):
        out["core_identity"] = cfg["core_identity"]
    if isinstance(cfg.get("upsells"), dict):
        out["upsells"] = cfg["upsells"]
    return out

# ---------- Admin ----------
def require_admin(
    x_admin_token: str | None = Header(None, alias="X-Admin-Token"),
    token: str | None = Query(None),
):
    supplied = x_admin_token or token
    if supplied != ADMIN_TOKEN:
        raise HTTPException(status_code=401, detail="Unauthorized")

@app.get("/learning", dependencies=[Depends(require_admin)])
def list_learning(limit: int = 100) -> List[Dict]:
    conn = sqlite3.connect(LEARNING_DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("""
        CREATE TABLE IF NOT EXISTS learning_examples (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            intent TEXT,
            question TEXT,
            answer TEXT,
            coach_prompt TEXT,
            created_at TEXT
        )
    """)
    rows = conn.execute(
        "SELECT id, intent, question, answer, coach_prompt, created_at "
        "FROM learning_examples ORDER BY id DESC LIMIT ?",
        (limit,),
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]

@app.get("/feedback", dependencies=[Depends(require_admin)])
def list_feedback(limit: int = 100) -> List[Dict]:
    conn = sqlite3.connect(LEARNING_DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("""
        CREATE TABLE IF NOT EXISTS ai_feedback (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            conversation_id TEXT,
            question TEXT,
            ai_answer TEXT,
            rating TEXT,
            reason TEXT,
            user TEXT,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP
        )
    """)
    rows = conn.execute(
        "SELECT id, conversation_id, question, ai_answer, rating, reason, user, created_at "
        "FROM ai_feedback ORDER BY id DESC LIMIT ?",
        (limit,),
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]

@app.get("/feedback/summary", dependencies=[Depends(require_admin)])
def feedback_summary():
    conn = sqlite3.connect(LEARNING_DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("""
        CREATE TABLE IF NOT EXISTS ai_feedback (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            conversation_id TEXT,
            question TEXT,
            ai_answer TEXT,
            rating TEXT,
            reason TEXT,
            user TEXT,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP
        )
    """)
    rows = conn.execute("""
        SELECT rating, COUNT(*) AS n
        FROM ai_feedback
        GROUP BY rating
        ORDER BY n DESC
    """).fetchall()
    top_reasons = conn.execute("""
        SELECT reason, COUNT(*) AS n
        FROM ai_feedback
        WHERE rating = 'down' AND reason IS NOT NULL AND reason <> ''
        GROUP BY reason
        ORDER BY n DESC
        LIMIT 10
    """).fetchall()
    conn.close()
    return {
        "counts": [{"rating": r["rating"], "count": r["n"]} for r in rows],
        "top_reasons": [{"reason": r["reason"], "count": r["n"]} for r in top_reasons],
    }

@app.get("/feedback/export.csv", dependencies=[Depends(require_admin)])
def feedback_export_csv():
    from fastapi.responses import PlainTextResponse
    conn = sqlite3.connect(LEARNING_DB_PATH)
    conn.row_factory = sqlite3.Row
    rows = conn.execute("""
        SELECT id, conversation_id, question, ai_answer, rating, reason, user, created_at
        FROM ai_feedback ORDER BY id DESC
    """).fetchall()
    conn.close()
    header = "id,conversation_id,question,ai_answer,rating,reason,user,created_at"
    def _csv_escape(s):
        if s is None: return ""
        s = str(s).replace('"', '""')
        return f'"{s}"'
    body = "\n".join(
        ",".join([
            str(r["id"]),
            _csv_escape(r["conversation_id"]),
            _csv_escape(r["question"]),
            _csv_escape(r["ai_answer"]),
            _csv_escape(r["rating"]),
            _csv_escape(r["reason"]),
            _csv_escape(r["user"]),
            _csv_escape(r["created_at"]),
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
    ev_core = (
        d.get("id")
        or d.get("hash")
        or d.get("channelThreadMessageId")
        or d.get("conversationId")
        or d.get("reservationId")
        or ""
    )
    event_key = f"{payload.object}:{payload.event}:{ev_core}"
    if already_processed(event_key):
        return {"status": "duplicate"}

    guest_msg = d.get("body", "")
    if not guest_msg:
        if d.get("attachments"):
            logging.info("📷 Skipping image-only message.")
        else:
            logging.info("🧾 Empty message skipped.")
        mark_processed(event_key)
        return {"status": "ignored"}

    conv_id = d.get("conversationId")
    reservation_id = d.get("reservationId")
    listing_id = d.get("listingMapId")
    guest_id = d.get("userId", "")
    communication_type = d.get("communicationType", "channel")
    channel_id = d.get("channelId")

    # Inbound event log (non-fatal)
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
    convo_res = convo_obj.get("result", {}) or {}
    if convo_res:
        logging.info("✅ Conversation %s fetched with messages.", convo_res.get("id"))
    guest_photo = (
        convo_res.get("recipientPicture")
        or convo_res.get("guestPicture")
        or res.get("guestPicture")
        or None
    )
    if not guest_email:
        guest_email = convo_res.get("guestEmail") or convo_res.get("recipientEmail")

    returning_tag = ""
    if guest_email:
        try:
            seen_count = note_guest(guest_email.strip().lower())
        except Exception as e:
            logging.error(f"note_guest failed: {e}")
            seen_count = 1
        if seen_count > 1:
            returning_tag = " • Returning guest!"
        elif SHOW_NEW_GUEST_TAG and seen_count == 1:
            returning_tag = " • New guest!"

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

    listing_obj = fetch_hostaway_listing(listing_id)
    addr_raw = (listing_obj or {}).get("result", {}).get("address") or "Address unavailable"
    if isinstance(addr_raw, dict):
        property_address = (
            ", ".join(
                str(addr_raw.get(k, "")).strip()
                for k in ["address", "city", "state", "zip", "country"]
                if addr_raw.get(k)
            )
            or "Address unavailable"
        )
    else:
        property_address = str(addr_raw)

    loc_res = (listing_obj or {}).get("result", {}) or {}
    lat = loc_res.get("latitude") or loc_res.get("lat")
    lng = loc_res.get("longitude") or loc_res.get("lng")

    listing_cfg = load_listing_config(listing_id)
    tz_name = (
        loc_res.get("timeZone") or loc_res.get("timezone")
        or (listing_cfg.get("property_profile") or {}).get("timezone")
        or None
    )

    access = extract_access_details(listing_obj, reservation)
    pet_policy = extract_pet_policy(listing_obj)

    msgs = []
    if "result" in convo_obj and "conversationMessages" in convo_obj["result"]:
        msgs = convo_obj["result"]["conversationMessages"] or []
    conversation_history = [
        {"role": "guest" if m.get("isIncoming") else "host", "text": m.get("body", "")}
        for m in msgs[-MAX_THREAD_MESSAGES:]
        if m.get("body")
    ]

    property_profile = {"checkin_time": "4:00 PM", "checkout_time": "11:00 AM"}
    policies = {
        "pets_allowed": pet_policy.get("pets_allowed"),
        "pet_fee": pet_policy.get("pet_fee"),
        "pet_deposit_refundable": pet_policy.get("pet_deposit_refundable"),
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
        "policies": policies,
        "access": access,
        "location": {"lat": lat, "lng": lng},
    }
    meta_for_ai = apply_listing_config_to_meta(meta_for_ai, listing_cfg)

    # Optional: Local Places (guarded)
    local_recs_api: List[Dict[str, Any]] = []
    try:
        if lat is not None and lng is not None and should_fetch_local_recs(guest_msg):
            local_recs_api = build_local_recs(lat, lng, guest_msg)
    except Exception as e:
        logging.warning(f"Local recs fetch failed: {e}")
        local_recs_api = []
    meta_for_ai["local_recs_api"] = local_recs_api

    # ---- Compose AI once ----
    ai_json, _unused_blocks = ac_compose(
        guest_message=guest_msg,
        conversation_history=conversation_history,
        meta=meta_for_ai,
    )
    ai_reply_raw = ai_json.get("reply", "") or ""
    if (ai_json.get("intent") or "").lower() == "food_recs":
        ai_reply = ai_reply_raw
    else:
        ai_reply = clean_ai_reply(ai_reply_raw)
    detected_intent = ai_json.get("intent", "other")

    # Persist AI suggestion (non-fatal)
    try:
        log_ai_exchange(
            conversation_id=str(conv_id) if conv_id else None,
            guest_message=guest_msg,
            ai_suggestion=ai_reply,
            intent=detected_intent,
            meta={
                "listing_id": listing_id,
                "reservation_id": reservation_id,
                "policies": meta_for_ai.get("policies"),
                "access": meta_for_ai.get("access"),
                "timezone": meta_for_ai.get("timezone"),
            },
        )
    except Exception as e:
        logging.warning(f"ai exchange logging failed: {e}")

    us_check_in = format_us_date(check_in)
    us_check_out = format_us_date(check_out)
    phase = trip_phase(check_in, check_out)

    button_meta = {
        "conv_id": conv_id,
        "listing_id": listing_id,
        "guest_id": guest_id,
        "type": communication_type,
        "guest_name": guest_name,
        "guest_message": guest_msg,
        "ai_suggestion": ai_reply,
        "check_in": check_in,
        "check_out": check_out,
        "guest_count": guest_count,
        "status": res_status_pretty,
        "detected_intent": detected_intent,
        "channel_pretty": channel_pretty,
        "property_address": property_address,
        "price": total_price_str,
        "guest_portal_url": guest_portal_url,
        "location": {"lat": lat, "lng": lng},
    }
    logging.info("button_meta: %s", json.dumps(button_meta, indent=2))

    header_text = (
        f"*{channel_pretty} message* from *{guest_name}*{returning_tag}\n"
        f"Property: *{property_address}*\n"
        f"Dates: *{us_check_in} → {us_check_out}*\n"
        f"Guests: *{guest_count}* | Res: *{res_status_pretty}* | Price: *{total_price_str}*"
    )
    header_block: Dict[str, Any] = {
        "type": "section",
        "text": {"type": "mrkdwn", "text": header_text},
    }
    if (guest_photo or ""):
        header_block["accessory"] = {
            "type": "image",
            "image_url": guest_photo,
            "alt_text": guest_name or "Guest photo",
        }

    actions_elements = [
        {
            "type": "button",
            "text": {"type": "plain_text", "text": "✅ Send"},
            "value": json.dumps({**button_meta, "action": "send"}),
            "action_id": "send",
        },
        {
            "type": "button",
            "text": {"type": "plain_text", "text": "✏️ Edit"},
            "value": json.dumps({**button_meta, "action": "edit"}),
            "action_id": "edit",
        },
    ]
    if guest_portal_url and show_portal_button:
        actions_elements.append(
            {
                "type": "button",
                "style": "primary",
                "text": {"type": "plain_text", "text": "🔗 Send guest portal"},
                "value": json.dumps({**button_meta, "action": "send_guest_portal"}),
                "action_id": "send_guest_portal",
            }
        )
    rating_payload = {
        "conv_id": conv_id,
        "listing_id": listing_id,
        "guest_message": guest_msg,
        "ai_suggestion": ai_reply,
        "detected_intent": detected_intent,
    }
    actions_elements.extend([
        {
            "type": "button",
            "text": {"type": "plain_text", "text": "👍 Useful"},
            "value": json.dumps(rating_payload),
            "action_id": "rate_up",
        },
        {
            "type": "button",
            "text": {"type": "plain_text", "text": "👎 Needs work"},
            "style": "danger",
            "value": json.dumps(rating_payload),
            "action_id": "rate_down",
        },
    ])

    blocks = [
        header_block,
        {"type": "section", "text": {"type": "mrkdwn", "text": f"> {guest_msg}"}},
        {"type": "section", "text": {"type": "mrkdwn", "text": f"*Suggested Reply:*\n>{ai_reply}"}},
        {"type": "context", "elements": [
            {"type": "mrkdwn", "text": f"*Intent:* {detected_intent}  •  *Trip:* {phase}  •  *Msg:* {msg_status}"}
        ]},
        {"type": "actions", "elements": actions_elements},
    ]

    # Post to Slack
    from slack_sdk import WebClient
    from slack_sdk.errors import SlackApiError

    slack_client = WebClient(token=os.getenv("SLACK_BOT_TOKEN"))
    slack_channel = os.getenv("SLACK_CHANNEL")
    if not slack_channel:
        logging.error("SLACK_CHANNEL missing; skipping Slack post.")
        mark_processed(event_key)
        return {"status": "ok"}
    try:
        slack_client.chat_postMessage(
            channel=slack_channel,
            blocks=blocks,
            text="New guest message",
        )
        mark_processed(event_key)
    except SlackApiError as e:
        try:
            status = getattr(e, "response", {}).status_code if hasattr(e, "response") else None
        except Exception:
            status = None
        if status == 429 and hasattr(e, "response"):
            retry_after = int(e.response.headers.get("Retry-After", "1"))
            time.sleep(max(retry_after, 1))
            slack_client.chat_postMessage(channel=slack_channel, blocks=blocks, text="New guest message")
            mark_processed(event_key)
            return {"status": "ok"}
        logging.error(f"❌ Slack send error: {e.response.data if hasattr(e, 'response') else e}")
        raise
    except Exception as e:
        logging.error(f"❌ Slack send error: {e}")
        raise

    return {"status": "ok"}

# ---------- Health ----------
@app.get("/ping")
def ping():
    return {"status": "ok"}
