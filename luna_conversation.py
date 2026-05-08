"""Luna conversation: recent exchanges per scope. Persisted to disk.

Also owns a rolling per-scope summary (``data/conversation_summaries.json``) that
captures the gist of older exchanges as they fall outside the active prompt
window — so Luna does not "forget" beyond the 200-message conversation cap.
"""

from __future__ import annotations
import json, os, threading
from datetime import datetime, timezone

_DATA = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")
_CONV_FILE = os.path.join(_DATA, "conversations.json")
_SUMMARY_FILE = os.path.join(_DATA, "conversation_summaries.json")
_lock = threading.Lock()
_summary_lock = threading.Lock()

# Hard cap on the rolling summary length per scope. Keeps it within prompt budget
# even after many merge cycles. The merge pass is responsible for staying within.
ROLLING_SUMMARY_MAX_CHARS = 2400


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


# ── Rolling per-scope summary ────────────────────────────────────────────────
# State per scope:
#   {
#     "summary": str,              # the digest itself (capped to ROLLING_SUMMARY_MAX_CHARS)
#     "last_user_hash": str,       # sha1 of the last *user* message already incorporated
#     "msgs_in_summary": int,      # total exchanges incorporated (book-keeping)
#     "updated_at": ISO8601,
#   }


def _load_summaries() -> dict[str, dict]:
    try:
        with open(_SUMMARY_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, dict):
            return {k: v for k, v in data.items() if isinstance(k, str) and isinstance(v, dict)}
    except (FileNotFoundError, json.JSONDecodeError):
        pass
    return {}


def _save_summaries(data: dict[str, dict]) -> None:
    os.makedirs(_DATA, exist_ok=True)
    tmp = _SUMMARY_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    os.replace(tmp, _SUMMARY_FILE)


def get_rolling_summary(scope: str) -> str:
    """Return the rolling summary text for `scope`, or "" if none yet."""
    k = _key(scope)
    if not k:
        return ""
    with _summary_lock:
        data = _load_summaries()
        entry = data.get(k) or {}
    return str(entry.get("summary") or "").strip()


def get_rolling_summary_state(scope: str) -> dict:
    """Return the full state dict for `scope` (summary, last_user_hash, msgs_in_summary, updated_at)."""
    k = _key(scope)
    if not k:
        return {}
    with _summary_lock:
        data = _load_summaries()
        entry = data.get(k) or {}
    return {
        "summary": str(entry.get("summary") or "").strip(),
        "last_user_hash": str(entry.get("last_user_hash") or "").strip(),
        "msgs_in_summary": int(entry.get("msgs_in_summary") or 0),
        "updated_at": str(entry.get("updated_at") or ""),
    }


def set_rolling_summary(
    scope: str,
    summary: str,
    *,
    last_user_hash: str = "",
    msgs_added: int = 0,
) -> None:
    """Persist `summary` as the rolling digest for `scope`.

    `msgs_added` is added to the prior `msgs_in_summary` counter (book-keeping).
    `last_user_hash` should be the sha1 of the most recent *user* message that
    was incorporated into this summary, so the next merge can skip everything
    older without re-reading it.
    """
    k = _key(scope)
    if not k:
        return
    summary = (summary or "").strip()[:ROLLING_SUMMARY_MAX_CHARS]
    with _summary_lock:
        data = _load_summaries()
        prior = data.get(k) or {}
        entry = {
            "summary": summary,
            "last_user_hash": (last_user_hash or "").strip() or str(prior.get("last_user_hash") or ""),
            "msgs_in_summary": int(prior.get("msgs_in_summary") or 0) + max(0, int(msgs_added or 0)),
            "updated_at": datetime.now(timezone.utc).isoformat(),
        }
        if not summary and not entry["last_user_hash"]:
            data.pop(k, None)
        else:
            data[k] = entry
        _save_summaries(data)


def clear_rolling_summary(scope: str) -> bool:
    """Drop the rolling summary for `scope`. Returns True if one existed."""
    k = _key(scope)
    if not k:
        return False
    with _summary_lock:
        data = _load_summaries()
        had = k in data
        data.pop(k, None)
        if had:
            _save_summaries(data)
    return had


def list_rolling_summary_scopes() -> list[str]:
    with _summary_lock:
        data = _load_summaries()
    return sorted(data.keys())
