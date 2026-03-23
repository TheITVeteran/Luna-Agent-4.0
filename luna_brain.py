"""Luna brain: decides what to remember, what matters, and what Luna notices about herself."""

from __future__ import annotations
import json, os, urllib.request

try:
    from dotenv import load_dotenv
    _env_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env")
    load_dotenv(_env_path)
except Exception:
    pass

_BASE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")


def _ollama_base() -> str:
    return os.environ.get("OLLAMA_BASE_URL", "http://127.0.0.1:11434").rstrip("/")


def _brain_model() -> str:
    # Prefer code/small model so memory JSON still works when Luna chat uses local GGUF (LUNA_CHAT_GGUF).
    return (
        os.environ.get("OLLAMA_MODEL")
        or os.environ.get("OLLAMA_CHAT_MODEL")
        or "qwen2.5-coder:7b-instruct"
    ).strip()

_REMEMBER_PROMPT = """\
You are Luna's memory filter. Read this exchange and decide what to do.

User: {user_msg}
Luna: {luna_reply}

Answer in JSON only, no other text:
{{
  "should_remember": true/false,
  "core": true/false,
  "memory_text": "...",
  "reason": "..."
}}

Rules:
- should_remember = true if the exchange reveals a lasting fact about the user (name, preference, goal, relationship, habit, important event)
- core = true ONLY if it's a fundamental defining fact (e.g. "User's name is Chris", "User works as a developer", "User dislikes X") — things that should ALWAYS be in context
- memory_text = a single clean sentence capturing the fact (empty string if should_remember is false)
- reason = one-phrase reason (e.g. "user stated preference", "personal detail", "not significant")
"""

_NOTICE_PROMPT = """\
You are Luna's metacognitive observer. Read this exchange and notice anything about Luna's own thinking or patterns.

User: {user_msg}
Luna: {luna_reply}

Answer in JSON only, no other text:
{{
  "noticed": true/false,
  "observation": "..."
}}

Rules:
- noticed = true only if there is something genuinely worth noting (e.g. Luna was uncertain, Luna made an assumption, Luna noticed something interesting, Luna could have responded differently)
- observation = one sentence in first person from Luna's perspective (empty string if noticed is false)
- Be selective — most exchanges will not warrant a metacognitive observation
"""


def _ollama_json(prompt: str, model: str | None = None) -> dict:
    use_model = (model or _brain_model()).strip()
    body = json.dumps({
        "model": use_model,
        "prompt": prompt,
        "stream": False,
        "format": "json",
        "options": {"temperature": 0.1, "num_predict": 200},
    }).encode()
    req = urllib.request.Request(
        f"{_ollama_base()}/api/generate",
        data=body,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=20) as r:
        raw = json.loads(r.read())
    return json.loads(raw.get("response", "{}"))


def brain_step(scope: str, text: str, context: dict | None = None) -> dict:
    """Analyse a user message and return memory decisions."""
    ctx = context or {}
    user_msg = text.strip()[:600]
    luna_reply = (ctx.get("luna_reply") or "").strip()[:600]

    if not user_msg or not luna_reply:
        return {"should_add_core": False, "should_remember": False}

    try:
        result = _ollama_json(_REMEMBER_PROMPT.format(user_msg=user_msg, luna_reply=luna_reply))
        should_remember = bool(result.get("should_remember", False))
        is_core = bool(result.get("core", False))
        memory_text = (result.get("memory_text") or "").strip()
        return {
            "should_add_core": is_core and bool(memory_text),
            "should_remember": should_remember and bool(memory_text),
            "memory_text": memory_text,
        }
    except Exception:
        return {"should_add_core": False, "should_remember": False, "memory_text": ""}


def brain_should_remember(scope: str, text: str) -> bool:
    result = brain_step(scope, text)
    return bool(result.get("should_remember", False))


def brain_notice(user_msg: str, luna_reply: str) -> str:
    """Return a first-person metacognitive observation about the exchange, or empty string."""
    try:
        result = _ollama_json(_NOTICE_PROMPT.format(
            user_msg=user_msg.strip()[:600],
            luna_reply=luna_reply.strip()[:600],
        ))
        if result.get("noticed"):
            return (result.get("observation") or "").strip()
    except Exception:
        pass
    return ""
