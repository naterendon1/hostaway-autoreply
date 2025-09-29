# places.py
#
# Purpose: One reliable path for Google Places Text Search and Distance Matrix
# with small, safe fallbacks and light intent helpers for local recs.
#
# Required ENV on Render:
#   GOOGLE_PLACES_API_KEY
# Optional:
#   GOOGLE_DISTANCE_MATRIX_API_KEY  (falls back to GOOGLE_PLACES_API_KEY)

import os
import re
import math
import time
import logging
from typing import List, Dict, Any, Optional, Tuple, Union
import requests

# ---------- Intent heuristics (lightweight) ----------
FOOD_POS = re.compile(
    r"\b(restaurant|eat|dinner|lunch|breakfast|coffee|cafe|brunch|bar|brewery|pizza|sushi)\b",
    re.I,
)
FOOD_NEG_HARD = re.compile(
    r"\b(trash|garbage|bin[s]?|disabled|wifi|portal|code|lock|check[- ]?in|check[- ]?out|parking)\b",
    re.I,
)

def should_fetch_food_recs(guest_text: str) -> bool:
    """
    True when the guest is actually asking about food/drink,
    and not an ops/support message.
    """
    text = (guest_text or "").strip()
    if not text:
        return False
    if FOOD_NEG_HARD.search(text):
        return False
    return bool(FOOD_POS.search(text))

# ---------- Session / keys ----------
SESSION = requests.Session()
DEFAULT_TIMEOUT = 12

PLACES_KEY = os.getenv("GOOGLE_PLACES_API_KEY")
DM_KEY = os.getenv("GOOGLE_DISTANCE_MATRIX_API_KEY") or PLACES_KEY

def _req_with_retry(url: str, params: Dict[str, Any], tries: int = 2) -> requests.Response:
    last_exc = None
    for i in range(tries):
        try:
            r = SESSION.get(url, params=params, timeout=DEFAULT_TIMEOUT)
            if r.status_code == 200:
                return r
            if r.status_code in (429, 500, 502, 503, 504):
                time.sleep(1.2 * (i + 1))
            else:
                r.raise_for_status()
        except Exception as e:
            last_exc = e
            time.sleep(0.7 * (i + 1))
    if last_exc:
        raise last_exc
    raise RuntimeError("Unknown request failure")

# ---------- Core: Text Search ----------
def text_search_place(
    query: str,
    bias_lat: Optional[float] = None,
    bias_lng: Optional[float] = None,
    city: Optional[str] = None,
    state: Optional[str] = None,
) -> Optional[Dict[str, Any]]:
    """
    Google Places Text Search. Returns first result dict (or None).
    """
    if not PLACES_KEY:
        logging.warning("GOOGLE_PLACES_API_KEY missing")
        return None

    full_query = query
    bias_suffix = " ".join([p for p in [city, state] if p])
    if bias_suffix:
        full_query = f"{query} {bias_suffix}"

    params = {"query": full_query, "key": PLACES_KEY}
    if bias_lat is not None and bias_lng is not None:
        # Light bias — circle search
        params["location"] = f"{bias_lat},{bias_lng}"
        params["radius"] = 20000  # meters (~12mi)

    url = "https://maps.googleapis.com/maps/api/place/textsearch/json"
    data = _req_with_retry(url, params).json()
    status = data.get("status")
    if status not in ("OK", "ZERO_RESULTS"):
        logging.warning(f"Places Text Search status={status} error={data.get('error_message')}")
    results = data.get("results") or []
    return results[0] if results else None

def get_place_gmaps_url(place_id: str) -> str:
    # Link pattern recommended by Google docs
    return f"https://www.google.com/maps/place/?q=place_id:{place_id}"

# ---------- Distance Matrix ----------
def get_drive_distance_duration(
    origin: Union[str, Tuple[float, float]],
    destination: Union[str, Tuple[float, float]],
) -> Optional[Dict[str, Any]]:
    """
    Returns dict with {"miles": float, "minutes": int, "raw": <element>} or None.
    """
    if not DM_KEY:
        logging.warning("No Distance Matrix key available")
        return None

    def fmt(x):
        if isinstance(x, (tuple, list)) and len(x) == 2:
            return f"{x[0]},{x[1]}"
        return x

    params = {
        "origins": fmt(origin),
        "destinations": fmt(destination),
        "key": DM_KEY,
        "mode": "driving",
        "departure_time": "now",
    }
    url = "https://maps.googleapis.com/maps/api/distancematrix/json"
    data = _req_with_retry(url, params).json()
    if data.get("status") != "OK":
        logging.warning(f"Distance Matrix status={data.get('status')} error={data.get('error_message')}")
        return None
    rows = data.get("rows") or []
    if not rows or not rows[0].get("elements"):
        return None
    el = rows[0]["elements"][0]
    if el.get("status") != "OK":
        return None

    meters = el["distance"]["value"]
    seconds = el.get("duration_in_traffic", el.get("duration", {})).get("value", 0)
    miles = round(meters / 1609.344, 1)
    minutes = max(1, math.ceil(seconds / 60)) if seconds else None
    return {"miles": miles, "minutes": minutes, "raw": el}

# ---------- Nearby search (optional helper used by suggesters) ----------
def _nearby(
    lat: float,
    lng: float,
    *,
    type_: Optional[str] = None,
    keyword: Optional[str] = None,
    radius: int = 5000,
    max_results: int = 6,
) -> List[Dict[str, Any]]:
    """
    Places Nearby Search (simple wrapper). Returns simplified dict list.
    """
    if not PLACES_KEY:
        logging.warning("GOOGLE_PLACES_API_KEY missing")
        return []
    params = {"key": PLACES_KEY, "location": f"{lat},{lng}", "radius": radius}
    if type_:
        params["type"] = type_
    if keyword:
        params["keyword"] = keyword

    url = "https://maps.googleapis.com/maps/api/place/nearbysearch/json"
    data = _req_with_retry(url, params).json()
    if data.get("status") not in ("OK", "ZERO_RESULTS"):
        logging.warning(f"Nearby status={data.get('status')} error={data.get('error_message')}")
        return []
    out: List[Dict[str, Any]] = []
    for r in (data.get("results") or [])[:max_results]:
        out.append(
            {
                "name": r.get("name"),
                "rating": r.get("rating"),
                "vicinity": r.get("vicinity"),
                "place_id": r.get("place_id"),
                "maps_url": get_place_gmaps_url(r.get("place_id", "")),
            }
        )
    return out

# ---------- Category suggestion from free text ----------
def suggest_category_queries(guest_text: str) -> List[Tuple[str, Dict[str, Any]]]:
    """
    Returns a small list of (label, search_params) to feed into _nearby.
    Keeps it simple and capped to at most 3 categories.
    """
    t = (guest_text or "").lower()

    cats: List[Tuple[str, Dict[str, Any]]] = []

    def add(label: str, *, type_: Optional[str] = None, keyword: Optional[str] = None):
        params: Dict[str, Any] = {}
        if type_:
            params["type_"] = type_
        if keyword:
            params["keyword"] = keyword
        cats.append((label, params))

    if any(k in t for k in ["restaurant", "food", "eat", "dinner", "lunch"]):
        add("Good Restaurants", type_="restaurant")
    if any(k in t for k in ["coffee", "cafe", "breakfast", "brunch"]):
        add("Coffee & Breakfast", type_="cafe")
    if any(k in t for k in ["museum", "art", "exhibit"]):
        add("Museums & Culture", keyword="museum")
    if any(k in t for k in ["kids", "family", "children", "playground", "zoo", "aquarium"]):
        add("Family-friendly", keyword="playground")
    if any(k in t for k in ["grocery", "supermarket", "market", "whole foods", "heb", "trader joe"]):
        add("Groceries", type_="supermarket")

    if not cats:
        # Default “things to do” set
        add("Good Restaurants", type_="restaurant")
        add("Coffee & Breakfast", type_="cafe")
        add("Sights & Attractions", keyword="tourist attraction")

    return cats[:3]
