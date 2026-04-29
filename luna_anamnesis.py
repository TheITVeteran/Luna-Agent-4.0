import json
import urllib.error
import urllib.request


def _post_json(url: str, payload: dict, timeout_sec: float) -> dict | None:
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=body,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout_sec) as r:
            raw = r.read().decode("utf-8", errors="replace")
        data = json.loads(raw) if raw else {}
        return data if isinstance(data, dict) else None
    except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, OSError, json.JSONDecodeError):
        return None


def _extract_text(data: dict | None) -> str:
    if not isinstance(data, dict):
        return ""
    for k in ("summary", "text", "result", "memory", "recall"):
        v = data.get(k)
        if isinstance(v, str) and v.strip():
            return v.strip()
    items = data.get("items")
    if isinstance(items, list):
        parts = []
        for it in items[:8]:
            if isinstance(it, str) and it.strip():
                parts.append(it.strip())
            elif isinstance(it, dict):
                s = it.get("text") or it.get("summary") or it.get("memory") or ""
                if isinstance(s, str) and s.strip():
                    parts.append(s.strip())
        return "\n".join(parts).strip()
    return ""


def get_associative_memory_context(
    *,
    enabled: bool,
    endpoint: str,
    scope: str,
    user_message: str,
    timeout_sec: float,
    max_chars: int,
) -> str:
    """
    Optional secondary associative-memory retrieval.
    Keep this separate from 3-level memory so primary memory remains source of truth.
    """
    if not enabled:
        return ""
    ep = (endpoint or "").strip()
    msg = (user_message or "").strip()
    sc = (scope or "").strip() or "web"
    if not ep or len(msg) < 2:
        return ""

    payload = {
        "query": msg[:1200],
        "scope": sc,
        "max_items": 8,
        "mode": "associative_recall",
    }
    data = _post_json(ep, payload, timeout_sec)
    text = _extract_text(data)
    if not text:
        return ""
    text = text[: max(200, int(max_chars or 1100))]
    return (
        "## Associative memory (secondary)\n"
        "Use this only as supportive context. 3-level memory/profile remain the ground truth.\n"
        f"{text}"
    )

