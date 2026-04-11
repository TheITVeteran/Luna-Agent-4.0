"""
Twitch chat via IRC (irc.chat.twitch.tv): receive PRIVMSG and send replies with PRIVMSG.
OAuth token needs scopes: chat:read, chat:edit (for sending).
"""

from __future__ import annotations

import re
import socket
import ssl
import threading
from collections.abc import Callable

TWITCH_IRC_HOST = "irc.chat.twitch.tv"
TWITCH_IRC_PORT = 6697

_send_lock = threading.Lock()
_active_send: Callable[[str], None] | None = None


def normalize_oauth_token(token: str) -> str:
    t = (token or "").strip().strip('"').strip("'")
    if not t:
        return ""
    if not t.lower().startswith("oauth:"):
        t = "oauth:" + t
    return t


def send_twitch_chat_message(message: str) -> bool:
    """Send a line to the joined channel. Thread-safe. Twitch max 500 chars."""
    with _send_lock:
        fn = _active_send
    if not fn:
        return False
    try:
        fn(message)
        return True
    except Exception:
        return False


def _set_twitch_send_handler(fn: Callable[[str], None] | None) -> None:
    global _active_send
    with _send_lock:
        _active_send = fn


def parse_privmsg(line: str) -> tuple[str, str, str] | None:
    """
    Parse a Twitch IRC PRIVMSG line. Returns (login, display_name, text) or None.
    login/display may be empty if parsing fails partially.
    """
    if "PRIVMSG #" not in line:
        return None
    m = re.search(r"PRIVMSG #[^ ]+ :(.*)$", line)
    if not m:
        return None
    body = m.group(1).rstrip("\r\n")
    dm = re.search(r"display-name=([^;]*)", line)
    display = (dm.group(1).strip() if dm else "") or ""
    um = re.search(
        r":([^\s!]+)![^\s@]+@[^\s.]+\.tmi\.twitch\.tv\s+PRIVMSG",
        line,
    )
    login = um.group(1) if um else ""
    if not login:
        um2 = re.search(r":([^\s!]+)!", line)
        login = um2.group(1) if um2 else ""
    if not display:
        display = login
    return (login.lower(), display, body)


def run_twitch_irc_reader(
    channel: str,
    nick: str,
    oauth_token: str,
    on_privmsg: Callable[[str, str, str], None],
    stop_event: threading.Event,
    log: Callable[[str], None] | None = None,
) -> None:
    """
    Blocking loop: connect, join #channel, call on_privmsg(login, display_name, text) for each chat line.
    Register send_twitch_chat_message while connected. Reconnects until stop_event is set.
    """
    _log = log or (lambda s: None)
    ch = (channel or "").strip().lstrip("#").lower()
    name = (nick or "").strip().lower()
    pwd = normalize_oauth_token(oauth_token)
    if not ch or not name or not pwd:
        _log("[Twitch] Missing TWITCH_CHANNEL, TWITCH_BOT_USERNAME, or TWITCH_OAUTH_TOKEN.")
        return

    irc_lock = threading.Lock()

    while not stop_event.is_set():
        ssock = None
        try:
            _log(f"[Twitch] Connecting to {TWITCH_IRC_HOST} as {name} (#{ch})…")
            raw = socket.create_connection((TWITCH_IRC_HOST, TWITCH_IRC_PORT), timeout=30)
            ctx = ssl.create_default_context()
            ssock = ctx.wrap_socket(raw, server_hostname=TWITCH_IRC_HOST)
            ssock.settimeout(120.0)

            def send_raw(line: str) -> None:
                with irc_lock:
                    ssock.sendall((line + "\r\n").encode("utf-8"))

            def send_privmsg_to_channel(msg: str) -> None:
                m = (msg or "").replace("\r", " ").replace("\n", " ").strip()
                if not m:
                    return
                if len(m) > 500:
                    m = m[:497] + "..."
                send_raw(f"PRIVMSG #{ch} :{m}")

            send_raw(f"PASS {pwd}")
            send_raw(f"NICK {name}")
            send_raw("CAP REQ :twitch.tv/tags twitch.tv/commands")
            send_raw(f"JOIN #{ch}")

            _set_twitch_send_handler(send_privmsg_to_channel)

            buf = b""
            while not stop_event.is_set():
                try:
                    chunk = ssock.recv(4096)
                except socket.timeout:
                    continue
                if not chunk:
                    _log("[Twitch] Connection closed by server.")
                    break
                buf += chunk
                while b"\r\n" in buf:
                    line_b, buf = buf.split(b"\r\n", 1)
                    try:
                        line = line_b.decode("utf-8", errors="replace")
                    except Exception:
                        continue
                    if line.startswith("PING "):
                        send_raw("PONG :tmi.twitch.tv")
                        continue
                    parsed = parse_privmsg(line)
                    if not parsed:
                        continue
                    login, display, text = parsed
                    if not text.strip():
                        continue
                    try:
                        on_privmsg(login, display, text)
                    except Exception as e:
                        _log(f"[Twitch] on_privmsg error: {e}")
        except Exception as e:
            _log(f"[Twitch] IRC error: {e}")
        finally:
            _set_twitch_send_handler(None)
            try:
                if ssock:
                    ssock.close()
            except Exception:
                pass
        if stop_event.wait(timeout=5):
            break
        _log("[Twitch] Reconnecting in 5s…")
