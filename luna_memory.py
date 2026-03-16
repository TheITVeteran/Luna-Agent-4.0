"""Luna memory: core/short/long-term memories per scope. In-memory stub."""

from __future__ import annotations

_CORE: dict[str, list[str]] = {}
_LONG: dict[str, list[str]] = {}
_SHORT: dict[str, list[str]] = {}

def _key(scope: str) -> str:
    return scope or ""

def get_memory_prompt(scope: str) -> str:
    core = get_core_memories(scope)
    long = get_long_term_memories(scope, 5)
    if not core and not long:
        return ""
    parts = []
    if core:
        parts.append("Core memories:\n" + "\n".join(f"- {m}" for m in core[:10]))
    if long:
        parts.append("Long-term:\n" + "\n".join(f"- {m}" for m in long[:5]))
    return "\n".join(parts) if parts else ""

def get_memories(scope: str) -> list[str]:
    return get_core_memories(scope) + get_long_term_memories(scope, 20) + get_short_term_memories(scope)

def get_core_memories(scope: str) -> list[str]:
    return list(_CORE.get(_key(scope), []))

def get_short_term_memories(scope: str) -> list[str]:
    return list(_SHORT.get(_key(scope), []))

def get_long_term_memories(scope: str, n: int = 10) -> list[str]:
    return list(_LONG.get(_key(scope), []))[:n]

def add_memory(scope: str, text: str) -> None:
    k = _key(scope)
    if k not in _LONG:
        _LONG[k] = []
    _LONG[k].append(text.strip()[:1500])
    _LONG[k] = _LONG[k][-100:]

def add_core_memory(scope: str, text: str) -> None:
    k = _key(scope)
    if k not in _CORE:
        _CORE[k] = []
    s = text.strip()[:1500]
    if s and s not in _CORE[k]:
        _CORE[k].append(s)
        _CORE[k] = _CORE[k][-50:]

def clear_memories(scope: str) -> int:
    k = _key(scope)
    n = len(_LONG.get(k, []))
    _LONG[k] = []
    _SHORT[k] = []
    return n

def clear_core_memories(scope: str) -> int:
    k = _key(scope)
    n = len(_CORE.get(k, []))
    _CORE[k] = []
    return n

def clear_all_memories(scope: str) -> tuple[int, int]:
    nc = clear_core_memories(scope)
    nl = clear_memories(scope)
    return nc, nl

def merge_memories(scope: str, legacy_scopes: list[str]) -> None:
    k = _key(scope)
    for leg in legacy_scopes:
        lk = _key(leg)
        for m in _CORE.get(lk, []):
            add_core_memory(scope, m)
        for m in _LONG.get(lk, []):
            add_memory(scope, m)
