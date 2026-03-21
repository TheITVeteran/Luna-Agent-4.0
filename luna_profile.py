"""Luna profile: user profile fields per scope. Persisted to disk."""

from __future__ import annotations
import json, os, threading

_DATA = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")
_PROFILE_FILE = os.path.join(_DATA, "profiles.json")
_lock = threading.Lock()

PROFILE_FIELDS = ["name", "about", "style", "goals", "preferences"]

# ── Disk helpers ──────────────────────────────────────────────────────────────

def _load() -> dict:
    try:
        with open(_PROFILE_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, dict):
            return data
    except (FileNotFoundError, json.JSONDecodeError):
        pass
    return {}


def _save(data: dict) -> None:
    os.makedirs(_DATA, exist_ok=True)
    tmp = _PROFILE_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    os.replace(tmp, _PROFILE_FILE)

# ── Public API ────────────────────────────────────────────────────────────────

def get_profile_prompt(scope: str) -> str:
    p = get_profile(scope)
    parts = [f"{k}: {v}" for k, v in p.items() if v]
    return "Profile: " + "; ".join(parts) if parts else ""


def get_profile(scope: str) -> dict[str, str]:
    with _lock:
        data = _load()
    return dict(data.get(scope or "", {}))


def set_profile_field(scope: str, field: str, value: str) -> None:
    scope = scope or ""
    with _lock:
        data = _load()
        if scope not in data:
            data[scope] = {}
        data[scope][field.strip().lower()] = (value or "").strip()[:1000]
        _save(data)


def try_capture_profile_from_reply(scope: str, text: str) -> bool:
    return False


def clear_profile(scope: str) -> int:
    scope = scope or ""
    with _lock:
        data = _load()
        n = len(data.get(scope, {}))
        data[scope] = {}
        _save(data)
    return n


def merge_profiles(scope: str, legacy_scopes: list[str]) -> None:
    scope = scope or ""
    with _lock:
        data = _load()
        if scope not in data:
            data[scope] = {}
        for leg in legacy_scopes:
            for f, v in data.get(leg or "", {}).items():
                if v and (f not in data[scope] or not data[scope][f]):
                    data[scope][f] = v
        _save(data)
