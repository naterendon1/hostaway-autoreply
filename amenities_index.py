# path: amenities_index.py
from __future__ import annotations
from typing import Any, Dict, List, Optional, Tuple
import re

# Optional mapping if your account provides amenityId->name. Leave empty if unknown.
# You can fill a few critical ones you use often; unknown IDs will be kept as "amenity_123".
AMENITY_ID_TO_NAME: Dict[int, str] = {
    # 2: "Wi-Fi",
    # 3: "Parking",
    # ... add if you have the mapping from Hostaway
}

# Common synonyms → canonical keys
_AMENITY_SYNONYMS = {
    "wifi": {"wifi", "wi fi", "wi-fi", "internet"},
    "parking": {"parking", "garage", "driveway"},
    "pool": {"pool", "swimming pool"},
    "hot_tub": {"hot tub", "jacuzzi", "spa"},
    "ac": {"ac", "a/c", "air conditioning", "aircon"},
    "heating": {"heating", "heater", "heat"},
    "kitchen": {"kitchen", "full kitchen"},
    "washer": {"washer", "washing machine", "laundry"},
    "dryer": {"dryer", "tumble dryer", "laundry"},
    "dishwasher": {"dishwasher"},
    "tv": {"tv", "television", "smart tv"},
    "pets_allowed": {"pets", "pet friendly", "dogs", "cats"},
    "gym": {"gym", "fitness"},
    "elevator": {"elevator", "lift"},
    "balcony": {"balcony", "terrace", "patio"},
    "grill": {"grill", "bbq", "barbecue"},
    "crib": {"crib", "pack n play", "pack-and-play"},
    "ev_charger": {"ev charger", "ev charging", "electric vehicle charger"},
}

# Bed type IDs → human-friendly labels (extend as you learn your account’s IDs)
BEDTYPE_ID_TO_NAME: Dict[int, str] = {
    1: "King bed", 2: "Queen bed", 3: "Double bed", 4: "Single bed",
    5: "Sofa bed", 6: "Bunk bed", 7: "Futon", 8: "Crib",
    33: "Air mattress",  # example from your sample
    # Add/adjust to match your Hostaway account if it differs
}

def _slug(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", (s or "").lower()).strip("_")

def _canonicalize_name(raw_name: str) -> str:
    s = re.sub(r"[^a-z0-9]+", " ", (raw_name or "").lower()).strip()
    for key, syns in _AMENITY_SYNONYMS.items():
        if s in syns or any(tok in s for tok in syns):
            return key
    return _slug(s) or "unknown"

def _canonicalize_id(amenity_id: int) -> Tuple[str, str]:
    # returns canonical_key, display_name
    name = AMENITY_ID_TO_NAME.get(int(amenity_id))
    if name:
        canon = _canonicalize_name(name)
        return canon, name
    # unknown id → create stable key
    return f"amenity_{int(amenity_id)}", f"amenity_{int(amenity_id)}"

def _bool(v: Any) -> Optional[bool]:
    if isinstance(v, bool): return v
    if v in (0, 1): return bool(v)
    if isinstance(v, str):
        t = v.strip().lower()
        if t in {"true","yes","1"}: return True
        if t in {"false","no","0"}: return False
    return None

def _int(v: Any) -> Optional[int]:
    try:
        if v is None or v == "": return None
        return int(float(v))
    except Exception:
        return None

def _hhmm_from_24h(hour: Optional[int]) -> Optional[str]:
    if hour is None: return None
    try:
        h = int(hour)
    except Exception:
        return None
    if not (0 <= h <= 23): return None
    ampm = "AM" if h < 12 else "PM"
    hr = h % 12 or 12
    return f"{hr}:00 {ampm}"

class AmenitiesIndex:
    """
    Build a normalized amenity + facts index from Hostaway listing.result.
    Exposes:
      - amenities: Dict[canonical_key, bool]
      - meta: Dict of concrete values (counts/times/policies/wifi/etc.)
      - bed_types: Dict[label, int]
      - images: List[{caption, url, sort}]
    """

    def __init__(self, result: Dict[str, Any]):
        self.raw = result or {}

        # Amenity booleans and meta facts
        self.amenities: Dict[str, bool] = {}
        self.meta: Dict[str, Any] = {}

        # Counts/capacity
        self.meta["bedrooms"] = _int(self.raw.get("bedroomsNumber"))
        self.meta["bathrooms"] = _int(self.raw.get("bathroomsNumber"))
        self.meta["beds"] = _int(self.raw.get("bedsNumber"))
        self.meta["max_guests"] = _int(self.raw.get("personCapacity") or self.raw.get("guestsIncluded"))

        # Times/policies
        self.meta["check_in_start"] = _hhmm_from_24h(self.raw.get("checkInTimeStart"))
        self.meta["check_in_end"]   = _hhmm_from_24h(self.raw.get("checkInTimeEnd"))
        self.meta["check_out_time"] = _hhmm_from_24h(self.raw.get("checkOutTime"))
        self.meta["cancellation_policy"] = (self.raw.get("cancellationPolicy") or "").strip() or None

        # WiFi details
        self.meta["wifi_username"] = (self.raw.get("wifiUsername") or "").strip() or None
        self.meta["wifi_password"] = (self.raw.get("wifiPassword") or "").strip() or None
        if self.meta["wifi_username"] or self.meta["wifi_password"]:
            self.amenities["wifi"] = True

        # Pets from explicit/maxPetsAllowed or text
        pets_allowed_explicit = self.raw.get("maxPetsAllowed")
        if pets_allowed_explicit is not None:
            try:
                self.amenities["pets_allowed"] = int(pets_allowed_explicit) > 0
            except Exception:
                self.amenities["pets_allowed"] = bool(pets_allowed_explicit)

        desc_blob = " ".join(str(self.raw.get(k) or "") for k in ("description","houseRules","specialInstruction")).lower()
        if "no pets" in desc_blob or "pets not allowed" in desc_blob:
            self.amenities["pets_allowed"] = False
        if "pets allowed" in desc_blob or "pet friendly" in desc_blob:
            self.amenities["pets_allowed"] = True

        # Parking heuristic if not set by amenity list
        if any(w in desc_blob for w in ("parking", "garage", "driveway")):
            self.amenities.setdefault("parking", True)

        # listingAmenities: can be objects with amenityId OR name
        for item in (self.raw.get("listingAmenities") or []):
            amenity_id = item.get("amenityId")
            name = item.get("name") or item.get("amenityName")
            if name:
                key = _canonicalize_name(name)
                self.amenities[key] = True
            elif amenity_id is not None:
                key, _ = _canonicalize_id(int(amenity_id))
                self.amenities[key] = True

        # Images
        self.images: List[Dict[str, Any]] = []
        for im in (self.raw.get("listingImages") or []):
            cap = im.get("caption") or im.get("airbnbCaption") or im.get("vrboCaption") or ""
            self.images.append({
                "caption": cap,
                "url": im.get("url"),
                "sort": _int(im.get("sortOrder")) or 0,
            })
        self.images.sort(key=lambda x: x["sort"])

        # Bed types
        self.bed_types: Dict[str, int] = {}
        for bt in (self.raw.get("listingBedTypes") or []):
            bt_id = _int(bt.get("bedTypeId"))
            qty = _int(bt.get("quantity")) or 0
            if not bt_id or qty <= 0:
                continue
            label = BEDTYPE_ID_TO_NAME.get(bt_id, f"Bed type {bt_id}")
            self.bed_types[label] = self.bed_types.get(label, 0) + qty

        # Location/meta extras
        self.meta["address"] = self.raw.get("address") or None
        self.meta["city"] = self.raw.get("city") or None
        self.meta["state"] = self.raw.get("state") or None
        self.meta["country"] = self.raw.get("country") or None
        self.meta["square_meters"] = _int(self.raw.get("squareMeters"))
        self.meta["room_type"] = self.raw.get("roomType")  # entire_home/private_room/shared_room
        self.meta["bathroom_type"] = self.raw.get("bathroomType")  # private/shared

    # Query helpers
    def supports(self, key: str) -> Optional[bool]:
        k = _slug(key)
        if k in self.amenities: return bool(self.amenities[k])
        # try synonyms
        for canon, syns in _AMENITY_SYNONYMS.items():
            if k == canon or k.replace("_"," ") in syns or k in syns:
                return bool(self.amenities.get(canon))
        return None

    def value(self, key: str) -> Any:
        return self.meta.get(key)

    def find_images(self, keywords: str, limit: int = 3) -> List[Dict[str, Any]]:
        q = (keywords or "").lower()
        if not q:
            return self.images[:limit]
        hits = [im for im in self.images if (im.get("caption") or "").lower().find(q) >= 0]
        return (hits or self.images)[:limit]

    def to_api(self) -> Dict[str, Any]:
        return {
            "amenities": self.amenities,
            "meta": self.meta,
            "bed_types": self.bed_types,
            "images": self.images,
        }
