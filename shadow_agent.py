"""Shadow agent: command prefix and runner. Stub when not fully configured."""

import random

_UNRECOGNIZED_PHRASES = [
    "I didn't catch that — try **!help** for the full list.",
    "Not sure what you meant. Say **!help** for options.",
    "That one went over my head. **!help** shows what I can do.",
    "I didn't recognize that. **!help** has the full list.",
    "Hmm, not a command I know. **!help** will show you.",
    "No idea what that was — **!help** for commands.",
    "Couldn't parse that. Try **!help** to see what works.",
]

_COMMAND_HINTS = (
    "news",
    "msg",
    "share song",
    "play",
    "instagram dm",
    "share facebook",
    "yt comment",
    "pc vitals",
    "skip",
    "stop",
)


def _unrecognized_reply() -> str:
    """Return a dynamic reply when the command wasn't recognized."""
    base = random.choice(_UNRECOGNIZED_PHRASES)
    if random.random() < 0.5:
        n = min(2, len(_COMMAND_HINTS))
        hints = random.sample(_COMMAND_HINTS, n)
        hint_str = ", ".join(f"**{c}**" for c in hints)
        return f"{base} Or try: {hint_str}."
    return base


def strip_shadow_prefix(text: str) -> str | None:
    """Return stripped text after shadow prefix, or None if no prefix."""
    if not text or not isinstance(text, str):
        return None
    t = text.strip()
    for prefix in ("shadow", "!", "agent"):
        if t.lower().startswith(prefix):
            rest = t[len(prefix):].strip().lstrip(":,.")
            return rest if rest else None
    return None


def run_shadow(rest: str, scope: str, parse_command, run_cmd, *, log_fn=None, **kwargs) -> str:
    """Run shadow agent: parse natural-language command and execute via run_cmd. Extra kwargs (e.g. permission_fn, author_id) are ignored."""
    rest = (rest or "").strip()
    if not rest:
        return "Say what to do, e.g. **Shadow, news** or **Shadow, share facebook**. Use **!help** for the full list."
    parsed = parse_command(rest)
    if not parsed:
        return _unrecognized_reply()
    cmd, params = parsed
    reply = run_cmd(cmd, params, scope)
    if log_fn and reply:
        try:
            log_fn(cmd, params, reply)
        except Exception:
            pass
    return reply if reply else "Done."
