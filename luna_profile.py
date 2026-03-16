"""Luna profile: user profile fields per scope. In-memory stub."""

from __future__ import annotations

PROFILE_FIELDS = ["name", "about", "style", "goals", "preferences"]

_STORE: dict[str, dict[str, str]] = {}

def _key(scope: str) -> str:
    return scope or ""

def get_profile_prompt(scope: str) -> str:
    p = get_profile(scope)
    parts = [f"{k}: {v}" for k, v in p.items() if v]
    return "Profile: " + "; ".join(parts) if parts else ""

def get_profile(scope: str) -> dict[str, str]:
    return dict(_STORE.get(_key(scope), {}))

def set_profile_field(scope: str, field: str, value: str) -> None:
    k = _key(scope)
    if k not in _STORE:
        _STORE[k] = {}
    _STORE[k][field.strip().lower()] = (value or "").strip()[:1000]

def try_capture_profile_from_reply(scope: str, text: str) -> bool:
    return False

def clear_profile(scope: str) -> int:
    k = _key(scope)
    n = len(_STORE.get(k, {}))
    _STORE[k] = {}
    return n

def merge_profiles(scope: str, legacy_scopes: list[str]) -> None:
    k = _key(scope)
    if k not in _STORE:
        _STORE[k] = {}
    for leg in legacy_scopes:
        for f, v in _STORE.get(_key(leg), {}).items():
            if v and (f not in _STORE[k] or not _STORE[k][f]):
                _STORE[k][f] = v
