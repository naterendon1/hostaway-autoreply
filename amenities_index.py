# path: amenities_index.py
from __future__ import annotations
from typing import Any, Dict, List, Optional, Tuple
import os, json, re

def _load_id_map(path_env: str) -> Dict[int, str]:
    p = os.getenv(path_env, "").strip()
    if not p:
        return {}
    try:
        with open(p, "r", encoding="utf-8") as f:
            data = json.load(f)
        out: Dict[int, str] = {}
        if isinstance(data, dict):
            for k, v in data.items():
                try:
                    out[int(k)] = str(v)
                except Exception:
                    continue
        elif isinstance(data, list):
            for item in data:
                try:
                    out[int(item.get("id"))] = str(item.get("name"))
                except Exception:
                    pass
        return out
    except Exception:
        return {}

_AMENITY_ID_NAME = _load_id_map("AMENITY_ID_MAP_PATH")
_BEDTYPE_ID_NAME = _load_id_map("BEDTYPE_ID_MAP_PATH")

_SYN_MAP = {
    "wifi": {"wifi","wi fi","wi-fi","internet"},
    "parking": {"parking","garage","driveway"},
    "pool": {"pool","swimming pool"},
    "hot_tub": {"hot tub","jacuzzi","spa"},
    "ac": {"ac","a/c","air conditioning","aircon"},
    "heating": {"heating","heater","heat"},
    "kitchen": {"kitchen","full kitchen"},
    "washer": {"washer","washing machine","laundry"},
    "dryer": {"dryer","tumble dryer","laundry"},
    "dishwasher": {"dishwasher"},
    "tv": {"tv","television","smart tv"},
    "pets_allowed": {"pets","pet friendly","dogs","cats"},
    "gym": {"gym","fitness"},
    "elevator": {"elevator","lift"},
    "balcony": {"balcony","terrace","patio"},
    "grill": {"grill","bbq","barbecue"},
    "crib": {"crib","pack n play","pack-and-play"},
    "ev_charger": {"ev charger","ev charging","electric vehicle charger"},
}

def _slug(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", (s or "").lower()).strip("_")

def _norm_text(s: Any) -> str:
    return re.sub(r"\s+", " ", str(s or "")).strip()

def _canonical_from_name(raw_name: str) -> str:
    s = re.sub(r"[^a-z0-9]+", " ", (raw_name or "").lower()).strip()
    for key, syns in _SYN_MAP.items():
        if s in syns or any(tok in s for tok in syns):
            return key
    return _slug(s) or "unknown"

def _hhmm_from_24h(hour: Any) -> Optional[str]:
    try:
        h = int(hour)
    except Exception:
        return None
    if 0 <= h <= 23:
        ampm = "AM" if h < 12 else "PM"
        hr = h % 12 or 12
        return f"{hr}:00 {ampm}"
    return None

def _is_scalar(v: Any) -> bool:
    return isinstance(v, (str, int, float, bool)) or v is None

class AmenitiesIndex:
    def __init__(self, listing_result: Dict[str, Any]):
        self.raw: Dict[str, Any] = listing_result or {}
        self.amenities: Dict[str, bool] = {}
        self.amenity_labels: Dict[str, str] = {}
        self.meta: Dict[str, Any] = {}
        self.bed_types: Dict[str, int] = {}
        self.images: List[Dict[str, Any]] = []
        self.custom_fields: Dict[str, Any] = {}
        self._corpus: List[Tuple[str, str, str]] = []

        self._ingest_meta_scalars()
        self._ingest_times_wifi_pets_parking_text()
        self._ingest_listing_amenities()
        self._ingest_bed_types()
        self._ingest_images()
        self._ingest_custom_fields()
        self._build_corpus()

    def _ingest_meta_scalars(self) -> None:
        for k, v in (self.raw or {}).items():
            if _is_scalar(v):
                self.meta[k] = v
        self.meta["check_in_start"] = _hhmm_from_24h(self.raw.get("checkInTimeStart"))
        self.meta["check_in_end"] = _hhmm_from_24h(self.raw.get("checkInTimeEnd"))
        self.meta["check_out_time"] = _hhmm_from_24h(self.raw.get("checkOutTime"))

    def _ingest_times_wifi_pets_parking_text(self) -> None:
        if (self.raw.get("wifiUsername") or self.raw.get("wifiPassword")):
            self.amenities["wifi"] = True
            self.amenity_labels.setdefault("wifi", "Wi-Fi")
        desc_blob = " ".join(_norm_text(self.raw.get(k)) for k in ("description","houseRules","specialInstruction")).lower()
        mp = self.raw.get("maxPetsAllowed")
        if mp is not None:
            try:
                self.amenities["pets_allowed"] = int(mp) > 0
            except Exception:
                self.amenities["pets_allowed"] = bool(mp)
            self.amenity_labels.setdefault("pets_allowed", "Pets allowed")
        if "no pets" in desc_blob or "pets not allowed" in desc_blob:
            self.amenities["pets_allowed"] = False
            self.amenity_labels.setdefault("pets_allowed", "Pets allowed")
        if "pets allowed" in desc_blob or "pet friendly" in desc_blob:
            self.amenities["pets_allowed"] = True
            self.amenity_labels.setdefault("pets_allowed", "Pets allowed")
        if any(w in desc_blob for w in ("parking","garage","driveway")):
            self.amenities.setdefault("parking", True)
            self.amenity_labels.setdefault("parking", "Parking")

    def _ingest_listing_amenities(self) -> None:
        for item in (self.raw.get("listingAmenities") or []):
            name = item.get("name") or item.get("amenityName")
            amenity_id = item.get("amenityId")
            if name:
                key = _canonical_from_name(name)
                self.amenities[key] = True
                self.amenity_labels.setdefault(key, name.strip())
            elif amenity_id is not None:
                disp = _AMENITY_ID_NAME.get(int(amenity_id), f"amenity_{int(amenity_id)}")
                key = _canonical_from_name(disp)
                self.amenities[key] = True
                self.amenity_labels.setdefault(key, disp)

    def _ingest_bed_types(self) -> None:
        for bt in (self.raw.get("listingBedTypes") or []):
            bt_id = bt.get("bedTypeId")
            qty = bt.get("quantity")
            try:
                qty = int(qty)
            except Exception:
                qty = 0
            if qty <= 0:
                continue
            label = None
            if bt_id is not None:
                label = _BEDTYPE_ID_NAME.get(int(bt_id)) or f"Bed type {int(bt_id)}"
            self.bed_types[label or "Bed"] = self.bed_types.get(label or "Bed", 0) + qty

    def _ingest_images(self) -> None:
        for im in (self.raw.get("listingImages") or []):
            cap = im.get("caption") or im.get("airbnbCaption") or im.get("vrboCaption") or ""
            url = im.get("url")
            if not url:
                continue
            sort = 0
            try:
                sort = int(im.get("sortOrder") or 0)
            except Exception:
                pass
            self.images.append({"caption": cap, "url": url, "sort": sort})
        self.images.sort(key=lambda x: x["sort"])

    def _ingest_custom_fields(self) -> None:
        for cf in (self.raw.get("customFieldValues") or []):
            key = re.sub(r"[^a-z0-9]+","_", str(cf.get("name") or cf.get("key") or cf.get("id") or "custom").lower()).strip("_")
            val = cf.get("value")
            if key:
                self.custom_fields[key] = val

    def _build_corpus(self) -> None:
        for key, val in (self.amenities or {}).items():
            label = self.amenity_labels.get(key, key.replace("_"," ").title())
            vtxt = "yes" if val else "no"
            self._corpus.append((f"amenity:{key}", label, vtxt))
            for syn_key, syns in _SYN_MAP.items():
                if syn_key == key:
                    for s in syns:
                        self._corpus.append((f"amenity:{key}", s, vtxt))
        for k, v in (self.meta or {}).items():
            if _is_scalar(v):
                self._corpus.append((f"meta:{_slug(k)}", k, _norm_text(v)))
        for name, qty in (self.bed_types or {}).items():
            self._corpus.append((f"bedtype:{_slug(name)}", name, str(qty)))
        for k, v in (self.custom_fields or {}).items():
            self._corpus.append((f"custom:{_slug(k)}", k, _norm_text(v)))
        for im in self.images:
            cap = _norm_text(im.get("caption"))
            if cap:
                self._corpus.append(("image:caption", "image", cap))

    def supports(self, key_or_name: str) -> Optional[bool]:
        k = _canonical_from_name(key_or_name)
        if k in self.amenities:
            return bool(self.amenities[k])
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
            "amenity_labels": self.amenity_labels,
            "meta": self.meta,
            "bed_types": self.bed_types,
            "images": self.images,
            "custom_fields": self.custom_fields,
        }

    def search(self, query: str, topk: int = 5) -> List[Dict[str, str]]:
        q = _norm_text(query).lower()
        if not q:
            return []
        toks = [t for t in re.split(r"[^a-z0-9]+", q) if t]
        if not toks:
            return []
        scored: List[Tuple[float, Tuple[str,str,str]]] = []
        for key, label, val in self._corpus:
            text = f"{label} {val}".lower()
            score = 0.0
            for t in toks:
                if t and t in text:
                    score += 1.0 + (0.5 if t in (label.lower()) else 0.0)
            if score > 0:
                scored.append((score, (key, label, val)))
        scored.sort(key=lambda x: -x[0])
        out = []
        for s, (k, l, v) in scored[:topk]:
            out.append({"key": k, "label": l, "value": v})
        return out
