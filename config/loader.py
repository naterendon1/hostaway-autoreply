# file: config/loader.py
import os, json
from typing import Any, Dict

CONFIG_DIR = os.getenv("LISTING_CONFIG_DIR", "config/listings")

def _read_json(path: str) -> Dict[str, Any]:
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f) or {}
    except Exception:
        return {}

def _deep_merge(a: Any, b: Any) -> Any:
    # prefer specific (b) over base (a), recursively
    if isinstance(a, dict) and isinstance(b, dict):
        out = dict(a)
        for k, v in b.items():
            out[k] = _deep_merge(out.get(k), v)
        return out
    return b if b is not None else a

def load_listing_config(listing_id: int | str | None) -> Dict[str, Any]:
    """
    Loads config/listings/default.json and then overlays config/listings/<listing_id>.json
    Returns {} if nothing found (safe no-op).
    """
    base = CONFIG_DIR
    default_cfg = _read_json(os.path.join(base, "default.json"))
    if not listing_id:
        return default_cfg
    specific = _read_json(os.path.join(base, f"{listing_id}.json"))
    return _deep_merge(default_cfg, specific)
