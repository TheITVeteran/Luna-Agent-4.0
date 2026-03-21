"""Luna memory: core / short / long-term memories per scope. Persisted to disk."""

from __future__ import annotations
import json, os, threading

_DATA = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")
_MEMORY_FILE = os.path.join(_DATA, "memories.json")
_lock = threading.Lock()

# ── Disk helpers ──────────────────────────────────────────────────────────────

def _load() -> dict:
    try:
        with open(_MEMORY_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, dict):
            return data
    except (FileNotFoundError, json.JSONDecodeError):
        pass
    return {}


def _save(data: dict) -> None:
    os.makedirs(_DATA, exist_ok=True)
    tmp = _MEMORY_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    os.replace(tmp, _MEMORY_FILE)


def _get(data: dict, scope: str, tier: str) -> list[str]:
    return list(data.get(scope, {}).get(tier, []))


def _set(data: dict, scope: str, tier: str, items: list[str]) -> None:
    if scope not in data:
        data[scope] = {}
    data[scope][tier] = items

# ── Public API ────────────────────────────────────────────────────────────────

def get_memory_prompt(scope: str) -> str:
    with _lock:
        data = _load()
    core = _get(data, scope, "core")
    long = _get(data, scope, "long")[:5]
    short = _get(data, scope, "short")[-3:]  # most recent self-notices
    if not core and not long and not short:
        return ""
    parts = []
    if core:
        parts.append("Core memories:\n" + "\n".join(f"- {m}" for m in core[:10]))
    if long:
        parts.append("Long-term:\n" + "\n".join(f"- {m}" for m in long))
    if short:
        parts.append("Recent self-observations:\n" + "\n".join(f"- {m}" for m in short))
    return "\n".join(parts)


def get_memories(scope: str) -> list[str]:
    with _lock:
        data = _load()
    return (
        _get(data, scope, "core") +
        _get(data, scope, "long") +
        _get(data, scope, "short")
    )


def get_core_memories(scope: str) -> list[str]:
    with _lock:
        data = _load()
    return _get(data, scope, "core")


def get_short_term_memories(scope: str) -> list[str]:
    with _lock:
        data = _load()
    return _get(data, scope, "short")


def get_long_term_memories(scope: str, n: int = 10) -> list[str]:
    with _lock:
        data = _load()
    return _get(data, scope, "long")[:n]


def add_memory(scope: str, text: str) -> None:
    text = text.strip()[:1500]
    if not text:
        return
    with _lock:
        data = _load()
        items = _get(data, scope, "long")
        if text not in items:
            items.append(text)
        items = items[-100:]
        _set(data, scope, "long", items)
        _save(data)


def add_core_memory(scope: str, text: str) -> None:
    text = text.strip()[:1500]
    if not text:
        return
    with _lock:
        data = _load()
        items = _get(data, scope, "core")
        if text not in items:
            items.append(text)
        items = items[-50:]
        _set(data, scope, "core", items)
        _save(data)


def add_short_term_memory(scope: str, text: str) -> None:
    text = text.strip()[:500]
    if not text:
        return
    with _lock:
        data = _load()
        items = _get(data, scope, "short")
        items.append(text)
        items = items[-20:]
        _set(data, scope, "short", items)
        _save(data)


def clear_memories(scope: str) -> int:
    with _lock:
        data = _load()
        n = len(_get(data, scope, "long")) + len(_get(data, scope, "short"))
        if scope in data:
            data[scope]["long"] = []
            data[scope]["short"] = []
        _save(data)
    return n


def clear_core_memories(scope: str) -> int:
    with _lock:
        data = _load()
        n = len(_get(data, scope, "core"))
        if scope in data:
            data[scope]["core"] = []
        _save(data)
    return n


def clear_all_memories(scope: str) -> tuple[int, int]:
    with _lock:
        data = _load()
        nc = len(_get(data, scope, "core"))
        nl = len(_get(data, scope, "long")) + len(_get(data, scope, "short"))
        data[scope] = {"core": [], "long": [], "short": []}
        _save(data)
    return nc, nl


def merge_memories(scope: str, legacy_scopes: list[str]) -> None:
    with _lock:
        data = _load()
        for leg in legacy_scopes:
            for m in _get(data, leg, "core"):
                items = _get(data, scope, "core")
                if m not in items:
                    items.append(m)
                _set(data, scope, "core", items[-50:])
            for m in _get(data, leg, "long"):
                items = _get(data, scope, "long")
                if m not in items:
                    items.append(m)
                _set(data, scope, "long", items[-100:])
        _save(data)
