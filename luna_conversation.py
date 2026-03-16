"""Luna conversation: recent exchanges per scope. In-memory stub."""

from __future__ import annotations

_STORE: dict[str, list[dict]] = {}

def _key(scope: str) -> str:
    return scope or ""

def get_recent_conversation(scope: str, n: int = 30) -> list[dict]:
    """Return list of {role, content} for context."""
    return list(_STORE.get(_key(scope), []))[-n:]

def append_exchange(scope: str, user_msg: str, assistant_reply: str) -> None:
    k = _key(scope)
    if k not in _STORE:
        _STORE[k] = []
    _STORE[k].append({"role": "user", "content": user_msg or ""})
    _STORE[k].append({"role": "assistant", "content": assistant_reply or ""})
    _STORE[k] = _STORE[k][-200:]

def merge_conversations(scope: str, legacy_scopes: list[str]) -> None:
    k = _key(scope)
    base = list(_STORE.get(k, []))
    for leg in legacy_scopes:
        base.extend(_STORE.get(_key(leg), []))
    _STORE[k] = base[-200:] if base else []

def get_recent_user_messages(scope: str, n: int = 20) -> list[str]:
    return [m["content"] for m in _STORE.get(_key(scope), []) if m.get("role") == "user"][-n:]

def count_user_messages(scope: str) -> int:
    return sum(1 for m in _STORE.get(_key(scope), []) if m.get("role") == "user")
