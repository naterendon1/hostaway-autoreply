# places.py
import os
import logging
from typing import List, Dict, Any, Optional
import requests
import re

FOOD_POS = re.compile(r"\b(restaurant|eat|dinner|lunch|breakfast|coffee|cafe|brunch|bar|brewery|pizza|sushi)\b", re.I)
FOOD_NEG_HARD = re.compile(r"\b(trash|garbage|bin[s]?|disabled|wheelchair|elevator|accessib|ramp|portal|code|lock|check[- ]?in|check[- ]?out|parking)\b", re.I)

def should_fetch_local_recs(msg: str) -> bool:
    text = (msg or "").strip()
    if not text:
        return False
    if FOOD_NEG_HARD.search(text):
        return False  # other intents beat food
    # require an explicit positive
    return bool(FOOD_POS.search(text))


PLACES_KEY = os.getenv("GOOGLE_PLACES_API_KEY")
DM_KEY = os.getenv("GOOGLE_DISTANCE_MATRIX_API_KEY") or PLACES_KEY

PLACES_URL = "https://maps.googleapis.com/maps/api/place/nearbysearch/json"
DM_URL = "https://maps.googleapis.com/maps/api/distancematrix/json"


def _maps_url(place_id: str) -> str:
    return f"https://www.google.com/maps/place/?q=place_id:{place_id}"


def _nearby(
    lat: float,
    lng: float,
    *,
    type_: Optional[str] = None,
    keyword: Optional[str] = None,
    radius: int = 5000,
    max_results: int = 6,
) -> List[Dict[str, Any]]:
    """Thin wrapper over Places Nearby. Returns simplified dicts."""
    if not PLACES_KEY:
        logging.warning("GOOGLE_PLACES_API_KEY missing; skipping Places lookup.")
        return []

    params = {
        "key": PLACES_KEY,
        "location": f"{lat},{lng}",
        "radius": radius,
    }
    if type_:
        params["type"] = type_
    if keyword:
        params["keyword"] = keyword

    r = requests.get(PLACES_URL, params=params, timeout=10)
    r.raise_for_status()
    data = r.json()
    results = []
    for p in (data.get("results") or [])[:max_results]:
        geom = (p.get("geometry") or {}).get("location") or {}
        pid = p.get("place_id") or ""
        results.append(
            {
                "place_id": pid,
                "name": p.get("name"),
                "vicinity": p.get("vicinity"),
                "rating": p.get("rating"),
                "user_ratings_total": p.get("user_ratings_total"),
                "types": p.get("types") or [],
                "lat": geom.get("lat"),
                "lng": geom.get("lng"),
                "maps_url": _maps_url(pid) if pid else None,
            }
        )
    return results


def _distance_matrix(origin_lat: float, origin_lng: float, dests: List[Dict[str, Any]]) -> None:
    """Annotate each place with drive distance/duration (text) using Distance Matrix."""
    if not DM_KEY or not dests:
        return
    destinations = "|".join(f"{d['lat']},{d['lng']}" for d in dests if d.get("lat") and d.get("lng"))
    if not destinations:
        return
    params = {
        "key": DM_KEY,
        "origins": f"{origin_lat},{origin_lng}",
        "destinations": destinations,
        "mode": "driving",
        "units": "imperial",
    }
    try:
        r = requests.get(DM_URL, params=params, timeout=10)
        r.raise_for_status()
        data = r.json()
        rows = data.get("rows") or []
        elems = (rows[0] or {}).get("elements") if rows else []
        i = 0
        for d in dests:
            if i >= len(elems):
                break
            el = elems[i]
            i += 1
            if (el or {}).get("status") != "OK":
                continue
            d["distance_text"] = (el.get("distance") or {}).get("text")
            d["duration_text"] = (el.get("duration") or {}).get("text")
    except Exception as e:
        logging.warning(f"Distance Matrix failed: {e}")


def _infer_categories(guest_text: str) -> List[Dict[str, Any]]:
    """Choose categories from the user’s message; fallback to a sensible default set."""
    t = (guest_text or "").lower()

    cats: List[Dict[str, Any]] = []
    def add(label, **kw): cats.append({"label": label, **kw})

    if any(k in t for k in ["coffee", "espresso", "latte", "breakfast", "brunch"]):
        add("Coffee & Breakfast", type_="cafe")
    if any(k in t for k in ["restaurant", "eat", "dinner", "lunch", "food", "bbq", "tacos", "pizza"]):
        add("Good Restaurants", type_="restaurant")
    if any(k in t for k in ["bar", "pub", "beer", "cocktail", "wine"]):
        add("Bars", type_="bar")
    if any(k in t for k in ["hike", "trail", "park", "outdoor", "nature"]):
        add("Hiking & Parks", keyword="hiking trail")
    if any(k in t for k in ["museum", "art", "history", "exhibit", "gallery"]):
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

    # cap to 3 categories to keep Slack + LLM prompt lean
    return cats[:3]


def should_fetch_local_recs(guest_text: str) -> bool:
    """Heuristic: only call APIs when the guest message asks about local stuff."""
    t = (guest_text or "").lower()
    triggers = [
        "things to do", "what to do", "recommend", "recommendations", "nearby", "around the house",
        "restaurants", "coffee", "brunch", "bar", "hike", "park", "museum", "groceries", "grocery",
        "where can we", "any good", "places to"
    ]
    return any(x in t for x in triggers)


def build_local_recs(lat: Optional[float], lng: Optional[float], guest_text: str) -> List[Dict[str, Any]]:
    """
    Returns a compact structure the LLM can use directly:
    [
      {"label": "Good Restaurants", "places": [
        {"name": "...", "vicinity": "...", "rating": 4.5, "distance_text": "1.2 mi", "duration_text": "6 mins", "maps_url": "..."},
        ...
      ]},
      ...
    ]
    """
    if lat is None or lng is None:
        return []

    categories = _infer_categories(guest_text)
    bundle: List[Dict[str, Any]] = []

    for cat in categories:
        places = _nearby(
            lat,
            lng,
            type_=cat.get("type_"),
            keyword=cat.get("keyword"),
            radius=cat.get("radius", 6000),
            max_results=5,
        )
        _distance_matrix(lat, lng, places)
        # Keep only the lean fields the model actually needs
        for p in places:
            p.pop("lat", None)
            p.pop("lng", None)
            p.pop("types", None)
            p.pop("user_ratings_total", None)
            p.pop("place_id", None)
        bundle.append({"label": cat["label"], "places": places})

    return bundle
