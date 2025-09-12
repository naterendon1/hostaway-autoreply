# =========================
# File: main.py
# =========================
import os
import logging
import json
import sqlite3
from typing import List, Dict, Optional, Any
from datetime import date, datetime

from fastapi import FastAPI, Depends, HTTPException, Header, Query
from pydantic import BaseModel

# Local places (Google) helpers
from places import should_fetch_local_recs, build_local_recs

# Slack interactivity router (separate file)
from slack_interactivity import router as slack_router

# Guarded AI composer
from assistant_core import compose_reply as ac_compose

# Utils you already have
from utils import (
    fetch_hostaway_listing,
    fetch_hostaway_reservation,
    fetch_hostaway_conversation,
    clean_ai_reply,
)

# ---- App & logging ----
logging.basicConfig(level=logging.INFO)
app = FastAPI()
app.include_router(slack_router, prefix="/slack")

# ---- Env checks ----
LEARNING_DB_PATH = os.getenv("LEARNING_DB_PATH", "learning.db")
ADMIN_TOKEN = os.getenv("ADMIN_TOKEN", "dev-token")
SHOW_NEW_GUEST_TAG = os.getenv("SHOW_NEW_GUEST_TAG", "0") in ("1", "true", "True", "yes", "YES")

REQUIRED_ENV_VARS = [
    "HOSTAWAY_CLIENT_ID",
    "HOSTAWAY_CLIENT_SECRET",
    "OPENAI_API_KEY",  # assistant_core needs this
    "SLACK_CHANNEL",
    "SLACK_BOT_TOKEN",
    "SLACK_SIGNING_SECRET",
]
missing = [v for v in REQUIRED_ENV_VARS if not os.getenv(v)]
if missing:
    raise RuntimeError(f"Missing required environment variables: {missing}")

MAX_THREAD_MESSAGES = 10

# ---------- DB bootstrap & helpers ----------
from db import init_db as db_init
from db import (
    get_slack_thread as db_get_slack_thread,   # kept for compat (not used; we post new parents)
    upsert_slack_thread as db_upsert_slack_thread,
    note_guest,                                # returning/new guest counter
    already_processed,                         # idempotency helpers (single arg)
    mark_processed,
)
db_init()


def _get_thread_ts(conv_id: Optional[int | str]) -> Optional[str]:
    if not conv_id:
        return None
    rec = db_get_slack_thread(str(conv_id))
    return rec["ts"] if rec else None


def _set_thread_ts(conv_id: Optional[int | str], ts: str) -> None:
    if not conv_id or not ts:
        return
    channel = os.getenv("SLACK_CHANNEL")
    db_upsert_slack_thread(str(conv_id), channel or "", ts)


# ---------- Legacy tiny local helper (kept for backwards safety, unused now) ----------
def _bump_guest_seen_local(email: Optional[str]) -> int:
    """
    Old local counter using learning.db. Prefer db.note_guest now.
    Kept only as safety; not used.
    """
    if not email:
        return 0
    key = (email or "").strip().lower()
    if not key:
        return 0
    conn = sqlite3.connect(LEARNING_DB_PATH)
    conn.row_factory = sqlite3.Row
    cur = conn.cursor()
    cur.execute("""
        CREATE TABLE IF NOT EXISTS guest_contacts (
            email TEXT PRIMARY KEY,
            seen_count INTEGER NOT NULL DEFAULT 0
        )
    """)
    row = cur.execute("SELECT seen_count FROM guest_contacts WHERE email=?", (key,)).fetchone()
    if row:
        seen = int(row["seen_count"]) + 1
        cur.execute("UPDATE guest_contacts SET seen_count=? WHERE email=?", (seen, key))
    else:
        seen = 1
        cur.execute("INSERT INTO guest_contacts(email, seen_count) VALUES(?, ?)", (key, seen))
    conn.commit()
    conn.close()
    return seen


# ---------- Helpers for Slack header formatting ----------
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
BOOKED_STATUSES = {"new", "modified"}  # treat these as confirmed/active


def format_us_date(d: str | None) -> str:
    """YYYY-MM-DD / YYYY-MM-DDTHH:MM:SS -> MM/DD/YYYY"""
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


# ---------- Extractors for AI context (door code, pets, etc.) ----------
def extract_access_details(listing_obj: Optional[Dict], reservation_obj: Optional[Dict]) -> Dict[str, Optional[str]]:
    """
    Best-effort extraction of door/arrival info from Hostaway responses.
    Looks through common fields on both reservation and listing payloads.
    """
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
    """
    Try to infer the pet policy from structured fields or free text rules.
    """
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


# ---------- Per-listing config loader (local JSON files) ----------
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
    """
    Load config/listings/{listing_id}.json, fallback to config/listings/default.json.
    """
    if not listing_id:
        return _safe_read_json("config/listings/default.json")
    by_id = _safe_read_json(f"config/listings/{listing_id}.json")
    if by_id:
        return by_id
    return _safe_read_json("config/listings/default.json")


def apply_listing_config_to_meta(meta: Dict, cfg: Dict) -> Dict:
    """
    Merge selected fields from per-listing config into the AI meta dict.
    Nonexistent sections are ignored.
    """
    out = dict(meta)

    # Property profile (check-in/out times, etc.)
    prof = dict(out.get("property_profile") or {})
    if isinstance(cfg.get("property_profile"), dict):
        prof.update({k: v for k, v in cfg["property_profile"].items() if v is not None})
    out["property_profile"] = prof

    # Policies
    pol = dict(out.get("policies") or {})
    if isinstance(cfg.get("policies"), dict):
        pol.update({k: v for k, v in cfg["policies"].items() if v is not None})
    if "pets_allowed" in cfg:
        pol["pets_allowed"] = cfg.get("pets_allowed")
    out["policies"] = pol

    # Access / arrival details
    acc = dict(out.get("access") or {})
    if isinstance(cfg.get("access_and_arrival"), dict):
        for k, v in cfg["access_and_arrival"].items():
            if v is not None:
                acc[k] = v
    out["access"] = acc

    # House rules & fees (free-form context)
    if isinstance(cfg.get("house_rules"), dict):
        out["house_rules"] = cfg["house_rules"]

    # Amenities & quirks (free-form context)
    if isinstance(cfg.get("amenities_and_quirks"), dict):
        out["amenities_and_quirks"] = cfg["amenities_and_quirks"]

    # Safety & emergencies
    if isinstance(cfg.get("safety_and_emergencies"), dict):
        out["safety_and_emergencies"] = cfg["safety_and_emergencies"]

    # Core identity (optional style hints)
    if isinstance(cfg.get("core_identity"), dict):
        out["core_identity"] = cfg["core_identity"]

    # Upsells / add-ons if present
    if isinstance(cfg.get("upsells"), dict):
        out["upsells"] = cfg["upsells"]

    return out


# ---------- Admin endpoints ----------
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
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS learning_examples (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            intent TEXT,
            question TEXT,
            answer TEXT,
            coach_prompt TEXT,
            created_at TEXT
        )
    """
    )
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
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS ai_feedback (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            conversation_id TEXT,
            question TEXT,
            ai_answer TEXT,
            rating TEXT,
            user TEXT,
            created_at TEXT
        )
    """
    )
    rows = conn.execute(
        "SELECT id, conversation_id, question, ai_answer, rating, user, created_at "
        "FROM ai_feedback ORDER BY id DESC LIMIT ?",
        (limit,),
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


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
    logging.info(f"📬 Webhook received: {json.dumps(payload.dict(), indent=2)}")

    # We only handle inbound guest messages (conversation message received)
    if payload.event != "message.received" or payload.object != "conversationMessage":
        return {"status": "ignored"}

    # ------------- Idempotency (single-argument key) -------------
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

    # Basic IDs/context
    conv_id = d.get("conversationId")
    reservation_id = d.get("reservationId")
    listing_id = d.get("listingMapId")
    guest_id = d.get("userId", "")
    communication_type = d.get("communicationType", "channel")
    channel_id = d.get("channelId")

    # Reservation (Hostaway)
    reservation = fetch_hostaway_reservation(reservation_id) or {}
    res = reservation.get("result", {}) or {}

    guest_name = res.get("guestFirstName", "Guest")
    guest_email = res.get("guestEmail") or None

    # Conversation (for picture/email fallback and history)
    convo_obj = fetch_hostaway_conversation(conv_id) or {}
    convo_res = convo_obj.get("result", {}) or {}
    guest_photo = (
        convo_res.get("recipientPicture")
        or convo_res.get("guestPicture")
        or res.get("guestPicture")
        or None
    )
    if not guest_email:
        guest_email = convo_res.get("guestEmail") or convo_res.get("recipientEmail")

    # Returning / New guest tag (uses persistent db.guests)
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

    # Reservation status & total price
    raw_status = (res.get("status") or "").strip().lower()
    res_status_pretty = pretty_res_status(raw_status)
    total_price_str = format_price(res.get("totalPrice"), (res.get("currency") or "USD"))

    # Portal button eligibility (only for confirmed/active bookings)
    guest_portal_url = (res.get("guestPortalUrl") or "").strip() or None
    show_portal_button = bool(guest_portal_url and raw_status in BOOKED_STATUSES)

    # Message transport status (from webhook payload)
    msg_status = pretty_status(d.get("status") or "sent")

    # Slack channel/label
    channel_pretty = channel_label_from(channel_id, communication_type)

    # Fetch listing to build address and extract access/pets
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

    # Lat/Lng for food/distance recs
    loc_res = (listing_obj or {}).get("result", {}) or {}
    lat = loc_res.get("latitude") or loc_res.get("lat")
    lng = loc_res.get("longitude") or loc_res.get("lng")

    # Extract access details & pet policy for AI context
    access = extract_access_details(listing_obj, reservation)
    pet_policy = extract_pet_policy(listing_obj)

    # Conversation context (last few)
    msgs = []
    if "result" in convo_obj and "conversationMessages" in convo_obj["result"]:
        msgs = convo_obj["result"]["conversationMessages"] or []

    conversation_history = [
        {"role": "guest" if m.get("isIncoming") else "host", "text": m.get("body", "")}
        for m in msgs[-MAX_THREAD_MESSAGES:]
        if m.get("body")
    ]

    # Minimal property profile (sane defaults; assistant_core may override)
    property_profile = {"checkin_time": "4:00 PM", "checkout_time": "11:00 AM"}

    # Policies for the model (include pet policy)
    policies = {
        "pets_allowed": pet_policy.get("pets_allowed"),
        "pet_fee": pet_policy.get("pet_fee"),
        "pet_deposit_refundable": pet_policy.get("pet_deposit_refundable"),
    }

    # ---------- Load & apply per-listing config ----------
    listing_cfg = load_listing_config(listing_id)  # reads config/listings/{id}.json or default.json

    # Build base meta for the model
    meta_for_ai: Dict[str, Any] = {
        "listing_id": (str(listing_id) if listing_id is not None else ""),
        "listing_map_id": listing_id,
        "reservation_id": reservation_id,
        "check_in": check_in if isinstance(check_in, str) else str(check_in),
        "check_out": check_out if isinstance(check_out, str) else str(check_out),
        "reservation_status": (res.get("status") or "").strip(),  # raw status for guards
        "property_profile": property_profile,
        "policies": policies,
        "access": access,  # door_code & arrival_instructions if available
        "location": {"lat": lat, "lng": lng},
    }
    meta_for_ai = apply_listing_config_to_meta(meta_for_ai, listing_cfg)

    # --- Local Places (live POIs) ---
    local_recs_api: List[Dict[str, Any]] = []
    try:
        if lat is not None and lng is not None and should_fetch_local_recs(guest_msg):
            local_recs_api = build_local_recs(lat, lng, guest_msg)
    except Exception as e:
        logging.warning(f"Local recs fetch failed: {e}")
        local_recs_api = []
    # Include in meta for the model
    meta_for_ai["local_recs_api"] = local_recs_api

    # ---- Guarded AI (assistant_core) ----
    ai_json, _unused_blocks = ac_compose(
        guest_message=guest_msg,
        conversation_history=conversation_history,
        meta=meta_for_ai,
    )
    ai_reply = clean_ai_reply(ai_json.get("reply", "") or "")
    detected_intent = ai_json.get("intent", "other")

    # US dates & phase
    us_check_in = format_us_date(check_in)
    us_check_out = format_us_date(check_out)
    phase = trip_phase(check_in, check_out)

    # Slack button meta (Edit kept)
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
        "status": res_status_pretty,  # pretty status for display / interactivity
        "detected_intent": detected_intent,
        "channel_pretty": channel_prety if (channel_prety := channel_pretty) else channel_pretty,  # keep name stable
        "property_address": property_address,
        "price": total_price_str,
        "guest_portal_url": guest_portal_url,
        # keep lat/lng small if present for downstream features
        "location": {"lat": lat, "lng": lng},
    }
    logging.info("button_meta: %s", json.dumps(button_meta, indent=2))

    # Slack blocks
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
    if guest_photo:
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

    # Guest portal button (confirmed bookings only)
    if show_portal_button:
        actions_elements.append(
            {
                "type": "button",
                "style": "primary",
                "text": {"type": "plain_text", "text": "🔗 Send guest portal"},
                "value": json.dumps({**button_meta, "action": "send_guest_portal"}),
                "action_id": "send_guest_portal",
            }
        )

    blocks = [
        header_block,
        {"type": "section", "text": {"type": "mrkdwn", "text": f"> {guest_msg}"}},
        {"type": "section", "text": {"type": "mrkdwn", "text": f"*Suggested Reply:*\n>{ai_reply}"}},
        {"type": "context", "elements": [
            {"type": "mrkdwn", "text": f"*Intent:* {detected_intent}  •  *Trip:* {phase}  •  *Msg:* {msg_status}"}
        ]},
        {"type": "actions", "elements": actions_elements},
    ]

    # Post to Slack — ALWAYS create a new parent message (no threading by conv_id)
    from slack_sdk import WebClient
    from slack_sdk.errors import SlackApiError

    slack_client = WebClient(token=os.getenv("SLACK_BOT_TOKEN"))
    slack_channel = os.getenv("SLACK_CHANNEL")

    try:
        slack_client.chat_postMessage(
            channel=slack_channel,
            blocks=blocks,
            text="New guest message",
        )
        # mark processed only after successful handling
        mark_processed(event_key)
    except SlackApiError as e:
        logging.error(f"❌ Slack send error: {e.response.data if hasattr(e, 'response') else e}")
        # do not mark processed so it can retry
        raise
    except Exception as e:
        logging.error(f"❌ Slack send error: {e}")
        raise

    return {"status": "ok"}


# ---------- Health ----------
@app.get("/ping")
def ping():
    return {"status": "ok"}
