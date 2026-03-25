"""Luna conversation: recent exchanges per scope. Persisted to disk."""

from __future__ import annotations
import json, os, threading

_DATA = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")
_CONV_FILE = os.path.join(_DATA, "conversations.json")
_lock = threading.Lock()


def _key(scope: str) -> str:
    return (scope or "").strip()


def _load() -> dict[str, list[dict]]:
    try:
        with open(_CONV_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, dict):
            out: dict[str, list[dict]] = {}
            for k, v in data.items():
                if not isinstance(k, str) or not isinstance(v, list):
                    continue
                cleaned = []
                for msg in v:
                    if not isinstance(msg, dict):
                        continue
                    role = str(msg.get("role") or "").strip()
                    content = str(msg.get("content") or "")
                    if role in ("user", "assistant"):
                        cleaned.append({"role": role, "content": content})
                out[k] = cleaned[-200:]
            return out
    except (FileNotFoundError, json.JSONDecodeError):
        pass
    return {}


def _save(data: dict[str, list[dict]]) -> None:
    os.makedirs(_DATA, exist_ok=True)
    tmp = _CONV_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    os.replace(tmp, _CONV_FILE)


def get_recent_conversation(scope: str, n: int = 30) -> list[dict]:
    """Return list of {role, content} for context."""
    k = _key(scope)
    with _lock:
        data = _load()
        return list(data.get(k, []))[-n:]


def append_exchange(scope: str, user_msg: str, assistant_reply: str) -> None:
    k = _key(scope)
    with _lock:
        data = _load()
        if k not in data:
            data[k] = []
        data[k].append({"role": "user", "content": user_msg or ""})
        data[k].append({"role": "assistant", "content": assistant_reply or ""})
        data[k] = data[k][-200:]
        _save(data)


def merge_conversations(scope: str, legacy_scopes: list[str]) -> None:
    k = _key(scope)
    with _lock:
        data = _load()
        base = list(data.get(k, []))
        for leg in legacy_scopes:
            base.extend(data.get(_key(leg), []))
        if base:
            data[k] = base[-200:]
            _save(data)


def get_recent_user_messages(scope: str, n: int = 20) -> list[str]:
    k = _key(scope)
    with _lock:
        data = _load()
        return [m["content"] for m in data.get(k, []) if m.get("role") == "user"][-n:]


def count_user_messages(scope: str) -> int:
    k = _key(scope)
    with _lock:
        data = _load()
        return sum(1 for m in data.get(k, []) if m.get("role") == "user")
