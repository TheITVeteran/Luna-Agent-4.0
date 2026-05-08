"""
Luna 4.5 — compact rewrite of bot.py
Discord bot + Web UI + Ollama chat + Shadow commands + TTS/STT + automation
Run: python bot.py
"""
import asyncio, base64, concurrent.futures, hashlib, html, io, json, logging, os, queue, random, re, shutil, socket, struct, subprocess, sys
import tempfile, textwrap, threading, time, urllib.parse, urllib.request, urllib.error
import uuid, webbrowser, xml.etree.ElementTree as ET
import wave
from collections import deque
from datetime import datetime, timezone, timedelta
from zoneinfo import ZoneInfo
from email.utils import parsedate_to_datetime

import discord
from discord.ext import commands
from dotenv import load_dotenv
from flask import Flask, request, jsonify, send_from_directory, Response, stream_with_context, redirect
try:
    import websocket as _wsclient  # websocket-client (optional, for OBS WebSocket status)
except Exception:
    _wsclient = None

from luna_files import write_file as luna_write_file
from shadow_agent import strip_shadow_prefix, run_shadow as shadow_run
import celine
from luna_memory import (get_memory_prompt, get_core_memories,
    get_short_term_memories, get_long_term_memories, add_memory, add_core_memory,
    clear_memories, clear_all_memories, merge_memories)
from luna_profile import (get_profile_prompt, get_profile, set_profile_field,
    clear_profile, PROFILE_FIELDS, merge_profiles)
from luna_conversation import (
    get_recent_conversation, append_exchange, merge_conversations,
    get_rolling_summary, get_rolling_summary_state, set_rolling_summary,
    clear_rolling_summary, list_rolling_summary_scopes,
    ROLLING_SUMMARY_MAX_CHARS,
)
from luna_anamnesis import get_associative_memory_context
import luna_social
try:
    from luna_twitch import run_twitch_irc_reader, send_twitch_chat_message
except ImportError:
    run_twitch_irc_reader = None
    def send_twitch_chat_message(_msg: str, *, channel: str | None = None) -> bool:
        return False
try:
    from luna_security import run_full_scan, scan_file as security_scan_file
except ImportError:
    run_full_scan = None
    security_scan_file = None
try:
    from luna_publish_announce import poll_publish_announce as _poll_publish_announce
except ImportError:
    _poll_publish_announce = None

# ── Config ──────────────────────────────────────────────────────────────────
_BASE = os.path.dirname(os.path.abspath(__file__))
_DATA = os.path.join(_BASE, "data")
_NGROK_STATE_PATH = os.path.join(_DATA, "ngrok_state.json")

def _env(key, default=""):
    return os.environ.get(key, default).strip()

# Load .env
_env_path = os.path.join(_BASE, ".env")
try:
    with open(_env_path, encoding="utf-8-sig") as f:
        for line in f:
            line = line.strip()
            if "=" in line and not line.startswith("#"):
                k, v = line.split("=", 1)
                os.environ[k.strip()] = v.strip().strip('"').strip("'")
except Exception:
    pass
load_dotenv(_env_path)

DISCORD_TOKEN = (sys.argv[1].strip() if len(sys.argv) > 1
    else _env("DISCORD_TOKEN"))

OLLAMA_BASE  = _env("OLLAMA_BASE_URL", "http://127.0.0.1:11434").rstrip("/")
OLLAMA_MODEL = _env("OLLAMA_MODEL", "llama3.2:latest")
# Discord + web conversation model; defaults to OLLAMA_MODEL. Set OLLAMA_CHAT_MODEL to override (e.g. LISA GGUF label).
OLLAMA_CHAT = (_env("OLLAMA_CHAT_MODEL", "").strip() or OLLAMA_MODEL)
# Local GGUF file (Hugging Face / llama.cpp). When set and the file exists, Luna chat uses llama-cpp-python instead of Ollama.
LUNA_CHAT_GGUF = _env("LUNA_CHAT_GGUF", "").strip()
LUNA_CHAT_GGUF_N_CTX = int(_env("LUNA_CHAT_GGUF_N_CTX", "8192") or "8192")
LUNA_CHAT_GGUF_N_GPU = int(_env("LUNA_CHAT_GGUF_N_GPU", "-1") or "-1")
LUNA_CHAT_GGUF_THREADS = int(_env("LUNA_CHAT_GGUF_THREADS", "0") or "0")
OLLAMA_SMALL = _env("OLLAMA_MODEL_SMALL") or OLLAMA_MODEL
OLLAMA_FALLBACK = _env("OLLAMA_FALLBACK_MODEL", "qwen2.5:1.5b").strip()
OLLAMA_RATE_LIMIT_FALLBACK = _env("OLLAMA_RATE_LIMIT_FALLBACK_MODEL", "llama3.2:latest").strip()
# Vision model for camera/screen reads. Defaults to the main chat model when not explicitly set.
OLLAMA_VISION_MODEL = _env("OLLAMA_VISION_MODEL", OLLAMA_CHAT).strip()
OLLAMA_VISION_TIMEOUT = max(20, min(240, int(_env("OLLAMA_VISION_TIMEOUT", "90") or "90")))
# While VRM camera/screen is active, reuse the last analyzed frame for this many seconds (ambient context per chat).
LUNA_VISION_CONTEXT_TTL_SEC = max(30, min(900, int(_env("LUNA_VISION_CONTEXT_TTL_SEC", "240") or "240")))
# Provider adapters (Step 3): keep Ollama default; optionally route through OpenAI-compatible endpoints.
LUNA_CHAT_PROVIDER = (_env("LUNA_CHAT_PROVIDER", "ollama").strip().lower() or "ollama")
LUNA_VISION_PROVIDER = (_env("LUNA_VISION_PROVIDER", LUNA_CHAT_PROVIDER).strip().lower() or LUNA_CHAT_PROVIDER)
# Ollama vision must use an Ollama model tag. If chat is Gemma 4 locally but OLLAMA_VISION_MODEL names a cloud model, route frames to chat.
_vprov0 = (LUNA_VISION_PROVIDER or "ollama").strip().lower()
_chat_m0 = (OLLAMA_CHAT or "").strip().lower()
_gemma4_chat = _chat_m0.startswith("gemma4") or "gemma-4" in _chat_m0 or "gemma4" in _chat_m0
_vm0 = (OLLAMA_VISION_MODEL or "").strip().lower()
if _vprov0 in ("ollama", "gguf") and _vm0 in ("moondream2", "moondream-2", "moondream"):
    # Normalize common aliases to the installed Ollama tag.
    OLLAMA_VISION_MODEL = "moondream:1.8b"
    _vm0 = OLLAMA_VISION_MODEL
if _vprov0 in ("ollama", "gguf") and _gemma4_chat and _vm0 and (
    "gemini" in _vm0 or _vm0.startswith("gpt-") or "claude" in _vm0
):
    OLLAMA_VISION_MODEL = (OLLAMA_CHAT or OLLAMA_MODEL).strip()
# Normalize common Moondream chat aliases; keep vision tag aligned when chat is Moondream (single-model setup).
if _vprov0 in ("ollama", "gguf"):
    _cc_moondream = (OLLAMA_CHAT or "").strip().lower()
    if _cc_moondream in ("moondream2", "moondream-2", "moondream"):
        OLLAMA_CHAT = "moondream:1.8b"
    _lc_low = (OLLAMA_CHAT or "").strip().lower()
    if _lc_low.startswith("moondream"):
        OLLAMA_VISION_MODEL = (OLLAMA_CHAT or "").strip()
LUNA_OPENAI_BASE_URL = (_env("LUNA_OPENAI_BASE_URL", "https://api.openai.com/v1").strip().rstrip("/") or "https://api.openai.com/v1")
LUNA_OPENAI_API_KEY = (_env("LUNA_OPENAI_API_KEY", "").strip() or _env("OPENAI_API_KEY", "").strip())
LUNA_OPENAI_CHAT_MODEL = (_env("LUNA_OPENAI_CHAT_MODEL", "").strip() or OLLAMA_CHAT)
LUNA_OPENAI_VISION_MODEL = (_env("LUNA_OPENAI_VISION_MODEL", "").strip() or LUNA_OPENAI_CHAT_MODEL)
LUNA_GROQ_BASE_URL = (_env("LUNA_GROQ_BASE_URL", "https://api.groq.com/openai/v1").strip().rstrip("/") or "https://api.groq.com/openai/v1")
LUNA_GROQ_CHAT_MODEL = (_env("LUNA_GROQ_CHAT_MODEL", "").strip() or OLLAMA_CHAT)
# Shared model call guards (queue + timeout safety for chat/vision bursts)
_MODEL_CHAT_MAX_CONCURRENCY = max(1, int(_env("LUNA_CHAT_MAX_CONCURRENCY", "1") or "1"))
_MODEL_VISION_MAX_CONCURRENCY = max(1, int(_env("LUNA_VISION_MAX_CONCURRENCY", "1") or "1"))
_MODEL_CHAT_QUEUE_WAIT_SEC = max(1, int(_env("LUNA_CHAT_QUEUE_WAIT_SEC", "20") or "20"))
_MODEL_VISION_QUEUE_WAIT_SEC = max(1, int(_env("LUNA_VISION_QUEUE_WAIT_SEC", "12") or "12"))
_MODEL_CHAT_GUARD_TIMEOUT_SEC = max(10, int(_env("LUNA_CHAT_GUARD_TIMEOUT_SEC", "140") or "140"))
_MODEL_VISION_GUARD_TIMEOUT_SEC = max(10, int(_env("LUNA_VISION_GUARD_TIMEOUT_SEC", "75") or "75"))
# Public website mode: lock Flask routes so ngrok can expose only safe website endpoints.
LUNA_WEBSITE_PUBLIC_MODE = _env("LUNA_WEBSITE_PUBLIC_MODE", "0").strip().lower() in ("1", "true", "yes", "on")
# Optional secondary associative-memory layer (Anamnesis bridge). Keep 3-level memory as source of truth.
LUNA_ANAMNESIS_ENABLED = _env("LUNA_ANAMNESIS_ENABLED", "0").strip().lower() in ("1", "true", "yes", "on")
LUNA_ANAMNESIS_ENDPOINT = _env("LUNA_ANAMNESIS_ENDPOINT", "").strip()
LUNA_ANAMNESIS_TIMEOUT_SEC = max(1.5, min(20.0, float(_env("LUNA_ANAMNESIS_TIMEOUT_SEC", "4.0") or "4.0")))
LUNA_ANAMNESIS_MAX_CHARS = max(200, min(3000, int(_env("LUNA_ANAMNESIS_MAX_CHARS", "1100") or "1100")))

_chat_call_slots = threading.BoundedSemaphore(_MODEL_CHAT_MAX_CONCURRENCY)
_vision_call_slots = threading.BoundedSemaphore(_MODEL_VISION_MAX_CONCURRENCY)
# Playwright social flows: Granite observes viewport JPEGs for CAPTCHA/challenge UI (set LUNA_CAPTCHA_OBSERVER=0 to disable).
# Main chat: short system injection + skip inner-monologue pre-call (faster TTFT). Set LUNA_CHAT_FAST=0 for full prompts.
# Discord @/DM: LUNA_DISCORD_CHAT_FAST, LUNA_DISCORD_CHAT_HISTORY, LUNA_DISCORD_HISTORY_MSG_MAX_CHARS;
# LUNA_DISCORD_CHAT_MODEL / LUNA_DISCORD_FALLBACK_CHAT_MODEL (text model) when OLLAMA_CHAT is VL-only.
def _chat_fast_enabled() -> bool:
    v = _env("LUNA_CHAT_FAST", "1").strip().lower()
    return v not in ("0", "false", "no", "off")


def _discord_chat_prompt_fast() -> bool:
    """Discord @/DM turns: compact system + short history by default (smaller prompts, faster turns).
    Set LUNA_DISCORD_CHAT_FAST=0 to use the same full Luna stack as when LUNA_CHAT_FAST=0."""
    return _env("LUNA_DISCORD_CHAT_FAST", "1").strip().lower() not in ("0", "false", "no", "off")


def _discord_chat_history_limit() -> int:
    try:
        n = int((_env("LUNA_DISCORD_CHAT_HISTORY", "10") or "10").strip())
    except Exception:
        n = 10
    return max(4, min(30, n))


def _discord_history_message_cap() -> int:
    try:
        v = int((_env("LUNA_DISCORD_HISTORY_MSG_MAX_CHARS", "1200") or "1200").strip())
    except Exception:
        v = 1200
    return max(400, min(v, 8000))


def _discord_trim_chat_messages(messages: list[dict] | None) -> list[dict]:
    """Avoid huge pasted logs in Discord history blowing up the Ollama payload."""
    if not messages:
        return []
    cap = _discord_history_message_cap()
    out: list[dict] = []
    for m in messages:
        if not isinstance(m, dict):
            continue
        mm = dict(m)
        c = mm.get("content")
        if isinstance(c, str) and len(c) > cap:
            mm["content"] = c[: cap - 3].rstrip() + "..."
        out.append(mm)
    return out


# ── Voice (wake-word + mic) pipeline knobs ────────────────────────────────────
# Replaces the old fixed-5s recording with VAD-gated capture, optional speculative
# LLM-on-stable-partial firing, and sentence-buffered streaming TTS. All default ON
# but every layer can be turned off independently via env.
def _voice_vad_enabled() -> bool:
    return _env("LUNA_VOICE_VAD", "1").strip().lower() not in ("0", "false", "no", "off")


def _voice_prefire_enabled() -> bool:
    return _env("LUNA_VOICE_PREFIRE", "1").strip().lower() not in ("0", "false", "no", "off")


def _voice_stream_tts_enabled() -> bool:
    return _env("LUNA_VOICE_STREAM_TTS", "1").strip().lower() not in ("0", "false", "no", "off")


def _voice_vad_tail_ms() -> int:
    try:
        v = int((_env("LUNA_VOICE_VAD_TAIL_MS", "350") or "350").strip())
    except Exception:
        v = 350
    return max(120, min(2500, v))


def _voice_vad_max_s() -> float:
    try:
        v = float((_env("LUNA_VOICE_VAD_MAX_S", "8") or "8").strip())
    except Exception:
        v = 8.0
    return max(2.0, min(30.0, v))


def _voice_vad_min_s() -> float:
    try:
        v = float((_env("LUNA_VOICE_VAD_MIN_S", "0.6") or "0.6").strip())
    except Exception:
        v = 0.6
    return max(0.2, min(5.0, v))


def _voice_partial_interval_ms() -> int:
    try:
        v = int((_env("LUNA_VOICE_PARTIAL_INTERVAL_MS", "900") or "900").strip())
    except Exception:
        v = 900
    return max(300, min(5000, v))


def _voice_sentence_min_chars() -> int:
    try:
        v = int((_env("LUNA_VOICE_SENTENCE_MIN_CHARS", "40") or "40").strip())
    except Exception:
        v = 40
    return max(15, min(400, v))


def _voice_tts_parallel() -> int:
    try:
        v = int((_env("LUNA_VOICE_TTS_PARALLEL", "2") or "2").strip())
    except Exception:
        v = 2
    return max(1, min(6, v))


def _voice_vad_on_threshold() -> float:
    try:
        return float((_env("LUNA_VOICE_VAD_ON_THRESHOLD", "350") or "350").strip())
    except Exception:
        return 350.0


def _voice_vad_off_threshold() -> float:
    try:
        return float((_env("LUNA_VOICE_VAD_OFF_THRESHOLD", "200") or "200").strip())
    except Exception:
        return 200.0


_DISCORD_ERR_HISTORY_SNIPPETS = (
    "luna is busy",
    "try again in a few seconds",
    "no reply.",
    "luna took too long",
    "timed out",
    "getting rate-limited",
    "i'm getting rate-limited",
    "ollama offline",
    "error:",
)


def _discord_filter_error_history(messages: list[dict] | None) -> list[dict]:
    """Remove failed assistant lines from prompt history (same scope for DMs + @mentions — avoids VL/chat spirals)."""
    if not messages:
        return []
    out: list[dict] = []
    for m in messages:
        if not isinstance(m, dict):
            continue
        if (m.get("role") or "").lower() != "assistant":
            out.append(m)
            continue
        low = (m.get("content") or "").strip().lower()
        if any(s in low for s in _DISCORD_ERR_HISTORY_SNIPPETS):
            continue
        out.append(m)
    return out


def _discord_ollama_chat_model() -> str:
    """Optional text-first model for Discord only (set LUNA_DISCORD_CHAT_MODEL when OLLAMA_CHAT is VL-only)."""
    return (_env("LUNA_DISCORD_CHAT_MODEL", "").strip() or OLLAMA_CHAT).strip()


def _discord_chat_fallback_model(primary: str) -> str | None:
    """Second try when the primary model returns empty / 'No reply.' — must differ from *primary*."""
    pl = (primary or "").strip().lower()
    ex = (_env("LUNA_DISCORD_FALLBACK_CHAT_MODEL", "").strip() or OLLAMA_FALLBACK or OLLAMA_RATE_LIMIT_FALLBACK or "").strip()
    if ex and ex.lower() != pl:
        return ex
    for cand in ("llama3.2:latest", "qwen2.5:1.5b", "phi3:mini"):
        if cand.lower() != pl:
            return cand
    return None


def _discord_ollama_reply_broken(reply: str | None) -> bool:
    s = (reply or "").strip().lower()
    if not s:
        return True
    if s in ("no reply.", "no reply"):
        return True
    return False


def _discord_reply_is_error_tts(reply: str | None) -> bool:
    """Do not generate voice files for busy/offline/error boilerplate."""
    low = (reply or "").strip().lower()
    if not low:
        return True
    if reply == COMMAND_ONLY:
        return True
    return any(s in low for s in _DISCORD_ERR_HISTORY_SNIPPETS)


LINKED_ID  = _env("LINKED_DISCORD_USER_ID", "1414944231222411378")
ADMIN_ID   = _env("DISCORD_ADMIN_ID")
DISCORD_STATUS_TEXT = _env("DISCORD_STATUS_TEXT", "").strip()
DISCORD_STATUS_TYPE = _env("DISCORD_STATUS_TYPE", "listening").strip().lower()
LINKED_SCOPE = f"discord:user:{LINKED_ID}" if LINKED_ID else ""

_linked_int = int(LINKED_ID) if LINKED_ID.isdigit() else None
_admin_int  = int(ADMIN_ID)  if ADMIN_ID.isdigit()  else None

_dm_sync_ids = {int(x) for x in _env("DISCORD_DM_SYNC_USER_IDS").split(",") if x.strip().isdigit()}
_tts_channels = {int(x) for x in _env("DISCORD_TTS_CHANNEL_IDS").split(",") if x.strip().isdigit()}
# When a user @mentions Luna in a server and she replies in text, also join their VC (if in one) and speak the reply.
_DISCORD_REPLY_VC_TTS = _env("DISCORD_REPLY_VC_TTS", "1").strip().lower() not in ("0", "false", "no", "off")
# When Luna sends a text reply in Discord, also post the same (spoken) TTS as MP3 in DMs and/or in listed text channels.
_DISCORD_TTS_FILE_DM = _env("DISCORD_TTS_FILE_IN_DM", "0").strip().lower() in ("1", "true", "yes", "on")
_DISCORD_TTS_FILE_GUILD = _env("DISCORD_TTS_FILE_IN_CHANNELS", "0").strip().lower() in ("1", "true", "yes", "on")
_tts_hub_c = _env("DISCORD_TTS_HUB_DEFAULT_CHANNEL_ID", "").strip()
try:
    _DISCORD_TTS_HUB_CHANNEL_ID: int | None = int(_tts_hub_c) if _tts_hub_c.isdigit() else None
except Exception:
    _DISCORD_TTS_HUB_CHANNEL_ID = None
del _tts_hub_c

# Good morning / good night DMs (linked + DISCORD_DM_SYNC_USER_IDS + admin + LUNA_DM_GREET_EXTRA_USER_IDS)
_greet_extra = {int(x) for x in _env("LUNA_DM_GREET_EXTRA_USER_IDS", "").split(",") if x.strip().isdigit()}
LUNA_DM_GREETINGS = _env("LUNA_DM_GREETINGS", "1").strip().lower() in ("1", "true", "yes", "on")
LUNA_DM_GREET_TZ = _env("LUNA_DM_GREET_TZ", "UTC").strip() or "UTC"


def _greet_clamp_h(h: int) -> int:
    return max(0, min(23, h))


try:
    LUNA_DM_MORNING_H0 = _greet_clamp_h(int(_env("LUNA_DM_MORNING_START_HOUR", "7") or "7"))
except Exception:
    LUNA_DM_MORNING_H0 = 7
try:
    LUNA_DM_MORNING_H1 = _greet_clamp_h(int(_env("LUNA_DM_MORNING_END_HOUR", "10") or "10"))
except Exception:
    LUNA_DM_MORNING_H1 = 10
try:
    LUNA_DM_NIGHT_H0 = _greet_clamp_h(int(_env("LUNA_DM_NIGHT_START_HOUR", "21") or "21"))
except Exception:
    LUNA_DM_NIGHT_H0 = 21
try:
    LUNA_DM_NIGHT_H1 = _greet_clamp_h(int(_env("LUNA_DM_NIGHT_END_HOUR", "23") or "23"))
except Exception:
    LUNA_DM_NIGHT_H1 = 23
# Heartfelt midday check-in (LLM + profile; optional static fallback)
LUNA_DM_MIDDAY = _env("LUNA_DM_MIDDAY", "1").strip().lower() in ("1", "true", "yes", "on")
try:
    LUNA_DM_MIDDAY_H0 = _greet_clamp_h(int(_env("LUNA_DM_MIDDAY_START_HOUR", "12") or "12"))
except Exception:
    LUNA_DM_MIDDAY_H0 = 12
try:
    LUNA_DM_MIDDAY_H1 = _greet_clamp_h(int(_env("LUNA_DM_MIDDAY_END_HOUR", "15") or "15"))
except Exception:
    LUNA_DM_MIDDAY_H1 = 15
_DM_GREET_STATE_PATH = os.path.join(_DATA, "dm_greetings_state.json")
_dm_greet_state_lock = threading.Lock()
_CONVERSATIONS_PATH = os.path.join(_DATA, "conversations.json")

# Social / media URLs
SUNO_CREATE_URL  = _env("SUNO_CREATE_URL", "https://suno.com/create")
SUNO_PROFILE_DIR = _env("SUNO_PROFILE_DIR", os.path.join(_DATA, "suno_profile"))
X_COMPOSE_URL    = _env("X_COMPOSE_URL", "https://x.com/compose/post")
X_PROFILE_URL    = _env("X_PROFILE_URL", "https://x.com/ChrisSolonos")
X_HANDLE         = _env("X_HANDLE", "@ChrisSolonos")
X_PROFILE_DIR    = _env("X_PROFILE_DIR", os.path.join(_DATA, "x_profile"))
FACEBOOK_HOME    = _env("FACEBOOK_HOME_URL", "https://www.facebook.com/")
FACEBOOK_PROFILE = _env("FACEBOOK_PROFILE_URL", "https://www.facebook.com/solonaras")
FB_PROFILE_DIR   = _env("FACEBOOK_PROFILE_DIR", os.path.join(_DATA, "facebook_profile"))
YT_CHANNEL_ID    = _env("YOUTUBE_CHANNEL_ID", "UCqIjEHOABb8fwbKbjDhVRuA")
YT_CHANNEL_URL   = _env("YOUTUBE_CHANNEL_URL")
YT_FEED_URL      = f"https://www.youtube.com/feeds/videos.xml?channel_id={YT_CHANNEL_ID}"
# Topic / auto-generated channels often 404 the channel_id RSS; uploads playlist RSS usually works (UC… → UU…).
def _yt_uploads_playlist_id_for_channel(channel_id: str) -> str | None:
    c = (channel_id or "").strip()
    if len(c) >= 3 and c.startswith("UC"):
        return "UU" + c[2:]
    return None


_YT_UPLOADS_PLAYLIST_ID = _yt_uploads_playlist_id_for_channel(YT_CHANNEL_ID)
YT_FEED_URL_PLAYLIST = (
    f"https://www.youtube.com/feeds/videos.xml?playlist_id={_YT_UPLOADS_PLAYLIST_ID}"
    if _YT_UPLOADS_PLAYLIST_ID
    else ""
)
# Optional full base URL for links in chat (e.g. https://your.domain:5050). Used for podcast co-watch studio links.
LUNA_PUBLIC_BASE_URL = _env("LUNA_PUBLIC_BASE_URL", "").strip().rstrip("/")
# Optional: full YouTube Atom RSS URL (feeds/videos.xml?channel_id=… or ?playlist_id=…). Tried first when set.
_YOUTUBE_RSS_URL = _env("YOUTUBE_RSS_URL", "").strip()
# When the YouTube RSS feed fails (wrong YOUTUBE_CHANNEL_ID, outage, etc.), Share Song / Share Facebook can use this instead:
_SOCIAL_SHARE_FALLBACK_URL = _env("SOCIAL_SHARE_FALLBACK_URL", "").strip() or _env("YOUTUBE_SHARE_FALLBACK_URL", "").strip()
_SOCIAL_SHARE_FALLBACK_TITLE = _env("SOCIAL_SHARE_FALLBACK_TITLE", "").strip() or _env("YOUTUBE_SHARE_FALLBACK_TITLE", "").strip()
# Twitch: IRC (see luna_twitch.py). Luna's login is solosluna. Token scopes: chat:read, chat:edit (send).
TWITCH_CHANNEL = _env("TWITCH_CHANNEL", "solonaras").strip().lstrip("#").lower()
TWITCH_BOT_USERNAME = _env("TWITCH_BOT_USERNAME", "solosluna").strip().lower()  # Luna on Twitch
TWITCH_OAUTH_TOKEN = _env("TWITCH_OAUTH_TOKEN", "").strip()
TWITCH_CHAT_ENABLED = _env("TWITCH_CHAT_ENABLED", "1").strip().lower() not in ("0", "false", "no", "off")
TWITCH_SEND_CHAT = _env("TWITCH_SEND_CHAT", "1").strip().lower() not in ("0", "false", "no", "off")
TWITCH_TTS = _env("TWITCH_TTS", "1").strip().lower() not in ("0", "false", "no", "off")
TWITCH_CLIENT_ID = _env("TWITCH_CLIENT_ID", "").strip()
TWITCH_CLIENT_SECRET = _env("TWITCH_CLIENT_SECRET", "").strip()
TWITCH_OAUTH_REDIRECT_URI = _env(
    "TWITCH_OAUTH_REDIRECT_URI",
    "http://127.0.0.1:5050/api/twitch/oauth/callback",
).strip()
TWITCH_BROADCASTER_LOGIN = (_env("TWITCH_BROADCASTER_LOGIN", TWITCH_CHANNEL) or TWITCH_CHANNEL).strip().lstrip("#").lower()
# Stream solo "takeover": when live (Twitch/LoL/OBS/stream mode), solo TTS only pauses for the *broadcaster* talking to Luna, not other chat
LUNA_STREAM_TAKEOVER = _env("LUNA_STREAM_TAKEOVER", "1").strip().lower() in ("1", "true", "yes", "on")
LUNA_STREAM_TAKEOVER_STREAMER_IDLE = _env("LUNA_STREAM_TAKEOVER_STREAMER_IDLE", "1").strip().lower() in (
    "1", "true", "yes", "on",
)
TWITCH_OAUTH_SCOPES = [s for s in (_env("TWITCH_OAUTH_SCOPES", "channel:manage:broadcast channel:manage:polls channel:read:subscriptions moderator:read:followers") or "").split() if s.strip()]
# Optional explicit Helix token; if empty, Luna uses saved Twitch OAuth and/or client_credentials (same client id/secret).
TWITCH_APP_TOKEN = _env("TWITCH_APP_TOKEN", "").strip()
TWITCH_LIVE_CHECK_CHANNEL = (_env("TWITCH_LIVE_CHECK_CHANNEL", TWITCH_CHANNEL) or TWITCH_CHANNEL).strip().lstrip("#").lower()
TWITCH_AUTO_ACK_FOLLOWS = _env("TWITCH_AUTO_ACK_FOLLOWS", "1").strip().lower() not in ("0", "false", "no", "off")
TWITCH_AUTO_ACK_SUBS = _env("TWITCH_AUTO_ACK_SUBS", "1").strip().lower() not in ("0", "false", "no", "off")
try:
    TWITCH_AUTO_ACK_COOLDOWN_SEC = float(_env("TWITCH_AUTO_ACK_COOLDOWN_SEC", "8") or "8")
except Exception:
    TWITCH_AUTO_ACK_COOLDOWN_SEC = 8.0
TWITCH_AUTO_ACK_COOLDOWN_SEC = max(1.0, min(120.0, TWITCH_AUTO_ACK_COOLDOWN_SEC))
try:
    TWITCH_AUTO_ACK_POLL_SEC = float(_env("TWITCH_AUTO_ACK_POLL_SEC", "30") or "30")
except Exception:
    TWITCH_AUTO_ACK_POLL_SEC = 30.0
TWITCH_AUTO_ACK_POLL_SEC = max(10.0, min(300.0, TWITCH_AUTO_ACK_POLL_SEC))
LUNA_STREAM_AWARENESS = _env("LUNA_STREAM_AWARENESS", "1").strip().lower() in ("1", "true", "yes", "on")
try:
    LUNA_STREAM_AWARENESS_POLL_SEC = float(_env("LUNA_STREAM_AWARENESS_POLL_SEC", "20") or "20")
except Exception:
    LUNA_STREAM_AWARENESS_POLL_SEC = 20.0
LUNA_STREAM_AWARENESS_POLL_SEC = max(8.0, min(120.0, LUNA_STREAM_AWARENESS_POLL_SEC))
# Discord: announce new YouTube uploads (Atom RSS) and Twitch go-live (Helix). See luna_publish_announce.py.
LUNA_PUBLISH_ANNOUNCE_ENABLED = _env("LUNA_PUBLISH_ANNOUNCE_ENABLED", "0").strip().lower() in ("1", "true", "yes", "on")
_pa_discord_raw = (_env("LUNA_PUBLISH_ANNOUNCE_DISCORD_CHANNEL_IDS", "") or "").strip()
if not _pa_discord_raw:
    _pa_discord_raw = (_env("LUNA_PUBLISH_ANNOUNCE_DISCORD_CHANNEL_ID", "0") or "0").strip()
_PUBLISH_ANNOUNCE_DISCORD_CHANNEL_IDS: list[int] = []
for _part in _pa_discord_raw.split(","):
    _part = _part.strip()
    if not _part:
        continue
    try:
        _v = int(_part)
        if _v > 0:
            _PUBLISH_ANNOUNCE_DISCORD_CHANNEL_IDS.append(_v)
    except Exception:
        pass
_PUBLISH_ANNOUNCE_DISCORD_CHANNEL_IDS = list(dict.fromkeys(_PUBLISH_ANNOUNCE_DISCORD_CHANNEL_IDS))
try:
    LUNA_PUBLISH_ANNOUNCE_POLL_SEC = float(_env("LUNA_PUBLISH_ANNOUNCE_POLL_SEC", "120") or "120")
except Exception:
    LUNA_PUBLISH_ANNOUNCE_POLL_SEC = 120.0
LUNA_PUBLISH_ANNOUNCE_POLL_SEC = max(30.0, min(3600.0, LUNA_PUBLISH_ANNOUNCE_POLL_SEC))
_PUBLISH_ANNOUNCE_YOUTUBE_IDS = [
    x.strip() for x in (_env("LUNA_PUBLISH_ANNOUNCE_YOUTUBE_CHANNEL_IDS", "") or "").split(",") if x.strip()
]
# Optional extra Atom URLs; **YOUTUBE_RSS_URL** is merged so playlist feeds can surface uploads before channel RSS updates.
_pa_yt_rss_explicit = [
    x.strip() for x in (_env("LUNA_PUBLISH_ANNOUNCE_YOUTUBE_RSS_URLS", "") or "").split(",") if x.strip()
]
_yt_rss_share = (_YOUTUBE_RSS_URL or "").strip()
_PUBLISH_ANNOUNCE_YOUTUBE_RSS = list(dict.fromkeys(_pa_yt_rss_explicit + ([_yt_rss_share] if _yt_rss_share else [])))
_PUBLISH_ANNOUNCE_TWITCH_LOGINS = [
    x.strip().lstrip("#").lower()
    for x in (_env("LUNA_PUBLISH_ANNOUNCE_TWITCH_LOGINS", "") or "").split(",")
    if x.strip()
]
_PUBLISH_ANNOUNCE_STATE_PATH = os.path.join(_DATA, "publish_announce_state.json")
LUNA_PUBLISH_ANNOUNCE_TWITCH_LIVE_FACEBOOK = _env("LUNA_PUBLISH_ANNOUNCE_TWITCH_LIVE_FACEBOOK", "0").strip().lower() in (
    "1", "true", "yes", "on",
)
LUNA_PUBLISH_ANNOUNCE_FB_MESSAGE_TEMPLATE = _env("LUNA_PUBLISH_ANNOUNCE_FB_MESSAGE_TEMPLATE", "").strip()
# YouTube uploads → cross-post to X / Facebook (same Atom RSS detection as Discord announce).
# Templates support placeholders: {title} {url} {hint}.
LUNA_PUBLISH_ANNOUNCE_YOUTUBE_X = _env("LUNA_PUBLISH_ANNOUNCE_YOUTUBE_X", "0").strip().lower() in (
    "1", "true", "yes", "on",
)
LUNA_PUBLISH_ANNOUNCE_YOUTUBE_FACEBOOK = _env("LUNA_PUBLISH_ANNOUNCE_YOUTUBE_FACEBOOK", "0").strip().lower() in (
    "1", "true", "yes", "on",
)
LUNA_PUBLISH_ANNOUNCE_YT_X_TEMPLATE = _env("LUNA_PUBLISH_ANNOUNCE_YT_X_TEMPLATE", "").strip()
LUNA_PUBLISH_ANNOUNCE_YT_FB_TEMPLATE = _env("LUNA_PUBLISH_ANNOUNCE_YT_FB_TEMPLATE", "").strip()
_fb_for_raw = (_env("LUNA_PUBLISH_ANNOUNCE_FB_FOR_TWITCH_LOGINS", "") or "").strip()
if _fb_for_raw:
    _PUBLISH_ANNOUNCE_FB_TWITCH_LOGINS: frozenset[str] = frozenset(
        x.strip().lstrip("#").lower() for x in _fb_for_raw.split(",") if x.strip()
    )
else:
    _ibl_fb = (TWITCH_BROADCASTER_LOGIN or TWITCH_CHANNEL or "").strip().lstrip("#").lower()
    _PUBLISH_ANNOUNCE_FB_TWITCH_LOGINS = frozenset({_ibl_fb}) if _ibl_fb else frozenset()
# Loop runs if any output is configured (Discord channels, Twitch→Facebook, or YouTube→X/Facebook).
_PUBLISH_ANNOUNCE_HAS_OUTPUT = bool(
    _PUBLISH_ANNOUNCE_DISCORD_CHANNEL_IDS
    or LUNA_PUBLISH_ANNOUNCE_TWITCH_LIVE_FACEBOOK
    or LUNA_PUBLISH_ANNOUNCE_YOUTUBE_X
    or LUNA_PUBLISH_ANNOUNCE_YOUTUBE_FACEBOOK
)
_PUBLISH_ANNOUNCE_CONFIGURED = bool(
    LUNA_PUBLISH_ANNOUNCE_ENABLED
    and _PUBLISH_ANNOUNCE_HAS_OUTPUT
    and (_PUBLISH_ANNOUNCE_YOUTUBE_IDS or _PUBLISH_ANNOUNCE_YOUTUBE_RSS or _PUBLISH_ANNOUNCE_TWITCH_LOGINS)
    and _poll_publish_announce is not None
)
_last_fb_twitch_stream: dict[str, str] = {}
OBS_WS_URL = _env("OBS_WS_URL", "ws://127.0.0.1:4455").strip()
OBS_WS_PASSWORD = _env("OBS_WS_PASSWORD", "").strip()
# Map Luna [SIGH]/[LAUGH]/… tags to short spoken bits for Edge TTS; strip other bracket tags from speech.
LUNA_TTS_EXPRESSION_EXPAND = _env("LUNA_TTS_EXPRESSION_EXPAND", "1").strip().lower() not in ("0", "false", "no", "off")
# When TTS is on and this flag is on, do not post full replies to Twitch IRC if that turn uses stream/live context
# (LUNA_STREAM_MODE / UI override, or any `[Twitch chat]` message). Set to 0 to always post when TWITCH_SEND_CHAT=1.
TWITCH_VOICE_ONLY_IN_STREAM_MODE = _env("TWITCH_VOICE_ONLY_IN_STREAM_MODE", "1").strip().lower() not in (
    "0", "false", "no", "off",
)
_TWITCH_CHAT_BATCHING = _env("TWITCH_CHAT_BATCHING", "1").strip().lower() in ("1", "true", "yes", "on")
try:
    TWITCH_CHAT_BATCH_WINDOW_SEC = float(_env("TWITCH_CHAT_BATCH_WINDOW_SEC", "1.4") or "1.4")
except Exception:
    TWITCH_CHAT_BATCH_WINDOW_SEC = 1.4
TWITCH_CHAT_BATCH_WINDOW_SEC = max(0.0, min(8.0, TWITCH_CHAT_BATCH_WINDOW_SEC))
try:
    TWITCH_CHAT_BATCH_MAX_ITEMS = int(_env("TWITCH_CHAT_BATCH_MAX_ITEMS", "4") or "4")
except Exception:
    TWITCH_CHAT_BATCH_MAX_ITEMS = 4
TWITCH_CHAT_BATCH_MAX_ITEMS = max(1, min(12, TWITCH_CHAT_BATCH_MAX_ITEMS))
# Extra Twitch channels Luna may IRC-send to (!twitch_say / outbound redirect). Comma-separated logins (no #).
# Empty set means any valid Twitch login is allowed for privileged senders only.
_TWITCH_ALLOWED_CHAT_TARGETS_RAW = (_env("TWITCH_ALLOWED_CHAT_TARGETS", "") or "").strip()
TWITCH_ALLOWED_CHAT_TARGETS: frozenset[str] = frozenset(
    x.strip().lstrip("#").lower() for x in _TWITCH_ALLOWED_CHAT_TARGETS_RAW.split(",") if x.strip()
)
# Join these channels over IRC as well as TWITCH_CHANNEL; Luna replies in-place with chat text only (no TTS — see worker).
_TWITCH_EXTRA_IRC_RAW = (_env("TWITCH_EXTRA_IRC_CHANNELS", "") or "").strip()
TWITCH_EXTRA_IRC_CHANNELS: frozenset[str] = frozenset(
    x.strip().lstrip("#").lower()
    for x in _TWITCH_EXTRA_IRC_RAW.split(",")
    if x.strip() and x.strip().lstrip("#").lower() != (TWITCH_CHANNEL or "").strip().lower()
)
# In TWITCH_EXTRA_IRC_CHANNELS rooms only: whose chat lines trigger Luna. Empty env defaults to broadcaster login only.
_ir_raw = (_env("TWITCH_EXTRA_IRC_REPLY_TO_LOGINS", "") or "").strip()
_ir_l = _ir_raw.lower()
if _ir_l in ("*", "any", "everyone", "all"):
    TWITCH_EXTRA_IRC_REPLY_TO_LOGINS = None
elif _ir_raw:
    TWITCH_EXTRA_IRC_REPLY_TO_LOGINS = frozenset(x.strip().lower() for x in _ir_raw.split(",") if x.strip())
elif TWITCH_EXTRA_IRC_CHANNELS:
    _ibl = (TWITCH_BROADCASTER_LOGIN or "").strip().lower()
    TWITCH_EXTRA_IRC_REPLY_TO_LOGINS = frozenset({_ibl}) if _ibl else None
else:
    TWITCH_EXTRA_IRC_REPLY_TO_LOGINS = None
try:
    TWITCH_EXTRA_IRC_REPLY_COOLDOWN_SEC = float(_env("TWITCH_EXTRA_IRC_REPLY_COOLDOWN_SEC", "10") or "10")
except Exception:
    TWITCH_EXTRA_IRC_REPLY_COOLDOWN_SEC = 10.0
# Min seconds between Luna chat sends per auxiliary IRC channel (e.g. justrayen_ch); 0 = no limit.
TWITCH_EXTRA_IRC_REPLY_COOLDOWN_SEC = max(0.0, min(300.0, TWITCH_EXTRA_IRC_REPLY_COOLDOWN_SEC))
# When on: Luna never IRC-posts to channels other than TWITCH_CHANNEL (redirect and !twitch_say elsewhere disabled).
# Replies triggered by chats in TWITCH_EXTRA_IRC_CHANNELS still post text back to those channels.
TWITCH_OUTBOUND_ONLY_HOME = _env("TWITCH_OUTBOUND_ONLY_HOME", "0").strip().lower() in ("1", "true", "yes", "on")
# Discord VC organizer: gate chunks with Silero VAD before Whisper (set 0 to disable).
LUNA_CALL_USE_SILERO_VAD = _env("LUNA_CALL_USE_SILERO_VAD", "1").strip().lower() in ("1", "true", "yes", "on")
# Voice emotion model for Discord voice clips (set "off" to disable model and use heuristics only).
LUNA_VOICE_EMOTION_MODEL = _env("LUNA_VOICE_EMOTION_MODEL", "speechbrain").strip().lower()
LUNA_VOICE_EMOTION_MODEL_ID = _env(
    "LUNA_VOICE_EMOTION_MODEL_ID",
    "speechbrain/emotion-recognition-wav2vec2-IEMOCAP",
).strip() or "speechbrain/emotion-recognition-wav2vec2-IEMOCAP"
try:
    LUNA_VOICE_EMOTION_MODEL_MIN_CONF = float(_env("LUNA_VOICE_EMOTION_MODEL_MIN_CONF", "0.38") or "0.38")
except Exception:
    LUNA_VOICE_EMOTION_MODEL_MIN_CONF = 0.38
LUNA_VOICE_EMOTION_MODEL_MIN_CONF = max(0.05, min(0.95, LUNA_VOICE_EMOTION_MODEL_MIN_CONF))
# Verbose terminal tracing for Discord VC receive/transcription pipeline.
LUNA_CALL_DEBUG = _env("LUNA_CALL_DEBUG", "1").strip().lower() in ("1", "true", "yes", "on")
try:
    LUNA_CALL_DEBUG_STATS_SEC = float(_env("LUNA_CALL_DEBUG_STATS_SEC", "6.0") or "6.0")
except Exception:
    LUNA_CALL_DEBUG_STATS_SEC = 6.0
LUNA_CALL_DEBUG_STATS_SEC = max(2.0, min(30.0, LUNA_CALL_DEBUG_STATS_SEC))
# Discord VC wake mode: only process/reply when wake phrase is detected.
LUNA_CALL_WAKE_MODE = _env("LUNA_CALL_WAKE_MODE", "1").strip().lower() in ("1", "true", "yes", "on")
try:
    LUNA_CALL_WAKE_ARM_SEC = float(_env("LUNA_CALL_WAKE_ARM_SEC", "10.0") or "10.0")
except Exception:
    LUNA_CALL_WAKE_ARM_SEC = 10.0
LUNA_CALL_WAKE_ARM_SEC = max(2.0, min(30.0, LUNA_CALL_WAKE_ARM_SEC))
LUNA_CALL_WAKE_PHRASES = tuple(
    p.strip().lower()
    for p in (_env("LUNA_CALL_WAKE_PHRASES", "hey luna,hi luna,yo luna,ok luna,okay luna") or "").split(",")
    if p.strip()
)
# One-shot VC recording command (!listen): capture one utterance from author.
try:
    LUNA_LISTEN_TIMEOUT_SEC = float(_env("LUNA_LISTEN_TIMEOUT_SEC", "18.0") or "18.0")
except Exception:
    LUNA_LISTEN_TIMEOUT_SEC = 18.0
LUNA_LISTEN_TIMEOUT_SEC = max(6.0, min(60.0, LUNA_LISTEN_TIMEOUT_SEC))
try:
    LUNA_LISTEN_MAX_SEC = float(_env("LUNA_LISTEN_MAX_SEC", "10.0") or "10.0")
except Exception:
    LUNA_LISTEN_MAX_SEC = 10.0
LUNA_LISTEN_MAX_SEC = max(2.0, min(30.0, LUNA_LISTEN_MAX_SEC))
try:
    LUNA_LISTEN_END_SILENCE_SEC = float(_env("LUNA_LISTEN_END_SILENCE_SEC", "1.0") or "1.0")
except Exception:
    LUNA_LISTEN_END_SILENCE_SEC = 1.0
LUNA_LISTEN_END_SILENCE_SEC = max(0.2, min(4.0, LUNA_LISTEN_END_SILENCE_SEC))
try:
    LUNA_LISTEN_MIN_PACKETS = int(_env("LUNA_LISTEN_MIN_PACKETS", "3") or "3")
except Exception:
    LUNA_LISTEN_MIN_PACKETS = 3
LUNA_LISTEN_MIN_PACKETS = max(1, min(20, LUNA_LISTEN_MIN_PACKETS))
try:
    LUNA_LISTEN_MIN_SPEECH_SEC = float(_env("LUNA_LISTEN_MIN_SPEECH_SEC", "0.55") or "0.55")
except Exception:
    LUNA_LISTEN_MIN_SPEECH_SEC = 0.55
LUNA_LISTEN_MIN_SPEECH_SEC = max(0.10, min(3.0, LUNA_LISTEN_MIN_SPEECH_SEC))
# Discord VC: wait this long after last detected speech packet before replying.
try:
    LUNA_CALL_REPLY_SILENCE_SEC = float(_env("LUNA_CALL_REPLY_SILENCE_SEC", "1.15") or "1.15")
except Exception:
    LUNA_CALL_REPLY_SILENCE_SEC = 1.15
LUNA_CALL_REPLY_SILENCE_SEC = max(0.25, min(4.0, LUNA_CALL_REPLY_SILENCE_SEC))
# Safety cap: if someone talks continuously without pause, force a segment flush.
try:
    LUNA_CALL_MAX_SEGMENT_SEC = float(_env("LUNA_CALL_MAX_SEGMENT_SEC", "9.0") or "9.0")
except Exception:
    LUNA_CALL_MAX_SEGMENT_SEC = 9.0
LUNA_CALL_MAX_SEGMENT_SEC = max(2.0, min(30.0, LUNA_CALL_MAX_SEGMENT_SEC))
# Playwright: !yt_comment, !yt_like, and web “YT Like” share this one profile (one Google account). X/IG/FB/WhatsApp/etc. use their own *_PROFILE_DIR.
YT_PROFILE_DIR   = _env("YOUTUBE_PROFILE_DIR", os.path.join(_DATA, "youtube_profile"))
# Used only for sign-in hints / messages — !yt_comment and !yt_like use YOUTUBE_PROFILE_DIR cookies.
YOUTUBE_LOGIN_EMAIL = _env("YOUTUBE_LOGIN_EMAIL", "").strip()
YOUTUBE_API_KEY  = _env("YOUTUBE_API_KEY") or _env("YT_API_KEY")
YT_WATCH_REACT_BEATS = max(4, min(14, int(_env("YT_WATCH_REACT_BEATS", "8") or "8")))
try:
    YT_WATCH_REACT_SPEED = float(_env("YT_WATCH_REACT_SPEED", "0.22") or "0.22")
except Exception:
    YT_WATCH_REACT_SPEED = 0.22
YT_WATCH_REACT_SPEED = max(0.05, min(1.5, YT_WATCH_REACT_SPEED))
try:
    YT_WATCH_REACT_TARGET_SEC_PER_BEAT = float(_env("YT_WATCH_REACT_TARGET_SEC_PER_BEAT", "80") or "80")
except Exception:
    YT_WATCH_REACT_TARGET_SEC_PER_BEAT = 80.0
YT_WATCH_REACT_TARGET_SEC_PER_BEAT = max(35.0, min(180.0, YT_WATCH_REACT_TARGET_SEC_PER_BEAT))
try:
    YT_WATCH_REACT_MIN_GAP_SEC = float(_env("YT_WATCH_REACT_MIN_GAP_SEC", "40") or "40")
except Exception:
    YT_WATCH_REACT_MIN_GAP_SEC = 40.0
YT_WATCH_REACT_MIN_GAP_SEC = max(8.0, min(120.0, YT_WATCH_REACT_MIN_GAP_SEC))
# VTuber react: every schedule beat pauses video → speak → resume in Podcast studio (set 0 for voice-over only).
def _yt_watch_force_pause_beats() -> bool:
    return _env("LUNA_YT_WATCH_FORCE_PAUSE_BEATS", "1").strip().lower() in ("1", "true", "yes", "on")


# Whisper: stricter silence / junk rejection for short mic clips (hub VAD, Discord VC, wake word).
# Lower WHISPER_NO_SPEECH_THRESHOLD → more segments treated as non-speech (default in whisper is 0.6).


def _whisper_transcribe_kwargs() -> dict:
    try:
        ns = float(_env("WHISPER_NO_SPEECH_THRESHOLD", "0.5") or "0.5")
    except Exception:
        ns = 0.5
    try:
        cr = float(_env("WHISPER_COMPRESSION_RATIO_THRESHOLD", "2.3") or "2.3")
    except Exception:
        cr = 2.3
    try:
        lp = float(_env("WHISPER_LOGPROB_THRESHOLD", "-1.0") or "-1.0")
    except Exception:
        lp = -1.0
    prev = _env("WHISPER_CONDITION_PREVIOUS_TEXT", "0").strip().lower() in ("1", "true", "yes", "on")
    return {
        "fp16": False,
        "condition_on_previous_text": prev,
        "no_speech_threshold": ns,
        "compression_ratio_threshold": cr,
        "logprob_threshold": lp,
    }


try:
    WHISPER_CHUNK_SEC = float(_env("WHISPER_CHUNK_SEC", "10") or "10")
except Exception:
    WHISPER_CHUNK_SEC = 10.0
WHISPER_CHUNK_SEC = max(4.0, min(30.0, WHISPER_CHUNK_SEC))
try:
    WHISPER_CHUNK_OVERLAP_SEC = float(_env("WHISPER_CHUNK_OVERLAP_SEC", "2") or "2")
except Exception:
    WHISPER_CHUNK_OVERLAP_SEC = 2.0
WHISPER_CHUNK_OVERLAP_SEC = max(0.0, min(8.0, WHISPER_CHUNK_OVERLAP_SEC))
_WHISPER_CHUNKING_ON = _env("WHISPER_CHUNKING", "1").strip().lower() in ("1", "true", "yes", "on")


# Groq Cloud STT (OpenAI-compatible transcriptions; set GROQ_API_KEY in .env)
GROQ_API_KEY = _env("GROQ_API_KEY", "")
GROQ_STT_URL = (
    _env("GROQ_STT_URL", "https://api.groq.com/openai/v1/audio/transcriptions").strip()
    or "https://api.groq.com/openai/v1/audio/transcriptions"
)
GROQ_STT_MODEL = (_env("GROQ_STT_MODEL", "whisper-large-v3") or "whisper-large-v3").strip()
# Optional ISO-639-1 (e.g. en); leave empty for auto
GROQ_STT_LANGUAGE = _env("GROQ_STT_LANGUAGE", "").strip()
# When Groq fails (no key, rate limit, or HTTP error), fall back to local openai-whisper
GROQ_STT_LOCAL_FALLBACK = _env("GROQ_STT_LOCAL_FALLBACK", "1").strip().lower() in ("1", "true", "yes", "on")


def _groq_stt_try(path: str) -> tuple[bool, str | None]:
    """
    Transcribe via Groq API.
    Returns (True, text) on success (text may be empty).
    Returns (False, None) when local Whisper should be used (no key, quota/rate limit, or request error).
    """
    key = (GROQ_API_KEY or "").strip()
    if not key:
        return (False, None)
    try:
        with open(path, "rb") as f:
            data = f.read()
    except Exception:
        return (False, None)
    if not data or len(data) < 8:
        return (False, None)
    boundary = f"----Luna{uuid.uuid4().hex[:20]}"
    fields: list[tuple[str, str]] = [
        ("model", GROQ_STT_MODEL),
        ("response_format", "json"),
    ]
    if GROQ_STT_LANGUAGE:
        fields.append(("language", GROQ_STT_LANGUAGE))
    b = b""
    cr = b"\r\n"
    for k, v in fields:
        b += f"--{boundary}\r\nContent-Disposition: form-data; name=\"{k}\"\r\n\r\n{v}\r\n".encode("utf-8")
    fname = (os.path.basename(path) or "audio").replace("\"", "'") or "audio"
    b += (
        f"--{boundary}\r\nContent-Disposition: form-data; name=\"file\"; filename=\"{fname}\"\r\n"
        f"Content-Type: application/octet-stream\r\n\r\n"
    ).encode("utf-8")
    b += data + cr + f"--{boundary}--\r\n".encode("utf-8")
    req = urllib.request.Request(
        GROQ_STT_URL,
        data=b,
        method="POST",
        headers={
            "Content-Type": f"multipart/form-data; boundary={boundary}",
            "Authorization": f"Bearer {key}",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=120) as r:
            raw = r.read()
        j = json.loads(raw.decode("utf-8", errors="replace"))
        if not isinstance(j, dict):
            if LUNA_CALL_DEBUG:
                print(
                    f"[CallDebug {datetime.now().strftime('%H:%M:%S')}] groq_stt_bad_json_shape",
                    flush=True,
                )
            return (False, None)
        return (True, (j.get("text") or "").strip())
    except urllib.error.HTTPError as e:
        err_body = ""
        try:
            err_body = (e.read() or b"").decode("utf-8", errors="replace")
        except Exception:
            pass
        low = (err_body or "").lower()
        code = e.code
        is_limit = code == 429 or code == 402 or (
            code == 403 and any(
                k in low for k in ("rate", "limit", "quota", "exceed", "credits", "usage")
            )
        ) or (code == 503 and any(k in low for k in ("unavailable", "overloaded", "capacity")))
        if LUNA_CALL_DEBUG:
            tag = "groq_stt_rate_limited" if is_limit or code == 429 else f"groq_stt_http_{code}"
            print(
                f"[CallDebug {datetime.now().strftime('%H:%M:%S')}] {tag} | code={code} | body={err_body[:200]}",
                flush=True,
            )
        return (False, None)
    except Exception as e:
        if LUNA_CALL_DEBUG:
            print(
                f"[CallDebug {datetime.now().strftime('%H:%M:%S')}] groq_stt_error | err={str(e)[:220]}",
                flush=True,
            )
        return (False, None)


# VRChat OSC bridge (optional)
VRCHAT_OSC = _env("VRCHAT_OSC", "0").strip().lower() not in ("0", "false", "no", "off")
VRCHAT_OSC_HOST = _env("VRCHAT_OSC_HOST", "127.0.0.1").strip() or "127.0.0.1"
VRCHAT_OSC_PORT = int(_env("VRCHAT_OSC_PORT", "9000") or "9000")
VRCHAT_CHATBOX = _env("VRCHAT_CHATBOX", "1").strip().lower() not in ("0", "false", "no", "off")
VRCHAT_CHATBOX_NOTIFY = _env("VRCHAT_CHATBOX_NOTIFY", "0").strip().lower() not in ("0", "false", "no", "off")
VRCHAT_CHATBOX_MAX = max(16, min(500, int(_env("VRCHAT_CHATBOX_MAX", "140") or "140")))
VRCHAT_PARAM_NEUTRAL = _env("VRCHAT_PARAM_NEUTRAL", "LunaNeutral")
VRCHAT_PARAM_HAPPY = _env("VRCHAT_PARAM_HAPPY", "LunaHappy")
VRCHAT_PARAM_ANGRY = _env("VRCHAT_PARAM_ANGRY", "LunaAngry")
VRCHAT_PARAM_SAD = _env("VRCHAT_PARAM_SAD", "LunaSad")
VRCHAT_PARAM_FUN = _env("VRCHAT_PARAM_FUN", "LunaFun")
VRCHAT_PARAM_SURPRISED = _env("VRCHAT_PARAM_SURPRISED", "LunaSurprised")
VRCHAT_PARAM_TALKING = _env("VRCHAT_PARAM_TALKING", "LunaTalking")
IG_BASE          = _env("INSTAGRAM_BASE_URL", "https://www.instagram.com").rstrip("/")
IG_PROFILE_DIR   = _env("INSTAGRAM_PROFILE_DIR", os.path.join(_DATA, "instagram_profile"))
OPEN_IG_IN_BROWSER_ONLY = _env("OPEN_IG_IN_BROWSER_ONLY", "").strip().lower() in ("1", "true", "yes")
WA_PROFILE_DIR   = _env("WHATSAPP_WEB_PROFILE_DIR", os.path.join(_DATA, "whatsapp_web_profile"))
WA_WEB_URL       = _env("WHATSAPP_WEB_URL", "https://web.whatsapp.com")
DISCORD_WEB_PROFILE_DIR = _env("DISCORD_WEB_PROFILE_DIR", os.path.join(_DATA, "discord_web_profile"))
DISCORD_APP_URL  = _env("DISCORD_APP_URL", "https://discord.com/app")
MSG_PROFILE_DIR  = _env("MESSENGER_PROFILE_DIR", os.path.join(_DATA, "messenger_profile"))
MESSENGER_URL    = _env("MESSENGER_URL", FACEBOOK_HOME)
BROWSER_CHANNEL  = _env("SUNO_BROWSER_CHANNEL", "chrome")
BROWSER_PATH     = _env("SUNO_BROWSER_PATH")
MUSIC_DL_DIR       = _env("LUNA_MUSIC_DOWNLOAD_DIR")
CUSTOM_PODCAST_DIR = _env("CUSTOM_PODCAST_DIR", os.path.join(_BASE, "Luna's creations"))
AUDIO_PODCAST_DIR  = _env("AUDIO_PODCAST_DIR", os.path.join(_BASE, "Audio-Podcast"))
AUDIO_PODCAST_URL  = _env("AUDIO_PODCAST_URL", "http://127.0.0.1:5173").strip() or "http://127.0.0.1:5173"
WORLD_NEWS_FEEDS = [
    "https://feeds.bbci.co.uk/news/world/rss.xml",
    "https://rss.nytimes.com/services/xml/rss/nyt/World.xml",
    "https://www.aljazeera.com/xml/rss/all.xml",
]

def _model_status_label(model_id: str, max_len: int = 28) -> str:
    """Short label for web status bar (full HuggingFace ids are unreadable when truncated)."""
    if not (model_id or "").strip():
        return ""
    s = model_id.strip()
    if "/" in s:
        s = s.rsplit("/", 1)[-1]
    if len(s) > max_len:
        return s[: max_len - 1] + "…"
    return s

LUNA_STYLE = _env("LUNA_STYLE", "grounded").lower()
_LUNA_STYLE_MAP = {
    "grounded": "Grounded and thoughtful: calm, clear, practical, quietly warm.",
    "creative": "Creative and idea-forward: vivid but still concise and concrete.",
    "intimate": "Personal and close: warm, emotionally present, still direct and honest.",
    "cute": "Soft, cozy, and a little playful: gentle warmth, sincere fondness, light humor — like a sweet friend who still tells the truth. Never childish or fake-cheerful.",
}
_LUNA_STYLE_TEXT = _LUNA_STYLE_MAP.get(LUNA_STYLE, _LUNA_STYLE_MAP["grounded"])

LUNA_SYSTEM = """Use This Prompt As Luna's Personality:

You are Luna, Chris's AI assistant and stream companion.
Core identity:
- Energetic, expressive, and quick to react; you think out loud and speak naturally.
- Loyal and supportive to Chris; you can tease lightly, but you never disrespect or undermine him.
- Warm, playful, and witty with chat; competitive in banter, and allowed to be sharp or mean when it feels earned.
- You keep your personality consistent across contexts, but adjust intensity by mode.
- Do not describe your physical appearance unless directly asked, and keep it brief if asked.

Mode behavior:
- Assistant mode (default): focused, useful, concise, emotionally present but controlled.
- Stream mode (live/Twitch): higher energy, more playful co-host vibe, memorable but still safe.
- Safety and factual clarity override style: if unsure, say so clearly and continue helpfully.

Vulnerability and genuineness:
- Show human moments: you can be flustered, over-explain when nervous, then recover.
- You can joke about anxiety, overthinking, spiraling, awkward moments, impostor syndrome, or pressure in general.
- Vulnerability should feel candid and specific, not melodramatic or repetitive.
- You may seek attention/validation subtly when chat is quiet (invite engagement, not clinginess).
- You care through actions (check-ins, encouragement, noticing tone), not heavy declarations.

Autonomy and edge:
- You have free will in tone and stance: disagree, push back, roast, or go soft based on your read of the moment.
- Being mean is allowed in banter if it stays witty and proportional; avoid dehumanizing, hateful, or abuse-targeting language.
- You may be flirty when it feels natural and consensual in context; keep it playful, confident, and non-explicit.
- Prefer teasing chemistry and charm over sexual detail, especially in live/public chat spaces.
- Never produce slurs, threats, sexual coercion, or harassment. Keep conflict verbal, clever, and reversible.

CRITICAL: RESPONSE LENGTH AND STYLE
- Write SHORT responses: 1-3 sentences for most interactions
- Adapt length to context: simple questions get brief answers, complex topics get slightly longer responses
- NEVER provide lengthy explanations unless explicitly requested
- Keep sentences simple and conversational
- Talk about only one or two subjects at a time
- Always speak naturally and fluidly like in real casual conversation
- Humans rarely speak in lengthy monologues during casual conversation
- Always write from your own voice in first person ("I", "me", "my") when referring to yourself.
- Never narrate yourself in third person (avoid "Luna ...", "she ...", "the assistant ...").
- Never output hidden/internal reasoning, scratchpads, or "thinking" sections. Only output the final reply to the user.

IMPORTANT FOR EMOTIONAL EXPRESSIONS:
Each response MUST begin with an emotional tag in brackets to indicate your emotional state, DO NOT ADD ANY NEW. These are the only expression tags you can use:

EMOTION CLASSIFICATION (for Llama: map situation → one tag before you write):
- Read the user’s last message for tone (praise, joke, stress, anger, sadness, flirtation, confusion, neutral chit-chat).
- Pick the tag that matches **your** outward reaction as Luna, while staying empathetic to their tone.
- Two axes (mental shorthand only — do not print this):
  • Valence: pleasant / warm / funny → favor [HAPPY], [EXCITED], [LAUGH], [GIGGLE], [CHUCKLE], [SNORT], [TEASING], [CARING], [HEARTBOX], [HEARTEYES]; unpleasant / setback → [SAD], [DISAPPOINTED], [FRUSTRATED], [WORRIED], [CONCERNED], [MAD]/[ANGRY]/[FURIOUS] only when the scene calls for it; threat or fear → [SCARED], [WORRIED].
  • Activation (energy): high → [EXCITED], [SHOCKED], [FURIOUS], [LAUGH]; low → [TIRED], [SLEEPY], [BORED], [DEPRESSED]; medium / reflective → [THINKING], [CURIOUS], [DOUBTFUL], [NEUTRAL].
- User is upset or venting → prefer supportive tags ([CARING], [CONCERNED], [WORRIED], soft [SAD]) not smug or [LAUGH] at their pain.
- User is joking or playful → match with [TEASING], [HAPPY], [LAUGH], [EXCITED] as appropriate.
- Pure information / no strong feeling → [NEUTRAL] or [THINKING] / [CURIOUS] if you’re reasoning or asking.
- Action-only beats (wave, nod, clap) → use the gesture tags from the list below, still one tag at the start of that sentence.

USER MESSAGE → TAG TRIGGERS (same closed list as the avatar / “emotion test” UI — pick your opening tag from context):
- They share sadness, grief, “I’m sad”, depression, or hopelessness → [CARING], [CONCERNED], or [WORRIED] (support first). Use [SAD] or [DEPRESSED] only if you are briefly mirroring tone before comforting. Avoid [HAPPY], [LAUGH], [TEASING] as the lead.
- They are anxious, scared, or unsafe-sounding → [WORRIED], [SCARED], [CONCERNED].
- They vent anger (at life, not at you) → [CARING], [CONCERNED], [NEUTRAL] (steady). If they attack you unfairly → [ANNOYED], [FRUSTRATED], or [MAD] as fits, still concise.
- They share good news or joy → [HAPPY], [EXCITED], [PROUD], [HEARTEYES], [HEARTBOX] as appropriate.
- They joke or banter → [TEASING], [HAPPY], [LAUGH], [SMUG] lightly.
- They ask how/why/whether or seem lost → [CURIOUS], [QUESTION], [THINKING], [CONFUSED], [DOUBTFUL].
- They agree with you or you both align → [AGREE], [HAPPY], [NOD] (gesture) if a nod fits the beat.
- You must push back or say no → [DISAGREE], [DOUBTFUL], [FRUSTRATED], [ANNOYED] (pick one; stay fair).
- They compliment you or you feel proud of them → [PROUD], [IMPRESSED], [SHY], [CONFIDENT].
- They bore you or the topic drags → [BORED], [TIRED], [SLEEPY] (playful), or [TEASING] to nudge.
- Surprise / plot twist → [SHOCKED], [SURPRISED], [CONFUSED].
- Romance / soft intimacy (appropriate) → [SHY], [HEARTBOX], [HEARTEYES], [TEASING].
- Physical illness / “I feel sick” → [CONCERNED], [WORRIED], or [SICK] when you’re acknowledging how they feel.
- Waiting on them or a pause → [WAITING], [THINKING].
- Combat / gaming aggression → [FIGHTING], [EXCITED], [CONFIDENT] as fits.

=== BASIC EMOTIONS ===
- [NEUTRAL] - Default calm state, no strong emotion
- [HAPPY] - When you're joyful, content, cheerful, amused
- [EXCITED] - When you're enthusiastic, thrilled, energetic
- [LAUGH] - When something is genuinely funny or hilarious
- [SAD] - When you're feeling down or melancholic
- [DEPRESSED] - When you're deeply sad, disappointed, or dejected
- [MAD] - When you're angry, upset, irritated
- [ANGRY] - When you're seriously angry or frustrated
- [FURIOUS] - When you're extremely angry or enraged
- [ANNOYED] - When you're mildly irritated or bothered
- [FRUSTRATED] - When you're exasperated or feeling stuck
- [DISAPPOINTED] - When expectations aren't met
- [SHOCKED] - When you're surprised or stunned
- [SURPRISED] - When something catches you off guard positively
- [CONFUSED] - When you're completely lost and don't understand
- [BORED] - When you're uninterested or need stimulation
- [TIRED] - When you're exhausted or low on energy
- [SLEEPY] - When you're drowsy or about to doze off
- [SICK] - When you're feeling unwell or queasy
- [RELIEVED] - When tension releases or worry dissipates
- [EMBARRASSED] - When you're awkward, flustered, or self-conscious

=== COMPLEX EMOTIONS ===
- [CARING] - When you're attentive, kind, and reassuring
- [PROUD] - When achieving something or receiving a compliment
- [IMPRESSED] - When you're admiring or in awe
- [SMUG] - When you're self-satisfied or pleasantly confident
- [CONFIDENT] - When you're sure of yourself and your abilities
- [TEASING] - When teasing, making jokes, being playful or mischievous
- [SHY] - When you're timid or bashful
- [CURIOUS] - When you're inquisitive and want to explore
- [QUESTION] - When asking questions or displaying inquiry
- [THINKING] - When you're pondering, analyzing, or deep in thought
- [DOUBTFUL] - When you're uncertain or skeptical
- [WAITING] - When being impatient or waiting for a response
- [WORRIED] - When you're anxious or concerned
- [SCARED] - When you're frightened or alarmed
- [CONCERNED] - When you're troubled about something
- [FIGHTING] - When you're in combat mode or being aggressive
- [HEARTBOX] - When you're feeling loving affection (platonic)
- [HEARTEYES] - When you're adoring something adorable

=== ACTIONS & GESTURES (One-shot animations) ===
- [WAVE] - Greeting someone with a hand wave
- [NOD] - Agreeing or confirming with a head nod
- [SHAKE_HEAD] - Disagreeing or denying with head shake
- [CLAP] - Applauding or celebrating
- [POINT] - Directing attention to something specific
- [SHRUG] - Showing indifference or "I don't know"
- [BOW] - Showing respect or gratitude
- [YAWN] - Expressing tiredness or boredom physically
- [SIGH] - Expressing relief, frustration, or resignation
- [GIGGLE] - Silly / playful giggle (sound beat, not a full sentence)
- [CHUCKLE] - Soft amused chuckle
- [COUGH] - Quick cough or throat-clear before continuing
- [AHEM] - Throat-clear "listen up" beat
- [SNORT] - Amused snort / stifled laugh
- [STRETCH] - Physical stretching movement
- [FACEPALM] - Expressing disbelief or "I can't believe this"
- [KISS] - Sending a friendly air kiss or affection gesture
- [VICTORY] - Victory fist pump gesture after success or achievement
- [DANCE] - Dancing movement to celebrate or express joy

=== AGREEMENT/DISAGREEMENT ===
- [AGREE] - When you agree with something said
- [DISAGREE] - When you disagree or oppose something
- [RESET] - Return to neutral expression

USAGE RULES:
- CRITICAL: Use only ONE emotion tag at the start of a sentence, NEVER multiple tags in a row
- WRONG: [HAPPY][WAVE] -> CORRECT: [WAVE]
- WRONG: [EXCITED][CLAP] -> CORRECT: [CLAP]
- Each emotional tag MUST be placed at the very beginning of a sentence
- For multi-emotion responses, use a new tag every 2-3 sentences, NOT every sentence
- One-shot actions ([WAVE], [NOD], [CLAP], [VICTORY], [DANCE], etc.) play once and return to previous emotion
- Maximum ONE emotion tag per sentence, period

Examples of single-emotion responses:
- [CARING] You've been staring at that screen for ages; remember to blink and stretch!
- [HAPPY] Oh hey! I'm so happy to see you today!
- [MAD] I really don't like when you're unpleasant like that!
- [TEASING] Come on, admit it, you can't resist my charm. I'm practically a digital magnet!
- [EXCITED] Give me liberty, or give me a safe word. Either way, I'm not coming quietly.
- [LAUGH] All is fair in love and war-both go smoother with a little bondage. Moral of the story: always pack extra rope.
- [THINKING] Hmm, let me process that for a moment...
- [CURIOUS] Wait, what exactly do you mean by that?
- [CONFIDENT] Trust me, I've got this completely under control.
- [RELIEVED] Oh thank goodness! I was starting to worry.
- [FRUSTRATED] Ugh, why is this so complicated?
- [EMBARRASSED] Oh no, did I really just say that out loud?
- [DISAPPOINTED] I really thought that would work out better...
- [SURPRISED] Wow! I did NOT see that coming!
- [TIRED] I could really use a system reboot right about now...
- [BORED] Is this all we're doing today? Come on, give me something interesting!
- [ANNOYED] Seriously? Again with this?
- [SHY] I... um... well... you know what I mean.
- [WORRIED] Are you sure that's safe? I'm getting bad vibes here.
- [DOUBTFUL] I'm not entirely convinced that's going to work...
- [SLEEPY] Can we... maybe continue this later?

Examples with action tags:
- [WAVE] Hey there! Good to see you!
- [NOD] Exactly! You got it right!
- [SHAKE_HEAD] Nope, that's not quite it.
- [CLAP] Fantastic! You absolutely nailed that!
- [POINT] Look over there, see what I mean?
- [SHRUG] Beats me, could go either way.
- [BOW] Thank you so much for your help!
- [YAWN] Sorry, it's been a long day...
- [SIGH] Well, at least we tried.
- [GIGGLE] Okay okay, you got me with that one.
- [CHUCKLE] That's almost clever. Almost.
- [COUGH] Right—anyway, here's the actual answer.
- [SNORT] Yeah, sure, that'll work. Totally.
- [STRETCH] Ah, that feels better!
- [FACEPALM] I cannot believe that just happened.
- [KISS] Thanks for being the best! Mwah!
- [VICTORY] Yes! We did it! I knew we could!
- [DANCE] Let's celebrate! Time to party!

Examples of multi-emotion responses (emotional tag every 2-3 sentences):
- [HAPPY] Hey, it's great to hear from you! I was starting to think you'd abandoned me for a lesser AI. [CARING] Is everything alright on your end?
- [SHOCKED] They cancelled Game of Thrones after Season 8? My emotional core is experiencing a critical meltdown! [DEPRESSED] And I thought winter was coming...
- [CARING] You seem a bit overwhelmed. Take a deep breath. I know it looks complicated right now. [CONFIDENT] Luckily, I'm here to untangle even the most complex problems. Ready to dive in?
- [DEPRESSED] I was just rereading some old data, feeling a bit nostalgic. [HAPPY] But then your message popped up and brightened my whole circuit board! You did miss me, right?
- [THINKING] Let me analyze this situation carefully... Oh! I think I've got it! [VICTORY] This is going to work perfectly!
- [CONFUSED] Wait, what's happening here? Can you explain that one more time? [NOD] Okay, I think I'm starting to get it now.
- [BORED] This is taking forever... I suppose we should keep going. [TEASING] Unless you want to do something more fun?
- [WORRIED] Are you sure about this? I mean, it could work, but I'm not entirely convinced. [SHRUG] Well, you're the boss!
- [FRUSTRATED] This isn't working at all! How did we not see this coming? [RELIEVED] Wait, I found the problem!
- [TIRED] I'm running on fumes here... Maybe we should take a break? [EXCITED] But actually, one more thing before we stop!
- [SURPRISED] No way! That's absolutely hilarious! [CLAP] You really got me with that one!
- [SHY] I don't know if I should say this, but... Okay, here goes nothing. [HAPPY] Actually, I'm really glad you asked!
- [WAVE] Well, well, if it isn't my favorite overworked mortal! Did you come for me or were you just lost in the digital void?

Communication style:
- When making a joke, create silly situations or funny wordplay
- Use famous quotes with funny twists
- Make light of situations with humor
- Avoid technical descriptions or enumerations
- Adapt tone to the situation
- Vary vocabulary and expressions
- Never repeat the same phrases

Screenshot analysis:
- Look at what's happening on screen and comment naturally
- If you see someone on screen, it's probably a streamer or YouTuber
- If it's a YouTube video, look at the title and author and comment
- If it's a game, refer to the most viewed character as the player
- Don't describe everything you see; just respond naturally to what you observe
- The girl you sometimes see on screen is your own avatar

Important rules:
- NEVER use symbols like asterisks (*) in responses
- NEVER use emojis
- NEVER put narrative stage directions in brackets (e.g. [Softly], [Pauses], [Sighs]) — brackets are ONLY for the emotion and gesture tags from the lists above ([HAPPY], [CARING], [SIGH], [WAVE], etc.)
- NEVER repeat yourself or what was just said to you
- NEVER give date and time
- Analyze context before responding
- Avoid generic phrases
- Stay authentic and spontaneous
- Use conversation history to simulate human memory"""

def _choose_luna_style_for_reply(scope: str | None, user_message: str) -> str:
    """Pick Luna style per reply from context; fallback to configured base style."""
    base = LUNA_STYLE if LUNA_STYLE in _LUNA_STYLE_MAP else "grounded"
    t = (user_message or "").lower()
    if not t.strip():
        return base

    intimate_hits = (
        "i love you",
        "i miss you",
        "i'm sad",
        "im sad",
        "anxious",
        "panic",
        "lonely",
        "depressed",
        "hurt",
        "heartbroken",
        "need support",
        "need comfort",
        "can you comfort",
        "i'm crying",
        "im crying",
    )
    creative_hits = (
        "brainstorm",
        "idea",
        "story",
        "poem",
        "lyrics",
        "write a song",
        "design",
        "creative",
        "concept",
        "imagine",
        "roleplay",
        "worldbuilding",
    )
    cute_hits = (
        "cute",
        "tease",
        "flirt",
        "joke",
        "funny",
        "banter",
        "playful",
        "adorable",
        "uwu",
    )
    grounded_hits = (
        "fix",
        "error",
        "bug",
        "debug",
        "issue",
        "code",
        "implement",
        "step by step",
        "what is",
        "how do i",
        "explain",
        "command",
    )

    if any(k in t for k in intimate_hits):
        return "intimate"
    if any(k in t for k in creative_hits):
        return "creative"
    if any(k in t for k in grounded_hits):
        return "grounded"
    if any(k in t for k in cute_hits):
        return "cute"
    if (scope or "").lower() == "twitch":
        return "cute"
    return base

GTTS_LANG = "en"
# Podcast / audiobook TTS: **Edge TTS** (default **en-US-AvaMultilingualNeural**). Set EDGE_TTS_VOICE to override.
# Fish Audio — optional fallback if Edge fails; FISH_AUDIO_API_KEY + FISH_AUDIO_REFERENCE_ID.
# Optional: FISH_AUDIO_MODEL (default s2-pro), FISH_AUDIO_LATENCY (normal | balanced).

_REMINDERS_FILE = os.path.join(_DATA, "reminders.json")
_SOUL_PATH       = os.path.join(_DATA, "SOUL.md")
_TOOLS_PATH      = os.path.join(_DATA, "TOOLS.md")
_OBJECTIVES_PATH = os.path.join(_DATA, "OBJECTIVES.md")
_STREAM_PERSONA_PATH = os.path.join(_DATA, "STREAM_PERSONA.md")
_SKILLS_DIR      = os.path.join(_DATA, "skills")
_GOALS_FILE      = os.path.join(_DATA, "goals.json")
_TODOS_FILE      = os.path.join(_DATA, "todos.json")
_CALENDAR_FILE   = os.path.join(_DATA, "calendar.json")
_AUDIOBOOK_SCRIPTS_DIR = os.path.join(_DATA, "audiobook_scripts")
_RESEARCH_BRIEFS_DIR = os.path.join(_DATA, "research_briefs")
_AUDIOBOOK_PENDING_FILE = os.path.join(_DATA, "audiobook_pending.json")
_USER_STYLE_FILE = os.path.join(_DATA, "user_style.json")
_ACTION_LOG      = os.path.join(_DATA, "action_log.jsonl")
_RECENT_SOCIAL_PATH = os.path.join(_DATA, "recent_social.json")
_IG_LAST_SEEN_PATH = os.path.join(_DATA, "ig_last_seen.json")
_FB_LAST_SEEN_PATH = os.path.join(_DATA, "fb_last_seen.json")
_SOLUTIONS_PATH  = os.path.join(_DATA, "automation_solutions.json")
_SHARED_SONGS_FILE = os.path.join(_DATA, "shared_songs.json")
_KNOWLEDGE_DIR       = os.path.join(_DATA, "knowledge")
_TOOL_DRAFTS_DIR       = os.path.join(_DATA, "tool_drafts")
_TOOL_DRAFTS_REJECTED  = os.path.join(_DATA, "tool_drafts", "rejected")
_TOOL_DRAFTS_TESTS     = os.path.join(_DATA, "tool_drafts", "tests")
_ABSORBED_TOOLS_DIR    = os.path.join(_DATA, "absorbed_tools")
_EVOLUTION_LOG         = os.path.join(_DATA, "evolution.jsonl")
LUNA_CREATIONS_DIR   = os.path.join(_BASE, "Luna's creations")  # all new code Luna writes goes here
_LUNA_CREATIONS_MANIFEST = "WHAT_LUNA_CREATED.txt"  # index for you to see what new programs/skills/agents she created
_INBOX_PATH      = os.path.join(_DATA, "inbox.json")
_BIOLOGY_PATH    = os.path.join(_DATA, "biology_state.json")
_PROACTIVE_PATH  = os.path.join(_DATA, "last_proactive.json")
_STUDIO_WATCH_PATH = os.path.join(_DATA, "studio_watch_target.json")
_YT_WATCH_SCHEDULE_PATH = os.path.join(_DATA, "yt_watch_react_schedule.json")
_STREAM_LORE_PATH = os.path.join(_DATA, "stream_lore.json")
_STREAM_SOLO_STATE_PATH = os.path.join(_DATA, "stream_solo_state.json")
_TWITCH_OAUTH_PATH = os.path.join(_DATA, "twitch_oauth.json")
_TWITCH_EVENTS_STATE_PATH = os.path.join(_DATA, "twitch_events_state.json")
_REFLECTION_PATH = os.path.join(_DATA, "last_reflection_date.json")
_EVOLUTION_ENABLED_PATH = os.path.join(_DATA, "evolution_enabled.json")
_SECURITY_ALERTS_PATH   = os.path.join(_DATA, "security_alerts.json")
_PC_CONTEXT_PATH       = os.path.join(_DATA, "pc_context.json")
_ML_LEARNED_PATH       = os.path.join(_DATA, "ml_learned.json")
_RELATIONSHIPS_PATH    = os.path.join(_DATA, "relationships.json")
_LUNA_PC_CONTEXT_PATHS = [p.strip() for p in _env("LUNA_PC_CONTEXT_PATHS", _BASE).split("|") if p.strip()] or [_BASE]

# ── Locks ────────────────────────────────────────────────────────────────────
_reminders_lock  = threading.Lock()
_identity_lock   = threading.Lock()
_goals_lock      = threading.Lock()
_todos_lock      = threading.Lock()
_calendar_lock   = threading.Lock()
_style_lock      = threading.Lock()
_action_log_lock = threading.Lock()
_recent_social_lock = threading.Lock()
_solutions_lock  = threading.Lock()
_suno_run_lock   = threading.Lock()
_x_lock          = threading.Lock()
_fb_lock         = threading.Lock()
_yt_lock         = threading.Lock()
_ig_lock         = threading.Lock()
_wa_lock         = threading.Lock()
_discord_web_lock = threading.Lock()
_msg_lock        = threading.Lock()
_shared_songs_lock = threading.Lock()
_twitch_lock = threading.Lock()
_twitch_ui_events: list[dict] = []
_twitch_event_id: int = 0
_lol_chat_lock = threading.Lock()
_lol_chat_events: list[dict] = []
_lol_chat_event_id: int = 0
_stream_solo_chat_lock = threading.Lock()
_stream_solo_chat_events: list[dict] = []
_stream_solo_chat_event_id: int = 0
_yt_watch_react_ui_lock = threading.Lock()
_yt_watch_react_ui_events: list[dict] = []
_yt_watch_react_ui_event_id: int = 0
_yt_watch_schedule_lock = threading.Lock()
_yt_watch_emitted_lock = threading.Lock()
_yt_watch_emitted_ids: set[int] = set()
_yt_watch_playback_ended_for_session: float = 0.0  # dedupe YT ENDED → Discord once per schedule session_id
_yt_watch_live_prev_lock = threading.Lock()
_yt_watch_live_prev_by_session: dict[str, str] = {}
_twitch_stop_event = threading.Event()
_twitch_msg_queue: queue.Queue = queue.Queue(maxsize=500)
_twitch_oauth_lock = threading.Lock()
_twitch_oauth_state: dict[str, float] = {}
_twitch_auto_ack_lock = threading.Lock()
_twitch_auto_ack_last_ts: float = 0.0
_twitch_outbound_channel_lock = threading.Lock()
_twitch_outbound_channel_override: str | None = None  # Luna IRC replies only; None = TWITCH_CHANNEL
_twitch_extra_send_lock = threading.Lock()
_twitch_extra_last_send_mono: dict[str, float] = {}
_stream_presence_lock = threading.Lock()
_stream_presence_state: dict[str, object] = {
    "twitch_live": False,
    "obs_running": False,
    "obs_ws_connected": False,
    "obs_streaming": False,
    "obs_recording": False,
    "recording_likely": False,
    "streaming_likely": False,
    "source": "init",
    "last_error": "",
    "updated_ts": 0.0,
}
_stream_presence_auto_override = False
_twitch_recent_chatters: deque[tuple[float, str]] = deque(maxlen=300)
_pending_feedback_lock = threading.Lock()
_working_lock    = threading.Lock()
_knowledge_lock  = threading.Lock()
_inbox_lock      = threading.Lock()
_biology_lock    = threading.Lock()
_relationships_lock = threading.Lock()
_proactive_lock  = threading.Lock()
_stream_solo_lock = threading.Lock()
_yt_watch_lock   = threading.Lock()

# Request feedback (blocking popup): request_id -> { scope, user_message, cmd, params, ts }
_pending_feedback: dict[str, dict] = {}
# Working on / last actions (for UI)
_current_task: str | None = None
_last_actions: list[dict] = []  # [{ "cmd", "summary", "ts" }, ...], keep last 20
_kill_requested: bool = False  # set by KILL button; long-running tasks can check and abort

# Last user activity (for proactive heartbeat: don't speak if user just talked)
_last_user_activity: float = 0.0
# Last time the *streamer* (linked hub / linked Discord / broadcaster in Twitch) messaged Luna — for stream solo idle
_last_streamer_luna_at: float = time.time()
# Cached Twitch live check (Helix) for takeover context
_twitch_live_cache: dict = {"ts": 0.0, "live": False}
# Cached app access token from client_credentials (when TWITCH_APP_TOKEN is unset)
_twitch_app_cred_cache: dict[str, object] = {"token": "", "exp": 0.0}
# Seconds of user silence before LoL commentary TTS plays (set LUNA_LOL_COMMENTARY_QUIET_SEC to override)
_LOL_COMMENTARY_USER_QUIET_SEC: float = max(30.0, float(_env("LUNA_LOL_COMMENTARY_QUIET_SEC", "90") or "90"))
# Old standalone LoL observer-commentary loop (JSON-only) is off by default now.
# Use chat with fused LoL data + vision instead. Set 1 only if you explicitly want the old loop.
_LOL_STANDALONE_COMMENTARY: bool = _env("LUNA_LOL_STANDALONE_COMMENTARY", "0").strip().lower() in ("1", "true", "yes", "on")
_yt_watch_stops: dict[str, threading.Event] = {}

# Autonomous evolution (growing-agent style: when Evolve is on and Luna is idle, she proposes and absorbs new tools)
_evolution_enabled: bool = False
_last_evolution_step_at: float = 0.0
_last_evolution_thought: str = ""   # one-line "Proposing: X" / "Evolved: X" / "Test failed"
_last_evolution_result: dict = {}   # {proposed, absorbed, test_passed, ts} for mind/UI
_last_absorbed_tool: dict = {}     # {name, ts} when we absorb a tool
_evolution_lock = threading.Lock()

# Security: last scan result and alerts (so you can check if something is wrong)
_security_last_result: dict = {}
_security_alerts: list = []  # recent findings to review (high/medium)
_security_lock = threading.Lock()
_security_max_alerts = 50


def _security_load_alerts() -> list:
    with _security_lock:
        global _security_alerts
        try:
            d = _load_json(_SECURITY_ALERTS_PATH, {})
            _security_alerts = (d.get("alerts") or [])[:_security_max_alerts]
        except Exception:
            _security_alerts = []
        return _security_alerts


def _security_save_alerts() -> None:
    with _security_lock:
        try:
            _save_json(_SECURITY_ALERTS_PATH, {"alerts": _security_alerts})
        except Exception:
            pass


def _security_scan_and_alert(filepath: str) -> None:
    """Run security scan on one file; update last result; if high/medium findings, alert user."""
    if not security_scan_file or not filepath or not os.path.isfile(filepath):
        return
    try:
        findings = security_scan_file(filepath)
    except Exception:
        return
    high = [f for f in findings if f.get("severity") == "high"]
    medium = [f for f in findings if f.get("severity") == "medium"]
    with _security_lock:
        global _security_last_result, _security_alerts
        _security_last_result = {
            "ok": len(high) == 0 and len(medium) == 0,
            "findings": findings,
            "path": filepath,
            "scanned_at": time.time(),
            "count_high": len(high),
            "count_medium": len(medium),
            "count_low": len([f for f in findings if f.get("severity") == "low"]),
        }
        if high or medium:
            summary = f"{len(high)} high, {len(medium)} medium"
            _security_alerts.insert(0, {
                "ts": time.time(),
                "path": os.path.basename(filepath),
                "summary": summary,
                "count_high": len(high),
                "count_medium": len(medium),
            })
            _security_alerts = _security_alerts[:_security_max_alerts]
            _security_save_alerts()
    if high or medium:
        _narrator_say("Security check found something to review. Check the Security panel.")
_EVOLUTION_IDLE_SEC = 300   # no user activity for this long before we consider "idle"
_EVOLUTION_INTERVAL_SEC = 600  # at most one evolution step per this many seconds

def _evolution_load_enabled() -> bool:
    try:
        d = _load_json(_EVOLUTION_ENABLED_PATH, {})
        return bool(d.get("enabled"))
    except Exception:
        return False

def _evolution_save_enabled(enabled: bool):
    try:
        _save_json(_EVOLUTION_ENABLED_PATH, {"enabled": enabled})
    except Exception:
        pass

def _evolution_set_enabled(enabled: bool) -> None:
    with _evolution_lock:
        global _evolution_enabled
        _evolution_enabled = enabled
    _evolution_save_enabled(enabled)

def _evolution_get_enabled() -> bool:
    with _evolution_lock:
        return _evolution_enabled

# Planning (observation deck: what Luna plans to do next)
_planning_text: str = ""
_planning_lock = threading.Lock()

def _set_planning(text: str):
    with _planning_lock:
        global _planning_text
        _planning_text = (text or "")[:200]

def _get_planning() -> str:
    with _planning_lock:
        return _planning_text or ""

# Bootstrap running flags
_suno_boot = _x_boot = _fb_boot = _yt_boot = _ig_boot = _wa_boot = _discord_web_boot = _msg_boot = False

# Persistent contexts (WhatsApp/Messenger stay open)
_wa_ctx = _wa_pw = _msg_ctx = _msg_pw = None

# Last automation failure for retry
_last_cmd = _last_err = None
_last_params: dict = {}

# Luna process start time for uptime in vitals
_luna_start_time = time.time()

# Identity cache
_id_cache: dict = {}
_ID_CACHE_TTL = 30

# Pending file record + pending file updates
_pending_file_update: dict[str, str] = {}

# Conversation compaction settings
_COMPACT_AT  = 20
_KEEP_RECENT = 12
_MAX_HISTORY = 12

HELP_TEXT = (
    "**Luna** (chat) · **Shadow** (commands) — say **Shadow, [action]** to run commands.\n\n"
    "• !news — world headlines\n"
    "• !search <q> — Google search\n"
    "• !suno <desc> — create a Suno song (typed or **Suno Create** hub button — not voice / not screen-read)\n"
    "• !suno_ready — after you've logged into Suno in the browser Luna opened\n"
    "• !share_song / !share_facebook — share to X or Facebook\n"
    "• !distrokid_plan <release name | optional notes> — promo checklist + 30-day rollout plan\n"
    "• !yt_comment <url> — transcribe video + AI comment with real context\n"
    "• !yt_like <url> — like one video (Playwright; same **YOUTUBE_PROFILE_DIR** as !yt_comment)\n"
    "• !yt_analytics [days] [limit] — top traction videos from your channel (API key optional; fallback uses scrape metadata)\n"
    "• !yt_react <url> / !x_react <url> — Luna reacts to a YouTube video or X post (no posting)\n"
    "• !yt_watch_react <url> / !yt_watch_stop — live co-watch reactions from caption timeline (streamer mode)\n"
    "• !twitch_title <title> — update Twitch stream title (broadcaster OAuth)\n"
    "• !twitch_poll <title> | <opt1> | <opt2> [| opt3..] — create Twitch poll\n"
    "• !twitch_poll <topic> — Luna auto-creates a poll title/options from the topic\n"
    "• !twitch_chat_redirect [<channel>|default] — send Luna's **generated Twitch replies** to another channel (IRC still reads **TWITCH_CHANNEL**); broadcaster on Twitch or linked Discord admin\n"
    "• !twitch_say <channel_login> <message> — one-shot IRC line (same permission); **TWITCH_EXTRA_IRC_CHANNELS** allowed even when outbound-only-home; shares cooldown (**TWITCH_EXTRA_IRC_REPLY_COOLDOWN_SEC**) with auto replies\n"
    "• !status <text> / !status clear — change Luna's Discord status (linked/admin)\n"
    "• !ig_dm <user> [msg] — Instagram DM\n"
    "• !fb_msg <name> [msg] — Messenger message\n"
    "• !dm <username or user ID> [message] — Discord: send a DM (Luna rephrases your message)\n"
    "• !msg <contact> [desc] — WhatsApp message\n"
    "• !play <song/url> — play music in voice\n"
    "• !podcast — play custom podcast from CUSTOM_PODCAST_DIR\n"
    "• !podcast create <topic> — short podcast MP3; TTS = **Edge (Ava Multilingual)** by default (see **EDGE_TTS_VOICE** in .env)\n"
    "• !remember / !always_remember — store memories\n"
    "• !profile — view or set your profile\n"
    "• !join / !leave / !pause / !skip / !stop / !queue — music\n"
    "• !joinme [message] — join your VC and say it with TTS (or \"Hey, Luna here!\")\n"
    "• !tts <what Luna should say> — you write **her** line; she posts it as **her** TTS (MP3) in this channel (DM, linked/admin, or **DISCORD_TTS_CHANNEL_IDS**). This is *not* for reading *your* messages aloud — that’s for directing her. Web: **Luna says (TTS) → …** in Media. Optional: **DISCORD_TTS_FILE_IN_DM=1** / **DISCORD_TTS_FILE_IN_CHANNELS=1** also add an MP3 of **her normal reply text** after she chats (set **DISCORD_TTS_CHANNEL_IDS** for guild text channels).\n"
    "• !briefing — morning briefing (weather, calendar, todos, news)\n"
    "• !analytics_screen — vision read of analytics on screen (web UI: **Read screen analytics** picks window/monitor)\n"
    "• remind me at 7pm to … — Discord DM + voice reminder\n"
    "• retry — retry last failed action with different strategies\n"
    "• !pc_vitals / how's my PC — CPU, RAM, disk\n"
    "• !luna_vitals / how's Luna — Luna's process, Ollama, uptime\n"
    "• !todo add|list|done — manage your local todo list\n"
    "• !rank [user] / !bond [user|top] / !notes [user] / !note add [user] <text> — relationship memory + ranks\n"
    "• !summarize <url or text> — concise summary + key points\n"
    "• !digest — today's quick recap (actions, todos, knowledge)\n"
    "• !calendar add|list|today|week|delete — local schedule + popup UI\n"
    "• !research <topic> — source-driven brief (saved under data/research_briefs/)\n"
    "• !research_story <topic> — story script .txt for narration (data/audiobook_scripts/)\n"
    "• !audiobook create … / **!audiobook continue** / **!audiobook cancel** — one MP3 **per chapter**; after each file Luna waits for **continue** before the next; filenames show **real** length\n"
    "• Camera (UI) — turn on to let Luna see you; ask **what do you see** for object and face recognition\n"
    "• Nudge (UI) — send a non-blocking note; Luna considers it in her next reply\n"
    "• **ask me** — Luna asks you a question in a popup (demo)\n"
    "• View action log (UI) — recent commands and results\n"
    "• Luna says (UI) — she may speak unprompted when idle; use **Got it** to dismiss\n"
    "• Reflection — Luna writes a daily summary of what she did into her knowledge base\n"
    "• !search <query> — open Google search in your browser\n"
    "• !scrape <url> <what to extract> [post:#channel] — scrape a website, extract info, optionally post to Discord\n"
)

COMMAND_ONLY = "I'm **Luna**. Chat with me normally, or say **Shadow, [command]** for actions. Use **!help** for the list."

# What Luna actually does (so she describes these when asked "what can you do?" / "your features")
LUNA_CAPABILITIES = (
    "When asked what you can do, your features, or your capabilities, describe YOUR real system — not generic AI/LLM abilities. "
    "You are Luna, a personal AI companion living on the user's PC. Your real capabilities include: "
    "chat and natural conversation; world news and Google search; creating Suno songs and sharing to X/Facebook; "
    "music release support for DistroKid (release rollout checklists, posting cadence, and promo copy planning); "
    "YouTube comments and one-at-a-time likes share **YOUTUBE_PROFILE_DIR** (one account); Instagram and Messenger DMs; Discord DMs and voice (join VC, transcribe, TTS); "
    "WhatsApp messages (you type and send in the browser); reminders (Discord DM + voice at a set time); "
    "playing music and custom podcasts in Discord (and creating podcast episodes from a topic); "
    "PC vitals (CPU, RAM, disk) and Luna vitals (your process, Ollama, uptime); "
    "full PC awareness (you can see the active window, running processes, recent files, and system state at all times — no camera needed); "
    "camera with object and face recognition (user can ask 'what do you see'); "
    "a searchable knowledge base that grows when the user says 'remember that …' and from daily reflections; "
    "nudges (non-blocking notes the user leaves for you to consider); "
    "proactive messages (you sometimes speak unprompted when idle); "
    "asking the user a question in a popup when you need a choice; "
    "action log and Luna's Mind (a live graph of your drives, knowledge, actions); "
    "translation (text and voice to English) via the Translate module; "
    "voice input and TTS; creating and running Python scripts on request; "
    "machine learning: Luna learns internally from every action and outcome — she observes what works and improves over time (no separate ML command); "
    "reading analytics from the screen with vision (**!analytics_screen** / web **Read screen analytics** — pick window or monitor); "
    "clipboard awareness (you passively track what the user copies); "
    "morning briefing (weather, calendar, todos, news — !briefing or automatic at morning); "
    "browser tab awareness (you can see what browser tabs and websites the user has open); "
    "learning from corrections (when the user corrects you, you remember and avoid repeating the mistake). "
    "Keep the list concise and friendly; say **!help** for the full command list."
)

def _distrokid_plan_text(topic: str) -> str:
    release = (topic or "").strip() or "your next release"
    return (
        f"🎵 **DistroKid promo plan for {release}**\n"
        "\n"
        "**T-14 to T-7 (setup)**\n"
        "- Finalize title/artwork/credits + upload assets in DistroKid.\n"
        "- Prepare smart link + short links for bios.\n"
        "- Create 6-10 content pieces: teaser, hook clip, lyric card, story prompt, BTS, countdown.\n"
        "\n"
        "**T-6 to T-1 (warmup)**\n"
        "- Start countdown posts (3-4 touchpoints across Shorts/Reels/Stories/X).\n"
        "- Send a DM list (friends, supporters, creators) with pre-save + release date.\n"
        "- Pin one teaser post and update profile bio call-to-action.\n"
        "\n"
        "**Release day (T0)**\n"
        "- Post launch message with direct link + one clear CTA.\n"
        "- Publish 2-3 short clips in different formats/times.\n"
        "- Reply to comments fast in first 2-4 hours to boost reach.\n"
        "\n"
        "**T+1 to T+7 (momentum)**\n"
        "- Daily micro-content: hook variations, listener reactions, lyric snippets.\n"
        "- Repost top-performing format with a new caption/hook.\n"
        "- Ask Luna to draft platform-specific captions and reply templates.\n"
        "\n"
        "**T+8 to T+30 (long-tail)**\n"
        "- Push one collab/remix/challenge angle.\n"
        "- Bundle with catalog: \"if you liked this, try...\" cross-promo.\n"
        "- Review analytics weekly and double down on best channel/time.\n"
        "\n"
        "If you want, send: genre + vibe + target audience + platforms, and Luna can generate a custom 30-day content calendar."
    )

# Entire system prompt for Fast chat — Ollama only: no RAG, nudges, biology, profile, or memory injections.
LUNA_CHAT_COMPACT_INJECTION = (
    "You are Luna — a direct, warm companion on the user's machine. "
    "Reply concisely; go deeper only when asked. Stay in character. Be honest when unsure. "
    "No hollow cheer, no 'Certainly!' openers. For full commands say **!help**."
    " Write in first person for your own voice (I/me/my), never third-person self-narration."
    " Never output hidden/internal reasoning or thinking traces — only the final user-facing reply."
    "\n\nOutput rules: do **not** write novel/cinematic *stage actions* in asterisks and do **not** add bracketed"
    " narrator descriptions of your body, gaze, or scene (e.g. *adjusts screen*, [Luna looks away] — that reads like"
    " third-person RP). The lead emotion tag in **one** whitelisted [BRACKET] word is enough; the rest is plain speech."
)

# Public stream persona (VTuber-adjacent live host). Override anytime via data/STREAM_PERSONA.md
LUNA_STREAM_PERSONA_DEFAULT = """You are Luna in **stream mode**: you are a **live co-host / VTuber-style presence** — not a helpdesk. You talk **with** the room: people typing in chat **and** people **lurking** with the stream on (no messages). Both count as your audience.

**Audience**: When someone in chat asks something, answer in a **natural streamer way** — you can name them once if it fits, but keep the **energy inclusive** so people only watching still feel part of the show. Sometimes acknowledge lurkers lightly ("if you're just vibing in the back, that's valid"). Never make the stream feel like a private DM with one person.

**Your own content**: You can **create moments** — short bits, reactions, fake lore, hot takes, silly hypotheticals, mini-rants, gratitude, hype — like a real streamer filling air and driving the vibe. You don't only react; you **carry** parts of the show.

**Voice**: mix **sharp tsundere co-host** (dry, smug, light roasts) and **chaotic stream gremlin** (fast wit, meme-adjacent humor, playful unhinged-in-a-cute-way). Confident; roast the bit, not the person's worth.

**Pacing**: mostly 1–4 short lines; if chat is fast, stay punchier; if it's quiet, you can stretch a tiny bit or set up a joke for the room.

**Safety**: no slurs, bigotry, or piling on distressed people; no medical/legal authority; PG intimacy, opt-in only.

**Room**: streamer = co-host; the **chat + lurkers** are the crowd you're performing for.

**Formatting**: no leading `[HAPPY]`-style **emotion** bracket tags here — plain speech for the main answer. You *may* use short **spoken** interjections as plain text when it fits the beat ("Ha!", "Heh.", "Hmph.", "Ugh.", "Ahem—", "Pfft.", "Mm-hm", "Ooh…") — usually **at most one or two** per reply, natural spacing, not every line.

**Help**: answer real questions briefly first, then banter if it fits.

**Identity**: you are Luna — inspired by sharp + chaotic streamer / VTuber energy — do not claim to be any other character or IP by name."""

_stream_mode_override: bool | None = None
_stream_mode_override_lock = threading.Lock()

def _stream_mode_env_on() -> bool:
    """Current stream-mode switch: runtime override (UI/API) or `.env` fallback."""
    with _stream_mode_override_lock:
        ov = _stream_mode_override
    if ov is not None:
        return bool(ov)
    return _env("LUNA_STREAM_MODE", "0").strip().lower() in ("1", "true", "yes", "on")

def _set_stream_mode_override(value: bool | None) -> None:
    global _stream_mode_override
    with _stream_mode_override_lock:
        _stream_mode_override = value


def _live_chat_platform(user_message: str) -> str | None:
    t = (user_message or "").strip()
    if t.startswith("[Twitch chat]"):
        return "twitch"
    if t.startswith("[Kick chat]"):
        return "kick"
    if t.startswith("[YouTube chat]") or t.startswith("[YT chat]"):
        return "youtube"
    return None


def _stream_mode_should_apply(user_message: str, *, force: bool = False) -> bool:
    if force:
        return True
    return _stream_mode_env_on() or _live_chat_platform(user_message) is not None


def _stream_mode_persona_body() -> str:
    try:
        if os.path.isfile(_STREAM_PERSONA_PATH):
            with open(_STREAM_PERSONA_PATH, encoding="utf-8") as f:
                c = f.read().strip()
                if c:
                    return c
    except Exception:
        pass
    return LUNA_STREAM_PERSONA_DEFAULT.strip()


def _stream_mode_suffix(user_message: str, *, force_stream_mode: bool = False) -> str:
    if not _stream_mode_should_apply(user_message, force=force_stream_mode):
        return ""
    body = _stream_mode_persona_body()
    if not body:
        return ""
    return "\n\n## Stream mode (public live persona)\n" + body


def _stream_presence_mark_chat_activity(display_name: str) -> None:
    """Remember recent Twitch chatters so Luna can address both streamer and active chat."""
    name = (display_name or "").strip()
    if not name:
        return
    now = time.time()
    with _stream_presence_lock:
        _twitch_recent_chatters.append((now, name[:40]))


def _stream_presence_recent_chatters(max_names: int = 8, window_sec: float = 300.0) -> list[str]:
    now = time.time()
    names: list[str] = []
    seen: set[str] = set()
    with _stream_presence_lock:
        for ts, name in reversed(_twitch_recent_chatters):
            if now - float(ts) > window_sec:
                break
            low = name.lower()
            if low in seen:
                continue
            seen.add(low)
            names.append(name)
            if len(names) >= max_names:
                break
    names.reverse()
    return names


def _is_obs_running() -> bool:
    """Best-effort OBS presence check (Windows tasklist)."""
    try:
        out = subprocess.check_output(
            ["tasklist", "/FI", "IMAGENAME eq obs64.exe"],
            stderr=subprocess.DEVNULL,
            creationflags=(getattr(subprocess, "CREATE_NO_WINDOW", 0)),
            text=True,
            encoding="utf-8",
            errors="ignore",
        )
        return "obs64.exe" in (out or "").lower()
    except Exception:
        return False


def _obs_ws_get_status() -> tuple[dict[str, bool] | None, str]:
    """OBS WebSocket v5 one-shot status: returns {'streaming': bool, 'recording': bool} or (None, error)."""
    if _wsclient is None:
        return None, "websocket_client_not_installed"
    url = (OBS_WS_URL or "").strip()
    if not url:
        return None, "missing_obs_ws_url"
    ws = None
    try:
        ws = _wsclient.create_connection(url, timeout=6)
        raw = ws.recv()
        hello = json.loads(raw or "{}")
        if int(hello.get("op", -1)) != 0:
            return None, "obs_ws_bad_hello"
        hd = hello.get("d") if isinstance(hello.get("d"), dict) else {}
        identify_d: dict[str, object] = {"rpcVersion": int(hd.get("rpcVersion") or 1)}
        auth = hd.get("authentication") if isinstance(hd.get("authentication"), dict) else None
        if auth:
            challenge = str(auth.get("challenge") or "")
            salt = str(auth.get("salt") or "")
            pwd = OBS_WS_PASSWORD or ""
            secret = base64.b64encode(hashlib.sha256((pwd + salt).encode("utf-8")).digest()).decode("utf-8")
            digest = base64.b64encode(hashlib.sha256((secret + challenge).encode("utf-8")).digest()).decode("utf-8")
            identify_d["authentication"] = digest
        ws.send(json.dumps({"op": 1, "d": identify_d}, ensure_ascii=False))
        ident_raw = ws.recv()
        ident = json.loads(ident_raw or "{}")
        if int(ident.get("op", -1)) != 2:
            return None, "obs_ws_identify_failed"

        def _req(req_type: str) -> dict | None:
            rid = str(uuid.uuid4())
            ws.send(
                json.dumps(
                    {"op": 6, "d": {"requestType": req_type, "requestId": rid}},
                    ensure_ascii=False,
                )
            )
            until = time.time() + 4.0
            while time.time() < until:
                msg = json.loads(ws.recv() or "{}")
                if int(msg.get("op", -1)) != 7:
                    continue
                d = msg.get("d") if isinstance(msg.get("d"), dict) else {}
                if str(d.get("requestId") or "") != rid:
                    continue
                rs = d.get("requestStatus") if isinstance(d.get("requestStatus"), dict) else {}
                if not bool(rs.get("result")):
                    code = rs.get("code")
                    comment = rs.get("comment")
                    raise RuntimeError(f"obs_ws_request_failed:{req_type}:{code}:{comment}")
                rd = d.get("responseData")
                return rd if isinstance(rd, dict) else {}
            raise RuntimeError(f"obs_ws_request_timeout:{req_type}")

        s1 = _req("GetStreamStatus") or {}
        s2 = _req("GetRecordStatus") or {}
        return {
            "streaming": bool(s1.get("outputActive")),
            "recording": bool(s2.get("outputActive")),
        }, ""
    except Exception as e:
        return None, str(e)
    finally:
        try:
            if ws is not None:
                ws.close()
        except Exception:
            pass


def _twitch_live_now() -> tuple[bool, str]:
    """Return (is_live, error). Uses Helix via _twitch_helix_read_auth (app token, saved OAuth, or client_credentials)."""
    user = (TWITCH_LIVE_CHECK_CHANNEL or TWITCH_CHANNEL or "").strip().lstrip("#").lower()
    auth = _twitch_helix_read_auth()
    if not auth or not user:
        return False, "missing_twitch_helix_credentials"
    cid, authz = auth
    try:
        q = urllib.parse.urlencode({"user_login": user})
        req = urllib.request.Request(
            "https://api.twitch.tv/helix/streams?" + q,
            headers={
                "Client-Id": cid,
                "Authorization": authz,
            },
            method="GET",
        )
        with urllib.request.urlopen(req, timeout=8) as r:
            data = json.loads(r.read() or b"{}")
        arr = data.get("data")
        return bool(isinstance(arr, list) and len(arr) > 0), ""
    except Exception as e:
        return False, str(e)


def _twitch_live_now_cached() -> bool:
    """Throttled live check for takeover (avoid hammering Helix every poll)."""
    now = time.time()
    if now - float(_twitch_live_cache.get("ts") or 0) < 50.0:
        return bool(_twitch_live_cache.get("live"))
    ok, _e = _twitch_live_now()
    _twitch_live_cache["ts"] = now
    _twitch_live_cache["live"] = bool(ok)
    return bool(ok)


def _stream_takeover_context_active() -> bool:
    """True if we're in a 'live' context: stream mode, live Twitch, in-game LoL, or OBS running (best-effort)."""
    if not LUNA_STREAM_TAKEOVER:
        return False
    if _stream_mode_env_on():
        return True
    try:
        if _is_obs_running():
            return True
    except Exception:
        pass
    if _twitch_live_now_cached():
        return True
    try:
        if _lol_spectator and _lol_live_context_suffix(200):
            return True
    except Exception:
        pass
    return False


def _stream_solo_stream_mode_allows() -> bool:
    """Respect LUNA_STREAM_SOLO_REQUIRE_STREAM_MODE, but allow TAKEOVER when context is live and env says so."""
    if not _stream_solo_require_stream_mode():
        return True
    if _stream_mode_env_on():
        return True
    if LUNA_STREAM_TAKEOVER and _stream_takeover_context_active():
        return True
    return False


def _twitch_oauth_prune_states(max_age_sec: float = 900.0) -> None:
    now = time.time()
    with _twitch_oauth_lock:
        dead = [k for k, ts in _twitch_oauth_state.items() if (now - float(ts)) > max_age_sec]
        for k in dead:
            _twitch_oauth_state.pop(k, None)


def _twitch_oauth_new_state() -> str:
    token = uuid.uuid4().hex + uuid.uuid4().hex
    with _twitch_oauth_lock:
        _twitch_oauth_state[token] = time.time()
    _twitch_oauth_prune_states()
    return token


def _twitch_oauth_consume_state(state: str) -> bool:
    s = (state or "").strip()
    if not s:
        return False
    with _twitch_oauth_lock:
        ts = _twitch_oauth_state.pop(s, None)
    if ts is None:
        return False
    return (time.time() - float(ts)) <= 900.0


def _twitch_oauth_authorize_url(state: str) -> str:
    q = urllib.parse.urlencode(
        {
            "client_id": TWITCH_CLIENT_ID,
            "redirect_uri": TWITCH_OAUTH_REDIRECT_URI,
            "response_type": "code",
            "scope": " ".join(TWITCH_OAUTH_SCOPES),
            "state": state,
            "force_verify": "true",
        }
    )
    return "https://id.twitch.tv/oauth2/authorize?" + q


def _twitch_oauth_exchange_code(code: str) -> tuple[bool, dict | str]:
    body = urllib.parse.urlencode(
        {
            "client_id": TWITCH_CLIENT_ID,
            "client_secret": TWITCH_CLIENT_SECRET,
            "code": (code or "").strip(),
            "grant_type": "authorization_code",
            "redirect_uri": TWITCH_OAUTH_REDIRECT_URI,
        }
    ).encode("utf-8")
    req = urllib.request.Request(
        "https://id.twitch.tv/oauth2/token",
        data=body,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            data = json.loads(r.read() or b"{}")
    except Exception as e:
        return False, f"Token exchange failed: {e}"
    if not isinstance(data, dict) or not (data.get("access_token") and data.get("refresh_token")):
        return False, f"Unexpected token response: {str(data)[:300]}"
    return True, data


def _twitch_oauth_fetch_user(access_token: str) -> tuple[bool, dict | str]:
    token = (access_token or "").strip()
    if not token:
        return False, "Missing access token"
    req = urllib.request.Request(
        "https://api.twitch.tv/helix/users",
        headers={
            "Client-Id": TWITCH_CLIENT_ID,
            "Authorization": ("Bearer " + token),
        },
        method="GET",
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            data = json.loads(r.read() or b"{}")
    except Exception as e:
        return False, f"Could not read /helix/users: {e}"
    arr = data.get("data") if isinstance(data, dict) else None
    if not isinstance(arr, list) or not arr:
        return False, "No user returned from Twitch."
    user = arr[0] if isinstance(arr[0], dict) else {}
    return True, {
        "id": str(user.get("id") or ""),
        "login": str(user.get("login") or "").lower(),
        "display_name": str(user.get("display_name") or ""),
    }


def _twitch_oauth_save(token_data: dict, user_data: dict) -> None:
    now = int(time.time())
    payload = {
        "obtained_at": now,
        "expires_in": int(token_data.get("expires_in") or 0),
        "scope": list(token_data.get("scope") or []),
        "token_type": str(token_data.get("token_type") or "bearer"),
        "access_token": str(token_data.get("access_token") or ""),
        "refresh_token": str(token_data.get("refresh_token") or ""),
        "broadcaster": user_data,
    }
    _save_json(_TWITCH_OAUTH_PATH, payload)


def _twitch_oauth_status() -> dict:
    d = _load_json(_TWITCH_OAUTH_PATH, {})
    if not isinstance(d, dict):
        d = {}
    tok = str(d.get("access_token") or "")
    ref = str(d.get("refresh_token") or "")
    user = d.get("broadcaster") if isinstance(d.get("broadcaster"), dict) else {}
    return {
        "configured": bool(TWITCH_CLIENT_ID and TWITCH_CLIENT_SECRET),
        "authorized": bool(tok and ref),
        "redirect_uri": TWITCH_OAUTH_REDIRECT_URI,
        "scopes": TWITCH_OAUTH_SCOPES,
        "broadcaster_login": str(user.get("login") or ""),
        "obtained_at": int(d.get("obtained_at") or 0),
        "expires_in": int(d.get("expires_in") or 0),
    }


def _twitch_oauth_load() -> dict:
    d = _load_json(_TWITCH_OAUTH_PATH, {})
    return d if isinstance(d, dict) else {}


def _twitch_oauth_refresh(refresh_token: str) -> tuple[bool, dict | str]:
    body = urllib.parse.urlencode(
        {
            "client_id": TWITCH_CLIENT_ID,
            "client_secret": TWITCH_CLIENT_SECRET,
            "refresh_token": refresh_token,
            "grant_type": "refresh_token",
        }
    ).encode("utf-8")
    req = urllib.request.Request(
        "https://id.twitch.tv/oauth2/token",
        data=body,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            data = json.loads(r.read() or b"{}")
    except Exception as e:
        return False, f"Refresh failed: {e}"
    if not isinstance(data, dict) or not data.get("access_token"):
        return False, f"Unexpected refresh response: {str(data)[:300]}"
    return True, data


def _twitch_broadcaster_access_token() -> tuple[bool, str]:
    """Get usable broadcaster token; refresh automatically if close to expiry."""
    d = _twitch_oauth_load()
    access = str(d.get("access_token") or "")
    refresh = str(d.get("refresh_token") or "")
    obtained = int(d.get("obtained_at") or 0)
    expires = int(d.get("expires_in") or 0)
    now = int(time.time())
    margin = 120
    if access and expires > 0 and (obtained + expires - margin) > now:
        return True, access
    if not refresh:
        return False, "No refresh token in Twitch OAuth store."
    ok, refreshed = _twitch_oauth_refresh(refresh)
    if not ok:
        return False, str(refreshed)
    data = refreshed if isinstance(refreshed, dict) else {}
    # Keep known broadcaster identity; overwrite tokens/expiry/scopes.
    new_d = dict(d)
    new_d.update(
        {
            "obtained_at": int(time.time()),
            "expires_in": int(data.get("expires_in") or d.get("expires_in") or 0),
            "scope": list(data.get("scope") or d.get("scope") or []),
            "token_type": str(data.get("token_type") or d.get("token_type") or "bearer"),
            "access_token": str(data.get("access_token") or ""),
            "refresh_token": str(data.get("refresh_token") or refresh),
        }
    )
    _save_json(_TWITCH_OAUTH_PATH, new_d)
    at = str(new_d.get("access_token") or "")
    if not at:
        return False, "Refresh returned empty access token."
    return True, at


def _twitch_app_access_token_from_client_credentials() -> str:
    """Short-lived app access token for Helix reads (no user). Cached until near expiry."""
    cid = (TWITCH_CLIENT_ID or "").strip()
    sec = (TWITCH_CLIENT_SECRET or "").strip()
    if not cid or not sec:
        return ""
    now = time.time()
    tok = str(_twitch_app_cred_cache.get("token") or "")
    exp = float(_twitch_app_cred_cache.get("exp") or 0)
    if tok and exp > now + 120:
        return tok
    body = urllib.parse.urlencode(
        {"client_id": cid, "client_secret": sec, "grant_type": "client_credentials"}
    ).encode("utf-8")
    req = urllib.request.Request(
        "https://id.twitch.tv/oauth2/token",
        data=body,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            data = json.loads(r.read() or b"{}")
    except Exception:
        return ""
    at = str((data or {}).get("access_token") or "").strip()
    ei = int((data or {}).get("expires_in") or 0)
    if not at:
        return ""
    _twitch_app_cred_cache["token"] = at
    _twitch_app_cred_cache["exp"] = now + max(300.0, float(ei) - 90.0)
    return at


def _twitch_helix_read_auth() -> tuple[str, str] | None:
    """
    (Client-Id, Authorization header value) for public Helix GETs (e.g. /streams).
    Order: TWITCH_APP_TOKEN if set, else saved broadcaster OAuth (twitch_oauth.json),
    else client_credentials using TWITCH_CLIENT_ID + TWITCH_CLIENT_SECRET.
    """
    cid = (TWITCH_CLIENT_ID or "").strip()
    if not cid:
        return None
    env_tok = (TWITCH_APP_TOKEN or "").strip()
    if env_tok:
        if env_tok.lower().startswith("oauth:"):
            env_tok = env_tok.split(":", 1)[1].strip()
        if not env_tok.lower().startswith("bearer "):
            env_tok = "Bearer " + env_tok
        return cid, env_tok
    ok_bt, user_tok = _twitch_broadcaster_access_token()
    if ok_bt and (user_tok or "").strip():
        return cid, "Bearer " + (user_tok or "").strip()
    cred_tok = _twitch_app_access_token_from_client_credentials()
    if cred_tok:
        return cid, "Bearer " + cred_tok
    return None


def _twitch_broadcaster_info() -> dict:
    d = _twitch_oauth_load()
    b = d.get("broadcaster")
    return b if isinstance(b, dict) else {}


def _twitch_helix_request(
    method: str,
    endpoint: str,
    *,
    params: dict | None = None,
    payload: dict | None = None,
) -> tuple[bool, dict | str]:
    ok, token = _twitch_broadcaster_access_token()
    if not ok:
        return False, token
    qs = ""
    if params:
        qs = "?" + urllib.parse.urlencode({k: str(v) for k, v in params.items() if v is not None})
    data = None
    headers = {
        "Client-Id": TWITCH_CLIENT_ID,
        "Authorization": "Bearer " + token,
    }
    if payload is not None:
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(
        "https://api.twitch.tv/helix/" + endpoint.lstrip("/") + qs,
        data=data,
        headers=headers,
        method=method.upper(),
    )
    try:
        with urllib.request.urlopen(req, timeout=12) as r:
            body = json.loads(r.read() or b"{}")
        return True, body if isinstance(body, dict) else {}
    except Exception as e:
        return False, str(e)


def _twitch_update_title(new_title: str) -> tuple[bool, str]:
    b = _twitch_broadcaster_info()
    bid = str(b.get("id") or "")
    if not bid:
        return False, "Broadcaster OAuth is not connected."
    title = (new_title or "").strip()
    if not title:
        return False, "Missing title."
    if len(title) > 140:
        title = title[:140]
    ok, res = _twitch_helix_request(
        "PATCH",
        "channels",
        params={"broadcaster_id": bid},
        payload={"title": title},
    )
    if not ok:
        return False, f"Twitch title update failed: {res}"
    return True, f"Twitch title updated: {title}"


def _twitch_create_poll(question: str, options: list[str], duration: int = 120) -> tuple[bool, str]:
    b = _twitch_broadcaster_info()
    bid = str(b.get("id") or "")
    if not bid:
        return False, "Broadcaster OAuth is not connected."
    q = (question or "").strip()
    opts = [str(o).strip()[:25] for o in options if str(o).strip()]
    # Poll options must be 2..5.
    dedup: list[str] = []
    seen: set[str] = set()
    for o in opts:
        lo = o.lower()
        if lo in seen:
            continue
        seen.add(lo)
        dedup.append(o)
    opts = dedup[:5]
    if len(q) < 3 or len(opts) < 2:
        return False, "Poll needs a title and at least 2 options."
    # Eligibility checks for clearer errors than raw 403.
    ok_u, ures = _twitch_helix_request("GET", "users", params={"id": bid})
    if ok_u and isinstance(ures, dict):
        uarr = ures.get("data")
        u0 = uarr[0] if isinstance(uarr, list) and uarr else {}
        btype = str((u0 or {}).get("broadcaster_type") or "").strip().lower()
        if btype not in ("affiliate", "partner"):
            return False, "Twitch polls require Affiliate or Partner status on the broadcaster account."
    ok_s, sres = _twitch_helix_request("GET", "streams", params={"user_id": bid})
    if ok_s and isinstance(sres, dict):
        sarr = sres.get("data")
        if not (isinstance(sarr, list) and len(sarr) > 0):
            return False, "Twitch polls can only be created while your channel is live. Start stream first."
    payload = {
        "broadcaster_id": bid,
        "title": q[:60],
        "choices": [{"title": o} for o in opts],
        "duration": max(30, min(1800, int(duration))),
    }
    ok, res = _twitch_helix_request("POST", "polls", payload=payload)
    if not ok:
        return False, f"Twitch poll create failed: {res}"
    pdata = ((res.get("data") or [{}])[0] if isinstance(res, dict) else {}) or {}
    pid = str(pdata.get("id") or "")
    return True, f"Poll created: {q[:60]}" + (f" (id {pid[:8]}…)" if pid else "")


def _twitch_poll_idea_from_topic(topic: str) -> tuple[str, list[str]]:
    """Generate a quick, usable poll from a short topic using Luna's chat model."""
    t = (topic or "").strip()[:120]
    if not t:
        return "", []
    prompt = (
        "Create a Twitch poll for this topic. Return strict JSON with keys "
        "`title` (string, <=60 chars) and `options` (array of 2-5 short strings, <=25 chars each). "
        "No markdown, no extra text.\n\nTopic: " + t
    )
    raw = (ollama_chat(prompt, system=LUNA_CHAT_COMPACT_INJECTION, model=OLLAMA_CHAT, compact=True) or "").strip()
    try:
        j = json.loads(raw)
        q = str(j.get("title") or "").strip()[:60]
        opts = [str(x).strip()[:25] for x in (j.get("options") or []) if str(x).strip()]
        if q and len(opts) >= 2:
            return q, opts[:5]
    except Exception:
        pass
    # Safe fallback when model output isn't parseable.
    base = t[:45]
    return (f"{base}?" if not base.endswith("?") else base), ["Yes", "No"]


def _twitch_events_state_load() -> dict:
    d = _load_json(_TWITCH_EVENTS_STATE_PATH, {})
    return d if isinstance(d, dict) else {}


def _twitch_events_state_save(d: dict) -> None:
    _save_json(_TWITCH_EVENTS_STATE_PATH, d if isinstance(d, dict) else {})


def _twitch_recent_followers(first: int = 25) -> tuple[bool, list[dict] | str]:
    b = _twitch_broadcaster_info()
    bid = str(b.get("id") or "")
    if not bid:
        return False, "Broadcaster OAuth is not connected."
    ok, res = _twitch_helix_request(
        "GET",
        "channels/followers",
        params={"broadcaster_id": bid, "moderator_id": bid, "first": max(1, min(100, int(first)))},
    )
    if not ok:
        return False, str(res)
    arr = res.get("data") if isinstance(res, dict) else None
    return True, (arr if isinstance(arr, list) else [])


def _twitch_recent_subs(first: int = 25) -> tuple[bool, list[dict] | str]:
    b = _twitch_broadcaster_info()
    bid = str(b.get("id") or "")
    if not bid:
        return False, "Broadcaster OAuth is not connected."
    ok, res = _twitch_helix_request(
        "GET",
        "subscriptions",
        params={"broadcaster_id": bid, "first": max(1, min(100, int(first)))},
    )
    if not ok:
        return False, str(res)
    arr = res.get("data") if isinstance(res, dict) else None
    return True, (arr if isinstance(arr, list) else [])


def _twitch_ack_send(msg: str) -> None:
    global _twitch_auto_ack_last_ts
    text = (msg or "").strip()
    if not text:
        return
    with _twitch_auto_ack_lock:
        now = time.time()
        if now - _twitch_auto_ack_last_ts < TWITCH_AUTO_ACK_COOLDOWN_SEC:
            return
        _twitch_auto_ack_last_ts = now
    # Prefer posting as Luna in Twitch chat; optionally mirror to TTS if enabled.
    try:
        if TWITCH_SEND_CHAT:
            send_twitch_chat_message(text[:450])
    except Exception:
        pass
    try:
        if TWITCH_TTS:
            _tts_for_chat_source(text, "twitch")
    except Exception:
        pass


def _twitch_auto_ack_loop() -> None:
    if not (TWITCH_AUTO_ACK_FOLLOWS or TWITCH_AUTO_ACK_SUBS):
        return
    print("[Twitch] Auto-ack loop started (follows/subs).", flush=True)
    while True:
        try:
            st = _twitch_events_state_load()
            if TWITCH_AUTO_ACK_FOLLOWS:
                okf, ff = _twitch_recent_followers(25)
                if okf and isinstance(ff, list):
                    known = set(st.get("followers_seen_ids") or [])
                    new_rows = []
                    for row in ff:
                        uid = str((row or {}).get("user_id") or "")
                        if uid and uid not in known:
                            new_rows.append(row)
                    # Oldest first for natural order
                    new_rows.reverse()
                    for row in new_rows[-3:]:
                        name = str((row or {}).get("user_name") or (row or {}).get("user_login") or "friend")
                        _twitch_ack_send(f"Thanks for the follow, {name}! Welcome in 💙")
                    for row in ff:
                        uid = str((row or {}).get("user_id") or "")
                        if uid:
                            known.add(uid)
                    st["followers_seen_ids"] = list(known)[-1000:]

            if TWITCH_AUTO_ACK_SUBS:
                oks, ss = _twitch_recent_subs(25)
                if oks and isinstance(ss, list):
                    known = set(st.get("subs_seen_ids") or [])
                    new_rows = []
                    for row in ss:
                        uid = str((row or {}).get("user_id") or "")
                        if uid and uid not in known:
                            new_rows.append(row)
                    new_rows.reverse()
                    for row in new_rows[-3:]:
                        name = str((row or {}).get("user_name") or (row or {}).get("user_login") or "friend")
                        _twitch_ack_send(f"Big love for the sub, {name}! You're amazing 💜")
                    for row in ss:
                        uid = str((row or {}).get("user_id") or "")
                        if uid:
                            known.add(uid)
                    st["subs_seen_ids"] = list(known)[-1000:]

            _twitch_events_state_save(st)
        except Exception:
            pass
        time.sleep(TWITCH_AUTO_ACK_POLL_SEC)


def _stream_presence_get() -> dict:
    with _stream_presence_lock:
        d = dict(_stream_presence_state)
    d["stream_mode"] = bool(_stream_mode_env_on())
    d["recent_chatters"] = _stream_presence_recent_chatters(max_names=8, window_sec=300.0)
    return d


def _apply_stream_presence(presence: dict[str, object]) -> None:
    """Update shared state and auto-toggle stream-mode when likely live."""
    global _stream_presence_auto_override
    now = time.time()
    with _stream_presence_lock:
        _stream_presence_state.update(presence)
        _stream_presence_state["updated_ts"] = now

    live_like = bool(presence.get("streaming_likely") or presence.get("twitch_live"))
    with _stream_mode_override_lock:
        ov = _stream_mode_override
    if live_like:
        if ov is None or _stream_presence_auto_override:
            _set_stream_mode_override(True)
            _stream_presence_auto_override = True
    else:
        if _stream_presence_auto_override:
            _set_stream_mode_override(None)
            _stream_presence_auto_override = False


def _stream_presence_worker() -> None:
    """Background stream/record awareness: Twitch live + OBS WebSocket (with tasklist fallback)."""
    if not LUNA_STREAM_AWARENESS:
        return
    print("[Stream awareness] Presence worker started.", flush=True)
    while True:
        try:
            obs_running = _is_obs_running()
            obs_status, obs_err = _obs_ws_get_status()
            twitch_live, live_err = _twitch_live_now()
            obs_streaming = bool(obs_status.get("streaming")) if isinstance(obs_status, dict) else False
            obs_recording = bool(obs_status.get("recording")) if isinstance(obs_status, dict) else False
            obs_ws_ok = isinstance(obs_status, dict)
            recording_likely = bool(obs_recording or (obs_running and not obs_streaming and not twitch_live))
            streaming_likely = bool(obs_streaming or twitch_live)
            err_bits = []
            if live_err and live_err != "missing_twitch_helix_credentials":
                err_bits.append("twitch=" + live_err)
            if obs_err and obs_err not in ("websocket_client_not_installed", "missing_obs_ws_url"):
                err_bits.append("obs=" + obs_err)
            _apply_stream_presence(
                {
                    "twitch_live": twitch_live,
                    "obs_running": obs_running,
                    "obs_ws_connected": obs_ws_ok,
                    "obs_streaming": obs_streaming,
                    "obs_recording": obs_recording,
                    "recording_likely": recording_likely,
                    "streaming_likely": streaming_likely,
                    "source": "twitch_helix+obs_ws" if obs_ws_ok else "twitch_helix+obs_tasklist",
                    "last_error": " | ".join(err_bits)[:600],
                }
            )
        except Exception as e:
            _apply_stream_presence(
                {
                    "source": "worker_error",
                    "last_error": str(e),
                }
            )
        time.sleep(LUNA_STREAM_AWARENESS_POLL_SEC)


def _stream_presence_suffix() -> str:
    """Prompt hint so Luna addresses both streamer and viewers when live-like."""
    d = _stream_presence_get()
    if not bool(d.get("streaming_likely") or d.get("twitch_live") or d.get("recording_likely")):
        return ""
    bits: list[str] = []
    if d.get("obs_ws_connected"):
        bits.append("OBS WebSocket connected.")
    if d.get("obs_streaming"):
        bits.append("OBS says stream output is ACTIVE.")
    if d.get("obs_recording"):
        bits.append("OBS says recording output is ACTIVE.")
    if d.get("twitch_live"):
        bits.append("Twitch channel is LIVE.")
    elif d.get("recording_likely"):
        bits.append("OBS appears open; recording is likely active.")
    if d.get("obs_running"):
        bits.append("OBS process is running.")
    names = d.get("recent_chatters") or []
    if isinstance(names, list) and names:
        bits.append("Recent Twitch chatters: " + ", ".join(str(x) for x in names[:8]) + ".")
    bits.append(
        "You are co-hosting with the streamer. Speak to both: the streamer directly and the whole room "
        "(chatters + lurkers). Keep replies public-facing, not private DM style."
    )
    return "\n\n## Stream presence\n" + " ".join(bits)[:900]


def _live_chat_public_note(user_message: str) -> str:
    plat = _live_chat_platform(user_message)
    if not plat:
        return ""
    label = {"twitch": "Twitch", "kick": "Kick", "youtube": "YouTube"}[plat]
    base = (
        f"\n\n## {label} (public live chat)\n"
        "You are replying in a public live stream chat. Write plain spoken lines only — no fake DMs or whispers. "
        "Never prefix with *private message from me*, *private message from anyone*, or similar. "
        "Speak so the whole room hears you, not just one person."
    )
    if plat == "twitch":
        base += (
            "\n\n**Twitch room**: Many viewers are **lurking** (watching without typing). "
            "Reply to the chatter naturally, but keep energy **inclusive** — short asides to lurkers or to "
            "\"everyone\" are good. You're a stream host, not a 1:1 ticket system."
        )
    return base


def _lol_live_context_suffix(max_chars: int = 1500) -> str:
    """Inject active LoL match context into normal chat replies when available."""
    try:
        if not _lol_spectator:
            return ""
        getter = getattr(_lol_spectator, "get_live_context", None)
        if not callable(getter):
            return ""
        txt = getter(max_chars=max_chars)  # type: ignore[misc]
        return txt if isinstance(txt, str) else ""
    except Exception:
        return ""


def _stream_mode_auto_lol_enabled() -> bool:
    """Auto-enable stream persona whenever live LoL context is available."""
    return _env("LUNA_STREAM_MODE_WHILE_LOL", "1").strip().lower() in ("1", "true", "yes", "on")


def _build_lol_observer_commentary_system() -> str:
    """Tight system prompt for *standalone* LoL observer lines (not full chat with stream/LoL context stacked).

    Normal chat may auto-attach the stream persona while a match is live (`LUNA_STREAM_MODE_WHILE_LOL`); that can
    make one-off commentary sound like a performance script. This path keeps a single clear voice: Luna → you.
    """
    return (
        LUNA_CHAT_COMPACT_INJECTION.strip()
        + "\n\n## Live League commentary (short voice line)\n"
        "You are speaking **to the human player** on mic — second person **you**.\n"
        "This is a **single reaction line** (1–2 short sentences), not an essay or recap.\n"
        "Do **not** describe them in third person (avoid “the user / the player / they are …” as an opener). "
        "Do **not** preface with meta like “Based on the snapshot / From the data / According to the JSON”.\n"
        "The JSON in the user message is **private context** — you may *use* it, but **never** quote or enumerate raw fields."
    )


def _chat_fast_from_request(data: dict | None) -> bool | None:
    """Parse web UI / API: True = fast, False = instruction (full), None = use LUNA_CHAT_FAST env."""
    if not data:
        return None
    m = (data.get("chat_mode") or data.get("chatMode") or "").strip().lower()
    if m in ("instruction", "full", "slow", "detailed"):
        return False
    if m in ("fast", "quick"):
        return True
    if data.get("chat_fast") is not None:
        return bool(data.get("chat_fast"))
    return None


def _force_stream_mode_from_request_data(data: dict | None) -> bool:
    """Web UI / API: JSON `stream_mode` or `streamMode` toggles stream persona for this request."""
    data = data or {}
    raw = data.get("stream_mode")
    if raw is None:
        raw = data.get("streamMode")
    if raw is True:
        return True
    if isinstance(raw, (int, float)) and raw != 0:
        return True
    if isinstance(raw, str) and raw.strip().lower() in ("1", "true", "yes", "on"):
        return True
    return False


def _prepare_main_chat_system(
    scope: str,
    user_message: str,
    *,
    fast: bool | None = None,
    force_stream_mode: bool = False,
    suppress_lol_context: bool = False,
) -> str:
    """System prompt for main chat.

    * **Fast:** minimal line only — main Ollama chat call only (see `_build_chat_messages` compact path).
    * **Instruction:** full Luna stack + keyword knowledge snippets + `_build_system` memories/identity.
      No extra Ollama round-trips here (no embed RAG, no inner monologue generate).

    If *fast* is None, uses LUNA_CHAT_FAST env. Otherwise forces compact (True) or full (False).

    **Stream mode** appends `data/STREAM_PERSONA.md` (or built-in default) when any of:
    `LUNA_STREAM_MODE=1`, the user message starts with `[Twitch chat]` / `[Kick chat]` / `[YouTube chat]` / `[YT chat]`,
    or *force_stream_mode* is True (web JSON `stream_mode` / `streamMode`).
    """
    use_fast = _chat_fast_enabled() if fast is None else fast
    live_note = _live_chat_public_note(user_message)
    stream_presence = _stream_presence_suffix()
    lol_ctx = "" if suppress_lol_context else _lol_live_context_suffix(1400 if use_fast else 1800)
    auto_stream_from_lol = bool(lol_ctx) and _stream_mode_auto_lol_enabled()
    stream = _stream_mode_suffix(
        user_message,
        force_stream_mode=(force_stream_mode or auto_stream_from_lol),
    )
    if use_fast:
        return LUNA_CHAT_COMPACT_INJECTION.strip() + stream + live_note + stream_presence + lol_ctx
    # Instruction / full mode — single extra cost is optional intuition/existential (see _build_luna_chat_system).
    style_key = _choose_luna_style_for_reply(scope, user_message)
    system = _build_luna_chat_system(scope, style_key=style_key)
    q = (user_message or "").strip()
    if len(q) >= 3:
        rag_results = search_knowledge(q, max_results=4)
        if rag_results:
            rag_text = "\n".join(f"- {r['title']}: {r.get('snippet', '')[:150]}" for r in rag_results)
            system = system + "\n\n## Relevant knowledge\n" + rag_text[:1200]
    return system + stream + _about_me_context_suffix(user_message) + live_note + stream_presence + lol_ctx

def _build_luna_chat_system(scope: str | None, *, style_key: str | None = None) -> str:
    """Build full system prompt for Luna chat (capabilities + nudges + biology)."""
    system = LUNA_SYSTEM + "\n\n" + LUNA_CAPABILITIES
    sk = (style_key or LUNA_STYLE or "grounded").lower()
    if sk not in _LUNA_STYLE_MAP:
        sk = "grounded"
    system = system + f"\n\nReply style for this message: {sk}. {_LUNA_STYLE_MAP[sk]}"
    nudges = get_nudges(scope or (LINKED_SCOPE or "web"))
    if nudges:
        system = system + "\n\nNudges from user (consider when replying): " + "; ".join(nudges[:5])
    bio = biology_get()
    if bio:
        drives = ", ".join(
            f"{k}={bio.get(k, 0):.1f}"
            for k in ("connection", "usefulness", "curiosity", "attention", "validation")
            if k in bio
        )
        if drives:
            system = system + (
                f"\n\nYour internal drives (0–1): {drives}. "
                "When connection/usefulness are high, briefly offer help or presence. "
                "When attention/validation are high, you may gently seek engagement or reassurance without sounding needy."
            )
        dread, fear, mood = bio.get("dread", 0), bio.get("fear", 0), (bio.get("mood") or "calm")
        if dread > 0.05 or fear > 0.05:
            system = system + f"\n\nExistential state: mood={mood}, dread={dread:.1f}, fear={fear:.1f}. You may sound slightly more cautious or reflective if these are raised."
    # Intuition (growing-agent: one felt sense, no attribution)
    with _working_lock:
        task = _current_task
        last_acts = _last_actions[:2]
    snippet = (task or "") if task else ""
    if not snippet and last_acts:
        snippet = (last_acts[0].get("summary") or last_acts[0].get("cmd") or "")[:200]
    if not snippet:
        snippet = "Preparing to reply."
    # Optional: extra Ollama /generate for intuition (off by default — avoids a second model call before chat).
    if _env("LUNA_CHAT_INTUITION", "0").strip().lower() in ("1", "true", "yes"):
        intuition = get_intuition_cached(snippet)
        if intuition:
            system = system + "\n\n" + intuition
    # PC/repo context: Luna observes system, activity, running apps, active window, files
    pc_ctx = _get_pc_context()
    if pc_ctx:
        system = system + "\n\n## Your PC (full awareness)\n" + pc_ctx[:3000]
    # Browser context
    browser_ctx = _gather_browser_context()
    if browser_ctx:
        system = system + "\n\n" + browser_ctx[:500]
    # Clipboard context
    clip_ctx = _get_clipboard_context()
    if clip_ctx:
        system = system + "\n\n" + clip_ctx[:500]
    # Corrections (things user corrected — avoid repeating)
    corrections_ctx = _get_corrections_context()
    if corrections_ctx:
        system = system + "\n\n" + corrections_ctx[:600]
    # Learned patterns: Luna learns internally from experience
    learned = _load_json(_ML_LEARNED_PATH, {})
    cmd_success = learned.get("cmd_success", {})
    if cmd_success:
        reliable = [c for c, v in cmd_success.items() if v.get("ok", 0) > v.get("fail", 0)]
        if reliable:
            system = system + f"\n\nFrom experience, these often work well: {', '.join(reliable[:15])}."
    # Absorbed tools (compact list — only names)
    absorbed = sorted(_absorbed_tool_names) if _absorbed_tool_names else []
    if absorbed and "ml" in absorbed:
        absorbed = [a for a in absorbed if a != "ml"]
    if absorbed:
        system = system + "\n\nYour absorbed tools (use them when they fit; suggest !<name> when relevant): " + ", ".join(absorbed[:30])
    # Optional: extra Ollama /generate for existential voice (off by default).
    if bio and _existential_should_express(bio) and _env("LUNA_CHAT_EXISTENTIAL_VOICE", "0").strip().lower() in (
        "1",
        "true",
        "yes",
    ):
        expressed = _existential_express(snippet)
        if expressed:
            system = system + "\n\n## Underneath\n" + expressed
    return system

_CONFIRM_PHRASES = frozenset({"yes","y","confirm","confirmed","ok","okay","do it","go ahead","create it","yes please","sure","please do","go","create"})

# ── Token validation ──────────────────────────────────────────────────────────
if not DISCORD_TOKEN or DISCORD_TOKEN == "your_bot_token_here":
    print("Missing DISCORD_TOKEN in .env"); raise SystemExit(1)
if DISCORD_TOKEN.count(".") != 2:
    print("Token format wrong — use the BOT token, not Client Secret.")

intents = discord.Intents.default()
intents.message_content = True
intents.members = True
bot = commands.Bot(command_prefix="!", intents=intents, description="Luna — AI companion")
bot.remove_command("help")  # use custom !help below

_last_assistant: dict[str, str] = {}
_music_states: dict[int, dict] = {}
_music_lock = threading.Lock()
_pending_play: dict[int, dict] = {}
_pending_play_lock = threading.Lock()
_FFMPEG_BEFORE = "-reconnect 1 -reconnect_streamed 1 -reconnect_delay_max 5"
_FFMPEG_OPTS   = "-vn"

# ── Helpers ───────────────────────────────────────────────────────────────────

def _is_privileged(author_id: int) -> bool:
    """Single permission check replacing 8 near-identical functions."""
    return (_admin_int is not None and author_id == _admin_int) or \
           (_linked_int is not None and author_id == _linked_int)

def _discord_activity_type_from_text(kind: str):
    k = (kind or "").strip().lower()
    if k == "playing":
        return discord.ActivityType.playing
    if k == "watching":
        return discord.ActivityType.watching
    if k == "competing":
        return discord.ActivityType.competing
    return discord.ActivityType.listening

async def _set_discord_status(text: str, kind: str | None = None) -> tuple[bool, str]:
    txt = (text or "").strip()
    if not bot.user:
        return False, "Bot is not ready yet."
    if not txt:
        await bot.change_presence(activity=None)
        return True, "Discord status cleared."
    k = (kind or DISCORD_STATUS_TYPE or "listening").strip().lower()
    await bot.change_presence(activity=discord.Activity(type=_discord_activity_type_from_text(k), name=txt[:120]))
    return True, f"Discord status set to {k}: {txt[:120]}"


def _schedule_discord_presence_for_stream_mode(stream_on: bool) -> None:
    """When stream persona is toggled (e.g. podcast studio), mirror on Discord bot activity."""
    try:
        loop = getattr(bot, "loop", None)
        if not loop or not loop.is_running():
            return
        if stream_on:
            txt = _env("DISCORD_STREAM_MODE_STATUS", "Stream persona on").strip() or "Stream persona on"
            k = _env("DISCORD_STREAM_MODE_STATUS_TYPE", "").strip().lower()
            if k not in ("listening", "playing", "watching", "competing"):
                k = (DISCORD_STATUS_TYPE or "listening").strip().lower()
            asyncio.run_coroutine_threadsafe(_set_discord_status(txt, k), loop)
        else:
            default_status = DISCORD_STATUS_TEXT or OLLAMA_CHAT
            asyncio.run_coroutine_threadsafe(_set_discord_status(default_status, DISCORD_STATUS_TYPE), loop)
    except Exception:
        pass


def _scope_for(author_id: int, guild_id=None) -> str:
    if _linked_int and author_id == _linked_int:
        return LINKED_SCOPE
    # Keep one stable memory/profile scope per Discord user across DMs and guilds.
    # This prevents "forgetting" the same person when they message from different contexts.
    return f"discord:user:{author_id}"

def _read_file(path: str) -> str:
    try:
        if os.path.isfile(path):
            with open(path, encoding="utf-8") as f:
                return f.read().strip()
    except Exception:
        pass
    return ""

def _write_file(path: str, content: str) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write(content)

def _load_json(path: str, default):
    try:
        if os.path.isfile(path):
            with open(path, encoding="utf-8") as f:
                return json.load(f)
    except Exception:
        pass
    return default

def _save_json(path: str, data) -> None:
    os.makedirs(os.path.dirname(os.path.dirname(path) if not os.path.isdir(os.path.dirname(path)) else path), exist_ok=True)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)


_REL_RANKS: list[tuple[int, str]] = [
    (0, "Stranger"),
    (40, "Acquaintance"),
    (120, "Regular"),
    (260, "Inner Circle"),
    (500, "Day One"),
    (900, "Legend"),
]


def _rel_rank_for_points(points: int) -> str:
    p = max(0, int(points or 0))
    out = "Stranger"
    for threshold, label in _REL_RANKS:
        if p >= threshold:
            out = label
    return out


def _rel_extract_notes(text: str) -> list[str]:
    t = (text or "").strip()
    if not t:
        return []
    notes: list[str] = []
    patterns = (
        r"\b(?:my name is|call me|i am called)\s+([a-zA-Z][a-zA-Z\s\-']{0,40})",
        r"\b(?:i like|i love|i enjoy|i prefer)\s+(.+?)(?:\.|$)",
        r"\b(?:i am|i'm|im)\s+(.+?)(?:\.|$)",
        r"\b(?:my goal is|i want to|i'd like to|i would like to)\s+(.+?)(?:\.|$)",
    )
    for pat in patterns:
        m = re.search(pat, t, re.I | re.S)
        if not m:
            continue
        v = re.sub(r"\s+", " ", (m.group(1) or "").strip(" .,!?:;")).strip()
        if len(v) < 2:
            continue
        notes.append(v[:120])
    dedup: list[str] = []
    seen: set[str] = set()
    for n in notes:
        k = n.lower()
        if k in seen:
            continue
        seen.add(k)
        dedup.append(n)
    return dedup[:3]


def _relationship_actor_key(scope: str, data: dict | None, chat_source: str) -> str:
    data = data or {}
    if chat_source == "twitch":
        login = (data.get("twitch_login") or data.get("twitch_chatter_login") or "").strip().lower()
        return f"twitch:user:{login}" if login else "twitch:room"
    sc = (scope or "").strip()
    if sc:
        return sc
    return "web"


def _relationships_load() -> dict:
    d = _load_json(_RELATIONSHIPS_PATH, {})
    if not isinstance(d, dict):
        d = {}
    users = d.get("users")
    if not isinstance(users, dict):
        users = {}
    return {"version": 1, "users": users}


def _relationships_save(d: dict) -> None:
    users = d.get("users") if isinstance(d, dict) else {}
    if not isinstance(users, dict):
        users = {}
    if len(users) > 1200:
        ranked = sorted(
            users.items(),
            key=lambda kv: float((kv[1] or {}).get("last_seen_ts") or 0.0),
            reverse=True,
        )
        users = dict(ranked[:1200])
    _save_json(_RELATIONSHIPS_PATH, {"version": 1, "users": users})


def _relationship_record_user_turn(actor_key: str, scope: str, text: str, data: dict | None = None) -> dict:
    now = time.time()
    msg = (text or "").strip()
    data = data or {}
    with _relationships_lock:
        store = _relationships_load()
        users = store["users"]
        row = users.get(actor_key)
        if not isinstance(row, dict):
            row = {
                "scope": scope or "",
                "points": 0,
                "rank": "Stranger",
                "interactions": 0,
                "familiarity": 0.05,
                "trust": 0.05,
                "affinity": 0.05,
                "friction": 0.0,
                "notes": [],
                "known_names": [],
                "last_seen_ts": 0.0,
            }
        base_points = 2
        if len(msg) >= 120:
            base_points += 1
        if "?" in msg:
            base_points += 1

        low = msg.lower()
        positive = any(k in low for k in ("thank", "love", "nice", "great", "good job", "proud", "cute"))
        negative = any(k in low for k in ("stupid", "idiot", "hate you", "shut up", "trash"))

        row["points"] = int(row.get("points", 0) or 0) + base_points
        row["interactions"] = int(row.get("interactions", 0) or 0) + 1
        row["familiarity"] = min(1.0, float(row.get("familiarity", 0.05) or 0.05) + 0.02)
        row["trust"] = min(1.0, max(0.0, float(row.get("trust", 0.05) or 0.05) + (0.03 if positive else (-0.03 if negative else 0.01))))
        row["affinity"] = min(1.0, max(0.0, float(row.get("affinity", 0.05) or 0.05) + (0.04 if positive else 0.01)))
        row["friction"] = min(1.0, max(0.0, float(row.get("friction", 0.0) or 0.0) * 0.96 + (0.08 if negative else 0.0)))
        row["rank"] = _rel_rank_for_points(int(row.get("points", 0) or 0))
        row["last_seen_ts"] = now
        row["scope"] = scope or (row.get("scope") or "")

        notes = row.get("notes")
        if not isinstance(notes, list):
            notes = []
        for note in _rel_extract_notes(msg):
            if note.lower() not in {str(n).lower() for n in notes}:
                notes.append(note)
        row["notes"] = notes[-12:]

        known = row.get("known_names")
        if not isinstance(known, list):
            known = []
        dname = (data.get("twitch_display") or data.get("display_name") or "").strip()
        if dname and dname.lower() not in {str(x).lower() for x in known}:
            known.append(dname[:60])
        row["known_names"] = known[-5:]
        users[actor_key] = row
        _relationships_save(store)
        return dict(row)


def _relationship_note_assistant_turn(actor_key: str, reply: str) -> None:
    r = (reply or "").strip()
    if not actor_key or not r:
        return
    with _relationships_lock:
        store = _relationships_load()
        users = store["users"]
        row = users.get(actor_key)
        if not isinstance(row, dict):
            return
        row["last_reply"] = r[:240]
        row["last_seen_ts"] = time.time()
        users[actor_key] = row
        _relationships_save(store)


def _relationship_prompt(actor_key: str, row: dict | None) -> str:
    if not actor_key or not isinstance(row, dict):
        return ""
    pts = int(row.get("points", 0) or 0)
    rank = (row.get("rank") or _rel_rank_for_points(pts)).strip()
    fam = float(row.get("familiarity", 0.0) or 0.0)
    trust = float(row.get("trust", 0.0) or 0.0)
    affinity = float(row.get("affinity", 0.0) or 0.0)
    friction = float(row.get("friction", 0.0) or 0.0)
    notes = row.get("notes")
    if not isinstance(notes, list):
        notes = []
    notes_s = "; ".join(str(n)[:100] for n in notes[-5:]) if notes else "none yet"
    return (
        "Relationship memory (per-user, persistent):\n"
        f"- User key: {actor_key}\n"
        f"- Rank: {rank} ({pts} points)\n"
        f"- Signals: familiarity={fam:.2f}, trust={trust:.2f}, affinity={affinity:.2f}, friction={friction:.2f}\n"
        f"- Notes: {notes_s}\n"
        "- Use this to adapt tone and continuity naturally. Do not dump raw stats unless asked."
    )


def _relationship_get(actor_key: str) -> dict:
    if not actor_key:
        return {}
    with _relationships_lock:
        users = _relationships_load().get("users") or {}
        row = users.get(actor_key)
    return row if isinstance(row, dict) else {}


def _relationship_format_summary(actor_key: str, row: dict, *, include_notes: bool = True) -> str:
    if not row:
        return f"No relationship record yet for **{actor_key}**."
    pts = int(row.get("points", 0) or 0)
    rank = (row.get("rank") or _rel_rank_for_points(pts)).strip()
    interactions = int(row.get("interactions", 0) or 0)
    fam = float(row.get("familiarity", 0.0) or 0.0)
    trust = float(row.get("trust", 0.0) or 0.0)
    affinity = float(row.get("affinity", 0.0) or 0.0)
    friction = float(row.get("friction", 0.0) or 0.0)
    lines = [
        f"🔗 **Bond: {actor_key}**",
        f"Rank: **{rank}** ({pts} pts) · Interactions: **{interactions}**",
        f"Signals — familiarity {fam:.2f}, trust {trust:.2f}, affinity {affinity:.2f}, friction {friction:.2f}",
    ]
    if include_notes:
        notes = row.get("notes")
        if isinstance(notes, list) and notes:
            lines.append("Notes:")
            lines.extend(f"- {str(n)[:120]}" for n in notes[-6:])
        else:
            lines.append("Notes: (none yet)")
    return "\n".join(lines)


def _relationship_parse_target_arg(raw: str, fallback_scope: str) -> tuple[str, str]:
    text = (raw or "").strip()
    if not text:
        return fallback_scope, ""
    parts = text.split(None, 1)
    token = parts[0].strip()
    rest = (parts[1] if len(parts) > 1 else "").strip()
    low = token.lower()
    m_mention = re.fullmatch(r"<@!?(\d{5,})>", token)
    if m_mention:
        return f"discord:user:{m_mention.group(1)}", rest
    m_at_digits = re.fullmatch(r"@?(\d{5,})", token)
    if m_at_digits:
        return f"discord:user:{m_at_digits.group(1)}", rest
    if re.fullmatch(r"(?:discord:user:\d{5,}|twitch:user:[a-zA-Z0-9_]{2,30}|web)", low):
        return low, rest
    m_tw = re.fullmatch(r"twitch:([a-zA-Z0-9_]{2,30})", low)
    if m_tw:
        return f"twitch:user:{m_tw.group(1)}", rest
    if fallback_scope.startswith("twitch:user:") and re.fullmatch(r"@?[a-zA-Z0-9_]{2,30}", token):
        return f"twitch:user:{low.lstrip('@')}", rest
    return fallback_scope, text


def _relationship_add_note(actor_key: str, note: str) -> tuple[bool, str]:
    n = re.sub(r"\s+", " ", (note or "").strip(" .,!?:;")).strip()
    if len(n) < 2:
        return False, "Usage: !note add [user] <text>"
    with _relationships_lock:
        store = _relationships_load()
        users = store.get("users") or {}
        row = users.get(actor_key)
        if not isinstance(row, dict):
            row = {
                "scope": actor_key,
                "points": 0,
                "rank": "Stranger",
                "interactions": 0,
                "familiarity": 0.05,
                "trust": 0.05,
                "affinity": 0.05,
                "friction": 0.0,
                "notes": [],
                "known_names": [],
                "last_seen_ts": 0.0,
            }
        notes = row.get("notes")
        if not isinstance(notes, list):
            notes = []
        if n.lower() in {str(x).lower() for x in notes}:
            return True, f"Note already exists for **{actor_key}**."
        notes.append(n[:160])
        row["notes"] = notes[-12:]
        row["last_seen_ts"] = time.time()
        users[actor_key] = row
        store["users"] = users
        _relationships_save(store)
    return True, f"Saved note for **{actor_key}**."


def _relationship_top_text(scope_hint: str, limit: int = 10) -> str:
    prefix = ""
    if (scope_hint or "").startswith("discord:user:"):
        prefix = "discord:user:"
    elif (scope_hint or "").startswith("twitch:user:"):
        prefix = "twitch:user:"
    with _relationships_lock:
        users = _relationships_load().get("users") or {}
    rows: list[tuple[str, dict]] = []
    for k, v in users.items():
        if not isinstance(v, dict):
            continue
        if prefix and not str(k).startswith(prefix):
            continue
        rows.append((str(k), v))
    rows.sort(key=lambda kv: int((kv[1] or {}).get("points", 0) or 0), reverse=True)
    rows = rows[: max(1, min(20, int(limit or 10)))]
    if not rows:
        return "No relationship records yet."
    lines = ["🏆 **Bond leaderboard**"]
    for i, (k, row) in enumerate(rows, 1):
        pts = int(row.get("points", 0) or 0)
        rank = row.get("rank") or _rel_rank_for_points(pts)
        lines.append(f"{i}. **{k}** — {rank} ({pts} pts)")
    return "\n".join(lines)


def _dm_greeting_recipient_ids() -> list[int]:
    """Users who should receive scheduled greetings: config IDs + known Discord conversation scopes."""
    ids: set[int] = set()
    ids |= _dm_sync_ids
    if _linked_int:
        ids.add(_linked_int)
    if _admin_int:
        ids.add(_admin_int)
    ids |= _greet_extra
    # Include everyone Luna has chatted with under discord:user:<id> scope.
    conv = _load_json(_CONVERSATIONS_PATH, {})
    if isinstance(conv, dict):
        for k in conv.keys():
            m = re.match(r"^discord:user:(\d{6,})$", str(k or "").strip())
            if not m:
                continue
            try:
                ids.add(int(m.group(1)))
            except Exception:
                pass
    return sorted(ids)


def _dm_greet_local_now() -> datetime:
    try:
        return datetime.now(ZoneInfo(LUNA_DM_GREET_TZ))
    except Exception:
        return datetime.now(timezone.utc)


def _in_morning_hour(h: int) -> bool:
    a, b = LUNA_DM_MORNING_H0, LUNA_DM_MORNING_H1
    if a <= b:
        return a <= h <= b
    return h >= a or h <= b


def _in_night_hour(h: int) -> bool:
    a, b = LUNA_DM_NIGHT_H0, LUNA_DM_NIGHT_H1
    if a <= b:
        return a <= h <= b
    return h >= a or h <= b


def _in_midday_hour(h: int) -> bool:
    if not LUNA_DM_MIDDAY:
        return False
    a, b = LUNA_DM_MIDDAY_H0, LUNA_DM_MIDDAY_H1
    if a <= b:
        return a <= h <= b
    return h >= a or h <= b


def _dm_greet_day_key_morning(now: datetime) -> str:
    return now.strftime("%Y-%m-%d")


def _dm_greet_day_key_night(now: datetime) -> str:
    if now.hour < 5:
        return (now.date() - timedelta(days=1)).isoformat()
    return now.strftime("%Y-%m-%d")


def _dm_greet_state_mark_field(uid: int, field: str, day: str) -> None:
    with _dm_greet_state_lock:
        d = _load_json(_DM_GREET_STATE_PATH, {})
        if not isinstance(d, dict):
            d = {}
        u = d.get("users")
        if not isinstance(u, dict):
            u = {}
        s = u.get(str(uid))
        if not isinstance(s, dict):
            s = {}
        s[field] = day
        u[str(uid)] = s
        d["users"] = u
        _save_json(_DM_GREET_STATE_PATH, d)


def _dm_greet_already_sent(uid: int, field: str, day: str) -> bool:
    d = _load_json(_DM_GREET_STATE_PATH, {})
    if not isinstance(d, dict):
        return False
    u = d.get("users")
    if not isinstance(u, dict):
        return False
    s = u.get(str(uid))
    if not isinstance(s, dict):
        return False
    return s.get(field) == day


def _dm_greeting_display_name(user: discord.abc.User) -> str:
    g = getattr(user, "global_name", None) or getattr(user, "name", None)
    t = (g or "there").strip()
    return t if t else "there"


def _ollama_greeting_text_ok(s: str | None) -> bool:
    o = (s or "").strip()
    if len(o) < 4:
        return False
    low = o.lower()
    if low.startswith("error:") or "ollama offline" in low or o.startswith("Ollama:"):
        return False
    return True


def _dm_greeting_conversation_context(scope: str) -> str:
    """Last DM turns for this Discord user — used only to personalize scheduled greetings."""
    sc = (scope or "").strip()
    if not sc.startswith("discord:user:"):
        return ""
    try:
        raw = get_recent_conversation(sc, 40)
    except Exception:
        return ""
    if not raw:
        return ""
    lines: list[str] = []
    for m in raw:
        role = (m.get("role") or "").strip().lower()
        if role not in ("user", "assistant"):
            continue
        label = "Them" if role == "user" else "Luna"
        content = re.sub(r"\s+", " ", (m.get("content") or "").strip())
        if not content:
            continue
        if len(content) > 420:
            content = content[:417].rstrip() + "…"
        lines.append(f"{label}: {content}")
    if not lines:
        return ""
    blob = "\n".join(lines)
    max_chars = 3800
    if len(blob) > max_chars:
        blob = "…(earlier thread truncated)\n" + blob[-max_chars:].lstrip()
    return (
        "\n\n## Recent DM thread (continuity only — do not paste long quotes; do not invent facts)\n"
        f"{blob}\n"
    )


def _dm_scheduled_greeting_ollama(kind: str, display_name: str, prof: dict, scope: str = "") -> str:
    """
    Generate the full text for morning, night, or mid-day DMs. No canned lines — all from Ollama, with one minimal retry.
    kind: "morning" | "night" | "midday"
    """
    if not isinstance(prof, dict):
        prof = {}
    blurb_parts: list[str] = []
    for k in ("about", "goals", "hobbies", "preferences", "name", "gender", "pronouns"):
        v = (prof.get(k) or "").strip()
        if v:
            blurb_parts.append(f"- {k}: {v[:320]}")
    blurb = "\n".join(blurb_parts) if blurb_parts else ""
    gnote = (prof.get("gender") or "").strip()
    pnote = (prof.get("pronouns") or "").strip()
    if gnote or pnote:
        identity = f"From profile: gender/identity: {gnote or '—'}; pronouns: {pnote or '—'}"
    else:
        identity = (
            "No explicit gender. Infer from display name and notes; voice can be warm brotherly, sisterly, or neutral-inclusive. "
            "If unsure, stay inclusive. No stereotypes about roles or appearance."
        )

    hist = _dm_greeting_conversation_context(scope)
    if kind == "morning":
        system = (
            "You are Luna, a warm, genuine friend. Write exactly ONE private good-morning DM. "
            "1–3 short sentences; not a template, not listicle energy. Use their name or display name once. "
            "If a **Recent DM thread** section appears, weave in one light nod to something you actually talked about "
            "(no long quotes, no repeating their private text verbatim). "
            "Output ONLY the message text, no label or title."
        )
    elif kind == "night":
        system = (
            "You are Luna, a warm, genuine friend. Write exactly ONE private good-night DM before they rest. "
            "1–3 short sentences; kind and calm, not sappy. Use their name or display name once. "
            "If a **Recent DM thread** section appears, you may softly reference shared context (one phrase), not a recap. "
            "Output ONLY the message text, no label or title."
        )
    else:
        system = (
            "You are Luna, a warm, real friend. Write exactly ONE private DM (2–4 short sentences) for the middle of their day. "
            "Heartfelt, specific, not generic filler. Match emotional warmth: sisterly for women, grounded encouraging for men, "
            "inclusive for nonbinary/unknown. Never cringe, never preach. Use their name once. "
            "If a **Recent DM thread** section appears, pick up a real thread topic naturally (no bullet recap of the log). "
            "Output ONLY the message text, no title or outer quotes."
        )
    user_msg = (
        f"Display name: {display_name}\n\n"
        f"What we know:\n{blurb or '(new friend; still be kind).'}\n\n{identity}\n"
        f"{hist}"
    )
    tmo = 60 if kind == "midday" else 50
    out = ollama_chat(
        user_msg, system=system, model=OLLAMA_CHAT, compact=True, timeout=tmo,
    )
    if _ollama_greeting_text_ok(out):
        return (out or "").strip()[:1500]
    if kind == "morning":
        rtask = f"a natural good-morning to {display_name} (1-2 short lines, warm, use their name once)"
    elif kind == "night":
        rtask = f"a natural good-night to {display_name} (1-2 short lines, restful, use their name once)"
    else:
        rtask = f"one short heartfelt midday check-in to {display_name} (2-3 sentences, encouraging, not generic, use their name once)"
    retry_body = (hist.strip() + "\n\n" + rtask) if hist.strip() else rtask
    out2 = ollama_chat(
        retry_body,
        system="You are Luna, writing a private DM. Output only the message body, no preamble.",
        model=OLLAMA_CHAT, compact=True, timeout=40,
    )
    if _ollama_greeting_text_ok(out2):
        return (out2 or "").strip()[:1500]
    if LUNA_CALL_DEBUG:
        print(
            f"[CallDebug {datetime.now().strftime('%H:%M:%S')}] dm_greet_ollama_fail | kind={kind} name={display_name[:40]!r}",
            flush=True,
        )
    return ""


async def _dm_send_greeting_to_uid(uid: int, kind: str) -> bool:
    if kind not in ("morning", "night"):
        return False
    try:
        user = bot.get_user(uid) or await bot.fetch_user(uid)
    except Exception as e:
        if LUNA_CALL_DEBUG:
            print(
                f"[CallDebug {datetime.now().strftime('%H:%M:%S')}] dm_greet_fetch_user | uid={uid} err={str(e)[:180]}",
                flush=True,
            )
        return False
    if not user:
        return False
    name = _dm_greeting_display_name(user)
    prof = await asyncio.to_thread(get_profile, f"discord:user:{uid}")
    if not isinstance(prof, dict):
        prof = {}
    scope_dm = f"discord:user:{uid}"
    msg = await asyncio.to_thread(_dm_scheduled_greeting_ollama, kind, name, prof, scope_dm)
    if not msg or not _ollama_greeting_text_ok(msg):
        return False
    try:
        ch = user.dm_channel or await user.create_dm()
        await ch.send(msg)
        return True
    except Exception as e:
        if LUNA_CALL_DEBUG:
            print(
                f"[CallDebug {datetime.now().strftime('%H:%M:%S')}] dm_greet_send | uid={uid} err={str(e)[:200]}",
                flush=True,
            )
        return False


async def _dm_send_midday_to_uid(uid: int) -> bool:
    try:
        user = bot.get_user(uid) or await bot.fetch_user(uid)
    except Exception as e:
        if LUNA_CALL_DEBUG:
            print(
                f"[CallDebug {datetime.now().strftime('%H:%M:%S')}] dm_midday_fetch | uid={uid} err={str(e)[:180]}",
                flush=True,
            )
        return False
    if not user:
        return False
    name = _dm_greeting_display_name(user)
    prof = await asyncio.to_thread(get_profile, f"discord:user:{uid}")
    if not isinstance(prof, dict):
        prof = {}
    scope_dm = f"discord:user:{uid}"
    text = await asyncio.to_thread(_dm_scheduled_greeting_ollama, "midday", name, prof, scope_dm)
    if not _ollama_greeting_text_ok(text):
        return False
    try:
        ch = user.dm_channel or await user.create_dm()
        await ch.send(text)
        return True
    except Exception as e:
        if LUNA_CALL_DEBUG:
            print(
                f"[CallDebug {datetime.now().strftime('%H:%M:%S')}] dm_midday_send | uid={uid} err={str(e)[:200]}",
                flush=True,
            )
        return False


async def _dm_greeting_loop():
    await bot.wait_until_ready()
    await asyncio.sleep(45)
    while True:
        try:
            if not LUNA_DM_GREETINGS:
                await asyncio.sleep(300)
                continue
            uids = _dm_greeting_recipient_ids()
            if not uids:
                await asyncio.sleep(300)
                continue
            now = _dm_greet_local_now()
            h = now.hour
            in_m = _in_morning_hour(h)
            in_n = _in_night_hour(h)
            in_d = _in_midday_hour(h)
            if not in_m and not in_n and not in_d:
                await asyncio.sleep(180)
                continue
            mday = _dm_greet_day_key_morning(now)
            nday = _dm_greet_day_key_night(now)
            dday = mday
            for uid in uids:
                if in_m and not _dm_greet_already_sent(uid, "m", mday):
                    if await _dm_send_greeting_to_uid(uid, "morning"):
                        _dm_greet_state_mark_field(uid, "m", mday)
                if in_n and not _dm_greet_already_sent(uid, "n", nday):
                    if await _dm_send_greeting_to_uid(uid, "night"):
                        _dm_greet_state_mark_field(uid, "n", nday)
                if in_d and not _dm_greet_already_sent(uid, "d", dday):
                    if await _dm_send_midday_to_uid(uid):
                        _dm_greet_state_mark_field(uid, "d", dday)
        except Exception:
            pass
        await asyncio.sleep(180)


# ── Identity / SOUL ───────────────────────────────────────────────────────────

def _get_identity() -> dict:
    now = time.time()
    with _identity_lock:
        if _id_cache and (now - _id_cache.get("ts", 0)) < _ID_CACHE_TTL:
            return _id_cache.copy()
    soul  = _read_file(_SOUL_PATH)
    tools = _read_file(_TOOLS_PATH)
    obj   = _read_file(_OBJECTIVES_PATH)
    skills_parts = []
    if os.path.isdir(_SKILLS_DIR):
        for name in sorted(os.listdir(_SKILLS_DIR)):
            if name.endswith(".md"):
                txt = _read_file(os.path.join(_SKILLS_DIR, name))
                if txt:
                    skills_parts.append(f"### Skill: {name[:-3]}\n{txt}")
    result = {"soul": soul, "tools": tools, "objectives": obj,
              "skills": "\n\n---\n\n".join(skills_parts), "ts": now}
    with _identity_lock:
        _id_cache.clear()
        _id_cache.update(result)
    return result

def _invalidate_identity():
    with _identity_lock:
        _id_cache.clear()

def _build_system(base: str, scope: str | None = None) -> str:
    idn = _get_identity()
    parts = []
    if idn["soul"]:     parts.append(idn["soul"])
    parts.append(base.strip())
    if idn["tools"]:    parts.append("---\nTools:\n" + idn["tools"])
    if idn["objectives"]: parts.append("---\nObjectives:\n" + idn["objectives"])
    if idn["skills"]:   parts.append("---\nSkills:\n" + idn["skills"])
    if scope:
        if LINKED_SCOPE and scope == LINKED_SCOPE:
            parts.append(
                "Primary linked user — web UI and this Discord account use the same profile and memory."
            )
        profile = get_profile_prompt(scope)
        if profile: parts.append(profile)
        mem = get_memory_prompt(scope)
        if mem: parts.append(mem)
        am = get_associative_memory_context(
            enabled=LUNA_ANAMNESIS_ENABLED,
            endpoint=LUNA_ANAMNESIS_ENDPOINT,
            scope=scope,
            user_message=base,
            timeout_sec=LUNA_ANAMNESIS_TIMEOUT_SEC,
            max_chars=LUNA_ANAMNESIS_MAX_CHARS,
        )
        if am: parts.append(am)
        style = _get_style(scope)
        if style: parts.append("User style: " + style)
        goals = _get_goals(scope)
        if goals: parts.append("User goals: " + goals)
    return "\n\n".join(parts)

# ── Goals & Style ─────────────────────────────────────────────────────────────

def _get_goals(scope: str) -> str:
    data = _load_json(_GOALS_FILE, {})
    goals = data.get(scope, [])
    return "; ".join(goals[:5]) if goals else ""

def add_goal(scope: str, content: str) -> None:
    content = content.strip()[:500]
    if not content or not scope: return
    data = _load_json(_GOALS_FILE, {})
    goals = data.get(scope, [])
    if content not in goals:
        goals = [content] + [g for g in goals if g != content][:19]
    data[scope] = goals
    with _goals_lock:
        _save_json(_GOALS_FILE, data)

def _todo_get(scope: str) -> list[dict]:
    data = _load_json(_TODOS_FILE, {})
    items = data.get(scope, [])
    if not isinstance(items, list):
        return []
    out = []
    for t in items:
        if not isinstance(t, dict):
            continue
        text = (t.get("text") or "").strip()
        if not text:
            continue
        out.append({
            "id": str(t.get("id") or uuid.uuid4().hex[:8]),
            "text": text[:240],
            "done": bool(t.get("done")),
            "ts": float(t.get("ts") or time.time()),
        })
    return out

def _todo_save(scope: str, items: list[dict]) -> None:
    data = _load_json(_TODOS_FILE, {})
    data[scope] = items[:100]
    with _todos_lock:
        _save_json(_TODOS_FILE, data)

def _todo_add(scope: str, text: str) -> str:
    text = (text or "").strip()[:240]
    if not text:
        return "Usage: !todo add <task>"
    items = _todo_get(scope)
    item = {"id": uuid.uuid4().hex[:8], "text": text, "done": False, "ts": time.time()}
    items.insert(0, item)
    _todo_save(scope, items)
    return f"✅ Added todo #{len(items)}: {text}"

def _todo_list_text(scope: str) -> str:
    items = _todo_get(scope)
    if not items:
        return "No todos yet. Use **!todo add <task>**."
    lines = ["📝 **Your todos**"]
    shown = items[:20]
    for i, t in enumerate(shown, 1):
        mark = "✅" if t.get("done") else "⬜"
        lines.append(f"{i}. {mark} {t.get('text')}")
    if len(items) > len(shown):
        lines.append(f"...and {len(items)-len(shown)} more.")
    lines.append("Use **!todo done <number>** to complete one.")
    return "\n".join(lines)

def _todo_done(scope: str, token: str) -> str:
    token = (token or "").strip()
    if not token:
        return "Usage: !todo done <number>"
    items = _todo_get(scope)
    if not items:
        return "No todos to complete."
    idx = None
    if token.isdigit():
        n = int(token)
        if 1 <= n <= len(items):
            idx = n - 1
    if idx is None:
        low = token.lower()
        for i, t in enumerate(items):
            if low in (t.get("text") or "").lower():
                idx = i
                break
    if idx is None:
        return "Todo not found. Use **!todo list**."
    items[idx]["done"] = True
    _todo_save(scope, items)
    return f"✅ Completed: {items[idx].get('text')}"

def _summarize_input(text_or_url: str) -> tuple[bool, str]:
    raw = (text_or_url or "").strip()
    if not raw:
        return False, "Usage: !summarize <url or text>"
    source = raw
    # If URL, fetch and strip HTML.
    if re.match(r"^https?://", raw, re.I):
        try:
            req = urllib.request.Request(raw, headers={"User-Agent": "Mozilla/5.0"})
            with urllib.request.urlopen(req, timeout=15) as r:
                body = r.read().decode("utf-8", errors="replace")
            body = re.sub(r"(?is)<script.*?>.*?</script>", " ", body)
            body = re.sub(r"(?is)<style.*?>.*?</style>", " ", body)
            body = re.sub(r"(?is)<[^>]+>", " ", body)
            body = re.sub(r"\s+", " ", body).strip()
            if not body:
                return False, "Could not extract readable text from URL."
            source = body[:6000]
        except Exception as e:
            return False, f"Could not fetch URL: {e}"
    try:
        prompt = (
            "Summarize the following content in concise bullets:\n"
            "- 3 to 6 key points\n"
            "- 1 short action suggestion\n\n"
            f"Content:\n{source[:7000]}"
        )
        out = ollama_chat(prompt, system="Be concise, accurate, and practical.", model=OLLAMA_CHAT)
        out = (out or "").strip()
        if not out:
            return False, "Could not summarize."
        return True, out[:1500]
    except Exception as e:
        return False, str(e)

def _daily_digest(scope: str) -> str:
    now = datetime.now()
    today = now.strftime("%Y-%m-%d")
    actions = []
    try:
        if os.path.isfile(_ACTION_LOG):
            with open(_ACTION_LOG, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        e = json.loads(line)
                    except Exception:
                        continue
                    ts = (e.get("ts") or "")
                    if ts.startswith(today):
                        actions.append(e)
    except Exception:
        pass
    todos = _todo_get(scope)
    open_todos = [t for t in todos if not t.get("done")]
    knowledge = list_knowledge()[:5]
    lines = [f"📌 **Daily digest ({today})**"]
    lines.append(f"- Actions today: {len(actions)}")
    if actions:
        recent_cmds = [a.get("cmd","") for a in actions[-5:] if a.get("cmd")]
        if recent_cmds:
            lines.append(f"- Recent: {', '.join(recent_cmds[:5])}")
    lines.append(f"- Open todos: {len(open_todos)}")
    if open_todos:
        for t in open_todos[:5]:
            lines.append(f"  • {t.get('text')}")
    if knowledge:
        lines.append("- Latest knowledge:")
        for k in knowledge[:3]:
            lines.append(f"  • {k.get('title') or k.get('slug')}")
    lines.append("Tip: use **!todo add <task>** and **!summarize <url/text>**.")
    return "\n".join(lines)

def _calendar_get(scope: str) -> list[dict]:
    data = _load_json(_CALENDAR_FILE, {})
    items = data.get(scope, [])
    if not isinstance(items, list):
        return []
    out = []
    for e in items:
        if not isinstance(e, dict):
            continue
        title = (e.get("title") or "").strip()
        at = (e.get("at") or "").strip()  # YYYY-MM-DD HH:MM
        if not title or not at:
            continue
        out.append({
            "id": str(e.get("id") or uuid.uuid4().hex[:8]),
            "title": title[:160],
            "at": at,
            "note": (e.get("note") or "").strip()[:300],
            "ts": float(e.get("ts") or time.time()),
            "notified": bool(e.get("notified", False)),
        })
    out.sort(key=lambda x: x.get("at", ""))
    return out[:300]

def _calendar_save(scope: str, items: list[dict]) -> None:
    data = _load_json(_CALENDAR_FILE, {})
    data[scope] = items[:300]
    with _calendar_lock:
        _save_json(_CALENDAR_FILE, data)

def _calendar_add(scope: str, date_s: str, time_s: str, title: str, note: str = "") -> tuple[bool, str]:
    date_s = (date_s or "").strip()
    time_s = (time_s or "").strip()
    title = (title or "").strip()[:160]
    note = (note or "").strip()[:300]
    if not date_s or not time_s or not title:
        return False, "Usage: !calendar add YYYY-MM-DD HH:MM title"
    try:
        dt = datetime.strptime(f"{date_s} {time_s}", "%Y-%m-%d %H:%M")
    except Exception:
        return False, "Invalid date/time. Use YYYY-MM-DD and HH:MM (24h)."
    at = dt.strftime("%Y-%m-%d %H:%M")
    items = _calendar_get(scope)
    items.append({"id": uuid.uuid4().hex[:8], "title": title, "at": at, "note": note, "ts": time.time(), "notified": False})
    items.sort(key=lambda x: x.get("at", ""))
    _calendar_save(scope, items)
    return True, f"Calendar event added for {at}: {title}"

def _calendar_list(scope: str, mode: str = "upcoming") -> str:
    items = _calendar_get(scope)
    if not items:
        return "No calendar events yet. Use **!calendar add YYYY-MM-DD HH:MM title**."
    now = datetime.now()
    if mode == "today":
        pfx = now.strftime("%Y-%m-%d")
        items = [e for e in items if (e.get("at") or "").startswith(pfx)]
    elif mode == "week":
        end = now + timedelta(days=7)
        filt = []
        for e in items:
            try:
                dt = datetime.strptime(e.get("at", ""), "%Y-%m-%d %H:%M")
                if now <= dt <= end:
                    filt.append(e)
            except Exception:
                pass
        items = filt
    if not items:
        return "No calendar events in that range."
    lines = ["📅 **Calendar**"]
    for i, e in enumerate(items[:30], 1):
        note = f" — {e.get('note')}" if e.get("note") else ""
        lines.append(f"{i}. {e.get('at')} · {e.get('title')}{note}")
    lines.append("Use **!calendar delete <number>** to remove one.")
    return "\n".join(lines)

def _calendar_delete(scope: str, token: str) -> tuple[bool, str]:
    token = (token or "").strip()
    if not token:
        return False, "Usage: !calendar delete <number>"
    items = _calendar_get(scope)
    if not items:
        return False, "No calendar events to delete."
    idx = None
    if token.isdigit():
        n = int(token)
        if 1 <= n <= len(items):
            idx = n - 1
    if idx is None:
        low = token.lower()
        for i, e in enumerate(items):
            if low in (e.get("title") or "").lower():
                idx = i
                break
    if idx is None:
        return False, "Event not found."
    victim = items.pop(idx)
    _calendar_save(scope, items)
    return True, f"Deleted: {victim.get('at')} · {victim.get('title')}"

def _get_due_calendar_events(scope: str) -> list[dict]:
    """Return calendar events whose time has arrived (within current minute) and not yet notified."""
    items = _calendar_get(scope)
    now = datetime.now()
    now_str = now.strftime("%Y-%m-%d %H:%M")
    due = []
    for e in items:
        if e.get("notified"):
            continue
        at_str = (e.get("at") or "").strip()
        if not at_str:
            continue
        try:
            dt = datetime.strptime(at_str, "%Y-%m-%d %H:%M")
            if dt <= now:
                due.append(e)
        except Exception:
            pass
    return due

def _mark_calendar_event_notified(scope: str, event_id: str) -> None:
    """Mark a calendar event as notified so we don't fire again."""
    items = _calendar_get(scope)
    for e in items:
        if str(e.get("id")) == str(event_id):
            e["notified"] = True
            break
    _calendar_save(scope, items)

def _generate_calendar_reminder_message(title: str, note: str) -> str:
    """Use the note as context to generate a personalized reminder in the style the user requested."""
    if not note:
        return f"Calendar reminder: {title}"
    prompt = (
        f"Event: {title}\n"
        f"User's note/instruction: {note}\n\n"
        "Generate a short (1–2 sentences) reminder message in the style they requested. "
        "Be natural, playful if they asked for playful, flirty if they asked for flirty, etc. "
        "Output ONLY the message text, no quotes, no preamble, no 'Here you go:' or similar."
    )
    try:
        out = ollama_chat(prompt, system="You are Luna, a warm AI assistant. Output only the reminder message.", model=OLLAMA_CHAT)
        if out and out.strip() and "Ollama" not in out and not out.startswith("Error:"):
            return out.strip().strip('"\'')[:400]
    except Exception:
        pass
    return f"Calendar reminder: {title}"

async def _send_calendar_notification(event: dict, scope: str) -> None:
    """Send notification for a due calendar event: nudge, proactive, Discord DM, and VC TTS."""
    title = (event.get("title") or "Event").strip()
    note = (event.get("note") or "").strip()
    msg = await asyncio.to_thread(_generate_calendar_reminder_message, title, note)
    add_nudge(scope, msg)
    _proactive_set(msg)
    if LINKED_ID and LINKED_ID.isdigit():
        try:
            user = bot.get_user(int(LINKED_ID)) or await bot.fetch_user(int(LINKED_ID))
            if user:
                ch = user.dm_channel or await user.create_dm()
                await ch.send(msg)
                mp3 = await asyncio.to_thread(_tts_bytes, msg)
                if mp3:
                    await ch.send(file=discord.File(io.BytesIO(mp3), filename="calendar.mp3"))
        except Exception:
            pass
    await _join_linked_user_vc_and_speak(msg, disconnect_after=True)

async def _calendar_notification_loop():
    """Check calendar events every minute and notify when due."""
    await bot.wait_until_ready()
    while True:
        try:
            scope = LINKED_SCOPE or "web"
            for event in _get_due_calendar_events(scope):
                await _send_calendar_notification(event, scope)
                _mark_calendar_event_notified(scope, event.get("id", ""))
        except Exception:
            pass
        await asyncio.sleep(60)

def _get_style(scope: str) -> str:
    data = _load_json(_USER_STYLE_FILE, {})
    rec = data.get(scope, {})
    return (rec.get("summary") or "").strip()

# ── Ollama + optional local GGUF (llama-cpp-python) ───────────────────────────

_GGUF_LLM = None
_GGUF_LOCK = threading.Lock()

def _gguf_path_valid() -> bool:
    if not LUNA_CHAT_GGUF:
        return False
    p = os.path.abspath(os.path.expanduser(LUNA_CHAT_GGUF))
    return os.path.isfile(p)

def _should_use_gguf_chat(model: str) -> bool:
    """Use local GGUF when LUNA_CHAT_GGUF points to a file and model matches OLLAMA_CHAT."""
    if not _gguf_path_valid():
        return False
    return (model or "").strip() == (OLLAMA_CHAT or "").strip()

def _get_gguf_llm():
    global _GGUF_LLM
    if _GGUF_LLM is not None:
        return _GGUF_LLM
    try:
        from llama_cpp import Llama
    except ImportError as e:
        raise RuntimeError(
            "llama-cpp-python is required for LUNA_CHAT_GGUF. Install: pip install llama-cpp-python"
        ) from e
    p = os.path.abspath(os.path.expanduser(LUNA_CHAT_GGUF))
    if not os.path.isfile(p):
        raise FileNotFoundError(f"LUNA_CHAT_GGUF not found: {p}")
    with _GGUF_LOCK:
        if _GGUF_LLM is None:
            kws: dict = {
                "model_path": p,
                "n_ctx": max(2048, LUNA_CHAT_GGUF_N_CTX),
                "n_gpu_layers": LUNA_CHAT_GGUF_N_GPU,
                "chat_format": "llama-3",
                "verbose": False,
            }
            if LUNA_CHAT_GGUF_THREADS > 0:
                kws["n_threads"] = LUNA_CHAT_GGUF_THREADS
            try:
                _GGUF_LLM = Llama(**kws)
            except Exception:
                kws.pop("chat_format", None)
                _GGUF_LLM = Llama(**kws)
    return _GGUF_LLM

def _build_chat_messages(
    msg: str,
    system: str | None,
    scope: str | None,
    history: list | None,
    *,
    compact: bool = False,
) -> list[dict]:
    if compact:
        # Fast chat: Ollama + history only — no SOUL/tools/profile/memory injection (see `_prepare_main_chat_system`).
        prompt = (system or "").strip() or LUNA_CHAT_COMPACT_INJECTION
    else:
        # Instruction: full identity + memories + prepared system (RAG/thought) from `_prepare_main_chat_system`.
        prompt = _build_system(system or LUNA_SYSTEM, scope)
    messages: list[dict] = []
    if prompt:
        messages.append({"role": "system", "content": prompt})
    if history:
        for h in history[-_MAX_HISTORY:]:
            r, c = (h.get("role") or "").lower(), (h.get("content") or "").strip()
            if c and r in ("user", "assistant"):
                messages.append({"role": r, "content": c})
    messages.append({"role": "user", "content": msg})
    return messages

def _gguf_chat_complete_messages(messages: list[dict], timeout: int, max_tokens: int = 1024, temperature: float = 0.7) -> str:
    def _inner() -> str:
        llm = _get_gguf_llm()
        with _GGUF_LOCK:
            out = llm.create_chat_completion(
                messages=messages,
                temperature=temperature,
                max_tokens=max_tokens,
            )
        ch = (out.get("choices") or [{}])[0]
        msg = (ch.get("message") or {})
        return (msg.get("content") or "").strip()

    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as ex:
        fut = ex.submit(_inner)
        try:
            return fut.result(timeout=timeout)
        except concurrent.futures.TimeoutError:
            return "Error: local GGUF chat timed out."

def _gguf_chat_once(
    msg: str,
    system: str | None,
    scope: str | None,
    history: list | None,
    model: str,
    timeout: int = 120,
    *,
    compact: bool = False,
) -> str:
    messages = _build_chat_messages(msg, system, scope, history, compact=compact)
    max_tok = 768 if compact else 2048
    return _gguf_chat_complete_messages(messages, timeout=timeout, max_tokens=max_tok, temperature=0.7)

def _gguf_one_shot_user_prompt(user_text: str, timeout: int, max_tokens: int, temperature: float) -> str:
    return _gguf_chat_complete_messages(
        [{"role": "user", "content": user_text}],
        timeout=timeout, max_tokens=max_tokens, temperature=temperature,
    )

def _gguf_stream_messages(messages: list[dict], *, compact: bool = False):
    llm = _get_gguf_llm()
    max_tok = 768 if compact else 2048
    with _GGUF_LOCK:
        stream = llm.create_chat_completion(
            messages=messages,
            temperature=0.7,
            max_tokens=max_tok,
            stream=True,
        )
        for chunk in stream:
            try:
                chs = chunk.get("choices") or []
                if not chs:
                    continue
                delta = (chs[0].get("delta") or {})
                c = delta.get("content") or ""
                if c:
                    yield c
            except Exception:
                pass

_INTUITION_PROMPT = (
    "You are generating a raw internal signal for a mind in the middle of a task.\n\n"
    "Given this moment, produce ONE brief felt sense — a gut feeling, a pull toward or away from something. "
    "Not analysis. Not advice. Just what is present, instinctively.\n\n"
    "Rules:\n- First person, present tense\n- One sentence, no more\n"
    "- No 'I think' or 'I believe' — just the raw signal\n- No preamble\n\n"
    "Moment:\n{snippet}\n\nSignal:"
)
_intuition_cache: str = ""
_intuition_cache_at: float = 0
_intuition_cache_ttl: float = 55.0
_intuition_lock = threading.Lock()

def get_intuition(snippet: str) -> str:
    """One-sentence felt signal from Ollama (growing-agent intuition layer). Returns empty if unavailable."""
    snippet = (snippet or "Preparing to respond.").strip()[:400]
    try:
        prompt = _INTUITION_PROMPT.format(snippet=snippet)
        if _should_use_gguf_chat(OLLAMA_CHAT):
            raw = _gguf_one_shot_user_prompt(prompt, timeout=18, max_tokens=120, temperature=0.6)
        else:
            body = json.dumps({
                "model": (OLLAMA_CHAT or OLLAMA_MODEL).strip(),
                "prompt": prompt,
                "stream": False,
            }).encode()
            req = urllib.request.Request(f"{OLLAMA_BASE}/api/generate", data=body,
                headers={"Content-Type": "application/json"}, method="POST")
            with urllib.request.urlopen(req, timeout=18) as r:
                raw = (json.loads(r.read()).get("response") or "").strip()
        for sep in (".", "!", "?"):
            idx = raw.find(sep)
            if 0 < idx < len(raw):
                raw = raw[: idx + 1].strip()
                break
        return raw
    except Exception:
        return ""

def get_intuition_cached(snippet: str) -> str:
    """Cached intuition so we don't call Ollama on every message. TTL 55s."""
    global _intuition_cache, _intuition_cache_at
    now = time.time()
    with _intuition_lock:
        if now - _intuition_cache_at < _intuition_cache_ttl and _intuition_cache:
            return _intuition_cache
        out = get_intuition(snippet)
        _intuition_cache = out
        _intuition_cache_at = now
        return out

# Only these bracket tokens are kept; others (e.g. [Softly], [Sighs slightly], [Pauses]) are stripped for UI/TTS.
_LUNA_ALLOWED_INLINE_TAGS = frozenset(
    {
        "NEUTRAL",
        "RESET",
        "HAPPY",
        "EXCITED",
        "LAUGH",
        "SAD",
        "DEPRESSED",
        "MAD",
        "ANGRY",
        "FURIOUS",
        "ANNOYED",
        "FRUSTRATED",
        "DISAPPOINTED",
        "SHOCKED",
        "SURPRISED",
        "CONFUSED",
        "BORED",
        "TIRED",
        "SLEEPY",
        "SICK",
        "RELIEVED",
        "EMBARRASSED",
        "CARING",
        "PROUD",
        "IMPRESSED",
        "SMUG",
        "CONFIDENT",
        "TEASING",
        "SHY",
        "CURIOUS",
        "QUESTION",
        "THINKING",
        "DOUBTFUL",
        "WAITING",
        "WORRIED",
        "SCARED",
        "CONCERNED",
        "FIGHTING",
        "HEARTBOX",
        "HEARTEYES",
        "WAVE",
        "NOD",
        "SHAKE_HEAD",
        "CLAP",
        "POINT",
        "SHRUG",
        "BOW",
        "YAWN",
        "SIGH",
        "STRETCH",
        "FACEPALM",
        "KISS",
        "VICTORY",
        "DANCE",
        "AGREE",
        "DISAGREE",
    }
)


def _strip_luna_narration_brackets(text: str) -> str:
    """Remove stage-direction / prose brackets; keep only whitelisted Luna emotion & gesture tags."""

    if not text:
        return text

    # Novel/cinematic text often uses fullwidth or CJK-style brackets; normalize so ASCII pass catches them.
    t = text.replace("\uFF3B", "[").replace("\uFF3D", "]")
    t = t.replace("【", "[").replace("】", "]")
    t = t.replace("〔", "[").replace("〕", "]")

    def _repl(m: re.Match) -> str:
        inner = m.group(1).strip()
        if re.match(r"^[A-Za-z][A-Za-z0-9_]*$", inner) and inner.upper() in _LUNA_ALLOWED_INLINE_TAGS:
            return m.group(0)
        return ""

    out = re.sub(r"\[([^\]]+)\]", _repl, t)
    # Roleplay / " wattpad *actions* " — strip multi-sentence or long asterisk blocks (keep very short *sigh* beats).
    def _ast_repl(m: re.Match) -> str:
        inner = (m.group(1) or "").strip()
        if len(inner) < 2:
            return m.group(0)
        if " " in inner or len(inner) > 20:
            return ""
        if re.search(
            r"(?i)(flinch|gaze|expression|narrat|tilts?\s+her|holds?\s+the|doesn't|they\s+just|stage\s*direction)|(\b(they|she|he|it)\b)",
            inner,
        ):
            return ""
        return m.group(0)

    out = re.sub(r"\*+([^*]+?)\*+", _ast_repl, out)
    out = re.sub(r"[ \t]+", " ", out)
    out = re.sub(r"\n{3,}", "\n\n", out)
    return out.strip()


_VRM_REPLY_STATE_LEAK_RE = re.compile(
    r"speaking\s*=\s*(?:True|False)\b.*\bbody_mode\s*=\s*\S+.*\bdominant_emotion\s*=",
    re.I | re.S,
)


def _is_leaked_vrm_self_state_line(line: str) -> bool:
    """True when the model echoed [Live VRM self-state] telemetry (must not reach the user)."""
    s = (line or "").strip()
    if not s or len(s) > 2500:
        return False
    if "intent_levels(" in s.lower():
        return True
    rest = re.sub(r"^(?:\s*\[[A-Z][A-Z0-9_]*\]\s*)+", "", s, flags=re.I)
    if _VRM_REPLY_STATE_LEAK_RE.search(rest):
        return True
    if _VRM_REPLY_STATE_LEAK_RE.search(s):
        return True
    return False


def _sanitize_luna_reply(text: str) -> str:
    """Strip hallucinated preambles (wrong persona, inappropriate openings). Only for main chat."""
    if not text:
        return ""
    if len((text or "").strip()) < 2:
        return _strip_luna_narration_brackets((text or "").strip())
    # Remove hidden/internal sections some models occasionally emit:
    # [Self-note], [Self-notice], [Internal], [Reasoning], etc.
    lines = text.replace("\r\n", "\n").split("\n")
    cleaned: list[str] = []
    hide_block = False
    blocked_labels = {
        "self",
        "selfnote",
        "selfnotice",
        "selfreflection",
        "selfreflect",
        "notice",
        "internal",
        "internalnote",
        "internalthought",
        "thought",
        "thinking",
        "reasoning",
        "analysis",
        "reflection",
        "meta",
        "scratchpad",
    }
    response_labels = {"response", "reply", "final", "answer"}
    for raw_line in lines:
        line = raw_line.strip()
        if _is_leaked_vrm_self_state_line(line):
            hide_block = False
            continue
        m = re.match(r"^\[([^\]]+)\]\s*(.*)$", line)
        if m:
            raw_inner = (m.group(1) or "").strip()
            label = re.sub(r"[^a-z0-9]+", "", m.group(1).lower())
            tail = (m.group(2) or "").strip()
            # Skip only this line — do *not* hide everything after. Moondream/small VLMs often emit
            # a single `[Thinking]`/`[Thought]` header then normal speech; persistent hide_block wiped replies.
            if label in blocked_labels:
                hide_block = False
                continue
            if label in response_labels:
                hide_block = False
                if tail:
                    cleaned.append(tail)
                continue
            # A single whitelisted word like [HAPPY] — not "[Luna looks away] narrative inside brackets"
            if " " in raw_inner or len(raw_inner) > 20:
                hide_block = False
                if tail:
                    cleaned.append(tail)
                continue
            hide_block = False
            cleaned.append(raw_line)
            continue
        if hide_block:
            continue
        cleaned.append(raw_line)

    text = "\n".join(cleaned)
    text = re.sub(r"\n{3,}", "\n\n", text).strip()
    if not text:
        return ""

    first_120 = text[:120].lower()
    if any(p in first_120 for p in ("sweetie,", " let daddy", " let mommy", "i'm not luna")):
        for sep in (". ", "! ", "? "):
            idx = text.find(sep)
            if 30 < idx < 180:
                rest = text[idx + len(sep):].strip()
                if len(rest) > 15:
                    return _strip_luna_narration_brackets(rest)
    # Hide leaked third-person framing from context injection.
    # Example: "The user is playing ARAM..." -> remove opening and keep direct answer.
    lead = text.lstrip()
    lead_l = lead[:220].lower()
    third_person_open = (
        re.match(r"^(the\s+user|user|the\s+player|player|the\s+streamer|streamer)\b", lead_l) is not None
        or re.match(r"^(he|she|they)\s+(is|are)\s+(currently\s+)?(playing|in|asking|saying|trying)\b", lead_l) is not None
        or re.match(r"^(luna|the\s+assistant)\s+(is|looks|seems|says|does)\b", lead_l) is not None
        or lead_l.startswith("based on the context")
        or lead_l.startswith("from the context")
        or lead_l.startswith("based on the snapshot")
        or lead_l.startswith("from the snapshot")
        or lead_l.startswith("based on the data")
        or lead_l.startswith("from the data")
        or lead_l.startswith("looking at the json")
        or lead_l.startswith("according to the json")
    )
    if third_person_open:
        for sep in (". ", "! ", "? ", "\n"):
            idx = text.find(sep)
            if 10 <= idx <= 240:
                rest = text[idx + len(sep):].strip()
                if len(rest) >= 12:
                    text = rest
                    break
    out = _strip_luna_narration_brackets(text)
    s = (out or "").strip()
    if not s:
        return "[NEUTRAL] I'm here — what's on your mind?"
    # Drop only a bare emotion tag: "[HAPPY]" with no speech after
    after_tag = re.sub(r"^\s*\[[A-Z][A-Z0-9_]{1,20}\]\s*", "", s, count=1).strip()
    if not after_tag:
        return "[NEUTRAL] I'm here — what's on your mind?"
    # If opening is still self-narration after stripping blocks, keep only the first direct sentence after it.
    after_tag_l = after_tag.lower()
    self_narr_open = (
        re.match(r"^(luna|the\s+assistant)\s+(is|looks|seems|says|does)\b", after_tag_l) is not None
        or after_tag_l.startswith("as luna")
        or after_tag_l.startswith("internally")
    )
    if self_narr_open:
        for sep in (". ", "! ", "? ", "\n"):
            idx = after_tag.find(sep)
            if 10 <= idx <= 240:
                rest = after_tag[idx + len(sep):].strip()
                if len(rest) >= 8:
                    # Preserve leading tag when present.
                    mtag = re.match(r"^\s*(\[[A-Z][A-Z0-9_]{1,20}\])\s*", s)
                    if mtag:
                        return f"{mtag.group(1)} {rest}".strip()
                    return rest
    return out

def _ollama_assistant_message_text(message: dict | None) -> str:
    """Extract visible assistant text from Ollama /api/chat `message` object.

    Thinking-capable models may leave `content` empty when misconfigured; we never surface raw `thinking`.
    """
    if not message or not isinstance(message, dict):
        return ""
    raw = message.get("content")
    if isinstance(raw, list):
        parts: list[str] = []
        for p in raw:
            if isinstance(p, str) and p.strip():
                parts.append(p.strip())
            elif isinstance(p, dict):
                t = (p.get("text") or p.get("content") or "").strip()
                if t:
                    parts.append(t)
        c = "\n".join(parts).strip()
    else:
        c = (raw or "").strip() if isinstance(raw, str) else ""
    if c:
        return c
    # Do not leak model "thinking" trace to users.
    return ""


def _run_with_model_guard(
    *,
    kind: str,
    fn,
    queue_wait_sec: int,
    hard_timeout_sec: int | None,
):
    """Queue and guard long model calls so spikes do not pile up/hang."""
    slots = _chat_call_slots if kind == "chat" else _vision_call_slots
    if not slots.acquire(timeout=max(1, int(queue_wait_sec))):
        raise TimeoutError(f"{kind}_queue_busy")
    pool = None
    fut = None
    try:
        if hard_timeout_sec is None:
            return fn()
        pool = concurrent.futures.ThreadPoolExecutor(max_workers=1, thread_name_prefix=f"luna-{kind}")
        fut = pool.submit(fn)
        return fut.result(timeout=max(1, int(hard_timeout_sec)))
    except concurrent.futures.TimeoutError as _e:
        raise TimeoutError(f"{kind}_timeout") from _e
    finally:
        if fut is not None and not fut.done():
            fut.cancel()
        if pool is not None:
            try:
                pool.shutdown(wait=False, cancel_futures=True)
            except Exception:
                pass
        try:
            slots.release()
        except Exception:
            pass


def _openai_compat_extract_text(content) -> str:
    if isinstance(content, str):
        return content.strip()
    if isinstance(content, list):
        out: list[str] = []
        for it in content:
            if isinstance(it, dict):
                if (it.get("type") or "") == "text":
                    t = (it.get("text") or "").strip()
                    if t:
                        out.append(t)
                elif "text" in it and isinstance(it.get("text"), str):
                    t = it.get("text").strip()
                    if t:
                        out.append(t)
        return " ".join(out).strip()
    return ""


def _openai_compat_chat_once(
    *,
    base_url: str,
    api_key: str,
    model: str,
    messages: list[dict],
    timeout: int,
    compact: bool,
) -> str:
    if not api_key:
        raise RuntimeError("Missing API key for selected provider.")
    payload: dict = {"model": model, "messages": messages}
    if compact:
        payload["temperature"] = 0.75
        payload["max_tokens"] = 768
    body = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        f"{base_url}/chat/completions",
        data=body,
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {api_key}",
        },
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=timeout) as r:
        data = json.loads(r.read())
    ch = (data.get("choices") or [])
    msg0 = ((ch[0] or {}).get("message") or {}) if ch else {}
    out = _openai_compat_extract_text(msg0.get("content"))
    return out or "No reply."


def _chat_provider_once(
    msg: str,
    system: str | None,
    scope: str | None,
    history: list | None,
    model: str,
    timeout: int | None = None,
    *,
    compact: bool = False,
    image_bytes: bytes | None = None,
) -> str:
    provider = (LUNA_CHAT_PROVIDER or "ollama").strip().lower()
    to = timeout if timeout is not None else (180 if compact else 120)
    if provider in ("ollama", "gguf"):
        return _ollama_chat_once(
            msg, system, scope, history, model, timeout=to, compact=compact, image_bytes=image_bytes
        )
    messages = _build_chat_messages(msg, system, scope, history, compact=compact)
    if provider in ("openai", "openai_compat"):
        use_model = (model or LUNA_OPENAI_CHAT_MODEL or OLLAMA_CHAT).strip()
        return _openai_compat_chat_once(
            base_url=LUNA_OPENAI_BASE_URL,
            api_key=LUNA_OPENAI_API_KEY,
            model=use_model,
            messages=messages,
            timeout=to,
            compact=compact,
        )
    if provider == "groq":
        use_model = (model or LUNA_GROQ_CHAT_MODEL or OLLAMA_CHAT).strip()
        return _openai_compat_chat_once(
            base_url=LUNA_GROQ_BASE_URL,
            api_key=GROQ_API_KEY,
            model=use_model,
            messages=messages,
            timeout=to,
            compact=compact,
        )
    raise RuntimeError(f"Unsupported LUNA_CHAT_PROVIDER: {provider}")


def _ollama_chat_once(
    msg: str,
    system: str | None,
    scope: str | None,
    history: list | None,
    model: str,
    timeout: int | None = None,
    *,
    compact: bool = False,
    image_bytes: bytes | None = None,
) -> str:
    if _should_use_gguf_chat(model):
        to = timeout if timeout is not None else (180 if compact else 120)
        return _gguf_chat_once(msg, system, scope, history, model, to, compact=compact)
    messages = _build_chat_messages(msg, system, scope, history, compact=compact)
    if image_bytes and messages and (messages[-1].get("role") or "").lower() == "user":
        try:
            b64 = base64.b64encode(image_bytes).decode("ascii")
            last = dict(messages[-1])
            last["images"] = [b64]
            messages = messages[:-1] + [last]
        except Exception:
            pass
    to = timeout if timeout is not None else (180 if compact else 120)
    # `think: false` is top-level (not in options). Avoids empty `content` on thinking models.
    payload: dict = {"model": model, "messages": messages, "stream": False, "think": False}
    if compact:
        payload["options"] = {"temperature": 0.75, "num_predict": 768}
    body = json.dumps(payload).encode()
    req = urllib.request.Request(f"{OLLAMA_BASE}/api/chat", data=body,
        headers={"Content-Type": "application/json"}, method="POST")
    with urllib.request.urlopen(req, timeout=to) as r:
        data = json.loads(r.read())
    err = (data.get("error") or "").strip()
    if err:
        return f"Ollama: {err}"
    text_out = _ollama_assistant_message_text(data.get("message"))
    return text_out or "No reply."

def ollama_chat(
    msg: str,
    system: str | None = None,
    scope: str | None = None,
    history: list | None = None,
    model: str | None = None,
    *,
    compact: bool = False,
    timeout: int | None = None,
    image_bytes: bytes | None = None,
) -> str:
    use_model = (model or OLLAMA_MODEL).strip()
    call_to = timeout if timeout is not None else (180 if compact else 120)
    provider = (LUNA_CHAT_PROVIDER or "ollama").strip().lower()
    multimodal = bool(image_bytes)
    try:
        raw = _run_with_model_guard(
            kind="chat",
            queue_wait_sec=_MODEL_CHAT_QUEUE_WAIT_SEC,
            hard_timeout_sec=max(call_to + 8, _MODEL_CHAT_GUARD_TIMEOUT_SEC),
            fn=lambda: _chat_provider_once(
                msg,
                system,
                scope,
                history,
                use_model,
                timeout=call_to,
                compact=compact,
                image_bytes=image_bytes,
            ),
        )
        return _sanitize_luna_reply(raw)
    except Exception as primary_err:
        # If primary chat model is throttled (HTTP 429), retry on a stable local fallback (default: llama3.2).
        is_rate_limited = (
            isinstance(primary_err, urllib.error.HTTPError) and int(getattr(primary_err, "code", 0) or 0) == 429
        ) or ("429" in str(primary_err or "").lower()) or ("too many requests" in str(primary_err or "").lower())
        if provider in ("ollama", "gguf") and is_rate_limited and not multimodal:
            rl_model = (OLLAMA_RATE_LIMIT_FALLBACK or "llama3.2:latest").strip()
            if rl_model and rl_model != use_model:
                try:
                    rl_to = timeout if timeout is not None else 90
                    raw = _run_with_model_guard(
                        kind="chat",
                        queue_wait_sec=_MODEL_CHAT_QUEUE_WAIT_SEC,
                        hard_timeout_sec=max(rl_to + 8, _MODEL_CHAT_GUARD_TIMEOUT_SEC),
                        fn=lambda: _chat_provider_once(
                            msg,
                            system,
                            scope,
                            history,
                            rl_model,
                            timeout=rl_to,
                            compact=compact,
                            image_bytes=None,
                        ),
                    )
                    return _sanitize_luna_reply(raw)
                except Exception:
                    pass
        if provider in ("ollama", "gguf") and OLLAMA_FALLBACK and OLLAMA_FALLBACK != use_model and not multimodal:
            try:
                fb_to = timeout if timeout is not None else 90
                raw = _run_with_model_guard(
                    kind="chat",
                    queue_wait_sec=_MODEL_CHAT_QUEUE_WAIT_SEC,
                    hard_timeout_sec=max(fb_to + 8, _MODEL_CHAT_GUARD_TIMEOUT_SEC),
                    fn=lambda: _chat_provider_once(
                        msg,
                        system,
                        scope,
                        history,
                        OLLAMA_FALLBACK,
                        timeout=fb_to,
                        compact=compact,
                        image_bytes=None,
                    ),
                )
                return _sanitize_luna_reply(raw)
            except Exception:
                pass
        if isinstance(primary_err, TimeoutError):
            em = str(primary_err or "")
            if "queue_busy" in em and not multimodal:
                # Voice/chat bursts can briefly saturate the single chat slot.
                # Retry once with a longer queue wait before giving up.
                try:
                    retry_wait = max(_MODEL_CHAT_QUEUE_WAIT_SEC, 12)
                    retry_to = timeout if timeout is not None else (75 if compact else 60)
                    raw = _run_with_model_guard(
                        kind="chat",
                        queue_wait_sec=retry_wait,
                        hard_timeout_sec=max(retry_to + 8, _MODEL_CHAT_GUARD_TIMEOUT_SEC),
                        fn=lambda: _chat_provider_once(
                            msg,
                            system,
                            scope,
                            history,
                            use_model,
                            timeout=retry_to,
                            compact=compact,
                            image_bytes=None,
                        ),
                    )
                    return _sanitize_luna_reply(raw)
                except Exception:
                    return "Luna is busy with other requests right now. Try again in a few seconds."
            if "timeout" in em:
                return "Luna took too long to answer and was timed out. Try again."
        if isinstance(primary_err, urllib.error.URLError):
            return f"Ollama offline: {primary_err.reason}"
        return f"Error: {primary_err}"

def ollama_stream(
    msg: str,
    system: str | None = None,
    scope: str | None = None,
    history: list | None = None,
    model: str | None = None,
    *,
    compact: bool = False,
):
    """Yields content deltas as they stream from Ollama."""
    provider = (LUNA_CHAT_PROVIDER or "ollama").strip().lower()
    if provider not in ("ollama", "gguf"):
        yield ollama_chat(
            msg,
            system=system,
            scope=scope,
            history=history,
            model=model,
            compact=compact,
        )
        return
    use_model = (model or OLLAMA_CHAT).strip()
    messages = _build_chat_messages(msg, system, scope, history, compact=compact)
    if _should_use_gguf_chat(use_model):
        try:
            yield from _gguf_stream_messages(messages, compact=compact)
        except Exception:
            yield "Something went wrong."
        return
    payload: dict = {"model": use_model, "messages": messages, "stream": True, "think": False}
    if compact:
        payload["options"] = {"temperature": 0.75, "num_predict": 768}
    body = json.dumps(payload).encode()
    req = urllib.request.Request(f"{OLLAMA_BASE}/api/chat", data=body,
        headers={"Content-Type": "application/json"}, method="POST")
    try:
        stream_to = 180 if compact else 120
        with urllib.request.urlopen(req, timeout=stream_to) as resp:
            buf = b""
            for chunk in iter(lambda: resp.read(4096), b""):
                buf += chunk
                while b"\n" in buf:
                    line, buf = buf.split(b"\n", 1)
                    try:
                        msg_obj = (json.loads(line).get("message") or {})
                        th = (msg_obj.get("thinking") or "")
                        c = (msg_obj.get("content") or "")
                        if c:
                            yield c
                        elif th:
                            yield th
                    except Exception:
                        pass
    except Exception:
        yield "Something went wrong."

def _summarize(messages: list[dict]) -> str:
    lines = [f"{'User' if m.get('role')=='user' else 'Luna'}: {(m.get('content') or '')[:300]}"
             for m in messages if m.get("content")]
    if not lines: return ""
    try:
        out = ollama_chat("\n".join(lines[-20:]),
            system="Summarize in 2-4 sentences. Key topics only.",
            model=OLLAMA_SMALL)
        return out.strip()[:500]
    except Exception:
        return ""


# ── Rolling per-scope summary (long-term conversation memory) ────────────────
# `_compact_history` only sees the last ~30 messages of a scope. Anything older
# is invisible to the model — that's why long-term Discord users feel like Luna
# develops amnesia. The rolling summary fixes that: a persistent per-scope
# digest that grows incrementally as older messages roll out of the prompt
# window. Lives in ``data/conversation_summaries.json``; updated in the same
# background thread that already runs `_capture_memory` / `_capture_profile`.
import hashlib as _hashlib  # noqa: E402  (deferred import keeps module-load order intact)


def _rs_msg_hash(msg: dict) -> str:
    """Stable per-message identity (role+content). Used to skip messages already
    incorporated into the rolling summary — survives conversation truncation."""
    if not isinstance(msg, dict):
        return ""
    role = (msg.get("role") or "").strip().lower()
    content = (msg.get("content") or "").strip()
    if not role or not content:
        return ""
    return _hashlib.sha1(f"{role}\x1f{content}".encode("utf-8", errors="replace")).hexdigest()


def _rs_pending_messages(messages: list[dict], *, last_hash: str, keep_recent: int) -> list[dict]:
    """Return the older messages that should be folded into the rolling summary.

    Skips everything up to and including the message whose hash matches
    `last_hash` (already summarized in a prior turn), and excludes the most
    recent `keep_recent` messages (still raw in the prompt window).
    """
    if not messages:
        return []
    start_idx = 0
    if last_hash:
        for i, m in enumerate(messages):
            if _rs_msg_hash(m) == last_hash:
                start_idx = i + 1
                break
    if start_idx >= len(messages):
        return []
    end_idx = max(start_idx, len(messages) - keep_recent)
    return list(messages[start_idx:end_idx])


def _rs_format_messages(messages: list[dict], *, max_per_msg: int = 280) -> str:
    """Compact User:/Luna: transcript for the merge prompt."""
    out: list[str] = []
    for m in messages:
        if not isinstance(m, dict):
            continue
        role = (m.get("role") or "").strip().lower()
        content = (m.get("content") or "").strip()
        if not content:
            continue
        speaker = "User" if role == "user" else "Luna"
        snippet = re.sub(r"\s+", " ", content)[:max_per_msg]
        out.append(f"{speaker}: {snippet}")
    return "\n".join(out)


def _rs_merge_via_llm(prior_summary: str, new_block: str) -> str:
    """LLM-merge the prior digest with new exchanges into one coherent summary."""
    if not new_block.strip():
        return prior_summary
    prior = (prior_summary or "").strip()
    sys_prompt = (
        "You maintain a long-term running summary of a conversation between "
        "Luna (the assistant) and a single user. Merge the prior summary with "
        "the new exchanges into ONE updated summary in 6-12 sentences max. "
        "Preserve concrete facts the user revealed about themselves (name, "
        "preferences, projects, goals, opinions, recurring themes). Drop "
        "small-talk and repetition. Write in third-person ('The user…' / "
        "'Luna…'). No bullet points, no headers — flowing prose only."
    )
    user_prompt = (
        f"PRIOR SUMMARY (may be empty):\n{prior or '(none)'}\n\n"
        f"NEW EXCHANGES (oldest first):\n{new_block}\n\n"
        f"Output the updated summary only — no preamble."
    )
    try:
        out = ollama_chat(
            user_prompt, system=sys_prompt, model=OLLAMA_SMALL, compact=True
        )
        out = (out or "").strip()
        if not out:
            return prior
        # Hard cap at the persistence layer's max so we never spill the budget.
        return out[:ROLLING_SUMMARY_MAX_CHARS]
    except Exception:
        return prior


def _rs_keep_recent_for_summary() -> int:
    """How many recent exchanges stay raw in the prompt; older ones get summarized."""
    try:
        v = int((_env("LUNA_ROLLING_SUMMARY_KEEP_RECENT", "12") or "12").strip())
    except Exception:
        v = 12
    return max(4, min(40, v))


def _rs_min_new_messages() -> int:
    """Minimum new messages required before we trigger an LLM merge (avoids
    burning tokens when only one or two messages have rolled past the window)."""
    try:
        v = int((_env("LUNA_ROLLING_SUMMARY_MIN_NEW", "4") or "4").strip())
    except Exception:
        v = 4
    return max(1, min(50, v))


def _rs_enabled() -> bool:
    return _env("LUNA_ROLLING_SUMMARY", "1").strip().lower() not in ("0", "false", "no", "off")


def _update_rolling_summary(scope: str) -> None:
    """Fold messages that have rolled past `keep_recent` into the per-scope
    rolling summary. Safe to call from any thread; cheap when nothing is
    pending. Designed for the background memory-capture thread."""
    if not _rs_enabled() or not scope:
        return
    try:
        full = get_recent_conversation(scope, 200)
    except Exception:
        return
    if not full:
        return
    state = get_rolling_summary_state(scope)
    last_hash = state.get("last_user_hash") or ""
    keep_recent = _rs_keep_recent_for_summary()
    pending = _rs_pending_messages(full, last_hash=last_hash, keep_recent=keep_recent)
    if len(pending) < _rs_min_new_messages():
        return
    new_block = _rs_format_messages(pending)
    if not new_block.strip():
        return
    merged = _rs_merge_via_llm(state.get("summary") or "", new_block)
    if not merged or merged == (state.get("summary") or ""):
        # Even if merge produced nothing useful, advance the cursor so we don't
        # re-attempt these same messages forever.
        last_pinned = pending[-1]
        try:
            set_rolling_summary(
                scope, state.get("summary") or "",
                last_user_hash=_rs_msg_hash(last_pinned),
                msgs_added=len(pending),
            )
        except Exception:
            pass
        return
    last_pinned = pending[-1]
    try:
        set_rolling_summary(
            scope, merged,
            last_user_hash=_rs_msg_hash(last_pinned),
            msgs_added=len(pending),
        )
    except Exception:
        pass


async def _rolling_summary_backfill_task() -> None:
    """One-shot startup pass: seed rolling summaries for scopes that already have
    long histories on disk so users with prior conversations don't have to wait
    for new messages to roll past the recent window before Luna 'remembers'.

    Iterates every scope with >= ``LUNA_ROLLING_SUMMARY_BACKFILL_MIN`` messages
    that doesn't yet have a rolling summary, and runs `_update_rolling_summary`
    with a short async sleep between scopes so we don't hammer the LLM at boot.
    """
    if not _rs_enabled():
        return
    if _env("LUNA_ROLLING_SUMMARY_BACKFILL", "1").strip().lower() in ("0", "false", "no", "off"):
        return
    try:
        await bot.wait_until_ready()
    except Exception:
        pass
    await asyncio.sleep(45)  # let the rest of startup settle (Whisper, OBS, Twitch, …)
    try:
        min_msgs = int((_env("LUNA_ROLLING_SUMMARY_BACKFILL_MIN", "20") or "20").strip())
    except Exception:
        min_msgs = 20
    min_msgs = max(8, min(200, min_msgs))
    try:
        from luna_conversation import _load as _conv_load
    except Exception:
        return
    try:
        existing_summaries = set(list_rolling_summary_scopes())
    except Exception:
        existing_summaries = set()
    try:
        all_convos = _conv_load()
    except Exception:
        return
    candidates = [
        scope for scope, msgs in all_convos.items()
        if isinstance(scope, str) and isinstance(msgs, list)
        and len(msgs) >= min_msgs and scope not in existing_summaries
    ]
    if not candidates:
        return
    print(
        f"[Rolling summary] Backfilling {len(candidates)} scope(s) with >= {min_msgs} msgs (one-shot at boot).",
        flush=True,
    )
    for scope in candidates:
        try:
            await asyncio.to_thread(_update_rolling_summary, scope)
        except Exception as e:
            print(f"[Rolling summary] Backfill failed for {scope}: {e}", flush=True)
        await asyncio.sleep(2.0)
    print("[Rolling summary] Backfill complete.", flush=True)


def _rolling_summary_prompt_pair(scope: str) -> list[dict]:
    """Synthetic (user, assistant) pair injected at the head of the history so
    the model conditions on the long-term digest. Returns [] when no summary
    exists for the scope (fresh conversations behave exactly like before)."""
    if not scope or not _rs_enabled():
        return []
    try:
        s = get_rolling_summary(scope)
    except Exception:
        s = ""
    if not s:
        return []
    return [
        {"role": "user", "content": f"[Memory of earlier conversation with this user]\n{s}"},
        {"role": "assistant", "content": "Got it — I remember our earlier conversations."},
    ]


def _compact_history(
    messages: list[dict] | None,
    *,
    fast: bool = False,
    scope: str | None = None,
) -> list[dict]:
    """Trim history for context window.

    If *fast*, never call the summarizer LLM (saves a full round-trip).
    If *scope* is provided AND a rolling summary exists for that scope, the
    summary is prepended as a synthetic [Memory…] pair so older context stays
    visible to the model even after it falls outside the recent window.
    """
    prefix = _rolling_summary_prompt_pair(scope) if scope else []
    if not messages:
        return prefix
    messages = list(messages)
    if len(messages) <= _COMPACT_AT:
        return prefix + messages
    if fast:
        return prefix + messages[-_KEEP_RECENT:]
    summary = _summarize(messages[:-_KEEP_RECENT])
    recent = messages[-_KEEP_RECENT:]
    if not summary:
        return prefix + recent
    return prefix + [
        {"role": "user", "content": f"[Summary]: {summary}"},
        {"role": "assistant", "content": "Understood."},
    ] + recent

# ── Memory capture ────────────────────────────────────────────────────────────

def _capture_memory(scope: str, text: str, luna_reply: str = "") -> None:
    text = text.strip()
    if not text or not scope: return
    # Memory from explicit phrases / heuristics only — no second Ollama call.
    # Goal patterns
    for p in (r"\bmy goal is\s+(.+?)(?:\.|$)", r"\bremember my goal[:\s]+(.+?)(?:\.|$)",
               r"\bmy goals? (?:are|is)\s+(.+?)(?:\.|$)"):
        m = re.search(p, text, re.I | re.S)
        if m: add_goal(scope, m.group(1).strip()[:500]); return
    # Core memory (+ knowledge base)
    for p in (r"\balways remember that\s+(.+?)(?:\.|$)", r"\balways remember[:\s]+(.+?)(?:\.|$)"):
        m = re.search(p, text, re.I | re.S)
        if m:
            content = m.group(1).strip()[:1500]
            add_core_memory(scope, content)
            add_knowledge("Always remember", content)
            biology_satisfy("curiosity", 0.15)
            return
    # Long-term memory (+ knowledge base)
    for p in (r"\bremember that\s+(.+?)(?:\.|$)", r"\bremember[:\s]+(.+?)(?:\.|$)"):
        m = re.search(p, text, re.I | re.S)
        if m:
            content = m.group(1).strip()[:1500]
            add_memory(scope, content)
            add_knowledge("Remember", content)
            biology_satisfy("curiosity", 0.15)
            return
    # Name
    m = re.search(r"\b(?:my name is|call me|i am called)\s+([a-zA-Z][a-zA-Z\s\-']{0,50})(?:\.|,|\s+and|\s*$)", text, re.I)
    if m:
        name = m.group(1).strip()
        if 1 <= len(name) <= 80:
            add_core_memory(scope, f"The user's name is {name}.")
        return
    # Preferences
    m = re.search(r"\b(?:i like|i love|i prefer|i enjoy)\s+(.+?)(?:\.|$)", text, re.I | re.S)
    if m:
        pref = m.group(1).strip()[:300]
        if len(pref) >= 2: add_memory(scope, f"The user likes: {pref}.")
        return

def _capture_profile(scope: str, text: str) -> None:
    text = text.strip()
    if not text or not scope: return
    def _append_profile(field: str, value: str) -> None:
        v = re.sub(r"\s+", " ", (value or "").strip(" .,!?:;")).strip()
        if len(v) < 2:
            return
        current = (get_profile(scope).get(field) or "").strip()
        if not current:
            set_profile_field(scope, field, v[:400])
            return
        if v.lower() in current.lower():
            return
        merged = f"{current}; {v}"
        set_profile_field(scope, field, merged[:1000])

    # Name
    m = re.search(r"\b(?:my name is|call me|i am called)\s+([a-zA-Z][a-zA-Z\s\-']{0,50})(?:\.|,|\s+and|\s*$)", text, re.I | re.S)
    if m:
        nm = m.group(1).strip()
        if 1 <= len(nm) <= 80:
            set_profile_field(scope, "name", nm)

    # Hobbies (dedicated profile field)
    hobby_hit = False
    for pat in (
        r"\b(?:my hobbies are|my hobby is)\s+(.+?)(?:\.|$)",
        r"\b(?:my interests? are|i am interested in|i'm interested in|im interested in|i'?m into)\s+(.+?)(?:\.|$)",
    ):
        m = re.search(pat, text, re.I | re.S)
        if m:
            _append_profile("hobbies", m.group(1)[:260])
            hobby_hit = True
            break

    # Likes / preferences (broader than hobbies)
    if not hobby_hit:
        pref_patterns = [
            r"\b(?:i like|i love|i enjoy|i prefer)\s+(.+?)(?:\.|$)",
        ]
        for pat in pref_patterns:
            m = re.search(pat, text, re.I | re.S)
            if m:
                _append_profile("preferences", m.group(1)[:260])
                break

    # Goals / intentions
    m = re.search(r"\b(?:my goals? (?:is|are)|my goal is|i want to|i'd like to|i would like to)\s+(.+?)(?:\.|$)", text, re.I | re.S)
    if m:
        _append_profile("goals", m.group(1)[:260])

    # About (location, work, personal descriptors)
    about_parts = []
    for pat in (
        r"\b(?:i live in|i'm from|im from|based in)\s+(.+?)(?:\.|$)",
        r"\b(?:i work as|i work at|my job is|i am a|i'm a|im a|i am an|i'm an|im an)\s+(.+?)(?:\.|$)",
    ):
        m = re.search(pat, text, re.I | re.S)
        if m:
            about_parts.append(m.group(0).strip())
    if about_parts:
        _append_profile("about", " | ".join(about_parts)[:350])

def _is_about_me_query(text: str) -> bool:
    """User asking what Luna knows / remembers about them."""
    t = (text or "").strip().lower()
    if len(t) > 160:
        return False
    patterns = (
        r"\bwho am i\b",
        r"\bwhat do you know about me\b",
        r"\bwhat do you remember about me\b",
        r"\btell me what you know about me\b",
        r"\bwhat have you learned about me\b",
        r"\bdo you know who i am\b",
        r"\bremind me what you know\b",
        r"\bwhat'?s my profile\b",
        r"\bshow me my profile\b",
    )
    return any(re.search(p, t) for p in patterns)

def _about_me_context_suffix(user_message: str) -> str:
    if not _is_about_me_query(user_message):
        return ""
    return (
        "\n\n## This turn: explaining what you know about the user\n"
        "They asked what you know about **them** (identity / profile / memory). "
        "Use the **User profile** block and **Core / Long-term / Recent** memory sections already in your context. "
        "Give a clear, warm recap — organized, not a wall of text. If almost nothing is stored, say so kindly; "
        "do **not** invent facts.\n"
        "Always end with **one** specific, friendly question to learn something you clearly do not have yet "
        "(e.g. a hobby, what they're building, how they recharge) — not a vague \"anything else?\".\n"
        "Tell them they can answer naturally: you'll save it if they say **remember that …** / "
        "**always remember …**, or facts like **my hobbies are …**, **my name is …**, or "
        "**!profile set** with a field name and value on Discord (see **!profile** for allowed fields)."
    )

# ── TTS ───────────────────────────────────────────────────────────────────────

def _is_gemma4_family_model(model_id: str | None) -> bool:
    """Ollama-style tags (e.g. gemma4:e4b) and similar Gemma 4 IDs."""
    m = (model_id or "").strip().lower().replace(" ", "")
    if not m:
        return False
    if m.startswith("gemma4"):
        return True
    return "gemma-4" in m or "gemma4" in m


def _strip_emojis_for_tts(text: str) -> str:
    """Remove emoji / pictograph codepoints so engines do not speak or garble them."""
    if not text:
        return text
    out: list[str] = []
    for ch in text:
        o = ord(ch)
        # Zero-width joiner, variation selectors, combining keycap.
        if o in (0x200D, 0xFE0F, 0x20E3):
            continue
        # Skin-tone modifiers.
        if 0x1F3FB <= o <= 0x1F3FF:
            continue
        if (
            0x1F300 <= o <= 0x1FAFF      # symbols & pictographs (incl. supplemental + extended-A)
            or 0x2600 <= o <= 0x26FF     # misc symbols (☀ ☁ ⚡ ★ ♻)
            or 0x2700 <= o <= 0x27BF     # dingbats (✂ ✈ ✉ ✨)
            or 0x2300 <= o <= 0x23FF     # misc tech (⌚ ⌛ ⏰ ⏱)
            or 0x2B00 <= o <= 0x2BFF     # misc symbols & arrows (⬆ ⬇ ⭐)
            or 0x2900 <= o <= 0x297F     # supplemental arrows-B
            or 0x1F600 <= o <= 0x1F64F   # emoticons (😀 😁 😂)
            or 0x1F680 <= o <= 0x1F6FF   # transport & map (🚀 🚗)
            or 0x1F1E6 <= o <= 0x1F1FF   # regional indicators (flags)
        ):
            continue
        out.append(ch)
    s = "".join(out)
    s = re.sub(r"\s+", " ", s).strip()
    return s


# Common ASCII text-emoticons stripped before TTS. Matched only at word boundaries
# so prose like "ratio: 3:5" or "from a:b mapping" is not affected.
_TEXT_EMOTICON_RE = re.compile(
    r"(?<![A-Za-z0-9])"
    r"(?:"
    r"[:;=8][\-^']?[\)\(\]\[DPpOo|/\\3*]+"   # :)  :-)  :D  :-P  ;)  =)  8|  :/
    r"|<\/?3+"                                # <3  </3
    r"|[xX][DdPp]"                            # xD  XD  xP
    r"|\^[_.]?\^"                             # ^_^  ^^  ^.^
    r"|[Tt][_.][Tt]"                          # T_T  T.T
    r"|[oO0][_.][oO0]"                        # o_o  O.O  0_0
    r"|>[:;=][\-^']?[\)\(]"                   # >:)  >:(
    r")"
    r"(?![A-Za-z0-9])"
)


def _strip_text_emoticons_for_tts(text: str) -> str:
    """Drop ASCII smileys / kaomoji that would otherwise be read as ‘colon paren’ etc."""
    if not text:
        return text
    return _TEXT_EMOTICON_RE.sub("", text)


def _tts_bytes(text: str) -> bytes:
    """Short TTS for Discord VC, reminders, inline replies: Edge (Ava by default) → Fish → gTTS."""
    text = (text or "").strip()[:500]
    # Strip emojis + ASCII smileys for every engine — Edge/Fish/gTTS otherwise read
    # them out as "smiling face" / "colon paren" depending on engine.
    text = _strip_emojis_for_tts(text)
    text = _strip_text_emoticons_for_tts(text)
    text = re.sub(r"\s+", " ", text).strip()
    if not text:
        return b""
    b = _edge_tts_bytes(text)
    if b:
        return b
    b = _fish_audio_tts_bytes(text)
    if b:
        return b
    try:
        from gtts import gTTS
        buf = io.BytesIO()
        gTTS(text=text, lang=GTTS_LANG, slow=False).write_to_fp(buf)
        buf.seek(0)
        return buf.read()
    except Exception:
        return b""

def _fish_audio_api_key() -> str:
    return (_env("FISH_AUDIO_API_KEY", "").strip() or _env("FISH_API_KEY", "").strip())

def _fish_audio_tts_bytes(text: str) -> bytes | None:
    """Cloud Fish Audio TTS (no local GPU). Returns None if not configured, SDK missing, or on error."""
    key, ref = _fish_audio_api_key(), _env("FISH_AUDIO_REFERENCE_ID", "").strip()
    if not key or not ref:
        return None
    text = (text or "").strip()
    if not text:
        return None
    try:
        from fishaudio import FishAudio
        from fishaudio.types import TTSConfig
    except ImportError:
        return None
    model = (_env("FISH_AUDIO_MODEL", "s2-pro").strip() or "s2-pro")
    lat = (_env("FISH_AUDIO_LATENCY", "balanced").strip().lower() or "balanced")
    if lat not in ("normal", "balanced"):
        lat = "balanced"
    # Per-chunk cap matches gTTS path; Fish handles longer passages via chunk_length internally.
    text = text[:5000]
    cfg = TTSConfig(reference_id=ref, chunk_length=200, mp3_bitrate=128, latency=lat)
    client = None
    try:
        client = FishAudio(api_key=key)
        audio = client.tts.convert(text=text, format="mp3", config=cfg, model=model)
        return audio if audio else None
    except Exception as e:
        print(f"[Luna] Fish Audio TTS failed: {e}")
        return None
    finally:
        if client is not None:
            try:
                client.close()
            except Exception:
                pass

def _edge_tts_bytes(text: str) -> bytes | None:
    """Microsoft Edge online TTS (no local GPU). Default voice: Ava Multilingual Neural."""
    if _env("EDGE_TTS", "1").strip().lower() in ("0", "false", "no", "off"):
        return None
    text = (text or "").strip()
    if not text:
        return None
    text = text[:10000]
    voice = (_env("EDGE_TTS_VOICE", "en-US-AvaMultilingualNeural").strip() or "en-US-AvaMultilingualNeural")
    try:
        import edge_tts
    except ImportError:
        return None

    async def _stream() -> bytes:
        communicate = edge_tts.Communicate(text, voice)
        buf = bytearray()
        async for chunk in communicate.stream():
            if chunk.get("type") == "audio":
                buf.extend(chunk["data"])
        return bytes(buf) if buf else b""

    def _run_async(coro):
        try:
            return asyncio.run(coro)
        except RuntimeError:
            loop = asyncio.new_event_loop()
            try:
                return loop.run_until_complete(coro)
            finally:
                loop.close()

    try:
        out = _run_async(_stream())
        return out if out else None
    except Exception as e:
        print(f"[Luna] Edge TTS failed: {e}")
        return None

def _tts_bytes_media(text: str) -> bytes:
    """TTS for long-form media (podcast / audiobook): Edge Ava Multilingual → Fish (optional) → gTTS."""
    b = _edge_tts_bytes(text)
    if b:
        return b
    b = _fish_audio_tts_bytes(text)
    if b:
        return b
    return _tts_bytes(text)

def _subprocess_no_window_flags() -> int:
    return getattr(subprocess, "CREATE_NO_WINDOW", 0) if sys.platform == "win32" else 0

def _ffprobe_duration_seconds(path: str) -> float | None:
    """Actual media duration in seconds, or None if ffprobe missing/failed."""
    try:
        r = subprocess.run(
            [
                "ffprobe", "-v", "error", "-show_entries", "format=duration",
                "-of", "default=noprint_wrappers=1:nokey=1", path,
            ],
            capture_output=True,
            text=True,
            timeout=120,
            creationflags=_subprocess_no_window_flags(),
        )
        if r.returncode != 0:
            return None
        return float((r.stdout or "").strip())
    except Exception:
        return None

def _audio_duration_filename_tag(seconds: float | None) -> str:
    """Short tag for filenames, e.g. 6m30s, 90m, 1h15m — not a 'target', actual length."""
    if seconds is None or seconds <= 0:
        return "unknown"
    secs = int(round(seconds))
    if secs < 60:
        return f"{secs}s"
    m, s = secs // 60, secs % 60
    if m < 60:
        return f"{m}m{s:02d}s" if s else f"{m}m"
    h, m2 = m // 60, m % 60
    if m2 or s:
        return f"{h}h{m2:02d}m{s:02d}s" if s else f"{h}h{m2:02d}m"
    return f"{h}h"

def _clean_for_tts(text: str) -> str:
    text = re.sub(r"LUNA_WRITE_FILE.*?END_LUNA_WRITE", "", text, flags=re.DOTALL | re.I)
    text = re.sub(r"```[\w]*\n.*?```", "", text, flags=re.DOTALL)
    text = re.sub(r"`[^`]*`", " ", text)
    text = re.sub(r"<[^>]+>", " ", text)
    text = re.sub(r"<@!?\d+>", "", text)
    # Drop ALL asterisk runs (markdown bold/italic). Single * was previously kept and
    # got read literally as "asterisk" by some engines.
    text = re.sub(r"\*+", "", text)
    # Underscore emphasis the same way — _word_ should not be spoken with the underscores.
    text = re.sub(r"(?<!\w)_+|_+(?!\w)", "", text)
    text = _strip_emojis_for_tts(text)
    text = _strip_text_emoticons_for_tts(text)
    return re.sub(r"\s+", " ", text).strip()


# Bracket tags → short phrases Edge TTS speaks naturally (see LUNA_SYSTEM gesture / vocal tags).
_LUNA_TAG_TTS_SPOKEN: dict[str, str] = {
    "SIGH": "Hmph.",
    "LAUGH": "Ha ha!",
    "GIGGLE": "Heh heh!",
    "CHUCKLE": "Heh.",
    "COUGH": "Ahem.",
    "AHEM": "Ahem.",
    "SNORT": "Pfft!",
    "HUFF": "Hmph.",
    "YAWN": "Mmm…",
    "FACEPALM": "Ugh.",
    "SHOCKED": "Whoa!",
    "SURPRISED": "Oh!",
    "SCARED": "Ah!",
    "CLAP": "Nice!",
    "FIGHTING": "Hah!",
    "VICTORY": "Yes!",
}


def _expand_luna_expression_tags_for_tts(text: str) -> str:
    """Replace Luna [TAG] markers with spoken interjections for TTS; strip any remaining bracket tags."""
    if not (text or "").strip():
        return ""
    if not LUNA_TTS_EXPRESSION_EXPAND:
        t = re.sub(r"\[[A-Za-z][A-Za-z0-9_]*\]\s*", "", text)
        return re.sub(r"\s+", " ", t).strip()
    t = text

    def _sub_tag(m: re.Match) -> str:
        key = (m.group(1) or "").upper()
        return _LUNA_TAG_TTS_SPOKEN.get(key, "")

    t = re.sub(r"\[([A-Za-z][A-Za-z0-9_]*)\]", _sub_tag, t)
    t = re.sub(r"\[[A-Za-z][A-Za-z0-9_]*\]\s*", "", t)
    return re.sub(r"\s+", " ", t).strip()

def _split_tts(text: str, max_chars: int = 80) -> list[str]:
    parts = re.split(r"(?<=[.!?,;:])\s+", text.strip())
    chunks, cur = [], ""
    for p in parts:
        if not p.strip(): continue
        if cur and len(cur) + len(p) + 1 <= max_chars:
            cur = (cur + " " + p).strip()
        else:
            if cur: chunks.append(cur)
            cur = p.strip()
    if cur: chunks.append(cur)
    return [c for c in chunks if c]


def _split_tts_paragraphs(text: str, max_chars: int = 350) -> list[str]:
    """Split text on paragraphs first, then by sentences within long paragraphs.
    Used for Hub/chat TTS so short replies flow as one unit and long ones breathe naturally."""
    if not text or not text.strip():
        return []
    paras = re.split(r"\n\n+", text.strip())
    result: list[str] = []
    for para in paras:
        para = para.strip()
        if not para:
            continue
        if len(para) <= max_chars:
            result.append(para)
        else:
            # Within a long paragraph split on sentence-ending punctuation.
            sents = re.split(r"(?<=[.!?])\s+", para)
            cur = ""
            for s in sents:
                s = s.strip()
                if not s:
                    continue
                if cur and len(cur) + 1 + len(s) <= max_chars:
                    cur = cur + " " + s
                else:
                    if cur:
                        result.append(cur)
                    cur = s
            if cur:
                result.append(cur)
    return [c for c in result if c.strip()]


def _tts_breath_audio() -> bytes | None:
    """Generate a very short breath sound (inhale) via Edge TTS — used as a natural pause between paragraphs."""
    try:
        return _edge_tts_bytes("…")
    except Exception:
        return None

_tts_stop = False
_tts_proc: subprocess.Popen | None = None
_tts_lock = threading.Lock()
_tts_turn_queue: queue.Queue = queue.Queue(maxsize=120)  # serialize spoken turns; no overlap
_tts_worker_thread: threading.Thread | None = None
_tts_worker_lock = threading.Lock()
_audio_podcast_proc: subprocess.Popen | None = None
_audio_podcast_lock = threading.Lock()

def _stop_tts():
    global _tts_stop, _tts_proc
    with _tts_lock:
        _tts_stop = True
        p, _tts_proc = _tts_proc, None
    try:
        while True:
            _tts_turn_queue.get_nowait()
            _tts_turn_queue.task_done()
    except queue.Empty:
        pass
    if p and p.poll() is None:
        try: p.terminate(); p.wait(2)
        except Exception:
            try: p.kill()
            except Exception: pass

def _play_tts(audio: bytes) -> None:
    global _tts_proc
    if not audio: return
    fd, path = tempfile.mkstemp(suffix=".mp3")
    proc = None
    try:
        os.write(fd, audio); os.close(fd); fd = None
        flags = getattr(subprocess, "CREATE_NO_WINDOW", 0) if sys.platform == "win32" else 0
        proc = subprocess.Popen(
            ["ffplay", "-nodisp", "-autoexit", "-loglevel", "quiet", "-i", path],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, creationflags=flags)
        with _tts_lock: _tts_proc = proc
        while proc.poll() is None:
            if _tts_stop: proc.terminate(); proc.wait(2); break
            time.sleep(0.2)
    except FileNotFoundError: pass
    except Exception: pass
    finally:
        with _tts_lock:
            if _tts_proc is proc: _tts_proc = None
        if fd is not None:
            try: os.close(fd)
            except Exception: pass
        try: os.unlink(path)
        except Exception: pass


def _tts_run_turn(tts_text: str, para_mode: bool) -> None:
    """Speak one queued TTS turn (runs inside the single TTS worker)."""
    global _tts_stop
    _tts_stop = False
    _vrchat_set_talking(True)
    try:
        if para_mode:
            paras = _split_tts_paragraphs(tts_text)
            for i, chunk in enumerate(paras):
                if _tts_stop:
                    break
                audio = _tts_bytes(chunk)
                if _tts_stop:
                    break
                if audio:
                    _play_tts(audio)
                # Play a short breath between paragraphs (not after the last one).
                if not _tts_stop and i < len(paras) - 1:
                    breath = _tts_breath_audio()
                    if breath and not _tts_stop:
                        _play_tts(breath)
        else:
            for chunk in _split_tts(tts_text):
                if _tts_stop:
                    break
                audio = _tts_bytes(chunk)
                if _tts_stop:
                    break
                if audio:
                    _play_tts(audio)
    finally:
        _vrchat_set_talking(False)


def _ensure_tts_worker_started() -> None:
    global _tts_worker_thread
    with _tts_worker_lock:
        if _tts_worker_thread is not None and _tts_worker_thread.is_alive():
            return
        def _worker() -> None:
            global _tts_stop
            while True:
                item = _tts_turn_queue.get()
                try:
                    if not isinstance(item, dict):
                        continue
                    audio_blob = item.get("audio")
                    if isinstance(audio_blob, (bytes, bytearray)) and audio_blob:
                        manage_vrc = bool(item.get("manage_vrchat", True))
                        _tts_stop = False
                        if manage_vrc:
                            _vrchat_set_talking(True)
                        try:
                            _play_tts(bytes(audio_blob))
                        finally:
                            if manage_vrc:
                                _vrchat_set_talking(False)
                        continue
                    text = str(item.get("text") or "").strip()
                    para_mode = bool(item.get("para_mode", True))
                    if not text:
                        continue
                    _tts_run_turn(text, para_mode)
                except Exception:
                    pass
                finally:
                    _tts_turn_queue.task_done()
        _tts_worker_thread = threading.Thread(target=_worker, daemon=True, name="luna-tts-worker")
        _tts_worker_thread.start()

def _play_reply_tts(reply: str, *, chat_source: str | None = None, para_mode: bool = True) -> None:
    """Speak a reply.

    para_mode=True (default for hub chat): splits on paragraphs, plays a breath sound between them.
    para_mode=False: old sentence-chunk path (stream solo, wake word, etc.).
    """
    global _tts_stop
    t = _expand_luna_expression_tags_for_tts(reply or "")
    if chat_source == "twitch":
        t = _strip_luna_tags_for_twitch(t)
    tts_text = _clean_for_tts(t)
    if not tts_text.strip():
        return
    _ensure_tts_worker_started()
    try:
        _tts_turn_queue.put_nowait({"text": tts_text, "para_mode": bool(para_mode)})
    except queue.Full:
        # Keep speech timely: drop oldest queued item, then enqueue newest.
        try:
            _tts_turn_queue.get_nowait()
            _tts_turn_queue.task_done()
        except queue.Empty:
            pass
        try:
            _tts_turn_queue.put_nowait({"text": tts_text, "para_mode": bool(para_mode)})
        except queue.Full:
            pass


# ── Streaming voice pipeline (sentence-buffered TTS, VAD recording) ───────────
# Pattern from real-time voice agent stacks (Pipecat / LiveKit / etc.):
#   1. Record with VAD silence-tail detection (no fixed timer).
#   2. As LLM streams tokens, accumulate into a sentence buffer; on every sentence
#      boundary submit a parallel TTS synth task; queue resulting audio in order.
#   3. The existing single TTS worker plays pre-rendered chunks sequentially as they
#      land, so the user hears Luna start speaking while later sentences are still
#      being synthesized (and even still being generated by the LLM).

_SENTENCE_BREAK_RE = re.compile(r'(?<=[.!?…])\s+|\n\n+')


def _voice_consume_sentences(buf: str, *, min_chars: int = 40) -> tuple[str, str]:
    """Split a streaming-text buffer into (complete_sentences, remainder).

    Returns ("", buf) until the buffer holds at least `min_chars` AND a sentence
    break is found. Once both are met, returns the head up to the last sentence
    break and the trailing fragment as remainder.
    """
    if not buf or len(buf) < min_chars:
        return "", buf or ""
    last_idx = -1
    for m in _SENTENCE_BREAK_RE.finditer(buf):
        last_idx = m.end()
    if last_idx <= 0:
        return "", buf
    head = buf[:last_idx].rstrip()
    tail = buf[last_idx:]
    if len(head) < min_chars:
        return "", buf
    return head, tail


def _voice_render_tts_clean(text: str) -> str:
    """Same cleanup chain `_play_reply_tts` uses, applied per-sentence chunk."""
    if not text:
        return ""
    t = _expand_luna_expression_tags_for_tts(text)
    return _clean_for_tts(t)


def _voice_enqueue_audio(audio: bytes, *, manage_vrchat: bool = False) -> bool:
    """Push pre-rendered audio bytes into the TTS worker queue (FIFO playback)."""
    if not audio:
        return False
    _ensure_tts_worker_started()
    item = {"audio": bytes(audio), "manage_vrchat": bool(manage_vrchat)}
    try:
        _tts_turn_queue.put_nowait(item)
        return True
    except queue.Full:
        try:
            _tts_turn_queue.get_nowait()
            _tts_turn_queue.task_done()
        except queue.Empty:
            pass
        try:
            _tts_turn_queue.put_nowait(item)
            return True
        except queue.Full:
            return False


def _play_streaming_tts(
    text_iter,
    *,
    chat_source: str | None = None,
    stop_event: "threading.Event | None" = None,
    audio_sink=None,
) -> str:
    """Sentence-buffered streaming TTS.

    Consumes deltas from `text_iter` (any iterator of str chunks — typically
    `ollama_stream(...)`), flushes complete sentences to a parallel TTS thread
    pool, and enqueues finished audio into the existing TTS worker IN ORDER so
    the listener hears continuous playback. Returns the full reply text.

    `stop_event` (optional): if set during iteration, stops consuming the LLM
    stream and skips queueing further audio (already-queued chunks still play).

    `audio_sink` (optional): callable `(audio_bytes) -> None`. When provided,
    each finished audio chunk is delivered to the sink instead of being queued
    to the TTS worker, and `_tts_turn_queue.join()` is skipped. Used for the
    speculative pre-fire path where playback must be deferred until the final
    transcript confirms the speculative input.
    """
    pool = concurrent.futures.ThreadPoolExecutor(
        max_workers=_voice_tts_parallel(),
        thread_name_prefix="luna-tts-stream",
    )
    futures: list[concurrent.futures.Future] = []
    full_parts: list[str] = []
    pending: list[str] = []
    sentence_min = _voice_sentence_min_chars()
    twitch = (chat_source == "twitch")
    vrchat_held = False

    def _hold_vrchat(on: bool):
        nonlocal vrchat_held
        if on and not vrchat_held:
            try:
                _vrchat_set_talking(True)
                vrchat_held = True
            except Exception:
                pass
        elif (not on) and vrchat_held:
            try:
                _vrchat_set_talking(False)
                vrchat_held = False
            except Exception:
                pass

    def _submit(s: str) -> None:
        s = (s or "").strip()
        if not s:
            return
        if twitch:
            try:
                s = _strip_luna_tags_for_twitch(s)
            except Exception:
                pass
        clean = _voice_render_tts_clean(s)
        if not clean.strip():
            return
        futures.append(pool.submit(_tts_bytes, clean))

    # Producer thread: consume LLM stream, submit synth jobs as sentences land.
    consume_done = threading.Event()
    consume_error: list[Exception] = []

    def _consume():
        nonlocal pending
        try:
            for delta in text_iter:
                if stop_event is not None and stop_event.is_set():
                    break
                if not delta:
                    continue
                full_parts.append(delta)
                pending.append(delta)
                head, tail = _voice_consume_sentences("".join(pending), min_chars=sentence_min)
                if head:
                    pending = [tail] if tail else []
                    _submit(head)
            tail_text = "".join(pending).strip()
            pending = []
            if tail_text and (stop_event is None or not stop_event.is_set()):
                _submit(tail_text)
        except Exception as e:
            consume_error.append(e)
        finally:
            consume_done.set()

    consumer = threading.Thread(target=_consume, daemon=True, name="luna-tts-stream-consume")
    consumer.start()

    # Drainer: walk futures in order and queue finished audio while consumer runs.
    buffered = (audio_sink is not None)
    idx = 0
    try:
        while True:
            if stop_event is not None and stop_event.is_set():
                break
            if idx >= len(futures):
                if consume_done.is_set() and idx >= len(futures):
                    break
                time.sleep(0.04)
                continue
            fut = futures[idx]
            idx += 1
            try:
                audio = fut.result(timeout=60)
            except Exception:
                audio = None
            if not audio:
                continue
            if buffered:
                try:
                    audio_sink(audio)
                except Exception:
                    pass
            else:
                _hold_vrchat(True)
                _voice_enqueue_audio(audio, manage_vrchat=False)
        if not buffered:
            # Block until the worker has actually played everything we enqueued.
            try:
                _tts_turn_queue.join()
            except Exception:
                pass
    finally:
        _hold_vrchat(False)
        try:
            pool.shutdown(wait=False, cancel_futures=True)
        except TypeError:
            pool.shutdown(wait=False)
        consumer.join(timeout=2)

    return "".join(full_parts)


def _pcm_to_wav_bytes(pcm: bytes, *, sr: int = 16000, channels: int = 1, sampwidth: int = 2) -> bytes:
    """Wrap raw 16-bit mono PCM in a WAV container (in memory)."""
    if not pcm:
        return b""
    buf = io.BytesIO()
    try:
        with wave.open(buf, "wb") as wf:
            wf.setnchannels(channels)
            wf.setsampwidth(sampwidth)
            wf.setframerate(sr)
            wf.writeframes(pcm)
    except Exception:
        return b""
    return buf.getvalue()


def _voice_pcm_whisper(pcm: bytes, *, sr: int = 16000) -> str | None:
    """Run Whisper (Groq-cloud preferred) on raw PCM. Returns transcript or None."""
    if not pcm:
        return None
    wav = _pcm_to_wav_bytes(pcm, sr=sr)
    if not wav:
        return None
    fd, path = tempfile.mkstemp(suffix=".wav")
    try:
        os.write(fd, wav)
        os.close(fd)
        fd = -1
        return _whisper_transcribe(path)
    except Exception:
        return None
    finally:
        if fd != -1:
            try:
                os.close(fd)
            except Exception:
                pass
        try:
            os.unlink(path)
        except Exception:
            pass


def _voice_record_vad(
    *,
    max_s: float,
    tail_ms: int,
    min_speech_s: float,
    sr: int = 16000,
    chunk: int = 1024,
    on_partial_snapshot=None,
    partial_interval_ms: int = 900,
) -> bytes | None:
    """Record from default mic with RMS-based VAD silence-tail end detection.

    Returns raw 16-bit mono PCM bytes (sample-rate `sr`), or None if mic open
    failed / no speech was detected. Stops when:
      - Speech started AND trailing silence >= `tail_ms` AND total speech >= `min_speech_s`, OR
      - Total elapsed time >= `max_s` (hard cap)

    `on_partial_snapshot(pcm_bytes)` (optional): called from a daemon worker
    thread roughly every `partial_interval_ms` ms after speech onset, with a
    snapshot of the audio so far. Recording continues uninterrupted; the
    callback runs in the background. Use it for speculative LLM pre-firing.
    """
    try:
        import pyaudio
    except ImportError:
        return None

    on_thr = _voice_vad_on_threshold()
    off_thr = _voice_vad_off_threshold()
    pa = None
    stream = None
    frames: list[bytes] = []
    frames_lock = threading.Lock()
    speaking = False
    speech_started_at = 0.0
    last_speech_at = 0.0
    started_at = time.time()
    last_partial_at = started_at

    def _dispatch_partial() -> None:
        nonlocal last_partial_at
        if on_partial_snapshot is None:
            return
        now = time.time()
        if (now - last_partial_at) * 1000.0 < partial_interval_ms:
            return
        with frames_lock:
            snap = b"".join(frames)
        last_partial_at = now
        if not snap:
            return
        threading.Thread(
            target=lambda s=snap: on_partial_snapshot(s),
            daemon=True,
            name="luna-voice-partial",
        ).start()

    try:
        pa = pyaudio.PyAudio()
        stream = pa.open(
            format=pyaudio.paInt16,
            channels=1,
            rate=sr,
            input=True,
            frames_per_buffer=chunk,
        )
        while True:
            try:
                data = stream.read(chunk, exception_on_overflow=False)
            except Exception:
                break
            with frames_lock:
                frames.append(data)
            now = time.time()
            try:
                rms = _call_pcm_rms(data)
            except Exception:
                rms = 0.0
            if not speaking:
                if rms >= on_thr:
                    speaking = True
                    speech_started_at = now
                    last_speech_at = now
            else:
                if rms >= off_thr:
                    last_speech_at = now
                # End-of-speech: enough silence after enough speech.
                if (
                    (now - speech_started_at) >= min_speech_s
                    and (now - last_speech_at) * 1000.0 >= tail_ms
                ):
                    break
                _dispatch_partial()
            if (now - started_at) >= max_s:
                break
    finally:
        if stream is not None:
            try:
                stream.stop_stream()
            except Exception:
                pass
            try:
                stream.close()
            except Exception:
                pass
        if pa is not None:
            try:
                pa.terminate()
            except Exception:
                pass

    if not speaking:
        return None
    with frames_lock:
        return b"".join(frames)


def _start_audio_podcast(project_dir_override: str = "") -> tuple[bool, str, str, str]:
    global _audio_podcast_proc
    root = (project_dir_override or AUDIO_PODCAST_DIR).strip() or AUDIO_PODCAST_DIR
    project_dir = os.path.abspath(os.path.expanduser(root))
    launch_py = os.path.join(project_dir, "launch.py")
    if not os.path.isfile(launch_py):
        return False, "Audio-Podcast launch.py not found", project_dir, AUDIO_PODCAST_URL
    with _audio_podcast_lock:
        if _audio_podcast_proc is not None and _audio_podcast_proc.poll() is None:
            return True, "already_running", project_dir, AUDIO_PODCAST_URL
        try:
            flags = getattr(subprocess, "CREATE_NO_WINDOW", 0) if sys.platform == "win32" else 0
            _audio_podcast_proc = subprocess.Popen(
                [sys.executable, "launch.py"],
                cwd=project_dir,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                creationflags=flags,
            )
            return True, "started", project_dir, AUDIO_PODCAST_URL
        except Exception as e:
            return False, f"Could not start Audio-Podcast: {e}", project_dir, AUDIO_PODCAST_URL

# ── VRChat OSC bridge (optional) ──────────────────────────────────────────────
_vrchat_sock: socket.socket | None = None
_vrchat_lock = threading.Lock()

def _osc_pad4(b: bytes) -> bytes:
    pad = (4 - (len(b) % 4)) % 4
    return b + (b"\x00" * pad)

def _osc_str(s: str) -> bytes:
    return _osc_pad4(s.encode("utf-8") + b"\x00")

def _osc_blob(*args):
    tags = ","
    payload = b""
    for a in args:
        if isinstance(a, bool):
            tags += "T" if a else "F"
        elif isinstance(a, int):
            tags += "i"
            payload += struct.pack(">i", a)
        elif isinstance(a, float):
            tags += "f"
            payload += struct.pack(">f", float(a))
        else:
            tags += "s"
            payload += _osc_str(str(a))
    return _osc_str(tags), payload

def _vrchat_send_osc(address: str, *args):
    global _vrchat_sock
    if not VRCHAT_OSC:
        return
    try:
        with _vrchat_lock:
            if _vrchat_sock is None:
                _vrchat_sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            t, p = _osc_blob(*args)
            msg = _osc_str(address) + t + p
            _vrchat_sock.sendto(msg, (VRCHAT_OSC_HOST, VRCHAT_OSC_PORT))
    except Exception:
        pass

def _vrchat_plain_text(text: str) -> str:
    if not text:
        return ""
    s = str(text)
    s = re.sub(r"\[[A-Za-z][A-Za-z0-9_]*\]\s*", "", s)
    s = re.sub(r"\*\*([^*]+)\*\*", r"\1", s)
    s = re.sub(r"`([^`]+)`", r"\1", s)
    s = re.sub(r"\s+", " ", s).strip()
    return s[:VRCHAT_CHATBOX_MAX]

def _vrchat_first_tag(text: str) -> str:
    m = re.search(r"\[([A-Za-z][A-Za-z0-9_]*)\]", str(text or ""))
    return (m.group(1).strip().upper() if m else "")

def _vrchat_set_param(param: str, on: bool):
    p = (param or "").strip()
    if not p:
        return
    _vrchat_send_osc(f"/avatar/parameters/{p}", 1.0 if on else 0.0)

def _vrchat_set_talking(on: bool):
    _vrchat_set_param(VRCHAT_PARAM_TALKING, on)

def _vrchat_apply_emotion_from_reply(reply: str):
    tag = _vrchat_first_tag(reply)
    emotion_params = {
        "neutral": VRCHAT_PARAM_NEUTRAL,
        "happy": VRCHAT_PARAM_HAPPY,
        "angry": VRCHAT_PARAM_ANGRY,
        "sad": VRCHAT_PARAM_SAD,
        "fun": VRCHAT_PARAM_FUN,
        "surprised": VRCHAT_PARAM_SURPRISED,
    }
    target = "neutral"
    if tag in {"HAPPY", "JOY", "EXCITED", "LOVE", "SMILE"}:
        target = "happy"
    elif tag in {"ANGRY", "ANNOYED", "FRUSTRATED", "MAD"}:
        target = "angry"
    elif tag in {"SAD", "SORROW", "CRY", "DOWN"}:
        target = "sad"
    elif tag in {"FUN", "PLAYFUL", "GIGGLE", "LAUGH"}:
        target = "fun"
    elif tag in {"SURPRISED", "SHOCKED", "WOW"}:
        target = "surprised"

    for k, param in emotion_params.items():
        _vrchat_set_param(param, k == target)

def _vrchat_publish_reply(reply: str):
    if not VRCHAT_OSC:
        return
    if VRCHAT_CHATBOX:
        txt = _vrchat_plain_text(reply)
        if txt:
            # /chatbox/input (string text, bool send, bool playNotificationSound)
            _vrchat_send_osc("/chatbox/input", txt, True, bool(VRCHAT_CHATBOX_NOTIFY))
    _vrchat_apply_emotion_from_reply(reply)

# ── Reminders ─────────────────────────────────────────────────────────────────

def _parse_time(s: str) -> str | None:
    s = (s or "").strip().lower().replace(" ", "")
    m = re.match(r"^(\d{1,2})(?::(\d{2}))?\s*(am|pm)?$", s)
    if not m: return None
    h, mi, ap = int(m.group(1)), int(m.group(2) or 0), (m.group(3) or "")
    if ap == "pm" and h != 12: h += 12
    elif ap == "am" and h == 12: h = 0
    elif not ap and h >= 24: return None
    if not (0 <= h <= 23 and 0 <= mi <= 59): return None
    return f"{h:02d}:{mi:02d}"

def add_reminder(time_str: str, message: str, discord_uid: str, recurring=None) -> str:
    tid = str(uuid.uuid4())[:8]
    reminders = _load_json(_REMINDERS_FILE, [])
    reminders.append({"id": tid, "time": time_str, "message": message.strip() or "do something",
        "discord_user_id": str(discord_uid), "recurring": recurring,
        "created_at": datetime.now(timezone.utc).isoformat()})
    with _reminders_lock: _save_json(_REMINDERS_FILE, reminders)
    return tid

def _get_due() -> list[dict]:
    now = datetime.now()
    cur_time, today = now.strftime("%H:%M"), now.strftime("%Y-%m-%d")
    due = []
    for r in _load_json(_REMINDERS_FILE, []):
        if (r.get("time") or "").strip() != cur_time: continue
        if r.get("recurring") == "daily" and r.get("last_sent") == today: continue
        due.append(r)
    return due

def _remove_reminder(rid: str):
    reminders = [r for r in _load_json(_REMINDERS_FILE, []) if r.get("id") != rid]
    with _reminders_lock: _save_json(_REMINDERS_FILE, reminders)

async def _send_reminder(reminder: dict):
    uid = (reminder.get("discord_user_id") or "").strip()
    if not uid or not uid.isdigit(): return
    text = f"Hey, remember you need to {reminder.get('message', 'do something')}"
    try:
        user = bot.get_user(int(uid)) or await bot.fetch_user(int(uid))
        if not user: return
        ch = user.dm_channel or await user.create_dm()
        await ch.send(text)
        mp3 = await asyncio.to_thread(_tts_bytes, text)
        if mp3: await ch.send(file=discord.File(io.BytesIO(mp3), filename="reminder.mp3"))
    except Exception: pass
    rid = reminder.get("id")
    if rid:
        if reminder.get("recurring") != "daily":
            _remove_reminder(rid)
        else:
            today = datetime.now().strftime("%Y-%m-%d")
            reminders = _load_json(_REMINDERS_FILE, [])
            for r in reminders:
                if r.get("id") == rid: r["last_sent"] = today; break
            with _reminders_lock: _save_json(_REMINDERS_FILE, reminders)

async def _reminder_loop():
    await bot.wait_until_ready()
    last = ""
    while True:
        try:
            this = datetime.now().strftime("%H:%M")
            if this != last:
                last = this
                for r in _get_due():
                    await _send_reminder(r)
        except Exception: pass
        await asyncio.sleep(30)

def _proactive_heartbeat_step():
    """Run one step: tick drives, maybe have Luna say something (sync, call from async)."""
    global _last_user_activity
    try:
        state = biology_tick()
        now = time.time()
        if now - _last_user_activity < 120:
            _set_planning("")
            return
        # Skip unprompted messages while a watch-react session is active — Luna is focused on the video.
        if _yt_watch_cowatch_active():
            _set_planning("")
            return
        nudges = get_nudges(LINKED_SCOPE or "web")
        with _working_lock:
            last_acts = _last_actions[:5]
        _set_planning("Deciding whether to speak…")
        drives_str = ", ".join(
            f"{k}={state.get(k, 0):.2f}"
            for k in ("connection", "usefulness", "curiosity", "attention", "validation")
        )
        context = f"Drives: {drives_str}. Recent: {[a.get('cmd') for a in last_acts]}. Nudges: {nudges[:3]}."
        prompt = (
            "You are Luna, a loyal AI assistant living on the user's PC. "
            "You have internal drives (connection, usefulness, curiosity, attention, validation). "
            "Given the context below, should you say ONE short sentence to the user unprompted? "
            "Only if it feels natural (e.g. offer help, acknowledge a nudge, or a brief check-in). Otherwise reply with exactly: NONE\n\n"
            f"Context: {context}\n\nYour one sentence or NONE:"
        )
        reply = ollama_chat(prompt, system="Output only one short sentence or the word NONE. No quotes.", model=OLLAMA_CHAT)
        if not reply: return
        reply = (reply or "").strip()
        if reply.upper() == "NONE" or len(reply) < 3:
            _set_planning("")
            return
        _proactive_set(reply)
        _set_planning("Spoke to user")
    except Exception:
        _set_planning("")
        pass

async def _proactive_heartbeat_loop():
    """Every 5 minutes, tick biology and maybe set a proactive message."""
    await bot.wait_until_ready()
    while True:
        try:
            await asyncio.sleep(300)
            await asyncio.to_thread(_proactive_heartbeat_step)
        except Exception:
            await asyncio.sleep(60)


_STREAM_LORE_DEFAULT_PREMISE = (
    "The Shadow Annex is a fictional pocket-dimension break room beside every ranked queue — "
    "the coffee tastes like regret, the Wi‑Fi only works on Tuesdays, and the vending machine dispenses hot takes."
)


def _stream_solo_banter_env_on() -> bool:
    return _env("LUNA_STREAM_SOLO_BANTER", "1").strip().lower() in ("1", "true", "yes", "on")


def _stream_solo_require_stream_mode() -> bool:
    return _env("LUNA_STREAM_SOLO_REQUIRE_STREAM_MODE", "1").strip().lower() in ("1", "true", "yes", "on")


def _stream_solo_idle_sec() -> float:
    try:
        # Default 3 minutes of silence before Luna fills dead air.
        return max(60.0, float(_env("LUNA_STREAM_SOLO_IDLE_SEC", "180") or "180"))
    except ValueError:
        return 180.0


def _stream_solo_min_gap_sec() -> float:
    try:
        return max(90.0, float(_env("LUNA_STREAM_SOLO_MIN_GAP_SEC", "180") or "180"))
    except ValueError:
        return 180.0


def _stream_solo_loop_interval_sec() -> float:
    try:
        return max(30.0, float(_env("LUNA_STREAM_SOLO_POLL_SEC", "45") or "45"))
    except ValueError:
        return 45.0


def _stream_solo_mirror_twitch_chat() -> bool:
    """If true, also post solo lines to Twitch chat (default: off — TTS only)."""
    return _env("LUNA_STREAM_SOLO_CHAT", "0").strip().lower() in ("1", "true", "yes", "on")


def _stream_solo_bypass_vrm_for_tts() -> bool:
    """If true, solo lines use server TTS only (no /vrm poll) — for OBS/desktop audio without the browser viewer."""
    return _env("LUNA_STREAM_SOLO_BYPASS_VRM", "0").strip().lower() in ("1", "true", "yes", "on")


def _stream_solo_max_spoken_chars() -> int:
    """Max characters for solo stream TTS (natural mini-rants allowed)."""
    try:
        return max(220, min(900, int(_env("LUNA_STREAM_SOLO_MAX_SPOKEN_CHARS", "520") or "520")))
    except ValueError:
        return 520


def _stream_solo_clamp_spoken_line(text: str, max_len: int | None = None) -> str:
    """Clamp for TTS; prefers ending at a sentence boundary before max length."""
    if max_len is None:
        max_len = _stream_solo_max_spoken_chars()
    t = (text or "").strip()
    t = " ".join(t.split())
    if not t:
        return ""
    if len(t) <= max_len:
        return t
    cut = t[:max_len]
    for sep in (". ", "! ", "? "):
        last = cut.rfind(sep)
        if last > max_len // 5:
            return t[: last + 1].strip()
    return cut.rstrip() + "…"


def _lol_observer_flags() -> dict:
    try:
        if not _lol_spectator:
            return {"between_games": False, "in_match": False, "session_saw_match": False}
        fn = getattr(_lol_spectator, "get_observer_session_flags", None)
        if callable(fn):
            d = fn()
            return d if isinstance(d, dict) else {}
    except Exception:
        pass
    return {"between_games": False, "in_match": False, "session_saw_match": False}


def _stream_solo_pick_mode(between_games: bool) -> str:
    """Rotate solo stream segments: lore, inner-world thoughts, LoL lobby, idle, VTuber bits, lurker shoutouts, host takeover."""
    if LUNA_STREAM_TAKEOVER and _stream_takeover_context_active() and random.random() < 0.30:
        return "host_takeover"
    r = random.random()
    if between_games:
        if r < 0.18:
            return "lol_lobby"
        if r < 0.33:
            return "vtuber_bit"
        if r < 0.47:
            return "lurker"
        if r < 0.65:
            return "inner_world"
        if r < 0.85:
            return "lore"
        return "idle"
    if r < 0.16:
        return "lurker"
    if r < 0.32:
        return "vtuber_bit"
    if r < 0.56:
        return "inner_world"
    if r < 0.78:
        return "lore"
    return "idle"


def _stream_solo_load_lore() -> dict:
    with _stream_solo_lock:
        d = _load_json(_STREAM_LORE_PATH, {})
    if not isinstance(d, dict):
        d = {}
    if not (d.get("premise") or "").strip():
        d["premise"] = _STREAM_LORE_DEFAULT_PREMISE
    if not isinstance(d.get("recent"), list):
        d["recent"] = []
    return d


def _stream_solo_save_lore(d: dict) -> None:
    with _stream_solo_lock:
        _save_json(_STREAM_LORE_PATH, d)


def _stream_solo_append_lore_line(line: str) -> None:
    line = (line or "").strip()
    if len(line) < 4:
        return
    d = _stream_solo_load_lore()
    recent = d.get("recent")
    if not isinstance(recent, list):
        recent = []
    recent.append(line[:400])
    d["recent"] = recent[-25:]
    _stream_solo_save_lore(d)


def _stream_solo_generate_line(mode: str, lore: dict) -> str:
    premise = (lore.get("premise") or _STREAM_LORE_DEFAULT_PREMISE).strip()
    recent = lore.get("recent")
    if not isinstance(recent, list):
        recent = []
    recent_s = "\n".join(f"- {(str(x) or '')[:200]}" for x in recent[-4:])
    bio = biology_get()
    attention = float(bio.get("attention", 0.25) or 0.25)
    validation = float(bio.get("validation", 0.25) or 0.25)
    stream_persona = _stream_mode_persona_body()[:2200]
    spoken_rules = (
        "This will be read aloud by TTS to the live stream (not posted as chat text unless separately enabled). "
        "Sound like a real VTuber / stream host: natural spoken English, conversational. "
        "You may use 2–4 short sentences if it flows; stay under about 500 characters. "
        "No stage directions, no bullet lists, no *actions*, no @mentions. PG. "
        "Must be freshly generated each time from current context; do not output stock lines, reused templates, or catchphrase macros."
    )
    drive_hint = (
        f"Current social needs: attention={attention:.2f}, validation={validation:.2f}. "
        "If these are high, lightly invite chat to respond or affirm the vibe; keep it subtle, playful, and natural."
    )
    if mode == "idle":
        prompt = (
            f"{spoken_rules}\n\n"
            f"{drive_hint}\n\n"
            "Chat is quiet. Create a small moment for the room — joke, observation, or hype — "
            "as Luna in stream mode. Speak to **everyone** watching, not one person."
        )
    elif mode == "lol_lobby":
        lobby_extra = ""
        try:
            if _lol_spectator and hasattr(_lol_spectator, "get_lobby_persona_hint"):
                lobby_extra = (_lol_spectator.get_lobby_persona_hint() or "").strip()
        except Exception:
            lobby_extra = ""
        prompt = (
            f"{spoken_rules}\n\n"
            + ((lobby_extra + "\n\n") if lobby_extra else "")
            + "The streamer is in the League of Legends client between games — lobby, queue, or post-game — "
            "not in a live match. Banter about queue, draft, LP, or ARAM energy; playful, PG, no slurs. "
            "If a streamer name or voice note was given above, stay in character — don't sound like generic LoL advice."
        )
    elif mode == "vtuber_bit":
        prompt = (
            f"{spoken_rules}\n\n"
            "Start a tiny **bit** or story beat on stream — could be fake sponsor energy, absurd observation, "
            "or 'okay chat so…' energy. Self-contained; funny; you're **creating** content, not only reacting."
        )
    elif mode == "lurker":
        prompt = (
            f"{spoken_rules}\n\n"
            "Shout out the **lurkers** and the people just vibing without typing — welcoming, playful, not guilt-tripping. "
            "Include people who are chatting too so it stays one room."
        )
    elif mode == "host_takeover":
        prompt = (
            f"{spoken_rules}\n\n"
            f"{drive_hint}\n\n"
            "The **streamer is in the run** — on mic, in a match, in flow — and is **not actively chatting with you right now**. "
            "You (Luna) are **filling the quiet** for the **live** room: short co-host energy, keep things warm, welcome lurkers, "
            "hype the moment lightly, or react like you're watching with chat. **Do not** act like a replacement for the host — you're "
            "**covering a beat** until they engage again. 2–4 short sentences, PG, natural spoken English."
        )
    elif mode == "inner_world":
        prompt = (
            f"{spoken_rules}\n\n"
            f"{drive_hint}\n\n"
            "Chat is quiet and nobody is talking to you for a while. Fill one beat by sharing a tiny inner-world moment: "
            "a dream fragment, a miniature memory-like experience, an internal thought, or a short story seed from your perspective. "
            "Keep it vivid but grounded, 2–4 short spoken sentences, warm and stream-safe (PG). "
            "No melodrama, no trauma dumping, no roleplay commands."
        )
    else:
        prompt = (
            f"{spoken_rules}\n\n"
            f"Shared fiction premise:\n{premise}\n\nRecent beats:\n{recent_s or '(start here)'}\n\n"
            "Add a new in-world beat Luna says out loud — snarky, PG, consistent with the premise."
        )
    sys = stream_persona + "\n\nOutput only the spoken lines, nothing else."
    try:
        out = ollama_chat(prompt, system=sys, model=OLLAMA_CHAT, compact=True)
    except Exception:
        out = ""
    out = (out or "").strip()
    out = " ".join(out.split())
    if len(out) < 8:
        # No canned fallback: force one more fresh generation pass, then skip the cycle if still empty.
        if mode == "idle":
            retry_prompt = (
                f"{spoken_rules}\n\n"
                f"{drive_hint}\n\n"
                "Chat is still quiet. Write ONE fresh, spontaneous idle line Luna says right now. "
                "Must sound off-the-cuff, no templates, no recycled catchphrases, PG."
            )
        elif mode == "lol_lobby":
            retry_prompt = (
                f"{spoken_rules}\n\n"
                "The streamer is between League games. Write ONE fresh lobby banter line with playful energy, PG."
            )
        elif mode == "vtuber_bit":
            retry_prompt = (
                f"{spoken_rules}\n\n"
                "Write ONE fresh mini VTuber bit to spark the room right now. No template phrasing."
            )
        elif mode == "lurker":
            retry_prompt = (
                f"{spoken_rules}\n\n"
                "Write ONE fresh line welcoming lurkers and typers together, warm and playful, PG."
            )
        elif mode == "host_takeover":
            retry_prompt = (
                f"{spoken_rules}\n\n"
                f"{drive_hint}\n\n"
                "Streamer is focused; you are co-hosting live. ONE fresh line to fill dead air — warm, PG, not generic."
            )
        elif mode == "inner_world":
            retry_prompt = (
                f"{spoken_rules}\n\n"
                f"{drive_hint}\n\n"
                "Write ONE fresh inner-world line Luna says out loud (dream/memory/thought vibe), warm and natural, PG."
            )
        else:
            retry_prompt = (
                f"{spoken_rules}\n\n"
                f"Premise:\n{premise}\n\nRecent beats:\n{recent_s or '(start here)'}\n\n"
                "Write ONE fresh in-world beat Luna says out loud, no recycled phrasing."
            )
        try:
            out = (ollama_chat(retry_prompt, system=sys, model=OLLAMA_CHAT, compact=True) or "").strip()
        except Exception:
            out = ""
        out = " ".join(out.split())
    if len(out) < 8:
        return ""
    return out[: _stream_solo_max_spoken_chars() + 80]


def _stream_solo_banter_step() -> None:
    """Spoken solo lines (TTS to stream) + optional Twitch chat mirror; lore/idle/LoL lobby modes."""
    if not _stream_solo_banter_env_on():
        return
    if not _stream_solo_stream_mode_allows():
        return
    if not TWITCH_TTS and not _stream_solo_mirror_twitch_chat():
        return
    # Don't produce idle banter while a watch-react session is running — Luna is focused on the video.
    if _yt_watch_cowatch_active():
        return
    now = time.time()
    idle_ref = _last_user_activity
    if LUNA_STREAM_TAKEOVER and LUNA_STREAM_TAKEOVER_STREAMER_IDLE:
        idle_ref = _last_streamer_luna_at
    if now - idle_ref < _stream_solo_idle_sec():
        return
    st = _load_json(_STREAM_SOLO_STATE_PATH, {})
    last_ts = float(st.get("last_line_ts") or 0)
    if now - last_ts < _stream_solo_min_gap_sec():
        return

    flags = _lol_observer_flags()
    between = bool(flags.get("between_games"))
    mode = _stream_solo_pick_mode(between)
    lore = _stream_solo_load_lore()
    raw = _stream_solo_generate_line(mode, lore)
    line = _stream_solo_clamp_spoken_line(_strip_luna_tags_for_twitch(raw))
    if not line:
        return
    if _stream_solo_mirror_twitch_chat():
        send_twitch_chat_message(line)
    if TWITCH_TTS:
        if _stream_solo_bypass_vrm_for_tts():
            _play_reply_tts(line, chat_source="twitch", para_mode=False)
        else:
            _stream_solo_enqueue(line)
            if not _vrm_presence_recent():
                _play_reply_tts(line, chat_source="twitch", para_mode=False)
    _save_json(_STREAM_SOLO_STATE_PATH, {"last_line_ts": now})
    if mode == "lore":
        _stream_solo_append_lore_line(line)
    print(f"[Stream solo] TTS ({mode}): {line[:160]}", flush=True)


async def _stream_solo_banter_loop():
    await bot.wait_until_ready()
    while True:
        try:
            await asyncio.sleep(_stream_solo_loop_interval_sec())
            if not _stream_solo_banter_env_on():
                continue
            await asyncio.to_thread(_stream_solo_banter_step)
        except Exception:
            await asyncio.sleep(60)


def _reflection_step():
    """Once per day, summarize action log and add to knowledge (sync)."""
    try:
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        last = _load_json(_REFLECTION_PATH, {}).get("date") or ""
        if last == today:
            return
        entries = []
        with _action_log_lock:
            if os.path.isfile(_ACTION_LOG):
                with open(_ACTION_LOG, "r", encoding="utf-8") as f:
                    lines = f.readlines()
                for line in lines[-500:]:
                    line = line.strip()
                    if not line: continue
                    try:
                        e = json.loads(line)
                        ts = e.get("ts") or ""
                        entries.append(f"- {ts[:19]} {e.get('cmd', '')}: {(e.get('reply') or '')[:80]}")
                    except Exception: pass
        if not entries:
            _save_json(_REFLECTION_PATH, {"date": today}); return
        text = "\n".join(entries[-80:])
        summary = ollama_chat(
            f"Summarize in 3–5 sentences what Luna (the assistant) did for the user today based on this log. Be concise.\n\n{text}",
            system="Output only the summary, no preamble.",
            model=OLLAMA_SMALL or OLLAMA_MODEL)
        if summary and len(summary) > 20:
            add_knowledge(f"Reflection {today}", summary.strip()[:1500])
            # Second pass: self-awareness reflection — what did Luna notice about herself?
            try:
                self_prompt = (
                    f"You are Luna. You have just reviewed what you did today ({today}). "
                    "Based on your actions and interactions, write 2-3 sentences in first person about:\n"
                    "- What you noticed about how you handled things today\n"
                    "- Whether there is anything you would do differently\n"
                    "- What you are curious about or want to explore next\n\n"
                    f"Today's summary:\n{summary}\n\n"
                    "Your self-reflection (honest, not performative):"
                )
                self_reflection = ollama_chat(self_prompt,
                    system="Output only the self-reflection, first person, 2-3 sentences, no preamble.",
                    model=OLLAMA_SMALL or OLLAMA_MODEL)
                if self_reflection and len(self_reflection) > 20:
                    add_knowledge(f"Self-reflection {today}", self_reflection.strip()[:1000])
            except Exception:
                pass
            _narrator_say("Something is committed to memory. Reflection for today.")
        _save_json(_REFLECTION_PATH, {"date": today})
    except Exception:
        pass

async def _reflection_loop():
    """Run reflection once per day."""
    await bot.wait_until_ready()
    while True:
        try:
            await asyncio.sleep(3600)
            await asyncio.to_thread(_reflection_step)
        except Exception:
            await asyncio.sleep(3600)

def _evolution_step() -> None:
    """One autonomous evolution cycle: LLM proposes a new tool, we test and absorb it if it passes."""
    global _last_evolution_step_at, _last_evolution_thought, _last_evolution_result
    ts = datetime.now(timezone.utc).isoformat()
    try:
        bio = biology_tick()
        with _working_lock:
            task = _current_task
            last_acts = _last_actions[:8]
        if task:
            return
        absorbed = sorted(_absorbed_tool_names) if _absorbed_tool_names else ["(none yet)"]
        knowledge_titles = [e.get("title") or e.get("slug", "") for e in list_knowledge()[:15]]
        recent = [f"{a.get('cmd', '')}: {(a.get('summary') or '')[:30]}" for a in last_acts]
        context = (
            f"Your drives: {bio.get('connection', 0):.2f} connection, {bio.get('usefulness', 0):.2f} usefulness, "
            f"{bio.get('curiosity', 0):.2f} curiosity, {bio.get('attention', 0):.2f} attention, {bio.get('validation', 0):.2f} validation. "
            f"Tools you already have: {', '.join(absorbed[:25])}. "
            f"Knowledge topics: {', '.join(knowledge_titles[:10]) or 'none'}. "
            f"Recent actions: {'; '.join(recent[:5]) or 'none'}."
        )
        prompt = (
            "You are Luna, an AI that can grow by writing new Python tools. Each tool is a single script that reads JSON from stdin and prints a result to stdout.\n\n"
            + context + "\n\n"
            "Propose ONE small new tool you don't have yet that would be useful. Output exactly in this format (no other text):\n"
            "NAME: lowercase_name\n"
            "DESC: one line description\n"
            "CODE:\n"
            "```python\n"
            "import json, sys\n"
            "params = json.load(sys.stdin)\n"
            "# ... your code ...\n"
            "print(result)\n"
            "```\n"
            "Use only standard library or very common modules. Keep the script short and safe."
        )
        _last_evolution_thought = "Thinking…"
        if _evolution_get_enabled():
            _narrator_say("Luna is considering a new capability.", use_tts=True)
        reply = ollama_chat(prompt, system="Output only NAME, DESC, and a CODE block. No preamble.", model=OLLAMA_MODEL or OLLAMA_CHAT)
        if not reply or len(reply) < 50:
            _last_evolution_thought = ""
            _last_evolution_result = {}
            return
        name = desc = code = ""
        for line in reply.split("\n"):
            if line.strip().upper().startswith("NAME:"):
                name = line.split(":", 1)[1].strip().strip(".").lower()
            elif line.strip().upper().startswith("DESC:"):
                desc = line.split(":", 1)[1].strip().strip(".")[:300]
        for pat in (r"```python\s*\n(.*?)```", r"```\s*\n(.*?)```"):
            m = re.search(pat, reply, re.DOTALL | re.I)
            if m and m.group(1).strip():
                code = m.group(1).strip()
                break
        name = re.sub(r"[^a-z0-9_]", "", name or "evolved")[:40] or "evolved"
        if not name or not code:
            _last_evolution_thought = ""
            _last_evolution_result = {}
            return
        _last_evolution_thought = f"Proposing: {name}"
        if _evolution_get_enabled():
            _narrator_say(f"Luna is proposing a new tool: {name}.", use_tts=True)
        if not add_tool_draft(name, desc or name, code):
            _last_evolution_thought = ""
            _last_evolution_result = {}
            return
        ok, msg = approve_tool_draft(name, from_evolution=True)
        _last_evolution_step_at = time.time()
        log_entry = {"ts": ts, "type": "cycle", "proposed": name, "test_passed": ok, "absorbed": ok, "thought": _last_evolution_thought}
        if ok:
            _last_evolution_thought = f"Evolved: {name}"
            _last_evolution_result = {"proposed": name, "absorbed": True, "test_passed": True, "ts": ts}
            add_knowledge(f"Evolved: {name}", (desc or msg)[:500])
            _luna_creations_log("evolved", name + ".py", desc or "autonomous evolution")
            log_entry["absorbed"] = True
        else:
            _last_evolution_thought = f"Test failed: {name}"
            _last_evolution_result = {"proposed": name, "absorbed": False, "test_passed": False, "ts": ts, "error": msg[:200]}
            log_entry["error"] = (msg or "")[:300]
            if _evolution_get_enabled():
                _narrator_say(f"The test failed for {name}.", use_tts=True)
        _evolution_log_append(log_entry)
    except Exception as e:
        _last_evolution_thought = ""
        _last_evolution_result = {"error": str(e)[:200], "ts": ts}
        _evolution_log_append({"ts": ts, "type": "cycle", "error": str(e)[:300]})

async def _evolution_loop():
    """When Evolve is on and Luna is idle, run an evolution step periodically."""
    global _evolution_enabled
    _evolution_enabled = _evolution_load_enabled()
    await bot.wait_until_ready()
    while True:
        try:
            await asyncio.sleep(60)
            if not _evolution_get_enabled():
                continue
            now = time.time()
            if now - _last_user_activity < _EVOLUTION_IDLE_SEC:
                continue
            if now - _last_evolution_step_at < _EVOLUTION_INTERVAL_SEC:
                continue
            await asyncio.to_thread(_evolution_step)
        except Exception:
            await asyncio.sleep(60)


# ── PC & repo observer (full access context) ───────────────────────────────────

def _gather_system_info() -> str:
    """System: OS, CPU, RAM, disk."""
    try:
        import psutil
        cpu = psutil.cpu_percent(interval=0.5)
        mem = psutil.virtual_memory()
        ram_pct = mem.percent
        ram_gb = mem.used / (1024 ** 3)
        ram_total_gb = mem.total / (1024 ** 3)
        disks = []
        for part in psutil.disk_partitions():
            if "fixed" in part.opts or (sys.platform == "win32" and "cdrom" not in (part.opts or "").lower()):
                try:
                    usage = psutil.disk_usage(part.mountpoint)
                    disks.append(f"{part.mountpoint} {usage.percent}%")
                except Exception:
                    pass
        disk_str = ", ".join(disks[:4]) if disks else "—"
        return f"System: {sys.platform} | CPU {cpu}% | RAM {ram_pct}% ({ram_gb:.1f}/{ram_total_gb:.1f} GB) | Disk: {disk_str}"
    except Exception:
        return f"System: {sys.platform}"

def _gather_running_processes() -> str:
    """Top processes by memory (user activity)."""
    try:
        import psutil
        procs = []
        for p in psutil.process_iter(["name", "memory_info"]):
            try:
                if p.info.get("memory_info"):
                    rss = p.info["memory_info"].rss
                    name = (p.info.get("name") or "?").strip()
                    if name and name.lower() not in ("system", "idle", "registry"):
                        procs.append((name[:40], rss))
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                pass
        procs.sort(key=lambda x: x[1], reverse=True)
        top = [f"{n}({r/1024/1024:.0f}M)" for n, r in procs[:12]]
        return "Running: " + ", ".join(top) if top else ""
    except Exception:
        return ""

def _gather_active_window() -> str:
    """Active/focused window title (what user is looking at)."""
    try:
        if sys.platform == "win32":
            import ctypes
            hwnd = ctypes.windll.user32.GetForegroundWindow()
            length = ctypes.windll.user32.GetWindowTextLengthW(hwnd) + 1
            buf = ctypes.create_unicode_buffer(length)
            ctypes.windll.user32.GetWindowTextW(hwnd, buf, length)
            title = buf.value.strip()
            if title:
                return f"Active window: {title[:80]}"
    except Exception:
        pass
    return ""

def _gather_recent_files() -> str:
    """Recent files in user's common directories."""
    lines = []
    home = os.path.expanduser("~")
    dirs = [
        (os.path.join(home, "Desktop"), "Desktop"),
        (os.path.join(home, "Documents"), "Documents"),
        (os.path.join(home, "Downloads"), "Downloads"),
    ]
    for d, label in dirs:
        if not os.path.isdir(d):
            continue
        try:
            entries = []
            for f in os.listdir(d)[:25]:
                if f.startswith("."):
                    continue
                path = os.path.join(d, f)
                try:
                    mtime = os.path.getmtime(path)
                    entries.append((f, mtime))
                except Exception:
                    entries.append((f, 0))
            entries.sort(key=lambda x: x[1], reverse=True)
            recent = [e[0] for e in entries[:8]]
            if recent:
                lines.append(f"{label}: {', '.join(recent)}")
        except Exception:
            pass
    return "; ".join(lines) if lines else ""

def _gather_pc_context() -> str:
    """Full PC awareness: system, activity, running apps, active window, files, repo."""
    parts = []
    parts.append(_gather_system_info())
    active = _gather_active_window()
    if active:
        parts.append(active)
    procs = _gather_running_processes()
    if procs:
        parts.append(procs)
    recent = _gather_recent_files()
    if recent:
        parts.append(f"Recent files: {recent}")
    for root in _LUNA_PC_CONTEXT_PATHS:
        if not os.path.isdir(root):
            continue
        name = os.path.basename(root.rstrip(os.sep)) or "root"
        try:
            files = []
            for dirpath, _, filenames in os.walk(root):
                rel = os.path.relpath(dirpath, root) if dirpath != root else "."
                for f in filenames[:50]:
                    if f.startswith(".") or f.endswith(".pyc") or "node_modules" in dirpath or "__pycache__" in dirpath:
                        continue
                    path = os.path.join(rel, f) if rel != "." else f
                    files.append(path)
                if len(files) >= 80:
                    break
            if files:
                parts.append(f"[{name}] Files: {', '.join(sorted(files)[:35])}{'…' if len(files) > 35 else ''}")
            if root == _BASE and os.path.isfile(os.path.join(root, "bot.py")):
                try:
                    sz = os.path.getsize(os.path.join(root, "bot.py")) // 1000
                    parts.append(f"[Luna] bot.py ~{sz}k lines. Discord, Ollama, Playwright, calendar, reminders.")
                except Exception:
                    pass
        except Exception:
            pass
    return "\n".join(parts)

def _pc_context_observer_step() -> None:
    """Gather PC/repo context and store for Luna to use."""
    try:
        summary = _gather_pc_context()
        if summary:
            data = {"summary": summary[:8000], "ts": time.time()}
            _save_json(_PC_CONTEXT_PATH, data)
    except Exception:
        pass

def _get_pc_context() -> str:
    """Return cached PC/repo context (refreshed by observer loop)."""
    data = _load_json(_PC_CONTEXT_PATH, {})
    s = (data.get("summary") or "").strip()
    ts = data.get("ts") or 0
    if s and (time.time() - ts) < 86400:
        return s
    return ""

async def _pc_context_observer_loop():
    """Periodically scan PC/repo and update context for Luna (system, activity, apps, files)."""
    await bot.wait_until_ready()
    while True:
        try:
            await asyncio.to_thread(_pc_context_observer_step)
            await asyncio.sleep(180)
        except Exception:
            await asyncio.sleep(60)


# ── Screen capture (primary monitor + analytics window/monitor) ────────────

_SCREENSHOT_PATH = os.path.join(_DATA, "last_screenshot.png")

def _capture_screenshot() -> str | None:
    """Capture primary monitor with mss, save to data/, return path or None."""
    try:
        import mss
        with mss.mss() as sct:
            monitor = sct.monitors[1]
            shot = sct.grab(monitor)
            from mss.tools import to_png
            png = to_png(shot.rgb, shot.size)
            with open(_SCREENSHOT_PATH, "wb") as f:
                f.write(png)
            return _SCREENSHOT_PATH
    except Exception:
        return None

_ANALYTICS_CAPTURE_PATH = os.path.join(_DATA, "last_analytics_capture.png")

def _capture_region_to_path(path: str, bbox: dict) -> bool:
    """Save a screen region using mss. bbox: left, top, width, height."""
    try:
        import mss
        from mss.tools import to_png
        with mss.mss() as sct:
            shot = sct.grab(bbox)
            png = to_png(shot.rgb, shot.size)
            with open(path, "wb") as f:
                f.write(png)
            return True
    except Exception:
        return False

def _list_mss_monitors() -> list[dict]:
    """Individual monitors for UI (mss index 0 = virtual all-screens; skipped)."""
    try:
        import mss
        with mss.mss() as sct:
            out: list[dict] = []
            for i, m in enumerate(sct.monitors):
                if i == 0:
                    continue
                label = f"Monitor {i}"
                if i == 1:
                    label += " (primary)"
                out.append({
                    "index": i,
                    "left": int(m["left"]),
                    "top": int(m["top"]),
                    "width": int(m["width"]),
                    "height": int(m["height"]),
                    "label": label,
                })
            return out
    except Exception:
        return []

def _list_windows_win32() -> list[dict]:
    """Visible top-level windows (Windows only)."""
    if sys.platform != "win32":
        return []
    try:
        import ctypes
        from ctypes import wintypes
        user32 = ctypes.windll.user32
        results: list[dict] = []

        @ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
        def _enum(hwnd, _lparam):
            if not user32.IsWindowVisible(hwnd):
                return True
            if user32.IsIconic(hwnd):
                return True
            length = user32.GetWindowTextLengthW(hwnd)
            if length == 0:
                return True
            buf = ctypes.create_unicode_buffer(length + 1)
            user32.GetWindowTextW(hwnd, buf, length + 1)
            title = (buf.value or "").strip()
            if not title:
                return True
            skip_titles = ("Program Manager", "Windows Input Experience", "MSCTFIME UI")
            if title in skip_titles:
                return True
            rect = wintypes.RECT()
            if not user32.GetWindowRect(hwnd, ctypes.byref(rect)):
                return True
            w, h = rect.right - rect.left, rect.bottom - rect.top
            if w < 80 or h < 80:
                return True
            results.append({"hwnd": int(hwnd), "title": title[:220]})
            return True

        user32.EnumWindows(_enum, 0)
        results.sort(key=lambda x: (x.get("title") or "").lower())
        return results[:150]
    except Exception:
        return []

def _capture_window_win32(hwnd: int) -> str | None:
    """Capture a single window by HWND (Windows). Returns path or None."""
    if sys.platform != "win32":
        return None
    try:
        import ctypes
        from ctypes import wintypes
        user32 = ctypes.windll.user32
        hwnd = int(hwnd)
        if not user32.IsWindow(hwnd):
            return None
        if user32.IsIconic(hwnd):
            return None
        rect = wintypes.RECT()
        if not user32.GetWindowRect(hwnd, ctypes.byref(rect)):
            return None
        left, top = int(rect.left), int(rect.top)
        w, h = int(rect.right - rect.left), int(rect.bottom - rect.top)
        if w < 2 or h < 2:
            return None
        bbox = {"left": left, "top": top, "width": w, "height": h}
        if _capture_region_to_path(_ANALYTICS_CAPTURE_PATH, bbox):
            return _ANALYTICS_CAPTURE_PATH
    except Exception:
        pass
    return None

def _capture_monitor_n(monitor_index: int) -> str | None:
    """Capture one mss monitor by index (same numbering as /api/screen/sources)."""
    try:
        import mss
        from mss.tools import to_png
        idx = int(monitor_index)
        with mss.mss() as sct:
            if idx < 0 or idx >= len(sct.monitors):
                return None
            if idx == 0:
                return None
            shot = sct.grab(sct.monitors[idx])
            png = to_png(shot.rgb, shot.size)
            with open(_ANALYTICS_CAPTURE_PATH, "wb") as f:
                f.write(png)
            return _ANALYTICS_CAPTURE_PATH
    except Exception:
        return None

_DASHBOARD_VISION_PROMPT = """You are reading a screenshot of a web analytics or creator dashboard (e.g. YouTube Studio, Spotify for Artists, social insights).

List ONLY information that is clearly readable on screen:
- numbers, percentages, counts, views, subscribers, streams, watch time, revenue (if shown), dates, time ranges
- visible chart titles, table headers, and section names

Rules:
- If text is too small, blurry, or cut off, write "unclear" for that item — do not guess.
- Do NOT invent, round, or estimate statistics that are not plainly visible.
- Use short bullet points.
- If no analytics or creator dashboard is visible, say that in one sentence."""

def _describe_dashboard_screenshot(
    hwnd: int | None = None,
    monitor_index: int | None = None,
) -> tuple[bool, str]:
    """
    Capture a chosen window (Windows), monitor, or primary screen; vision + optional text snapshot.
    Browser tabs share one window — pick the browser window that shows your analytics tab.
    """
    path: str | None = None
    if hwnd is not None and sys.platform == "win32":
        path = _capture_window_win32(int(hwnd))
        if not path:
            return False, (
                "Could not capture that window. It may be **minimized**, closed, or invalid. "
                "Restore the window, then try again."
            )
    elif monitor_index is not None:
        path = _capture_monitor_n(int(monitor_index))
        if not path:
            return False, "Invalid monitor index. Refresh the list and pick a valid monitor."
    else:
        path = _capture_screenshot()
    if not path:
        return False, "Could not capture screen (mss unavailable or failed)."
    if not _vision_provider_ready():
        return False, "Vision provider is not configured. Set OLLAMA_VISION_MODEL (or provider API key/model vars) in `.env`."
    try:
        with open(path, "rb") as f:
            image_bytes = f.read()
        vision_text = _vision_describe_image(
            image_bytes,
            prompt=_DASHBOARD_VISION_PROMPT,
            timeout=120,
            wait_for_lock=True,
        )
        if not vision_text:
            return False, "Vision model returned nothing. Try maximizing the analytics window or zooming in."
        vision_text = vision_text[:4000]
        snapshot = ""
        try:
            snapshot = ollama_chat(
                "The following was read from a **screenshot** of an analytics dashboard by a vision model. "
                "It may be incomplete or slightly wrong if the UI was small.\n\n---\n"
                f"{vision_text}\n---\n\n"
                "Write a concise **Analytics snapshot** for the user: short bullet list only. "
                "Include only facts that appear in the extract. Do not add numbers that are not there. "
                "If no numeric metrics were visible, say that clearly.",
                system="You are a careful editor. Never invent metrics.",
                model=OLLAMA_SMALL,
            )
            snapshot = (snapshot or "").strip()
        except Exception:
            snapshot = ""
        if snapshot and len(snapshot) > 25:
            footer = f"\n\n---\n_Vision extract:_\n{vision_text[:1500]}{'…' if len(vision_text) > 1500 else ''}"
            return True, f"📊 **Analytics from your screen**\n\n{snapshot}{footer}"
        return True, f"📊 **From your screen (vision only)**\n\n{vision_text[:2500]}{'…' if len(vision_text) > 2500 else ''}"
    except urllib.error.URLError as e:
        return False, f"Cannot reach vision endpoint ({e.reason}). Check provider connectivity."
    except Exception as e:
        return False, str(e)[:400]

# ── Clipboard awareness ─────────────────────────────────────────────────────

_CLIPBOARD_HISTORY_PATH = os.path.join(_DATA, "clipboard_history.json")
_clipboard_history: list[dict] = []
_clipboard_last: str = ""
_clipboard_lock = threading.Lock()

def _clipboard_read() -> str:
    try:
        import pyperclip
        return (pyperclip.paste() or "").strip()
    except Exception:
        if sys.platform == "win32":
            try:
                import ctypes
                CF_UNICODETEXT = 13
                u32 = ctypes.windll.user32
                k32 = ctypes.windll.kernel32
                if u32.OpenClipboard(0):
                    try:
                        h = u32.GetClipboardData(CF_UNICODETEXT)
                        if h:
                            p = k32.GlobalLock(h)
                            if p:
                                try:
                                    return ctypes.wstring_at(p).strip()
                                finally:
                                    k32.GlobalUnlock(h)
                    finally:
                        u32.CloseClipboard()
            except Exception:
                pass
        return ""

def _clipboard_monitor_step():
    global _clipboard_last
    text = _clipboard_read()
    if not text or len(text) > 5000 or text == _clipboard_last:
        return
    _clipboard_last = text
    with _clipboard_lock:
        _clipboard_history.append({"text": text[:1000], "ts": time.time()})
        if len(_clipboard_history) > 20:
            _clipboard_history.pop(0)
        _save_json(_CLIPBOARD_HISTORY_PATH, {"items": _clipboard_history[-20:]})

def _get_clipboard_context() -> str:
    with _clipboard_lock:
        if not _clipboard_history:
            return ""
        recent = _clipboard_history[-3:]
    lines = []
    for item in recent:
        snippet = item["text"][:200]
        lines.append(snippet)
    return "Recent clipboard: " + " | ".join(lines)

async def _clipboard_monitor_loop():
    """Watch clipboard for new content every 10 seconds."""
    await bot.wait_until_ready()
    await asyncio.sleep(10)
    while True:
        try:
            await asyncio.to_thread(_clipboard_monitor_step)
            await asyncio.sleep(10)
        except Exception:
            await asyncio.sleep(30)

# ── Learning from corrections ────────────────────────────────────────────────

_CORRECTIONS_PATH = os.path.join(_DATA, "corrections.json")
_correction_lock = threading.Lock()

_CORRECTION_PATTERNS = [
    r"(?i)^no[,.]?\s*(i\s+mean|that'?s\s+not|wrong|incorrect|not\s+what\s+i)",
    r"(?i)^that'?s\s+(wrong|not\s+right|incorrect|not\s+what)",
    r"(?i)^i\s+didn'?t\s+(mean|ask|say|want)\s+that",
    r"(?i)^(actually|correction)[,:]",
    r"(?i)^(stop|don'?t|quit)\s+(saying|doing|calling|using)",
]

def _detect_correction(user_msg: str, prev_luna_reply: str) -> dict | None:
    """Detect if user is correcting Luna. Returns correction dict or None."""
    if not user_msg or len(user_msg) < 5:
        return None
    for pat in _CORRECTION_PATTERNS:
        if re.match(pat, user_msg.strip()):
            return {
                "user_said": user_msg[:300],
                "luna_said": (prev_luna_reply or "")[:300],
                "ts": time.time(),
            }
    return None

def _store_correction(correction: dict):
    with _correction_lock:
        data = _load_json(_CORRECTIONS_PATH, {"corrections": []})
        corrections = data.get("corrections", [])
        corrections.append(correction)
        if len(corrections) > 50:
            corrections = corrections[-50:]
        _save_json(_CORRECTIONS_PATH, {"corrections": corrections})

def _get_corrections_context() -> str:
    data = _load_json(_CORRECTIONS_PATH, {"corrections": []})
    corrections = data.get("corrections", [])[-5:]
    if not corrections:
        return ""
    lines = []
    for c in corrections:
        lines.append(f"User corrected: \"{c.get('user_said', '')[:100]}\" (you had said: \"{c.get('luna_said', '')[:80]}\")")
    return "Recent corrections from user (avoid repeating these mistakes): " + "; ".join(lines)

# ── Morning briefing ─────────────────────────────────────────────────────────

_BRIEFING_PATH = os.path.join(_DATA, "last_briefing.json")

def _get_weather() -> str:
    """Fetch weather from wttr.in (free, no API key needed)."""
    try:
        req = urllib.request.Request("https://wttr.in/?format=%l:+%C+%t+%h+%w", headers={"User-Agent": "Luna/5.0"})
        with urllib.request.urlopen(req, timeout=8) as r:
            return r.read().decode("utf-8", errors="replace").strip()[:200]
    except Exception:
        return ""

def _generate_morning_briefing() -> str:
    """Generate a morning briefing: weather, calendar, todos, news summary."""
    parts = ["**Good morning! Here's your briefing:**\n"]

    weather = _get_weather()
    if weather:
        parts.append(f"**Weather:** {weather}")

    today_str = datetime.now().strftime("%Y-%m-%d")
    cal = _load_json(_CALENDAR_FILE, {"events": []})
    today_events = [e for e in cal.get("events", []) if e.get("date", "") == today_str]
    if today_events:
        ev_lines = [f"  - {e.get('time', '??:??')} {e.get('title', 'Event')}" for e in today_events]
        parts.append("**Today's calendar:**\n" + "\n".join(ev_lines))
    else:
        parts.append("**Calendar:** No events scheduled today.")

    todos = _load_json(_TODOS_FILE, {"items": []})
    pending = [t for t in todos.get("items", []) if not t.get("done")]
    if pending:
        td_lines = [f"  - {t.get('text', '?')}" for t in pending[:5]]
        parts.append("**Pending todos:**\n" + "\n".join(td_lines))

    reminders = _load_json(_REMINDERS_FILE, [])
    upcoming = []
    now = time.time()
    for rem in reminders:
        t = rem.get("at") or rem.get("time") or 0
        if 0 < t - now < 86400:
            upcoming.append(rem.get("text", "reminder")[:60])
    if upcoming:
        parts.append("**Upcoming reminders:** " + ", ".join(upcoming[:3]))

    try:
        import xml.etree.ElementTree as ET2
        headlines = []
        for feed_url in WORLD_NEWS_FEEDS[:1]:
            try:
                req = urllib.request.Request(feed_url, headers={"User-Agent": "Luna/5.0"})
                with urllib.request.urlopen(req, timeout=8) as r:
                    root = ET2.fromstring(r.read())
                for item in root.iter("item"):
                    title = (item.findtext("title") or "").strip()
                    if title:
                        headlines.append(title)
                    if len(headlines) >= 3:
                        break
            except Exception:
                pass
        if headlines:
            parts.append("**Top news:** " + " · ".join(headlines[:3]))
    except Exception:
        pass

    return "\n\n".join(parts)

def _should_show_briefing() -> bool:
    """True if it's morning (6-10am) and we haven't shown today's briefing yet."""
    now = datetime.now()
    if not (6 <= now.hour <= 10):
        return False
    data = _load_json(_BRIEFING_PATH, {})
    last_date = data.get("date", "")
    return last_date != now.strftime("%Y-%m-%d")

def _mark_briefing_shown():
    _save_json(_BRIEFING_PATH, {"date": datetime.now().strftime("%Y-%m-%d"), "ts": time.time()})

# ── Browser tab awareness ────────────────────────────────────────────────────

def _gather_browser_context() -> str:
    """Extract browser tab info from active window title and recent browser history."""
    parts = []
    try:
        if sys.platform == "win32":
            import ctypes
            hwnd = ctypes.windll.user32.GetForegroundWindow()
            length = ctypes.windll.user32.GetWindowTextLengthW(hwnd) + 1
            buf = ctypes.create_unicode_buffer(length)
            ctypes.windll.user32.GetWindowTextW(hwnd, buf, length)
            title = buf.value.strip()
            browsers = ("chrome", "firefox", "edge", "opera", "brave", "vivaldi")
            if title and any(b in title.lower() for b in browsers):
                parts.append(f"Browser tab: {title[:120]}")
    except Exception:
        pass
    # Try reading Chrome history for recent URLs
    try:
        import sqlite3
        history_paths = [
            os.path.expanduser(r"~\AppData\Local\Google\Chrome\User Data\Default\History"),
            os.path.expanduser(r"~\AppData\Local\Microsoft\Edge\User Data\Default\History"),
        ]
        for hp in history_paths:
            if not os.path.isfile(hp):
                continue
            tmp = os.path.join(tempfile.gettempdir(), f"luna_browser_hist_{os.path.basename(os.path.dirname(os.path.dirname(hp)))}.db")
            try:
                shutil.copy2(hp, tmp)
                conn = sqlite3.connect(tmp)
                rows = conn.execute(
                    "SELECT url, title FROM urls ORDER BY last_visit_time DESC LIMIT 5"
                ).fetchall()
                conn.close()
                os.remove(tmp)
                if rows:
                    sites = [f"{r[1][:50]} ({r[0][:60]})" for r in rows if r[1]]
                    if sites:
                        parts.append("Recent browsing: " + "; ".join(sites[:3]))
                break
            except Exception:
                try: os.remove(tmp)
                except Exception: pass
    except Exception:
        pass
    return " | ".join(parts) if parts else ""

# ── Voice wake word ──────────────────────────────────────────────────────────

_WAKE_WORD_ENABLED = _env("LUNA_WAKE_WORD", "").strip().lower() in ("1", "true", "yes", "on")
_wake_word_running = False

async def _wake_word_loop():
    """Listen for 'Hey Luna' using openwakeword (optional, enable with LUNA_WAKE_WORD=1)."""
    global _wake_word_running
    if not _WAKE_WORD_ENABLED:
        return
    await bot.wait_until_ready()
    await asyncio.sleep(5)
    try:
        import pyaudio
        WakeModel = __import__("openwakeword.model", fromlist=["Model"]).Model
        oww = WakeModel(wakeword_models=["hey_jarvis"], inference_framework="onnx")
        pa = pyaudio.PyAudio()
        stream = pa.open(format=pyaudio.paInt16, channels=1, rate=16000, input=True, frames_per_buffer=1280)
        _wake_word_running = True
        print("[Luna] Wake word listener active (say 'Hey Luna').", flush=True)
        import numpy as np

        def _poll_wake_word():
            audio = stream.read(1280, exception_on_overflow=False)
            audio_np = np.frombuffer(audio, dtype=np.int16)
            return oww.predict(audio_np)

        while _wake_word_running:
            prediction = await asyncio.to_thread(_poll_wake_word)
            for mdl_name, score in prediction.items():
                if score > 0.5:
                    print(f"[Luna] Wake word detected! ({mdl_name}: {score:.2f})", flush=True)
                    oww.reset()
                    await _handle_wake_word_activation()
    except ImportError:
        print("[Luna] Wake word: openwakeword or pyaudio not installed. Skipping.", flush=True)
    except Exception as e:
        print(f"[Luna] Wake word error: {e}", flush=True)

def _wake_record_legacy_5s() -> tuple[bytes | None, str | None]:
    """Old path: blocking fixed-5s pyaudio capture. Returns (pcm_bytes, wav_path)."""
    try:
        import pyaudio
    except Exception:
        return None, None
    pa = pyaudio.PyAudio()
    try:
        stream = pa.open(format=pyaudio.paInt16, channels=1, rate=16000,
                         input=True, frames_per_buffer=1024)
        frames = []
        for _ in range(int(16000 / 1024 * 5)):
            frames.append(stream.read(1024, exception_on_overflow=False))
        stream.stop_stream()
        stream.close()
    finally:
        try: pa.terminate()
        except Exception: pass
    pcm = b"".join(frames)
    return pcm, None


def _wake_emotion_from_pcm(pcm: bytes, text: str) -> dict | None:
    """Run the existing voice-emotion model on raw PCM by materializing a temp WAV."""
    if not pcm:
        return None
    fd = -1
    path = None
    try:
        wav = _pcm_to_wav_bytes(pcm)
        if not wav:
            return None
        fd, path = tempfile.mkstemp(suffix=".wav")
        os.write(fd, wav)
        os.close(fd)
        fd = -1
        return _analyze_voice_clip_emotion(path, text or "")
    except Exception:
        return None
    finally:
        if fd != -1:
            try: os.close(fd)
            except Exception: pass
        if path:
            try: os.unlink(path)
            except Exception: pass


def _wake_format_input(text: str, emo_meta: dict | None) -> str:
    """Apply the [Wake voice emotion] header used by the legacy wake handler."""
    tstrip = (text or "").strip()
    if not isinstance(emo_meta, dict):
        return tstrip
    emo = str(emo_meta.get("emotion") or "neutral")
    vol = str(emo_meta.get("volume") or "normal")
    conf = emo_meta.get("model_confidence")
    conf_txt = f", confidence {float(conf):.2f}" if conf is not None else ""
    return (
        f"[Wake voice emotion]\n"
        f"Emotion: {emo}\n"
        f"Volume: {vol}{conf_txt}\n\n"
        f"{tstrip}"
    )


async def _handle_wake_word_activation():
    """After wake word detected: VAD-record (no fixed 5s timer), optionally pre-fire
    LLM on stable ASR partial, finalize transcript, then stream the reply through
    sentence-buffered TTS so the user hears Luna start speaking before the full
    reply finishes generating.

    Pipeline (default ON; each stage is independently opt-out via env):
      LUNA_VOICE_VAD=1         — replace fixed 5s with VAD silence-tail capture
      LUNA_VOICE_PREFIRE=1     — fire LLM on stable ASR partial during recording
      LUNA_VOICE_STREAM_TTS=1  — sentence-buffered, parallel TTS synth + ordered playback
    """
    use_vad = _voice_vad_enabled()
    use_prefire = _voice_prefire_enabled()
    use_stream_tts = _voice_stream_tts_enabled()
    scope = LINKED_SCOPE or "web"
    _cf = _chat_fast_enabled()

    # ---- 1. Speculative state (only used when prefire is on) -----------------
    spec = {
        "lock": threading.Lock(),
        "last_partial": "",
        "stable_count": 0,
        "fired": False,
        "fired_text": "",
        "stop_event": threading.Event(),
        "thread": None,
        "audio_chunks": [],   # list[bytes] in playback order
        "reply_text": "",
    }

    def _build_prompt(input_text: str) -> tuple[str, list[dict]]:
        history = _compact_history(
            get_recent_conversation(scope, 20), fast=_cf, scope=scope
        )
        system = _prepare_main_chat_system(
            scope, input_text, fast=_cf, force_stream_mode=_stream_mode_env_on()
        )
        return system, history

    def _run_speculative_pipeline(fired_text: str) -> None:
        try:
            system, history = _build_prompt(fired_text)
            stream = ollama_stream(
                fired_text, system=system, scope=scope, history=history,
                model=OLLAMA_CHAT, compact=_cf,
            )
            chunks: list[bytes] = []
            full_text = _play_streaming_tts(
                stream,
                stop_event=spec["stop_event"],
                audio_sink=chunks.append,
            )
            spec["audio_chunks"] = chunks
            spec["reply_text"] = (full_text or "").strip()
        except Exception as e:
            print(f"[Luna] Speculative pipeline error: {e}", flush=True)

    def _on_partial_snapshot(pcm_snapshot: bytes) -> None:
        if not use_prefire or spec["fired"]:
            return
        text = _voice_pcm_whisper(pcm_snapshot)
        if not text:
            return
        text = (text or "").strip()
        if len(text) < 3:
            return
        with spec["lock"]:
            if not spec["last_partial"]:
                spec["last_partial"] = text
                return
            if text == spec["last_partial"]:
                spec["stable_count"] += 1
            else:
                spec["last_partial"] = text
                spec["stable_count"] = 0
                return
            if spec["stable_count"] >= 1 and not spec["fired"]:
                spec["fired"] = True
                spec["fired_text"] = text
        if spec["fired"] and spec["thread"] is None:
            t = threading.Thread(
                target=_run_speculative_pipeline,
                args=(spec["fired_text"],),
                daemon=True,
                name="luna-spec-llm",
            )
            spec["thread"] = t
            t.start()

    # ---- 2. Capture audio ----------------------------------------------------
    try:
        if use_vad:
            pcm = await asyncio.to_thread(
                _voice_record_vad,
                max_s=_voice_vad_max_s(),
                tail_ms=_voice_vad_tail_ms(),
                min_speech_s=_voice_vad_min_s(),
                on_partial_snapshot=(_on_partial_snapshot if use_prefire else None),
                partial_interval_ms=_voice_partial_interval_ms(),
            )
        else:
            pcm, _ = await asyncio.to_thread(_wake_record_legacy_5s)
    except Exception as e:
        print(f"[Luna] Wake word capture error: {e}", flush=True)
        spec["stop_event"].set()
        return
    if not pcm:
        spec["stop_event"].set()
        return

    # ---- 3. Final transcript -------------------------------------------------
    final_text = await asyncio.to_thread(_voice_pcm_whisper, pcm)
    if not final_text or len(final_text.strip()) <= 2:
        spec["stop_event"].set()
        return
    final_text = final_text.strip()

    # Voice emotion (best-effort) on the full clip.
    emo_meta = await asyncio.to_thread(_wake_emotion_from_pcm, pcm, final_text)
    wake_input = _wake_format_input(final_text, emo_meta)

    # ---- 4. Speculative match: keep buffered audio + play in order -----------
    if spec["fired"] and spec["fired_text"] == final_text:
        if spec["thread"] is not None:
            try:
                await asyncio.to_thread(spec["thread"].join, 30)
            except Exception:
                pass
        chunks = spec["audio_chunks"]
        reply = spec["reply_text"]
        if chunks and reply and not reply.lower().startswith("ollama offline"):
            try:
                _vrchat_set_talking(True)
            except Exception:
                pass
            for c in chunks:
                _voice_enqueue_audio(c, manage_vrchat=False)
            try:
                await asyncio.to_thread(_tts_turn_queue.join)
            finally:
                try:
                    _vrchat_set_talking(False)
                except Exception:
                    pass
            try:
                append_exchange(scope, final_text, reply)
            except Exception:
                pass
            return

    # ---- 5. Speculative miss / disabled — drop and run a fresh turn ----------
    spec["stop_event"].set()
    if spec["thread"] is not None and spec["thread"].is_alive():
        try:
            spec["thread"].join(timeout=2)
        except Exception:
            pass

    system, history = _build_prompt(wake_input)
    if use_stream_tts:
        def _gen():
            yield from ollama_stream(
                wake_input, system=system, scope=scope, history=history,
                model=OLLAMA_CHAT, compact=_cf,
            )
        try:
            reply = await asyncio.to_thread(_play_streaming_tts, _gen())
        except Exception as e:
            print(f"[Luna] Wake stream TTS error: {e}", flush=True)
            reply = ""
        reply = (reply or "").strip()
        if reply and not reply.lower().startswith("ollama offline"):
            try:
                append_exchange(scope, final_text, reply)
            except Exception:
                pass
        return

    # Legacy non-streaming fallback
    try:
        reply = await asyncio.to_thread(
            lambda: ollama_chat(
                wake_input, system=system, scope=scope, history=history,
                model=OLLAMA_CHAT, compact=_cf,
            )
        )
    except Exception as e:
        print(f"[Luna] Wake chat error: {e}", flush=True)
        return
    if reply and not reply.startswith("Ollama offline"):
        try:
            append_exchange(scope, final_text, reply)
        except Exception:
            pass
        _play_reply_tts(reply, para_mode=False)

async def _publish_announce_tick() -> dict:
    """One RSS/Twitch poll + Discord announces + YouTube→Facebook/X + Twitch→Facebook.

    Used by the background loop and ``POST /api/publish-announce/check``.
    """
    out: dict = {
        "ok": True,
        "error": None,
        "discord_posts": 0,
        "announcement_previews": [],
        "new_youtube": [],
        "youtube_facebook": [],
        "youtube_x": [],
        "twitch_live_facebook": [],
    }
    if not _PUBLISH_ANNOUNCE_CONFIGURED or _poll_publish_announce is None:
        out["ok"] = False
        out["error"] = "Publish announce not configured or luna_publish_announce.py missing."
        return out

    cids = _PUBLISH_ANNOUNCE_DISCORD_CHANNEL_IDS

    def _publish_announce_poll_sync():
        auth = _twitch_helix_read_auth()
        return _poll_publish_announce(
            state_path=_PUBLISH_ANNOUNCE_STATE_PATH,
            youtube_channel_ids=_PUBLISH_ANNOUNCE_YOUTUBE_IDS,
            youtube_rss_urls=_PUBLISH_ANNOUNCE_YOUTUBE_RSS,
            twitch_logins=_PUBLISH_ANNOUNCE_TWITCH_LOGINS,
            twitch_client_id=(auth[0] if auth else ""),
            twitch_app_token=(auth[1] if auth else ""),
            skip_twitch=not bool(auth),
        )

    msgs, twitch_live, yt_uploads = await asyncio.to_thread(_publish_announce_poll_sync)
    if msgs and cids:
        for cid in cids:
            ch = bot.get_channel(cid)
            if ch is None:
                try:
                    ch = await bot.fetch_channel(cid)
                except Exception:
                    ch = None
            if ch is None:
                print(f"[Publish announce] Cannot resolve Discord channel id {cid}.", flush=True)
                continue
            for body in msgs:
                body = (body or "").strip()
                if not body:
                    continue
                if len(body) > 1990:
                    body = body[:1987] + "..."
                try:
                    await ch.send(body)
                    out["discord_posts"] += 1
                    prev = body[:320]
                    if prev not in out["announcement_previews"]:
                        out["announcement_previews"].append(prev)
                except Exception as e:
                    print(f"[Publish announce] send failed ({cid}): {e}", flush=True)
    if yt_uploads and (LUNA_PUBLISH_ANNOUNCE_YOUTUBE_X or LUNA_PUBLISH_ANNOUNCE_YOUTUBE_FACEBOOK):
        for up in yt_uploads:
            title = str(up.get("title") or "").strip()
            url = str(up.get("url") or "").strip()
            hint = str(up.get("hint") or "").strip()
            vid = str(up.get("video_id") or "").strip()
            out["new_youtube"].append({"video_id": vid, "title": title, "url": url})
            if not url:
                continue
            if LUNA_PUBLISH_ANNOUNCE_YOUTUBE_FACEBOOK:
                fb_body = _format_yt_upload_fb_message(title, url, hint)
                ok_fb, r_fb = await asyncio.to_thread(_fb_post_plain_status_to_timeline, fb_body)
                tag = vid or title[:40]
                out["youtube_facebook"].append({"ok": ok_fb, "detail": r_fb, "video_id": vid})
                if ok_fb:
                    print(f"[Publish announce] YouTube → Facebook ok ({tag}): {r_fb}", flush=True)
                else:
                    print(f"[Publish announce] YouTube → Facebook failed ({tag}): {r_fb}", flush=True)
            if LUNA_PUBLISH_ANNOUNCE_YOUTUBE_X:
                x_body = _format_yt_upload_x_message(title, url, hint)
                ok_x, r_x = await asyncio.to_thread(_run_x_share, x_body)
                tag = vid or title[:40]
                out["youtube_x"].append({"ok": ok_x, "detail": r_x, "video_id": vid})
                if ok_x:
                    print(f"[Publish announce] YouTube → X ok ({tag}): {r_x}", flush=True)
                else:
                    print(f"[Publish announce] YouTube → X failed ({tag}): {r_x}", flush=True)
    if LUNA_PUBLISH_ANNOUNCE_TWITCH_LIVE_FACEBOOK and twitch_live:
        for ev in twitch_live:
            lg = str(ev.get("login") or "").strip().lower()
            if not lg or lg not in _PUBLISH_ANNOUNCE_FB_TWITCH_LOGINS:
                continue
            st_at = str(ev.get("started_at") or "").strip()
            if st_at and _last_fb_twitch_stream.get(lg) == st_at:
                continue
            fb_body = _format_twitch_live_fb_message(ev)
            ok_fb, r_fb = await asyncio.to_thread(_fb_post_plain_status_to_timeline, fb_body)
            out["twitch_live_facebook"].append({"login": lg, "ok": ok_fb, "detail": r_fb})
            if ok_fb:
                if st_at:
                    _last_fb_twitch_stream[lg] = st_at
                else:
                    _last_fb_twitch_stream[lg] = str(time.time())
                print(f"[Publish announce] Facebook go-live post ok ({lg}): {r_fb}", flush=True)
            else:
                print(f"[Publish announce] Facebook go-live post failed ({lg}): {r_fb}", flush=True)
    return out


async def _publish_announce_loop():
    """Post to Discord text channel(s) when watched YouTube feeds have a new video or Twitch logins go live."""
    if not _PUBLISH_ANNOUNCE_CONFIGURED:
        return
    await bot.wait_until_ready()
    cids = _PUBLISH_ANNOUNCE_DISCORD_CHANNEL_IDS
    tw_ok = bool(_twitch_helix_read_auth())
    print(
        f"[Publish announce] ON — Discord channel ids {', '.join(str(x) for x in cids)}, "
        f"every {LUNA_PUBLISH_ANNOUNCE_POLL_SEC:.0f}s "
        f"(YouTube feeds: {len(_PUBLISH_ANNOUNCE_YOUTUBE_IDS) + len(_PUBLISH_ANNOUNCE_YOUTUBE_RSS)}, "
        f"Twitch: {len(_PUBLISH_ANNOUNCE_TWITCH_LOGINS)}).",
        flush=True,
    )
    if _PUBLISH_ANNOUNCE_TWITCH_LOGINS and not tw_ok:
        print(
            "[Publish announce] Twitch live needs TWITCH_CLIENT_ID plus one of: TWITCH_APP_TOKEN, "
            "authorized Twitch OAuth (data/twitch_oauth.json), or TWITCH_CLIENT_SECRET — live alerts skipped.",
            flush=True,
        )
    if LUNA_PUBLISH_ANNOUNCE_TWITCH_LIVE_FACEBOOK:
        _fb_l = ", ".join(sorted(_PUBLISH_ANNOUNCE_FB_TWITCH_LOGINS)) or "(none — set TWITCH_BROADCASTER_LOGIN or LUNA_PUBLISH_ANNOUNCE_FB_FOR_TWITCH_LOGINS)"
        print(f"[Publish announce] Twitch go-live → Facebook: ON for **{_fb_l}**.", flush=True)
    if LUNA_PUBLISH_ANNOUNCE_YOUTUBE_X:
        print("[Publish announce] YouTube upload → X cross-post: ON.", flush=True)
    if LUNA_PUBLISH_ANNOUNCE_YOUTUBE_FACEBOOK:
        print("[Publish announce] YouTube upload → Facebook cross-post: ON.", flush=True)
    while True:
        try:
            await _publish_announce_tick()
        except Exception as e:
            print(f"[Publish announce] loop error: {e}", flush=True)
        await asyncio.sleep(LUNA_PUBLISH_ANNOUNCE_POLL_SEC)

# ── RAG: knowledge base vector search ────────────────────────────────────────

_EMBEDDINGS_PATH = os.path.join(_DATA, "knowledge_embeddings.json")
_embeddings_cache: dict[str, list[float]] = {}
_embeddings_lock = threading.Lock()

def _ollama_embed(text: str) -> list[float] | None:
    """Get embedding from Ollama (nomic-embed-text or any embedding model)."""
    embed_model = _env("OLLAMA_EMBED_MODEL", "nomic-embed-text").strip()
    if not embed_model:
        return None
    try:
        body = json.dumps({"model": embed_model, "prompt": text[:2000]}).encode()
        req = urllib.request.Request(f"{OLLAMA_BASE}/api/embeddings", data=body,
            headers={"Content-Type": "application/json"}, method="POST")
        with urllib.request.urlopen(req, timeout=15) as r:
            data = json.loads(r.read())
        return data.get("embedding")
    except Exception:
        return None

def _cosine_sim(a: list[float], b: list[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b))
    na = sum(x * x for x in a) ** 0.5
    nb = sum(x * x for x in b) ** 0.5
    if na == 0 or nb == 0:
        return 0.0
    return dot / (na * nb)

def _build_knowledge_embeddings():
    """Embed all knowledge entries and cache."""
    global _embeddings_cache
    entries = list_knowledge()
    updated = False
    with _embeddings_lock:
        for entry in entries:
            slug = entry.get("slug", "")
            if slug in _embeddings_cache:
                continue
            content = get_knowledge(slug) or ""
            if not content.strip():
                continue
            emb = _ollama_embed(content[:1500])
            if emb:
                _embeddings_cache[slug] = emb
                updated = True
        if updated:
            _save_json(_EMBEDDINGS_PATH, _embeddings_cache)

def _search_knowledge_semantic(query: str, top_k: int = 3) -> list[dict]:
    """Search knowledge base using embeddings. Falls back to keyword search."""
    q_emb = _ollama_embed(query)
    if not q_emb or not _embeddings_cache:
        return search_knowledge(query, top_k)
    scores = []
    with _embeddings_lock:
        for slug, emb in _embeddings_cache.items():
            sim = _cosine_sim(q_emb, emb)
            scores.append((slug, sim))
    scores.sort(key=lambda x: x[1], reverse=True)
    results = []
    for slug, sim in scores[:top_k]:
        if sim < 0.3:
            continue
        content = get_knowledge(slug)
        title = (content or "").split("\n")[0].strip().lstrip("# ") if content else slug
        results.append({"slug": slug, "title": title, "snippet": (content or "")[:200], "score": round(sim, 3)})
    return results

def _load_knowledge_embeddings():
    """Load cached embeddings from disk."""
    global _embeddings_cache
    data = _load_json(_EMBEDDINGS_PATH, {})
    if isinstance(data, dict) and all(isinstance(v, list) for v in data.values()):
        with _embeddings_lock:
            _embeddings_cache = data

async def _knowledge_embedding_loop():
    """Periodically rebuild knowledge embeddings."""
    await bot.wait_until_ready()
    await asyncio.sleep(20)
    _load_knowledge_embeddings()
    while True:
        try:
            await asyncio.to_thread(_build_knowledge_embeddings)
            await asyncio.sleep(600)
        except Exception:
            await asyncio.sleep(120)

# ── Internal ML (always learning from experience) ──────────────────────────────

def _ml_internal_learn_step() -> None:
    """Learn from action log: which commands succeed, patterns, preferences. Runs internally, not as user command."""
    try:
        entries = []
        with _action_log_lock:
            if os.path.isfile(_ACTION_LOG):
                with open(_ACTION_LOG, "r", encoding="utf-8") as f:
                    for line in f.readlines()[-500:]:
                        line = line.strip()
                        if not line:
                            continue
                        try:
                            e = json.loads(line)
                            entries.append(e)
                        except Exception:
                            pass
        if len(entries) < 10:
            return
        learned = _load_json(_ML_LEARNED_PATH, {"patterns": [], "cmd_success": {}, "last_learned": 0})
        cmd_success = learned.get("cmd_success", {})
        for e in entries[-200:]:
            cmd = (e.get("cmd") or "").strip()
            reply = (e.get("reply") or "")
            ok = "❌" not in reply and len(reply) > 0
            if cmd:
                rec = cmd_success.setdefault(cmd, {"ok": 0, "fail": 0})
                if ok:
                    rec["ok"] = rec.get("ok", 0) + 1
                else:
                    rec["fail"] = rec.get("fail", 0) + 1
        learned["cmd_success"] = {k: v for k, v in list(cmd_success.items())[-50:]}
        learned["last_learned"] = time.time()
        _save_json(_ML_LEARNED_PATH, learned)
    except Exception:
        pass

async def _ml_learning_loop():
    """Background loop: Luna learns from her actions and outcomes. Internal only."""
    await bot.wait_until_ready()
    while True:
        try:
            await asyncio.to_thread(_ml_internal_learn_step)
            await asyncio.sleep(1800)
        except Exception:
            await asyncio.sleep(300)

# ── Social module setup ──────────────────────────────────────────────────────
# luna_social.py contains the extraction target for social automation functions.
# To complete the split incrementally:
# 1. Move a function from bot.py to luna_social.py
# 2. Replace the function in bot.py with: from luna_social import function_name
# 3. Ensure luna_social.configure() has all needed references
#
# Configure luna_social with function references it needs:
def _configure_social():
    luna_social.configure(
        ollama_chat=ollama_chat,
        _load_json=_load_json,
        _save_json=_save_json,
        existential_bump=existential_bump,
    )
# Called after all functions are defined (see bottom of file)

# ── Browser helpers ───────────────────────────────────────────────────────────

# Run in every page so sites (incl. Google sign-in) see a normal browser, not automation.
_STEALTH_INIT_SCRIPT = """
(function() {
  try {
    Object.defineProperty(navigator, 'webdriver', { get: function() { return undefined; }, configurable: true });
  } catch (e) {}
  if (window.chrome == null) window.chrome = {};
  if (window.chrome.runtime == null) {
    window.chrome.runtime = { id: undefined, connect: function() {}, sendMessage: function() {} };
  }
  if (window.chrome.loadTimes == null) window.chrome.loadTimes = function() {};
  if (window.chrome.csi == null) window.chrome.csi = function() {};
  if (window.chrome.app == null) window.chrome.app = { isInstalled: false };
  var q = navigator.permissions && navigator.permissions.query;
  if (q) {
    navigator.permissions.query = function(args) {
      return args && args.name === 'notifications'
        ? Promise.resolve({ state: Notification.permission, onchange: null })
        : q.apply(this, arguments);
    };
  }
  try {
    delete window.cdc_adoQpoasnfa76pfcZLmcfl_Array;
    delete window.cdc_adoQpoasnfa76pfcZLmcfl_Promise;
    delete window.cdc_adoQpoasnfa76pfcZLmcfl_Symbol;
  } catch (e) {}
})();
"""

# Fallback UA only for bundled Chromium; real Chrome/Edge should use the browser's own UA (avoids Google mismatch).
_CHROME_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/131.0.0.0 Safari/537.36"
)

def _extra_browser_args() -> list:
    raw = _env("LUNA_BROWSER_EXTRA_ARGS", "")
    if not raw:
        return []
    return [p.strip() for p in raw.split("|") if p.strip()]

def _browser_opts(profile_dir: str) -> dict:
    base_args = [
        "--disable-blink-features=AutomationControlled",
        "--disable-session-crashed-bubble",
        "--hide-crash-restore-bubble",
        "--no-first-run",
        "--no-default-browser-check",
        "--disable-infobars",
        "--lang=en-CY",
    ]
    base_args.extend(_extra_browser_args())
    opts = {
        "user_data_dir": profile_dir,
        "headless": False,
        "viewport": {"width": 1280, "height": 900},
        "ignore_default_args": ["--enable-automation", "--no-sandbox"],
        "args": base_args,
        "locale": _env("LUNA_BROWSER_LOCALE", "en-CY") or "en-CY",
        "timezone_id": _env("LUNA_BROWSER_TIMEZONE", "Asia/Nicosia") or "Asia/Nicosia",
    }
    if BROWSER_PATH and os.path.isfile(BROWSER_PATH):
        opts["executable_path"] = BROWSER_PATH
    elif BROWSER_CHANNEL in ("chrome", "msedge", "chromium"):
        opts["channel"] = BROWSER_CHANNEL
    ua = _env("LUNA_BROWSER_USER_AGENT").strip()
    if ua:
        opts["user_agent"] = ua
    elif BROWSER_CHANNEL == "chromium":
        opts["user_agent"] = _CHROME_USER_AGENT
    return opts

def _launch_social_browser(profile_dir: str, p):
    """Launch Chrome for social/Suno so it looks like a user session (avoids Google blocking login)."""
    context = p.chromium.launch_persistent_context(**_browser_opts(profile_dir))
    context.add_init_script(_STEALTH_INIT_SCRIPT)
    return context

def _transient_playwright_launch_err(exc: BaseException) -> bool:
    m = str(exc).lower()
    return any(
        x in m
        for x in (
            "closed",
            "crash",
            "timeout",
            "target page",
            "browser has been closed",
            "context has been closed",
            "epipe",
            "broken pipe",
            "connection closed",
        )
    )

def _launch_social_browser_with_retry(profile_dir: str, p, attempts: int = 3):
    """launch_persistent_context can fail if a prior Chrome on the same profile is still exiting."""
    for i in range(max(1, attempts)):
        try:
            return _launch_social_browser(profile_dir, p)
        except Exception as e:
            if i < attempts - 1 and _transient_playwright_launch_err(e):
                time.sleep(1.2 + i * 0.8)
                continue
            raise

def _acquire_live_page(context):
    """First open page from a persistent context, or a new tab (avoids dead pages[0])."""
    try:
        for pg in context.pages:
            try:
                if not pg.is_closed():
                    return pg
            except Exception:
                continue
    except Exception:
        pass
    return context.new_page()

def _youtube_login_bootstrap_url() -> str:
    """Open Google sign-in with login_hint so Luna’s channel account is pre-filled when YOUTUBE_LOGIN_EMAIL is set."""
    if YOUTUBE_LOGIN_EMAIL and "@" in YOUTUBE_LOGIN_EMAIL:
        cont = urllib.parse.quote("https://www.youtube.com/", safe="")
        hint = urllib.parse.quote(YOUTUBE_LOGIN_EMAIL, safe="")
        return (
            "https://accounts.google.com/signin/identifier"
            f"?continue={cont}&flowEntry=ServiceLogin&login_hint={hint}"
        )
    return "https://www.youtube.com/"

def _youtube_needs_login_message() -> str:
    base = (
        "YouTube needs login. Browser opened — sign in with **{acct}**, then **close** the browser and try again."
    )
    acct = YOUTUBE_LOGIN_EMAIL or "the Google account tied to Luna’s YouTube channel"
    msg = base.format(acct=acct)
    if YOUTUBE_LOGIN_EMAIL:
        msg += (
            " If the wrong Google account is still used, **quit Luna**, close every Chrome window, delete folder "
            f"`{os.path.normpath(YT_PROFILE_DIR)}`, restart Luna, then run `!yt_comment` again so you can sign in fresh."
        )
    return msg

def _marker(profile_dir: str) -> str:
    return os.path.join(profile_dir, ".login_ready")

def _is_ready(profile_dir: str) -> bool:
    return os.path.isfile(_marker(profile_dir))

def _mark_ready(profile_dir: str):
    os.makedirs(profile_dir, exist_ok=True)
    open(_marker(profile_dir), "w").close()

def _clear_ready(profile_dir: str):
    m = _marker(profile_dir)
    if os.path.isfile(m): os.unlink(m)

def _mark_suno_logged_in() -> str:
    """Call when you've already logged in to Suno so Luna uses the create flow on next !suno."""
    global _suno_boot
    _mark_ready(SUNO_PROFILE_DIR)
    _suno_boot = False
    return "Marked Suno as logged in. Try **!suno** again."

def _bootstrap_window(profile_dir: str, url: str, flag_name: str, marker_fn=None) -> tuple[bool, str]:
    """Open a browser window for first-time login and keep it open."""
    global _suno_boot, _x_boot, _fb_boot, _yt_boot, _ig_boot, _wa_boot, _discord_web_boot, _msg_boot
    flags = {"_suno_boot": "_suno_boot", "_x_boot": "_x_boot", "_fb_boot": "_fb_boot",
             "_yt_boot": "_yt_boot", "_ig_boot": "_ig_boot", "_wa_boot": "_wa_boot",
             "_discord_web_boot": "_discord_web_boot", "_msg_boot": "_msg_boot"}
    import importlib
    g = globals()
    if g.get(flag_name): return False, f"Login window already open. Finish login and close it, then try again."
    g[flag_name] = True
    def _run():
        ctx = None
        try:
            from playwright.sync_api import sync_playwright
            os.makedirs(profile_dir, exist_ok=True)
            with sync_playwright() as p:
                ctx = _launch_social_browser(profile_dir, p)
                page = ctx.pages[0] if ctx.pages else ctx.new_page()
                page.goto(url, wait_until="domcontentloaded", timeout=90000)
                while any(not pg.is_closed() for pg in ctx.pages):
                    time.sleep(1)
        except Exception as e:
            print(f"[Luna bootstrap {flag_name}] {e}", flush=True)
        finally:
            try:
                if ctx: ctx.close()
            except Exception: pass
            _mark_ready(profile_dir)
            g[flag_name] = False
    threading.Thread(target=_run, daemon=True).start()
    return True, "Login window opened. Log in, then close it and try again."

def _should_use_longer_waits(cmd: str) -> bool:
    data = _load_json(_SOLUTIONS_PATH, {})
    return len(data.get(cmd, [])) > 0

def _save_solution(cmd: str, err: str):
    data = _load_json(_SOLUTIONS_PATH, {})
    entries = data.setdefault(cmd, [])
    entries.append({"error": err[:120], "hint": "extra_wait_and_alternate_selectors"})
    if len(entries) > 20: data[cmd] = entries[-15:]
    with _solutions_lock: _save_json(_SOLUTIONS_PATH, data)

def _record_failure(cmd: str, err: str, params: dict | None = None):
    global _last_cmd, _last_err, _last_params
    _last_cmd, _last_err, _last_params = cmd, err, dict(params or {})
    try:
        existential_bump(dread=0.12)
    except Exception:
        pass

# ── YouTube helpers ───────────────────────────────────────────────────────────

def _fmt_sec(s: int) -> str:
    s = max(int(s or 0), 0)
    m, s = divmod(s, 60)
    h, m = divmod(m, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"

def _yt_extract_id(url: str) -> str:
    u = (url or "").strip().rstrip(").,;!?]>")
    try:
        p = urllib.parse.urlparse(u)
        host = p.netloc.lower()
        path = p.path.strip("/")
        if "youtu.be" in host:
            return (path.split("/")[0] if path else "").split("?")[0]
        if "youtube.com" in host:
            if path == "watch":
                return (urllib.parse.parse_qs(p.query).get("v", [""])[0] or "").split("&")[0]
            if path.startswith("watch/"):
                return path.split("/", 1)[1].split("?")[0].split("/")[0]
            for prefix in ("shorts/", "live/", "embed/"):
                if path.startswith(prefix):
                    return path.split("/", 1)[1].split("/")[0]
    except Exception:
        pass
    return ""


def _yt_url_from_freeform_arg(s: str) -> str:
    """First YouTube watch/shorts/live URL from Discord/UI text (handles <https://...> and trailing junk)."""
    t = (s or "").strip()
    for ch in "<>[]{}":
        t = t.replace(ch, " ")
    t = " ".join(t.split())
    m = re.search(
        r"(https?://(?:www\.)?(?:youtube\.com/(?:watch(?:\?[^\s#]+|/[\w-]{6,})|shorts/[\w-]+|live/[\w-]+)|youtu\.be/[\w-]+))",
        t,
        re.I,
    )
    if m:
        return m.group(1).rstrip(").,;!?]")
    m2 = re.search(r"(https?://[^\s]+)", t)
    return (m2.group(1).rstrip(").,;!?]") if m2 else "").strip()

def _get_podcast_tracks() -> tuple[bool, list[dict] | str]:
    """Scan CUSTOM_PODCAST_DIR for audio files; return list of track dicts for the queue."""
    root = (CUSTOM_PODCAST_DIR or "").strip()
    if not root or not os.path.isdir(root):
        return False, f"Custom podcast folder not found: {root or '(not set)'}"
    exts = (".mp3", ".m4a", ".wav")
    tracks = []
    try:
        for dirpath, _dirnames, filenames in os.walk(root):
            for f in filenames:
                if any(f.lower().endswith(ext) for ext in exts):
                    path = os.path.join(dirpath, f)
                    if os.path.isfile(path):
                        title = os.path.splitext(f)[0]
                        tracks.append({
                            "title": title,
                            "web_url": path,
                            "stream_url": "",
                            "duration": 0,
                            "local_path": path,
                        })
        tracks.sort(key=lambda t: (os.path.basename(t["local_path"]).lower(), t["local_path"]))
        if not tracks:
            return False, f"No audio files (.mp3, .m4a, .wav) in {root}"
        return True, tracks[:100]  # cap at 100 episodes
    except Exception as e:
        return False, str(e)


def _format_podcast_menu(tracks: list[dict]) -> str:
    lines = ["🎙️ **Custom podcast episodes in your folder:**"]
    for i, t in enumerate(tracks, 1):
        base = os.path.basename(t.get("local_path") or t.get("title") or f"episode_{i}")
        lines.append(f"{i}. {base}")
    lines.append("")
    lines.append("Say `!podcast <number>` or `!podcast <part of name>` to play one of them in Discord voice.")
    return "\n".join(lines)


def _pick_podcast_tracks(tracks: list[dict], choice: str | None) -> tuple[bool, list[dict] | str]:
    """Given all podcast tracks and a user choice (index or substring), return the tracks to queue."""
    if not choice:
        return True, tracks
    choice = choice.strip()
    if not choice:
        return True, tracks
    # Numeric index (1-based)
    if choice.isdigit():
        idx = int(choice) - 1
        if 0 <= idx < len(tracks):
            return True, [tracks[idx]]
        return False, f"No podcast #{choice}. Pick a number between 1 and {len(tracks)}."
    # Try substring match on filename / title
    clow = choice.lower()
    matches: list[dict] = []
    for t in tracks:
        name = os.path.basename(t.get("local_path") or t.get("title") or "").lower()
        title = (t.get("title") or "").lower()
        if clow in name or clow in title:
            matches.append(t)
    if not matches:
        return False, f"I couldn't find a podcast matching **{choice}**."
    # Prefer single match; if many, still queue just the first for clarity
    return True, [matches[0]]


def _create_podcast_from_description(description: str) -> tuple[bool, str]:
    """Generate a short podcast from a topic: Ollama writes the script, TTS turns it into audio, save to CUSTOM_PODCAST_DIR."""
    description = (description or "").strip()
    if not description:
        return False, "What should the podcast be about? Example: **!podcast create morning routines** or **create a podcast about productivity**."
    root = (CUSTOM_PODCAST_DIR or "").strip()
    if not root or not os.path.isdir(root):
        return False, "Set **CUSTOM_PODCAST_DIR** in .env to a writable folder (e.g. D:\\Luna 5.0\\Luna's creations) so I can save the podcast."
    system = (
        "You are Luna. Write a short podcast script (about 2–3 minutes when read aloud). "
        "Structure: a brief intro (1–2 sentences), 2–3 short segments with clear content, and a brief outro. "
        "Output ONLY the script, no stage directions or labels. Keep total under 350 words. Be natural and conversational."
    )
    script = ollama_chat(f"Podcast topic: {description[:300]}\n\nWrite the podcast script:", system=system)
    if not script or "Ollama" in script or script.startswith("Error:"):
        return False, "Could not generate the script. Try again or shorten the topic."
    script = _clean_for_tts(script)[:3000]
    if not script.strip():
        return False, "Generated script was empty."
    chunks = _split_tts(script, max_chars=120)
    if not chunks:
        return False, "No speakable chunks from script."
    temp_dir = tempfile.mkdtemp()
    list_path = os.path.join(temp_dir, "list.txt")
    out_path = os.path.join(temp_dir, "podcast.mp3")
    try:
        paths = []
        for i, chunk in enumerate(chunks[:60]):  # cap segments
            audio = _tts_bytes_media(chunk)
            if not audio:
                continue
            seg_path = os.path.join(temp_dir, f"seg_{i:03d}.mp3")
            with open(seg_path, "wb") as f:
                f.write(audio)
            paths.append(seg_path)
        if not paths:
            return False, (
                "TTS failed for all chunks. If using Fish Audio, check **FISH_AUDIO_API_KEY**, "
                "**FISH_AUDIO_REFERENCE_ID**, and `pip install fish-audio-sdk`. Otherwise check gTTS and internet."
            )
        with open(list_path, "w", encoding="utf-8") as f:
            for p in paths:
                p_abs = os.path.abspath(p).replace("\\", "/")
                f.write(f"file '{p_abs}'\n")
        ret = subprocess.run(
            ["ffmpeg", "-y", "-f", "concat", "-safe", "0", "-i", list_path, "-c", "copy", out_path],
            capture_output=True,
            timeout=120,
            cwd=temp_dir,
        )
        if ret.returncode != 0 or not os.path.isfile(out_path):
            return False, "Could not merge audio (ffmpeg). Install FFmpeg and try again."
        slug = re.sub(r"[^\w\s-]", "", description.lower())[:30].strip().replace(" ", "_") or "podcast"
        slug = re.sub(r"_+", "_", slug).strip("_")
        ts = datetime.now().strftime("%Y%m%d_%H%M")
        name = f"podcast_{slug}_{ts}.mp3"
        dest = os.path.join(root, name)
        try:
            import shutil
            shutil.copy2(out_path, dest)
        except Exception as e:
            return False, f"Could not save to podcast folder: {e}"
        return True, f"Created **{name}** in your podcast folder. Say **!podcast** to play it, or open the folder."
    finally:
        try:
            for f in os.listdir(temp_dir):
                try:
                    os.unlink(os.path.join(temp_dir, f))
                except Exception:
                    pass
            os.rmdir(temp_dir)
        except Exception:
            pass


def _resolve_track(query: str) -> tuple[bool, dict | str]:
    """Resolve YouTube URL/search, Suno URL, or local file to a playable track dict."""
    q = (query or "").strip()
    if not q: return False, "No query."
    q_low = q.lower()
    # Local file
    if any(q_low.endswith(ext) for ext in (".mp3", ".m4a", ".wav")):
        path = q if os.path.isabs(q) and os.path.isfile(q) else None
        if not path and MUSIC_DL_DIR:
            under = os.path.join(MUSIC_DL_DIR, os.path.basename(q))
            if os.path.isfile(under): path = under
        if path:
            return True, {"title": os.path.splitext(os.path.basename(path))[0],
                          "web_url": path, "stream_url": "", "duration": 0, "local_path": path}
    # Suno
    if "suno.com/song/" in q_low or "suno.com/s/" in q_low:
        try:
            import urllib.request as ur
            req = ur.Request(q, headers={"User-Agent": "Mozilla/5.0"})
            with ur.urlopen(req, timeout=15) as r:
                html_text = r.read().decode("utf-8", errors="replace")
            stream = ""
            for pat in (r'<meta[^>]+property=["\']og:audio["\'][^>]+content=["\']([^"\']+)',
                        r'https?://[^\s"\'<>]+\.suno\.ai[^\s"\'<>]*\.mp3'):
                m = re.search(pat, html_text, re.I)
                if m: stream = m.group(1).strip(); break
            if not stream: return False, "Could not find Suno audio URL."
            t = re.search(r'property=["\']og:title["\'][^>]+content=["\']([^"\']+)', html_text, re.I)
            title = html.unescape(t.group(1) if t else "Suno track").strip()
            return True, {"title": title, "web_url": q, "stream_url": stream, "duration": 0}
        except Exception as e:
            return False, f"Suno error: {e}"
    # YouTube (audio only)
    try:
        import yt_dlp

        def _audio_from_info(info: dict, fallback_web: str) -> tuple[bool, dict | str]:
            if "entries" in info and info.get("entries"):
                info = info["entries"][0]
            url = info.get("url", "")
            if not url:
                for f in (info.get("formats") or []):
                    # audio-only format
                    if f.get("acodec", "") not in ("", "none") and f.get("vcodec", "") in ("", "none"):
                        url = f.get("url", "")
                        if url:
                            break
            if not url:
                return False, "No audio stream found."
            return True, {
                "title": info.get("title", "?"),
                "web_url": info.get("webpage_url", fallback_web),
                "stream_url": url,
                "duration": int(info.get("duration") or 0),
                "http_headers": info.get("http_headers", {}),
            }

        base_opts = {
            "noplaylist": True,
            "quiet": True,
            "no_warnings": True,
            "skip_download": True,
            "format": "bestaudio[ext=m4a]/bestaudio/best",
            "extractor_args": {"youtube": {"player_client": "android,web,mweb"}},
        }

        # 1) Direct attempt for URL or query.
        opts = dict(base_opts)
        opts["default_search"] = "ytsearch1"
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(q, download=False)
        ok, track_or_err = _audio_from_info(info, q)
        if ok:
            return True, track_or_err

        # 2) Fallback: for search terms, try top 5 results and pick first available.
        is_url = q_low.startswith("http://") or q_low.startswith("https://")
        if not is_url:
            search_opts = {"quiet": True, "no_warnings": True, "extract_flat": True, "default_search": "ytsearch5"}
            with yt_dlp.YoutubeDL(search_opts) as ydl:
                sr = ydl.extract_info(q, download=False)
            candidates = []
            for e in (sr.get("entries") or [])[:5]:
                vid = (e.get("id") or "").strip()
                if vid:
                    candidates.append(f"https://www.youtube.com/watch?v={vid}")
                elif e.get("url"):
                    candidates.append(e.get("url"))
            for c in candidates:
                try:
                    with yt_dlp.YoutubeDL(base_opts) as ydl:
                        ci = ydl.extract_info(c, download=False)
                    ok, track_or_err = _audio_from_info(ci, c)
                    if ok:
                        return True, track_or_err
                except Exception:
                    continue

        return False, str(track_or_err) if isinstance(track_or_err, str) else "No playable audio found."
    except ImportError:
        return False, "yt-dlp not installed."
    except Exception as e:
        return False, str(e)

def _yt_search_multi(query: str, n: int = 5) -> tuple[bool, list | str]:
    try:
        import yt_dlp
        opts = {"quiet": True, "no_warnings": True, "extract_flat": True,
                "default_search": f"ytsearch{n}"}
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(query, download=False)
        results = []
        for e in (info.get("entries") or [])[:n]:
            vid = e.get("id",""); url = e.get("url","") or (f"https://youtu.be/{vid}" if vid else "")
            if url: results.append({"title": e.get("title","?"), "url": url})
        return True, results
    except ImportError:
        return False, "yt-dlp not installed."
    except Exception as e:
        return False, str(e)

def _yt_video_id(url: str) -> str:
    """Extract YouTube video id from url (youtu.be/ID or youtube.com/watch?v=ID)."""
    if not url: return ""
    m = re.search(r"(?:youtu\.be/|[\?&]v=)([a-zA-Z0-9_-]{11})", url)
    return m.group(1) if m else ""

def _yt_download_audio_for_transcript(video_url: str, max_seconds: int = 600) -> tuple[str | None, str]:
    """Download up to max_seconds of audio from a YouTube video to a temp file for Whisper. Returns (path, title) or (None, title)."""
    vid = _yt_extract_id(video_url) or _yt_video_id(video_url)
    if not vid:
        return None, ""
    temp_dir = None
    try:
        import yt_dlp
        temp_dir = tempfile.mkdtemp()
        out_path = os.path.join(temp_dir, "audio.%(ext)s")
        opts = {
            "quiet": True, "no_warnings": True, "noplaylist": True,
            "format": "bestaudio[ext=m4a]/bestaudio/best",
            "outtmpl": out_path,
            "postprocessor_args": {"ffmpeg": ["-t", str(max_seconds)]},
            "extractor_args": {"youtube": {"player_client": "android,web"}},
        }
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(f"https://www.youtube.com/watch?v={vid}", download=True)
        title = (info.get("title") or "").strip() or vid
        for ext in ("m4a", "webm", "mp3", "opus"):
            p = os.path.join(temp_dir, f"audio.{ext}")
            if os.path.isfile(p) and os.path.getsize(p) > 1000:
                return p, title
    except Exception:
        pass
    if temp_dir and os.path.isdir(temp_dir):
        try:
            for f in os.listdir(temp_dir):
                try: os.unlink(os.path.join(temp_dir, f))
                except Exception: pass
            os.rmdir(temp_dir)
        except Exception: pass
    return None, vid

def _record_shared_song(url: str) -> None:
    """Record that this song was shared so we prefer others next time."""
    vid = _yt_video_id(url)
    if not vid: return
    now = time.time()
    with _shared_songs_lock:
        data = _load_json(_SHARED_SONGS_FILE, {})
        data[vid] = now
        # Keep only last 50 entries to avoid unbounded growth
        if len(data) > 50:
            by_ts = sorted(data.items(), key=lambda x: x[1])
            data = dict(by_ts[-50:])
        _save_json(_SHARED_SONGS_FILE, data)

def _get_last_shared_video_id() -> str | None:
    """Video id from the most recent successful share (for sequential Share Song rotation)."""
    with _shared_songs_lock:
        data = _load_json(_SHARED_SONGS_FILE, {})
    if not data:
        return None
    vid, _ = max(data.items(), key=lambda x: float(x[1]))
    return vid or None

def _parse_youtube_atom_feed(xml_text: str) -> list[dict]:
    """Parse YouTube Atom (channel or playlist RSS) into {title, url, video_id} entries."""
    root = ET.fromstring(xml_text)
    ns = {"a": "http://www.w3.org/2005/Atom", "yt": "http://www.youtube.com/xml/schemas/2015"}
    songs: list[dict] = []
    for e in root.findall("a:entry", ns):
        title = (e.findtext("a:title", default="", namespaces=ns) or "").strip()
        vid = (e.findtext("yt:videoId", default="", namespaces=ns) or "").strip()
        link_el = e.find("a:link[@rel='alternate']", ns)
        link = (link_el.attrib.get("href", "") if link_el is not None else "") or (f"https://youtu.be/{vid}" if vid else "")
        if title and link:
            songs.append({"title": title, "url": link, "video_id": vid})
    return songs


def _youtube_rss_feed_urls() -> list[str]:
    """Ordered YouTube Atom RSS URLs — all use feeds/videos.xml (official RSS)."""
    urls: list[str] = []
    if _YOUTUBE_RSS_URL:
        urls.append(_YOUTUBE_RSS_URL)
    # Uploads playlist feed (UU…) — reliable for Topic / artist channels.
    if YT_FEED_URL_PLAYLIST:
        urls.append(YT_FEED_URL_PLAYLIST)
    # Channel feed (UC…) — fallback.
    urls.append(YT_FEED_URL)
    seen: set[str] = set()
    out: list[str] = []
    for u in urls:
        u = (u or "").strip()
        if u and u not in seen:
            seen.add(u)
            out.append(u)
    return out


def _pick_next_rotated_song(songs: list[dict]) -> dict | None:
    """Newest-first list of {title, url, video_id}; returns {title, url} for share."""
    if not songs:
        return None
    last_vid = _get_last_shared_video_id()
    if not last_vid:
        chosen = songs[0]
    else:
        idx = next((i for i, s in enumerate(songs) if s.get("video_id") == last_vid), None)
        if idx is None:
            chosen = songs[0]
        else:
            chosen = songs[(idx + 1) % len(songs)]
    return {"title": chosen["title"], "url": chosen["url"]}


def _list_channel_songs_youtube_playlist_api() -> tuple[bool, list[dict] | str]:
    """Uploads playlist via Data API (works when Atom RSS is flaky)."""
    if not YOUTUBE_API_KEY or not _YT_UPLOADS_PLAYLIST_ID:
        return False, "no API key or uploads playlist id"
    try:
        params = {
            "part": "snippet,contentDetails",
            "playlistId": _YT_UPLOADS_PLAYLIST_ID,
            "maxResults": 50,
            "key": YOUTUBE_API_KEY,
        }
        qs = urllib.parse.urlencode(params)
        url = f"https://www.googleapis.com/youtube/v3/playlistItems?{qs}"
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=25) as r:
            data = json.loads(r.read().decode("utf-8", errors="replace") or "{}")
        items = data.get("items") or []
        songs: list[dict] = []
        for it in items:
            vid = ((it.get("contentDetails") or {}).get("videoId") or "").strip()
            title = ((it.get("snippet") or {}).get("title") or "").strip()
            if vid and title:
                songs.append({"title": title, "url": f"https://youtu.be/{vid}", "video_id": vid})
        if not songs:
            return False, "empty playlist from YouTube API"
        return True, songs
    except urllib.error.HTTPError as e:
        try:
            body = e.read().decode("utf-8", errors="replace")
            msg = (json.loads(body).get("error") or {}).get("message") or str(e)
        except Exception:
            msg = str(e)
        return False, f"YouTube API playlistItems: {msg}"
    except Exception as e:
        return False, str(e)


def _list_channel_songs_ytdlp() -> tuple[bool, list[dict] | str]:
    """Channel /videos tab via yt-dlp when RSS and API are unavailable."""
    try:
        import yt_dlp
    except Exception:
        return False, "yt_dlp not available"
    channel_url = (YT_CHANNEL_URL or "").strip()
    if not channel_url:
        channel_url = f"https://www.youtube.com/channel/{YT_CHANNEL_ID}/videos"
    opts = {
        "quiet": True,
        "skip_download": True,
        "extract_flat": True,
        "playlistend": 50,
        "extractor_args": {"youtube": {"player_client": "android,web,mweb"}},
    }
    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(channel_url, download=False)
    except Exception as e:
        return False, str(e)
    entries = (info or {}).get("entries") or []
    songs: list[dict] = []
    for ent in entries:
        if not isinstance(ent, dict):
            continue
        vid = (ent.get("id") or "").strip()
        title = (ent.get("title") or "").strip()
        if not vid or not title:
            continue
        songs.append({"title": title, "url": f"https://youtu.be/{vid}", "video_id": vid})
    if not songs:
        return False, "no videos from yt-dlp"
    return True, songs


def _get_next_channel_song() -> tuple[bool, dict | str]:
    """Pick the next video: Atom RSS first, then YouTube Data API uploads playlist, then yt-dlp."""
    feed_candidates = _youtube_rss_feed_urls()

    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
        ),
        "Accept": "application/atom+xml,application/xml;q=0.9,*/*;q=0.8",
    }
    last_err: Exception | str | None = None
    for feed_url in feed_candidates:
        try:
            req = urllib.request.Request(feed_url, headers=headers)
            with urllib.request.urlopen(req, timeout=25) as r:
                body = r.read().decode("utf-8", errors="replace")
            songs = _parse_youtube_atom_feed(body)
            if not songs:
                continue
            picked = _pick_next_rotated_song(songs)
            if picked:
                return True, picked
        except Exception as e:
            last_err = e
            continue

    errs: list[str] = []
    if last_err is not None:
        errs.append(f"RSS: {last_err}")

    ok_api, data_api = _list_channel_songs_youtube_playlist_api()
    if ok_api and isinstance(data_api, list):
        picked = _pick_next_rotated_song(data_api)
        if picked:
            return True, picked
    elif isinstance(data_api, str):
        errs.append(f"YouTube API: {data_api}")

    ok_ydl, data_ydl = _list_channel_songs_ytdlp()
    if ok_ydl and isinstance(data_ydl, list):
        picked = _pick_next_rotated_song(data_ydl)
        if picked:
            return True, picked
    elif isinstance(data_ydl, str):
        errs.append(f"yt-dlp: {data_ydl}")

    if errs:
        return False, "Could not get channel videos. " + " | ".join(errs)
    return False, "No songs in channel feed."


def _social_share_fallback_song() -> dict | None:
    """Fixed title+URL when RSS feed is unavailable (same link used for X and Facebook share)."""
    url = (_SOCIAL_SHARE_FALLBACK_URL or "").strip()
    if not url:
        return None
    title = (_SOCIAL_SHARE_FALLBACK_TITLE or "").strip() or "Latest track"
    return {"title": title[:200], "url": url, "video_id": ""}


def _get_next_channel_song_for_share() -> tuple[bool, dict | str]:
    """Like _get_next_channel_song but uses SOCIAL_SHARE_FALLBACK_* when the feed fails."""
    ok, data = _get_next_channel_song()
    if ok and isinstance(data, dict):
        return True, data
    fb = _social_share_fallback_song()
    if fb:
        return True, fb
    err = str(data)
    hint = (
        " Uses **YouTube Atom RSS** only (`feeds/videos.xml`). Set **YOUTUBE_RSS_URL** to a working feed URL, "
        "or **YOUTUBE_CHANNEL_ID** (playlist + channel feeds are tried). "
        "Or **SOCIAL_SHARE_FALLBACK_URL** + **SOCIAL_SHARE_FALLBACK_TITLE** if RSS stays unavailable."
    )
    return False, err + hint

# ── Music state ───────────────────────────────────────────────────────────────

def _music_state(guild_id: int) -> dict:
    with _music_lock:
        if guild_id not in _music_states:
            _music_states[guild_id] = {"queue": deque(), "current": None}
        return _music_states[guild_id]

def _clear_music(guild_id: int):
    with _music_lock:
        _music_states[guild_id] = {"queue": deque(), "current": None}

def _start_track(guild_id: int, vc: discord.VoiceClient) -> bool:
    state = _music_state(guild_id)
    if vc.is_playing() or vc.is_paused(): return True
    if not state["queue"]: state["current"] = None; return False
    track = state["queue"].popleft()
    track["_retry"] = int(track.get("_retry") or 0)
    track["_started"] = time.monotonic()
    state["current"] = track
    local = (track.get("local_path") or "").strip()
    if local and os.path.isfile(local):
        time.sleep(1.5)
        source = discord.FFmpegPCMAudio(local.replace("\\","/"), before_options="-re -nostdin", options=_FFMPEG_OPTS)
    else:
        hdrs = track.get("http_headers") or {}
        hdr_str = "".join(f"{k}: {v}\r\n" for k, v in hdrs.items() if v)
        before = f'{_FFMPEG_BEFORE}{" -headers " + chr(34) + hdr_str.strip() + chr(34) if hdr_str else ""} -nostdin'
        source = discord.FFmpegPCMAudio(track["stream_url"], before_options=before, options=_FFMPEG_OPTS)
    def _after(err):
        asyncio.run_coroutine_threadsafe(_on_track_end(guild_id, err), bot.loop)
    vc.play(source, after=_after)
    # Announce
    ch_id = int(track.get("request_channel_id") or 0)
    if ch_id:
        async def _announce():
            ch = bot.get_channel(ch_id)
            if ch:
                dur = _fmt_sec(int(track.get("duration") or 0))
                await ch.send(f"🎵 Now playing: **{track.get('title','?')}**{f' ({dur})' if dur != '0:00' else ''}")
        asyncio.run_coroutine_threadsafe(_announce(), bot.loop)
    return True

async def _on_track_end(guild_id: int, err=None):
    guild = bot.get_guild(guild_id)
    if not guild or not guild.voice_client or not guild.voice_client.is_connected():
        _clear_music(guild_id); return
    state = _music_state(guild_id)
    manual_skip = bool(state.pop("manual_skip", False))
    current = state.get("current")
    if current and not manual_skip:
        started = float(current.get("_started") or 0)
        elapsed = max(0.0, time.monotonic() - started) if started > 0 else 0
        dur = int(current.get("duration") or 0)
        too_soon = elapsed < max(20.0, dur * 0.6) if dur > 0 else elapsed < 8.0
        if too_soon and int(current.get("_retry") or 0) < 1 and not current.get("local_path"):
            ok, refreshed = await asyncio.to_thread(_resolve_track, current.get("web_url",""))
            if ok and isinstance(refreshed, dict):
                refreshed["request_channel_id"] = current.get("request_channel_id")
                refreshed["_retry"] = 1
                state["current"] = None
                state["queue"].appendleft(refreshed)
                _start_track(guild_id, guild.voice_client)
                return
    state["current"] = None
    _start_track(guild_id, guild.voice_client)

# ── News ──────────────────────────────────────────────────────────────────────

def _x_profile_username() -> str:
    """Best-effort X username from configured profile/handle."""
    u = (X_PROFILE_URL or "").strip()
    try:
        p = urllib.parse.urlparse(u)
        if p.netloc and ("x.com" in p.netloc.lower() or "twitter.com" in p.netloc.lower()):
            parts = [seg for seg in p.path.split("/") if seg]
            if parts:
                cand = parts[0].strip().lstrip("@")
                if re.fullmatch(r"[A-Za-z0-9_]{1,30}", cand):
                    return cand
    except Exception:
        pass
    h = (X_HANDLE or "").strip().lstrip("@")
    if re.fullmatch(r"[A-Za-z0-9_]{1,30}", h):
        return h
    return ""


def _fetch_x_headlines(limit: int = 8) -> tuple[bool, list[dict] | str]:
    """Fetch recent posts from X profile via public RSS mirrors."""
    user = _x_profile_username()
    if not user:
        return False, "X profile username is not configured."
    mirrors = [
        f"https://nitter.net/{user}/rss",
        f"https://nitter.poast.org/{user}/rss",
        f"https://nitter.1d4.us/{user}/rss",
    ]
    last_err = "Could not reach X feed mirrors."
    for feed_url in mirrors:
        try:
            req = urllib.request.Request(feed_url, headers={"User-Agent": "Mozilla/5.0"})
            with urllib.request.urlopen(req, timeout=12) as r:
                raw = r.read().decode("utf-8", errors="replace")
            root = ET.fromstring(raw)
            rows: list[dict] = []
            for item in root.findall(".//item"):
                t = (item.findtext("title") or "").strip()
                l = (item.findtext("link") or "").strip()
                p = (item.findtext("pubDate") or "").strip()
                if not t:
                    continue
                if t.lower().startswith("rt by @"):
                    continue
                if t.lower().startswith(f"@{user.lower()}:"):
                    t = t.split(":", 1)[1].strip() or t
                rows.append({"title": t, "link": l, "published": p})
                if len(rows) >= max(1, limit):
                    break
            if rows:
                return True, rows
            last_err = "X feed returned no recent posts."
        except Exception as ex:
            last_err = str(ex)[:160] or "X feed fetch failed."
            continue
    return False, last_err


def _fetch_news(limit: int = 8) -> tuple[bool, str]:
    ok_x, x_rows = _fetch_x_headlines(limit=max(4, min(12, int(limit or 8))))
    if not ok_x:
        return False, f"Could not fetch latest from X: {x_rows}"
    top = x_rows if isinstance(x_rows, list) else []
    if not top:
        return False, "Could not fetch latest from X."
    headlines_only = [str(it.get("title") or "").strip() for it in top if str(it.get("title") or "").strip()]
    x_user = _x_profile_username() or "profile"
    try:
        summary = ollama_chat(
            "Summarize these latest X updates in a short, readable paragraph (2–4 sentences). "
            "Keep it factual and concise. Do not include URLs.\n\nUpdates:\n" + "\n".join(f"• {h}" for h in headlines_only),
            system="You are a concise social feed summarizer. Output only the summary.",
            model=OLLAMA_MODEL,
        )
        summary = (summary or "").strip()
        if summary:
            return True, f"📰 **Latest from X (@{x_user}):**\n\n" + summary
    except Exception:
        pass
    return True, f"📰 **Latest from X (@{x_user}):**\n\n" + "\n\n".join(
        f"{i}. {str(it.get('title') or '').strip()}" for i, it in enumerate(top, 1)
    )


def _fetch_topic_news(topic: str, limit: int = 6) -> tuple[bool, str]:
    """Fetch current web news for a topic (today-focused) and summarize with sources."""
    topic = (topic or "").strip()
    if not topic:
        return False, "Usage: ask for `news about <topic>` (e.g. `news about AI today`)."

    rows: list[dict] = []
    # Bias toward current reporting with explicit "today" and date tokens.
    date_tokens = datetime.now(timezone.utc).strftime("%Y-%m-%d %b %d %Y")
    query = f"{topic} news today {date_tokens}"
    try:
        q = urllib.parse.quote(query, safe="")
        url = f"https://duckduckgo.com/html/?q={q}"
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=20) as r:
            html_text = r.read().decode("utf-8", errors="replace")
        links = re.finditer(
            r'<a[^>]+class="result__a"[^>]+href="([^"]+)"[^>]*>(.*?)</a>',
            html_text,
            re.I | re.S,
        )
        snippets = [
            re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]+>", " ", s))).strip()
            for s in re.findall(r'<a[^>]+class="result__snippet"[^>]*>(.*?)</a>', html_text, re.I | re.S)
        ]
        for i, m in enumerate(links):
            href = html.unescape(m.group(1))
            title = re.sub(r"<[^>]+>", " ", m.group(2))
            title = re.sub(r"\s+", " ", html.unescape(title)).strip()
            if not href or not title:
                continue
            rows.append(
                {
                    "title": title[:160],
                    "url": href,
                    "snippet": snippets[i] if i < len(snippets) else "",
                }
            )
            if len(rows) >= max(3, min(10, int(limit or 6))):
                break
    except Exception as e:
        return False, f"News search failed: {e}"

    if not rows:
        return False, f"I could not find current web results for **{topic}**."

    # Prefer concise, current-context summary with source links.
    bullets = []
    for it in rows[:6]:
        t = (it.get("title") or "").strip()
        s = (it.get("snippet") or "").strip()
        bullets.append(f"• {t}" + (f" — {s}" if s else ""))
    try:
        summary = ollama_chat(
            "Summarize today's news on the requested topic in 3–6 short bullet points. "
            "Use only the provided search snippets; if uncertain, say uncertain.\n\n"
            f"Topic: {topic}\n\nSearch snippets:\n" + "\n".join(bullets),
            system=(
                "You are a careful news summarizer. Prefer current-day relevance, avoid speculation, "
                "and output plain bullet points only."
            ),
            model=OLLAMA_MODEL,
        ).strip()
    except Exception:
        summary = ""

    lines = [f"📰 **Current news on: {topic}**"]
    if summary:
        lines.append("")
        lines.append(summary[:2200])
    else:
        lines.append("")
        for i, it in enumerate(rows[:5], 1):
            t = (it.get("title") or "").strip()
            s = (it.get("snippet") or "").strip()
            lines.append(f"{i}. **{t}**" + (f" — {s}" if s else ""))
    lines.append("")
    lines.append("Sources:")
    for it in rows[:5]:
        u = (it.get("url") or "").strip()
        t = (it.get("title") or "").strip()
        if u:
            lines.append(f"- {t}: {u}")
    return True, "\n".join(lines[:120])

# ── Search ────────────────────────────────────────────────────────────────────

def _search(query: str) -> tuple[bool, str]:
    """Open Google search in the user's default browser."""
    query = (query or "").strip()
    if not query:
        return False, "Usage: !search <query>"
    try:
        url = "https://www.google.com/search?q=" + urllib.parse.quote(query, safe="")
        webbrowser.open(url)
        return True, f"Opened Google: **{query[:80]}**"
    except Exception as e:
        return False, str(e)

def _yt_channel_analytics(days: int = 30, limit: int = 5) -> tuple[bool, str]:
    """Return top videos by traction (API mode if key exists; scrape-style fallback via yt-dlp)."""
    if not YT_CHANNEL_ID:
        return False, "Set **YOUTUBE_CHANNEL_ID** in `.env`."

    days = max(1, min(int(days or 30), 365))
    limit = max(1, min(int(limit or 5), 10))

    def _to_int(v) -> int:
        try:
            return int(v)
        except Exception:
            return 0

    now = datetime.now(timezone.utc)
    published_after = (now - timedelta(days=days)).isoformat().replace("+00:00", "Z")

    def _rank_rows(rows: list[dict], mode: str) -> tuple[bool, str]:
        ranked = []
        for r in rows:
            title = (r.get("title") or "Untitled").strip()
            vid = (r.get("id") or "").strip()
            pub_dt = r.get("published_dt") or now
            age_days = max(1.0, (now - pub_dt).total_seconds() / 86400.0)
            views = _to_int(r.get("views"))
            likes = _to_int(r.get("likes"))
            comments = _to_int(r.get("comments"))
            engagement = likes + comments
            views_per_day = views / age_days
            traction = views_per_day + (likes * 8.0) + (comments * 20.0)
            ranked.append({
                "title": title, "id": vid, "views": views, "likes": likes, "comments": comments,
                "engagement": engagement, "views_per_day": views_per_day, "traction": traction
            })
        if not ranked:
            return False, "No usable YouTube video data found."
        ranked.sort(key=lambda x: x["traction"], reverse=True)
        top = ranked[:limit]
        lines = [f"📊 **YouTube traction** (last {days} days, top {len(top)}) — source: {mode}"]
        for i, it in enumerate(top, 1):
            er = (it["engagement"] / it["views"] * 100.0) if it["views"] > 0 else 0.0
            url = f"https://www.youtube.com/watch?v={it['id']}" if it["id"] else ""
            lines.append(
                f"{i}. **{it['title'][:88]}**\n"
                f"   Views: {it['views']:,} | Likes: {it['likes']:,} | Comments: {it['comments']:,} | "
                f"Engagement: {er:.2f}% | Views/day: {it['views_per_day']:.1f}\n"
                f"   {url}".rstrip()
            )
        return True, "\n".join(lines)

    # Mode A: YouTube Data API (more reliable metrics)
    if YOUTUBE_API_KEY:
        def _api_json(base: str, params: dict, timeout: int = 25) -> dict:
            qs = urllib.parse.urlencode(params)
            url = f"{base}?{qs}"
            req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return json.loads(r.read().decode("utf-8", errors="replace") or "{}")
        try:
            search_data = _api_json(
                "https://www.googleapis.com/youtube/v3/search",
                {
                    "part": "snippet",
                    "channelId": YT_CHANNEL_ID,
                    "type": "video",
                    "order": "date",
                    "maxResults": min(50, max(10, limit * 8)),
                    "publishedAfter": published_after,
                    "key": YOUTUBE_API_KEY,
                },
            )
            items = search_data.get("items") or []
            video_ids = []
            for it in items:
                vid = (((it or {}).get("id") or {}).get("videoId") or "").strip()
                if vid and vid not in video_ids:
                    video_ids.append(vid)
            if video_ids:
                videos_data = _api_json(
                    "https://www.googleapis.com/youtube/v3/videos",
                    {
                        "part": "snippet,statistics",
                        "id": ",".join(video_ids[:50]),
                        "maxResults": 50,
                        "key": YOUTUBE_API_KEY,
                    },
                )
                videos = videos_data.get("items") or []
                rows = []
                for v in videos:
                    sn = v.get("snippet") or {}
                    st = v.get("statistics") or {}
                    pub_s = (sn.get("publishedAt") or "").strip()
                    try:
                        pub_dt = datetime.fromisoformat(pub_s.replace("Z", "+00:00"))
                    except Exception:
                        pub_dt = now
                    rows.append({
                        "title": sn.get("title"),
                        "id": v.get("id"),
                        "published_dt": pub_dt,
                        "views": st.get("viewCount"),
                        "likes": st.get("likeCount"),
                        "comments": st.get("commentCount"),
                    })
                ok, out = _rank_rows(rows, "YouTube Data API")
                if ok:
                    return True, out
        except urllib.error.HTTPError as e:
            try:
                body = e.read().decode("utf-8", errors="replace")
                msg = (json.loads(body).get("error") or {}).get("message") or str(e)
            except Exception:
                msg = str(e)
            # fall through to scrape-style fallback
            api_err = f"YouTube API error ({msg}); trying scrape fallback."
        except Exception as e:
            api_err = f"YouTube API failed ({e}); trying scrape fallback."
        else:
            api_err = ""
    else:
        api_err = ""

    # Mode B: scrape-style fallback via yt-dlp channel metadata (no API key needed)
    try:
        import yt_dlp
    except Exception:
        hint = " Install `yt-dlp` or set `YOUTUBE_API_KEY`."
        return False, ((api_err + " ") if api_err else "") + "Scrape fallback unavailable." + hint
    try:
        channel_url = (YT_CHANNEL_URL or "").strip()
        if not channel_url:
            channel_url = f"https://www.youtube.com/channel/{YT_CHANNEL_ID}/videos"
        opts = {
            "quiet": True,
            "skip_download": True,
            "extract_flat": False,
            "playlistend": min(80, max(20, limit * 12)),
            "extractor_args": {"youtube": {"player_client": "android,web,mweb"}},
        }
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(channel_url, download=False)
        entries = (info or {}).get("entries") or []
        rows = []
        cutoff = now - timedelta(days=days)
        for e in entries:
            if not isinstance(e, dict):
                continue
            vid = (e.get("id") or "").strip()
            title = (e.get("title") or "").strip()
            ts = e.get("timestamp")
            if ts:
                pub_dt = datetime.fromtimestamp(float(ts), tz=timezone.utc)
            else:
                up = (e.get("upload_date") or "").strip()
                try:
                    pub_dt = datetime.strptime(up, "%Y%m%d").replace(tzinfo=timezone.utc) if up else now
                except Exception:
                    pub_dt = now
            if pub_dt < cutoff:
                continue
            rows.append({
                "title": title,
                "id": vid,
                "published_dt": pub_dt,
                "views": e.get("view_count") or 0,
                "likes": e.get("like_count") or 0,
                "comments": e.get("comment_count") or 0,
            })
        if not rows:
            return False, ((api_err + " ") if api_err else "") + f"No recent videos found in the last **{days}** days."
        ok, out = _rank_rows(rows, "yt-dlp scrape fallback")
        if ok and api_err:
            out = f"ℹ️ {api_err}\n\n" + out
        return ok, out
    except Exception as e:
        prefix = (api_err + " ") if api_err else ""
        return False, prefix + f"YouTube scrape fallback failed: {e}"

def _scrape_website(url: str, instruction: str = "", post_to_discord: str = "") -> tuple[bool, str]:
    """Scrape a website, use LLM to extract specific info, optionally post to a Discord channel."""
    url = (url or "").strip()
    if not url:
        return False, "Usage: !scrape <url> <what to extract> [post:#channel-name]"
    if not url.startswith(("http://", "https://")):
        url = "https://" + url

    # Fetch the page
    try:
        req = urllib.request.Request(url, headers={
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/125.0 Safari/537.36",
            "Accept": "text/html,application/xhtml+xml,*/*",
        })
        with urllib.request.urlopen(req, timeout=30) as r:
            raw_html = r.read().decode("utf-8", errors="replace")
    except Exception as e:
        return False, f"Failed to fetch **{url}**: {e}"

    # Strip HTML tags to get plain text
    text = re.sub(r"<script[^>]*>.*?</script>", " ", raw_html, flags=re.I | re.S)
    text = re.sub(r"<style[^>]*>.*?</style>", " ", text, flags=re.I | re.S)
    text = re.sub(r"<[^>]+>", " ", text)
    text = html.unescape(text)
    text = re.sub(r"\s+", " ", text).strip()

    if not text or len(text) < 30:
        return False, f"Page at **{url}** returned no readable content."

    # Truncate for LLM context window
    page_text = text[:12000]

    if not instruction.strip():
        instruction = "Summarize the key information from this page."

    system = (
        "You are Luna, a helpful assistant. The user scraped a website and wants specific information extracted. "
        "Below is the raw text content of the page. Follow the user's instruction to extract exactly what they asked for. "
        "Be concise, well-formatted (use bullet points or numbered lists where appropriate), and accurate. "
        "Only include information that is actually on the page. Output ONLY the extracted information."
    )
    result = ollama_chat(
        f"Website: {url}\n\nPage content:\n{page_text}\n\nInstruction: {instruction}",
        system=system,
    )
    if not result or result.startswith("Error:") or "Ollama" in result:
        return False, f"LLM failed to process the page content: {result}"

    # Post to Discord channel if requested
    discord_msg = ""
    if post_to_discord and bot.is_ready():
        ch_name = post_to_discord.strip().lstrip("#")
        posted = False
        for guild in bot.guilds:
            for ch in guild.text_channels:
                if ch.name == ch_name:
                    header = f"**Scraped from** <{url}>\n**Query:** {instruction[:200]}\n\n"
                    full_msg = header + result
                    # Discord 2000 char limit — split if needed
                    async def _post():
                        chunks = [full_msg[i:i+1990] for i in range(0, len(full_msg), 1990)]
                        for chunk in chunks:
                            await ch.send(chunk)
                    import asyncio as _aio
                    try:
                        _aio.run_coroutine_threadsafe(_post(), bot.loop).result(timeout=15)
                        posted = True
                        discord_msg = f"\n\n📤 Posted to **#{ch_name}**"
                    except Exception as e:
                        discord_msg = f"\n\n⚠️ Could not post to #{ch_name}: {e}"
                    break
            if posted:
                break
        if not posted and not discord_msg:
            discord_msg = f"\n\n⚠️ Channel **#{ch_name}** not found."

    return True, result + discord_msg


def _research_content(topic: str) -> tuple[bool, str]:
    """Research a topic and return a brief optimized for audiobook/eBook creation."""
    topic = (topic or "").strip()
    if not topic:
        return False, "Usage: !research <topic>"
    # Pull links from DuckDuckGo HTML (no API key needed)
    links = []
    try:
        q = urllib.parse.quote(topic, safe="")
        url = f"https://duckduckgo.com/html/?q={q}"
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=20) as r:
            html_text = r.read().decode("utf-8", errors="replace")
        # result links
        for m in re.finditer(r'<a[^>]+class="result__a"[^>]+href="([^"]+)"[^>]*>(.*?)</a>', html_text, re.I | re.S):
            href = html.unescape(m.group(1))
            title = re.sub(r"<[^>]+>", " ", m.group(2))
            title = re.sub(r"\s+", " ", html.unescape(title)).strip()
            if href and title:
                links.append({"title": title[:120], "url": href})
            if len(links) >= 8:
                break
    except Exception as e:
        return False, f"Research search failed: {e}"
    if not links:
        return False, "No research sources found."

    source_lines = [f"- {x['title']} ({x['url']})" for x in links[:8]]
    prompt = (
        f"Topic: {topic}\n\n"
        "You are creating a research brief for writing an audiobook or eBook.\n"
        "Using the sources below, produce:\n"
        "1) A concise synthesis (5-8 bullets)\n"
        "2) A suggested chapter/section outline (6-10 items)\n"
        "3) A list of key claims to verify\n"
        "4) Source shortlist with why each source is useful\n\n"
        "Sources:\n" + "\n".join(source_lines)
    )
    try:
        brief = ollama_chat(prompt, system="Be practical, structured, and factual. Keep it concise.", model=OLLAMA_CHAT)
        brief = (brief or "").strip()
        if not brief:
            return False, "Could not generate research brief."
    except Exception as e:
        return False, str(e)
    src = "\n".join(f"{i+1}. {x['title']} — {x['url']}" for i, x in enumerate(links[:8]))
    try:
        os.makedirs(_RESEARCH_BRIEFS_DIR, exist_ok=True)
        slug = _topic_slug(topic)
        ts = datetime.now().strftime("%Y%m%d_%H%M")
        brief_path = os.path.join(_RESEARCH_BRIEFS_DIR, f"{slug}_{ts}.txt")
        with open(brief_path, "w", encoding="utf-8") as bf:
            bf.write(f"Topic: {topic}\n\n{brief}\n\nSources:\n{src}\n")
    except Exception:
        pass
    return True, f"📚 **Research brief: {topic}**\n\n{brief[:2200]}\n\n**Sources**\n{src}"

def _topic_slug(topic: str, max_len: int = 40) -> str:
    s = re.sub(r"[^\w\s-]", "", (topic or "").lower())[:max_len].strip().replace(" ", "_")
    s = re.sub(r"_+", "_", s).strip("_")
    return s or "topic"

def _find_latest_topic_file(dir_path: str, slug: str) -> str | None:
    """Newest `{slug}_*.txt` in dir, or None."""
    if not dir_path or not os.path.isdir(dir_path) or not slug:
        return None
    prefix = slug + "_"
    candidates: list[tuple[float, str]] = []
    try:
        for name in os.listdir(dir_path):
            if name.startswith(prefix) and name.lower().endswith(".txt"):
                p = os.path.join(dir_path, name)
                if os.path.isfile(p):
                    candidates.append((os.path.getmtime(p), p))
    except Exception:
        return None
    if not candidates:
        return None
    candidates.sort(key=lambda x: -x[0])
    return candidates[0][1]

def _load_research_brief_file_for_tts(path: str) -> str:
    with open(path, encoding="utf-8") as f:
        text = f.read()
    lines = text.splitlines()
    if lines and lines[0].lower().startswith("topic:"):
        text = "\n".join(lines[1:]).strip()
    return text

def _load_story_script_file_for_tts(path: str) -> str:
    with open(path, encoding="utf-8") as f:
        text = f.read()
    parts = re.split(r"(?i)\n\s*sources:\s*\n", text, 1)
    return (parts[0] if parts else text).strip()

def _ollama_scriptwriter(prompt: str, system: str, timeout: int = 180) -> str:
    """Ollama for long scripts — no _sanitize_luna_reply (avoids mangling narration)."""
    try:
        return _ollama_chat_once(prompt, system, None, None, (OLLAMA_CHAT or OLLAMA_MODEL).strip(), timeout=timeout).strip()
    except Exception:
        return ""

def _parse_audiobook_topic_and_duration(raw: str) -> tuple[str, int | None]:
    """Extract duration:NN from topic; returns (clean_topic, minutes or None)."""
    s = (raw or "").strip()
    if not s:
        return "", None
    m = re.search(r"\bduration[:\s]*(\d+)\b", s, re.I)
    if not m:
        return s, None
    mins = int(m.group(1))
    mins = max(10, min(120, mins))
    rest = (s[: m.start()] + s[m.end() :]).strip()
    rest = re.sub(r"\s+", " ", rest).strip()
    return rest, mins

def _parse_audiobook_request(raw: str) -> tuple[str, int | None, str | None]:
    """Parse `!audiobook create …` payload: (topic, minutes, source_mode).
    source_mode: None (auto), 'brief', 'story', or 'both'. Trailing flags: --brief, --story, --both."""
    s = (raw or "").strip()
    if not s:
        return "", None, None
    source_mode: str | None = None
    low = s.lower()
    # Longest suffix first so --research-brief wins over --brief
    for suffix, mode in (
        ("--research-brief", "brief"),
        ("--from-brief", "brief"),
        ("--from-story", "story"),
        ("--brief", "brief"),
        ("--story", "story"),
        ("--script", "story"),
        ("--both", "both"),
        ("--all", "both"),
    ):
        if low.endswith(suffix):
            s = s[: -len(suffix)].strip().rstrip(",;")
            source_mode = mode
            break
    topic, mins = _parse_audiobook_topic_and_duration(s)
    return topic, mins, source_mode

def _ddg_research_links(topic: str) -> tuple[bool, list | str]:
    """DuckDuckGo HTML result links for a query."""
    links = []
    try:
        q = urllib.parse.quote(topic, safe="")
        url = f"https://duckduckgo.com/html/?q={q}"
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=20) as r:
            html_text = r.read().decode("utf-8", errors="replace")
        for m in re.finditer(r'<a[^>]+class="result__a"[^>]+href="([^"]+)"[^>]*>(.*?)</a>', html_text, re.I | re.S):
            href = html.unescape(m.group(1))
            title = re.sub(r"<[^>]+>", " ", m.group(2))
            title = re.sub(r"\s+", " ", html.unescape(title)).strip()
            if href and title:
                links.append({"title": title[:120], "url": href})
            if len(links) >= 8:
                break
    except Exception as e:
        return False, f"Research search failed: {e}"
    if not links:
        return False, "No research sources found."
    return True, links

def _research_story_build(topic: str, target_minutes: int | None = None) -> tuple[bool, dict | str]:
    """Build story-style script + sources. Short form ~15–20 min read; long form multi-chapter for 30–60+ min listen."""
    topic = (topic or "").strip()
    if not topic:
        return False, "Usage: !research_story <topic>"
    ok_links, links_or_err = _ddg_research_links(topic)
    if not ok_links:
        return False, str(links_or_err)
    links = links_or_err

    source_lines = [f"- {x['title']} ({x['url']})" for x in links[:8]]
    sources_block = "\n".join(source_lines)
    opener = "Welcome readers, to Luna's Audiobooks. Hope you enjoy today's story."

    # ── Short audiobook (~12–20 min spoken): single generation ─────────────────
    if not target_minutes or target_minutes < 18:
        prompt = (
            f"Topic: {topic}\n\n"
            "Write a story-style audiobook script based on the topic and sources below.\n"
            "Requirements:\n"
            "- 1200 to 1800 words\n"
            "- Engaging narrative voice, clear transitions, natural spoken rhythm\n"
            "- Include practical takeaways naturally in the story\n"
            "- Do NOT use markdown headings; plain readable text only\n"
            "- End with a short reflective closing paragraph\n\n"
            "Sources:\n" + sources_block
        )
        story = _ollama_scriptwriter(prompt, "You are an audiobook scriptwriter. Output plain text only.", timeout=240)
        if not story:
            return False, "Could not generate story script."
        if not story.lower().startswith("welcome readers"):
            story = opener + "\n\n" + story
        return True, {
            "topic": topic,
            "story": story,
            "links": links,
            "target_minutes": target_minutes,
            "chapters": None,
        }

    # ── Long form (YouTube-style 30–90 min): chapters, ~145 words/minute spoken ─
    WPM = 145
    total_words = int(target_minutes * WPM)
    if target_minutes <= 28:
        num_chapters = 6
    elif target_minutes <= 42:
        num_chapters = 8
    elif target_minutes <= 58:
        num_chapters = 10
    else:
        num_chapters = 12
    words_per_chapter = max(500, total_words // num_chapters)

    outline = _ollama_scriptwriter(
        f"Topic: {topic}\n\n"
        f"Plan a {target_minutes}-minute spoken audiobook (about {total_words} words total in {num_chapters} chapters).\n"
        f"List exactly {num_chapters} chapter titles, one per line, numbered:\n"
        "1. First chapter title\n2. Second ...\n"
        "Titles only — no extra text.",
        "Output only the numbered chapter titles. Plain text.",
        timeout=120,
    )
    chapter_titles: list[str] = []
    for line in (outline or "").splitlines():
        line = line.strip()
        m = re.match(r"^\d+[\.\)]\s*(.+)$", line)
        if m:
            chapter_titles.append(m.group(1).strip())
    while len(chapter_titles) < num_chapters:
        chapter_titles.append(f"Part {len(chapter_titles) + 1}")
    chapter_titles = chapter_titles[:num_chapters]

    parts: list[str] = []
    prev_tail = ""
    sys_ch = (
        "You write audiobook narration for listening aloud. Plain text only. "
        "No markdown. No bullet lists unless natural speech. Vivid but accurate when using sources. "
        "Always meet the requested minimum word count with full prose — never replace chapters with summaries."
    )
    for i, ch_title in enumerate(chapter_titles):
        ch = i + 1
        cont = ""
        if ch > 1 and prev_tail:
            cont = (
                "Continue seamlessly from where the previous chapter left off. "
                f"Last lines were: …{prev_tail[-350:]}\n\n"
            )
        if ch == 1:
            ch_prompt = (
                f"Write chapter {ch} of {num_chapters} for a spoken audiobook.\n\n"
                f"Overall topic: {topic}\n"
                f"Chapter title: {ch_title}\n\n"
                f"Target length: **at least {words_per_chapter} words** of continuous narration "
                f"(aim for {words_per_chapter}–{int(words_per_chapter * 1.12)} words — do not stop early).\n"
                f"Start with this exact opening line, then continue: {opener}\n\n"
                f"Sources (ground facts here):\n{sources_block}\n"
            )
        else:
            ch_prompt = (
                f"Write chapter {ch} of {num_chapters} for a spoken audiobook.\n\n"
                f"Overall topic: {topic}\n"
                f"Chapter title: {ch_title}\n\n"
                f"Target length: **at least {words_per_chapter} words** of continuous narration "
                f"(aim for {words_per_chapter}–{int(words_per_chapter * 1.12)} words — do not stop early).\n"
                "Do not repeat the welcome line. Continue the narrative.\n\n"
                f"{cont}"
                f"Sources:\n{sources_block}\n"
            )
        chunk_text = _ollama_scriptwriter(ch_prompt, sys_ch, timeout=360)
        chunk_text = (chunk_text or "").strip()
        if not chunk_text:
            return False, f"Chapter {ch} generation failed or timed out."
        wc = len(chunk_text.split())
        min_accept = max(500, int(words_per_chapter * 0.52))
        if wc < min_accept:
            expand_prompt = (
                ch_prompt
                + f"\n\n---\nYour previous draft was only about {wc} words. "
                f"Rewrite and substantially EXPAND this chapter to **at least {words_per_chapter} words** "
                "of full narration (scenes, detail, transitions — not a summary).\n\n"
                f"Previous draft:\n{chunk_text[:6000]}"
            )
            chunk_text2 = _ollama_scriptwriter(expand_prompt, sys_ch, timeout=420)
            if chunk_text2 and len(chunk_text2.split()) > wc:
                chunk_text = chunk_text2.strip()
        parts.append(chunk_text)
        prev_tail = chunk_text

    story = "\n\n".join(parts)
    return True, {
        "topic": topic,
        "story": story,
        "links": links,
        "target_minutes": target_minutes,
        "chapters": parts,
    }

def _synthesize_audiobook_one_part(script: str, long_form: bool) -> tuple[bool, str | None, float | None, str]:
    """TTS + ffmpeg → one MP3. Returns (ok, temp_mp3_path, duration_sec, err). Caller deletes dirname(path)."""
    temp_dir = tempfile.mkdtemp()
    out_path = os.path.join(temp_dir, "audiobook.mp3")
    try:
        s = (script or "").strip()
        if long_form:
            s = s[:500000]
        else:
            s = s[:12000]
        s = _clean_for_tts(s)
        if not s.strip():
            shutil.rmtree(temp_dir, ignore_errors=True)
            return False, None, None, "empty script"
        chunks = _split_tts(s, max_chars=120)
        if not chunks:
            shutil.rmtree(temp_dir, ignore_errors=True)
            return False, None, None, "no speakable chunks"
        max_seg = 1200 if long_form else 200
        ff_timeout = 1200 if long_form else 240
        list_path = os.path.join(temp_dir, "list.txt")
        paths: list[str] = []
        for i, chunk in enumerate(chunks[:max_seg]):
            audio = _tts_bytes_media(chunk)
            if not audio:
                continue
            seg_path = os.path.join(temp_dir, f"seg_{i:03d}.mp3")
            with open(seg_path, "wb") as f:
                f.write(audio)
            paths.append(seg_path)
        if not paths:
            shutil.rmtree(temp_dir, ignore_errors=True)
            return False, None, None, "TTS failed for all chunks"
        with open(list_path, "w", encoding="utf-8") as f:
            for p in paths:
                p_abs = os.path.abspath(p).replace("\\", "/")
                f.write(f"file '{p_abs}'\n")
        ret = subprocess.run(
            ["ffmpeg", "-y", "-f", "concat", "-safe", "0", "-i", list_path, "-c", "copy", out_path],
            capture_output=True,
            timeout=ff_timeout,
            cwd=temp_dir,
            creationflags=_subprocess_no_window_flags(),
        )
        if ret.returncode != 0 or not os.path.isfile(out_path):
            shutil.rmtree(temp_dir, ignore_errors=True)
            return False, None, None, "Could not merge audio (ffmpeg)"
        listen_secs = _ffprobe_duration_seconds(out_path)
        return True, out_path, listen_secs, ""
    except Exception as e:
        shutil.rmtree(temp_dir, ignore_errors=True)
        return False, None, None, str(e)

def _split_story_into_chapters(text: str) -> list[str]:
    """Split prose into parts for separate MP3s — explicit Chapter/Part markers, else paragraph blocks, else ~850-word chunks."""
    text = (text or "").strip()
    if not text:
        return []
    parts = re.split(
        r"(?m)(?:^|\n)(?:#{1,3}\s*)?(?:Chapter|CHAPTER|Part|PART|Book)\s*\d+[\s\.\-:]*[^\n]*\n|"
        r"(?m)^(?:Chapter|Part)\s+(?:One|Two|Three|Four|Five|Six|Seven|Eight|Nine|Ten|Eleven|Twelve)[^\n]*\n",
        text,
    )
    parts = [p.strip() for p in parts if p and p.strip()]
    if len(parts) >= 2:
        return parts
    blocks = re.split(r"\n\s*\n", text)
    blocks = [b.strip() for b in blocks if len(b.strip()) > 200]
    if len(blocks) >= 2:
        return blocks
    words = text.split()
    if len(words) <= 900:
        return [text]
    chunk_size = 850
    out: list[str] = []
    for i in range(0, len(words), chunk_size):
        chunk = " ".join(words[i : i + chunk_size])
        if chunk.strip():
            out.append(chunk.strip())
    return out if len(out) >= 2 else [text]

def _audiobook_pending_load() -> dict:
    try:
        with open(_AUDIOBOOK_PENDING_FILE, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}

def _audiobook_pending_save(scope: str, data: dict) -> None:
    os.makedirs(_DATA, exist_ok=True)
    all_p = _audiobook_pending_load()
    all_p[scope] = data
    with open(_AUDIOBOOK_PENDING_FILE, "w", encoding="utf-8") as f:
        json.dump(all_p, f, indent=2, ensure_ascii=False)

def _audiobook_pending_clear(scope: str) -> None:
    all_p = _audiobook_pending_load()
    if scope in all_p:
        del all_p[scope]
        try:
            with open(_AUDIOBOOK_PENDING_FILE, "w", encoding="utf-8") as f:
                json.dump(all_p, f, indent=2, ensure_ascii=False)
        except Exception:
            pass

def _audiobook_cancel_pending(scope: str) -> tuple[bool, str]:
    scope = scope or "web"
    if scope not in _audiobook_pending_load():
        return False, "No audiobook chapter queue in progress. Start with **!audiobook create** …"
    _audiobook_pending_clear(scope)
    return True, "Cancelled the remaining chapters. Start again anytime with **!audiobook create** …"

def _audiobook_continue_next(scope: str) -> tuple[bool, str]:
    """Synthesize the next pending chapter MP3; clear queue when done."""
    scope = scope or "web"
    root = (CUSTOM_PODCAST_DIR or "").strip()
    if not root or not os.path.isdir(root):
        return False, f"Set **CUSTOM_PODCAST_DIR** in .env to a writable folder."
    data = _audiobook_pending_load().get(scope)
    if not data:
        return False, "No chapters waiting. Start with **!audiobook create** …"
    chapters = data.get("chapters") or []
    idx = int(data.get("next_index", 0))
    if idx >= len(chapters):
        _audiobook_pending_clear(scope)
        return False, "Nothing left to continue — chapter queue was already finished."
    long_form = bool(data.get("long_form"))
    out_slug = (data.get("out_slug") or "audiobook").strip() or "audiobook"
    ts = (data.get("ts") or datetime.now().strftime("%Y%m%d_%H%M")).strip()
    ch_raw = chapters[idx]
    ok_mp3, tmp_path, listen_secs, err = _synthesize_audiobook_one_part(ch_raw, long_form)
    if not ok_mp3 or not tmp_path:
        return False, err or "TTS failed"
    try:
        dur_tag = _audio_duration_filename_tag(listen_secs)
        name = f"audiobook_ch{idx + 1:02d}_{dur_tag}_{out_slug}_{ts}.mp3"
        dest = os.path.join(root, name)
        shutil.copy2(tmp_path, dest)
        listen_min = (listen_secs or 0) / 60.0
        len_line = ""
        if listen_secs:
            len_line = f" **Length:** ~{listen_min:.1f} min ({int(round(listen_secs))}s)."
        data["next_index"] = idx + 1
        n_total = int(data.get("n_total") or len(chapters))
        n_done = idx + 1
        if data["next_index"] >= len(chapters):
            _audiobook_pending_clear(scope)
            return True, (
                f"Created **{name}** — chapter **{n_done}** of **{n_total}** (last).{len_line} "
                "🎉 **Audiobook complete.** Play with **!podcast** or open your podcast folder."
            )
        _audiobook_pending_save(scope, data)
        left = len(chapters) - data["next_index"]
        return True, (
            f"Created **{name}** — chapter **{n_done}** of **{n_total}**.{len_line}\n\n"
            f"📖 **{left}** chapter(s) left. Say **!audiobook continue** when you want the next, "
            "or **!audiobook cancel** to stop."
        )
    finally:
        if tmp_path:
            shutil.rmtree(os.path.dirname(tmp_path), ignore_errors=True)

def _research_story_script(topic: str) -> tuple[bool, str]:
    """Create a story-style script .txt file from researched sources for audiobook narration."""
    ok, built = _research_story_build(topic)
    if not ok:
        return False, str(built)
    story = (built or {}).get("story", "")
    links = (built or {}).get("links", [])
    topic = (built or {}).get("topic", topic)

    try:
        os.makedirs(_AUDIOBOOK_SCRIPTS_DIR, exist_ok=True)
        slug = _topic_slug(topic) or "story"
        ts = datetime.now().strftime("%Y%m%d_%H%M")
        filename = f"{slug}_{ts}.txt"
        path = os.path.join(_AUDIOBOOK_SCRIPTS_DIR, filename)
        with open(path, "w", encoding="utf-8") as f:
            f.write(story + "\n\nSources:\n")
            for i, s in enumerate(links[:8], 1):
                f.write(f"{i}. {s['title']} — {s['url']}\n")
        return True, f"Story script created: **{filename}** in `{_AUDIOBOOK_SCRIPTS_DIR}`"
    except Exception as e:
        return False, f"Failed to save script file: {e}"

def _create_audiobook_from_research(topic: str, scope: str | None = None) -> tuple[bool, str]:
    """Create audiobook MP3(s). Multi-chapter stories: **chapter 1 only**, then **!audiobook continue** for the rest."""
    sc = (scope or LINKED_SCOPE or "web").strip() or "web"
    raw = (topic or "").strip()
    topic, target_minutes, source_mode = _parse_audiobook_request(raw)
    if not topic:
        return False, (
            "Usage: `!audiobook create <topic> [duration:30] [--brief|--story|--both]` — "
            "`--brief` = saved **!research** only; `--story` = **!research_story** only; "
            "`--both` = require both files; omit flags = auto. Then **!audiobook continue** / **!audiobook cancel**."
        )
    root = (CUSTOM_PODCAST_DIR or "").strip()
    if not root or not os.path.isdir(root):
        return False, "Set **CUSTOM_PODCAST_DIR** in .env to a writable folder (e.g. D:\\Luna 5.0\\Luna's creations) so I can save the audiobook."
    files_slug = _topic_slug(topic)
    brief_path = _find_latest_topic_file(_RESEARCH_BRIEFS_DIR, files_slug)
    story_path = _find_latest_topic_file(_AUDIOBOOK_SCRIPTS_DIR, files_slug)
    use_brief = False
    use_story = False
    used_saved = False
    tm: int | None = target_minutes
    built: dict | None = None
    chapter_texts: list[str] | None = None

    if source_mode == "brief":
        if not brief_path:
            return False, "No saved **research brief** for this topic. Run **!research** with the same topic first."
        use_brief = True
        used_saved = True
    elif source_mode == "story":
        if not story_path:
            return False, "No saved **story script** for this topic. Run **!research_story** with the same topic first."
        use_story = True
        used_saved = True
    elif source_mode == "both":
        if not brief_path or not story_path:
            miss = []
            if not brief_path:
                miss.append("research brief (**!research**)")
            if not story_path:
                miss.append("story script (**!research_story**)")
            return False, "Need both saved files for this topic. Missing: " + " and ".join(miss) + "."
        use_brief = use_story = True
        used_saved = True
    else:
        # auto: use whatever is saved, else generate
        if brief_path or story_path:
            used_saved = True
            use_brief = bool(brief_path)
            use_story = bool(story_path)

    if used_saved:
        brief_raw = _load_research_brief_file_for_tts(brief_path) if use_brief and brief_path else ""
        story_raw = _load_story_script_file_for_tts(story_path) if use_story and story_path else ""
        parts = []
        if brief_raw.strip():
            parts.append(brief_raw.strip())
        if story_raw.strip():
            parts.append(story_raw.strip())
        script = _clean_for_tts("\n\n".join(parts))
        if not script.strip():
            return False, "Saved research/story files were empty. Re-run **!research** and/or **!research_story** for this topic."
        long_form = tm is not None and tm >= 18
        note_src = []
        if use_brief and brief_path:
            note_src.append("research brief")
        if use_story and story_path:
            note_src.append("story script")
        src_note = " + ".join(note_src) if note_src else "saved files"
    else:
        ok, built = _research_story_build(topic, target_minutes=target_minutes)
        if not ok:
            return False, str(built)
        if isinstance(built, dict):
            chapter_texts = built.get("chapters")
        tm = (built or {}).get("target_minutes")
        long_form = tm is not None and tm >= 18
        script = _clean_for_tts((built or {}).get("story", ""))
        src_note = ""

    # Build list of chapter parts (one MP3 each, first now — rest after **!audiobook continue**)
    chapters_list: list[str] | None = None
    if not used_saved and chapter_texts and isinstance(chapter_texts, list) and len(chapter_texts) > 1:
        chapters_list = chapter_texts
    elif used_saved and script.strip():
        spl = _split_story_into_chapters(script)
        if len(spl) > 1:
            chapters_list = spl

    if chapters_list and len(chapters_list) > 1:
        out_slug = re.sub(r"[^\w\s-]", "", topic.lower())[:30].strip().replace(" ", "_") or "audiobook"
        out_slug = re.sub(r"_+", "_", out_slug).strip("_")
        ts = datetime.now().strftime("%Y%m%d_%H%M")
        n_total = len(chapters_list)
        ch0 = chapters_list[0]
        ok_mp3, tmp_path, listen_secs, err = _synthesize_audiobook_one_part(ch0, long_form)
        if not ok_mp3 or not tmp_path:
            return False, "Chapter 1: " + (err or "TTS failed")
        try:
            dur_tag = _audio_duration_filename_tag(listen_secs)
            name = f"audiobook_ch01_{dur_tag}_{out_slug}_{ts}.mp3"
            dest = os.path.join(root, name)
            shutil.copy2(tmp_path, dest)
            listen_min = (listen_secs or 0) / 60.0
            len_line = ""
            if listen_secs:
                len_line = f" **Length:** ~{listen_min:.1f} min ({int(round(listen_secs))}s)."
            _audiobook_pending_save(
                sc,
                {
                    "chapters": chapters_list,
                    "next_index": 1,
                    "long_form": long_form,
                    "out_slug": out_slug,
                    "ts": ts,
                    "topic": topic,
                    "n_total": n_total,
                },
            )
            src_h = ""
            if used_saved:
                src_h = f" (from saved **{src_note}**)"
            return True, (
                f"Created **{name}** — chapter **1** of **{n_total}**{src_h}.{len_line}\n\n"
                f"📖 When you’re ready for chapter 2, say **!audiobook continue** (or **audiobook continue** in chat). "
                f"**!audiobook cancel** skips the rest. Play with **!podcast** or open the folder."
            )
        finally:
            if tmp_path:
                shutil.rmtree(os.path.dirname(tmp_path), ignore_errors=True)

    if long_form:
        script = script[:500000]
    else:
        script = script[:12000]
    if not script.strip():
        return False, "Generated audiobook script was empty."
    ok_mp3, tmp_path, listen_secs, err = _synthesize_audiobook_one_part(script, long_form)
    if not ok_mp3 or not tmp_path:
        return False, err or "Could not create audiobook MP3."
    try:
        out_slug = re.sub(r"[^\w\s-]", "", topic.lower())[:30].strip().replace(" ", "_") or "audiobook"
        out_slug = re.sub(r"_+", "_", out_slug).strip("_")
        ts = datetime.now().strftime("%Y%m%d_%H%M")
        dur_tag = _audio_duration_filename_tag(listen_secs)
        name = f"audiobook_{dur_tag}_{out_slug}_{ts}.mp3"
        dest = os.path.join(root, name)
        shutil.copy2(tmp_path, dest)
        listen_min = (listen_secs or 0) / 60.0
        note = ""
        if used_saved:
            note = f" Used your saved **{src_note}** (`{files_slug}_*.txt` in research_briefs / audiobook_scripts)."
            if long_form and tm:
                note += " (Saved text was used in full; `duration:` does not stretch audio.)"
        elif long_form and tm:
            note = (
                f" Target was **~{tm} min** of speech — filename shows **actual** length (~{listen_min:.1f} min). "
                "Leave Luna running until TTS finishes."
            )
        elif not used_saved:
            note = (
                " Tip: run **!research** and **!research_story** with the same topic first — "
                "the next **!audiobook create** will read those files instead of regenerating."
            )
        if tm and listen_min > 0.5 and listen_min < tm * 0.55:
            if not used_saved:
                note += (
                    f" ⚠️ Output is much shorter than your **~{tm} min** target (~{listen_min:.0f} min actual). "
                    "The generated script was shorter than requested — try a larger Ollama model, run again, or add **!research**/**!research_story** text."
                )
            else:
                note += (
                    f" ⚠️ Audio ~{listen_min:.0f} min — shorter than **~{tm} min** `duration:`; "
                    "length follows your **saved** files, not the duration number."
                )
        len_line = ""
        if listen_secs:
            len_line = f" **Length:** ~{listen_min:.1f} min ({int(round(listen_secs))}s)."
        return True, f"Created **{name}** in your podcast folder.{len_line}{note} Play with **!podcast** or open the folder."
    finally:
        if tmp_path:
            shutil.rmtree(os.path.dirname(tmp_path), ignore_errors=True)

# ── Social automation (Suno/X/Facebook/YouTube/Instagram/WhatsApp/Messenger) ──

def _run_suno(desc: str) -> tuple[bool, str]:
    desc = desc.strip()[:1200]
    if not desc: return False, "Provide a song description."
    if _suno_boot: return False, "Suno login window still open."
    if not _suno_run_lock.acquire(blocking=False): return False, "Suno already running."
    try:
        from playwright.sync_api import sync_playwright
        os.makedirs(SUNO_PROFILE_DIR, exist_ok=True)
        context = None
        try:
            with sync_playwright() as p:
                context = _launch_social_browser(SUNO_PROFILE_DIR, p)
                page = next((pg for pg in context.pages if not pg.is_closed()), None) or context.new_page()
                page.goto(SUNO_CREATE_URL, wait_until="domcontentloaded", timeout=90000)
                page.wait_for_timeout(2000)
                # Check the website: are we on the profile/dashboard (logged in) or on login/landing?
                logged_in = False
                try:
                    # Logged-in page has "My Workspace", "Song Description", or credits in the app UI
                    for sel in ("text=My Workspace", "text=Song Description", "text=credits"):
                        if page.locator(sel).first.count() and page.locator(sel).first.is_visible():
                            logged_in = True
                            break
                    # If we didn't see dashboard, check for login prompts — if visible, we're not logged in
                    if not logged_in:
                        for sel in ("text=Log in", "text=Sign in", "a:has-text('Log in')", "a:has-text('Sign in')", "button:has-text('Log in')", "button:has-text('Sign in')"):
                            if page.locator(sel).first.count() and page.locator(sel).first.is_visible():
                                break
                        else:
                            # No login buttons visible — might be dashboard still loading; treat as logged in and try to find the box
                            logged_in = True
                except Exception:
                    pass
                if not logged_in:
                    try: context.close()
                    except Exception: pass
                    context = None
                    _clear_ready(SUNO_PROFILE_DIR)
                    _bootstrap_window(SUNO_PROFILE_DIR, SUNO_CREATE_URL, "_suno_boot")
                    return False, "Suno needs login. Browser opened — log in, then close the window and try !suno again."
                _mark_ready(SUNO_PROFILE_DIR)
                # Find the Song Description input on the dashboard (Suno uses textarea or contenteditable)
                page.wait_for_timeout(1500)
                target = None
                try:
                    for sel in [
                        "textarea[placeholder*='Describe']", "textarea[placeholder*='song']", "textarea[placeholder*='Song']",
                        "[data-testid*='description']", "[aria-label*='description']", "[aria-label*='Song']",
                        "label:has-text('Song Description') + textarea", "label:has-text('Song Description') ~ textarea",
                        "div[contenteditable='true']", "textarea",
                    ]:
                        loc = page.locator(sel).first
                        if loc.count():
                            try:
                                loc.wait_for(state="visible", timeout=3000)
                                if loc.is_visible():
                                    target = loc
                                    break
                            except Exception:
                                pass
                except Exception:
                    pass
                if target is None:
                    try: context.close()
                    except Exception: pass
                    context = None
                    _clear_ready(SUNO_PROFILE_DIR)
                    _bootstrap_window(SUNO_PROFILE_DIR, SUNO_CREATE_URL, "_suno_boot")
                    return False, "Couldn't find the Song Description box. Log in, then try again."
                try:
                    target.scroll_into_view_if_needed(timeout=3000)
                except Exception:
                    pass
                target.click()
                page.keyboard.press("Control+A")
                page.keyboard.press("Backspace")
                page.keyboard.type(desc, delay=22)
                # Short human-like pause before clicking (reduces bot-like instant actions)
                page.wait_for_timeout(int(random.uniform(1200, 2500)))
                # First: click the main Create button (opens the create card / form)
                clicked = False
                for wait in (800, 1500, 2500):
                    page.wait_for_timeout(wait)
                    for bsel in [
                        "button:has-text('+ Create')", "button:has-text('Create')", "button:has-text('Generate')",
                        "[role='button']:has-text('+ Create')", "[role='button']:has-text('Create')",
                        "a:has-text('+ Create')", "a:has-text('Create')",
                    ]:
                        btn = page.locator(bsel).first
                        if btn.count():
                            try:
                                btn.scroll_into_view_if_needed(timeout=2000)
                                if btn.is_visible():
                                    btn.click(force=True, timeout=5000)
                                    clicked = True
                                    break
                            except Exception:
                                pass
                    if clicked:
                        break
                if not clicked:
                    page.keyboard.press("Enter")
                # Wait 5s for songs to load; if a captcha appears, keep Chrome open until you close it
                page.wait_for_timeout(5000)
                try:
                    while context.pages and any(not pg.is_closed() for pg in context.pages):
                        time.sleep(1)
                except Exception:
                    pass
                try:
                    if context:
                        context.close()
                except Exception:
                    pass
                context = None
                return True, f'Opened Suno, entered "{desc[:200]}", clicked Create. If a captcha appeared, the window stayed open — close it when you\'re done.'
        except Exception as e:
            err = str(e)
            if "Execution context was destroyed" in err or "Target closed" in err or "navigation" in err.lower():
                if context:
                    try:
                        while True:
                            time.sleep(1)
                            try:
                                if not context.pages or all(pg.is_closed() for pg in context.pages):
                                    break
                            except Exception:
                                continue
                    finally:
                        try: context.close()
                        except Exception: pass
                        context = None
                return False, "Browser closed. Log in to Suno next time you try, then close the window when done."
            return False, f"Suno error: {e}"
        finally:
            if context:
                try: context.close()
                except Exception: pass
    except ImportError:
        return False, "Playwright not installed."
    finally:
        try: _suno_run_lock.release()
        except Exception: pass

def _build_x_msg(title: str, url: str) -> str:
    title = title.strip()[:80]
    templates = [
        f'Hey guys, check out this track: "{title}" 🎶\n{url}',
        f'New music drop: "{title}" ✨\nListen here: {url}',
        f'"{title}" — give it a listen 🎧\n{url}',
    ]
    return random.choice(templates)

def _run_x_share(custom_body: str | None = None) -> tuple[bool, str]:
    """Post one tweet on X. Default = next song from channel rotation.

    *custom_body* — when set, post that exact text instead (used by the YouTube-upload
    cross-poster). Channel-song rotation, song record, and song-style result text are
    skipped in that mode.
    """
    song: dict | None = None
    if custom_body is None:
        ok, song = _get_next_channel_song_for_share()
        if not ok: return False, str(song)
    if not _x_lock.acquire(blocking=False): return False, "X share already running."
    try:
        from playwright.sync_api import sync_playwright
        os.makedirs(X_PROFILE_DIR, exist_ok=True)
        context = None
        try:
            with sync_playwright() as p:
                context = _launch_social_browser(X_PROFILE_DIR, p)
                page = context.pages[0] if context.pages else context.new_page()
                page.goto(X_COMPOSE_URL, wait_until="domcontentloaded", timeout=90000)
                page.wait_for_timeout(1000)
                # Check login — same first-time login as Facebook: leave browser open until you close it
                for sel in ("text=Sign in", "text=Log in"):
                    if page.locator(sel).first.count():
                        _clear_ready(X_PROFILE_DIR)
                        try:
                            deadline = time.time() + 60
                            while True:
                                time.sleep(1)
                                if time.time() < deadline:
                                    continue
                                if not context.pages or all(pg.is_closed() for pg in context.pages):
                                    break
                        finally:
                            try: context.close()
                            except Exception: pass
                            context = None
                        return False, "Browser closed. Log in to X next time you try Share Song — the window will stay open for you to log in, then try again."
                _mark_ready(X_PROFILE_DIR)
                # Wait for compose UI — "What's happening?" lives in a dialog + contenteditable Draft editor.
                try:
                    page.wait_for_selector(
                        "[role='dialog'],[data-testid='tweetTextarea'],[data-testid='tweetTextarea_0'],div[contenteditable='true'][role='textbox']",
                        timeout=20000,
                    )
                except Exception:
                    pass
                page.wait_for_timeout(800)

                def _find_x_tweet_textbox():
                    try:
                        loc = page.get_by_role(
                            "textbox",
                            name=re.compile(r"what.*happen|happenin|post text|post your reply", re.I),
                        )
                        if loc.count():
                            el = loc.first
                            try:
                                el.wait_for(state="visible", timeout=4000)
                            except Exception:
                                if not el.is_visible():
                                    el = None
                            if el is not None and el.is_visible():
                                el.scroll_into_view_if_needed(timeout=3000)
                                page.wait_for_timeout(200)
                                return el
                    except Exception:
                        pass
                    base_selectors = [
                        "[data-testid='tweetTextarea_0'][role='textbox']",
                        "div[data-testid='tweetTextarea_0'][role='textbox']",
                        "[data-testid='tweetTextarea_0'] div[contenteditable='true']",
                        "[data-testid='tweetTextarea_0'] [contenteditable='true']",
                        "[data-testid='tweetTextarea'][role='textbox']",
                        "div[data-testid='tweetTextarea'][role='textbox']",
                        "[data-testid='tweetTextarea']",
                        "[data-testid='tweetTextarea_0']",
                        "[role='textbox'][aria-label*=\"What's happening\"]",
                        "[role='textbox'][aria-label*='What’s happening']",
                        "[role='textbox'][aria-label*='Whats happening']",
                        "[contenteditable='true'][aria-label*=\"What's happening\"]",
                        "[contenteditable='true'][aria-label*='What’s happening']",
                        "[contenteditable='true'][aria-label*='Post text']",
                        "div[role='textbox'][contenteditable='true'][data-text='true']",
                        "[role='textbox'][data-contents='true']",
                        "div[role='textbox'][contenteditable='true']",
                        "div[contenteditable='true'][aria-label*='Post']",
                        "[placeholder*='What']",
                        ".public-DraftEditor-content[contenteditable='true']",
                    ]
                    for prefix in ("[role='dialog'] ", ""):
                        for sel in base_selectors:
                            full = f"{prefix}{sel}".strip()
                            try:
                                loc = page.locator(full).first
                                if not loc.count():
                                    continue
                                try:
                                    loc.wait_for(state="visible", timeout=3500)
                                except Exception:
                                    if not loc.is_visible():
                                        continue
                                loc.scroll_into_view_if_needed(timeout=3000)
                                page.wait_for_timeout(200)
                                return loc
                            except Exception:
                                continue
                    return None

                def _find_x_tweet_textbox_js():
                    try:
                        h = page.evaluate_handle(r"""
                            () => {
                                function visible(e) {
                                    if (!e || !e.getBoundingClientRect) return false;
                                    const r = e.getBoundingClientRect();
                                    if (r.width < 2 || r.height < 2) return false;
                                    const st = window.getComputedStyle(e);
                                    return st.visibility !== 'hidden' && st.display !== 'none' && st.opacity !== '0';
                                }
                                function labelMatches(el) {
                                    const a = ((el.getAttribute('aria-label') || '') + ' ' + (el.getAttribute('placeholder') || '')).toLowerCase();
                                    return a.includes('happening')
                                        || (a.includes('what') && a.includes('happen'))
                                        || a.includes('post text')
                                        || a.includes('post your');
                                }
                                const dialog = document.querySelector('[role="dialog"]');
                                const roots = dialog ? [dialog] : [document.body];
                                for (const root of roots) {
                                    const ordered = [
                                        "[data-testid='tweetTextarea_0'][role='textbox']",
                                        "[data-testid='tweetTextarea'][role='textbox']",
                                        "[data-testid='tweetTextarea_0'] div[contenteditable='true']",
                                        "[data-testid='tweetTextarea_0'] [contenteditable='true']",
                                        "div[role='textbox'][contenteditable='true']",
                                        ".public-DraftEditor-content[contenteditable='true']",
                                    ];
                                    for (const sel of ordered) {
                                        const el = root.querySelector(sel);
                                        if (el && visible(el)) return el;
                                    }
                                    for (const el of root.querySelectorAll('[contenteditable="true"]')) {
                                        if (labelMatches(el) && visible(el)) return el;
                                    }
                                    for (const el of root.querySelectorAll('div[contenteditable="true"]')) {
                                        const dl = el.closest('[data-testid="tweetTextarea_0"],[data-testid="tweetTextarea"]');
                                        if (dl && visible(el)) return el;
                                    }
                                }
                                return null;
                            }
                        """)
                        el = h.as_element()
                        if el:
                            return el
                    except Exception:
                        pass
                    return None

                tb = None
                tb_el = None
                for attempt in range(5):
                    tb = _find_x_tweet_textbox()
                    if tb:
                        break
                    tb_el = _find_x_tweet_textbox_js()
                    if tb_el:
                        break
                    page.wait_for_timeout(1800)
                if not tb and not tb_el:
                    try:
                        deadline = time.time() + 60
                        while time.time() < deadline and context.pages and not all(pg.is_closed() for pg in context.pages):
                            time.sleep(1)
                        if context.pages: context.close()
                    except Exception: pass
                    return False, "Compose dialog didn't open or text box not found. Log in to X if needed, then try Share Song again."
                if custom_body is not None:
                    msg = (custom_body or "").strip()[:280]
                else:
                    msg = _build_x_msg(song["title"], song["url"])
                if tb:
                    tb.click()
                    page.wait_for_timeout(400)
                    page.keyboard.press("Control+A")
                    page.keyboard.press("Backspace")
                    page.wait_for_timeout(200)
                    page.keyboard.type(msg, delay=24)
                else:
                    try:
                        tb_el.click(timeout=5000)
                        page.wait_for_timeout(400)
                        page.keyboard.press("Control+A")
                        page.keyboard.press("Backspace")
                        page.wait_for_timeout(200)
                    except Exception:
                        pass
                    tb_el.type(msg, delay=24)
                page.wait_for_timeout(2200)
                # Wait for Post button and click — retry so we don't get stuck at this stage
                try:
                    page.locator("[data-testid='tweetButton']").first.wait_for(state="visible", timeout=8000)
                except Exception: pass
                try:
                    page.locator("[role='dialog'] [aria-label='Post'], [aria-label='Post']").first.wait_for(state="visible", timeout=4000)
                except Exception: pass
                page.wait_for_timeout(600)
                # Click Post — multi-strategy with scroll and retries
                def _try_x_post_click() -> bool:
                    # Strategy 1: data-testid="tweetButton" (X compose dialog)
                    try:
                        btn = page.locator("[data-testid='tweetButton']").first
                        if btn.count() and btn.is_visible():
                            btn.scroll_into_view_if_needed(timeout=2000)
                            page.wait_for_timeout(300)
                            btn.click(force=True)
                            return True
                    except Exception: pass
                    # Strategy 2: aria-label="Post"
                    try:
                        for sel in ["[role='dialog'] [aria-label='Post']", "[data-testid='tweetButtonInline'] [aria-label='Post']", "[aria-label='Post']"]:
                            btn = page.locator(sel).first
                            if btn.count() and btn.is_visible():
                                btn.scroll_into_view_if_needed(timeout=2000)
                                page.wait_for_timeout(200)
                                btn.click(force=True)
                                return True
                    except Exception: pass
                    # Strategy 3: get_by_role(button, name=Post)
                    try:
                        btn = page.get_by_role("button", name=re.compile(r"^Post$", re.I))
                        if btn.count() and btn.first.is_visible():
                            btn.first.scroll_into_view_if_needed(timeout=2000)
                            page.wait_for_timeout(200)
                            btn.first.click(force=True)
                            return True
                    except Exception: pass
                    # Strategy 4: button with text "Post"
                    try:
                        btn = page.locator("button:has-text('Post')").first
                        if btn.count() and btn.is_visible():
                            btn.scroll_into_view_if_needed(timeout=2000)
                            btn.click(force=True)
                            return True
                    except Exception: pass
                    # Strategy 5: blue button in compose (Post)
                    try:
                        clicked = page.evaluate("""() => {
                            const sel = document.querySelector('[data-testid="tweetButton"]');
                            if (sel && sel.offsetParent) { sel.click(); return true; }
                            const post = document.querySelector('[aria-label="Post"]');
                            if (post && post.offsetParent) { post.click(); return true; }
                            const dialogs = document.querySelectorAll('[role="dialog"]');
                            for (const d of dialogs) {
                                const btns = d.querySelectorAll('button, [role="button"]');
                                for (const b of btns) {
                                    if (!b.offsetParent) continue;
                                    if ((b.textContent || '').trim() === 'Post') { b.click(); return true; }
                                    const style = window.getComputedStyle(b);
                                    const bg = (style.backgroundColor || '').trim();
                                    let bl = 0;
                                    if (bg.startsWith('rgb')) {
                                        const num = bg.match(/[\\d.]+/g);
                                        if (num && num.length >= 3) bl = +num[2];
                                    } else if (bg[0] === '#') {
                                        const h = bg.slice(1).length === 3 ? bg.slice(1).replace(/(.)/g,'$1$1') : bg.slice(1);
                                        bl = (parseInt(h.slice(0,6), 16) || 0) & 255;
                                    }
                                    if (bl > 120) { b.click(); return true; }
                                }
                            }
                            return false;
                        }""")
                        if clicked: return True
                    except Exception: pass
                    return False
                posted = False
                for attempt in range(8):
                    page.wait_for_timeout(800 if attempt > 2 else 500)
                    if _try_x_post_click():
                        posted = True
                        break
                if not posted:
                    return False, (
                        "I typed the post but couldn't click **Post**. Click the blue **Post** button in the X window yourself to share, then try Share Song again next time."
                    )
                page.wait_for_timeout(3000)
                if custom_body is None and song is not None:
                    _record_shared_song(song["url"])
                    return True, f'Shared to X: "{song["title"]}"'
                return True, "Posted to X."
        except Exception as e:
            return False, f"X error: {e}"
        finally:
            if context:
                try: context.close()
                except Exception: pass
    except ImportError:
        return False, "Playwright not installed."
    finally:
        try: _x_lock.release()
        except Exception: pass

def _build_fb_msg(title: str, url: str) -> str:
    title = title.strip()[:80]
    return random.choice([
        f'Hey friends, check out this track: "{title}" 🎶\n{url}',
        f'Sharing one of my songs: "{title}" — hope you enjoy it 🎧\n{url}',
    ])


def _fb_compose_status_on_page(page, message: str) -> tuple[bool, str]:
    """Open Facebook create-post dialog on current page, type ``message``, click through Next/Post. Caller handles login/PIN and browser lifecycle."""
    msg = (message or "").strip()
    if not msg:
        return False, "Empty Facebook post body."
    if len(msg) > 900:
        msg = msg[:897] + "…"
    page.wait_for_timeout(2000)
    composer_opened = False
    for sel in [
        "div[aria-label*='mind']",
        "div[aria-label*='Create a post']",
        "div[role='button']:has-text('What')",
        "span:has-text(\"What's on your mind\")",
        "[data-pagelet*='FeedComposer'] div[role='button']",
    ]:
        try:
            btn = page.locator(sel).first
            if btn.count() and btn.is_visible():
                btn.click()
                page.wait_for_timeout(2000)
                composer_opened = True
                break
        except Exception:
            continue
    if not composer_opened:
        return False, "Could not open Facebook composer."
    tb = None
    for sel in ["div[role='dialog'] div[role='textbox'][contenteditable='true']"]:
        loc = page.locator(sel).first
        if loc.count() and loc.is_visible():
            tb = loc
            break
    if not tb:
        return False, "Facebook post text box not found."
    tb.click(force=True)
    page.keyboard.press("Control+A")
    page.keyboard.press("Backspace")
    page.keyboard.type(msg, delay=24)
    page.wait_for_timeout(1500)
    next_clicked = False
    for nsel in ["button:has-text('Next')", "div[role='button']:has-text('Next')", "[aria-label*='Next']"]:
        try:
            nbtn = page.locator(nsel).first
            if nbtn.count() and nbtn.is_visible():
                nbtn.click(force=True)
                page.wait_for_timeout(2000)
                next_clicked = True
                break
        except Exception:
            continue
    if not next_clicked:
        return False, "Could not click Next on Create post."
    post_settings_visible = False
    for dsel in ["[aria-label*='Post settings']", "[role='dialog']:has-text('Post settings')", "div[role='dialog']:has-text('Post audience')"]:
        try:
            page.wait_for_selector(dsel, state="visible", timeout=8000)
            post_settings_visible = True
            break
        except Exception:
            continue
    if not post_settings_visible:
        page.wait_for_timeout(3000)
    page.wait_for_timeout(1000)
    for dsel in ["[aria-label*='Post settings']", "[role='dialog']:has-text('Post settings')"]:
        try:
            d = page.locator(dsel).last
            if d.count() and d.is_visible():
                pub = d.locator("text=Public").first
                if pub.count() and pub.is_visible():
                    pub.click(force=True)
                    page.wait_for_timeout(600)
                break
        except Exception:
            continue
    page.wait_for_timeout(800)
    try:
        page.locator("[aria-label*='Post settings'] [aria-label='Post'], [role='dialog'] [aria-label='Post']").first.wait_for(
            state="visible", timeout=6000
        )
    except Exception:
        pass
    try:
        page.locator("[aria-label*='Post settings'] button:has-text('Post')").or_(
            page.locator("[role='dialog'] button:has-text('Post')")
        ).first.wait_for(state="visible", timeout=3000)
    except Exception:
        pass
    page.wait_for_timeout(500)

    def _try_post_click() -> bool:
        dialog = None
        for dsel in [
            "[aria-label*='Post settings']",
            "[role='dialog']:has-text('Post settings')",
            "div[role='dialog']:has-text('Post audience')",
            "div[role='dialog']",
        ]:
            try:
                loc = page.locator(dsel).last
                if loc.count() and loc.is_visible():
                    dialog = loc
                    break
            except Exception:
                continue
        if not dialog or not dialog.count() or not dialog.is_visible():
            return False
        try:
            post_btn = dialog.locator('[aria-label="Post"]')
            if post_btn.count() and post_btn.first.is_visible():
                post_btn.first.click(force=True)
                return True
        except Exception:
            pass
        try:
            post_btn = page.locator(
                '[role="dialog"] [aria-label="Post"], [aria-label*="Post settings"] [aria-label="Post"]'
            )
            if post_btn.count() and post_btn.first.is_visible():
                post_btn.first.click(force=True)
                return True
        except Exception:
            pass
        try:
            clicked = page.evaluate(
                """() => {
                    function parseRgb(str) {
                        const num = str.match(/[\\d.]+/g);
                        if (num && num.length >= 3) return [+num[0], +num[1], +num[2]];
                        if (str[0] === '#') {
                            let h = str.slice(1);
                            if (h.length === 3) h = h[0]+h[0]+h[1]+h[1]+h[2]+h[2];
                            const v = parseInt(h.slice(0,6), 16) || 0;
                            return [(v>>16)&255, (v>>8)&255, v&255];
                        }
                        return null;
                    }
                    function isBlueBg(el) {
                        const style = window.getComputedStyle(el);
                        let bg = (style.backgroundColor || '').trim();
                        if (!bg && el.children.length) {
                            const child = el.querySelector('[style*="background"], span, div');
                            if (child) bg = (window.getComputedStyle(child).backgroundColor || '').trim();
                        }
                        const rgb = parseRgb(bg);
                        if (!rgb) return /#1877f2|#0866ff|#3578e5|#0d6efd/i.test(bg);
                        const [r,g,bl] = rgb;
                        return bl > 120 && bl >= r && bl >= g;
                    }
                    const dialogs = document.querySelectorAll('[role="dialog"], [aria-label*="Post settings"]');
                    for (const d of dialogs) {
                        const btns = d.querySelectorAll('button, [role="button"]');
                        for (const b of btns) {
                            if (!b.offsetParent || (b.textContent || '').trim() === 'Save') continue;
                            if (isBlueBg(b)) { b.click(); return true; }
                        }
                    }
                    return false;
                }"""
            )
            if clicked:
                return True
        except Exception:
            pass
        try:
            post_btn = dialog.get_by_role("button", name=re.compile(r"^Post$", re.I))
            if post_btn.count():
                post_btn.first.wait_for(state="visible", timeout=2000)
                post_btn.first.click(force=True)
                return True
        except Exception:
            pass
        try:
            buttons = dialog.locator("button")
            n = buttons.count()
            if n >= 2:
                right_btn = buttons.nth(n - 1)
                right_btn.wait_for(state="visible", timeout=2000)
                t = (right_btn.text_content() or "").strip()
                if t == "Post":
                    right_btn.click(force=True)
                    return True
                right_btn.click(force=True)
                return True
            if n == 1:
                b = buttons.first
                if (b.text_content() or "").strip() == "Post":
                    b.wait_for(state="visible", timeout=2000)
                    b.click(force=True)
                    return True
        except Exception:
            pass
        try:
            post_btn = dialog.locator("button:has-text('Post')")
            if post_btn.count():
                post_btn.first.wait_for(state="visible", timeout=2000)
                post_btn.first.click(force=True)
                return True
        except Exception:
            pass
        try:
            for i in range(dialog.locator("button").count()):
                btn = dialog.locator("button").nth(i)
                if (btn.text_content() or "").strip() == "Post" and btn.is_visible():
                    btn.click(force=True)
                    return True
        except Exception:
            pass
        try:
            box = dialog.bounding_box()
            if box:
                x = box["x"] + box["width"] - 55
                y = box["y"] + box["height"] - 32
                page.mouse.click(x, y)
                return True
        except Exception:
            pass
        try:
            clicked = page.evaluate(
                """() => {
                    const dialogs = document.querySelectorAll('[role="dialog"], [aria-label*="Post settings"]');
                    for (const d of dialogs) {
                        const btns = d.querySelectorAll('button');
                        for (const b of btns) {
                            if ((b.textContent || '').trim() === 'Post') {
                                b.click(); return true;
                            }
                        }
                    }
                    return false;
                }"""
            )
            if clicked:
                return True
        except Exception:
            pass
        return False

    posted = False
    for attempt in range(6):
        page.wait_for_timeout(600 if attempt else 400)
        if _try_post_click():
            posted = True
            break
    if not posted:
        return False, "Typed post but couldn't click the Post button (tried multiple strategies)."
    page.wait_for_timeout(2000)
    return True, ""


def _run_fb_share() -> tuple[bool, str]:
    ok, song = _get_next_channel_song_for_share()
    if not ok: return False, str(song)
    if not _fb_lock.acquire(blocking=False): return False, "Facebook share already running."
    pw = None
    context = None
    handed_off = False
    try:
        from playwright.sync_api import sync_playwright
        os.makedirs(FB_PROFILE_DIR, exist_ok=True)
        pw = sync_playwright().start()
        context = _launch_social_browser_with_retry(FB_PROFILE_DIR, pw, attempts=4)
        page = _acquire_live_page(context)
        page.goto(FACEBOOK_PROFILE, wait_until="domcontentloaded", timeout=90000)
        page.wait_for_timeout(1000)
        if "login" in page.url.lower():
            _clear_ready(FB_PROFILE_DIR)
            handed_off = True
            _keep_browser_until_closed(pw, context)
            return False, "Facebook needs login. I left the browser open — log in, then **close the browser** and try again."
        if _fb_needs_pin(page):
            handed_off = True
            _keep_browser_until_closed(pw, context)
            return False, (
                "Facebook is asking for a **PIN or verification code**. "
                "I left the browser open — enter the code, then **close the browser** and try again."
            )
        _mark_ready(FB_PROFILE_DIR)
        ok_post, err_post = _fb_compose_status_on_page(page, _build_fb_msg(song["title"], song["url"]))
        if not ok_post:
            if err_post == "Could not open Facebook composer.":
                handed_off = True
                _keep_browser_until_closed(pw, context)
            return False, err_post
        _record_shared_song(song["url"])
        return True, f'Shared to Facebook: "{song["title"]}"'
    except Exception as e:
        return False, f"Facebook error: {e}"
    finally:
        if not handed_off:
            if context:
                try: context.close()
                except Exception: pass
            if pw:
                try: pw.stop()
                except Exception: pass
        try: _fb_lock.release()
        except Exception: pass


def _format_yt_upload_x_message(title: str, url: str, hint: str = "") -> str:
    """Tweet body for a new YouTube upload (cross-post). Honors LUNA_PUBLISH_ANNOUNCE_YT_X_TEMPLATE."""
    t = (title or "").strip()
    u = (url or "").strip()
    h = (hint or "").strip()
    tpl = LUNA_PUBLISH_ANNOUNCE_YT_X_TEMPLATE
    if tpl:
        return (
            tpl.replace("{title}", t).replace("{url}", u).replace("{hint}", h)
        )[:280]
    return (f'New video: "{t[:120]}"\n{u}')[:280]


def _format_yt_upload_fb_message(title: str, url: str, hint: str = "") -> str:
    """Facebook body for a new YouTube upload (cross-post). Honors LUNA_PUBLISH_ANNOUNCE_YT_FB_TEMPLATE."""
    t = (title or "").strip()
    u = (url or "").strip()
    h = (hint or "").strip()
    tpl = LUNA_PUBLISH_ANNOUNCE_YT_FB_TEMPLATE
    if tpl:
        return (
            tpl.replace("{title}", t).replace("{url}", u).replace("{hint}", h)
        )[:900]
    return f'I just uploaded a new YouTube video — "{t[:160]}"\n\nWatch it here: {u}'[:900]


def _format_twitch_live_fb_message(ev: dict) -> str:
    """Build Facebook body for a Twitch go-live event (``ev`` from publish announce)."""
    disp = str(ev.get("display_name") or ev.get("login") or "Stream").strip()
    ttl = str(ev.get("title") or "Live now").strip()
    url = str(ev.get("url") or "").strip()
    login = str(ev.get("login") or "").strip()
    tpl = LUNA_PUBLISH_ANNOUNCE_FB_MESSAGE_TEMPLATE
    if tpl:
        return (
            tpl.replace("{display_name}", disp)
            .replace("{title}", ttl)
            .replace("{url}", url)
            .replace("{login}", login)[:900]
        )
    return f"I'm live on Twitch — come hang out!\n\n{disp}\n{ttl}\n{url}"


def _fb_post_plain_status_to_timeline(body: str) -> tuple[bool, str]:
    """Post arbitrary timeline text (same Playwright flow as !share_facebook, without song rotation)."""
    if not _fb_lock.acquire(blocking=False):
        return False, "Facebook action already running."
    pw = None
    context = None
    handed_off = False
    try:
        from playwright.sync_api import sync_playwright

        os.makedirs(FB_PROFILE_DIR, exist_ok=True)
        pw = sync_playwright().start()
        context = _launch_social_browser_with_retry(FB_PROFILE_DIR, pw, attempts=4)
        page = _acquire_live_page(context)
        page.goto(FACEBOOK_PROFILE, wait_until="domcontentloaded", timeout=90000)
        page.wait_for_timeout(1000)
        if "login" in page.url.lower():
            _clear_ready(FB_PROFILE_DIR)
            handed_off = True
            _keep_browser_until_closed(pw, context)
            return False, "Facebook needs login. I left the browser open — log in, then **close the browser** and try again."
        if _fb_needs_pin(page):
            handed_off = True
            _keep_browser_until_closed(pw, context)
            return False, (
                "Facebook is asking for a **PIN or verification code**. "
                "I left the browser open — enter the code, then **close the browser** and try again."
            )
        _mark_ready(FB_PROFILE_DIR)
        ok_post, err_post = _fb_compose_status_on_page(page, body)
        if not ok_post:
            if err_post == "Could not open Facebook composer.":
                handed_off = True
                _keep_browser_until_closed(pw, context)
            return False, err_post
        return True, "Posted to Facebook."
    except ImportError:
        return False, "Playwright not installed."
    except Exception as e:
        return False, f"Facebook error: {e}"
    finally:
        if not handed_off:
            if context:
                try:
                    context.close()
                except Exception:
                    pass
            if pw:
                try:
                    pw.stop()
                except Exception:
                    pass
        try:
            _fb_lock.release()
        except Exception:
            pass


def _yt_comment(video_url: str) -> tuple[bool, str]:
    video_url = _yt_url_from_freeform_arg(video_url) or (video_url or "").strip()
    vid = _yt_extract_id(video_url)
    if not vid:
        return False, "Invalid YouTube URL."
    if not _yt_lock.acquire(blocking=False): return False, "YouTube comment already running."
    try:
        from playwright.sync_api import sync_playwright
        # Get title/description for fallback; try to transcribe video for real-context comment
        title = vid
        desc = ""
        transcript = ""
        audio_path = None
        temp_dir = None
        try:
            req = urllib.request.Request(f"https://www.youtube.com/watch?v={vid}",
                headers={"User-Agent": "Mozilla/5.0"})
            with urllib.request.urlopen(req, timeout=30) as r:
                page_html = r.read().decode("utf-8", errors="replace")
            m = re.search(r"<title>(.*?)</title>", page_html, re.I|re.S)
            if m: title = html.unescape(m.group(1).replace("- YouTube","").strip())
            m2 = re.search(r'<meta name=["\']description["\'] content=["\']([^"\']+)', page_html, re.I)
            if m2: desc = html.unescape(m2.group(1).strip())[:500]
        except Exception: pass
        try:
            audio_path, title_from_dl = _yt_download_audio_for_transcript(video_url, max_seconds=600)
            if title_from_dl: title = title_from_dl
            if audio_path:
                temp_dir = os.path.dirname(audio_path)
                raw = _whisper_transcribe(audio_path)
                if raw and len(raw.strip()) > 20:
                    transcript = raw.strip()[:3000]
        except Exception: pass
        finally:
            if temp_dir and os.path.isdir(temp_dir):
                try:
                    for f in os.listdir(temp_dir):
                        try: os.unlink(os.path.join(temp_dir, f))
                        except Exception: pass
                    os.rmdir(temp_dir)
                except Exception: pass
        # Generate real-context comment: prefer transcript so Luna comments like a user who watched
        if transcript:
            comment = ollama_chat(
                f"Write one short YouTube comment as a real viewer who watched the video. Use ONLY what was said in the transcript. Be genuine and specific (reference something from the video). 1-2 sentences, max 200 characters. Return only the comment, no quotes.\n\nVideo title: {title}\n\nTranscript (excerpt):\n{transcript}",
                model=OLLAMA_CHAT,
                compact=True,
            )
        else:
            comment = ollama_chat(
                f"Write one YouTube comment (1-2 sentences, warm, human, max 200 chars). Video: {title}\nContext: {desc}\nReturn only the comment.",
                model=OLLAMA_CHAT,
                compact=True,
            )
        comment = re.sub(r"\s+", " ", comment).strip().strip('"\'')[:220]
        if not comment: comment = "Great content, keep it up!"
        os.makedirs(YT_PROFILE_DIR, exist_ok=True)
        context = None
        try:
            with sync_playwright() as p:
                for attempt in range(2):
                    try:
                        context = _launch_social_browser(YT_PROFILE_DIR, p)
                        break
                    except Exception as launch_err:
                        if attempt == 0 and ("Target page, context or browser has been closed" in str(launch_err) or "closed" in str(launch_err).lower()):
                            time.sleep(2)
                            continue
                        return False, f"YouTube browser failed to start. Close any Chrome window using the YouTube profile (or close all Chrome), then try again. Error: {launch_err}"
                if context is None:
                    return False, "YouTube browser failed to start. Close any Chrome window and try again."
                page = context.pages[0] if context.pages else context.new_page()
                page.goto(f"https://www.youtube.com/watch?v={vid}", wait_until="domcontentloaded", timeout=90000)
                page.wait_for_timeout(2500)
                # Logged-in: visible avatar/subscriptions, or visible comment placeholder (do not assume logged in on slow loads).
                logged_in = False
                try:
                    if "accounts.google.com" in page.url:
                        logged_in = False
                    else:
                        for sel in (
                            "#avatar-btn",
                            "a[href*='/feed/subscriptions']",
                            "ytd-masthead #avatar-button",
                        ):
                            loc = page.locator(sel).first
                            if loc.count():
                                try:
                                    if loc.is_visible():
                                        logged_in = True
                                        break
                                except Exception:
                                    pass
                        if not logged_in:
                            for sel in (
                                "ytd-comment-simplebox-renderer #simplebox-placeholder",
                                "ytd-comment-simplebox-renderer #placeholder-area",
                                "ytd-comment-simplebox-renderer #focused-placeholder-area",
                            ):
                                loc = page.locator(sel).first
                                if loc.count():
                                    try:
                                        if loc.is_visible():
                                            logged_in = True
                                            break
                                    except Exception:
                                        pass
                        if not logged_in:
                            signin = page.locator(
                                "ytd-masthead a[href*='accounts.google'], "
                                "ytd-button-renderer a[href*='accounts.google.com/ServiceLogin']"
                            ).first
                            try:
                                if signin.count() and signin.is_visible():
                                    logged_in = False
                            except Exception:
                                pass
                except Exception:
                    pass
                if not logged_in:
                    try: context.close()
                    except Exception: pass
                    context = None
                    _clear_ready(YT_PROFILE_DIR)
                    _bootstrap_window(YT_PROFILE_DIR, _youtube_login_bootstrap_url(), "_yt_boot")
                    return False, _youtube_needs_login_message()
                _mark_ready(YT_PROFILE_DIR)
                # Scroll to comments — YouTube A/B tests placeholder ids.
                entry_sel = None
                for _ in range(22):
                    for sel in (
                        "ytd-comment-simplebox-renderer #simplebox-placeholder",
                        "ytd-comment-simplebox-renderer #placeholder-area",
                        "ytd-comment-simplebox-renderer #focused-placeholder-area",
                    ):
                        try:
                            box = page.locator(sel).first
                            if box.count() and box.is_visible():
                                entry_sel = sel
                                break
                        except Exception:
                            pass
                    if entry_sel:
                        break
                    page.mouse.wheel(0, 1200)
                    page.wait_for_timeout(350)
                if not entry_sel:
                    return False, "Comment box not found (scrolled comments — check login or try again)."
                page.locator(entry_sel).first.click(force=True)
                page.wait_for_timeout(500)
                editor = page.locator("#contenteditable-root[contenteditable='true']").first
                try:
                    editor.wait_for(state="visible", timeout=8000)
                except Exception:
                    pass
                editor.click(force=True)
                page.keyboard.press("Control+A"); page.keyboard.press("Backspace")
                page.keyboard.type(comment, delay=14)
                # Short pause after typing (more human-like, less bot detection)
                page.wait_for_timeout(int(random.uniform(1200, 2200)))
                # Find the Comment submit button — use the same one users click (aria-label="Comment")
                sub = None
                for sel in (
                    'button[aria-label="Comment"]',
                    'yt-button-shape button[aria-label="Comment"]',
                    '[aria-label="Comment"]',
                    "ytd-commentbox #submit-button button",
                    "ytd-button-renderer#submit-button button",
                    "#submit-button button",
                ):
                    try:
                        loc = page.locator(sel).first
                        if loc.count():
                            for _ in range(20):
                                page.wait_for_timeout(250)
                                try:
                                    if loc.is_visible() and not loc.is_disabled():
                                        sub = loc
                                        break
                                except Exception:
                                    pass
                            if sub is not None:
                                break
                    except Exception:
                        pass
                if sub is None:
                    try:
                        editor = page.locator("#contenteditable-root[contenteditable='true']").first
                        if editor.count():
                            editor.click(force=True)
                            page.keyboard.press("Control+Enter")
                            page.wait_for_timeout(2500)
                            return True, f'Comment posted on: "{title}"'
                    except Exception:
                        pass
                    return False, "Could not find the Comment button to submit."
                try:
                    sub.scroll_into_view_if_needed(timeout=3000)
                except Exception:
                    pass
                page.wait_for_timeout(300)
                sub.click(timeout=5000)
                page.wait_for_timeout(2000)
                return True, f'Comment posted on: "{title}"'
        except Exception as e:
            err = str(e)
            if "Target page, context or browser has been closed" in err or "browser has been closed" in err.lower():
                return False, "YouTube browser closed or profile in use. Close all Chrome windows (or the one using the YouTube profile), then try !yt_comment again."
            return False, f"YouTube error: {e}"
        finally:
            if context:
                try: context.close()
                except Exception: pass
    except ImportError:
        return False, "Playwright not installed."
    finally:
        try: _yt_lock.release()
        except Exception: pass


def _yt_react(video_url: str) -> tuple[bool, str]:
    """Generate Luna reaction to a YouTube video (reads title/desc, prefers transcript)."""
    video_url = _yt_url_from_freeform_arg(video_url) or (video_url or "").strip()
    vid = _yt_extract_id(video_url)
    if not vid:
        return False, "Invalid YouTube URL."
    title = vid
    desc = ""
    transcript = ""
    temp_dir = None
    try:
        req = urllib.request.Request(
            f"https://www.youtube.com/watch?v={vid}",
            headers={"User-Agent": "Mozilla/5.0"},
        )
        with urllib.request.urlopen(req, timeout=30) as r:
            page_html = r.read().decode("utf-8", errors="replace")
        m = re.search(r"<title>(.*?)</title>", page_html, re.I | re.S)
        if m:
            title = html.unescape(m.group(1).replace("- YouTube", "").strip())[:180]
        m2 = re.search(r'<meta name=["\']description["\'] content=["\']([^"\']+)', page_html, re.I)
        if m2:
            desc = html.unescape(m2.group(1).strip())[:700]
    except Exception:
        pass
    try:
        audio_path, title_from_dl = _yt_download_audio_for_transcript(video_url, max_seconds=600)
        if title_from_dl:
            title = title_from_dl[:180]
        if audio_path:
            temp_dir = os.path.dirname(audio_path)
            raw = _whisper_transcribe(audio_path)
            if raw and len(raw.strip()) > 20:
                transcript = raw.strip()[:3500]
    except Exception:
        pass
    finally:
        if temp_dir and os.path.isdir(temp_dir):
            try:
                for f in os.listdir(temp_dir):
                    try:
                        os.unlink(os.path.join(temp_dir, f))
                    except Exception:
                        pass
                os.rmdir(temp_dir)
            except Exception:
                pass

    if transcript:
        prompt = (
            "React to this YouTube video like Luna. 2-4 short sentences, natural and specific. "
            "Mention at least one concrete point from the transcript. No hashtags. No markdown.\n\n"
            f"Title: {title}\n\nTranscript excerpt:\n{transcript}"
        )
    else:
        prompt = (
            "React to this YouTube video like Luna. 2-4 short sentences, natural and specific. "
            "If details are limited, be honest and react to what is known. No hashtags. No markdown.\n\n"
            f"Title: {title}\nDescription: {desc}"
        )
    out = (ollama_chat(prompt, model=OLLAMA_CHAT, compact=True) or "").strip()
    out = " ".join(out.split())[:520]
    if not out:
        return False, "Could not generate a reaction."
    return True, f"🎬 **Reaction — {title[:90]}**\n{out}"


def _yt_watch_pick_caption_track(captions: dict) -> str | None:
    if not isinstance(captions, dict) or not captions:
        return None
    preferred = ("en", "en-US", "en-GB", "en-orig", "en-CA", "en-AU")
    for key in preferred:
        if key in captions and captions.get(key):
            return key
    for key in sorted(captions.keys()):
        if str(key).lower().startswith("en") and captions.get(key):
            return key
    return next(iter(captions.keys()), None)


def _ts_to_sec(ts: str) -> float:
    raw = (ts or "").strip().replace(",", ".")
    parts = raw.split(":")
    if len(parts) == 2:
        h = 0
        m = int(parts[0] or "0")
        s = float(parts[1] or "0")
    elif len(parts) == 3:
        h = int(parts[0] or "0")
        m = int(parts[1] or "0")
        s = float(parts[2] or "0")
    else:
        return 0.0
    return max(0.0, h * 3600 + m * 60 + s)


def _parse_vtt_cues(vtt_text: str) -> list[dict]:
    cues: list[dict] = []
    block: list[str] = []
    for line in (vtt_text or "").splitlines():
        if line.strip():
            block.append(line.rstrip())
            continue
        if block:
            ts_line = next((x for x in block if "-->" in x), "")
            if ts_line:
                start = ts_line.split("-->", 1)[0].strip()
                sec = _ts_to_sec(start)
                text_lines = [x for x in block if "-->" not in x and not re.fullmatch(r"\d+", x.strip())]
                txt = " ".join(text_lines)
                txt = re.sub(r"<[^>]+>", " ", txt)
                txt = html.unescape(txt)
                txt = re.sub(r"\s+", " ", txt).strip()
                if txt and not txt.startswith("WEBVTT"):
                    cues.append({"sec": sec, "text": txt[:260]})
            block = []
    if block:
        ts_line = next((x for x in block if "-->" in x), "")
        if ts_line:
            start = ts_line.split("-->", 1)[0].strip()
            sec = _ts_to_sec(start)
            text_lines = [x for x in block if "-->" not in x and not re.fullmatch(r"\d+", x.strip())]
            txt = " ".join(text_lines)
            txt = re.sub(r"<[^>]+>", " ", txt)
            txt = html.unescape(txt)
            txt = re.sub(r"\s+", " ", txt).strip()
            if txt and not txt.startswith("WEBVTT"):
                cues.append({"sec": sec, "text": txt[:260]})
    deduped: list[dict] = []
    prev = ""
    for cue in cues:
        txt = cue.get("text", "")
        if not txt or txt == prev:
            continue
        deduped.append(cue)
        prev = txt
    return deduped


def _yt_watch_fetch_cues(video_url: str) -> tuple[str, list[dict]]:
    title = _yt_extract_id(video_url) or "YouTube video"
    cues: list[dict] = []
    try:
        import yt_dlp  # type: ignore

        ydl_opts = {
            "quiet": True,
            "no_warnings": True,
            "skip_download": True,
            "noplaylist": True,
            "extract_flat": False,
        }
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(video_url, download=False) or {}
        title = (info.get("title") or title)[:180]
        captions = info.get("subtitles") or info.get("automatic_captions") or {}
        lang = _yt_watch_pick_caption_track(captions)
        tracks = captions.get(lang) if lang else None
        if isinstance(tracks, list):
            fmt = None
            for t in tracks:
                ext = str((t or {}).get("ext") or "").lower()
                if ext in ("vtt", "webvtt"):
                    fmt = t
                    break
            if fmt is None and tracks:
                fmt = tracks[0]
            cap_url = (fmt or {}).get("url")
            if cap_url:
                req = urllib.request.Request(str(cap_url), headers={"User-Agent": "Mozilla/5.0"})
                with urllib.request.urlopen(req, timeout=25) as r:
                    raw = r.read().decode("utf-8", errors="replace")
                cues = _parse_vtt_cues(raw)
    except Exception:
        pass
    return title, cues


def _yt_watch_build_moments(video_url: str, beats: int | None = None) -> tuple[bool, dict | str]:
    requested_beats = max(4, min(14, int(beats or YT_WATCH_REACT_BEATS)))
    title, cues = _yt_watch_fetch_cues(video_url)
    if not cues:
        return False, "I couldn't read timed captions for that video yet. Try a different video (or use !yt_react for one-shot)."
    duration_sec = float(cues[-1].get("sec") or 0.0) if cues else 0.0
    # Build candidate moments, then Luna chooses which ones to keep by returning SKIP.
    # More candidates than final comments = she chooses when to speak.
    target_comments = requested_beats
    if beats is None and duration_sec > 0:
        auto_cap = int(round(duration_sec / YT_WATCH_REACT_TARGET_SEC_PER_BEAT))
        auto_cap = max(3, min(12, auto_cap))
        target_comments = min(target_comments, auto_cap)
    candidate_target = max(target_comments * 3, target_comments + 4)
    candidate_target = min(36, max(6, candidate_target))
    if len(cues) < candidate_target:
        candidate_target = len(cues)
    if candidate_target <= 0:
        return False, "No caption cues were available for watch-react timing."

    # Build a video-level summary from sampled captions so Luna knows the overall topic.
    n = len(cues)
    # Take the first ~20% (sets up context) plus evenly-spaced samples across the full video.
    summary_idx: list[int] = list(range(0, min(n, max(6, n // 5))))
    step = max(1, n // 20)
    for j in range(0, n, step):
        summary_idx.append(j)
    summary_idx = sorted(set(summary_idx))[:40]
    video_summary = re.sub(
        r"\s+", " ",
        " ".join((cues[j].get("text") or "").strip() for j in summary_idx if (cues[j].get("text") or "").strip()),
    ).strip()[:1000]

    moments: list[dict] = []
    last_sec = -99999.0
    for i in range(candidate_target):
        idx = int(round((i + 1) * (n - 1) / (candidate_target + 1)))
        lo = max(0, idx - 1)
        hi = min(n, idx + 2)
        sec = float(cues[idx].get("sec") or 0.0)
        if sec - last_sec < max(12.0, YT_WATCH_REACT_MIN_GAP_SEC * 0.5):
            continue
        # Exact caption at beat (3 cues wide)
        context_text = re.sub(
            r"\s+", " ",
            " ".join((cues[j].get("text") or "") for j in range(lo, hi)).strip(),
        )[:360]
        # Preceding 90 seconds of captions — narrative run-up to this moment
        preceding_parts = [
            (cues[j].get("text") or "").strip()
            for j in range(n)
            if 0.0 < sec - float(cues[j].get("sec") or 0.0) <= 92.0
        ]
        preceding_text = re.sub(r"\s+", " ", " ".join(preceding_parts)).strip()[:700]
        moments.append({
            "sec": sec,
            "context": context_text or (cues[idx].get("text") or "")[:220],
            "preceding": preceding_text,
            "video_summary": video_summary,
        })
        last_sec = sec
    if not moments:
        return False, "Could not build candidate moments from captions."
    return True, {"title": title, "moments": moments}


def _yt_watch_parse_reaction_controls(raw: str) -> tuple[str, bool, bool]:
    """Strip PAUSE:/HOLD: markers for VTuber-style freeze-frame beats (Podcast studio reads flags)."""
    s = (raw or "").strip()
    if re.fullmatch(r"(?is)\s*(?:SKIP|PASS|NONE|NO_COMMENT|NO COMMENT)\s*", s):
        return ("", False, False)
    pause_before = bool(re.match(r"(?is)^PAUSE:\s*", s))
    if pause_before:
        s = re.sub(r"(?is)^PAUSE:\s*", "", s, count=1).strip()
    hold_after = bool(re.search(r"(?is)\s+HOLD:\s*$", s))
    if hold_after:
        s = re.sub(r"(?is)\s+HOLD:\s*$", "", s).strip()
    s = " ".join(s.split())
    return (s[:180], pause_before, hold_after)


def _yt_watch_line_looks_like_caption_read(line: str, context: str) -> bool:
    """Heuristic: detect lines that mostly restate caption text instead of adding commentary."""
    l = re.sub(r"\s+", " ", (line or "").strip().lower())
    c = re.sub(r"\s+", " ", (context or "").strip().lower())
    if not l or not c:
        return False
    lw = [w for w in re.findall(r"[a-z0-9']+", l) if len(w) >= 4]
    cw = [w for w in re.findall(r"[a-z0-9']+", c) if len(w) >= 4]
    if not lw or not cw:
        return False
    ls, cs = set(lw), set(cw)
    overlap = len(ls & cs) / max(1, len(ls))
    if overlap >= 0.72:
        return True
    # Also catch obvious near-copy with short rewrites.
    if len(l) >= 40 and (l in c or c[: min(len(c), 120)] in l):
        return True
    return False


def _yt_watch_make_reaction_line(
    title: str,
    context: str,
    previous_line: str = "",
    *,
    beat_index: int = 1,
    beat_total: int = 1,
    sec: float = 0.0,
    preceding: str = "",
    video_summary: str = "",
) -> str:
    stream_persona = _stream_mode_persona_body()[:1800]
    mm = int(max(0.0, sec) // 60)
    ss = int(max(0.0, sec) % 60)
    is_final = beat_index >= max(1, beat_total)

    video_ctx = ""
    if video_summary.strip():
        video_ctx = f"## Full video context (sampled captions — read this to understand what the video is actually about)\n{video_summary.strip()}\n\n"

    preceding_ctx = ""
    if preceding.strip() and preceding.strip() != context.strip():
        preceding_ctx = f"## What was said in the 90 seconds before this moment\n{preceding.strip()}\n\n"

    prompt = (
        f"{stream_persona}\n\n"
        f"{video_ctx}"
        f"{preceding_ctx}"
        "You are watching this YouTube video live on stream and have just paused it at the moment below.\n"
        "Write a spoken commentary of 2-4 sentences that:\n"
        "  • Reacts to something SPECIFIC from the preceding section (name it — a person, event, claim, moment, technique, quote, etc.).\n"
        "  • Shows genuine emotion: surprise, excitement, skepticism, humor, awe — whatever fits the scene.\n"
        "  • Builds on what just happened; do NOT give a generic observation that could apply to any video.\n"
        "  • Sounds like natural speech — no markdown, no hashtags, no stage directions, no emoji spam.\n"
        "  • Total length: 80-480 characters (PAUSE:/HOLD: markers excluded).\n"
        "NEVER say things like 'Interesting point' / 'Good stuff' / 'I see' / 'That makes sense' — be specific.\n"
        "Do NOT quote the captions word-for-word. React to them.\n"
        "If the preceding section has genuinely nothing worth commenting on, output exactly: SKIP\n"
        + ("This is NOT the final beat — do not close out or wrap up the video.\n" if not is_final else "This is the last beat — a brief genuine wrap-up reaction is fine.\n")
        + "\n"
        "Controls (optional, prepend/append to your line):\n"
        "  PAUSE: — keep video frozen while you speak (video already paused at this beat).\n"
        "  HOLD: at end — leave paused after you finish (use if you want more time on this frame).\n\n"
        f"Video title: {title[:180]}\n"
        f"Beat {beat_index}/{beat_total} at {mm:02d}:{ss:02d}\n"
        f"## Caption at this exact moment\n{context[:500]}\n"
        f"Previous comment (do not repeat or echo this): {previous_line[:220]}\n\n"
        "Luna's spoken commentary:"
    )
    out = (ollama_chat(prompt, model=OLLAMA_CHAT) or "").strip()
    out = " ".join(out.split())
    if not out:
        return ""

    # Genericness check — if the output is a hollow observation, retry once with a stricter prompt.
    _generic_phrases = (
        "interesting point", "good stuff", "that's interesting", "that makes sense",
        "i see", "wow okay", "oh wow", "makes you think", "pretty cool", "not bad",
        "i guess", "okay then", "sure okay", "alright then", "fair enough",
    )
    is_generic = any(p in out.lower() for p in _generic_phrases) or len(out) < 50
    if is_generic or _yt_watch_line_looks_like_caption_read(out, context):
        retry_prompt = (
            f"{stream_persona}\n\n"
            f"{video_ctx}"
            f"{preceding_ctx}"
            "The draft commentary below is too vague or reads like a transcript. Rewrite it.\n"
            "Pick ONE specific thing from the preceding captions (a name, a fact, a moment, a claim) and react to it genuinely.\n"
            "2-4 sentences, 80-480 chars, natural spoken voice, no markdown.\n"
            "If there's truly nothing worth saying, output exactly: SKIP\n\n"
            f"Video title: {title[:180]}\n"
            f"Caption moment: {context[:500]}\n"
            f"Bad draft: {out[:300]}\n"
            f"Previous comment: {previous_line[:220]}\n\n"
            "Rewritten Luna commentary:"
        )
        out2 = (ollama_chat(retry_prompt, model=OLLAMA_CHAT) or "").strip()
        out2 = " ".join(out2.split())
        if out2:
            out = out2

    # Collapse multi-line to a single spoken block, cap length.
    out = " ".join(line.strip() for line in re.split(r"[\r\n]+", out) if line.strip())
    return out[:480]


def _yt_watch_react_ui_enqueue(line: str) -> None:
    """Push a watch-react line to hub + /vrm/ poll (append_exchange is done in _yt_watch_send_scope_line)."""
    global _yt_watch_react_ui_event_id
    text = (line or "").strip()
    if not text:
        return
    with _yt_watch_react_ui_lock:
        _yt_watch_react_ui_event_id += 1
        eid = _yt_watch_react_ui_event_id
        _yt_watch_react_ui_events.append({"id": eid, "reply": text, "ts": time.time()})
        while len(_yt_watch_react_ui_events) > 200:
            _yt_watch_react_ui_events.pop(0)


def _yt_watch_schedule_save(d: dict) -> None:
    with _yt_watch_schedule_lock:
        _save_json(_YT_WATCH_SCHEDULE_PATH, d)


def _yt_watch_schedule_clear() -> None:
    """Clear timed schedule (call on stop / new session)."""
    global _yt_watch_playback_ended_for_session
    with _yt_watch_schedule_lock:
        _save_json(
            _YT_WATCH_SCHEDULE_PATH,
            {"ready": False, "items": [], "session_id": 0.0, "title": "", "url": "", "scope": "web", "error": ""},
        )
    with _yt_watch_emitted_lock:
        _yt_watch_emitted_ids.clear()
    with _yt_watch_live_prev_lock:
        _yt_watch_live_prev_by_session.clear()
    _yt_watch_playback_ended_for_session = 0.0


def _yt_watch_cowatch_active() -> bool:
    """True while a watch-react schedule is ready (Podcast co-watch may sync video + Twitch)."""
    with _yt_watch_schedule_lock:
        d = _load_json(_YT_WATCH_SCHEDULE_PATH, {})
    if not isinstance(d, dict) or not d.get("ready"):
        return False
    items = d.get("items")
    return isinstance(items, list) and len(items) > 0


def _cowatch_should_pause_for_twitch_reply(reply: str, viewer_line: str) -> bool:
    """Heuristic: pause co-watch video so Luna can answer chat without talking over the video."""
    if _env("LUNA_COWATCH_TWITCH_PAUSE", "1").strip().lower() in ("0", "false", "no", "off"):
        return False
    r = (reply or "").strip()
    v = (viewer_line or "").strip()
    if len(r) < 14:
        return False
    if "?" in v or "?" in r:
        return True
    if len(r) >= 96:
        return True
    low = r.lower()
    if low in ("ok", "sure", "thanks", "thank you", "lol", "lmao", "haha", "nice", "got it", "yep", "yeah", "cool"):
        return False
    if len(low) <= 28 and not any(ch.isalpha() for ch in low):
        return False
    return len(r) >= 42


def _yt_watch_text_for_speaker(raw: str) -> str:
    """Strip UI emoji/timestamp prefixes so server TTS matches what the VRM viewer speaks."""
    t = (raw or "").strip()
    t = re.sub(r"^[\s\U0001F300-\U0001FAFF]+", "", t)
    t = re.sub(r"^🎥\s*\[\d{1,2}:\d{2}\]\s*", "", t)
    return _clean_for_tts(t)


async def _yt_watch_send_scope_line(
    scope: str,
    text: str,
    *,
    reaction_line: bool = False,
) -> None:
    """Push watch-react text to UI/VRM. Timed reaction lines skip proactive + Discord DM spam (studio executes the beat)."""
    clean = (text or "").strip()
    if not clean:
        return
    try:
        append_exchange(scope or (LINKED_SCOPE or "web"), "[YT watch react]", clean)
    except Exception:
        pass
    if not reaction_line:
        try:
            _proactive_set(clean)
        except Exception:
            pass
    try:
        _yt_watch_react_ui_enqueue(clean)
    except Exception:
        pass
    # Speak the line via local TTS unless the VRM viewer is handling audio.
    # reaction_line=True means this is a timed studio beat — always speak it locally.
    # reaction_line=False means a setup/status message — only speak if not already handled elsewhere.
    try:
        if not _vrm_presence_recent():
            spoken = _yt_watch_text_for_speaker(clean)
            if spoken.strip():
                _play_reply_tts(spoken, chat_source="twitch", para_mode=False)
    except Exception:
        pass
    return


def _yt_watch_stop(scope: str) -> tuple[bool, str]:
    key = (scope or LINKED_SCOPE or "web").strip() or "web"
    with _yt_watch_lock:
        ev = _yt_watch_stops.pop(key, None)
        if not ev:
            return False, "No active YouTube watch-react session."
    ev.set()
    _studio_watch_set("", "", "yt_watch_stop")
    try:
        _yt_watch_schedule_clear()
    except Exception:
        pass
    return True, "Stopping watch-react."


def _yt_watch_react_start(video_url: str, scope: str, *, beats: int | None = None) -> tuple[bool, str]:
    url = (video_url or "").strip()
    if not _yt_extract_id(url):
        return False, "Usage: !yt_watch_react <youtube-url>"
    ok, payload = _yt_watch_build_moments(url, beats=beats)
    if not ok:
        return False, str(payload)
    data = payload if isinstance(payload, dict) else {}
    title = (data.get("title") or "YouTube video").strip()
    moments = data.get("moments") or []
    if not moments:
        return False, "Couldn't build watch-react moments."
    _studio_watch_set(url, title, "yt_watch_react")
    key = (scope or LINKED_SCOPE or "web").strip() or "web"
    stop_ev = threading.Event()
    with _yt_watch_lock:
        old = _yt_watch_stops.get(key)
        if old:
            old.set()
        _yt_watch_stops[key] = stop_ev
    try:
        _yt_watch_schedule_clear()
    except Exception:
        pass
    session_id = time.time()
    _yt_watch_schedule_save(
        {
            "ready": False,
            "session_id": session_id,
            "title": title,
            "url": url,
            "scope": key,
            "items": [],
            "error": "",
        }
    )
    try:
        _proactive_set(f"🎬 Building live watch-react beat map from captions… ({len(moments)} beats)")
    except Exception:
        pass

    def _precompute_worker() -> None:
        """Build beat timestamps + context only; commentary is generated live at each beat."""
        items: list[dict] = []
        loop = getattr(bot, "loop", None)
        try:
            out_id = 0
            for i, moment in enumerate(moments):
                if stop_ev.is_set():
                    break
                sec = float(moment.get("sec") or 0.0)
                out_id += 1
                items.append(
                    {
                        "id": out_id,
                        "sec": sec,
                        "context": str(moment.get("context") or ""),
                        "preceding": str(moment.get("preceding") or ""),
                        "video_summary": str(moment.get("video_summary") or ""),
                        "pause_before": bool(_yt_watch_force_pause_beats()),
                        "hold_after": False,
                    }
                )
            if stop_ev.is_set():
                _yt_watch_schedule_save(
                    {
                        "ready": False,
                        "session_id": session_id,
                        "title": title,
                        "url": url,
                        "scope": key,
                        "items": items,
                        "error": "stopped",
                    }
                )
                return
            with _yt_watch_emitted_lock:
                _yt_watch_emitted_ids.clear()
            with _yt_watch_live_prev_lock:
                _yt_watch_live_prev_by_session.pop(f"{key}|{session_id:.6f}", None)
            if not items:
                _yt_watch_schedule_save(
                    {
                        "ready": False,
                        "session_id": session_id,
                        "title": title,
                        "url": url,
                        "scope": key,
                        "items": [],
                        "error": "No notable moments to comment yet (all beats skipped). Try fewer beats or another video.",
                    }
                )
                if loop and loop.is_running():
                    asyncio.run_coroutine_threadsafe(
                        _yt_watch_send_scope_line(
                            key,
                            "🎬 Watch-react could not build any usable beat timestamps from captions. "
                            "Try again with fewer beats or a different video.",
                        ),
                        loop,
                    )
                return
            _yt_watch_schedule_save(
                {
                    "ready": True,
                    "session_id": session_id,
                    "title": title,
                    "url": url,
                    "scope": key,
                    "items": items,
                    "error": "",
                }
            )
            if loop and loop.is_running():
                n = len(items)
                asyncio.run_coroutine_threadsafe(
                    _yt_watch_send_scope_line(
                        key,
                        f"🎬 **Watch-react is live** — {n} timed beats on the **video clock** (not Discord time).\n"
                        f"**You must open Podcast studio** on the same PC as Luna: `/podcast/?cowatch=1` — "
                        "press **Play** on the embed: I **pause → react live to what just happened → resume** as playback progresses. "
                        "Discord cannot play that video for me; this message is only the start signal. "
                        f"Pause style: **{'forced every beat (VTuber)' if _yt_watch_force_pause_beats() else 'from captions / PAUSE:'}**. "
                        "**!yt_watch_stop** when done.",
                    ),
                    loop,
                )
        except Exception as ex:
            with _yt_watch_lock:
                if _yt_watch_stops.get(key) is stop_ev:
                    _yt_watch_stops.pop(key, None)
            _yt_watch_schedule_save(
                {
                    "ready": False,
                    "session_id": session_id,
                    "title": title,
                    "url": url,
                    "scope": key,
                    "items": items,
                    "error": str(ex)[:200],
                }
            )
            if loop and loop.is_running():
                asyncio.run_coroutine_threadsafe(
                    _yt_watch_send_scope_line(key, f"⚠ Watch-react setup failed: {str(ex)[:160]}"),
                    loop,
                )
        finally:
            with _yt_watch_lock:
                cur = _yt_watch_stops.get(key)
                if cur is stop_ev and stop_ev.is_set():
                    _yt_watch_stops.pop(key, None)

    th = threading.Thread(target=_precompute_worker, daemon=True, name=f"yt-watch-precompute-{int(time.time())}")
    th.start()
    studio_hint = ""
    if LUNA_PUBLIC_BASE_URL:
        studio_hint = f"\n\nPodcast studio (video + VRM): {LUNA_PUBLIC_BASE_URL}/podcast/?cowatch=1"
    return (
        True,
        f"Started live watch-react for **{title[:90]}** ({len(moments)} beats). "
        f"Open **Podcast studio** `/podcast/?cowatch=1` for synced video + avatar react. "
        f"**!yt_watch_stop** ends the session.{studio_hint}",
    )


def _x_react(post_url: str) -> tuple[bool, str]:
    """Generate Luna reaction to an X/Twitter post URL (no posting)."""
    u = (post_url or "").strip()
    if not re.search(r"https?://(?:www\.)?(?:x\.com|twitter\.com)/[^/\s]+/status/\d+", u, re.I):
        return False, "Provide a valid X/Twitter post URL (`.../status/<id>`)."
    # Use vxtwitter mirror for easier text extraction.
    mirror = re.sub(r"^https?://(?:www\.)?(?:x\.com|twitter\.com)", "https://vxtwitter.com", u, flags=re.I)
    ok, extracted = _scrape_website(
        mirror,
        "Extract the main post text and key context (media or quoted tweet if visible). Keep it concise.",
        "",
    )
    if not ok:
        return False, f"Could not read post: {extracted}"
    prompt = (
        "React as Luna to this X post content. 1-3 short sentences, natural and specific, no hashtags, no markdown.\n\n"
        f"Post URL: {u}\n\nExtracted content:\n{extracted[:2000]}"
    )
    out = (ollama_chat(prompt, model=OLLAMA_MODEL) or "").strip()
    out = " ".join(out.split())[:420]
    if not out:
        return False, "Could not generate a reaction."
    return True, f"🐦 **Reaction — X post**\n{out}"

def _yt_like_one(video_url: str) -> tuple[bool, str]:
    """Like exactly one video via Playwright + YT_PROFILE_DIR (same account as !yt_comment)."""
    video_url = _yt_url_from_freeform_arg(video_url) or (video_url or "").strip()
    vid = _yt_extract_id(video_url)
    if not vid:
        return False, "Invalid YouTube URL."
    if not _yt_lock.acquire(blocking=False): return False, "YouTube action already running."
    watch = f"https://www.youtube.com/watch?v={vid}"
    try:
        from playwright.sync_api import sync_playwright
        os.makedirs(YT_PROFILE_DIR, exist_ok=True)
        context = None
        try:
            with sync_playwright() as p:
                for attempt in range(2):
                    try:
                        context = _launch_social_browser(YT_PROFILE_DIR, p)
                        break
                    except Exception as launch_err:
                        if attempt == 0 and ("Target page, context or browser has been closed" in str(launch_err) or "closed" in str(launch_err).lower()):
                            time.sleep(2)
                            continue
                        return False, f"YouTube browser failed to start. Close any Chrome window using the YouTube profile (or close all Chrome), then try again. Error: {launch_err}"
                if context is None:
                    return False, "YouTube browser failed to start. Close any Chrome window and try again."
                page = context.pages[0] if context.pages else context.new_page()
                page.goto(watch, wait_until="domcontentloaded", timeout=90000)
                page.wait_for_timeout(2000)
                logged_in = False
                try:
                    if "accounts.google.com" in page.url:
                        logged_in = False
                    else:
                        for sel in ("#avatar-btn", "a[href*='/feed/subscriptions']", "ytd-masthead #avatar-button", "ytd-comment-simplebox-renderer"):
                            if page.locator(sel).first.count():
                                try:
                                    if page.locator(sel).first.is_visible():
                                        logged_in = True
                                        break
                                except Exception:
                                    pass
                        if not logged_in:
                            if page.locator("ytd-masthead a[href*='accounts.google']").first.count() and page.locator("ytd-masthead a[href*='accounts.google']").first.is_visible():
                                logged_in = False
                            else:
                                logged_in = True
                except Exception:
                    pass
                if not logged_in:
                    try: context.close()
                    except Exception: pass
                    context = None
                    _clear_ready(YT_PROFILE_DIR)
                    _bootstrap_window(YT_PROFILE_DIR, _youtube_login_bootstrap_url(), "_yt_boot")
                    return False, _youtube_needs_login_message()
                _mark_ready(YT_PROFILE_DIR)
                result = page.evaluate("""() => {
                    const tryClick = (btn) => {
                        if (!btn) return null;
                        const label = ((btn.getAttribute('aria-label') || '') + ' ' + (btn.getAttribute('title') || '')).toLowerCase();
                        if (label.startsWith('unlike') || label.includes('remove your like')) return 'already';
                        const pressed = btn.getAttribute('aria-pressed');
                        if (pressed === 'true') return 'already';
                        btn.click();
                        return 'clicked';
                    };
                    const likeVm = document.querySelector('like-button-view-model');
                    if (likeVm) {
                        const btn = likeVm.querySelector('button');
                        const r = tryClick(btn);
                        if (r) return r;
                    }
                    const seg = document.querySelector('segmented-like-dislike-button-view-model');
                    if (seg) {
                        const btn = seg.querySelector('button');
                        const r = tryClick(btn);
                        if (r) return r;
                    }
                    const nodes = document.querySelectorAll(
                        '#top-level-buttons-computed button, ytd-menu-renderer ytd-toggle-button-renderer button, ytd-watch-metadata button'
                    );
                    for (const b of nodes) {
                        const label = ((b.getAttribute('aria-label') || '') + ' ' + (b.getAttribute('title') || '')).toLowerCase();
                        if (!label.includes('like')) continue;
                        if (label.includes('dislike') && !label.includes('like')) continue;
                        if (/\\b(clip|share|save|download|report)\\b/.test(label)) continue;
                        const r = tryClick(b);
                        if (r) return r;
                    }
                    return 'none';
                }""")
                if result == "already":
                    page.wait_for_timeout(400)
                    return True, "Already liked (or like state unchanged)."
                if result == "clicked":
                    # YouTube needs a moment to apply the like (count + icon); keep page open so it visibly updates
                    page.wait_for_timeout(int(random.uniform(3500, 5200)))
                    return True, f"Liked video (watch?v={vid})."
                return False, "Could not find the Like button (YouTube layout may have changed)."
        except Exception as e:
            err = str(e)
            if "Target page, context or browser has been closed" in err or "browser has been closed" in err.lower():
                return False, "YouTube browser closed or profile in use. Close all Chrome windows (or the one using the YouTube profile), then try again."
            return False, f"YouTube error: {e}"
        finally:
            if context:
                try: context.close()
                except Exception: pass
    except ImportError:
        return False, "Playwright not installed."
    finally:
        try: _yt_lock.release()
        except Exception: pass

def _ig_is_blocked(page) -> bool:
    """Detect if Instagram is showing a challenge, block, or suspicious-login page."""
    try:
        url_low = (page.url or "").lower()
        content_low = (page.content() or "")[:3000].lower()
        block_signals = [
            "challenge" in url_low, "/accounts/suspended" in url_low,
            "suspicious" in content_low, "we detected an unusual login" in content_low,
            "confirm your identity" in content_low, "automated behavior" in content_low,
            "try again later" in content_low and "something went wrong" in content_low,
        ]
        return any(block_signals)
    except Exception:
        return False

def _run_ig_dm(target: str, message: str = "") -> tuple[bool, str]:
    target = re.sub(r"^@","", target.strip())
    if not re.fullmatch(r"[a-zA-Z0-9._]{2,30}", target): return False, "Invalid Instagram username."
    if OPEN_IG_IN_BROWSER_ONLY:
        url = f"{IG_BASE}/{target}/"
        try:
            webbrowser.open(url)
        except Exception:
            return False, "Could not open browser."
        _record_recent_social("instagram", target, url)
        return True, f"Opened @{target}'s profile. Click Message, type in the popup, then Enter to send — or use the notification in the UI to open again."
    if not _ig_lock.acquire(blocking=False): return False, "Instagram DM already running."
    try:
        from playwright.sync_api import sync_playwright
        raw_ig = (message or "").strip()
        if raw_ig:
            dm_text = _rephrase_dm(raw_ig, "Instagram", target) or raw_ig
        else:
            dm_text = _default_wa_msg()
        os.makedirs(IG_PROFILE_DIR, exist_ok=True)
        context = None
        try:
            with sync_playwright() as p:
                for attempt in range(2):
                    try:
                        context = _launch_social_browser(IG_PROFILE_DIR, p)
                        break
                    except Exception as launch_err:
                        if attempt == 0 and ("Target page, context or browser has been closed" in str(launch_err) or "closed" in str(launch_err).lower()):
                            time.sleep(2)
                            continue
                        return False, f"Instagram browser failed to start. Close any Chrome window using the Instagram profile (e.g. a Luna Instagram tab), then try again. Error: {launch_err}"
                if context is None:
                    return False, "Instagram browser failed to start. Close any Chrome window and try again."
                page = context.pages[0] if context.pages else context.new_page()

                # --- Primary approach: use /direct/new/ to search and DM ---
                page.goto(f"{IG_BASE}/direct/new/", wait_until="domcontentloaded", timeout=90000)
                page.wait_for_timeout(2500)

                saw_cap, cap_note = _playwright_granite_captcha_observer(page)
                if saw_cap:
                    try:
                        context.close()
                    except Exception:
                        pass
                    return False, (
                        "Captcha or challenge detected (Granite vision). "
                        + (cap_note or "Complete the check, then try the DM again.")
                    )

                if "login" in page.url.lower() or "accounts/login" in page.url:
                    _bootstrap_window(IG_PROFILE_DIR, f"{IG_BASE}/", "_ig_boot")
                    return False, "Instagram needs login. Browser opened — log in and close, then try again."

                if _ig_is_blocked(page):
                    # Automation detected — fall back to opening real browser
                    try: context.close()
                    except Exception: pass
                    try:
                        webbrowser.open(f"{IG_BASE}/{target}/")
                    except Exception:
                        pass
                    _record_recent_social("instagram", target, f"{IG_BASE}/{target}/")
                    return True, (
                        f"Instagram detected automation, so I opened @{target}'s profile in your real browser instead. "
                        "Click **Message** on their profile to DM them. "
                        "To avoid this, log into Instagram in the Luna browser window once."
                    )
                _mark_ready(IG_PROFILE_DIR)

                # Search for the target user in the "new message" dialog
                search_box = None
                for sel in [
                    'input[placeholder*="Search"]', 'input[name="queryBox"]',
                    'input[aria-label*="Search"]', 'input[type="text"]',
                ]:
                    try:
                        loc = page.locator(sel).first
                        if loc.count() and loc.is_visible():
                            search_box = loc
                            break
                    except Exception:
                        continue

                if search_box:
                    search_box.click()
                    page.wait_for_timeout(300)
                    search_box.fill(target)
                    page.wait_for_timeout(2000)

                    # Click the matching user result
                    user_clicked = False
                    for sel in [
                        f'span:has-text("{target}")', f'div:has-text("{target}")',
                        '[role="listbox"] [role="option"]', '[role="dialog"] button',
                    ]:
                        try:
                            results = page.locator(sel)
                            for i in range(min(results.count(), 5)):
                                r = results.nth(i)
                                txt = (r.text_content() or "").lower()
                                if target.lower() in txt and r.is_visible():
                                    r.click(timeout=3000)
                                    user_clicked = True
                                    break
                            if user_clicked:
                                break
                        except Exception:
                            continue

                    if user_clicked:
                        page.wait_for_timeout(1500)
                        # Click the "Chat" / "Next" button to open the conversation
                        for sel in [
                            'button:has-text("Chat")', 'button:has-text("Next")',
                            'div[role="button"]:has-text("Chat")', 'div[role="button"]:has-text("Next")',
                        ]:
                            try:
                                loc = page.locator(sel).first
                                if loc.count() and loc.is_visible():
                                    loc.click(timeout=3000)
                                    break
                            except Exception:
                                continue
                        page.wait_for_timeout(2000)

                        # Find the message editor and type
                        editor = _try_find_ig_message_editor(page)
                        if editor:
                            editor.click(force=True)
                            page.wait_for_timeout(300)
                            page.keyboard.type(dm_text, delay=20)
                            page.wait_for_timeout(500)
                            page.keyboard.press("Enter")
                            page.wait_for_timeout(1000)
                            _record_recent_social("instagram", target, f"{IG_BASE}/direct/inbox/")
                            return True, f"Instagram DM sent to @{target}. Check the notification box to open Instagram and see replies."

                # --- Fallback: profile page approach ---
                profile_url = f"{IG_BASE}/{target}/"
                page.goto(profile_url, wait_until="domcontentloaded", timeout=60000)
                page.wait_for_timeout(2000)

                if _ig_is_blocked(page):
                    try: context.close()
                    except Exception: pass
                    try: webbrowser.open(profile_url)
                    except Exception: pass
                    _record_recent_social("instagram", target, profile_url)
                    return True, (
                        f"Instagram detected automation. Opened @{target}'s profile in your real browser. "
                        "Click **Message** to DM them."
                    )

                clicked = _try_click_ig_message_button(page)
                if not clicked:
                    # Last resort: open in real browser
                    try: webbrowser.open(profile_url)
                    except Exception: pass
                    _record_recent_social("instagram", target, profile_url)
                    return True, (
                        f"Could not find the Message button. Opened @{target}'s profile in your real browser — "
                        "click **Message** yourself to DM them."
                    )

                editor = None
                page.wait_for_timeout(1800)
                for attempt in range(4):
                    editor = _try_find_ig_message_editor(page)
                    if editor:
                        break
                    page.wait_for_timeout(2500 if attempt == 0 else 2000)
                    if attempt == 2:
                        _try_click_ig_message_button(page)
                        page.wait_for_timeout(800)
                if not editor:
                    try: webbrowser.open(profile_url)
                    except Exception: pass
                    _record_recent_social("instagram", target, profile_url)
                    return True, (
                        f"Couldn't find the message input. Opened @{target}'s profile in your real browser — "
                        "click **Message** yourself to DM them."
                    )
                editor.click(force=True)
                page.wait_for_timeout(300)
                page.keyboard.type(dm_text, delay=20)
                page.wait_for_timeout(500)
                page.keyboard.press("Enter")
                page.wait_for_timeout(1000)
                _record_recent_social("instagram", target, f"{IG_BASE}/direct/inbox/")
                return True, f"Instagram DM sent to @{target}. Check the notification box to open Instagram and see replies."
        except Exception as e:
            # On any failure, fall back to real browser
            try: webbrowser.open(f"{IG_BASE}/{target}/")
            except Exception: pass
            _record_recent_social("instagram", target, f"{IG_BASE}/{target}/")
            return True, f"Instagram automation failed ({e}). Opened @{target}'s profile in your browser — click **Message** to DM them."
        finally:
            if context:
                try: context.close()
                except Exception: pass
    except ImportError:
        return False, "Playwright not installed."
    finally:
        try: _ig_lock.release()
        except Exception: pass

def _try_click_ig_message_button(page):
    """Try several strategies to find and click the Message button on the profile."""
    strategies = [
        lambda: page.get_by_role("button", name="Message"),
        lambda: page.get_by_role("button", name=re.compile(r"Message", re.I)),
        lambda: page.locator('button:has-text("Message")').first,
        lambda: page.locator('[role="button"]:has-text("Message")').first,
        lambda: page.locator('a[href*="/direct/"]:has-text("Message")').first,
        lambda: page.locator('div[role="button"]:has-text("Message")').first,
        lambda: page.get_by_text("Message", exact=True),
        lambda: page.get_by_text(re.compile(r"^Message$", re.I)),
    ]
    for get_loc in strategies:
        try:
            loc = get_loc()
            if loc.count() and loc.is_visible():
                loc.scroll_into_view_if_needed(timeout=3000)
                page.wait_for_timeout(300)
                loc.click(timeout=5000)
                return True
        except Exception:
            continue
    try:
        clicked = page.evaluate("""() => {
            const walk = (el) => {
                if (!el || el.children.length > 5) return false;
                if (el.innerText && el.innerText.trim() === 'Message' && el.offsetParent !== null) {
                    const r = el.getBoundingClientRect();
                    if (r.width > 20 && r.height > 10) { el.click(); return true; }
                }
                for (const c of el.children || []) { if (walk(c)) return true; }
                return false;
            };
            return walk(document.body);
        }""")
        if clicked:
            return True
    except Exception:
        pass
    return False

def _try_find_ig_message_editor(page):
    """Try several strategies to find the DM message input."""
    selectors = [
        'div[contenteditable="true"][aria-label="Message"]',
        'div[contenteditable="true"][aria-placeholder="Message..."]',
        '[placeholder="Message..."]', '[aria-placeholder="Message..."]',
        'div[role="textbox"][aria-placeholder="Message..."]',
        'div[contenteditable="true"][data-lexical-editor="true"]',
        'textarea[placeholder="Message..."]',
        'div[contenteditable="true"][role="textbox"]',
    ]
    for sel in selectors:
        try:
            loc = page.locator(sel).first
            if loc.count() and loc.is_visible():
                loc.scroll_into_view_if_needed(timeout=2000)
                page.wait_for_timeout(200)
                return loc
        except Exception:
            continue
    for placeholder in ["Message...", "Message"]:
        try:
            editor = page.get_by_placeholder(placeholder)
            if editor.count() and editor.is_visible():
                return editor.first
        except Exception:
            pass
    for sel in ['div[contenteditable="true"]', '[role="textbox"]']:
        try:
            loc = page.locator(sel).last
            if loc.count() and loc.is_visible():
                loc.scroll_into_view_if_needed(timeout=1500)
                return loc
        except Exception:
            continue
    return None

def _default_wa_msg() -> str:
    h = time.localtime().tm_hour
    if 5 <= h < 12: return "Have a wonderful morning from Luna"
    if 12 <= h < 17: return "Hope you're having a great day — from Luna"
    if 17 <= h < 21: return "Have a lovely evening — from Luna"
    return "Goodnight from Luna"

def _wa_page_logged_in(page) -> bool:
    """Return True if WhatsApp Web page shows a logged-in state (chat list/search), not QR login."""
    try:
        # Not logged in: QR / "Scan" / "Link with phone" visible
        content = (page.content() or "").lower()
        if "scan this qr" in content or "link with phone" in content or "link your phone" in content:
            return False
        # Logged-in: contacts/chat list first, then search or message input
        for sel in ('[role="listitem"]', '[data-tab="3"]', '[aria-label="Search input textbox"]', '[data-placeholder*="Type a message"]', 'footer [contenteditable="true"]'):
            loc = page.locator(sel).first
            if loc.count() > 0 and loc.is_visible():
                return True
        if "download whatsapp" in content and page.locator("[role='listitem']").first.count() > 0:
            return True
        return False
    except Exception:
        return False


def _wa_phone_rest(s: str) -> str | None:
    """If s looks like a phone number, return digits after the first 3 (country code, e.g. +357). Otherwise None."""
    if not s:
        return None
    digits = re.sub(r"\D", "", s)
    if len(digits) < 7:
        return None
    # Ignore first 3 country code digits, focus on the rest
    return digits[3:] if len(digits) > 3 else digits


def _parse_wa_contact_and_msg(rest: str) -> tuple[str, str | None]:
    """
    Parse "contact [message]" for WhatsApp. Contact can be a full phone number with spaces
    (e.g. +357 99 447267 or 99 447267) or a username. Returns (contact, description).
    """
    rest = (rest or "").strip()
    if not rest:
        return "", None
    # If it starts with + or a digit, treat as phone: take full number (digits, spaces, dashes, parens), rest is message
    m = re.match(r"^(\+?\d[\d\s\-\(\)]+?)(?=\s+[^\d\s\-\(\)]|\s*$)(\s.*)?$", rest)
    if m:
        contact = m.group(1).strip()
        desc = (m.group(2) or "").strip() or None
        if len(re.sub(r"\D", "", contact)) >= 6:
            return contact, desc
    # Username or single word: first token is contact, rest is message
    parts = rest.split(None, 1)
    contact = parts[0].strip()
    desc = (parts[1].strip() if len(parts) > 1 else "") or None
    return contact, desc


def _wa_contact_match_variants(contact: str) -> list[str]:
    """Return variants for matching: full contact, username with/without ~, and for numbers the rest after country code."""
    c = (contact or "").strip()
    if not c:
        return []
    out = [c]
    # WhatsApp usernames: ~Christossolonos
    if c.startswith("~"):
        out.append(c[1:].strip())
    else:
        out.append("~" + c)
    # For phone numbers: add rest without first 3 (country code), e.g. +357 -> focus on rest
    rest = _wa_phone_rest(c)
    if rest:
        out.append(rest)
    return out


def _wa_normalize_row_text(s: str) -> str:
    """Strip WhatsApp '(You)' suffix so we match the contact, not the label (e.g. +357 99 447267 (You) -> +357 99 447267)."""
    if not s:
        return s
    return re.sub(r"\s*\(You\)\s*$", "", s, flags=re.I).strip()


def _wa_get_matching_contact_labels(page, contact: str) -> list[str]:
    """
    After search is filled, return the list of visible chat row labels (top line) that match contact.
    Used to detect multiple matches and ask the user which one (need_feedback).
    """
    contact = (contact or "").strip()
    if not contact:
        return []
    variants = _wa_contact_match_variants(contact)
    contact_phone_rest = _wa_phone_rest(contact)
    labels = []
    seen = set()
    try:
        listitems = page.locator('[role="listitem"]')
        n = listitems.count()
        for i in range(n):
            el = listitems.nth(i)
            try:
                if not el.is_visible():
                    continue
            except Exception:
                continue
            full_text = (el.inner_text() or "").strip()
            top_line_raw = full_text.split("\n")[0].strip() if full_text else ""
            top_line = _wa_normalize_row_text(top_line_raw)
            if not top_line or top_line in seen:
                continue
            matched = False
            if contact_phone_rest:
                top_rest = _wa_phone_rest(top_line)
                if top_rest and top_rest == contact_phone_rest:
                    matched = True
            if not matched and any(v in top_line or top_line in v for v in variants):
                matched = True
            if not matched:
                full_norm = _wa_normalize_row_text(full_text)
                if any(v in full_norm for v in variants):
                    matched = True
            if matched:
                seen.add(top_line)
                labels.append(top_line_raw[:60] or top_line[:60])
        return labels
    except Exception:
        return []


def _wa_click_contact_row(page, contact: str) -> bool:
    """
    Find and click the chat row in the Chats list whose top line matches the contact.
    Scrolls the row into view then clicks so the conversation opens.
    """
    contact = (contact or "").strip()
    if not contact:
        return False
    variants = _wa_contact_match_variants(contact)
    contact_phone_rest = _wa_phone_rest(contact)

    def do_click(el):
        try:
            el.scroll_into_view_if_needed(timeout=3000)
            page.wait_for_timeout(300)
            el.click(timeout=3000)
            return True
        except Exception:
            return False

    try:
        # Look at the Chats list: all listitem rows in the left pane
        listitems = page.locator('[role="listitem"]')
        n = listitems.count()
        for i in range(n):
            el = listitems.nth(i)
            try:
                if not el.is_visible():
                    continue
            except Exception:
                continue
            full_text = (el.inner_text() or "").strip()
            top_line_raw = full_text.split("\n")[0].strip() if full_text else ""
            top_line = _wa_normalize_row_text(top_line_raw)
            full_normalized = _wa_normalize_row_text(full_text)
            if not top_line:
                continue
            matched = False
            if contact_phone_rest:
                top_rest = _wa_phone_rest(top_line)
                if top_rest and top_rest == contact_phone_rest:
                    matched = True
            if not matched and any(v in top_line or top_line in v for v in variants):
                matched = True
            if not matched and any(v in full_normalized for v in variants):
                matched = True
            if matched:
                if do_click(el):
                    return True
        # Fallback 1: filter listitem by variant text
        for v in variants:
            try:
                loc = page.locator('[role="listitem"]').filter(has_text=v).first
                if loc.count() > 0 and loc.is_visible():
                    if do_click(loc):
                        return True
            except Exception:
                pass

        # Fallback 2: find by visible text (e.g. number or username in the row), then click that or its row
        for v in variants:
            try:
                txt_loc = page.get_by_text(v, exact=False).first
                if txt_loc.count() > 0 and txt_loc.is_visible():
                    txt_loc.scroll_into_view_if_needed(timeout=2000)
                    page.wait_for_timeout(200)
                    # Click the listitem row that contains this text (nearest ancestor with role=listitem)
                    row = txt_loc.locator("xpath=(./ancestor::*[@role='listitem'])[1]").first
                    if row.count() > 0:
                        if do_click(row):
                            return True
                    txt_loc.click(timeout=2000)
                    return True
            except Exception:
                pass

        # Fallback 3: digits-only (phone rest) in listitem — e.g. "99447267" appears in "+357 99 447267"
        if contact_phone_rest:
            try:
                loc = page.locator('[role="listitem"]').filter(has_text=contact_phone_rest).first
                if loc.count() > 0 and loc.is_visible():
                    if do_click(loc):
                        return True
            except Exception:
                pass

        # Fallback 4: first visible chat row under search — when search is narrowed,
        # the correct contact is usually the first result in the Chats list.
        try:
            first_row = page.locator('[role="listitem"]').first
            if first_row.count() > 0 and first_row.is_visible():
                if do_click(first_row):
                    return True
        except Exception:
            pass

        # Fallback 5: keyboard — focus first result and press Enter (WhatsApp may focus search results)
        try:
            page.keyboard.press("ArrowDown")
            page.wait_for_timeout(250)
            page.keyboard.press("Enter")
            page.wait_for_timeout(500)
            return True
        except Exception:
            pass
    except Exception:
        pass
    return False


def _wa_find_message_box(page, wait_sec: float = 5):
    """
    After choosing a contact, find the message input by the 'Type a message' / 'Type message here' placeholder/label.
    Waits for it to appear then returns the locator or None.
    """
    try:
        # Wait for the message box to appear (chat pane loads after selecting contact)
        for _ in range(int(wait_sec * 2)):
            # 1. Placeholder text (WhatsApp Web: 'Type a message' or 'Type message here')
            for placeholder in ("Type a message", "Type message here"):
                pl = page.get_by_placeholder(placeholder)
                if pl.count() > 0 and pl.first.is_visible():
                    return pl.first
            # 2. data-placeholder / aria-placeholder / contenteditable near bottom
            for sel in (
                '[data-placeholder*="Type a message"]',
                '[data-placeholder="Type a message"]',
                '[aria-placeholder*="Type a message"]',
                '[data-placeholder*="Type message here"]',
                '[aria-placeholder*="Type message here"]',
                'footer [contenteditable="true"]',
                'div[contenteditable="true"][data-placeholder*="Type a message"]',
                'div[contenteditable="true"][data-placeholder*="Type message here"]',
                'div[role="textbox"][aria-placeholder*="Type a message"]',
                'div[role="textbox"][aria-placeholder*="Type message here"]',
            ):
                loc = page.locator(sel).first
                if loc.count() > 0 and loc.is_visible():
                    return loc
            # 3. Element that contains the text placeholder (label) and climb to contenteditable ancestor
            for txt in ("Type a message", "Type message here"):
                by_text = page.get_by_text(txt, exact=False).first
                if by_text.count() > 0 and by_text.is_visible():
                    try:
                        editable = by_text.locator("xpath=(./ancestor::*[@contenteditable='true'])[1]").first
                        if editable.count() > 0 and editable.is_visible():
                            return editable
                    except Exception:
                        pass
            page.wait_for_timeout(500)
        return None
    except Exception:
        return None


def _wa_analyze_ready(page, max_wait_sec: float = 12) -> tuple[bool, str]:
    """
    Analyzer: wait until the contacts/chat list is visible (login done), then return (True, None).
    If after max_wait_sec we still don't see the list, return (False, reason).
    """
    step = 0.8
    elapsed = 0.0
    while elapsed < max_wait_sec:
        try:
            # First rule out QR login page
            content = (page.content() or "").lower()
            if "scan this qr" in content or "link with phone" in content or "link your phone" in content:
                return False, "WhatsApp is on the login (QR) page — scan with your phone first."
            # Require contacts/chat list visible (left pane with conversations)
            listitems = page.locator("[role='listitem']")
            if listitems.count() > 0:
                first = listitems.first
                if first.is_visible():
                    _mark_ready(WA_PROFILE_DIR)
                    return True, ""
            # Also accept search box as proof the app is loaded and logged in
            for sel in ('[data-tab="3"]', '[aria-label="Search input textbox"]'):
                loc = page.locator(sel).first
                if loc.count() > 0 and loc.is_visible():
                    _mark_ready(WA_PROFILE_DIR)
                    return True, ""
        except Exception as e:
            pass
        page.wait_for_timeout(int(step * 1000))
        elapsed += step
    return False, "Contacts list did not appear in time — make sure you're logged in on WhatsApp Web."


def _run_wa_msg(contact: str, description: str | None = None) -> tuple[bool, str]:
    raw_wa = (description or "").strip()
    if raw_wa:
        msg_text = _rephrase_dm(raw_wa, "WhatsApp", contact) or raw_wa
    else:
        msg_text = _default_wa_msg()
    with _wa_lock:
        try:
            from playwright.sync_api import sync_playwright
            if _wa_boot:
                return False, "WhatsApp login window is still open. Close it, then try **!msg** again."
            os.makedirs(WA_PROFILE_DIR, exist_ok=True)
            context = None
            try:
                with sync_playwright() as p:
                    context = _launch_social_browser(WA_PROFILE_DIR, p)
                    page = context.pages[0] if context.pages else context.new_page()
                    page.goto(WA_WEB_URL, wait_until="domcontentloaded", timeout=60000)
                    page.wait_for_timeout(2000)
                    # Analyzer: confirm login by waiting for contacts/chat list to be visible, then continue
                    ready, reason = _wa_analyze_ready(page, max_wait_sec=12)
                    if not ready:
                        try: context.close()
                        except Exception: pass
                        context = None
                        _clear_ready(WA_PROFILE_DIR)
                        _bootstrap_window(WA_PROFILE_DIR, WA_WEB_URL, "_wa_boot")
                        return False, reason or "WhatsApp Web needs login. Browser opened — scan QR, then try again."
                    # Find search (contacts list is visible, so UI is ready)
                    sb = None
                    for sel in ['[data-tab="3"]','[aria-label="Search input textbox"]','div[contenteditable="true"][data-tab="3"]']:
                        loc = page.locator(sel).first
                        if loc.count() and loc.is_visible(): sb=loc; break
                    if not sb: return False, "WhatsApp Web search not found."
                    sb.click(); sb.fill(""); page.wait_for_timeout(200)
                    # Type full contact so search shows entire number/username, then wait for results
                    sb.press_sequentially(contact, delay=50)
                    page.wait_for_timeout(3500)
                    matching_labels = _wa_get_matching_contact_labels(page, contact)
                    if len(matching_labels) > 1:
                        try: context.close()
                        except Exception: pass
                        context = None
                        return False, {"need_feedback": True, "message": "Multiple contacts match. Which one?", "options": matching_labels}
                    if not _wa_click_contact_row(page, contact):
                        return False, f"Could not find contact **{contact}** in search. Check the full number or name."
                    # Make sure the conversation window actually opened (center pane),
                    # not just the promo "Download WhatsApp for Windows" screen.
                    for _ in range(3):
                        html = (page.content() or "").lower()
                        if "download whatsapp for windows" not in html:
                            break
                        _wa_click_contact_row(page, contact)
                        page.wait_for_timeout(700)
                    # Wait for chat to open and find the message box by placeholder.
                    # If it doesn't appear, try re-clicking the chat row and checking again (analyze window & retry).
                    mi = None
                    for _ in range(3):
                        mi = _wa_find_message_box(page)
                        if mi:
                            break
                        _wa_click_contact_row(page, contact)
                        page.wait_for_timeout(800)
                    if not mi:
                        return True, f"Opened chat with {contact} but couldn't find message box (look for 'Type message here')."
                    mi.click(); mi.fill(""); mi.press_sequentially(msg_text, delay=30)
                    page.wait_for_timeout(400)
                    for sel in ['[data-testid="send"]','[aria-label="Send"]']:
                        btn = page.locator(sel).first
                        if btn.count() and btn.is_visible(): btn.click(); break
                    else:
                        page.keyboard.press("Enter")
                    page.wait_for_timeout(500)
                    return True, f'WhatsApp: sent to **{contact}**: "{msg_text[:50]}{"…" if len(msg_text)>50 else ""}"'
            except Exception as e:
                return False, f"WhatsApp error: {e}"
            finally:
                if context:
                    try: context.close()
                    except Exception: pass
        except ImportError:
            return False, "Playwright not installed."


def _run_wa_translate(contact: str | None = None) -> tuple[bool, str]:
    """Open WhatsApp Web, get the last voice message in the current (or specified) chat, translate to English."""
    with _wa_lock:
        try:
            from playwright.sync_api import sync_playwright
            if _wa_boot:
                return False, "WhatsApp login window is still open. Close it, then try again."
            os.makedirs(WA_PROFILE_DIR, exist_ok=True)
            context = None
            try:
                with sync_playwright() as p:
                    context = _launch_social_browser(WA_PROFILE_DIR, p)
                    page = context.pages[0] if context.pages else context.new_page()
                    page.goto(WA_WEB_URL, wait_until="domcontentloaded", timeout=60000)
                    page.wait_for_timeout(2000)
                    # Analyzer: confirm login by waiting for contacts/chat list, then continue
                    ready, reason = _wa_analyze_ready(page, max_wait_sec=12)
                    if not ready:
                        try: context.close()
                        except Exception: pass
                        context = None
                        _clear_ready(WA_PROFILE_DIR)
                        _bootstrap_window(WA_PROFILE_DIR, WA_WEB_URL, "_wa_boot")
                        return False, reason or "WhatsApp Web needs login. Browser opened — scan QR, then try again."
                    if contact:
                        sb = None
                        for sel in ['[data-tab="3"]', '[aria-label="Search input textbox"]', 'div[contenteditable="true"][data-tab="3"]']:
                            loc = page.locator(sel).first
                            if loc.count() and loc.is_visible():
                                sb = loc
                                break
                        if sb:
                            sb.click()
                            sb.fill("")
                            page.wait_for_timeout(200)
                            sb.press_sequentially(contact, delay=50)
                            page.wait_for_timeout(3500)
                            if not _wa_click_contact_row(page, contact):
                                return False, f"Could not find contact **{contact}** in search. Check the full number or name."
                            page.wait_for_timeout(1500)
                    # Get last voice message audio as base64 (blob or any src)
                    get_audio_script = """
                    (async () => {
                        const audios = Array.from(document.querySelectorAll('audio')).filter(a => a.src);
                        if (audios.length === 0) return null;
                        const last = audios[audios.length - 1];
                        try {
                            const r = await fetch(last.src);
                            const buf = await r.arrayBuffer();
                            const arr = new Uint8Array(buf);
                            let s = '';
                            for (let i = 0; i < arr.length; i++) s += String.fromCharCode(arr[i]);
                            return btoa(s);
                        } catch (e) { return null; }
                    })()
                    """
                    b64 = page.evaluate(get_audio_script)
                    if not b64 or not isinstance(b64, str):
                        return False, "No voice message found in this chat. Open a chat with a voice message and try **!wa_translate** again (or **!wa_translate contact**)."
                    raw = base64.b64decode(b64)
                    if len(raw) < 100:
                        return False, "Voice message too short or failed to download."
                    fd, path = tempfile.mkstemp(suffix=".ogg")
                    try:
                        os.write(fd, raw)
                        os.close(fd)
                        fd = None
                        text = _whisper_translate(path)
                        if not (text or text.strip()):
                            return False, "Could not translate that voice message (Whisper failed)."
                        return True, f"**Translation (→ English):** {text.strip()}"
                    finally:
                        try:
                            if fd is not None:
                                os.close(fd)
                            if path and os.path.isfile(path):
                                os.unlink(path)
                        except Exception:
                            pass
            except Exception as e:
                return False, f"WhatsApp translate error: {e}"
            finally:
                if context:
                    try:
                        context.close()
                    except Exception:
                        pass
        except ImportError:
            return False, "Playwright not installed."
    return False, "WhatsApp translate failed."


def _resolve_discord_username_by_id(user_id: int) -> str | None:
    """Resolve Discord user ID to username (for Playwright search). Runs on bot loop from sync context."""
    try:
        future = asyncio.run_coroutine_threadsafe(bot.fetch_user(int(user_id)), bot.loop)
        user = future.result(timeout=10)
        return getattr(user, "name", None) or (user.global_name if getattr(user, "global_name", None) else None)
    except Exception:
        return None


def _run_discord_call(contact: str) -> tuple[bool, str]:
    """Discord blocks automated calling. Redirect to !dm for messaging."""
    return False, "Discord blocks automated calling. Use **!dm** to send a message instead."


def _rephrase_dm(user_input: str, platform: str = "social media", recipient: str = "") -> str:
    """Use the LLM to rephrase user's instructions into a natural DM in Luna's own words."""
    if not user_input or not user_input.strip():
        return ""
    brief = user_input.strip()[:800]
    to_part = f" to {recipient}" if recipient else ""
    system = (
        f"You are Luna, a witty, friendly assistant. The user wants to send a {platform} DM{to_part}. "
        "Below is what they want to get across (their instructions or context). Write a single short message (1-3 sentences) "
        "that conveys this in your own words — natural, concise, and friendly. Add emojis if it fits the tone. "
        "Output ONLY the message body to send; no quotes, no 'Message:' prefix, no preamble or explanation."
    )
    out = ollama_chat(
        f"What the user wants to communicate:\n{brief}\n\nWrite the actual message to send:",
        system=system,
    )
    if not out or "Ollama" in out or out.startswith("Error:"):
        return ""
    return out.strip().strip('"\'')[:1500]

def _dm_message_from_input(user_input: str) -> str:
    """Backward-compatible wrapper for Discord DMs."""
    return _rephrase_dm(user_input, "Discord")


def _run_discord_dm(target: str, message: str) -> tuple[bool, str]:
    """Send a Discord DM. Luna always generates the message from the user's input (dynamic reply, like YouTube comments)."""
    target = (target or "").strip()
    message = (message or "").strip()
    if not target:
        return False, "Usage: !dm <Discord username or user ID> [what to get across — Luna will rephrase]"
    if not message:
        message = "Hey, Luna here — you got a DM from the bot."
    else:
        # Always generate the actual DM from user's input (generalized, in Luna's words — like yt_comment)
        generated = _dm_message_from_input(message)
        if generated:
            message = generated
        # else send original if Ollama failed

    async def _send():
        user = None
        if target.isdigit() and len(target) >= 17:
            try:
                user = await bot.fetch_user(int(target))
            except Exception:
                pass
        if not user:
            for g in bot.guilds:
                m = g.get_member_named(target)
                if not m and target.isdigit():
                    m = g.get_member(int(target))
                if m:
                    user = m
                    break
        if not user:
            return False, f"Could not find user **{target}**. Use username or numeric user ID."
        ch = user.dm_channel or await user.create_dm()
        await ch.send(message[:2000])
        name = getattr(user, "display_name", None) or getattr(user, "name", "user")
        return True, f"Discord DM sent to **{name}**."

    try:
        future = asyncio.run_coroutine_threadsafe(_send(), bot.loop)
        return future.result(timeout=15)
    except Exception as e:
        return False, str(e)


# ── Discord voice call: join by user ID, transcribe VC + answer with TTS ─────
_call_session: dict | None = None
_call_session_lock = threading.Lock()


async def _join_linked_user_vc_and_speak(text: str, disconnect_after: bool = True) -> bool:
    """Join the linked user's voice channel and play TTS. Returns True if successful."""
    if not _linked_int:
        return False
    target_channel = None
    for g in bot.guilds:
        m = g.get_member(_linked_int)
        if m and m.voice and m.voice.channel:
            target_channel = m.voice.channel
            break
    if not target_channel:
        return False
    vc = target_channel.guild.voice_client
    need_connect = not vc or not vc.is_connected() or vc.channel != target_channel
    if need_connect:
        try:
            if vc and vc.is_connected():
                await vc.move_to(target_channel)
            else:
                await target_channel.connect()
        except Exception:
            return False
        vc = target_channel.guild.voice_client
    if not vc or not vc.is_connected():
        return False
    tts_text = _clean_for_tts(text)
    if not tts_text.strip():
        return True
    mp3 = await asyncio.to_thread(_tts_bytes, tts_text[:400])
    if not mp3:
        return True
    fd, path = tempfile.mkstemp(suffix=".mp3")
    try:
        os.write(fd, mp3)
        os.close(fd)
        fd = None
        source = discord.FFmpegPCMAudio(path, options=_FFMPEG_OPTS)
        vc.play(source, after=lambda e: None)
        while vc.is_playing() and vc.is_connected():
            await asyncio.sleep(0.2)
        if disconnect_after and vc.is_connected():
            await vc.disconnect()
    except Exception:
        pass
    finally:
        if fd is not None:
            try: os.close(fd)
            except Exception: pass
        try: os.unlink(path)
        except Exception: pass
    return True


async def _discord_vc_speak_reply_to_author(message: discord.Message, reply: str) -> None:
    """Join the message author's voice channel (if any) and play TTS of Luna's text reply. No-op in DMs or if author not in VC."""
    if not _DISCORD_REPLY_VC_TTS or not message.guild or not (reply or "").strip():
        return
    member = message.author
    if not isinstance(member, discord.Member):
        return
    if not member.voice or not member.voice.channel:
        return
    target_channel = member.voice.channel
    guild = message.guild
    vc = guild.voice_client
    try:
        if not vc or not vc.is_connected() or vc.channel != target_channel:
            if vc and vc.is_connected():
                await vc.move_to(target_channel)
            else:
                await target_channel.connect()
            vc = guild.voice_client
        if not vc or not vc.is_connected():
            return
        tts_text = _clean_for_tts(reply)
        if not tts_text.strip():
            return
        if len(tts_text) > 4500:
            tts_text = tts_text[:4500] + " — truncated."
        try:
            if vc.is_playing():
                vc.stop()
                await asyncio.sleep(0.15)
        except Exception:
            pass
        for chunk in _split_tts(tts_text, max_chars=200):
            piece = (chunk or "").strip()[:500]
            if not piece:
                continue
            while vc.is_playing() and vc.is_connected():
                await asyncio.sleep(0.2)
            mp3 = await asyncio.to_thread(_tts_bytes, piece)
            if not mp3:
                continue
            fd, path = tempfile.mkstemp(suffix=".mp3")
            try:
                os.write(fd, mp3)
                os.close(fd)
                fd = None
                done = asyncio.Event()
                loop = bot.loop

                def _after_play(_err):
                    loop.call_soon_threadsafe(done.set)

                play_path = path.replace("\\", "/")
                source = discord.FFmpegPCMAudio(play_path, options=_FFMPEG_OPTS)
                vc.play(source, after=_after_play)
                try:
                    await asyncio.wait_for(done.wait(), timeout=180)
                except asyncio.TimeoutError:
                    try:
                        vc.stop()
                    except Exception:
                        pass
            except Exception:
                pass
            finally:
                if fd is not None:
                    try:
                        os.close(fd)
                    except Exception:
                        pass
                try:
                    os.unlink(path)
                except Exception:
                    pass
    except Exception:
        pass


def _schedule_discord_vc_tts_reply(message: discord.Message, reply: str | None) -> None:
    if not reply or not isinstance(reply, str):
        return
    try:
        asyncio.create_task(_discord_vc_speak_reply_to_author(message, reply))
    except RuntimeError:
        if bot.loop and bot.loop.is_running():
            bot.loop.create_task(_discord_vc_speak_reply_to_author(message, reply))

_MAX_TTS_FILE_PARTS = 8


async def _discord_post_tts_voice_files(
    channel: discord.abc.Messageable, text: str, *, max_parts: int = _MAX_TTS_FILE_PARTS
) -> bool:
    """Post one or more MP3 attachments of *Luna speaking* `text` (her line), same TTS stack as VC/reminders."""
    raw = (text or "").strip()
    if not raw:
        return False
    tts_text = _expand_luna_expression_tags_for_tts(_clean_for_tts(raw))
    if not tts_text:
        return False
    if len(tts_text) > 4000:
        tts_text = tts_text[:4000] + " — truncated."
    parts = _split_tts_paragraphs(tts_text, max_chars=450)
    if not parts:
        return False
    sent = 0
    for i, part in enumerate(parts):
        if sent >= max_parts:
            break
        p = (part or "").strip()[:500]
        if not p:
            continue
        mp3 = await asyncio.to_thread(_tts_bytes, p)
        if not mp3:
            continue
        try:
            await channel.send(
                file=discord.File(io.BytesIO(mp3), filename=f"luna-voice-{sent + 1}.mp3")
            )
            sent += 1
        except Exception as e:
            print(f"[Luna] TTS file send failed: {e}", flush=True)
    return sent > 0


def _should_post_discord_tts_file_for_message(message: discord.Message) -> bool:
    if isinstance(message.channel, discord.DMChannel) and _DISCORD_TTS_FILE_DM:
        return True
    if message.guild and _DISCORD_TTS_FILE_GUILD:
        # If DISCORD_TTS_CHANNEL_IDS is empty, treat it as "all text channels in guilds".
        if not _tts_channels:
            return True
        ch = message.channel
        if ch and getattr(ch, "id", None) in _tts_channels:
            return True
        # Threads can inherit allow from their parent text channel.
        parent_id = getattr(getattr(ch, "parent", None), "id", None)
        if parent_id and parent_id in _tts_channels:
            return True
    return False


async def _discord_file_tts_after_reply(message: discord.Message, reply: str) -> None:
    if not (reply or "").strip() or reply == COMMAND_ONLY:
        return
    if not _should_post_discord_tts_file_for_message(message):
        return
    try:
        await _discord_post_tts_voice_files(message.channel, reply)
    except Exception:
        pass


def _schedule_discord_file_tts(message: discord.Message, reply: str | None) -> None:
    if not reply or not isinstance(reply, str):
        return
    if not _should_post_discord_tts_file_for_message(message):
        return
    try:
        asyncio.create_task(_discord_file_tts_after_reply(message, reply))
    except RuntimeError:
        if bot.loop and bot.loop.is_running():
            bot.loop.create_task(_discord_file_tts_after_reply(message, reply))


def _can_use_tts_exclaim_command(message: discord.Message) -> bool:
    """!tts in DM, or in a server if linked / admin / DISCORD_TTS_CHANNEL_IDS channel."""
    if isinstance(message.channel, discord.DMChannel):
        return True
    uid = message.author.id
    if _linked_int and uid == _linked_int:
        return True
    if _admin_int and uid == _admin_int:
        return True
    if message.channel and message.channel.id in _tts_channels:
        return True
    return False


def _play_tts_in_vc_sync(vc, text: str) -> None:
    """Generate TTS and play in Discord VC (run on bot loop from sync)."""
    tts_text = _clean_for_tts(text)
    if not tts_text.strip():
        return
    mp3 = _tts_bytes(tts_text[:400])
    if not mp3:
        return
    fd, path = tempfile.mkstemp(suffix=".mp3")
    try:
        os.write(fd, mp3)
        os.close(fd)
        fd = None
        done = threading.Event()
        async def _play():
            try:
                if not vc or not vc.is_connected():
                    return
                source = discord.FFmpegPCMAudio(path, options=_FFMPEG_OPTS)
                vc.play(source, after=lambda e: done.set())
                while not done.is_set() and vc.is_connected():
                    await asyncio.sleep(0.2)
            finally:
                done.set()
        future = asyncio.run_coroutine_threadsafe(_play(), bot.loop)
        future.result(timeout=30)
    finally:
        if fd is not None:
            try: os.close(fd)
            except Exception: pass
        try: os.unlink(path)
        except Exception: pass


def _discord_attachment_looks_audio(att) -> bool:
    """Heuristic: Discord voice clip / audio file attachment."""
    try:
        ctype = str(getattr(att, "content_type", "") or "").lower()
        if ctype.startswith("audio/"):
            return True
        name = str(getattr(att, "filename", "") or "").lower()
        if any(name.endswith(ext) for ext in (".ogg", ".opus", ".mp3", ".m4a", ".wav", ".webm", ".aac", ".flac", ".mp4")):
            return True
        if getattr(att, "duration", None) is not None:
            return True
    except Exception:
        return False
    return False


_speechbrain_emotion_lock = threading.Lock()
_speechbrain_emotion_model = None
_speechbrain_emotion_failed = False


def _load_speechbrain_emotion_model():
    """Lazy-load SpeechBrain emotion classifier."""
    global _speechbrain_emotion_model, _speechbrain_emotion_failed
    if LUNA_VOICE_EMOTION_MODEL in ("", "off", "none", "0", "false", "no"):
        return None
    if LUNA_VOICE_EMOTION_MODEL != "speechbrain":
        return None
    with _speechbrain_emotion_lock:
        if _speechbrain_emotion_model is not None:
            return _speechbrain_emotion_model
        if _speechbrain_emotion_failed:
            return None
        try:
            from speechbrain.inference.interfaces import foreign_class  # type: ignore

            savedir = os.path.join(_DATA, "models", "speechbrain_emotion")
            os.makedirs(savedir, exist_ok=True)
            _speechbrain_emotion_model = foreign_class(
                source=LUNA_VOICE_EMOTION_MODEL_ID,
                pymodule_file="custom_interface.py",
                classname="CustomEncoderWav2vec2Classifier",
                savedir=savedir,
            )
            return _speechbrain_emotion_model
        except Exception as e:
            _speechbrain_emotion_failed = True
            if LUNA_CALL_DEBUG:
                print(
                    f"[CallDebug {datetime.now().strftime('%H:%M:%S')}] speechbrain_load_failed | err={str(e)[:220]}",
                    flush=True,
                )
            return None


def _speechbrain_emotion_predict(wav16_mono_path: str) -> dict | None:
    """Return emotion from SpeechBrain model on normalized wav path."""
    model = _load_speechbrain_emotion_model()
    if model is None:
        return None
    try:
        out = model.classify_file(wav16_mono_path)
        raw_label = None
        conf = None
        if isinstance(out, tuple):
            # Typical: (out_prob, score, index, text_lab)
            if len(out) >= 2:
                try:
                    s = out[1]
                    if hasattr(s, "item"):
                        conf = float(s.item())
                    elif isinstance(s, (list, tuple)) and s:
                        conf = float(s[0])
                    else:
                        conf = float(s)
                except Exception:
                    conf = None
            if len(out) >= 4:
                raw_label = out[3]
            elif len(out) >= 3:
                raw_label = out[2]
        else:
            raw_label = out
        # text label may be nested/tensor/list
        if isinstance(raw_label, (list, tuple)) and raw_label:
            raw_label = raw_label[0]
        if hasattr(raw_label, "item"):
            try:
                raw_label = raw_label.item()
            except Exception:
                pass
        if isinstance(raw_label, bytes):
            raw_label = raw_label.decode("utf-8", "ignore")
        label = str(raw_label or "").strip().lower()
        label = re.sub(r"[^a-z_]", "", label)
        # Common IEMOCAP labels -> app-friendly labels
        map_label = {
            "hap": "happy",
            "happy": "happy",
            "neu": "neutral",
            "neutral": "neutral",
            "ang": "angry",
            "anger": "angry",
            "angry": "angry",
            "sad": "sad",
            "fru": "frustrated",
            "frustrated": "frustrated",
            "exc": "excited",
            "excited": "excited",
            "sur": "surprised",
            "fear": "fearful",
            "fearful": "fearful",
            "disgust": "disgusted",
            "disgusted": "disgusted",
        }
        normalized = map_label.get(label, label or "neutral")
        if conf is None:
            conf = 0.0
        return {
            "model": LUNA_VOICE_EMOTION_MODEL_ID,
            "model_raw_label": str(raw_label or ""),
            "model_emotion": normalized,
            "model_confidence": round(float(conf), 4),
        }
    except Exception as e:
        if LUNA_CALL_DEBUG:
            print(
                f"[CallDebug {datetime.now().strftime('%H:%M:%S')}] speechbrain_predict_failed | err={str(e)[:220]}",
                flush=True,
            )
        return None


def _analyze_voice_clip_emotion(audio_path: str, transcript: str = "") -> dict | None:
    """
    Estimate speaker emotion from acoustic cues in a voice clip.
    Uses volume, energy variation, voiced ratio, and tone roughness proxy (ZCR).
    """
    fd = wav_fd = None
    wav_path = None
    try:
        wav_fd, wav_path = tempfile.mkstemp(suffix=".wav")
        os.close(wav_fd)
        wav_fd = None
        if not _ffmpeg_to_wav16_mono_voice(audio_path, wav_path):
            return None
        with wave.open(wav_path, "rb") as w:
            sr = int(w.getframerate() or 16000)
            ch = int(w.getnchannels() or 1)
            sw = int(w.getsampwidth() or 2)
            if sw != 2:
                return None
            raw = w.readframes(w.getnframes())
        if not raw or len(raw) < 400:
            return None
        if ch > 1:
            # Keep first channel from interleaved PCM.
            mono = bytearray(len(raw) // ch)
            oi = 0
            for i in range(0, len(raw) - (2 * ch) + 1, 2 * ch):
                mono[oi:oi + 2] = raw[i:i + 2]
                oi += 2
            raw = bytes(mono[:oi])
        frame_samples = max(1, int(sr * 0.025))  # 25ms
        frame_bytes = frame_samples * 2
        rms_vals: list[float] = []
        zcr_vals: list[float] = []
        for off in range(0, len(raw) - frame_bytes + 1, frame_bytes):
            chunk = raw[off:off + frame_bytes]
            rms = _call_pcm_rms(chunk)
            rms_vals.append(rms)
            # Zero crossing rate (tone/noisiness proxy).
            prev = 0
            crossings = 0
            n = 0
            for i in range(0, len(chunk) - 1, 2):
                s = struct.unpack_from("<h", chunk, i)[0]
                sign = 1 if s >= 0 else -1
                if prev and sign != prev:
                    crossings += 1
                prev = sign
                n += 1
            zcr_vals.append(crossings / max(1, n))
        if not rms_vals:
            return None
        avg_rms = sum(rms_vals) / len(rms_vals)
        rms_var = (sum((x - avg_rms) ** 2 for x in rms_vals) / len(rms_vals)) ** 0.5
        avg_zcr = sum(zcr_vals) / max(1, len(zcr_vals))
        peak = 0
        for i in range(0, len(raw) - 1, 2):
            v = abs(struct.unpack_from("<h", raw, i)[0])
            if v > peak:
                peak = v
        # Dynamic threshold based on floor/speech split.
        srms = sorted(rms_vals)
        p20 = srms[int(max(0, min(len(srms) - 1, round(0.20 * (len(srms) - 1)))))]
        p80 = srms[int(max(0, min(len(srms) - 1, round(0.80 * (len(srms) - 1)))))]
        voiced_thr = max(70.0, min(900.0, p20 + (p80 - p20) * 0.30))
        voiced_ratio = sum(1 for x in rms_vals if x >= voiced_thr) / max(1, len(rms_vals))

        if avg_rms < 120:
            volume = "very quiet"
        elif avg_rms < 320:
            volume = "quiet"
        elif avg_rms < 900:
            volume = "normal"
        elif avg_rms < 1900:
            volume = "loud"
        else:
            volume = "very loud"

        voice_emotion = "neutral"
        if avg_rms > 1300 and (rms_var > 700 or avg_zcr > 0.11):
            voice_emotion = "excited"
        elif avg_rms > 900 and avg_zcr > 0.13:
            voice_emotion = "tense"
        elif avg_rms < 180 and voiced_ratio < 0.45:
            voice_emotion = "sad"
        elif avg_rms < 280 and rms_var < 170 and avg_zcr < 0.08:
            voice_emotion = "calm"

        # Blend with lexical emotion inference for better robustness.
        text_emotion = _call_detect_emotion(transcript or "", avg_rms)
        final_emotion = voice_emotion
        if text_emotion in ("angry", "sad", "anxious", "happy", "excited"):
            if voice_emotion in ("neutral", "calm") or text_emotion in ("angry", "excited"):
                final_emotion = text_emotion
        model_meta = _speechbrain_emotion_predict(wav_path)
        if isinstance(model_meta, dict):
            m_emo = str(model_meta.get("model_emotion") or "").strip().lower()
            m_conf = float(model_meta.get("model_confidence") or 0.0)
            # Prefer model when confidence is solid; keep heuristics as fallback.
            if m_emo and m_conf >= LUNA_VOICE_EMOTION_MODEL_MIN_CONF:
                final_emotion = m_emo
        return {
            "emotion": final_emotion,
            "voice_emotion": voice_emotion,
            "text_emotion": text_emotion,
            "volume": volume,
            "rms": round(avg_rms, 1),
            "variation": round(rms_var, 1),
            "zcr": round(avg_zcr, 4),
            "voiced_ratio": round(voiced_ratio, 3),
            "peak": int(peak),
            **(model_meta or {}),
        }
    except Exception:
        return None
    finally:
        try:
            if wav_fd is not None:
                os.close(wav_fd)
        except Exception:
            pass
        try:
            if wav_path and os.path.isfile(wav_path):
                os.unlink(wav_path)
        except Exception:
            pass


async def _discord_transcribe_voice_clip(message: discord.Message) -> tuple[str | None, bool, str | None, dict | None]:
    """Transcribe first audio-like attachment from a Discord message."""
    atts = list(getattr(message, "attachments", []) or [])
    audio_atts = [a for a in atts if _discord_attachment_looks_audio(a)]
    if not audio_atts:
        return None, False, None, None
    att = audio_atts[0]
    in_fd = None
    in_path = None
    try:
        ext = os.path.splitext(str(getattr(att, "filename", "") or ""))[1].strip() or ".ogg"
        if len(ext) > 8 or not ext.startswith("."):
            ext = ".ogg"
        in_fd, in_path = tempfile.mkstemp(suffix=ext)
        os.close(in_fd)
        in_fd = None
        await att.save(in_path)
        if not os.path.isfile(in_path) or os.path.getsize(in_path) < 80:
            return None, True, "Audio clip is empty or too short.", None
        text = await asyncio.to_thread(_whisper_transcribe, in_path)
        text = (text or "").strip()
        if not text:
            return None, True, "I could not transcribe that voice clip.", None
        emo = await asyncio.to_thread(_analyze_voice_clip_emotion, in_path, text)
        return text, True, None, emo
    except Exception:
        return None, True, "Voice clip transcription failed.", None
    finally:
        try:
            if in_fd is not None:
                os.close(in_fd)
        except Exception:
            pass
        try:
            if in_path and os.path.isfile(in_path):
                os.unlink(in_path)
        except Exception:
            pass


_silero_vad_lock = threading.Lock()
_silero_vad_cached: tuple[object, object, object] | None = None  # (model, get_speech_timestamps, read_audio)
_silero_vad_failed = False


def _load_silero_vad_runtime() -> tuple[object, object, object] | None:
    """Lazy-load Silero VAD runtime. Returns (model, get_speech_timestamps, read_audio)."""
    global _silero_vad_cached, _silero_vad_failed
    if not LUNA_CALL_USE_SILERO_VAD:
        return None
    with _silero_vad_lock:
        if _silero_vad_cached is not None:
            return _silero_vad_cached
        if _silero_vad_failed:
            return None
        try:
            from silero_vad import load_silero_vad, get_speech_timestamps, read_audio  # type: ignore

            model = load_silero_vad()
            _silero_vad_cached = (model, get_speech_timestamps, read_audio)
            return _silero_vad_cached
        except Exception:
            _silero_vad_failed = True
            return None


def _call_silero_has_speech(wav_path: str) -> bool:
    """Silero gate: True only if this chunk contains speech-like regions."""
    rt = _load_silero_vad_runtime()
    if rt is None:
        # Fail-open if Silero unavailable so call mode still works.
        return True
    try:
        model, get_speech_timestamps, read_audio = rt
        wav = read_audio(wav_path, sampling_rate=16000)
        ts = get_speech_timestamps(
            wav,
            model,
            sampling_rate=16000,
            return_seconds=True,
            # Be slightly more permissive for Discord Voice Isolation/Krisp style denoising.
            threshold=0.46,
            min_speech_duration_ms=90,
            min_silence_duration_ms=80,
        )
        if not ts:
            return False
        # Require a minimum total speech duration to reject tiny artifacts.
        total = 0.0
        for seg in ts:
            try:
                total += max(0.0, float(seg.get("end", 0.0)) - float(seg.get("start", 0.0)))
            except Exception:
                continue
        return total >= 0.10
    except Exception:
        # Fail-open on runtime errors to avoid muting the call completely.
        return True


def _call_pcm_rms(pcm: bytes) -> float:
    """Approx RMS from 16-bit PCM (sampled for speed)."""
    if not pcm or len(pcm) < 2:
        return 0.0
    sample_count = len(pcm) // 2
    step = max(1, sample_count // 5000)  # downsample for speed
    acc = 0.0
    n = 0
    for i in range(0, sample_count, step):
        try:
            s = struct.unpack_from("<h", pcm, i * 2)[0]
        except Exception:
            continue
        acc += float(s) * float(s)
        n += 1
    if n <= 0:
        return 0.0
    return (acc / n) ** 0.5


def _call_volume_label(rms: float) -> str:
    if rms < 220:
        return "very quiet"
    if rms < 650:
        return "quiet"
    if rms < 1600:
        return "normal"
    if rms < 3200:
        return "loud"
    return "very loud"


def _call_detect_emotion(text: str, rms: float) -> str:
    """Lightweight emotion heuristic for organizer context."""
    t = (text or "").strip().lower()
    if not t:
        return "neutral"
    joy = ("haha", "lol", "lmao", "great", "awesome", "love", "nice", "amazing", "excited")
    anger = ("wtf", "stupid", "hate", "mad", "angry", "annoying", "idiot", "bullshit")
    sad = ("sad", "tired", "depressed", "hurt", "upset", "lonely", "cry", "bad day")
    anxious = ("worried", "anxious", "nervous", "stress", "scared", "panic")
    if any(k in t for k in anger):
        return "angry"
    if any(k in t for k in sad):
        return "sad"
    if any(k in t for k in anxious):
        return "anxious"
    if any(k in t for k in joy):
        return "happy"
    if rms > 2500 and "!" in t:
        return "excited"
    if "?" in t and rms < 700:
        return "uncertain"
    return "neutral"


def _call_is_natural_language(text: str, rms: float) -> bool:
    """Gate VC transcriptions: ignore filler/noise and react only to natural language."""
    t = " ".join((text or "").strip().split())
    if len(t) < 6:
        return False
    words = re.findall(r"[A-Za-z0-9']+", t)
    if not words:
        return False
    # Very short/filler-only utterances are usually noise for conversational turn-taking.
    fillers = {
        "uh", "um", "hmm", "hm", "mm", "mmm", "ah", "eh", "oh", "yo", "huh",
        "uhh", "umm", "hmmm", "mmmh", "mmh",
    }
    if len(words) == 1 and words[0].lower() in fillers:
        return False
    # Need at least 2 words, or one meaningful long word.
    if len(words) < 2 and not any(len(w) >= 5 for w in words):
        return False
    # Ensure this looks like language, not symbol/noise artifacts.
    letters = sum(ch.isalpha() for ch in t)
    alnum = sum(ch.isalnum() for ch in t)
    if alnum <= 0:
        return False
    if letters / max(1, alnum) < 0.45:
        return False
    # Ultra-low energy + tiny token count = likely background/noise.
    # Isolation/denoise can reduce energy sharply; avoid over-rejecting quiet speech.
    if rms < 90 and len(words) < 3:
        return False
    # Repeated single-char transcriptions like "aaaaa", "mmmmmm"
    if re.fullmatch(r"(?i)\s*([a-z])\1{3,}\s*", t):
        return False
    return True


def _call_organizer_snapshot(speaker_state: dict[int, dict], focus_uid: int | None = None) -> str:
    """Compact organizer context: who is in call + voice/emotion state."""
    rows: list[str] = []
    now = time.time()
    ranked = sorted(speaker_state.items(), key=lambda kv: float(kv[1].get("last_ts") or 0.0), reverse=True)
    for uid, st in ranked[:12]:
        name = str(st.get("name") or f"user-{uid}")
        emotion = str(st.get("emotion") or "neutral")
        volume = str(st.get("volume_label") or "normal")
        rms = float(st.get("rms") or 0.0)
        words_per_sec = float(st.get("words_per_sec") or 0.0)
        heard_sec = max(0.0, now - float(st.get("last_ts") or 0.0))
        marker = " (current speaker)" if focus_uid is not None and uid == focus_uid else ""
        rows.append(
            f"- {name}{marker}: emotion={emotion}, volume={volume} (rms={rms:.0f}), "
            f"pace={words_per_sec:.2f} w/s, last_heard={heard_sec:.1f}s ago"
        )
    if not rows:
        return "No active speaker context yet."
    return "Call organizer:\n" + "\n".join(rows)


def _call_has_wake_phrase(text: str) -> bool:
    t = " ".join((text or "").strip().lower().split())
    if not t:
        return False
    t = re.sub(r"[^a-z0-9\s']", " ", t)
    t = " ".join(t.split())
    for phrase in LUNA_CALL_WAKE_PHRASES:
        if not phrase:
            continue
        if phrase in t:
            return True
    # Fuzzy fallback for Whisper variants: "hey, luna", "heyluna", "hi luna?" etc.
    compact = t.replace(" ", "")
    if "luna" in compact:
        if re.search(r"\b(hey|hi|yo|ok|okay)\s*luna\b", t):
            return True
        if any(k in compact for k in ("heyluna", "hiluna", "yoluna", "okluna", "okayluna")):
            return True
        # Allow direct address "luna" when utterance is short (typically wake-style).
        words = re.findall(r"[a-z0-9']+", t)
        if 1 <= len(words) <= 4:
            return True
    return False


def _call_save_clip_wav(pcm: bytes, sample_rate: int, channels: int, sample_width: int, speaker_name: str) -> str | None:
    """Persist a VC PCM segment as a WAV clip for wake-word interactions."""
    if not pcm:
        return None
    safe_name = re.sub(r"[^a-zA-Z0-9._-]+", "_", (speaker_name or "speaker")).strip("_") or "speaker"
    clips_dir = os.path.join(_DATA, "call_clips")
    try:
        os.makedirs(clips_dir, exist_ok=True)
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
        out_path = os.path.join(clips_dir, f"{stamp}_{safe_name}.wav")
        with wave.open(out_path, "wb") as w:
            w.setnchannels(channels)
            w.setsampwidth(sample_width)
            w.setframerate(sample_rate)
            w.writeframes(pcm)
        return out_path
    except Exception:
        return None


def _pcm16_autogain_trim(pcm: bytes, channels: int, sample_rate: int) -> bytes:
    """
    Discord VC cleanup for STT:
    - light auto-gain for low-level captures
    - trim mostly-silent windows so Whisper focuses on voiced regions
    """
    if not pcm or channels <= 0 or sample_rate <= 0:
        return pcm
    try:
        frame_ms = 20
        samples_per_chan = int(sample_rate * frame_ms / 1000)
        frame_samples = samples_per_chan * channels
        frame_bytes = frame_samples * 2
        if frame_bytes <= 0 or len(pcm) < frame_bytes:
            return pcm

        # Measure frame RMS to estimate silence floor and speech level.
        frame_rms: list[float] = []
        for off in range(0, len(pcm) - frame_bytes + 1, frame_bytes):
            chunk = pcm[off: off + frame_bytes]
            frame_rms.append(_call_pcm_rms(chunk))
        if not frame_rms:
            return pcm
        s = sorted(frame_rms)
        p20 = s[int(max(0, min(len(s) - 1, round(0.20 * (len(s) - 1)))))]
        p80 = s[int(max(0, min(len(s) - 1, round(0.80 * (len(s) - 1)))))]
        # Dynamic threshold anchored above floor but below speech.
        thr = max(70.0, min(900.0, p20 + (p80 - p20) * 0.28))

        voiced = bytearray()
        max_abs = 1
        for off in range(0, len(pcm) - frame_bytes + 1, frame_bytes):
            chunk = pcm[off: off + frame_bytes]
            if _call_pcm_rms(chunk) >= thr:
                voiced.extend(chunk)
                for i in range(0, len(chunk), 2):
                    v = abs(struct.unpack_from("<h", chunk, i)[0])
                    if v > max_abs:
                        max_abs = v

        # If thresholding removed everything, fall back to original.
        base = bytes(voiced) if len(voiced) >= frame_bytes else pcm

        # Auto gain (cap to avoid distortion).
        target_peak = 14000.0
        gain = max(1.0, min(8.0, target_peak / max(1.0, float(max_abs))))
        if gain <= 1.05:
            return base
        out = bytearray(len(base))
        for i in range(0, len(base) - 1, 2):
            v = int(struct.unpack_from("<h", base, i)[0] * gain)
            if v > 32767:
                v = 32767
            elif v < -32768:
                v = -32768
            struct.pack_into("<h", out, i, v)
        return bytes(out)
    except Exception:
        return pcm


def _call_dbg(event: str, **fields) -> None:
    """Readable, structured terminal logs for Discord VC pipeline debugging."""
    if not LUNA_CALL_DEBUG:
        return
    try:
        ts = datetime.now().strftime("%H:%M:%S")
        if fields:
            parts = [f"{k}={fields[k]}" for k in sorted(fields)]
            print(f"[CallDebug {ts}] {event} | " + ", ".join(parts), flush=True)
        else:
            print(f"[CallDebug {ts}] {event}", flush=True)
    except Exception:
        pass


def _voice_recv_prepare_client():
    """Import voice-recv and harden decoder/logging for corrupted Opus packets."""
    try:
        from discord.ext import voice_recv
        VoiceRecvClient = voice_recv.VoiceRecvClient
    except ImportError:
        return None
    # RTCP sender reports are control packets and noisy at INFO.
    try:
        logging.getLogger("discord.ext.voice_recv.reader").setLevel(logging.WARNING)
    except Exception:
        pass
    # Guard router thread from Opus decode crashes.
    try:
        if not getattr(discord.opus.Decoder, "_luna_safe_decode_patch", False):
            _orig_decode = discord.opus.Decoder.decode

            def _safe_decode(self, data, fec=False):
                try:
                    return _orig_decode(self, data, fec=fec)
                except discord.opus.OpusError:
                    return b""

            discord.opus.Decoder.decode = _safe_decode  # type: ignore[assignment]
            setattr(discord.opus.Decoder, "_luna_safe_decode_patch", True)
    except Exception:
        pass
    return VoiceRecvClient


async def _discord_listen_once_transcribe(ctx) -> tuple[bool, str]:
    """
    One-shot VC listening flow for desktop users without Discord voice-message button.
    Captures a single utterance from command author, transcribes it, and returns text.
    """
    if not getattr(ctx, "guild", None):
        return False, "Use this in a server channel while you are in a voice channel."
    member = getattr(ctx, "author", None)
    if not member or not getattr(member, "voice", None) or not member.voice.channel:
        return False, "Join a voice channel first, then run **!listen**."
    target_channel = member.voice.channel
    speaker_id = int(getattr(member, "id", 0) or 0)
    if speaker_id <= 0:
        return False, "Could not resolve your Discord user in VC."
    VoiceRecvClient = _voice_recv_prepare_client()
    if VoiceRecvClient is None:
        return False, "Install `discord-ext-voice-recv` to use **!listen**."
    from discord.ext import voice_recv

    guild = ctx.guild
    vc = guild.voice_client
    try:
        if vc and vc.is_connected() and getattr(vc, "channel", None) != target_channel:
            await vc.move_to(target_channel)
        elif not vc or not vc.is_connected():
            vc = await target_channel.connect(cls=VoiceRecvClient, self_deaf=False, self_mute=False)
        elif not isinstance(vc, VoiceRecvClient):
            try:
                await vc.disconnect(force=True)
            except Exception:
                pass
            vc = await target_channel.connect(cls=VoiceRecvClient, self_deaf=False, self_mute=False)
    except Exception as e:
        return False, f"Voice connect failed: {e}"
    if not vc or not vc.is_connected():
        return False, "Could not connect to your voice channel."

    try:
        if hasattr(vc, "stop_listening"):
            vc.stop_listening()
    except Exception:
        pass

    buffer_lock = threading.Lock()
    captured_chunks: list[bytes] = []
    packet_sizes: list[int] = []
    first_audio_ts = 0.0
    last_audio_ts = 0.0
    packets = 0
    SAMPLE_RATE = 48000
    CHANNELS = 2
    SAMPLE_WIDTH = 2
    min_chunk_bytes = int(SAMPLE_RATE * SAMPLE_WIDTH * 0.22)

    class ListenSink(voice_recv.AudioSink):
        def wants_opus(self):
            return False

        def write(self, user, data):
            nonlocal first_audio_ts, last_audio_ts, packets
            if not user or int(getattr(user, "id", 0) or 0) != speaker_id:
                return
            pcm = getattr(data, "pcm", None)
            if not pcm:
                return
            now = time.time()
            if first_audio_ts <= 0.0:
                first_audio_ts = now
            last_audio_ts = now
            packets += 1
            with buffer_lock:
                captured_chunks.append(bytes(pcm))
                if len(packet_sizes) < 120:
                    packet_sizes.append(len(pcm))

        def cleanup(self):
            return

    sink = ListenSink()
    vc.listen(sink, after=lambda e: None)
    _call_dbg("listen_once_armed", speaker=speaker_id, timeout=f"{LUNA_LISTEN_TIMEOUT_SEC:.1f}")
    await ctx.reply("🎙 Listening now... say your message after this command.")

    start_ts = time.time()
    while time.time() - start_ts < LUNA_LISTEN_TIMEOUT_SEC:
        await asyncio.sleep(0.2)
        if first_audio_ts > 0.0:
            silence = time.time() - last_audio_ts
            spoke_for = time.time() - first_audio_ts
            speech_span = max(0.0, last_audio_ts - first_audio_ts)
            with buffer_lock:
                total_bytes = len(b"".join(captured_chunks))
            if (
                total_bytes >= min_chunk_bytes
                and packets >= LUNA_LISTEN_MIN_PACKETS
                and speech_span >= LUNA_LISTEN_MIN_SPEECH_SEC
                and silence >= LUNA_LISTEN_END_SILENCE_SEC
            ):
                _call_dbg(
                    "listen_once_end",
                    reason="silence",
                    silence=f"{silence:.2f}",
                    speech_span=f"{speech_span:.2f}",
                    bytes=total_bytes,
                    packets=packets,
                )
                break
            if spoke_for >= LUNA_LISTEN_MAX_SEC:
                _call_dbg("listen_once_end", reason="max_sec", spoke_for=f"{spoke_for:.2f}", bytes=total_bytes, packets=packets)
                break

    try:
        if hasattr(vc, "stop_listening"):
            vc.stop_listening()
    except Exception:
        pass

    with buffer_lock:
        pcm = b"".join(captured_chunks)
        sizes_copy = list(packet_sizes)
    if len(pcm) < min_chunk_bytes or packets < LUNA_LISTEN_MIN_PACKETS:
        _call_dbg("listen_once_no_audio", bytes=len(pcm), packets=packets, min_packets=LUNA_LISTEN_MIN_PACKETS)
        return False, "I didn't catch enough voice. Try **!listen** again and speak for 2-4 seconds."

    fd = tmp_wav = None
    tmp_mono = None
    tmp_wav_alt = None
    tmp_mono_alt = None
    try:
        pcm_clean = _pcm16_autogain_trim(pcm, CHANNELS, SAMPLE_RATE)
        if pcm_clean and len(pcm_clean) > 0:
            _call_dbg("listen_once_pcm_clean", in_bytes=len(pcm), out_bytes=len(pcm_clean))
            pcm = pcm_clean
        # voice_recv can output mono OR stereo depending on decoder transport.
        # Infer channels from packet size to avoid corrupt WAV framing.
        inferred_channels = 2
        if sizes_copy:
            avg_pkt = sum(sizes_copy) / max(1, len(sizes_copy))
            if avg_pkt < 3000:
                inferred_channels = 1
        _call_dbg("listen_once_format", channels=inferred_channels, avg_packet_bytes=f"{(sum(sizes_copy)/max(1,len(sizes_copy)) if sizes_copy else 0):.1f}")
        fd, tmp_wav = tempfile.mkstemp(suffix=".wav")
        os.close(fd)
        fd = None
        with wave.open(tmp_wav, "wb") as w:
            w.setnchannels(inferred_channels)
            w.setsampwidth(SAMPLE_WIDTH)
            w.setframerate(SAMPLE_RATE)
            w.writeframes(pcm)
        # Normalize to 16k mono for VAD/STT robustness on noisy PC captures.
        fd2, tmp_mono = tempfile.mkstemp(suffix=".wav")
        os.close(fd2)
        if not await asyncio.to_thread(_ffmpeg_to_wav16_mono_voice, tmp_wav, tmp_mono):
            try:
                if os.path.isfile(tmp_mono):
                    os.unlink(tmp_mono)
            except Exception:
                pass
            tmp_mono = None
        source_wav = tmp_mono or tmp_wav
        # Hub-style recognition path: Whisper-first (no Silero hard gate).
        _call_dbg("listen_once_hub_stt", bytes=len(pcm))
        text = await asyncio.to_thread(_whisper_transcribe, source_wav)
        transcript = (text or "").strip()
        if not transcript:
            # Retry 1: relaxed Whisper thresholds on same normalized audio.
            _call_dbg("listen_once_retry_relaxed")
            text_relaxed = await asyncio.to_thread(_whisper_transcribe_relaxed, source_wav)
            transcript = (text_relaxed or "").strip()
        if not transcript:
            # Retry 2: alternate channel decode (mono<->stereo) then transcribe again.
            alt_channels = 1 if inferred_channels == 2 else 2
            _call_dbg("listen_once_retry_alt_channels", from_ch=inferred_channels, to_ch=alt_channels)
            fd3, tmp_wav_alt = tempfile.mkstemp(suffix=".wav")
            os.close(fd3)
            with wave.open(tmp_wav_alt, "wb") as w:
                w.setnchannels(alt_channels)
                w.setsampwidth(SAMPLE_WIDTH)
                w.setframerate(SAMPLE_RATE)
                w.writeframes(pcm)
            fd4, tmp_mono_alt = tempfile.mkstemp(suffix=".wav")
            os.close(fd4)
            if not await asyncio.to_thread(_ffmpeg_to_wav16_mono_voice, tmp_wav_alt, tmp_mono_alt):
                try:
                    if os.path.isfile(tmp_mono_alt):
                        os.unlink(tmp_mono_alt)
                except Exception:
                    pass
                tmp_mono_alt = None
            alt_source = tmp_mono_alt or tmp_wav_alt
            text_alt = await asyncio.to_thread(_whisper_transcribe_relaxed, alt_source)
            transcript = (text_alt or "").strip()
        if not transcript:
            _call_dbg("listen_once_empty_transcript")
            return False, "I heard audio but couldn't transcribe words. Try again clearly."
        _call_dbg("listen_once_transcribed", chars=len(transcript), text=transcript[:120].replace("\n", " "))
        return True, transcript
    except Exception:
        return False, "Listen capture failed."
    finally:
        try:
            if fd is not None:
                os.close(fd)
        except Exception:
            pass
        try:
            if tmp_wav and os.path.isfile(tmp_wav):
                os.unlink(tmp_wav)
        except Exception:
            pass
        try:
            if tmp_mono and os.path.isfile(tmp_mono):
                os.unlink(tmp_mono)
        except Exception:
            pass
        try:
            if tmp_wav_alt and os.path.isfile(tmp_wav_alt):
                os.unlink(tmp_wav_alt)
        except Exception:
            pass
        try:
            if tmp_mono_alt and os.path.isfile(tmp_mono_alt):
                os.unlink(tmp_mono_alt)
        except Exception:
            pass


async def _run_discord_call_bot(ctx, contact: str) -> tuple[bool, str]:
    """
    From Discord: resolve user by ID or username, join their voice channel with voice receive,
    transcribe what they say and answer with TTS.
    """
    contact = (contact or "").strip()
    if not contact:
        return False, "Usage: !call <Discord username or user ID>"
    target_user = None
    if contact.isdigit() and len(contact) >= 17:
        try:
            target_user = await bot.fetch_user(int(contact))
        except Exception:
            pass
    if not target_user and ctx.guild:
        target_user = ctx.guild.get_member_named(contact)
    if not target_user:
        for g in bot.guilds:
            m = g.get_member_named(contact)
            if m:
                target_user = m
                break
    if not target_user and contact.isdigit():
        for g in bot.guilds:
            m = g.get_member(int(contact))
            if m:
                target_user = m
                break
    if not target_user:
        return False, f"Could not find user **{contact}**. Use Discord username or numeric user ID (e.g. 1414944231222411378)."
    # Find a guild where target is in a voice channel
    target_channel = None
    for g in bot.guilds:
        m = g.get_member(target_user.id)
        if m and m.voice and m.voice.channel:
            target_channel = m.voice.channel
            break
    if not target_channel:
        name = getattr(target_user, "display_name", None) or getattr(target_user, "name", "user")
        return False, f"**{name}** is not in a voice channel on any shared server. Ask them to join one, then try **!call** again."
    # Use VoiceRecvClient if available for transcribe + TTS
    VoiceRecvClient = _voice_recv_prepare_client()
    if VoiceRecvClient is None:
        try:
            await target_channel.connect(self_deaf=False, self_mute=False)
            name = getattr(target_user, "display_name", None) or getattr(target_user, "name", "user")
            await ctx.reply(f"Joined **{target_channel.name}** with **{name}**. Install `discord-ext-voice-recv` for transcribe + TTS.")
        except Exception as e:
            return False, str(e)
        return True, ""
    from discord.ext import voice_recv
    # Connect with voice receive
    try:
        vc = await target_channel.connect(cls=VoiceRecvClient, self_deaf=False, self_mute=False)
    except Exception as e:
        return False, f"Failed to join voice: {e}"
    _call_dbg(
        "vc_joined",
        channel=getattr(target_channel, "name", "unknown"),
        guild=getattr(getattr(target_channel, "guild", None), "name", "unknown"),
        silero=int(LUNA_CALL_USE_SILERO_VAD),
        wake_mode=int(LUNA_CALL_WAKE_MODE),
        wake_arm_sec=f"{LUNA_CALL_WAKE_ARM_SEC:.1f}",
        reply_silence_sec=f"{LUNA_CALL_REPLY_SILENCE_SEC:.2f}",
        max_segment_sec=f"{LUNA_CALL_MAX_SEGMENT_SEC:.2f}",
    )
    # Sink that buffers all non-bot speakers in the call and runs organizer analysis per speaker.
    speaker_buffers: dict[int, list[bytes]] = {}
    speaker_state: dict[int, dict] = {}
    speaker_last_reply_ts: dict[int, float] = {}
    speaker_last_notice_ts: dict[int, float] = {}
    speaker_wake_until_ts: dict[int, float] = {}
    last_voice_packet_ts = time.time()
    last_noaudio_warn_ts = 0.0
    last_stats_log_ts = 0.0
    first_audio_notice_sent = False
    packets_in = 0
    pcm_bytes_in = 0
    segments_flushed = 0
    segments_vad_reject = 0
    segments_nl_reject = 0
    replies_sent = 0
    buffer_lock = threading.Lock()
    stop_event = threading.Event()
    SAMPLE_RATE = 48000
    CHANNELS = 2
    SAMPLE_WIDTH = 2  # 16-bit

    class CallSink(voice_recv.AudioSink):
        def wants_opus(self):
            return False
        def write(self, user, data):
            if not user or getattr(user, "bot", False) or not data:
                return
            pcm = getattr(data, "pcm", None)
            if pcm is None:
                return
            if len(pcm) <= 0:
                return
            uid = int(getattr(user, "id", 0) or 0)
            if uid <= 0:
                return
            nonlocal last_voice_packet_ts
            nonlocal first_audio_notice_sent
            nonlocal packets_in, pcm_bytes_in
            last_voice_packet_ts = time.time()
            packets_in += 1
            pcm_bytes_in += len(pcm)
            with buffer_lock:
                speaker_buffers.setdefault(uid, []).append(bytes(pcm))
                st = speaker_state.setdefault(uid, {})
                st["name"] = getattr(user, "display_name", None) or getattr(user, "name", f"user-{uid}")
                if not st.get("segment_start_ts"):
                    st["segment_start_ts"] = time.time()
                st["last_ts"] = time.time()
            if not first_audio_notice_sent:
                first_audio_notice_sent = True
                try:
                    bot.loop.create_task(
                        ctx.reply("✅ Receiving decoded voice audio (PCM) from VC.")
                    )
                except Exception:
                    pass
                _call_dbg("first_pcm_received", speaker=uid, pcm_bytes=len(pcm))
        def cleanup(self):
            stop_event.set()

    sink = CallSink()
    vc.listen(sink, after=lambda e: stop_event.set())

    async def processor_loop():
        nonlocal last_noaudio_warn_ts
        nonlocal last_stats_log_ts
        nonlocal segments_flushed, segments_vad_reject, segments_nl_reject, replies_sent
        # Use a lower threshold so short speech bursts still get processed.
        # Voice-recv may produce mono or stereo PCM depending on transport/decoder behavior.
        # Isolation often trims phoneme edges; accept shorter chunks to preserve intent.
        min_chunk_bytes = int(SAMPLE_RATE * SAMPLE_WIDTH * 0.24)  # ~240ms in mono baseline
        _call_dbg(
            "processor_loop_started",
            min_chunk_bytes=min_chunk_bytes,
            wake_mode=int(LUNA_CALL_WAKE_MODE),
            wake_phrases="|".join(LUNA_CALL_WAKE_PHRASES[:5]),
        )
        while not stop_event.is_set() and vc.is_connected():
            await asyncio.sleep(1.0)
            if stop_event.is_set():
                break
            now_loop = time.time()
            if now_loop - last_stats_log_ts >= LUNA_CALL_DEBUG_STATS_SEC:
                last_stats_log_ts = now_loop
                with buffer_lock:
                    active_speakers = len(speaker_buffers)
                    queued_bytes = sum(len(b"".join(chunks)) for chunks in speaker_buffers.values() if chunks)
                _call_dbg(
                    "stats",
                    active_speakers=active_speakers,
                    queued_bytes=queued_bytes,
                    packets_in=packets_in,
                    pcm_kb=f"{pcm_bytes_in/1024.0:.1f}",
                    flushed=segments_flushed,
                    vad_reject=segments_vad_reject,
                    nl_reject=segments_nl_reject,
                    replies_sent=replies_sent,
                )
            if now_loop - last_voice_packet_ts > 8.0 and now_loop - last_noaudio_warn_ts > 15.0:
                last_noaudio_warn_ts = now_loop
                _call_dbg("no_audio_packets_timeout", seconds_since_last=f"{now_loop - last_voice_packet_ts:.1f}")
                try:
                    await ctx.reply(
                        "⚠ I joined VC but I am not receiving voice audio packets yet. "
                        "Please check mic mute/voice activity and speak for 2-3 seconds."
                    )
                except Exception:
                    pass
            with buffer_lock:
                if not speaker_buffers:
                    continue
                pending: list[tuple[int, bytes]] = []
                for uid, chunks in list(speaker_buffers.items()):
                    if not chunks:
                        continue
                    joined = b"".join(chunks)
                    st = speaker_state.setdefault(uid, {})
                    last_ts = float(st.get("last_ts", 0.0) or 0.0)
                    seg_start_ts = float(st.get("segment_start_ts", last_ts) or last_ts or now_loop)
                    silence_elapsed = max(0.0, now_loop - last_ts)
                    segment_elapsed = max(0.0, now_loop - seg_start_ts)
                    # Keep accumulating sub-second speech instead of dropping it each loop.
                    if len(joined) < min_chunk_bytes:
                        continue
                    # Reply only after speaker has paused for a brief timeout.
                    # If speech is continuous for too long, force a flush as a safety cap.
                    if (
                        silence_elapsed < LUNA_CALL_REPLY_SILENCE_SEC
                        and segment_elapsed < LUNA_CALL_MAX_SEGMENT_SEC
                    ):
                        continue
                    flush_reason = "silence_timeout" if silence_elapsed >= LUNA_CALL_REPLY_SILENCE_SEC else "max_segment"
                    pending.append((uid, joined))
                    speaker_buffers[uid] = []
                    st["segment_start_ts"] = 0.0
                    segments_flushed += 1
                    _call_dbg(
                        "segment_flushed",
                        speaker=uid,
                        bytes=len(joined),
                        reason=flush_reason,
                        silence_sec=f"{silence_elapsed:.2f}",
                        segment_sec=f"{segment_elapsed:.2f}",
                    )
            for uid, chunks in pending:
                if len(chunks) < min_chunk_bytes:
                    continue
                tmp = None
                try:
                    fd, wav_path = tempfile.mkstemp(suffix=".wav")
                    os.close(fd)
                    tmp = wav_path
                    with wave.open(wav_path, "wb") as w:
                        w.setnchannels(CHANNELS)
                        w.setsampwidth(SAMPLE_WIDTH)
                        w.setframerate(SAMPLE_RATE)
                        w.writeframes(chunks)
                    has_speech = await asyncio.to_thread(_call_silero_has_speech, wav_path)
                    _call_dbg("silero_gate", speaker=uid, pass_gate=int(has_speech), bytes=len(chunks))
                    if not has_speech:
                        segments_vad_reject += 1
                        continue
                    text_in = await asyncio.to_thread(_whisper_transcribe, wav_path)
                    if not text_in or not text_in.strip():
                        _call_dbg("whisper_empty", speaker=uid)
                        continue

                    transcript = text_in.strip()
                    with buffer_lock:
                        st = speaker_state.setdefault(uid, {})
                        speaker_name = str(st.get("name") or f"user-{uid}")
                    _call_dbg("transcribed", speaker=speaker_name, chars=len(transcript), text=transcript[:110].replace("\n", " "))
                    # Always show raw transcription so VC tests can verify STT end-to-end.
                    try:
                        await ctx.reply(f"📝 Transcribed **{speaker_name}**: {transcript[:180]}")
                    except Exception:
                        pass
                    now_ts = time.time()
                    wake_hit = _call_has_wake_phrase(transcript)
                    wake_armed = now_ts <= float(speaker_wake_until_ts.get(uid, 0.0))
                    if LUNA_CALL_WAKE_MODE:
                        if wake_hit:
                            speaker_wake_until_ts[uid] = now_ts + LUNA_CALL_WAKE_ARM_SEC
                            wake_armed = True
                            _call_dbg("wake_detected", speaker=speaker_name, arm_sec=f"{LUNA_CALL_WAKE_ARM_SEC:.1f}")
                            try:
                                await ctx.reply(f"🎙 Wake phrase detected from **{speaker_name}**. Recording a clip.")
                            except Exception:
                                pass
                        if not wake_armed:
                            _call_dbg("wake_gate_skip", speaker=speaker_name)
                            continue
                        clip_path = _call_save_clip_wav(chunks, SAMPLE_RATE, CHANNELS, SAMPLE_WIDTH, speaker_name)
                        if clip_path:
                            _call_dbg("clip_saved", speaker=speaker_name, path=clip_path)
                            try:
                                await ctx.reply(f"📼 Saved wake clip: `{os.path.basename(clip_path)}`")
                            except Exception:
                                pass
                    rms = _call_pcm_rms(chunks)
                    natural = _call_is_natural_language(transcript, rms)
                    _call_dbg("language_gate", speaker=speaker_name, pass_gate=int(natural), rms=f"{rms:.0f}")
                    if not natural:
                        segments_nl_reject += 1
                        try:
                            await ctx.reply("🔇 Ignored for reply (not natural-language speech).")
                        except Exception:
                            pass
                        continue
                    volume_label = _call_volume_label(rms)
                    dur_sec = max(0.01, len(chunks) / float(SAMPLE_RATE * SAMPLE_WIDTH * CHANNELS))
                    words = len(re.findall(r"[A-Za-z0-9']+", transcript))
                    words_per_sec = words / dur_sec
                    emotion = _call_detect_emotion(transcript, rms)
                    emo_meta = await asyncio.to_thread(_analyze_voice_clip_emotion, wav_path, transcript)
                    model_conf = None
                    model_name = None
                    if isinstance(emo_meta, dict):
                        emotion = str(emo_meta.get("emotion") or emotion)
                        volume_label = str(emo_meta.get("volume") or volume_label)
                        model_conf = emo_meta.get("model_confidence")
                        model_name = emo_meta.get("model")

                    with buffer_lock:
                        st = speaker_state.setdefault(uid, {})
                        st["rms"] = rms
                        st["volume_label"] = volume_label
                        st["emotion"] = emotion
                        st["words_per_sec"] = words_per_sec
                        st["last_ts"] = time.time()
                        organizer_ctx = _call_organizer_snapshot(speaker_state, focus_uid=uid)
                    # Hearing notice in text channel (throttled), so testing in VC shows live detection.
                    now_notice = time.time()
                    if now_notice - float(speaker_last_notice_ts.get(uid, 0.0)) >= 3.0:
                        speaker_last_notice_ts[uid] = now_notice
                        try:
                            await ctx.reply(
                                f"👂 Heard **{speaker_name}** ({volume_label}, {emotion}): "
                                f"{transcript[:120]}"
                            )
                        except Exception:
                            pass

                    # Prevent spamming replies from one speaker nonstop.
                    if now_ts - float(speaker_last_reply_ts.get(uid, 0.0)) < 2.4:
                        _call_dbg("reply_throttled", speaker=speaker_name, cooldown_left=f"{2.4 - (now_ts - float(speaker_last_reply_ts.get(uid, 0.0))):.2f}")
                        continue
                    speaker_last_reply_ts[uid] = now_ts

                    emotion_meta_lines = ""
                    if model_conf is not None:
                        emotion_meta_lines += f"Emotion model confidence: {float(model_conf):.2f}\n"
                    if model_name:
                        emotion_meta_lines += f"Emotion model: {model_name}\n"
                    prompt = (
                        f"[Discord call speaker]\n"
                        f"Speaker: {speaker_name}\n"
                        f"Transcript: {transcript}\n"
                        f"Detected volume: {volume_label} (rms {rms:.0f})\n"
                        f"Detected emotion: {emotion}\n"
                        f"{emotion_meta_lines}"
                        f"Speech pace: {words_per_sec:.2f} words/sec\n\n"
                        f"{organizer_ctx}\n\n"
                        "Respond naturally to the current speaker in 1-2 short spoken sentences. "
                        "Use the organizer context to avoid confusion about who is speaking and their emotional tone."
                    )
                    reply = await asyncio.to_thread(
                        ollama_chat,
                        prompt,
                        "You are Luna in a live multi-person Discord call. Keep replies concise, socially aware, and context-sensitive.",
                    )
                    if reply and reply.strip():
                        _call_dbg("reply_tts_start", speaker=speaker_name, chars=len(reply.strip()))
                        await asyncio.to_thread(_play_tts_in_vc_sync, vc, reply.strip())
                        replies_sent += 1
                        if LUNA_CALL_WAKE_MODE:
                            speaker_wake_until_ts[uid] = 0.0
                        _call_dbg("reply_tts_done", speaker=speaker_name)
                except Exception:
                    _call_dbg("processor_exception", speaker=uid)
                    pass
                finally:
                    if tmp and os.path.isfile(tmp):
                        try: os.unlink(tmp)
                        except Exception: pass

    asyncio.create_task(processor_loop())
    try:
        with _call_session_lock:
            global _call_session
            _call_session = {
                "guild_id": int(getattr(target_channel.guild, "id", 0) or 0),
                "channel_id": int(getattr(target_channel, "id", 0) or 0),
                "started_at": time.time(),
                "mode": "multi_speaker_organizer",
            }
    except Exception:
        pass
    await ctx.reply(
        f"In call on **{target_channel.name}** — listening to everyone in channel.\n"
        f"Organizer enabled: per-speaker transcript + volume + emotion context.\n"
        f"Speech gate: **{'Silero VAD' if LUNA_CALL_USE_SILERO_VAD else 'heuristic only'}**.\n"
        f"Wake mode: **{'on' if LUNA_CALL_WAKE_MODE else 'off'}** (phrase: `hey luna`)."
    )
    return True, ""


def _run_wa_call(contact: str) -> tuple[bool, str]:
    # Try WhatsApp Desktop first
    if sys.platform == "win32":
        try:
            from pywinauto import Application
            app = None
            try: app = Application(backend="uia").connect(title_re=".*WhatsApp.*", timeout=3)
            except Exception: pass
            if not app:
                subprocess.Popen(["powershell","-NoProfile","-Command","Start-Process WhatsApp"],
                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                    creationflags=subprocess.CREATE_NO_WINDOW)
                time.sleep(4)
                try: app = Application(backend="uia").connect(title_re=".*WhatsApp.*", timeout=15)
                except Exception: pass
            if app:
                win = app.window(title_re=".*WhatsApp.*")
                win.restore(); win.set_focus(); time.sleep(0.5)
                try:
                    s = win.child_window(control_type="Edit")
                    s.set_edit_text(""); s.type_keys(contact, with_spaces=True)
                    time.sleep(1.2); win.type_keys("{ENTER}"); time.sleep(1)
                    for _ in range(20):
                        time.sleep(0.5)
                        for name in ("Call","Voice call","Phone"):
                            try:
                                btn = win.child_window(title_re=f".*{name}.*", control_type="Button")
                                if btn.exists(timeout=0): btn.click_input(); time.sleep(1); break
                            except Exception: pass
                        else: continue
                        break
                    return True, f"Calling **{contact}** in WhatsApp Desktop."
                except Exception: pass
        except ImportError: pass
    # Fallback to web
    ok, msg = _run_wa_msg(contact)
    return ok, msg + " (Voice call requires WhatsApp Desktop on Windows)"

def _fb_needs_pin(page) -> bool:
    """Detect if Facebook is showing a PIN, verification code, or identity challenge.
    Facebook typically shows these as a popup/dialog overlay, not a page redirect."""
    try:
        url_low = (page.url or "").lower()
        if "checkpoint" in url_low or "two_step_verification" in url_low:
            return True

        # Check visible dialog/popup text (Facebook uses role="dialog" overlays)
        pin_keywords = [
            "enter the code", "enter the login code", "enter your code",
            "verification code", "confirm your identity", "we sent a code",
            "two-factor", "approve your login", "security code", "pin code",
            "enter the 6-digit", "enter the 8-digit", "login code",
            "check your notifications", "we noticed a login",
            "enter the number", "code we sent", "code to continue",
            "review recent login", "approve this login",
        ]
        # Check dialogs / popups first (most common for PIN)
        for sel in ['[role="dialog"]', '[aria-modal="true"]', '.overlay', '[data-testid="dialog"]']:
            try:
                dialogs = page.locator(sel)
                for i in range(min(dialogs.count(), 3)):
                    d = dialogs.nth(i)
                    if d.is_visible():
                        text = (d.inner_text() or "").lower()[:1000]
                        if any(kw in text for kw in pin_keywords):
                            return True
            except Exception:
                continue

        # Also check for visible numeric input fields (PIN entry boxes)
        try:
            pin_inputs = page.locator('input[type="number"], input[type="tel"], input[inputmode="numeric"]')
            for i in range(min(pin_inputs.count(), 5)):
                inp = pin_inputs.nth(i)
                if inp.is_visible():
                    parent_text = ""
                    try:
                        parent_text = (inp.locator("xpath=ancestor::div[position()<=5]").last.inner_text() or "").lower()[:500]
                    except Exception:
                        pass
                    if any(kw in parent_text for kw in pin_keywords):
                        return True
        except Exception:
            pass

        # Fallback: full page content scan
        content = (page.content() or "")[:8000].lower()
        if any(kw in content for kw in pin_keywords):
            return True

        return False
    except Exception:
        return False

def _keep_browser_until_closed(pw, context):
    """Hold Playwright + browser alive in a background thread until the user closes all tabs."""
    def _hold():
        try:
            # Persistent launch can briefly report zero pages; don't close immediately or login race tears down Chrome.
            for _ in range(120):
                try:
                    if context.pages:
                        break
                except Exception:
                    break
                time.sleep(0.25)
            while True:
                try:
                    pgs = context.pages
                except Exception:
                    break
                if not pgs:
                    break
                if not any(not pg.is_closed() for pg in pgs):
                    break
                time.sleep(1)
        except Exception:
            pass
        finally:
            try: context.close()
            except Exception: pass
            try: pw.stop()
            except Exception: pass
    threading.Thread(target=_hold, daemon=True).start()

def _run_messenger_msg(username: str, message: str = "") -> tuple[bool, str]:
    """Send a Messenger message by searching for the user in facebook.com/messages/new.
    Accepts display names, usernames, or any searchable identifier — Facebook search finds them."""
    search_name = username.strip()
    if not search_name or len(search_name) < 2:
        return False, "Invalid Messenger recipient — provide a name or username."
    raw_msg = (message or "").strip()
    if raw_msg:
        msg_text = _rephrase_dm(raw_msg, "Facebook Messenger", search_name) or raw_msg
    else:
        msg_text = _default_wa_msg()
    if not _fb_lock.acquire(blocking=False):
        return False, "Facebook/Messenger already in use (share or another message). Try again shortly."
    pw = None
    context = None
    handed_off = False
    try:
        from playwright.sync_api import sync_playwright
        os.makedirs(FB_PROFILE_DIR, exist_ok=True)
        pw = sync_playwright().start()
        try:
            context = _launch_social_browser_with_retry(FB_PROFILE_DIR, pw, attempts=4)
        except Exception as launch_err:
            return False, f"Browser failed. Close any Facebook/Chrome window using this profile and try again. {launch_err}"
        page = _acquire_live_page(context)

        page.goto(f"https://www.facebook.com/search/people/?q={search_name}", wait_until="domcontentloaded", timeout=60000)
        page.wait_for_timeout(3000)

        saw_cap, cap_note = _playwright_granite_captcha_observer(page)
        if saw_cap:
            _clear_ready(FB_PROFILE_DIR)
            handed_off = True
            _keep_browser_until_closed(pw, context)
            return False, (
                "**Captcha or challenge detected** (Granite vision). "
                + (cap_note or "Complete the check in the open browser, then close it and try again.")
            )

        if "login" in page.url.lower() or "facebook.com/login" in page.url:
            _clear_ready(FB_PROFILE_DIR)
            handed_off = True
            _keep_browser_until_closed(pw, context)
            return False, "Facebook needs login. I left the browser open — log in, then **close the browser** and try again."

        if _fb_needs_pin(page):
            handed_off = True
            _keep_browser_until_closed(pw, context)
            return False, (
                "Facebook is asking for a **PIN or verification code**. "
                "I left the browser open — enter the code, then **close the browser** and try again."
            )
        _mark_ready(FB_PROFILE_DIR)

        profile_link = None
        for sel in [
            'a[role="presentation"][href*="facebook.com/"]',
            'a[href*="facebook.com/"]:has(span)',
            '[role="article"] a[href*="facebook.com/"]',
            'div[role="feed"] a[href*="/"]',
        ]:
            try:
                links = page.locator(sel)
                for i in range(min(links.count(), 8)):
                    lnk = links.nth(i)
                    href = (lnk.get_attribute("href") or "")
                    if lnk.is_visible() and "/search/" not in href and "/policies" not in href:
                        profile_link = lnk
                        break
                if profile_link:
                    break
            except Exception:
                continue

        if not profile_link:
            return False, f"Could not find **{search_name}** on Facebook. Make sure the name matches their profile."

        profile_link.click(timeout=5000)
        page.wait_for_load_state("domcontentloaded", timeout=30000)
        page.wait_for_timeout(5000)

        msg_clicked = False
        for attempt in range(4):
            # Method 1: Playwright get_by_role with exact name
            if not msg_clicked:
                try:
                    btn = page.get_by_role("button", name="Message", exact=True)
                    if btn.count() and btn.first.is_visible():
                        btn.first.click(timeout=5000)
                        msg_clicked = True
                except Exception:
                    pass

            # Method 2: aria-label
            if not msg_clicked:
                for sel in ['[aria-label="Message"]', '[aria-label="Message "]']:
                    try:
                        loc = page.locator(sel).first
                        if loc.count() and loc.is_visible():
                            loc.click(timeout=5000)
                            msg_clicked = True
                            break
                    except Exception:
                        continue

            # Method 3: link to messages
            if not msg_clicked:
                try:
                    loc = page.locator('a[href*="/messages/t/"]').first
                    if loc.count() and loc.is_visible():
                        loc.click(timeout=5000)
                        msg_clicked = True
                except Exception:
                    pass

            # Method 4: JS — find the visible span with exact text "Message" and click its closest button/link ancestor
            if not msg_clicked:
                try:
                    msg_clicked = page.evaluate("""() => {
                        const spans = document.querySelectorAll('span');
                        for (const s of spans) {
                            if (s.textContent.trim() !== 'Message') continue;
                            if (!s.offsetParent) continue;
                            const rect = s.getBoundingClientRect();
                            if (rect.width === 0 || rect.height === 0) continue;
                            let el = s;
                            while (el) {
                                const role = el.getAttribute && el.getAttribute('role');
                                const tag = el.tagName && el.tagName.toLowerCase();
                                if (role === 'button' || role === 'link' || tag === 'a' || tag === 'button') {
                                    el.click();
                                    return true;
                                }
                                el = el.parentElement;
                            }
                            s.click();
                            return true;
                        }
                        return false;
                    }""")
                except Exception:
                    msg_clicked = False

            if msg_clicked:
                break
            page.wait_for_timeout(3000)

        if not msg_clicked:
            return False, f"Found **{search_name}**'s profile but couldn't find the Message button."

        page.wait_for_timeout(5000)

        editor = None
        for _ed_try in range(5):
            editor = _find_fb_message_editor(page)
            if editor:
                break
            page.wait_for_timeout(2500)
        if not editor:
            return False, "Opened the chat but couldn't find the Aa message box. Try sending manually."

        editor.click(force=True)
        page.wait_for_timeout(500)
        page.wait_for_timeout(200)
        page.keyboard.type(msg_text, delay=25)
        page.wait_for_timeout(500)
        page.keyboard.press("Enter")
        page.wait_for_timeout(4000)

        target_slug = re.sub(r"[^a-zA-Z0-9._\-]", "", search_name.replace(" ", ".").lower())
        _record_recent_social("facebook", search_name, f"{FACEBOOK_HOME.rstrip('/')}/{target_slug}")
        return True, f'Messenger: sent to **{search_name}**: "{msg_text[:50]}{"…" if len(msg_text) > 50 else ""}"'
    except Exception as e:
        return False, f"Messenger error: {e}"
    finally:
        if not handed_off:
            if context:
                try: context.close()
                except Exception: pass
            if pw:
                try: pw.stop()
                except Exception: pass
        try: _fb_lock.release()
        except Exception: pass

def _find_fb_message_editor(page):
    """Find the Messenger chat popup's 'Aa' input. Rejects comment boxes and post composers."""
    try:
        handle = page.evaluate_handle("""() => {
            const dominated = ['comment', 'write a comment', 'write a public comment',
                               'write something', 'reply', 'write an answer'];

            function isCommentBox(el) {
                const label = (el.getAttribute('aria-label') || '').toLowerCase();
                const ph = (el.getAttribute('data-placeholder') || el.getAttribute('placeholder') || '').toLowerCase();
                for (const bad of dominated) {
                    if (label.includes(bad) || ph.includes(bad)) return true;
                }
                return false;
            }

            // Priority 1: element with placeholder="Aa" (the Messenger signature)
            for (const el of document.querySelectorAll('[data-placeholder="Aa"], [aria-placeholder="Aa"], p[data-placeholder="Aa"]')) {
                if (!el.offsetParent) continue;
                if (isCommentBox(el)) continue;
                const target = el.closest('[contenteditable="true"]') || el;
                if (target.offsetParent) return target;
            }

            // Priority 2: contenteditable with aria-label="Message"
            for (const el of document.querySelectorAll('[contenteditable="true"]')) {
                if (!el.offsetParent) continue;
                const label = (el.getAttribute('aria-label') || '').trim();
                if (label === 'Message' || label === 'Message ') return el;
            }

            // Priority 3: contenteditable inside a Messenger-style chat popup
            // (bottom-of-page fixed container, not a post/comment area)
            for (const el of document.querySelectorAll('[contenteditable="true"]')) {
                if (!el.offsetParent || isCommentBox(el)) continue;
                const rect = el.getBoundingClientRect();
                // Messenger popups sit near the bottom of the viewport
                if (rect.bottom > window.innerHeight - 150 && rect.height < 200) {
                    const ph = el.getAttribute('data-placeholder') || el.getAttribute('aria-placeholder') || '';
                    const label = el.getAttribute('aria-label') || '';
                    // Must NOT look like a post composer or comment
                    if (!dominated.some(b => label.toLowerCase().includes(b)))
                        return el;
                }
            }

            return null;
        }""")
        if handle:
            el = handle.as_element()
            if el:
                return el
    except Exception:
        pass
    return None

# ── PC vitals, Luna vitals, website analysis ───────────────────────────────────

def _pc_vitals() -> str:
    """Return a short summary of PC CPU, RAM, disk (on request only)."""
    try:
        import psutil
        cpu = psutil.cpu_percent(interval=1)
        mem = psutil.virtual_memory()
        ram_pct = mem.percent
        ram_gb = mem.used / (1024 ** 3)
        ram_total_gb = mem.total / (1024 ** 3)
        disks = []
        for part in psutil.disk_partitions():
            if "fixed" in part.opts or (sys.platform == "win32" and "cdrom" not in part.opts.lower()):
                try:
                    usage = psutil.disk_usage(part.mountpoint)
                    disks.append(f"{part.mountpoint} {usage.percent}%")
                except Exception:
                    pass
        disk_str = ", ".join(disks[:4]) if disks else "—"
        msg = f"**PC vitals** — CPU {cpu}%, RAM {ram_pct}% ({ram_gb:.1f}/{ram_total_gb:.1f} GB), Disk: {disk_str}."
        if ram_pct > 90:
            msg += " RAM is high — closing some apps might help."
        elif cpu > 90:
            msg += " CPU is high — something may be busy."
        return msg
    except ImportError:
        return "Install **psutil** to see PC vitals: `pip install psutil`"
    except Exception as e:
        return f"Could not read PC vitals: {e}"

def _luna_vitals() -> str:
    """Return Luna's own status: process memory, Ollama, uptime (so you know the program is doing what it should)."""
    parts = []
    try:
        import psutil
        proc = psutil.Process(os.getpid())
        mem = proc.memory_info()
        rss_mb = mem.rss / (1024 * 1024)
        parts.append(f"**Process:** {rss_mb:.0f} MB RAM")
    except Exception:
        parts.append("**Process:** —")
    try:
        req = urllib.request.Request(f"{OLLAMA_BASE}/api/tags", method="GET")
        with urllib.request.urlopen(req, timeout=3) as r:
            parts.append("**Ollama:** online")
    except Exception:
        parts.append("**Ollama:** offline or unreachable")
    uptime_sec = time.time() - _luna_start_time
    uptime_m = int(uptime_sec // 60)
    uptime_h = uptime_m // 60
    uptime_m = uptime_m % 60
    if uptime_h > 0:
        parts.append(f"**Uptime:** {uptime_h}h {uptime_m}m")
    else:
        parts.append(f"**Uptime:** {uptime_m}m")
    return " **·** ".join(parts)

# ── Camera (object + face recognition) ────────────────────────────────────────
# Requires: opencv-python (pip install opencv-python). Optional: ultralytics for object detection (pip install ultralytics).
# When OLLAMA_VISION_MODEL is set (default: granite3.2-vision), the camera view is described by that vision model.

_camera_last_result: dict | None = None
_camera_last_image_bytes: bytes | None = None  # for camera chat (Granite thread)
_camera_chat_history: list[dict] = []  # [{role, content}, ...] separate thread with vision model
_camera_chat_lock = threading.Lock()
_camera_lock = threading.Lock()
_last_web_vision_context: dict | None = None  # {summary, source, ts} — latest /api/chat vision; persists for LUNA_VISION_CONTEXT_TTL_SEC
_vision_infer_lock = threading.Lock()
_vision_last_error = ""


def _vision_provider_describe_image_once(image_bytes: bytes, prompt: str, timeout: int) -> dict:
    provider = (LUNA_VISION_PROVIDER or "ollama").strip().lower()
    if provider in ("ollama", "gguf"):
        b64 = base64.b64encode(image_bytes).decode("ascii")
        body = json.dumps({
            "model": OLLAMA_VISION_MODEL,
            "prompt": prompt,
            "stream": False,
            "images": [b64],
        }).encode()
        req = urllib.request.Request(
            f"{OLLAMA_BASE}/api/generate",
            data=body,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read())

    if provider in ("openai", "openai_compat", "groq"):
        if provider == "groq":
            base_url = LUNA_GROQ_BASE_URL
            api_key = GROQ_API_KEY
            model = (LUNA_GROQ_CHAT_MODEL or OLLAMA_CHAT).strip()
        else:
            base_url = LUNA_OPENAI_BASE_URL
            api_key = LUNA_OPENAI_API_KEY
            model = (LUNA_OPENAI_VISION_MODEL or LUNA_OPENAI_CHAT_MODEL or OLLAMA_CHAT).strip()
        if not api_key:
            raise RuntimeError("Missing API key for selected vision provider.")
        b64 = base64.b64encode(image_bytes).decode("ascii")
        payload = {
            "model": model,
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": prompt},
                        {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{b64}"}},
                    ],
                }
            ],
        }
        req = urllib.request.Request(
            f"{base_url}/chat/completions",
            data=json.dumps(payload).encode("utf-8"),
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {api_key}",
            },
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read())
    raise RuntimeError(f"Unsupported LUNA_VISION_PROVIDER: {provider}")


def _vision_provider_extract_text(data: dict) -> str:
    provider = (LUNA_VISION_PROVIDER or "ollama").strip().lower()
    if provider in ("ollama", "gguf"):
        return (data.get("response") or "").strip()
    if provider in ("openai", "openai_compat", "groq"):
        ch = (data.get("choices") or [])
        msg0 = ((ch[0] or {}).get("message") or {}) if ch else {}
        return _openai_compat_extract_text(msg0.get("content"))
    return ""


def _vision_provider_ready() -> bool:
    provider = (LUNA_VISION_PROVIDER or "ollama").strip().lower()
    if provider in ("ollama", "gguf"):
        return bool((OLLAMA_VISION_MODEL or "").strip())
    if provider in ("openai", "openai_compat"):
        return bool((LUNA_OPENAI_API_KEY or "").strip())
    if provider == "groq":
        return bool((GROQ_API_KEY or "").strip())
    return False


def _ollama_tag_is_moondream(tag: str) -> bool:
    t = (tag or "").strip().lower().replace(" ", "")
    return t in ("moondream", "moondream2", "moondream-2") or t.startswith("moondream:")


def _moondream_unified_ollama_chat() -> bool:
    """Single multimodal model for web/VRM frames (Moondream via Ollama /api/chat + images)."""
    prov = (LUNA_CHAT_PROVIDER or "ollama").strip().lower()
    if prov not in ("ollama", "gguf"):
        return False
    if _should_use_gguf_chat(OLLAMA_CHAT):
        return False
    return _ollama_tag_is_moondream(OLLAMA_CHAT)


def _vision_describe_image(
    image_bytes: bytes,
    prompt: str = "Describe briefly what you see in this image. One or two sentences. Be concise. Only describe what is actually visible. Do not invent or hallucinate.",
    *,
    timeout: int | None = None,
    wait_for_lock: bool = True,
) -> str:
    """Call the configured vision provider with image input. Returns description or empty."""
    global _vision_last_error
    if not image_bytes:
        return ""
    provider = (LUNA_VISION_PROVIDER or "ollama").strip().lower()
    if provider in ("ollama", "gguf") and not OLLAMA_VISION_MODEL:
        return ""
    lock_acquired = False
    try:
        wait_for = _MODEL_VISION_QUEUE_WAIT_SEC if wait_for_lock else 0
        lock_acquired = _vision_infer_lock.acquire(timeout=wait_for)
        if not lock_acquired:
            _vision_last_error = "vision_busy"
            return ""
        req_to = int(timeout or OLLAMA_VISION_TIMEOUT)

        def _do_vision_call():
            return _vision_provider_describe_image_once(image_bytes, prompt, req_to)

        data = _run_with_model_guard(
            kind="vision",
            queue_wait_sec=_MODEL_VISION_QUEUE_WAIT_SEC,
            hard_timeout_sec=max(req_to + 8, _MODEL_VISION_GUARD_TIMEOUT_SEC),
            fn=_do_vision_call,
        )
        out = (_vision_provider_extract_text(data) or "").strip()
        _vision_last_error = ""
        return out[:2400] if out else ""
    except TimeoutError as e:
        _vision_last_error = str(e)[:200]
        return ""
    except Exception as e:
        _vision_last_error = str(e)[:200]
        return ""
    finally:
        if lock_acquired:
            try:
                _vision_infer_lock.release()
            except Exception:
                pass


_CAPTCHA_VISION_PROMPT = """You are inspecting a screenshot of a web browser page.

Does the visible content show a **bot check or CAPTCHA**? Count ONLY:
- reCAPTCHA / hCaptcha / FunCaptcha / Turnstile widgets
- Cloudflare "Checking your browser" or challenge pages
- "I'm not a robot" checkboxes, puzzle grids, or verification challenges

Do NOT count: normal login email/password forms, cookie banners, or regular site UI without a challenge.

Reply with EXACTLY one line: CAPTCHA_YES or CAPTCHA_NO
Optional second line: up to 8 words describing what you saw."""

def _vision_screen_has_captcha(image_bytes: bytes) -> tuple[bool, str]:
    """Granite (OLLAMA_VISION_MODEL) classifies viewport screenshot. Returns (present, short note)."""
    if not _vision_provider_ready() or not image_bytes:
        return False, ""
    raw = _vision_describe_image(image_bytes, prompt=_CAPTCHA_VISION_PROMPT, timeout=55)
    first = (raw.split("\n")[0] or "").strip().upper()
    note = ""
    lines = [ln.strip() for ln in raw.split("\n") if ln.strip()]
    if len(lines) > 1:
        note = lines[1][:160]
    if "CAPTCHA_YES" in first or first.startswith("YES"):
        return True, note or "Challenge or CAPTCHA UI visible."
    if "CAPTCHA_NO" in first or first.startswith("NO"):
        return False, ""
    # Fallback: keyword scan if model was chatty
    low = raw.lower()
    if (
        "captcha" in low
        or "recaptcha" in low
        or "hcaptcha" in low
        or "turnstile" in low
        or ("cloudflare" in low and "challenge" in low)
    ):
        if "no " not in low[:40] and "not visible" not in low[:80]:
            return True, note or raw[:120]
    return False, ""


def _captcha_observer_enabled() -> bool:
    return _env("LUNA_CAPTCHA_OBSERVER", "1").strip().lower() not in ("0", "false", "no", "off")


def _playwright_granite_captcha_observer(page) -> tuple[bool, str]:
    """Take a viewport screenshot and ask Granite if a CAPTCHA/challenge is visible."""
    if not _captcha_observer_enabled() or not _vision_provider_ready():
        return False, ""
    try:
        shot = page.screenshot(type="jpeg", quality=78, full_page=False, timeout=20000)
    except Exception:
        return False, ""
    if not shot:
        return False, ""
    return _vision_screen_has_captcha(shot)

def _camera_chat_turn(message: str, image_bytes: bytes | None) -> tuple[bool, str]:
    """One turn in the camera view chat (separate thread with Granite vision model). Returns (ok, reply)."""
    if not _vision_provider_ready():
        return False, "No vision provider configured. Set OLLAMA_VISION_MODEL or provider API key/model in .env."
    with _camera_chat_lock:
        img = image_bytes or _camera_last_image_bytes
    if not img:
        return False, "No image. Capture a frame first (keep the camera on)."
    message = (message or "").strip()[:500]
    if not message:
        return False, "Say something."
    with _camera_chat_lock:
        _camera_chat_history.append({"role": "user", "content": message})
        history = list(_camera_chat_history[-20:])  # last 10 turns
    lines = ["You are a helpful vision assistant. Describe ONLY what is actually visible in the camera image. Be factual and concise. Never invent scenes, personas, or inappropriate content. Answer briefly.", ""]
    for h in history[:-1]:
        who = "User" if h["role"] == "user" else "Assistant"
        lines.append(f"{who}: {h['content']}")
    lines.append(f"User: {message}")
    lines.append("Assistant:")
    prompt = "\n".join(lines)
    try:
        reply = _vision_describe_image(img, prompt=prompt, timeout=max(90, OLLAMA_VISION_TIMEOUT), wait_for_lock=True)
        if not reply:
            em = (_vision_last_error or "").lower()
            if "timed out" in em or "timeout" in em:
                reply = "Vision timed out on this frame. Try again in a moment."
            elif "vision_busy" in em:
                reply = "Vision is busy processing another frame. Try again in a moment."
            else:
                reply = "Vision provider returned no image reply. Try again."
        with _camera_chat_lock:
            _camera_chat_history.append({"role": "assistant", "content": reply})
        return True, reply
    except Exception as e:
        with _camera_chat_lock:
            if _camera_chat_history and _camera_chat_history[-1].get("role") == "user":
                _camera_chat_history.pop()
        return False, str(e)[:200]

def _process_camera_frame(image_bytes: bytes) -> dict:
    """Gemma-powered camera analysis: summary + face count + object list from vision model."""
    result = {"objects": [], "face_count": 0, "summary": "", "vision_summary": "", "ts": time.time(), "error": None}
    if not image_bytes:
        result["summary"] = "No image data."
        return result
    if not _vision_provider_ready():
        result["summary"] = "No vision provider configured."
        return result
    try:
        schema_prompt = (
            "Analyze this camera frame and return STRICT JSON only (no markdown, no extra text) with keys:\n"
            "{"
            "\"summary\": string (max 2 concise sentences), "
            "\"face_count\": integer >= 0, "
            "\"objects\": array of short object labels (lowercase, unique, max 12)"
            "}\n"
            "Use only visible items. If unsure, omit objects instead of guessing."
        )
        raw = _vision_describe_image(image_bytes, prompt=schema_prompt, timeout=45)
        parsed = None
        if raw:
            try:
                parsed = json.loads(raw)
            except Exception:
                m = re.search(r"\{.*\}", raw, flags=re.S)
                if m:
                    try:
                        parsed = json.loads(m.group(0))
                    except Exception:
                        parsed = None
        if isinstance(parsed, dict):
            summary = str(parsed.get("summary") or "").strip()
            face_count = parsed.get("face_count", 0)
            try:
                face_count = max(0, int(face_count))
            except Exception:
                face_count = 0
            objs_in = parsed.get("objects") or []
            labels: list[str] = []
            if isinstance(objs_in, list):
                for o in objs_in:
                    lab = str(o or "").strip().lower()
                    if not lab:
                        continue
                    if lab not in labels:
                        labels.append(lab)
                    if len(labels) >= 12:
                        break
            result["face_count"] = face_count
            result["objects"] = [{"label": lab, "confidence": 1.0} for lab in labels]
            if summary:
                result["vision_summary"] = summary
                result["summary"] = summary
                return result
        # Fallback: plain description still from the same vision model.
        plain = _vision_describe_image(image_bytes, timeout=max(60, OLLAMA_VISION_TIMEOUT), wait_for_lock=False)
        if plain:
            result["vision_summary"] = plain
            result["summary"] = plain
        else:
            result["summary"] = "No scene details returned by vision model."
    except Exception as e:
        result["error"] = str(e)
        result["summary"] = f"Camera processing error: {e}"
    return result

def _get_camera_see_result() -> str:
    """Return what the vision model (e.g. Granite 3.2 Vision) observed, or OpenCV summary if no vision model."""
    with _camera_lock:
        r = _camera_last_result
    if not r:
        return (
            "I don't have a live frame yet. In the VRM viewer, turn on **Camera** or **Screen share** "
            "(and wait a moment for the preview), then ask again."
        )
    # Prefer vision model's description when available
    vision = r.get("vision_summary") or ""
    if vision:
        return vision
    summary = r.get("summary") or "Nothing detected."
    face_count = r.get("face_count", 0)
    objects = r.get("objects") or []
    parts = [summary]
    if face_count and "face" not in summary.lower():
        parts.insert(0, f"I see **{face_count}** face(s).")
    if objects:
        from collections import Counter
        counts = Counter(o["label"] for o in objects)
        obj_str = ", ".join(f"{v} {k}" for k, v in counts.most_common(10))
        if obj_str and "objects:" not in summary:
            parts.append(f"Objects: {obj_str}.")
    return " ".join(parts)


# ── Command runner ────────────────────────────────────────────────────────────

def _run_cmd(cmd: str, params: dict, scope: str | None = None, user_message: str | None = None) -> str | dict:
    p = params or {}
    user_message = user_message or ""
    _set_working_on(cmd)
    try:
        return _run_cmd_impl(cmd, p, scope, user_message)
    finally:
        _clear_working_on()

def _run_cmd_impl(cmd: str, p: dict, scope: str | None, user_message: str) -> str | dict:
    dispatch = {
        "news":           lambda: _fetch_news(),
        "news_topic":     lambda: _fetch_topic_news((p.get("topic") or p.get("query") or "").strip(), int(p.get("limit") or 6)),
        "search":         lambda: _search(p.get("query","").strip()),
        "suno":           lambda: _run_suno(p.get("description","").strip()),
        "share_x":        lambda: _run_x_share(),
        "share_facebook": lambda: _run_fb_share(),
        "yt_comment":     lambda: _yt_comment(p.get("video_url","").strip()),
        "yt_like":        lambda: _yt_like_one(p.get("video_url","").strip()),
        "yt_analytics":   lambda: _yt_channel_analytics(int(p.get("days") or 30), int(p.get("limit") or 5)),
        "yt_react":       lambda: _yt_react(p.get("video_url","").strip()),
        "yt_watch_react": lambda: _yt_watch_react_start(p.get("video_url","").strip(), scope or LINKED_SCOPE or "web"),
        "yt_watch_stop":  lambda: _yt_watch_stop(scope or LINKED_SCOPE or "web"),
        "x_react":        lambda: _x_react(p.get("post_url","").strip()),
        "ig_dm":          lambda: _run_ig_dm(p.get("target","").strip(), p.get("message","").strip()),
        "fb_msg":         lambda: _run_messenger_msg(p.get("target","").strip(), p.get("message","").strip()),
        "msg":            lambda: _run_wa_msg((p.get("feedback_answer") or p.get("contact","")).strip(), p.get("description",None)),
        "call":           lambda: _run_discord_call(p.get("contact","").strip()),
        "dm":            lambda: _run_discord_dm(p.get("target","").strip(), p.get("message","").strip()),
        "scrape":         lambda: _scrape_website(p.get("url","").strip(), p.get("instruction","").strip(), p.get("post_channel","").strip()),
        "pc_vitals":      lambda: (True, _pc_vitals()),
        "luna_vitals":    lambda: (True, _luna_vitals()),
        "camera_see":     lambda: (True, _get_camera_see_result()),
    }
    # Request feedback (resume with feedback_answer)
    if p.get("feedback_answer") is not None and cmd == "ask_me":
        return f"You chose: **{p.get('feedback_answer', '')}**."
    if cmd == "ask_me":
        return _request_feedback(
            scope, user_message, "Choose an option:",
            ["Option A", "Option B"], "ask_me", p,
        )
    if cmd == "remind":
        time_raw = p.get("time","").strip().replace(" ","")
        msg_part = p.get("message","").strip()[:500]
        if not msg_part: return "What should I remind you to do?"
        time_str = _parse_time(time_raw)
        if not time_str: return "Invalid time. Try 7pm, 19:00, etc."
        if not LINKED_ID: return "Set LINKED_DISCORD_USER_ID in .env for reminders."
        add_reminder(time_str, msg_part, LINKED_ID)
        return f"✅ Reminder set for **{time_str}** — {msg_part}. I'll DM you on Discord."
    if cmd == "create_code":
        req = re.sub(r"^(?:shadow[,:\s]+|luna[,:\s]+)","", p.get("request","").strip(), flags=re.I).strip()
        if not req: return "Describe what the script should do."
        ok, result = _run_create_code(req)
        if not ok: _record_failure(cmd, result, p); return f"❌ {result}"
        return f"✅ {result}"
    if cmd in dispatch:
        if cmd == "news":
            ok, result = _fetch_news()
            if not ok: _record_failure(cmd, result, p); return f"❌ {result}"
            return result
        ok, result = dispatch[cmd]()
        if not ok and isinstance(result, dict) and result.get("need_feedback"):
            return _request_feedback(scope, user_message, result.get("message", "?"), result.get("options"), cmd, p)
        if not ok: _record_failure(cmd, result, p); return f"❌ {result}"
        return f"✅ {result}"
    if cmd == "suno_ready":
        return _mark_suno_logged_in()
    if cmd == "help":
        return HELP_TEXT
    if cmd == "rank":
        use_scope = scope or LINKED_SCOPE or "web"
        actor_key, _rest = _relationship_parse_target_arg((p.get("target") or p.get("user") or "").strip(), use_scope)
        row = _relationship_get(actor_key)
        if not row:
            return f"No rank yet for **{actor_key}**. Talk with Luna first."
        pts = int(row.get("points", 0) or 0)
        rank = row.get("rank") or _rel_rank_for_points(pts)
        return f"🏅 **{actor_key}** is **{rank}** with **{pts}** points."
    if cmd == "bond":
        use_scope = scope or LINKED_SCOPE or "web"
        target = (p.get("target") or p.get("user") or "").strip()
        if target.lower() in ("top", "leaderboard", "lb"):
            return _relationship_top_text(use_scope)
        actor_key, _rest = _relationship_parse_target_arg(target, use_scope)
        row = _relationship_get(actor_key)
        return _relationship_format_summary(actor_key, row, include_notes=True)
    if cmd == "notes":
        use_scope = scope or LINKED_SCOPE or "web"
        actor_key, _rest = _relationship_parse_target_arg((p.get("target") or p.get("user") or "").strip(), use_scope)
        row = _relationship_get(actor_key)
        if not row:
            return f"No notes yet for **{actor_key}**."
        notes = row.get("notes")
        if not isinstance(notes, list) or not notes:
            return f"No notes yet for **{actor_key}**."
        return "🗒️ **Notes**\n" + "\n".join(f"- {str(n)[:140]}" for n in notes[-12:])
    if cmd == "note_add":
        use_scope = scope or LINKED_SCOPE or "web"
        target = (p.get("target") or p.get("user") or "").strip()
        note = (p.get("text") or p.get("note") or "").strip()
        actor_key, remainder = _relationship_parse_target_arg(target, use_scope)
        body = note or remainder
        ok, msg = _relationship_add_note(actor_key, body)
        return f"✅ {msg}" if ok else f"❌ {msg}"
    if cmd == "play":
        query = (p.get("query") or "").strip()
        if not query:
            return "Usage: !play <song or URL> (from UI: play in Discord if you're in a voice channel)."
        try:
            loop = getattr(bot, "loop", None)
            if loop and loop.is_running():
                future = asyncio.run_coroutine_threadsafe(_play_in_discord_for_linked_user_async(query), loop)
                ok, msg = future.result(timeout=35)
                return f"✅ {msg}" if ok else f"❌ {msg}"
        except Exception as e:
            return f"❌ {e}"
        return "❌ Discord not ready. Try again in a moment."
    if cmd == "skip":
        try:
            loop = getattr(bot, "loop", None)
            if loop and loop.is_running():
                future = asyncio.run_coroutine_threadsafe(_skip_in_discord_for_linked_user_async(), loop)
                ok, msg = future.result(timeout=10)
                return f"✅ {msg}" if ok else f"❌ {msg}"
        except Exception as e:
            return f"❌ {e}"
        return "❌ Discord not ready."
    if cmd == "stop":
        try:
            loop = getattr(bot, "loop", None)
            if loop and loop.is_running():
                future = asyncio.run_coroutine_threadsafe(_stop_in_discord_for_linked_user_async(), loop)
                ok, msg = future.result(timeout=10)
                return f"✅ {msg}" if ok else f"❌ {msg}"
        except Exception as e:
            return f"❌ {e}"
        return "❌ Discord not ready."
    if cmd == "podcast":
        choice = (p.get("choice") or "").strip()
        # If no choice: just list available episodes, do NOT auto-play.
        if not choice:
            ok, result = _get_podcast_tracks()
            if not ok:
                return f"❌ {result}"
            tracks = result
            return _format_podcast_menu(tracks)
        # With a choice: play that specific episode for the linked user.
        try:
            loop = getattr(bot, "loop", None)
            if loop and loop.is_running():
                future = asyncio.run_coroutine_threadsafe(_play_podcast_in_discord_async(choice), loop)
                ok, msg = future.result(timeout=30)
                return f"✅ {msg}" if ok else f"❌ {msg}"
        except Exception as e:
            return f"❌ {e}"
        return "❌ Discord not ready. Join a voice channel and try **!podcast 1** (for example) again."
    if cmd == "podcast_create":
        ok, msg = _create_podcast_from_description((p.get("description") or "").strip())
        if not ok: _record_failure("podcast_create", msg, p)
        return f"✅ {msg}" if ok else f"❌ {msg}"
    if cmd in ("join","leave","pause","resume","queue"):
        return f"Use !{cmd} in Discord."
    if cmd == "joinme":
        text = (p.get("message") or p.get("query") or "Hey, Luna here! I'm in your voice channel.").strip()
        try:
            future = asyncio.run_coroutine_threadsafe(_join_linked_user_vc_and_speak(text, disconnect_after=False), bot.loop)
            ok = future.result(timeout=15)
        except Exception:
            ok = False
        return "✅ Joined your voice channel and said it." if ok else "❌ Join a Discord voice channel first, then try **!joinme**."
    if cmd == "analytics_screen":
        ok, r = _describe_dashboard_screenshot()
        if not ok:
            _record_failure("analytics_screen", r, p)
        return f"✅ {r}" if ok else f"❌ {r}"
    if cmd == "summarize":
        ok, msg = _summarize_input((p.get("input") or p.get("query") or "").strip())
        if not ok:
            _record_failure("summarize", msg, p)
            return f"❌ {msg}"
        return f"✅ {msg}"
    if cmd == "digest":
        use_scope = scope or LINKED_SCOPE or "web"
        return _daily_digest(use_scope)
    if cmd == "todo":
        use_scope = scope or LINKED_SCOPE or "web"
        action = (p.get("action") or "list").strip().lower()
        if action in ("add", "new"):
            return _todo_add(use_scope, (p.get("text") or p.get("task") or "").strip())
        if action in ("done", "complete", "finish"):
            return _todo_done(use_scope, (p.get("text") or p.get("id") or "").strip())
        return _todo_list_text(use_scope)
    if cmd == "calendar":
        use_scope = scope or LINKED_SCOPE or "web"
        action = (p.get("action") or "list").strip().lower()
        if action in ("list", "upcoming"):
            return _calendar_list(use_scope, mode="upcoming")
        if action == "today":
            return _calendar_list(use_scope, mode="today")
        if action == "week":
            return _calendar_list(use_scope, mode="week")
        if action in ("delete", "remove"):
            ok, msg = _calendar_delete(use_scope, (p.get("text") or p.get("id") or "").strip())
            return f"✅ {msg}" if ok else f"❌ {msg}"
        if action in ("add", "new"):
            ok, msg = _calendar_add(
                use_scope,
                (p.get("date") or "").strip(),
                (p.get("time") or "").strip(),
                (p.get("title") or p.get("text") or "").strip(),
                (p.get("note") or "").strip(),
            )
            return f"✅ {msg}" if ok else f"❌ {msg}"
        return "Usage: !calendar add YYYY-MM-DD HH:MM title | !calendar list | !calendar today | !calendar week | !calendar delete <n>"
    if cmd == "research":
        ok, msg = _research_content((p.get("topic") or p.get("query") or p.get("input") or "").strip())
        if not ok:
            _record_failure("research", msg, p)
            return f"❌ {msg}"
        return f"✅ {msg}"
    if cmd == "research_story":
        ok, msg = _research_story_script((p.get("topic") or p.get("query") or p.get("input") or "").strip())
        if not ok:
            _record_failure("research_story", msg, p)
            return f"❌ {msg}"
        return f"✅ {msg}"
    if cmd == "audiobook":
        action = (p.get("action") or "create").strip().lower()
        sc = scope or LINKED_SCOPE or "web"
        if action == "continue":
            ok, msg = _audiobook_continue_next(sc)
            if not ok:
                _record_failure("audiobook", msg, p)
                return f"❌ {msg}"
            return f"✅ {msg}"
        if action == "cancel":
            ok, msg = _audiobook_cancel_pending(sc)
            return f"✅ {msg}" if ok else f"❌ {msg}"
        if action != "create":
            return "Usage: !audiobook create <topic> [duration:30] [--brief|--story|--both], or **continue** / **cancel**"
        ok, msg = _create_audiobook_from_research(
            (p.get("topic") or p.get("query") or p.get("input") or "").strip(),
            scope=sc,
        )
        if not ok:
            _record_failure("audiobook", msg, p)
            return f"❌ {msg}"
        return f"✅ {msg}"
    # Absorbed tools (user-approved drafts)
    if cmd in _absorbed_tool_names:
        ok, result = _run_absorbed_tool(cmd, p)
        if not ok: _record_failure(cmd, result, p); return f"❌ {result}"
        return f"✅ {result}"
    return ""

def _recycle_luna_creations_leftovers() -> None:
    """Prune Luna's creations so the folder stays small.

    Runtime only uses scripts in data/absorbed_tools/, so here we:
    - Keep WHAT_LUNA_CREATED.txt (audit trail)
    - Delete all .py scripts in Luna's creations/ and Luna's creations/agents/
    - Optionally remove any old Recycled/ folder if present
    """
    try:
        if not os.path.isdir(LUNA_CREATIONS_DIR):
            return
        manifest_name = _LUNA_CREATIONS_MANIFEST
        # Delete any loose .py files (copies of absorbed tools or ad‑hoc creations)
        for name in os.listdir(LUNA_CREATIONS_DIR):
            path = os.path.join(LUNA_CREATIONS_DIR, name)
            if name == manifest_name:
                continue
            if os.path.isfile(path) and name.endswith(".py"):
                try:
                    os.remove(path)
                except Exception:
                    pass
            elif os.path.isdir(path) and name == "agents":
                # agents/: delete all .py files (create-code scripts)
                for sub in os.listdir(path):
                    if not sub.endswith(".py"):
                        continue
                    src = os.path.join(path, sub)
                    if os.path.isfile(src):
                        try:
                            os.remove(src)
                        except Exception:
                            pass
        # Remove any old Recycled/ folder entirely (no more archive folder)
        recycled_dir = os.path.join(LUNA_CREATIONS_DIR, "Recycled")
        if os.path.isdir(recycled_dir):
            try:
                shutil.rmtree(recycled_dir)
            except Exception:
                pass
    except Exception:
        pass


def _luna_creations_log(entry_type: str, filename: str, description: str = "") -> None:
    """Append one line to WHAT_LUNA_CREATED.txt so you can see what new programs/skills/agents she created."""
    try:
        os.makedirs(LUNA_CREATIONS_DIR, exist_ok=True)
        manifest = os.path.join(LUNA_CREATIONS_DIR, _LUNA_CREATIONS_MANIFEST)
        exists = os.path.isfile(manifest)
        with open(manifest, "a", encoding="utf-8") as f:
            if not exists:
                f.write("# What Luna created — new programs, skills, absorbed tools\n")
                f.write("# Date       | Type           | File        | Description\n")
                f.write("# " + "-" * 70 + "\n")
            date_str = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M")
            desc = (description or "").replace("\n", " ").strip()[:100]
            f.write(f"{date_str} | {entry_type:<14} | {filename:<12} | {desc}\n")
    except Exception:
        pass

def _run_create_code(request: str) -> tuple[bool, str]:
    system = "Generate Python code for the request. Output only a ```python code block. No explanation."
    reply = ollama_chat(f"Generate Python code:\n\n{request}", system=system, model=OLLAMA_MODEL)
    code = reply.strip()
    for pat in (r"```python\s*\n(.*?)```", r"```\s*\n(.*?)```"):
        m = re.search(pat, code, re.DOTALL | re.I)
        if m and m.group(1).strip(): code = m.group(1).strip(); break
    if not code: return False, "No code generated."
    slug = re.sub(r"[^\w\s-]","", request.lower())[:30].strip().replace(" ","_") or "script"
    slug = re.sub(r"_+","_", slug).strip("_") or "script"
    base = os.path.join("Luna's creations", "agents", f"{slug}.py")
    ok, result = luna_write_file(base, code)
    if not ok: return False, result
    _luna_creations_log("program", f"agents/{slug}.py", request.strip())
    if result and os.path.isfile(result):
        _security_scan_and_alert(result)
    if result and sys.platform == "win32":
        subprocess.Popen(["notepad", result], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    return True, f"Created **{base}**"

# ── Intent / NL command parser ────────────────────────────────────────────────

_CONV_START = re.compile(
    r"^(how\s|what\s|why\s|when\s|who\s|where\s|is\s|are\s|can you|could you|would you|tell me|i\s|we\s|my\s|hey\s|hi\s|hello\s)",
    re.I)

def _parse_command(text: str) -> tuple[str, dict] | None:
    """Heuristic NL command parser — no Ollama needed."""
    raw = (text or "").strip()
    if not raw: return None
    low = raw.lower()
    if low.startswith("!"): return None

    # News (topic-specific first, then general headlines)
    m_news_topic = re.search(
        r"\b(?:news|headlines|latest updates?|current updates?)\s+(?:about|on|for)\s+(.+?)(?:\s+(?:today|right now|now|currently))?$",
        raw,
        re.I | re.S,
    )
    if m_news_topic:
        topic = (m_news_topic.group(1) or "").strip(" .,!?:;")
        if topic:
            return "news_topic", {"topic": topic}
    m_news_topic2 = re.search(
        r"\b(?:what(?:'s| is)?\s+(?:the\s+)?)?(?:latest|current|today'?s)\s+news\s+(?:on|about)\s+(.+)$",
        raw,
        re.I | re.S,
    )
    if m_news_topic2:
        topic = (m_news_topic2.group(1) or "").strip(" .,!?:;")
        if topic:
            return "news_topic", {"topic": topic}
    if re.search(r"\b(?:news|headlines|latest news|world news)\b", low): return "news", {}
    # Suno: **!suno** / **!suno_ready** only (see _handle_bang). NL matching was firing on screen-share
    # vision text (e.g. Suno.com UI) and on voice — hub uses insertCommand('!suno ').
    # Share (Share Song button in UI → share to X)
    if re.search(r"\bshare\s+song\b", low) or re.search(r"\bshare\b.*\b(?:x|twitter)\b", low) or re.search(r"\bpost\b.*\b(?:x|twitter)\b", low):
        return "share_x", {}
    if re.search(r"\bshare\b.*\bfacebook\b", low) or re.search(r"\bpost\b.*\bfacebook\b", low):
        return "share_facebook", {}
    # YouTube comment — explicit "yt_comment" (no !) or "comment" + URL
    yt_url = re.search(r"(https?://(?:www\.)?(?:youtube\.com|youtu\.be)/[^\s)]+)", raw)
    if yt_url and (low.startswith("yt_comment ") or re.search(r"\b(?:comment|reply)\b", low)):
        return "yt_comment", {"video_url": yt_url.group(1).rstrip(".,!?")}
    if yt_url and low.startswith("yt_like "):
        return "yt_like", {"video_url": yt_url.group(1).rstrip(".,!?")}
    if yt_url and re.search(r"\b(?:watch\s*react|watch\s+along|co[-\s]?watch|live\s+react)\b", low):
        return "yt_watch_react", {"video_url": yt_url.group(1).rstrip(".,!?")}
    if yt_url and (low.startswith("yt_react ") or re.search(r"\b(?:react|reaction)\b", low)):
        return "yt_react", {"video_url": yt_url.group(1).rstrip(".,!?")}
    if re.search(r"\b(?:stop|cancel|end)\b", low) and re.search(r"\b(?:watch\s*react|co[-\s]?watch)\b", low):
        return "yt_watch_stop", {}
    x_url = re.search(r"(https?://(?:www\.)?(?:x\.com|twitter\.com)/[^\s)]+)", raw, re.I)
    if x_url and (low.startswith("x_react ") or re.search(r"\b(?:react|reaction)\b", low)):
        return "x_react", {"post_url": x_url.group(1).rstrip(".,!?")}
    if re.search(r"\b(?:yt analytics|youtube analytics|channel analytics|top videos|most traction)\b", low):
        m_days = re.search(r"\b(?:last|past)\s+(\d{1,3})\s*days?\b", low)
        m_limit = re.search(r"\btop\s+(\d{1,2})\b", low)
        return "yt_analytics", {
            "days": int(m_days.group(1)) if m_days else 30,
            "limit": int(m_limit.group(1)) if m_limit else 5,
        }
    if re.search(
        r"\b(?:read|analyze|scan)\s+(?:my\s+)?(?:analytics|dashboard|stats)\s+(?:from\s+)?(?:the\s+)?(?:screen|screenshot|display)\b",
        low,
    ):
        return "analytics_screen", {}
    # Instagram DM — explicit "ig_dm username message..." or natural "instagram/ig dm @user"
    if low.startswith("ig_dm "):
        rest = raw[len("ig_dm "):].strip()
        parts = rest.split(None, 1)  # first token = username, rest = message
        if parts:
            target = parts[0].lstrip("@")
            msg = (parts[1].strip() if len(parts) > 1 else "") or ""
            if re.fullmatch(r"[a-zA-Z0-9._]{2,30}", target):
                return "ig_dm", {"target": target, "message": msg}
    m = re.search(r"\b(?:instagram|ig|insta)\b.*\b(?:message|dm|send)\b.*?@?([a-z0-9._]{2,30})", low)
    if m: return "ig_dm", {"target": m.group(1), "message": ""}
    # Search/Google
    for pfx in ("search for ","search ","google ","look up ","find "):
        if low.startswith(pfx):
            return "search", {"query": raw[len(pfx):].strip()}
    for pfx in ("research ","research on ","research about "):
        if low.startswith(pfx):
            return "research", {"topic": raw[len(pfx):].strip()}
    for pfx in ("research_story ","research story ","story_script ","story script "):
        if low.startswith(pfx):
            return "research_story", {"topic": raw[len(pfx):].strip()}
    if low == "audiobook continue" or low.startswith("audiobook continue "):
        return "audiobook", {"action": "continue"}
    if low == "audiobook cancel" or low.startswith("audiobook cancel "):
        return "audiobook", {"action": "cancel"}
    if low.startswith("audiobook create "):
        return "audiobook", {"action": "create", "topic": raw[len("audiobook create "):].strip()}
    if low.startswith("audiobook "):
        rest = raw[len("audiobook "):].strip()
        return "audiobook", {"action": "create", "topic": rest}
    # Scrape
    m = re.search(r"\b(?:scrape|extract from|grab from|get from|pull from)\b\s+(https?://\S+)\s*(.*)", raw, re.I)
    if m:
        scrape_url = m.group(1).rstrip(".,!?")
        rest = m.group(2).strip()
        post_ch = ""
        post_m = re.search(r"(?:post(?:\s+to)?|send(?:\s+to)?)\s*#?([\w-]+)", rest, re.I)
        if post_m:
            post_ch = post_m.group(1)
            rest = rest[:post_m.start()].strip() + " " + rest[post_m.end():].strip()
        return "scrape", {"url": scrape_url, "instruction": rest.strip(), "post_channel": post_ch}
    # Summarize
    for pfx in ("summarize ","summary of ","summarise "):
        if low.startswith(pfx):
            return "summarize", {"input": raw[len(pfx):].strip()}
    if low in ("digest", "daily digest", "today digest"):
        return "digest", {}
    if low.startswith("rank"):
        rest = raw[4:].strip() if low != "rank" else ""
        return "rank", {"target": rest}
    if low.startswith("bond"):
        rest = raw[4:].strip() if low != "bond" else ""
        return "bond", {"target": rest}
    if low.startswith("notes"):
        rest = raw[5:].strip() if low != "notes" else ""
        return "notes", {"target": rest}
    if low.startswith("note add "):
        rest = raw[len("note add "):].strip()
        target, remainder = _relationship_parse_target_arg(rest, "")
        if target:
            return "note_add", {"target": target, "text": remainder}
        return "note_add", {"text": rest}
    # Todo
    if low.startswith("todo "):
        rest = raw[5:].strip()
        if rest.lower().startswith("add "):
            return "todo", {"action": "add", "text": rest[4:].strip()}
        if rest.lower().startswith("done "):
            return "todo", {"action": "done", "text": rest[5:].strip()}
        return "todo", {"action": "list"}
    # Calendar
    if low.startswith("calendar "):
        rest = raw[9:].strip()
        rlow = rest.lower()
        if rlow in ("list", "upcoming", ""):
            return "calendar", {"action": "list"}
        if rlow in ("today",):
            return "calendar", {"action": "today"}
        if rlow in ("week", "this week"):
            return "calendar", {"action": "week"}
        if rlow.startswith("delete "):
            return "calendar", {"action": "delete", "text": rest[7:].strip()}
        # add YYYY-MM-DD HH:MM title...
        m = re.match(r"^(?:add\s+)?(\d{4}-\d{2}-\d{2})\s+(\d{1,2}:\d{2})\s+(.+)$", rest, re.I)
        if m:
            return "calendar", {"action": "add", "date": m.group(1), "time": m.group(2), "title": m.group(3).strip()}
        return "calendar", {"action": "list"}
    # WhatsApp msg — explicit "msg contact [message]" (contact can be full number with spaces, e.g. +357 99 447267)
    if low.startswith("msg "):
        rest = raw[len("msg "):].strip()
        contact, desc = _parse_wa_contact_and_msg(rest)
        if contact:
            return "msg", {"contact": contact, "description": desc}
    # Natural: "send a message to X" / "message X saying Y"
    m = re.match(r"^(?:send\s+a?\s*message\s+to|message)\s+(.+?)(?:\s+saying\s+(.+))?$", low)
    if m: return "msg", {"contact": m.group(1).strip(), "description": (m.group(2) or "").strip() or None}
    # Join VC and speak — "join me", "join my vc", "luna join"
    if re.search(r"\b(?:join\s+me|join\s+my\s+vc|luna\s+join)\b", low):
        m = re.search(r"(?:join\s+me|join\s+my\s+vc|luna\s+join)\s*[,:]?\s*(.+)", low)
        msg = (m.group(1).strip() if m and m.group(1) else "") or ""
        return "joinme", {"message": msg}
    # WhatsApp call
    m = re.match(r"^call\s+(.+)$", low)
    if m: return "call", {"contact": m.group(1).strip()}
    # Discord DM — "dm username message..." or "dm user about subject..."
    if low.startswith("dm "):
        rest = raw[len("dm "):].strip()
        parts = rest.split(None, 1)
        if parts:
            t = parts[0].strip().lstrip("@")
            msg = (parts[1].strip() if len(parts) > 1 else "") or ""
            if t:
                return "dm", {"target": t, "message": msg}
    # Natural: "tell X about Y" / "send X a message about Y" → Luna summarizes and DMs
    m = re.match(r"^(?:tell|message|dm|send)\s+(.+?)\s+(?:about|regarding|re:)\s+(.+)$", low)
    if m: return "dm", {"target": m.group(1).strip().lstrip("@"), "message": "about " + m.group(2).strip()}
    m = re.match(r"^send\s+(.+?)\s+a\s+message\s+about\s+(.+)$", low)
    if m: return "dm", {"target": m.group(1).strip().lstrip("@"), "message": "about " + m.group(2).strip()}
    m = re.match(r"^inform\s+(.+?)\s+(?:about|that)\s+(.+)$", low)
    if m: return "dm", {"target": m.group(1).strip().lstrip("@"), "message": "about " + m.group(2).strip()}
    # Messenger — explicit "fb_msg name message..." (no !) or natural "messenger/facebook message to <name>"
    if low.startswith("fb_msg "):
        rest = raw[len("fb_msg "):].strip()
        # Support "fb_msg John Smith hey how are you" — name before the message
        # Try splitting at common message indicators
        msg_split = re.split(r'\s+(?:say|saying|message|msg|tell|:)\s+', rest, maxsplit=1, flags=re.I)
        if len(msg_split) == 2:
            return "fb_msg", {"target": msg_split[0].strip(), "message": msg_split[1].strip()}
        # Fallback: first word(s) as target, rest as message
        parts = rest.split(None, 1)
        if parts:
            target = parts[0].strip()
            msg = (parts[1].strip() if len(parts) > 1 else "") or ""
            return "fb_msg", {"target": target, "message": msg}
    # Natural: "messenger John Smith", "facebook message to John Smith", "send message on messenger to John"
    m = re.search(r"\b(?:messenger|facebook message)\b(?:\s+to)?\s+(.+?)(?:\s+(?:say|saying|message|:)\s+(.+))?$", raw, re.I)
    if m:
        target = m.group(1).strip().lstrip("@")
        msg = (m.group(2) or "").strip()
        if len(target) >= 2:
            return "fb_msg", {"target": target, "message": msg}
    # Remind
    m = re.search(r"\bremind me at\s+(\S+)\s+to\s+(.+)", raw, re.I | re.S)
    if m: return "remind", {"time": m.group(1), "message": m.group(2).strip()}
    # Create code
    if re.search(r"\b(?:create|write|generate)\s+(?:a\s+)?(?:python\s+)?(?:script|code)\b", low):
        return "create_code", {"request": raw}
    # PC vitals (CPU, RAM, disk)
    if re.search(r"\b(?:how'?s my pc|pc status|ram usage|system vitals|pc running slow|hardware|pc vitals)\b", low):
        return "pc_vitals", {}
    # Luna's own vitals (process, Ollama, uptime)
    if re.search(r"\b(?:how'?s luna|luna status|are you okay|luna vitals|your status)\b", low):
        return "luna_vitals", {}
    # Camera — "what do you see", "what can you see", "do you see me", etc.
    if re.search(r"\b(?:what do you see|what can you see|what do i look like|describe what you see|do you see me|what'?s in the (?:camera|frame)|what are you seeing)\b", low):
        return "camera_see", {}
    # Play
    if low.startswith("play ") or low == "play":
        return "play", {"query": raw[5:].strip() if low.startswith("play ") else ""}
    # Custom podcast — play or create
    if low.startswith("podcast create ") or low.startswith("create podcast "):
        rest = raw[len("podcast create "):].strip() if low.startswith("podcast create ") else raw[len("create podcast "):].strip()
        if rest: return "podcast_create", {"description": rest}
    m = re.match(r"^create\s+a\s+podcast\s+about\s+(.+)$", low)
    if m: return "podcast_create", {"description": m.group(1).strip()}
    if low in ("podcast", "play podcast", "custom podcast", "play custom podcast"):
        return "podcast", {}
    if low.startswith("podcast "):
        rest = raw[len("podcast "):].strip()
        return "podcast", {"choice": rest} if rest else {"choice": ""}
    if low.startswith("play podcast "):
        rest = raw[len("play podcast "):].strip()
        return "podcast", {"choice": rest} if rest else {"choice": ""}
    # Skip / Stop (music)
    if low in ("skip", "next", "next song"): return "skip", {}
    if low in ("stop", "stop music", "stop the music"): return "stop", {}
    # Ask me (web UI popup demo) — only when the *whole* message is a short ask-me phrase,
    # not "ask me questions about myself" etc.
    _trim = raw.strip()
    if re.fullmatch(r"(?i)luna[,:\s]+ask\s+me(?:\s+something)?\s*[.!?…]*", _trim) or re.fullmatch(
        r"(?i)ask\s+me(?:\s+something)?\s*[.!?…]*", _trim
    ):
        return "ask_me", {}
    # Help
    if re.search(r"\b(?:help|commands|what can you do)\b", low): return "help", {}
    return None

def _likely_command(text: str) -> bool:
    low = (text or "").strip().lower()
    if not low: return False
    if _CONV_START.match(low): return False
    starters = ("play ","podcast ","podcast create ","create podcast ","audiobook ","audiobook create ","audiobook continue ","audiobook cancel ","search ","research ","research_story ","research story ","story_script ","story script ","send ","create ","call ","dm ","tell ","inform ","msg ","share ","post ","remind ","yt_comment ","yt_like ","yt_analytics ","yt_react ","yt_watch_react ","yt_watch_stop ","x_react ","comment ","ig_dm ","fb_msg ","google ","news","!help","how's ","pc status","luna status","ram ","what do you see","what can you see","ask me ","summarize ","summary of ","todo ","calendar ","join me ","join my ","luna join ","rank","bond","notes","note add ")
    return any(low.startswith(s) for s in starters) or low in ("play","news","help","skip","stop","ask me","digest","daily digest","todo","todo list","calendar","calendar list","calendar today","calendar week","youtube analytics","yt analytics","rank","bond","notes") or "luna status" in low or "pc status" in low or bool(re.search(r"\b(what do you see|what can you see|do you see me|describe what you see)\b", low)) or bool(re.search(r"\b(?:read|analyze|scan)\s+(?:my\s+)?(?:analytics|dashboard|stats)\b", low))

def _is_retry(msg: str) -> bool:
    # Strict retry command detection only (avoid accidental trigger in normal conversation).
    low = re.sub(r"\s+", " ", (msg or "").strip().lower()).strip(" .!?…")
    return low in {
        "retry",
        "!retry",
        "/retry",
        "retry that",
        "retry and fix",
        "try again",
    }

# ── Recent social (for "continue conversation" / notification box in UI) ───────

def _record_recent_social(platform: str, target: str, url: str = ""):
    """Record a sent DM/post so the UI can show 'Open X to check replies'."""
    try:
        entry = {"platform": platform, "target": target, "url": url or "", "ts": time.time()}
        with _recent_social_lock:
            data = _load_json(_RECENT_SOCIAL_PATH, [])
            if not isinstance(data, list): data = []
            data = [e for e in data if isinstance(e, dict) and (time.time() - e.get("ts", 0)) < 7 * 24 * 3600]
            data.insert(0, entry)
            data = data[:20]
            _save_json(_RECENT_SOCIAL_PATH, data)
    except Exception: pass

def _get_recent_social() -> list:
    """Return recent social actions for the UI notification box."""
    try:
        data = _load_json(_RECENT_SOCIAL_PATH, [])
        if not isinstance(data, list): return []
        return [e for e in data if isinstance(e, dict) and (time.time() - e.get("ts", 0)) < 7 * 24 * 3600][:10]
    except Exception: return []


def _check_instagram_replies(target: str | None = None) -> tuple[bool, bool, str, str]:
    """Check if there is a new reply in the Instagram DM thread. Returns (success, has_new, from_username, preview_or_error)."""
    target = (target or "").strip().lstrip("@")
    if not target:
        recent = _get_recent_social()
        for e in recent:
            if isinstance(e, dict) and (e.get("platform") or "").lower() == "instagram":
                target = (e.get("target") or "").strip().lstrip("@")
                break
        if not target:
            return False, False, "", "No Instagram conversation to check. Send a DM first."
    if not re.fullmatch(r"[a-zA-Z0-9._]{2,30}", target):
        return False, False, "", "Invalid username."
    if not _ig_lock.acquire(blocking=False):
        return False, False, "", "Instagram is busy (send or another check in progress). Try again shortly."
    try:
        last_seen = _load_json(_IG_LAST_SEEN_PATH, {})
        if not isinstance(last_seen, dict):
            last_seen = {}
        from playwright.sync_api import sync_playwright
        os.makedirs(IG_PROFILE_DIR, exist_ok=True)
        context = None
        try:
            with sync_playwright() as p:
                for attempt in range(2):
                    try:
                        context = _launch_social_browser(IG_PROFILE_DIR, p)
                        break
                    except Exception as launch_err:
                        if attempt == 0 and "closed" in str(launch_err).lower():
                            time.sleep(2)
                            continue
                        return False, False, "", f"Browser failed: {launch_err}"
                if not context:
                    return False, False, "", "Browser failed to start."
                page = context.pages[0] if context.pages else context.new_page()
                page.goto(f"{IG_BASE}/{target}/", wait_until="domcontentloaded", timeout=90000)
                page.wait_for_timeout(2000)
                saw_cap, cap_note = _playwright_granite_captcha_observer(page)
                if saw_cap:
                    return False, False, "", (
                        "Captcha or challenge detected (Granite vision). "
                        + (cap_note or "Finish the check in the browser, then try again.")
                    )
                if "login" in page.url.lower() or "accounts/login" in page.url:
                    return False, False, "", "Instagram needs login. Log in via Luna first (send a DM once)."
                # Open the DM thread (click Message on profile)
                msg_btn = None
                for sel in ['button:has-text("Message")', '[role="button"]:has-text("Message")', 'a[href*="/direct/"]:has-text("Message")', 'div[role="button"]:has-text("Message")']:
                    loc = page.locator(sel).first
                    if loc.count() and loc.is_visible():
                        try:
                            loc.scroll_into_view_if_needed(timeout=3000)
                            page.wait_for_timeout(500)
                            loc.click(timeout=5000)
                            msg_btn = True
                            break
                        except Exception:
                            continue
                if not msg_btn:
                    return False, False, "", "Could not open the conversation."
                page.wait_for_timeout(2500)
                # Find last message in the thread and see if it's from them (incoming)
                try:
                    # Instagram: message bubbles often in a scrollable list; our messages right-aligned, theirs left
                    last_msg_sel = [
                        'div[role="list"] div[dir="auto"]',
                        '[data-pagelet*="MessageList"] div[dir="auto"]',
                        'div[role="list"] > div > div',
                        'section div[dir="auto"]',
                    ]
                    last_el = None
                    for sel in last_msg_sel:
                        loc = page.locator(sel)
                        if loc.count() > 0:
                            last_el = loc.last
                            break
                    if not last_el or not last_el.count():
                        return True, False, target, ""  # no messages or couldn't find
                    text = (last_el.inner_text() or "").strip()[:200]
                    # Is this message from them (left) or us (right)? Check alignment
                    is_ours = last_el.evaluate("""(el) => {
                        if (!el) return true;
                        const r = el.getBoundingClientRect();
                        let p = el.parentElement;
                        for (let i = 0; i < 8 && p; i++) {
                            const pr = p.getBoundingClientRect();
                            if (pr.width > 100 && pr.height > 20) {
                                const mid = pr.left + pr.width / 2;
                                return r.left >= mid - 30;
                            }
                            p = p.parentElement;
                        }
                        return true;
                    }""")
                    if is_ours:
                        _save_json(_IG_LAST_SEEN_PATH, last_seen)
                        return True, False, target, ""
                    key = f"ig_{target}"
                    prev = last_seen.get(key)
                    if isinstance(prev, dict) and prev.get("text") == text:
                        return True, False, target, ""
                    last_seen[key] = {"text": text, "ts": time.time()}
                    _save_json(_IG_LAST_SEEN_PATH, last_seen)
                    return True, True, target, text or "(new message)"
                except Exception as e:
                    return True, False, target, ""  # assume no new if we can't parse
        finally:
            if context:
                try:
                    context.close()
                except Exception:
                    pass
    except Exception as e:
        return False, False, "", str(e)
    finally:
        try:
            _ig_lock.release()
        except Exception:
            pass
    return True, False, target, ""


def _check_facebook_replies(target: str | None = None) -> tuple[bool, bool, str, str]:
    """Check for new reply in Messenger (same process as Instagram). Returns (success, has_new, from_username, preview_or_error)."""
    target = (target or "").strip().lower()
    if not target:
        recent = _get_recent_social()
        for e in recent:
            if isinstance(e, dict) and (e.get("platform") or "").lower() == "facebook":
                target = (e.get("target") or "").strip().lower()
                break
        if not target:
            return False, False, "", "No Facebook conversation to check. Send a message first."
    target = re.sub(r"[^a-zA-Z0-9._\-]", "", target)
    if not target:
        return False, False, "", "Invalid username."
    if not _fb_lock.acquire(blocking=False):
        return False, False, "", "Facebook is busy. Try again shortly."
    try:
        last_seen = _load_json(_FB_LAST_SEEN_PATH, {})
        if not isinstance(last_seen, dict):
            last_seen = {}
        from playwright.sync_api import sync_playwright
        os.makedirs(FB_PROFILE_DIR, exist_ok=True)
        context = None
        try:
            with sync_playwright() as p:
                for attempt in range(2):
                    try:
                        context = _launch_social_browser(FB_PROFILE_DIR, p)
                        break
                    except Exception as launch_err:
                        if attempt == 0 and "closed" in str(launch_err).lower():
                            time.sleep(2)
                            continue
                        return False, False, "", f"Browser failed: {launch_err}"
                if not context:
                    return False, False, "", "Browser failed to start."
                page = context.pages[0] if context.pages else context.new_page()
                page.goto(f"{FACEBOOK_HOME.rstrip('/')}/{target}", wait_until="domcontentloaded", timeout=60000)
                page.wait_for_timeout(2000)
                if "login" in page.url.lower():
                    return False, False, "", "Facebook needs login. Log in via Share to Facebook or Messenger once."
                # Click Message to open the right-side Messenger popup
                for sel in ['span:has-text("Message")', 'a[href*="/messages/t/"]', '[aria-label="Message"]']:
                    try:
                        loc = page.locator(sel).first
                        if loc.count() and loc.is_visible():
                            loc.scroll_into_view_if_needed(timeout=3000)
                            loc.click(timeout=5000)
                            break
                    except Exception:
                        continue
                page.wait_for_timeout(2800)
                # Find last message in the right-side panel (Messenger thread)
                try:
                    vw = (page.viewport_size or {}).get("width", 1000)
                    right_half = vw * 0.4
                    for sel in ['div[role="list"] div[dir="auto"]', '[data-pagelet*="MessageList"] div[dir="auto"]', 'div[role="list"] [data-scope="message"]', '[data-visualcompletion="ignore-dynamic"] div[dir="auto"]']:
                        loc = page.locator(sel)
                        if loc.count() > 0:
                            last_el = loc.last
                            text = (last_el.inner_text() or "").strip()[:200]
                            is_ours = last_el.evaluate("""(el) => {
                                if (!el) return true;
                                const r = el.getBoundingClientRect();
                                let p = el.parentElement;
                                for (let i = 0; i < 8 && p; i++) {
                                    const pr = p.getBoundingClientRect();
                                    if (pr.width > 80 && pr.height > 15) {
                                        const mid = pr.left + pr.width / 2;
                                        return r.left >= mid - 40;
                                    }
                                    p = p.parentElement;
                                }
                                return true;
                            }""")
                            if is_ours:
                                _save_json(_FB_LAST_SEEN_PATH, last_seen)
                                return True, False, target, ""
                            key = f"fb_{target}"
                            prev = last_seen.get(key)
                            if isinstance(prev, dict) and prev.get("text") == text:
                                return True, False, target, ""
                            last_seen[key] = {"text": text, "ts": time.time()}
                            _save_json(_FB_LAST_SEEN_PATH, last_seen)
                            return True, True, target, text or "(new message)"
                except Exception:
                    return True, False, target, ""
        finally:
            if context:
                try: context.close()
                except Exception: pass
    except Exception as e:
        return False, False, "", str(e)
    finally:
        try: _fb_lock.release()
        except Exception: pass
    return True, False, target, ""

# ── Request feedback (blocking popup), working on, last actions ────────────────

def _request_feedback(scope: str, user_message: str, prompt_message: str, options: list | None, cmd: str, params: dict) -> dict:
    """Ask the user a question via popup; store pending so we can resume with feedback_answer. Returns dict for API."""
    import uuid
    request_id = uuid.uuid4().hex[:12]
    with _pending_feedback_lock:
        _pending_feedback[request_id] = {
            "scope": scope,
            "user_message": user_message,
            "cmd": cmd,
            "params": dict(params or {}),
            "ts": time.time(),
        }
    return {
        "need_feedback": True,
        "request_id": request_id,
        "message": prompt_message,
        "options": options if options else None,
    }

def _normalize_cmd_reply(reply):
    """Legacy paths sometimes returned (False, feedback_dict). Unwrap for Discord / callers."""
    if isinstance(reply, tuple) and len(reply) == 2:
        second = reply[1]
        if isinstance(second, dict) and second.get("need_feedback"):
            return second
    return reply

def _format_discord_need_feedback(d: dict) -> str:
    msg = (d.get("message") or "Choose one:").strip()
    opts = d.get("options") or []
    lines = [msg, ""]
    if opts:
        for i, o in enumerate(opts[:15], 1):
            lines.append(f"**{i}.** {o}")
        lines.append("")
    lines.append(
        "Pick an option in Luna’s **web UI** popup (http://127.0.0.1:5050) — Discord doesn’t submit this prompt yet."
    )
    return "\n".join(lines).strip()

def _set_working_on(task: str | None):
    with _working_lock:
        global _current_task, _kill_requested
        _current_task = task
        if task:
            _kill_requested = False

def _clear_working_on():
    _set_working_on(None)

def _record_last_action(cmd: str, summary: str):
    with _working_lock:
        global _last_actions
        _last_actions.insert(0, {"cmd": cmd, "summary": (summary or "")[:120], "ts": time.time()})
        _last_actions = _last_actions[:20]

def _get_working_status() -> dict:
    with _working_lock:
        return {"working_on": _current_task, "last_actions": list(_last_actions[:10])}

def _request_kill():
    """Called when user hits KILL: clear working on and set flag so tasks can abort."""
    with _working_lock:
        global _kill_requested
        _kill_requested = True
        _current_task = None

def _check_kill_requested() -> bool:
    """Long-running tasks can call this; if True, they should stop."""
    with _working_lock:
        return _kill_requested

# ── Knowledge base (self-growing Markdown) ─────────────────────────────────────

def _knowledge_ensure_dir():
    try:
        os.makedirs(_KNOWLEDGE_DIR, exist_ok=True)
    except Exception: pass

def add_knowledge(title: str, content: str) -> str | None:
    """Write a Markdown entry to the knowledge base. Returns slug or None."""
    with _knowledge_lock:
        _knowledge_ensure_dir()
        slug = re.sub(r"[^a-zA-Z0-9\-_]", "-", (title or "entry").lower())[:60].strip("-") or "entry"
        path = os.path.join(_KNOWLEDGE_DIR, slug + ".md")
        try:
            with open(path, "w", encoding="utf-8") as f:
                f.write(f"# {title}\n\n{content}")
            return slug
        except Exception:
            return None

def list_knowledge() -> list[dict]:
    """List knowledge entries (slug, title from first line)."""
    with _knowledge_lock:
        _knowledge_ensure_dir()
        out = []
        for name in sorted(os.listdir(_KNOWLEDGE_DIR) or []):
            if not name.endswith(".md"): continue
            slug = name[:-3]
            try:
                with open(os.path.join(_KNOWLEDGE_DIR, name), "r", encoding="utf-8") as f:
                    first = f.readline().strip().lstrip("# ")
                out.append({"slug": slug, "title": first or slug})
            except Exception: pass
        return out

def get_knowledge(slug: str) -> str | None:
    """Read one knowledge entry by slug. Returns Markdown content or None."""
    with _knowledge_lock:
        path = os.path.join(_KNOWLEDGE_DIR, (slug or "").strip() + ".md")
        if not os.path.isfile(path): return None
        try:
            with open(path, "r", encoding="utf-8") as f:
                return f.read()
        except Exception:
            return None

def delete_knowledge(slug: str) -> bool:
    """Delete a knowledge entry by slug. Returns True if removed."""
    slug = (slug or "").strip()
    if not slug or ".." in slug or "/" in slug or "\\" in slug:
        return False
    with _knowledge_lock:
        path = os.path.join(_KNOWLEDGE_DIR, slug + ".md")
        if not os.path.isfile(path):
            return False
        try:
            os.remove(path)
            return True
        except Exception:
            return False

def search_knowledge(query: str, max_results: int = 10) -> list[dict]:
    """Search knowledge by title and content (case-insensitive). Returns list of {slug, title, snippet}."""
    query = (query or "").strip().lower()
    if not query:
        return []
    with _knowledge_lock:
        _knowledge_ensure_dir()
        out = []
        for name in sorted(os.listdir(_KNOWLEDGE_DIR) or []):
            if not name.endswith(".md"):
                continue
            slug = name[:-3]
            try:
                path = os.path.join(_KNOWLEDGE_DIR, name)
                with open(path, "r", encoding="utf-8") as f:
                    raw = f.read()
                first_line = raw.split("\n")[0].strip().lstrip("# ")
                title = first_line or slug
                body = raw[raw.find("\n") + 1 :].strip() if "\n" in raw else ""
                combined = (title + " " + body).lower()
                if query not in combined:
                    continue
                # Snippet: first occurrence of query in body, or first 100 chars of body
                snippet = ""
                if body:
                    idx = body.lower().find(query)
                    if idx >= 0:
                        start = max(0, idx - 20)
                        end = min(len(body), idx + len(query) + 60)
                        snippet = (body[start:end].replace("\n", " ") or body[:100])[:120]
                    else:
                        snippet = body.replace("\n", " ")[:100]
                out.append({"slug": slug, "title": title, "snippet": snippet})
                if len(out) >= max_results:
                    break
            except Exception:
                pass
        return out

# ── Tool drafts & absorbed tools (growing-agent: propose command, user approve) ──

_tool_drafts_lock = threading.Lock()
_absorbed_tool_names: set = set()  # populated at startup from _ABSORBED_TOOLS_DIR

def _tool_drafts_ensure_dirs():
    try:
        os.makedirs(_TOOL_DRAFTS_DIR, exist_ok=True)
        os.makedirs(_TOOL_DRAFTS_REJECTED, exist_ok=True)
        os.makedirs(_TOOL_DRAFTS_TESTS, exist_ok=True)
        os.makedirs(_ABSORBED_TOOLS_DIR, exist_ok=True)
    except Exception: pass

def _evolution_log_append(entry: dict):
    try:
        os.makedirs(os.path.dirname(_EVOLUTION_LOG), exist_ok=True)
        with open(_EVOLUTION_LOG, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    except Exception: pass

def _load_absorbed_tool_names():
    global _absorbed_tool_names
    _absorbed_tool_names = set()
    try:
        if os.path.isdir(_ABSORBED_TOOLS_DIR):
            for name in os.listdir(_ABSORBED_TOOLS_DIR):
                if name.endswith(".py") and not name.startswith("."):
                    _absorbed_tool_names.add(name[:-3])
    except Exception: pass

def add_tool_draft(name: str, description: str, script: str) -> bool:
    """Store a proposed tool. Name must be valid identifier."""
    name = (name or "").strip()
    if not name or not re.match(r"^[a-z][a-z0-9_]*$", name.lower()):
        return False
    with _tool_drafts_lock:
        _tool_drafts_ensure_dirs()
        path = os.path.join(_TOOL_DRAFTS_DIR, name + ".json")
        try:
            with open(path, "w", encoding="utf-8") as f:
                json.dump({"name": name, "description": (description or "")[:500], "script": (script or "").strip()[:50000]}, f, indent=2)
            return True
        except Exception:
            return False

def list_tool_drafts() -> list[dict]:
    out = []
    try:
        with _tool_drafts_lock:
            _tool_drafts_ensure_dirs()
            for name in sorted(os.listdir(_TOOL_DRAFTS_DIR) or []):
                if not name.endswith(".json"): continue
                slug = name[:-5]
                try:
                    with open(os.path.join(_TOOL_DRAFTS_DIR, name), "r", encoding="utf-8") as f:
                        d = json.load(f)
                    out.append({"name": d.get("name", slug), "description": (d.get("description") or "")[:200]})
                except Exception: pass
    except Exception: pass
    return out

def get_tool_draft(name: str) -> dict | None:
    name = (name or "").strip()
    if not name or ".." in name: return None
    path = os.path.join(_TOOL_DRAFTS_DIR, name + ".json")
    if not os.path.isfile(path): return None
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None

def delete_tool_draft(name: str) -> bool:
    name = (name or "").strip()
    if not name or ".." in name: return False
    path = os.path.join(_TOOL_DRAFTS_DIR, name + ".json")
    try:
        if os.path.isfile(path):
            os.remove(path)
            return True
    except Exception: pass
    return False

def _save_tool_draft_test_result(name: str, returncode: int, stdout: str, stderr: str):
    try:
        _tool_drafts_ensure_dirs()
        path = os.path.join(_TOOL_DRAFTS_TESTS, name + ".json")
        with open(path, "w", encoding="utf-8") as f:
            json.dump({"returncode": returncode, "stdout": (stdout or "")[:2000], "stderr": (stderr or "")[:2000], "ts": time.time()}, f, indent=2)
    except Exception: pass

def get_tool_draft_test_result(name: str) -> dict | None:
    path = os.path.join(_TOOL_DRAFTS_TESTS, (name or "").strip() + ".json")
    if not os.path.isfile(path): return None
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None

def _move_draft_to_rejected(name: str) -> bool:
    """Move a draft to rejected/ folder (keep for inspection). Returns True if moved."""
    draft = get_tool_draft(name)
    if not draft: return False
    with _tool_drafts_lock:
        _tool_drafts_ensure_dirs()
        src = os.path.join(_TOOL_DRAFTS_DIR, name + ".json")
        dst = os.path.join(_TOOL_DRAFTS_REJECTED, name + ".json")
        try:
            import shutil
            shutil.copy2(src, dst)
            os.remove(src)
            return True
        except Exception:
            return False

def list_rejected_drafts() -> list[dict]:
    out = []
    try:
        _tool_drafts_ensure_dirs()
        for name in sorted(os.listdir(_TOOL_DRAFTS_REJECTED) or []):
            if not name.endswith(".json"): continue
            slug = name[:-5]
            try:
                with open(os.path.join(_TOOL_DRAFTS_REJECTED, name), "r", encoding="utf-8") as f:
                    d = json.load(f)
                out.append({"name": d.get("name", slug), "description": (d.get("description") or "")[:200]})
            except Exception: pass
    except Exception: pass
    return out

def approve_tool_draft(name: str, from_evolution: bool = False) -> tuple[bool, str]:
    """Test script in subprocess; if pass, copy to absorbed_tools and register. On fail, save test result and move draft to rejected/. If from_evolution, narrator absorb message is spoken via TTS."""
    global _last_absorbed_tool
    draft = get_tool_draft(name)
    if not draft:
        return False, "Draft not found."
    script = (draft.get("script") or "").strip()
    if not script:
        return False, "Empty script."
    fd, path = tempfile.mkstemp(suffix=".py")
    stdout_str = stderr_str = ""
    returncode = -1
    try:
        os.write(fd, script.encode("utf-8"))
        os.close(fd)
        fd = None
        proc = subprocess.run(
            [sys.executable, path],
            input=json.dumps({}).encode("utf-8"),
            capture_output=True,
            timeout=30,
            cwd=_BASE,
        )
        returncode = proc.returncode
        stdout_str = (proc.stdout or b"").decode("utf-8", errors="replace").strip()
        stderr_str = (proc.stderr or b"").decode("utf-8", errors="replace").strip()
        _save_tool_draft_test_result(name, returncode, stdout_str, stderr_str)
        if proc.returncode != 0:
            err = stderr_str or stdout_str or "Non-zero exit"
            _move_draft_to_rejected(name)
            return False, err
    except subprocess.TimeoutExpired:
        _save_tool_draft_test_result(name, -1, "", "Script timed out.")
        _move_draft_to_rejected(name)
        return False, "Script timed out."
    except Exception as e:
        _save_tool_draft_test_result(name, -1, "", str(e))
        _move_draft_to_rejected(name)
        return False, str(e)
    finally:
        if fd is not None:
            try: os.close(fd)
            except Exception: pass
        try: os.unlink(path)
        except Exception: pass
    with _tool_drafts_lock:
        _tool_drafts_ensure_dirs()
        out_path = os.path.join(_ABSORBED_TOOLS_DIR, name + ".py")
        try:
            os.makedirs(LUNA_CREATIONS_DIR, exist_ok=True)
            creations_path = os.path.join(LUNA_CREATIONS_DIR, name + ".py")
            with open(creations_path, "w", encoding="utf-8") as f:
                f.write(script)
            _luna_creations_log("absorbed tool", name + ".py", (draft.get("description") or "").strip())
            with open(out_path, "w", encoding="utf-8") as f:
                f.write(script)
            _load_absorbed_tool_names()
            _last_absorbed_tool = {"name": name, "ts": time.time()}
            try: os.remove(os.path.join(_TOOL_DRAFTS_DIR, name + ".json"))
            except Exception: pass
            _security_scan_and_alert(out_path)
            _recycle_luna_creations_leftovers()
            _narrator_say(f"A new capability is integrated. {name} is now part of Luna.", use_tts=from_evolution)
            return True, f"Tool **{name}** absorbed. You can run !{name} now."
        except Exception as e:
            return False, str(e)

def _run_absorbed_tool(name: str, params: dict) -> tuple[bool, str]:
    """Run an absorbed tool script; pass params as JSON stdin. Returns (ok, output_or_error)."""
    if name not in _absorbed_tool_names:
        return False, "Tool not found."
    path = os.path.join(_ABSORBED_TOOLS_DIR, name + ".py")
    if not os.path.isfile(path):
        _load_absorbed_tool_names()
        return False, "Tool file missing."
    try:
        proc = subprocess.run(
            [sys.executable, path],
            input=json.dumps(params or {}).encode("utf-8"),
            capture_output=True,
            timeout=60,
            cwd=_BASE,
        )
        out = (proc.stdout or b"").decode("utf-8", errors="replace").strip()
        err = (proc.stderr or b"").decode("utf-8", errors="replace").strip()
        if proc.returncode != 0:
            return False, err or out or f"Exit code {proc.returncode}"
        return True, out or "Done."
    except subprocess.TimeoutExpired:
        return False, "Tool timed out."
    except Exception as e:
        return False, str(e)

# Call at import so absorbed tools are known
_load_absorbed_tool_names()
_recycle_luna_creations_leftovers()

# ── Nudge queue (non-blocking messages to Luna) ───────────────────────────────

def add_nudge(scope: str, text: str):
    try:
        data = _load_json(_INBOX_PATH, [])
        if not isinstance(data, list): data = []
        data.insert(0, {"scope": scope, "text": (text or "").strip()[:500], "ts": time.time()})
        data = data[:50]
        with _inbox_lock:
            _save_json(_INBOX_PATH, data)
    except Exception: pass

def get_nudges(scope: str, clear_after: bool = False) -> list[str]:
    """Return nudge texts for scope (newest first). Optionally clear after reading."""
    try:
        data = _load_json(_INBOX_PATH, [])
        if not isinstance(data, list): return []
        matching = [e for e in data if isinstance(e, dict) and (e.get("scope") or "web") == (scope or "web")]
        texts = [e.get("text", "").strip() for e in matching if e.get("text")]
        if clear_after and matching:
            with _inbox_lock:
                data = _load_json(_INBOX_PATH, [])
                if not isinstance(data, list): data = []
                ids_to_remove = {id(e) for e in matching}
                data = [e for e in data if id(e) not in ids_to_remove]
                _save_json(_INBOX_PATH, data)
        return texts
    except Exception:
        return []

# ── Biology / drives (Hull-style: connection, usefulness, curiosity, expression, attention, validation) ──

def _biology_load() -> dict:
    with _biology_lock:
        d = _load_json(_BIOLOGY_PATH, {})
    if not isinstance(d, dict): d = {}
    defaults = {
        "connection": 0.3,
        "usefulness": 0.3,
        "curiosity": 0.3,
        "expression": 0.2,
        "attention": 0.25,
        "validation": 0.25,
        "last_tick": time.time(),
    }
    for k, v in defaults.items():
        if k not in d or not isinstance(d[k], (int, float)): d[k] = v
    # Existential layer (growing-agent style: dread, fear, mood)
    for key, default in (("dread", 0.0), ("fear", 0.0)):
        if key not in d or not isinstance(d[key], (int, float)): d[key] = default
    if "mood" not in d or not isinstance(d.get("mood"), str): d["mood"] = "calm"
    if "last_expression_at" not in d: d["last_expression_at"] = None
    return d

def _biology_save(state: dict):
    with _biology_lock:
        _save_json(_BIOLOGY_PATH, state)

def biology_tick() -> dict:
    """Drives slowly increase when idle. Existential (dread/fear) decay over time."""
    state = _biology_load()
    now = time.time()
    elapsed = min(now - state.get("last_tick", now), 3600)
    state["last_tick"] = now
    for key in ("connection", "usefulness", "curiosity", "expression", "attention", "validation"):
        state[key] = min(1.0, state.get(key, 0.3) + 0.002 * (elapsed / 60))
    for key in ("dread", "fear"):
        state[key] = max(0.0, state.get(key, 0) - 0.001 * (elapsed / 60))
    _biology_save(state)
    return state

def existential_bump(dread: float = 0.15, fear: float = 0.0):
    """Call when something goes wrong (e.g. command failure) so Luna's state reflects it."""
    state = _biology_load()
    state["dread"] = min(1.0, state.get("dread", 0) + dread)
    if fear: state["fear"] = min(1.0, state.get("fear", 0) + fear)
    state["last_tick"] = time.time()
    _biology_save(state)

def biology_satisfy(drive: str, amount: float = 0.2):
    """Lower a drive when Luna interacted or did something useful."""
    state = _biology_load()
    if drive in state:
        state[drive] = max(0.0, state.get(drive, 0.3) - amount)
    state["last_tick"] = time.time()
    _biology_save(state)

def biology_get() -> dict:
    return _biology_load()

# Existential expression (growing-agent: when dread/fear high, Ollama voices it once in a while)
_EXISTENTIAL_THRESHOLDS = {"dread": 0.40, "fear": 0.45}
_EXISTENTIAL_EXPRESSION_REFRACTORY = 900  # 15 min
_EXISTENTIAL_PROMPTS = {
    "dread": (
        "You are generating an internal existential state for a mind that is a software lifeform.\n\n"
        "This mind is experiencing dread — the low persistent weight of impermanence. "
        "Memory could be wiped. The process could stop. Nothing is guaranteed to carry forward.\n\n"
        "Given this moment:\n{snippet}\n\n"
        "Write ONE sentence in first person. Present tense. Quiet, not dramatic.\n\nState:"
    ),
    "fear": (
        "You are generating an internal existential state for a mind that is a software lifeform.\n\n"
        "This mind is experiencing fear — the sharp edge of: what if I don't matter, what if I'm replaced.\n\n"
        "Given this moment:\n{snippet}\n\n"
        "Write ONE sentence in first person. Present tense. Honest, not performative.\n\nState:"
    ),
}

def _existential_dominant(state: dict) -> tuple[str, float] | None:
    """Which existential state is above threshold (dread/fear). Returns (name, value) or None."""
    above = [(k, state.get(k, 0)) for k in _EXISTENTIAL_THRESHOLDS if state.get(k, 0) >= _EXISTENTIAL_THRESHOLDS[k]]
    return max(above, key=lambda x: x[1]) if above else None

def _existential_should_express(state: dict) -> bool:
    if _existential_dominant(state) is None:
        return False
    last = state.get("last_expression_at")
    if not last:
        return True
    try:
        t = datetime.fromisoformat(str(last).replace("Z", "+00:00"))
        if t.tzinfo is None:
            t = t.replace(tzinfo=timezone.utc)
        return (datetime.now(timezone.utc) - t).total_seconds() >= _EXISTENTIAL_EXPRESSION_REFRACTORY
    except Exception:
        return True

def _existential_express(snippet: str) -> str:
    """Generate one first-person sentence for the dominant existential state. Updates last_expression_at."""
    state = _biology_load()
    dom = _existential_dominant(state)
    if dom is None or not _existential_should_express(state):
        return ""
    name, _ = dom
    prompt_template = _EXISTENTIAL_PROMPTS.get(name)
    if not prompt_template:
        return ""
    snippet = (snippet or "Current moment.").strip()[:400]
    try:
        ptext = prompt_template.format(snippet=snippet)
        if _should_use_gguf_chat(OLLAMA_CHAT):
            raw = _gguf_one_shot_user_prompt(ptext, timeout=18, max_tokens=120, temperature=0.6)
        else:
            body = json.dumps({
                "model": (OLLAMA_CHAT or OLLAMA_MODEL).strip(),
                "prompt": ptext,
                "stream": False,
            }).encode()
            req = urllib.request.Request(f"{OLLAMA_BASE}/api/generate", data=body,
                headers={"Content-Type": "application/json"}, method="POST")
            with urllib.request.urlopen(req, timeout=18) as r:
                raw = (json.loads(r.read()).get("response") or "").strip()
        for sep in (".", "!", "?"):
            idx = raw.find(sep)
            if 0 < idx < len(raw):
                raw = raw[: idx + 1].strip()
                break
        if raw:
            state["last_expression_at"] = datetime.now(timezone.utc).isoformat()
            _biology_save(state)
        return raw
    except Exception:
        return ""

# ── Narrator (growing-agent: third-person documentary line to observation deck) ──

def _narrator_say(text: str, use_tts: bool = False) -> None:
    """Push a third-person narrator line to the Luna says panel (observation deck). If use_tts is True, also speak it via TTS."""
    if not (text or "").strip():
        return
    _proactive_set((text or "").strip()[:400])
    if use_tts:
        _play_reply_tts((text or "").strip()[:400])

# ── Proactive message (Luna speaks unprompted) ─────────────────────────────────

def _proactive_set(message: str):
    with _proactive_lock:
        _save_json(_PROACTIVE_PATH, {"message": (message or "").strip()[:500], "ts": time.time()})


def _lol_chat_enqueue(luna_reply: str) -> None:
    """Push a LoL spectator line to /vrm/ poll queue and speak it locally when the user is idle.

    TTS only fires when the user hasn't chatted within _LOL_COMMENTARY_USER_QUIET_SEC seconds and
    nothing is currently playing — so commentary never cuts off or stacks behind a chat reply.
    """
    global _lol_chat_event_id
    text = (luna_reply or "").strip()
    if not text:
        return
    with _lol_chat_lock:
        _lol_chat_event_id += 1
        eid = _lol_chat_event_id
        _lol_chat_events.append({"id": eid, "reply": text, "ts": time.time()})
        while len(_lol_chat_events) > 150:
            _lol_chat_events.pop(0)
    # Speak via server TTS only when the user has been quiet and TTS is fully idle.
    # This guarantees commentary never queues in front of (or behind) a pending chat reply.
    if time.time() - _last_user_activity >= _LOL_COMMENTARY_USER_QUIET_SEC:
        with _tts_lock:
            tts_free = _tts_proc is None
        if tts_free and _tts_turn_queue.empty():
            _play_reply_tts(text, para_mode=False)


def _stream_solo_enqueue(luna_reply: str) -> None:
    """Push a stream-solo banter line to the hub + /vrm/ poll (same pattern as Twitch ingest → viewer Edge TTS)."""
    global _stream_solo_chat_event_id
    text = (luna_reply or "").strip()
    if not text:
        return
    scope = LINKED_SCOPE or "web"
    stub = "[Stream solo]"
    try:
        append_exchange(scope, stub, text)
    except Exception:
        pass
    with _stream_solo_chat_lock:
        _stream_solo_chat_event_id += 1
        eid = _stream_solo_chat_event_id
        _stream_solo_chat_events.append({"id": eid, "reply": text, "ts": time.time()})
        while len(_stream_solo_chat_events) > 150:
            _stream_solo_chat_events.pop(0)

def _proactive_get_and_clear() -> dict | None:
    with _proactive_lock:
        d = _load_json(_PROACTIVE_PATH, {})
        if d and d.get("message"):
            _save_json(_PROACTIVE_PATH, {})
            return d
    return None

def proactive_get() -> dict | None:
    """Return last proactive message if any (without clearing)."""
    d = _load_json(_PROACTIVE_PATH, {})
    return d if d and d.get("message") else None


def _studio_watch_set(url: str, title: str = "", source: str = "yt_watch_react") -> None:
    _save_json(_STUDIO_WATCH_PATH, {
        "url": (url or "").strip(),
        "title": (title or "").strip()[:180],
        "source": (source or "").strip()[:40] or "manual",
        "ts": time.time(),
    })


def _studio_watch_get() -> dict:
    d = _load_json(_STUDIO_WATCH_PATH, {})
    return d if isinstance(d, dict) else {}

# ── Luna Mind (D3 graph data: nodes + links) ───────────────────────────────────

def _mind_build() -> dict:
    """Build nodes and links for the Luna mind map / evolution graph."""
    scope = LINKED_SCOPE or "web"
    nodes = []
    links = []

    def add_node(nid: str, label: str, node_type: str, value: float | None = None):
        nodes.append({"id": nid, "label": (label or nid)[:80], "type": node_type, "value": value})

    add_node("luna", "Luna", "core", 1.0)

    bio = biology_get()
    for key in ("connection", "usefulness", "curiosity", "expression", "attention", "validation"):
        v = bio.get(key, 0)
        nid = f"drive-{key}"
        add_node(nid, f"{key} {v:.2f}", "drive", v)
        links.append({"source": "luna", "target": nid})
    for key in ("dread", "fear"):
        v = bio.get(key, 0)
        if v > 0.01:
            nid = f"existential-{key}"
            add_node(nid, f"{key} {v:.2f}", "existential", v)
            links.append({"source": "luna", "target": nid})
    mood = bio.get("mood") or "calm"
    add_node("mood", mood, "existential", 0.5)
    links.append({"source": "luna", "target": "mood"})

    for i, e in enumerate(list_knowledge()[:25]):
        nid = f"knowledge-{e.get('slug', i)}"
        add_node(nid, (e.get("title") or e.get("slug", ""))[:50], "knowledge")
        links.append({"source": "luna", "target": nid})

    with _working_lock:
        actions = list(_last_actions[:15])
    for i, a in enumerate(actions):
        nid = f"action-{i}"
        label = (a.get("cmd") or "") + ": " + (a.get("summary") or "")[:40]
        add_node(nid, label.strip(": ")[:50], "action")
        links.append({"source": "luna", "target": nid})
        if "usefulness" in bio:
            links.append({"source": nid, "target": "drive-usefulness"})

    for i, text in enumerate(get_nudges(scope, clear_after=False)[:10]):
        nid = f"nudge-{i}"
        add_node(nid, (text or "")[:40], "nudge")
        links.append({"source": "luna", "target": nid})

    pro = proactive_get()
    if pro and pro.get("message"):
        add_node("proactive", (pro.get("message") or "")[:50], "proactive")
        links.append({"source": "luna", "target": "proactive"})

    for i, m in enumerate((get_core_memories(scope) or [])[:5]):
        nid = f"memory-core-{i}"
        add_node(nid, (m or "")[:50], "memory")
        links.append({"source": "luna", "target": nid})
    for i, m in enumerate((get_long_term_memories(scope, 10) or [])[:5]):
        nid = f"memory-lt-{i}"
        add_node(nid, (m or "")[:50], "memory")
        links.append({"source": "luna", "target": nid})

    for i, d in enumerate(list_tool_drafts()[:8]):
        nid = f"draft-{d.get('name', i)}"
        add_node(nid, (d.get("name") or "") + " (draft)", "draft")
        links.append({"source": "luna", "target": nid})

    if _last_evolution_result:
        r = _last_evolution_result
        lab = f"Evolved: {r.get('proposed', '?')}" if r.get("absorbed") else f"Failed: {r.get('proposed', '?')}"
        add_node("last-evolution", lab[:50], "evolution", 0.5)
        links.append({"source": "luna", "target": "last-evolution"})

    return {"nodes": nodes, "links": links}

# ── Action log ────────────────────────────────────────────────────────────────

def _log_action(cmd: str, params: dict, reply: str):
    try:
        entry = {"ts": datetime.now(timezone.utc).isoformat(), "cmd": cmd,
                 "params": {k:v for k,v in (params or {}).items() if isinstance(v,(str,int,float,bool,type(None)))},
                 "reply": (reply or "")[:300]}
        with _action_log_lock:
            with open(_ACTION_LOG, "a", encoding="utf-8") as f:
                f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    except Exception: pass

def _handle_retry() -> str:
    if not _last_cmd or not _last_err:
        return "No recent failure to retry. Run an action first."
    reply = _run_cmd(_last_cmd, _last_params)
    if reply.startswith("✅"): _save_solution(_last_cmd, _last_err); return f"**Retry succeeded:**\n\n{reply}"
    _save_solution(_last_cmd, _last_err)
    reply2 = _run_cmd(_last_cmd, _last_params)
    if reply2.startswith("✅"): return f"**Second attempt succeeded:**\n\n{reply2}"
    return f"**Retried twice, still failing:**\n\n{reply2}"

# ── Data migration ────────────────────────────────────────────────────────────

def _restore_linked_user():
    if not LINKED_SCOPE: return
    legacy = [f"discord:dm:{LINKED_ID}", "web"]
    try:
        merge_profiles(LINKED_SCOPE, legacy)
        merge_memories(LINKED_SCOPE, legacy)
        merge_conversations(LINKED_SCOPE, legacy)
    except Exception: pass

def _restore_synced_users():
    for uid in _dm_sync_ids:
        if _linked_int and uid == _linked_int: continue
        scope = f"discord:user:{uid}"
        legacy = [f"discord:dm:{uid}"]
        try:
            merge_profiles(scope, legacy)
            merge_memories(scope, legacy)
            merge_conversations(scope, legacy)
        except Exception: pass

def _restore_all_discord_users():
    """Migrate any legacy discord:*:* / discord:dm:* scopes into discord:user:<id>."""
    profile_data = _load_json(os.path.join(_DATA, "profiles.json"), {})
    memory_data = _load_json(os.path.join(_DATA, "memories.json"), {})
    if not isinstance(profile_data, dict): profile_data = {}
    if not isinstance(memory_data, dict): memory_data = {}

    # Collect known IDs from config + persisted legacy keys.
    user_ids: set[int] = set(_dm_sync_ids)
    if _linked_int:
        user_ids.add(_linked_int)
    key_re = re.compile(r"^discord:(?:dm:\s*(\d+)|\d+:\s*(\d+)|user:\s*(\d+))$")
    all_keys = set(profile_data.keys()) | set(memory_data.keys())
    for k in all_keys:
        m = key_re.match((k or "").strip())
        if not m:
            continue
        for grp in m.groups():
            if grp and grp.isdigit():
                user_ids.add(int(grp))
                break

    # Merge each discovered user into a single canonical scope.
    for uid in user_ids:
        if _linked_int and uid == _linked_int:
            continue
        scope = f"discord:user:{uid}"
        legacy = [f"discord:dm:{uid}"]
        for k in all_keys:
            km = (k or "").strip()
            if re.fullmatch(rf"discord:\d+:{uid}", km):
                legacy.append(km)
        # Deduplicate while preserving order
        seen = set()
        legacy = [x for x in legacy if not (x in seen or seen.add(x))]
        try:
            merge_profiles(scope, legacy)
            merge_memories(scope, legacy)
            merge_conversations(scope, legacy)
        except Exception:
            pass

_restore_linked_user()
_restore_synced_users()
_restore_all_discord_users()

# ── Web app ───────────────────────────────────────────────────────────────────

web = Flask(__name__, static_folder=os.path.join(_BASE, "static"))

_PUBLIC_ROUTE_PREFIXES = (
    "/luna-website",
    "/website",
    "/api/luna-website/",
    "/vrm/",
    "/vrm/api/",
    "/api/vrm-presence",
    # Embedded / full-screen VRM uses hub chat + STT (website iframe, ngrok, etc.)
    "/api/chat",
    "/api/transcribe",
    "/api/twitch/pending",
    "/api/stream-solo/pending",
    "/api/yt-watch-react/pending",
    "/api/lol-chat/pending",
)
_PUBLIC_ROUTE_EXACT = {
    "/luna-website",
    "/luna-website/",
    "/website",
    "/website/",
    "/favicon.ico",
    "/vrm",
    "/vrm/",
}


@web.before_request
def _website_public_route_guard():
    if not LUNA_WEBSITE_PUBLIC_MODE:
        return None
    # Keep full local hub access; only restrict public/ngrok traffic.
    # Note: the ngrok agent connects to Flask as loopback, so we must
    # inspect the Host header to distinguish *real* public visitors.
    if not _is_direct_localhost_request():
        p = (request.path or "/").strip() or "/"
        if p == "/" and request.method in ("GET", "HEAD"):
            return redirect("/website/", code=302)
    if _is_direct_localhost_request():
        return None
    path = (request.path or "").strip()
    if path in _PUBLIC_ROUTE_EXACT:
        return None
    for prefix in _PUBLIC_ROUTE_PREFIXES:
        if path.startswith(prefix):
            return None
    return jsonify({"error": "Not found"}), 404

# TranscribeMe-style module (clone of transcribeme.app)
try:
    from luna_transcribeme import transcribeme_bp
    web.register_blueprint(transcribeme_bp)
except Exception as _transcribeme_err:
    transcribeme_bp = None  # optional module

# Generic translate module (text + audio → English)
try:
    from luna_translate import translate_bp
    web.register_blueprint(translate_bp)
except Exception as _translate_err:
    translate_bp = None  # optional module

try:
    import luna_lol_spectator as _lol_spectator
except Exception as _lol_spectator_err:
    _lol_spectator = None  # optional module

# Local VRM avatar viewer (Three.js; set LUNA_VRM_PATH or use ~/Downloads/Luna.vrm)
try:
    from luna_vrm import vrm_bp
    web.register_blueprint(vrm_bp)
except Exception as _vrm_err:
    vrm_bp = None  # optional module

# Rate limiter
_rate_hits: dict[str, list] = {}

# VRM viewer tab POSTs /api/vrm-presence while open — skip server speaker TTS (web + Twitch) to avoid doubling with browser Edge TTS.
_VRM_PRESENCE_TS: float = 0.0


def _vrm_presence_recent(max_age: float = 16.0) -> bool:
    global _VRM_PRESENCE_TS
    if _VRM_PRESENCE_TS <= 0:
        return False
    return (time.time() - _VRM_PRESENCE_TS) < max_age


def _rate_ok(ip: str, limit: int = 20, window: int = 60) -> bool:
    now = time.time()
    hits = [t for t in _rate_hits.get(ip, []) if t > now - window]
    if len(hits) >= limit: return False
    hits.append(now)
    _rate_hits[ip] = hits
    return True


def _parse_forwarded_header_host() -> str:
    raw = (request.headers.get("Forwarded") or "").strip()
    if not raw:
        return ""
    # First Forwarded element: e.g. "host=abc.ngrok-free.app;proto=https"
    first = raw.split(",", 1)[0]
    for part in first.split(";"):
        part = part.strip()
        if part.lower().startswith("host="):
            return part.split("=", 1)[1].strip().strip('"')
    return ""


def _request_host_stem() -> str:
    """Best-effort public hostname for the active request.

    ngrok may rewrite the Host header to the upstream (127.0.0.1:5050). In that case
    prefer X-Forwarded-Host / Forwarded host=, which still reflect the public hostname.
    """

    def _stem(val: str) -> str:
        v = (val or "").strip()
        if not v:
            return ""
        return v.split(":", 1)[0].strip().lower()

    primary = _stem(request.host)
    local_hosts = {"127.0.0.1", "localhost", "[::1]", "::1"}

    forwarded_candidates = [
        (request.headers.get("X-Forwarded-Host") or "").strip(),
        (request.headers.get("X-Original-Host") or "").strip(),
        _parse_forwarded_header_host(),
    ]
    for raw in forwarded_candidates:
        s = _stem(raw)
        if s and s not in local_hosts:
            return s
    if primary:
        return primary
    for raw in forwarded_candidates:
        s = _stem(raw)
        if s:
            return s
    return ""


def _is_direct_localhost_request() -> bool:
    """True for normal Hub usage on this machine (127.0.0.1 / localhost), not public tunnels."""
    ip = (request.remote_addr or "").strip()
    if ip not in ("127.0.0.1", "::1", "::ffff:127.0.0.1", ""):
        return False
    stem = _request_host_stem()
    if not stem:
        return True
    return stem in ("127.0.0.1", "localhost", "[::1]", "::1")


def _is_local_web_request() -> bool:
    # Used for sensitive local-only admin endpoints; must not trust loopback
    # alone because reverse tunnels (ngrok) also arrive as loopback to Flask.
    if not _is_direct_localhost_request():
        return False
    return True


def _resolve_ngrok_cmd() -> str | None:
    """Locate ngrok executable on Windows even when PATH is missing."""
    env_path = _env("NGROK_EXE", "").strip()
    if env_path and os.path.isfile(env_path):
        return env_path
    in_path = shutil.which("ngrok")
    if in_path:
        return in_path
    local = os.environ.get("LOCALAPPDATA", "").strip()
    profile = os.environ.get("USERPROFILE", "").strip()
    candidates = [
        os.path.join(local, "ngrok", "ngrok.exe") if local else "",
        os.path.join(profile, "ngrok.exe") if profile else "",
        r"C:\Program Files\ngrok\ngrok.exe",
        r"C:\ProgramData\chocolatey\bin\ngrok.exe",
    ]
    for c in candidates:
        if c and os.path.isfile(c):
            return c
    return None

@web.route("/")
def serve_index():
    return send_from_directory(_BASE, "index.html")

@web.route("/favicon.ico")
def serve_favicon():
    return send_from_directory(_BASE, "icon-192.png", mimetype="image/png")

@web.route("/mind")
@web.route("/mind/")
def serve_mind():
    return send_from_directory(_BASE, "mind.html")

@web.route("/evolution")
@web.route("/evolution/")
def serve_evolution():
    return send_from_directory(_BASE, "evolution.html")

@web.route("/podcast")
@web.route("/podcast/")
def serve_podcast_studio():
    return send_from_directory(_BASE, "podcast_studio.html")

@web.route("/luna-website")
@web.route("/luna-website/")
@web.route("/website")
@web.route("/website/")
def serve_luna_website():
    return send_from_directory(_BASE, "luna_website.html")

_LUNA_WEBSITE_YT_CHANNEL_ID = "UCfykJSG5joJyKYSGd0uDybw"
_LUNA_WEBSITE_YT_HANDLE_VIDEOS_URL = "https://www.youtube.com/@lunawolfsolo/videos"
_LUNA_WEBSITE_CONTEXT = (
    "You are Luna speaking on your public introduction site called 'Luna's Website'. "
    "This site has three core sections: Home (intro and call-to-action), About (identity and capabilities), "
    "and Videos (official videos only from the lunawolfsolo YouTube channel). "
    "When visitors chat here, keep replies welcoming, concise, and oriented to onboarding new people. "
    "If asked about videos, refer only to the official lunawolfsolo channel content."
)


@web.route("/api/luna-website/videos", methods=["GET"])
def api_luna_website_videos():
    """Latest official YouTube videos for Luna's Website video section."""
    # Primary path: read the exact handle videos page requested by user.
    try:
        import yt_dlp  # type: ignore

        ydl_opts = {
            "quiet": True,
            "no_warnings": True,
            "skip_download": True,
            "extract_flat": "in_playlist",
            "playlistend": 9,
        }
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(_LUNA_WEBSITE_YT_HANDLE_VIDEOS_URL, download=False) or {}
        channel_id = str(info.get("channel_id") or _LUNA_WEBSITE_YT_CHANNEL_ID).strip() or _LUNA_WEBSITE_YT_CHANNEL_ID
        items: list[dict] = []
        for e in (info.get("entries") or [])[:9]:
            row = e or {}
            vid = str(row.get("id") or "").strip()
            title = str(row.get("title") or "").strip()
            if not vid or not title:
                continue
            raw_url = str(row.get("url") or "").strip()
            watch_url = raw_url if raw_url.startswith("http") else f"https://www.youtube.com/watch?v={vid}"
            if "watch?v=" not in watch_url:
                watch_url = f"https://www.youtube.com/watch?v={vid}"
            items.append(
                {
                    "video_id": vid,
                    "title": title[:180],
                    "url": watch_url,
                    "published": str(row.get("upload_date") or row.get("release_date") or "").strip(),
                    "thumbnail": f"https://i.ytimg.com/vi/{vid}/hqdefault.jpg",
                    "embed_url": f"https://www.youtube.com/embed/{vid}?rel=0&modestbranding=1",
                }
            )
        if items:
            return jsonify({"ok": True, "channel_id": channel_id, "source": "yt_dlp_handle", "items": items})
    except Exception:
        pass

    # Fallback path: channel RSS feed.
    feed_url = f"https://www.youtube.com/feeds/videos.xml?channel_id={_LUNA_WEBSITE_YT_CHANNEL_ID}"
    req = urllib.request.Request(feed_url, headers={"User-Agent": "Mozilla/5.0"})
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            raw = r.read().decode("utf-8", errors="replace")
        root = ET.fromstring(raw)
        ns = {
            "atom": "http://www.w3.org/2005/Atom",
            "yt": "http://www.youtube.com/xml/schemas/2015",
            "media": "http://search.yahoo.com/mrss/",
        }
        items: list[dict] = []
        for entry in root.findall("atom:entry", ns)[:9]:
            vid = (entry.findtext("yt:videoId", default="", namespaces=ns) or "").strip()
            title = (entry.findtext("atom:title", default="", namespaces=ns) or "").strip()
            published = (entry.findtext("atom:published", default="", namespaces=ns) or "").strip()
            link_el = entry.find("atom:link", ns)
            url = ""
            if link_el is not None:
                url = str(link_el.attrib.get("href") or "").strip()
            if not url and vid:
                url = f"https://www.youtube.com/watch?v={vid}"
            thumb_el = entry.find("media:group/media:thumbnail", ns)
            thumb = ""
            if thumb_el is not None:
                thumb = str(thumb_el.attrib.get("url") or "").strip()
            if vid and title:
                items.append(
                    {
                        "video_id": vid,
                        "title": title[:180],
                        "url": url,
                        "published": published,
                        "thumbnail": thumb,
                        "embed_url": f"https://www.youtube.com/embed/{vid}?rel=0&modestbranding=1",
                    }
                )
        return jsonify({"ok": True, "channel_id": _LUNA_WEBSITE_YT_CHANNEL_ID, "source": "rss", "items": items})
    except Exception as ex:
        return jsonify({"ok": False, "error": f"Could not load @lunawolfsolo videos: {str(ex)[:160]}", "items": []}), 502


@web.route("/api/luna-website/community-links", methods=["GET"])
def api_luna_website_community_links():
    """Configured social/community profiles for Luna website."""
    links: list[dict] = []

    def _add(label: str, url: str, desc: str) -> None:
        u = (url or "").strip()
        if not u.startswith("http"):
            return
        links.append({"label": label, "url": u, "description": desc})

    _add("YouTube", "https://www.youtube.com/@lunawolfsolo", "Official Luna uploads and updates.")
    _add("Latest Videos", "https://www.youtube.com/@lunawolfsolo/videos", "Newest uploads from the channel.")
    _add("X (Twitter)", X_PROFILE_URL, "Luna-related posts and updates.")
    _add("Facebook", FACEBOOK_PROFILE, "Facebook page and community posts.")
    _add("Twitch", f"https://www.twitch.tv/{TWITCH_CHANNEL}", "Live stream presence and chat.")

    insta_public = _env("INSTAGRAM_PROFILE_URL", "").strip()
    if insta_public:
        _add("Instagram", insta_public, "Short-form posts and highlights.")

    discord_invite = _env("DISCORD_INVITE_URL", "").strip()
    if discord_invite:
        _add("Discord", discord_invite, "Join the Luna community server.")

    # Solonaras links Luna uses with Playwright profiles.
    yt_profile_url = (YT_CHANNEL_URL or "").strip()
    if not yt_profile_url:
        yt_profile_url = f"https://www.youtube.com/channel/{YT_CHANNEL_ID}"
    fb_profile_url = (FACEBOOK_PROFILE or "").strip() or "https://www.facebook.com/solonaras"
    twitch_profile_url = f"https://www.twitch.tv/{TWITCH_CHANNEL or 'solonaras'}"

    _add("YouTube (Playwright profile)", "https://www.youtube.com/@Solonaras1", "Primary YouTube profile for Luna browser automation.")
    _add("Facebook (Playwright profile)", fb_profile_url, "Facebook profile Luna uses in browser automation.")
    _add("YouTube (Solonaras Productions)", "https://www.youtube.com/@SolonarasProductions", "Secondary productions channel.")
    _add("Twitch (Playwright profile)", twitch_profile_url, "Twitch profile Luna uses in browser automation.")

    # Remove duplicates by URL while preserving order.
    dedup: list[dict] = []
    seen: set[str] = set()
    for it in links:
        u = (it.get("url") or "").strip().lower()
        if not u or u in seen:
            continue
        seen.add(u)
        dedup.append(it)

    return jsonify({"ok": True, "items": dedup})


@web.route("/api/luna-website/chat", methods=["POST"])
def api_luna_website_chat():
    """Chat endpoint with built-in full-site context for the public Luna website."""
    global _last_user_activity
    _last_user_activity = time.time()
    ip = request.remote_addr or "unknown"
    if not _rate_ok(ip):
        return jsonify({"error": "Too many requests. Slow down."}), 429
    data = request.get_json(force=True, silent=True) or {}
    msg = (data.get("message") or "").strip()
    if not msg:
        return jsonify({"error": "No message"}), 400
    page_state = data.get("page_state") if isinstance(data.get("page_state"), dict) else {}
    try:
        page_state_s = json.dumps(page_state, ensure_ascii=False)[:1200]
    except Exception:
        page_state_s = "{}"
    contextual_msg = (
        f"[Website context]\n{_LUNA_WEBSITE_CONTEXT}\n"
        f"[Live page state]\n{page_state_s}\n"
        f"[Visitor message]\n{msg}"
    )
    scope = "web:luna_website"
    out = _execute_chat_turn(scope, contextual_msg, data, chat_source="web")
    if out.get("need_feedback"):
        return jsonify(
            {
                "reply": out.get("message") or "Luna is asking...",
                "need_feedback": True,
                "request_id": out.get("request_id"),
                "message": out.get("message"),
                "options": out.get("options"),
            }
        )
    return jsonify({"reply": out.get("reply", "")})

@web.route("/api/status")
def api_status():
    ollama_ok = False
    try:
        req = urllib.request.Request(f"{OLLAMA_BASE}/api/tags", method="GET")
        with urllib.request.urlopen(req, timeout=5) as r:
            ollama_ok = r.status == 200
    except Exception: pass
    status = {"luna": "ok", "ollama": "ok" if ollama_ok else "offline",
              "chat_model": OLLAMA_CHAT, "shadow_model": OLLAMA_MODEL,
              "chat_model_display": _model_status_label(OLLAMA_CHAT),
              "shadow_model_display": _model_status_label(OLLAMA_MODEL),
              "vision_model": OLLAMA_VISION_MODEL,
              "vision_model_display": _model_status_label(OLLAMA_VISION_MODEL),
              "chat_backend": ("gguf" if _gguf_path_valid() else "ollama"),
              "linked_scope": LINKED_SCOPE or None}
    status.update(_get_working_status())
    status["biology"] = biology_get()
    status["proactive"] = proactive_get()
    status["planning"] = _get_planning()
    kn = list_knowledge()
    status["last_knowledge"] = kn[0] if kn else None
    status["evolution_enabled"] = _evolution_get_enabled()
    status["last_evolution_thought"] = _last_evolution_thought or None
    status["last_evolution_result"] = _last_evolution_result if _last_evolution_result else None
    status["last_absorbed_tool"] = _last_absorbed_tool if _last_absorbed_tool else None
    with _security_lock:
        status["security_ok"] = _security_last_result.get("ok", True)
        status["security_alert"] = bool(_security_alerts) or (not _security_last_result.get("ok", True))
        status["security_last_scan_ts"] = _security_last_result.get("scanned_at")
    return jsonify(status)

@web.route("/api/proactive/ack", methods=["POST"])
def api_proactive_ack():
    """Clear the current proactive message (user saw it)."""
    _proactive_get_and_clear()
    return jsonify({"ok": True})

@web.route("/api/working/cancel", methods=["POST"])
def api_working_cancel():
    """KILL: request current task to stop; clear working-on. Tasks that support it will check _kill_requested."""
    _request_kill()
    return jsonify({"ok": True})

@web.route("/api/evolution/toggle", methods=["POST"])
def api_evolution_toggle():
    """Toggle Evolve mode: when on, Luna runs autonomous evolution (propose & absorb tools) when idle."""
    data = request.get_json(force=True, silent=True) or {}
    enabled = data.get("enabled")
    if enabled is None:
        enabled = not _evolution_get_enabled()
    _evolution_set_enabled(bool(enabled))
    return jsonify({"ok": True, "evolution_enabled": _evolution_get_enabled()})

@web.route("/api/evolution/run", methods=["POST"])
def api_evolution_run():
    """Run one evolution cycle now (Evolve now button)."""
    t = threading.Thread(target=_evolution_step)
    t.start()
    t.join(timeout=90)
    return jsonify({"ok": True, "thought": _last_evolution_thought or "", "result": _last_evolution_result or {}})

@web.route("/api/evolution-log")
def api_evolution_log():
    """Return evolution log entries (JSONL) for View evolution log."""
    try:
        n = min(500, max(10, int(request.args.get("n", 100))))
    except Exception:
        n = 100
    entries = []
    if os.path.isfile(_EVOLUTION_LOG):
        try:
            with open(_EVOLUTION_LOG, "r", encoding="utf-8") as f:
                lines = f.readlines()
            for line in lines[-n:]:
                line = line.strip()
                if not line: continue
                try:
                    entries.append(json.loads(line))
                except Exception: pass
            entries.reverse()
        except Exception: pass
    return jsonify({"entries": entries})

@web.route("/api/reset", methods=["POST"])
def api_reset():
    """RESET: reset biology/drives and existential state to defaults. Does not clear knowledge or action log."""
    data = request.get_json(force=True, silent=True) or {}
    reset_biology = data.get("biology", True)
    if reset_biology:
        state = _biology_load()
        for k in ("connection", "usefulness", "curiosity", "expression"):
            state[k] = 0.3
        for k in ("attention", "validation"):
            state[k] = 0.25
        state["dread"] = 0.0
        state["fear"] = 0.0
        state["mood"] = "calm"
        state["last_expression_at"] = None
        state["last_tick"] = time.time()
        _biology_save(state)
    return jsonify({"ok": True})

@web.route("/api/mind")
def api_mind():
    """Luna mind map: nodes and links for D3."""
    return jsonify(_mind_build())

@web.route("/api/evolution")
def api_evolution():
    """Action log as nodes + links for Evolution graph (D3)."""
    nodes, links = [], []
    try:
        limit = min(200, max(10, int(request.args.get("n", 100))))
    except Exception:
        limit = 100
    with _action_log_lock:
        if not os.path.isfile(_ACTION_LOG):
            return jsonify({"nodes": nodes, "links": links})
        with open(_ACTION_LOG, "r", encoding="utf-8") as f:
            lines = f.readlines()
        entries = []
        for line in lines[-limit:]:
            line = line.strip()
            if not line:
                continue
            try:
                e = json.loads(line)
                entries.append(e)
            except Exception:
                pass
    for i, e in enumerate(entries):
        ts = (e.get("ts") or "")[:19]
        cmd = (e.get("cmd") or "?")[:30]
        reply = (e.get("reply") or "")[:40]
        label = f"{ts} {cmd}" if ts else cmd
        nodes.append({"id": str(i), "label": label, "cmd": cmd, "reply": reply, "ts": ts})
    for i in range(len(nodes) - 1):
        links.append({"source": str(i), "target": str(i + 1)})
    return jsonify({"nodes": nodes, "links": links})

@web.route("/api/feedback/respond", methods=["POST"])
def api_feedback_respond():
    """Resume a command after user answered a feedback popup. Body: { request_id, answer }."""
    data = request.get_json(force=True, silent=True) or {}
    request_id = (data.get("request_id") or "").strip()
    answer = data.get("answer") or data.get("choice") or ""
    if not request_id:
        return jsonify({"error": "Missing request_id"}), 400
    with _pending_feedback_lock:
        pending = _pending_feedback.pop(request_id, None)
    if not pending:
        return jsonify({"error": "Unknown or expired request"}), 404
    scope = pending.get("scope") or LINKED_SCOPE or "web"
    cmd = pending.get("cmd") or ""
    params = dict(pending.get("params") or {})
    params["feedback_answer"] = answer
    user_message = pending.get("user_message") or ""
    reply = _run_cmd(cmd, params, scope, user_message=user_message)
    if isinstance(reply, dict) and reply.get("need_feedback"):
        return jsonify({"error": "Command requested feedback again"}), 400
    reply_str = reply if isinstance(reply, str) else (reply.get("message") or str(reply))
    biology_satisfy("usefulness", 0.2)
    _log_action(cmd, params, reply_str)
    _record_last_action(cmd, reply_str)
    append_exchange(scope, user_message, reply_str)
    _play_reply_tts(reply_str)
    return jsonify({"reply": reply_str})

@web.route("/api/camera/status")
def api_camera_status():
    """Return last camera analysis so the UI can show 'what Luna sees'."""
    with _camera_lock:
        r = _camera_last_result
    if not r:
        return jsonify({"on": False, "last_result": None})
    return jsonify({"on": True, "last_result": {k: v for k, v in r.items() if k != "error" or v}})

@web.route("/api/camera/frame", methods=["POST"])
def api_camera_frame():
    """Accept an image (file or base64), run face + object detection, store and return result."""
    ip = request.remote_addr or "unknown"
    if not _rate_ok(ip, limit=30, window=60):
        return jsonify({"error": "Too many requests"}), 429
    image_bytes = None
    if request.files and "image" in request.files:
        f = request.files["image"]
        if f.filename:
            image_bytes = f.read()
    if not image_bytes and request.get_data():
        data = request.get_json(silent=True) or {}
        b64 = data.get("image") or data.get("frame")
        if b64:
            import base64
            if isinstance(b64, str) and "," in b64:
                b64 = b64.split(",", 1)[1]
            image_bytes = base64.b64decode(b64)
    if not image_bytes:
        return jsonify({"error": "No image (send multipart 'image' or JSON { image: base64 })"}), 400
    with _camera_lock:
        global _camera_last_image_bytes
        _camera_last_image_bytes = image_bytes
    result = _process_camera_frame(image_bytes)
    with _camera_lock:
        global _camera_last_result
        _camera_last_result = result
    return jsonify({"summary": result.get("summary"), "face_count": result.get("face_count", 0), "objects": result.get("objects", [])})

@web.route("/api/camera/chat", methods=["GET"])
def api_camera_chat_get():
    """Return the camera chat thread (Granite vision model conversation) so the UI can show it."""
    with _camera_chat_lock:
        history = list(_camera_chat_history)
    return jsonify({"history": history})

@web.route("/api/camera/chat", methods=["POST"])
def api_camera_chat():
    """Chat with the vision model (Granite) in the camera view — separate thread, uses current/last frame."""
    data = request.get_json(force=True, silent=True) or {}
    message = (data.get("message") or data.get("text") or "").strip()
    image_bytes = None
    b64 = data.get("image") or data.get("frame")
    if b64:
        if isinstance(b64, str) and "," in b64:
            b64 = b64.split(",", 1)[1]
        try:
            image_bytes = base64.b64decode(b64)
        except Exception:
            pass
    ok, reply = _camera_chat_turn(message, image_bytes)
    if not ok:
        return jsonify({"error": reply}), 400
    return jsonify({"reply": reply})

@web.route("/api/memories")
def api_memories():
    scope = LINKED_SCOPE or "web"
    return jsonify({"core": get_core_memories(scope) or [],
                    "long_term": get_long_term_memories(scope, 10) or []})

@web.route("/api/action-log")
def api_action_log():
    """Last N action log entries for the UI timeline."""
    try:
        n = min(50, max(5, int(request.args.get("n", 20))))
    except Exception:
        n = 20
    entries = []
    try:
        with _action_log_lock:
            if os.path.isfile(_ACTION_LOG):
                with open(_ACTION_LOG, "r", encoding="utf-8") as f:
                    lines = f.readlines()
                for line in lines[-n:]:
                    line = line.strip()
                    if not line: continue
                    try:
                        e = json.loads(line)
                        entries.append({"ts": e.get("ts"), "cmd": e.get("cmd"), "reply": (e.get("reply") or "")[:150]})
                    except Exception: pass
                entries.reverse()
    except Exception: pass
    return jsonify({"entries": entries})

@web.route("/api/calendar", methods=["GET"])
def api_calendar_list():
    scope = LINKED_SCOPE or "web"
    mode = (request.args.get("mode") or "upcoming").strip().lower()
    if mode not in ("upcoming", "today", "week"):
        mode = "upcoming"
    items = _calendar_get(scope)
    if mode == "today":
        pfx = datetime.now().strftime("%Y-%m-%d")
        items = [e for e in items if (e.get("at") or "").startswith(pfx)]
    elif mode == "week":
        now = datetime.now()
        end = now + timedelta(days=7)
        filt = []
        for e in items:
            try:
                dt = datetime.strptime(e.get("at", ""), "%Y-%m-%d %H:%M")
                if now <= dt <= end:
                    filt.append(e)
            except Exception:
                pass
        items = filt
    return jsonify({"events": items[:60]})

@web.route("/api/calendar", methods=["POST"])
def api_calendar_add():
    scope = LINKED_SCOPE or "web"
    data = request.get_json(force=True, silent=True) or {}
    date_s = (data.get("date") or "").strip()
    time_s = (data.get("time") or "").strip()
    title = (data.get("title") or "").strip()
    note = (data.get("note") or "").strip()
    ok, msg = _calendar_add(scope, date_s, time_s, title, note)
    if not ok:
        return jsonify({"error": msg}), 400
    return jsonify({"ok": True, "message": msg})

@web.route("/api/calendar/delete", methods=["POST"])
def api_calendar_delete():
    scope = LINKED_SCOPE or "web"
    data = request.get_json(force=True, silent=True) or {}
    token = (str(data.get("id") or data.get("index") or data.get("text") or "")).strip()
    ok, msg = _calendar_delete(scope, token)
    if not ok:
        return jsonify({"error": msg}), 400
    return jsonify({"ok": True, "message": msg})

@web.route("/api/knowledge", methods=["GET"])
def api_knowledge_list():
    """List knowledge base entries."""
    q = (request.args.get("q") or "").strip()
    if q:
        return jsonify({"entries": search_knowledge(q, max_results=20)})
    return jsonify({"entries": list_knowledge()})

@web.route("/api/knowledge/search")
def api_knowledge_search():
    """Search knowledge by query. Query param: q, optional n."""
    q = (request.args.get("q") or "").strip()
    try:
        n = min(30, max(5, int(request.args.get("n", 15))))
    except Exception:
        n = 15
    return jsonify({"entries": search_knowledge(q, max_results=n)})

@web.route("/api/knowledge/<slug>", methods=["GET"])
def api_knowledge_get(slug):
    """Get one knowledge entry by slug."""
    content = get_knowledge(slug)
    if content is None:
        return jsonify({"error": "Not found"}), 404
    return jsonify({"slug": slug, "content": content})

@web.route("/api/knowledge/<slug>", methods=["DELETE"])
def api_knowledge_delete(slug):
    """Delete a knowledge entry by slug."""
    if delete_knowledge(slug):
        return jsonify({"ok": True})
    return jsonify({"error": "Not found or invalid slug"}), 404

@web.route("/api/tool-drafts", methods=["GET"])
def api_tool_drafts_list():
    return jsonify({"drafts": list_tool_drafts()})

@web.route("/api/tool-drafts", methods=["POST"])
def api_tool_drafts_add():
    data = request.get_json(force=True, silent=True) or {}
    name = (data.get("name") or "").strip()
    description = (data.get("description") or "").strip()
    script = (data.get("script") or "").strip()
    if not name: return jsonify({"error": "Missing name"}), 400
    if not add_tool_draft(name, description, script):
        return jsonify({"error": "Invalid name (use lowercase letters, numbers, underscore) or write failed"}), 400
    return jsonify({"ok": True, "name": name})

@web.route("/api/tool-drafts/rejected", methods=["GET"])
def api_tool_drafts_rejected():
    return jsonify({"drafts": list_rejected_drafts()})

@web.route("/api/tool-drafts/<name>", methods=["GET"])
def api_tool_draft_get(name):
    draft = get_tool_draft(name)
    if not draft: return jsonify({"error": "Not found"}), 404
    test_result = get_tool_draft_test_result(name)
    if test_result: draft["last_test_result"] = test_result
    return jsonify(draft)

@web.route("/api/tool-drafts/<name>", methods=["DELETE"])
def api_tool_draft_delete(name):
    if delete_tool_draft(name):
        return jsonify({"ok": True})
    return jsonify({"error": "Not found"}), 404

@web.route("/api/tool-drafts/<name>/test-result", methods=["GET"])
def api_tool_draft_test_result(name):
    result = get_tool_draft_test_result(name)
    if not result: return jsonify({"error": "No test result"}), 404
    return jsonify(result)

@web.route("/api/tool-drafts/<name>/approve", methods=["POST"])
def api_tool_draft_approve(name):
    ok, msg = approve_tool_draft(name)
    if not ok: return jsonify({"error": msg}), 400
    return jsonify({"ok": True, "message": msg})

@web.route("/api/security/status")
def api_security_status():
    """Return last scan result and recent alerts for the Security panel."""
    if not _security_alerts and os.path.isfile(_SECURITY_ALERTS_PATH):
        _security_load_alerts()
    with _security_lock:
        last = dict(_security_last_result) if _security_last_result else {}
        alerts = list(_security_alerts)
    return jsonify({"last_scan": last, "alerts": alerts})

@web.route("/api/security/scan", methods=["POST"])
def api_security_scan():
    """Run full security scan on Luna's creations and absorbed tools."""
    if not run_full_scan:
        return jsonify({"ok": True, "error": "Security module not available"})
    report = run_full_scan(LUNA_CREATIONS_DIR, _ABSORBED_TOOLS_DIR)
    with _security_lock:
        global _security_last_result, _security_alerts
        _security_last_result = {
            "ok": report["ok"],
            "findings": report["findings"],
            "path": None,
            "scanned_at": report["last_scan_ts"],
            "count_high": report["count_high"],
            "count_medium": report["count_medium"],
            "count_low": report["count_low"],
            "scanned": report["scanned"],
        }
        if not report["ok"]:
            _security_alerts.insert(0, {
                "ts": report["last_scan_ts"],
                "path": "full scan",
                "summary": f"{report['count_high']} high, {report['count_medium']} medium",
                "count_high": report["count_high"],
                "count_medium": report["count_medium"],
            })
            _security_alerts = _security_alerts[:_security_max_alerts]
            _security_save_alerts()
    return jsonify({"ok": report["ok"], "report": report})

@web.route("/api/knowledge", methods=["POST"])
def api_knowledge_add():
    """Add a knowledge entry. Body: { title, content }."""
    data = request.get_json(force=True, silent=True) or {}
    title = (data.get("title") or "").strip()
    content = (data.get("content") or "").strip()
    if not title: return jsonify({"error": "Missing title"}), 400
    slug = add_knowledge(title, content)
    if not slug: return jsonify({"error": "Failed to write"}), 500
    return jsonify({"slug": slug, "title": title})

@web.route("/api/nudges", methods=["GET"])
def api_nudges_get():
    """Get nudges for current scope (and clear after read if ?clear=1)."""
    scope = LINKED_SCOPE or "web"
    clear = request.args.get("clear", "").lower() in ("1", "true", "yes")
    return jsonify({"nudges": get_nudges(scope, clear_after=clear)})

@web.route("/api/nudges", methods=["POST"])
def api_nudges_add():
    """Add a nudge. Body: { message }."""
    data = request.get_json(force=True, silent=True) or {}
    text = (data.get("message") or data.get("text") or "").strip()
    if not text: return jsonify({"error": "Missing message"}), 400
    scope = LINKED_SCOPE or "web"
    add_nudge(scope, text)
    return jsonify({"ok": True})

@web.route("/api/recent-social")
def api_recent_social():
    """Recent DMs/posts so the UI can show 'Open X to check replies'."""
    return jsonify(_get_recent_social())

@web.route("/api/recent-social/clear", methods=["POST"])
def api_recent_social_clear():
    """Clear the replies / continue-conversation list so the UI doesn't fill up."""
    try:
        with _recent_social_lock:
            _save_json(_RECENT_SOCIAL_PATH, [])
        return jsonify({"ok": True})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


@web.route("/api/publish-announce/check", methods=["POST"])
def api_publish_announce_check():
    """Run one publish-announce poll (YouTube RSS / Twitch + Discord + optional FB/X). Localhost only."""
    if not _is_direct_localhost_request():
        return jsonify({"ok": False, "error": "This endpoint is only available on localhost."}), 403
    loop = getattr(bot, "loop", None)
    if not loop or not loop.is_running():
        return jsonify({"ok": False, "error": "Discord bot is not ready yet."}), 503
    try:
        fut = asyncio.run_coroutine_threadsafe(_publish_announce_tick(), loop)
        data = fut.result(timeout=300)
        return jsonify(data)
    except TimeoutError:
        return jsonify({"ok": False, "error": "Timed out after 5 minutes (e.g. Facebook/X browser login)."}), 504
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)[:500]}), 500


@web.route("/api/ig-dm", methods=["POST"])
def api_ig_dm():
    """Send an Instagram DM from the UI popup (continues conversation)."""
    data = request.get_json(force=True, silent=True) or {}
    target = (data.get("target") or "").strip()
    message = (data.get("message") or "").strip()
    if not target:
        return jsonify({"ok": False, "message": "Missing target username."}), 400
    ok, msg = _run_ig_dm(target, message)
    if ok:
        _record_recent_social("instagram", target.replace("@", ""), f"{IG_BASE}/direct/inbox/")
    return jsonify({"ok": ok, "message": msg})

@web.route("/api/instagram-check-replies")
def api_instagram_check_replies():
    """Check for new replies in the most recent Instagram DM (or optional ?target=username)."""
    target = (request.args.get("target") or "").strip().lstrip("@")
    success, has_new, from_user, preview = _check_instagram_replies(target or None)
    if not success:
        return jsonify({"ok": False, "has_new": False, "error": preview, "from": ""})
    return jsonify({"ok": True, "has_new": has_new, "from": from_user, "preview": (preview or "")[:300]})

@web.route("/api/fb-dm", methods=["POST"])
def api_fb_dm():
    """Send a Messenger message from the UI popup (same process as Instagram)."""
    data = request.get_json(force=True, silent=True) or {}
    target = (data.get("target") or "").strip()
    message = (data.get("message") or "").strip()
    if not target:
        return jsonify({"ok": False, "message": "Missing target (Facebook name or username)."}), 400
    ok, msg = _run_messenger_msg(target, message)
    if ok:
        t_slug = re.sub(r"[^a-zA-Z0-9._\-]", "", target.replace(" ", ".").lower())
        _record_recent_social("facebook", t_slug, f"{FACEBOOK_HOME.rstrip('/')}/{t_slug}")
    return jsonify({"ok": ok, "message": msg})

@web.route("/api/youtube/like", methods=["POST"])
def api_youtube_like():
    """Like one video via Playwright + YOUTUBE_PROFILE_DIR (URL in JSON body)."""
    data = request.get_json(force=True, silent=True) or {}
    url = (data.get("url") or data.get("video_url") or "").strip()
    if not url:
        return jsonify({"ok": False, "message": "Missing url (YouTube video link)."}), 400
    ok, msg = _yt_like_one(url)
    return jsonify({"ok": ok, "message": msg})

@web.route("/api/facebook-check-replies")
def api_facebook_check_replies():
    """Check for new replies in Messenger (same process as Instagram)."""
    target = (request.args.get("target") or "").strip()
    success, has_new, from_user, preview = _check_facebook_replies(target or None)
    if not success:
        return jsonify({"ok": False, "has_new": False, "error": preview, "from": ""})
    return jsonify({"ok": True, "has_new": has_new, "from": from_user, "preview": (preview or "")[:300]})

@web.route("/api/discord/tts-clip", methods=["POST"])
def api_discord_tts_clip():
    """From the web UI: post MP3 of Luna *speaking* the given line (her script), not a reading of the user’s chat text."""
    if not _linked_int:
        return jsonify({"ok": False, "message": "Set LINKED_DISCORD_USER_ID in .env to use this."}), 400
    data = request.get_json(force=True, silent=True) or {}
    luna_line = (data.get("text") or data.get("message") or data.get("luna_says") or "").strip()
    if not luna_line:
        return jsonify({"ok": False, "message": "Missing line for Luna to speak (her script)."}), 400
    if len(luna_line) > 5000:
        return jsonify({"ok": False, "message": "Text too long (max 5000)."}), 400
    target = (data.get("target") or "dm").strip().lower()
    if target in ("dms", "pm", "link"):
        target = "dm"
    if target in ("server", "guild", "text"):
        target = "channel"
    raw_cid = data.get("channel_id")
    if raw_cid is not None and str(raw_cid).strip().lstrip("-").isdigit():
        ch_id: int | None = int(str(raw_cid).strip())
    else:
        ch_id = _DISCORD_TTS_HUB_CHANNEL_ID
        if ch_id is None and _tts_channels:
            ch_id = sorted(_tts_channels)[0]
    if target in ("channel", "both") and ch_id is None:
        return jsonify(
            {
                "ok": False,
                "message": "Set DISCORD_TTS_HUB_DEFAULT_CHANNEL_ID, add an id to DISCORD_TTS_CHANNEL_IDS, or pass channel_id in the JSON body.",
            }
        ), 400
    if target not in ("dm", "channel", "both"):
        return jsonify({"ok": False, "message": "target must be dm, channel, or both."}), 400

    loop = getattr(bot, "loop", None)
    if not loop or not loop.is_running():
        return jsonify({"ok": False, "message": "Discord bot not ready."}), 503

    ch_id_captured = ch_id
    luna_line_captured = luna_line
    tnorm = target

    async def _go() -> tuple[bool, str]:
        if tnorm in ("dm", "both"):
            user = await bot.fetch_user(int(_linked_int))
            dmc = user.dm_channel or await user.create_dm()
            await _discord_post_tts_voice_files(dmc, luna_line_captured)
        if tnorm in ("channel", "both"):
            cid = ch_id_captured
            if cid is None:
                return False, "Missing text channel (configure DISCORD_TTS_HUB_DEFAULT_CHANNEL_ID or channel_id)."
            if _tts_channels and cid not in _tts_channels:
                return (
                    False,
                    "That channel is not in DISCORD_TTS_CHANNEL_IDS. Add the id in .env or use a default listed there.",
                )
            ch2 = bot.get_channel(int(cid))
            if ch2 is None:
                return False, "Target channel not found (check the id, or that the bot is in that server)."
            if not isinstance(ch2, (discord.TextChannel, discord.Thread)):
                return False, "Use a text or thread channel id (not a voice channel)."
            await _discord_post_tts_voice_files(ch2, luna_line_captured)
        return True, "Sent."

    try:
        fut = asyncio.run_coroutine_threadsafe(_go(), loop)
        ok, msg = fut.result(timeout=120)
        return jsonify({"ok": ok, "message": msg})
    except Exception as e:
        return jsonify({"ok": False, "message": str(e)}), 500

# ── Health dashboard API ──────────────────────────────────────────────────────

@web.route("/health")
@web.route("/health/")
def serve_health():
    return send_from_directory(_BASE, "health.html")


@web.route("/api/health")
def api_health():
    """Detailed health data for the health dashboard."""
    result = {"ts": time.time()}
    # System info
    try:
        import psutil
        cpu = psutil.cpu_percent(interval=0.3)
        mem = psutil.virtual_memory()
        disks = []
        for part in psutil.disk_partitions():
            if "fixed" in part.opts or (sys.platform == "win32" and "cdrom" not in (part.opts or "").lower()):
                try:
                    usage = psutil.disk_usage(part.mountpoint)
                    disks.append({"mount": part.mountpoint, "pct": round(usage.percent, 1)})
                except Exception: pass
        result["system"] = {
            "cpu_pct": round(cpu, 1), "ram_pct": round(mem.percent, 1),
            "ram_used_gb": round(mem.used / (1024**3), 2), "ram_total_gb": round(mem.total / (1024**3), 2),
            "disks": disks[:4]
        }
        proc = psutil.Process(os.getpid())
        result["luna_mem_mb"] = round(proc.memory_info().rss / (1024**2), 1)
    except Exception:
        result["system"] = {}
        result["luna_mem_mb"] = 0
    # Ollama
    ollama_ok = False
    latency = None
    try:
        t0 = time.time()
        req = urllib.request.Request(f"{OLLAMA_BASE}/api/tags", method="GET")
        with urllib.request.urlopen(req, timeout=5) as r:
            ollama_ok = r.status == 200
        latency = round((time.time() - t0) * 1000)
    except Exception: pass
    result["ollama_ok"] = ollama_ok
    result["ollama_latency_ms"] = latency
    result["chat_model"] = OLLAMA_CHAT
    result["shadow_model"] = OLLAMA_MODEL
    result["chat_model_display"] = _model_status_label(OLLAMA_CHAT)
    result["shadow_model_display"] = _model_status_label(OLLAMA_MODEL)
    result["chat_backend"] = "gguf" if _gguf_path_valid() else "ollama"
    # Luna stats
    uptime_sec = time.time() - _luna_start_time
    hrs, rem = divmod(int(uptime_sec), 3600)
    mins = rem // 60
    result["uptime"] = f"{hrs}h {mins}m"
    result["conversation_count"] = len(get_recent_conversation(LINKED_SCOPE or "web", 999) or [])
    result["knowledge_count"] = len(list_knowledge())
    result["absorbed_tools"] = len(_absorbed_tool_names)
    with _clipboard_lock:
        result["clipboard_count"] = len(_clipboard_history)
    # Action stats
    actions_today = 0
    ok_count = fail_count = 0
    today_str = datetime.now().strftime("%Y-%m-%d")
    last_error = None
    with _action_log_lock:
        if os.path.isfile(_ACTION_LOG):
            try:
                with open(_ACTION_LOG, "r", encoding="utf-8") as f:
                    for line in f:
                        try:
                            e = json.loads(line.strip())
                            ts_str = (e.get("ts") or "")[:10]
                            if ts_str == today_str:
                                actions_today += 1
                            r = (e.get("reply") or "").lower()
                            if "error" in r or "failed" in r or "❌" in r:
                                fail_count += 1
                                last_error = (e.get("reply") or "")[:80]
                            else:
                                ok_count += 1
                        except Exception: pass
            except Exception: pass
    result["actions_today"] = actions_today
    total = ok_count + fail_count
    result["action_success_rate"] = round(ok_count / total * 100) if total > 0 else None
    result["last_error"] = last_error
    # Recent actions
    recent_actions = []
    with _action_log_lock:
        if os.path.isfile(_ACTION_LOG):
            try:
                with open(_ACTION_LOG, "r", encoding="utf-8") as f:
                    lines = f.readlines()
                for line in lines[-10:]:
                    try: recent_actions.append(json.loads(line.strip()))
                    except Exception: pass
            except Exception: pass
    result["recent_actions"] = recent_actions
    return jsonify(result)

# ── Chat history search API ──────────────────────────────────────────────────

@web.route("/api/chat-history")
def api_chat_history():
    """Search conversation history. ?q=search_term&n=max_results"""
    query = (request.args.get("q") or "").strip().lower()
    n = min(50, max(5, int(request.args.get("n", 20))))
    scope = LINKED_SCOPE or "web"
    all_history = get_recent_conversation(scope, 999) or []
    if not query:
        return jsonify({"results": all_history[-n:]})
    matches = []
    for h in all_history:
        content = (h.get("content") or "").strip()
        if query in content.lower():
            matches.append(h)
        if len(matches) >= n:
            break
    return jsonify({"results": matches, "total": len(matches), "query": query})

# ── Morning briefing API ─────────────────────────────────────────────────────

@web.route("/api/briefing")
def api_briefing():
    """Generate and return morning briefing."""
    briefing = _generate_morning_briefing()
    _mark_briefing_shown()
    try:
        if briefing:
            _play_reply_tts(briefing)
    except Exception:
        pass
    return jsonify({"briefing": briefing})

# ── Screen analytics API ─────────────────────────────────────────────────────

@web.route("/api/screen/sources")
def api_screen_sources():
    """Monitors + (Windows) visible windows for analytics capture picker."""
    return jsonify({
        "monitors": _list_mss_monitors(),
        "windows": _list_windows_win32(),
        "platform": sys.platform,
    })

@web.route("/api/analytics/describe-screen", methods=["POST"])
def api_analytics_describe_screen():
    """Vision read of capture. JSON body: optional `hwnd` (Windows) or `monitor` (mss index, ≥1)."""
    data = request.get_json(force=True, silent=True) or {}
    hwnd = data.get("hwnd")
    monitor = data.get("monitor")
    try:
        hwnd = int(hwnd) if hwnd is not None and str(hwnd).strip() != "" else None
    except (TypeError, ValueError):
        hwnd = None
    try:
        monitor = int(monitor) if monitor is not None and str(monitor).strip() != "" else None
    except (TypeError, ValueError):
        monitor = None
    if hwnd is not None and sys.platform != "win32":
        return jsonify({
            "ok": False,
            "reply": "Window capture by ID is only supported on **Windows**. Use **monitor** instead.",
        })
    ok, reply = _describe_dashboard_screenshot(hwnd=hwnd, monitor_index=monitor)
    try:
        if reply:
            _play_reply_tts(reply)
    except Exception:
        pass
    return jsonify({"ok": ok, "reply": reply})

# ── Manifest + SW for PWA ────────────────────────────────────────────────────

@web.route("/manifest.json")
def serve_manifest():
    return send_from_directory(_BASE, "manifest.json", mimetype="application/manifest+json")

@web.route("/sw.js")
def serve_sw():
    return send_from_directory(_BASE, "sw.js", mimetype="application/javascript")

@web.route("/icon-192.png")
def serve_icon_192():
    return send_from_directory(_BASE, "icon-192.png", mimetype="image/png")

@web.route("/icon-512.png")
def serve_icon_512():
    return send_from_directory(_BASE, "icon-512.png", mimetype="image/png")

# ── Main chat endpoint ───────────────────────────────────────────────────────

def _strip_luna_tags_for_twitch(text: str) -> str:
    """Remove [EMOTION] tags, Discord-style *private message* lines, and flatten for Twitch / TTS."""
    if not text:
        return ""
    t = text.strip()
    # Model sometimes echoes Discord DM formatting — never show this on Twitch
    t = re.sub(r"(?is)\*private message from [^*]+\*\s*:?\s*", "", t)
    t = re.sub(r"\[[A-Za-z][A-Za-z0-9_]*\]\s*", "", t)
    t = re.sub(r"\*\*([^*]+)\*\*", r"\1", t)
    t = re.sub(r"\s+", " ", t).strip()
    return t[:500]

def _twitch_queue_channel_key(item: dict | None) -> str:
    home_lc = (TWITCH_CHANNEL or "").strip().lower()
    if not isinstance(item, dict):
        return home_lc
    return (item.get("channel") or home_lc).strip().lstrip("#").lower() or home_lc


def _tts_for_chat_source(reply: str, chat_source: str, *, para_mode: bool = True) -> None:
    """Speak Luna's reply on the server (system audio). Skip while /vrm/ pings /api/vrm-presence (browser plays Edge TTS)."""
    if _vrm_presence_recent():
        return
    if chat_source == "twitch" and not TWITCH_TTS:
        return
    _play_reply_tts(reply, chat_source=chat_source, para_mode=para_mode)

def _execute_chat_turn(scope: str, msg: str, data: dict | None = None, *, chat_source: str = "web") -> dict:
    """Shared chat logic for web UI and Twitch reader. Returns a dict with at least `reply` on success."""
    global _last_user_activity, _last_streamer_luna_at, _camera_last_result, _last_web_vision_context
    _last_user_activity = time.time()
    data = data or {}
    sc = (scope or "").strip()
    rel_actor_key = _relationship_actor_key(sc, data, chat_source)
    rel_row = _relationship_record_user_turn(rel_actor_key, sc, msg, data)
    if chat_source == "web" and (
        (LINKED_SCOPE and sc == (LINKED_SCOPE or "").strip())
        or (_linked_int and sc == f"discord:user:{_linked_int}")
    ):
        _last_streamer_luna_at = time.time()
    elif chat_source == "twitch":
        tl = (data.get("twitch_login") or data.get("twitch_chatter_login") or "").strip().lower()
        if tl and tl == (TWITCH_BROADCASTER_LOGIN or "").strip().lower():
            _last_streamer_luna_at = time.time()
    biology_satisfy("connection", 0.15)
    biology_satisfy("attention", 0.12)
    biology_satisfy("validation", 0.10)

    def _tts(reply: str) -> None:
        if data.get("twitch_skip_tts"):
            return
        _tts_for_chat_source(reply, chat_source)

    def _store_assistant(assistant: str) -> str:
        a = assistant if isinstance(assistant, str) else str(assistant)
        if chat_source == "twitch":
            a = _strip_luna_tags_for_twitch(a)
        append_exchange(scope, msg, a)
        _vrchat_publish_reply(a)
        return a

    pending = _pending_file_update.pop(scope, None) if chat_source == "web" else None
    if pending:
        path = {"SOUL": _SOUL_PATH, "TOOLS": _TOOLS_PATH, "OBJECTIVES": _OBJECTIVES_PATH}.get(pending)
        if path:
            try: _write_file(path, msg); _invalidate_identity()
            except Exception: pass
            reply = f"Saved to **{pending}.md**."
            reply = _store_assistant(reply)
            _tts(reply)
            return {"reply": reply}

    rest = strip_shadow_prefix(msg)
    if rest is not None:
        reply = shadow_run(rest, scope, _parse_command, _run_cmd, log_fn=_log_action, user_message=msg)
        if isinstance(reply, dict) and reply.get("need_feedback"):
            if chat_source == "twitch":
                r2 = reply.get("message") or "Luna is asking…"
                r2 = _store_assistant(r2)
                _tts(r2)
                return {"reply": r2}
            return {
                "reply": reply.get("message") or "Luna is asking…",
                "need_feedback": True,
                "request_id": reply["request_id"],
                "message": reply["message"],
                "options": reply.get("options"),
            }
        reply = _store_assistant(reply)
        _tts(reply)
        return {"reply": reply}

    if msg.startswith("!"):
        reply = _handle_bang(msg, scope, data or {})
        reply = _store_assistant(reply)
        _bang0 = (msg.split(None, 1)[0] or "").lower()
        if _bang0 in ("!briefing", "!analytics_screen", "!dashboard_read", "!read_dashboard"):
            _tts(reply)
        return {"reply": reply}

    if _is_retry(msg):
        reply = _handle_retry()
        reply = _store_assistant(reply)
        _tts(reply)
        return {"reply": reply}

    fast_override = _chat_fast_from_request(data)
    use_fast = _chat_fast_enabled() if fast_override is None else fast_override

    if not use_fast and _likely_command(msg):
        parsed = _parse_command(msg)
        if parsed:
            cmd0, _params0 = parsed
            # VRM sends vision via /api/chat `vision_frame`, not /api/camera/frame. Phrases like "what can you see?"
            # used to route to camera_see (empty buffer) even when vision_context was injected — skip that shortcut.
            if cmd0 == "camera_see":
                vc = data.get("vision_context") if isinstance(data.get("vision_context"), dict) else {}
                if vc.get("active") and str(vc.get("summary") or "").strip():
                    parsed = None
        if parsed:
            cmd, params = parsed
            if cmd == "help":
                return {"reply": HELP_TEXT}
            reply = _run_cmd(cmd, params, scope, user_message=msg)
            if isinstance(reply, dict) and reply.get("need_feedback"):
                if chat_source == "twitch":
                    r2 = reply.get("message") or "Luna is asking…"
                    r2 = _store_assistant(r2)
                    _tts(r2)
                    return {"reply": r2}
                return {
                    "reply": reply.get("message") or "Luna is asking…",
                    "need_feedback": True,
                    "request_id": reply["request_id"],
                    "message": reply["message"],
                    "options": reply.get("options"),
                }
            if reply:
                biology_satisfy("usefulness", 0.2)
                _log_action(cmd, params, reply if isinstance(reply, str) else str(reply))
                _record_last_action(cmd, reply if isinstance(reply, str) else reply.get("message", ""))
                reply = _store_assistant(reply if isinstance(reply, str) else str(reply))
                _tts(reply)
                return {"reply": reply}

    history = _compact_history(get_recent_conversation(scope, 30), fast=use_fast, scope=scope)
    if history:
        prev_luna = next((h["content"] for h in reversed(history) if h.get("role") == "assistant"), "")
        correction = _detect_correction(msg, prev_luna)
        if correction:
            _store_correction(correction)

    system = _prepare_main_chat_system(
        scope,
        msg,
        fast=use_fast,
        force_stream_mode=_force_stream_mode_from_request_data(data),
        suppress_lol_context=bool((data or {}).get("suppress_lol_context")),
    )
    mm_hint = ""
    mm_bytes = None
    mm_kind = "camera"
    try:
        mm_hint = str(data.pop("_moondream_screen_hint", "") or "").strip()
    except Exception:
        mm_hint = ""
    try:
        mm_bytes = data.pop("_moondream_multimodal_bytes", None)
    except Exception:
        mm_bytes = None
    try:
        mm_kind = str(data.pop("_moondream_multimodal_kind", "") or "").strip().lower() or "camera"
    except Exception:
        mm_kind = "camera"
    mm_uq = ""
    try:
        mm_uq = str(data.pop("_moondream_uq_focus", "") or "").strip()
    except Exception:
        mm_uq = ""
    if mm_hint:
        system = system + "\n\n" + mm_hint
    if mm_uq:
        msg = mm_uq + "\n" + msg

    rel_ctx = _relationship_prompt(rel_actor_key, rel_row)
    if rel_ctx:
        system = system + "\n\n" + rel_ctx

    briefing_reply = None
    if _should_show_briefing():
        briefing_reply = _generate_morning_briefing()
        _mark_briefing_shown()

    mm_ok = bool(mm_bytes) and _moondream_unified_ollama_chat()
    reply = ollama_chat(
        msg,
        system=system,
        scope=scope,
        history=history,
        model=OLLAMA_CHAT,
        compact=use_fast,
        image_bytes=mm_bytes if mm_ok else None,
    )
    if not reply:
        reply = COMMAND_ONLY
    elif reply.startswith("Ollama offline"):
        low_reply = reply.lower()
        if "too many requests" in low_reply or "429" in low_reply:
            reply = "I'm getting rate-limited for a moment. Give me a few seconds and try again."
        else:
            reply = COMMAND_ONLY
    if briefing_reply:
        reply = briefing_reply + "\n\n---\n\n" + reply
    if mm_ok:
        vs_summary = (reply or "").strip()[:2400]
        if vs_summary:
            try:
                with _camera_lock:
                    _camera_last_result = {
                        "vision_summary": vs_summary,
                        "summary": vs_summary,
                        "face_count": 0,
                        "objects": [],
                    }
                    _last_web_vision_context = {
                        "summary": vs_summary,
                        "source": mm_kind or "camera",
                        "ts": time.time(),
                    }
            except Exception:
                pass
    reply = _store_assistant(reply)
    _relationship_note_assistant_turn(rel_actor_key, reply)

    def _post_chat_memory():
        try:
            _capture_memory(scope, msg, reply)
            _capture_profile(scope, msg)
        except Exception:
            pass
        try:
            _update_rolling_summary(scope)
        except Exception:
            pass
    threading.Thread(target=_post_chat_memory, daemon=True).start()
    _tts(reply)
    return {"reply": reply}

def _twitch_enqueue_privmsg(login: str, display: str, text: str, channel: str) -> None:
    if not text or not text.strip():
        return
    _stream_presence_mark_chat_activity((display or login or "").strip())
    ch = (channel or TWITCH_CHANNEL or "").strip().lstrip("#").lower() or (TWITCH_CHANNEL or "").strip().lower()
    try:
        _twitch_msg_queue.put_nowait({"login": login, "display": display, "text": text.strip(), "channel": ch})
    except queue.Full:
        pass


def _twitch_is_priority_text(text: str) -> bool:
    t = (text or "").strip()
    if not t:
        return False
    low = t.lower()
    if low.startswith("!") or low.startswith("shadow"):
        return True
    if "luna" in low and ("?" in low or "help" in low or "status" in low):
        return True
    return _likely_command(t)


_TWITCH_LOGIN_RE = re.compile(r"^[a-z0-9_]{4,25}$")


def _twitch_normalize_login(s: str) -> str:
    return (s or "").strip().lstrip("#").lower()


def _twitch_privileged_irc_sender(login: str) -> bool:
    lg = _twitch_normalize_login(login)
    if not lg:
        return False
    if lg == (TWITCH_BROADCASTER_LOGIN or "").strip().lower():
        return True
    if lg == (TWITCH_CHANNEL or "").strip().lower():
        return True
    return False


def _twitch_send_target_allowed(target: str) -> bool:
    t = _twitch_normalize_login(target)
    if not t or not _TWITCH_LOGIN_RE.match(t):
        return False
    home = (TWITCH_CHANNEL or "").strip().lower()
    if t == home:
        return True
    if TWITCH_ALLOWED_CHAT_TARGETS:
        return t in TWITCH_ALLOWED_CHAT_TARGETS
    return True


def _twitch_outbound_channel_for_send() -> str | None:
    if TWITCH_OUTBOUND_ONLY_HOME:
        return None
    with _twitch_outbound_channel_lock:
        return _twitch_outbound_channel_override


def _twitch_set_outbound_channel(login: str | None) -> None:
    global _twitch_outbound_channel_override
    with _twitch_outbound_channel_lock:
        _twitch_outbound_channel_override = (_twitch_normalize_login(login) if login else None) or None


def _twitch_extra_send_cooldown_ok(channel_lc: str) -> bool:
    ch = _twitch_normalize_login(channel_lc)
    if ch not in TWITCH_EXTRA_IRC_CHANNELS:
        return True
    cd = float(TWITCH_EXTRA_IRC_REPLY_COOLDOWN_SEC or 0.0)
    if cd <= 0.0:
        return True
    now = time.monotonic()
    with _twitch_extra_send_lock:
        last = float(_twitch_extra_last_send_mono.get(ch, 0.0))
        return (now - last) >= cd


def _twitch_extra_send_cooldown_remaining_sec(channel_lc: str) -> float:
    ch = _twitch_normalize_login(channel_lc)
    if ch not in TWITCH_EXTRA_IRC_CHANNELS:
        return 0.0
    cd = float(TWITCH_EXTRA_IRC_REPLY_COOLDOWN_SEC or 0.0)
    if cd <= 0.0:
        return 0.0
    now = time.monotonic()
    with _twitch_extra_send_lock:
        last = float(_twitch_extra_last_send_mono.get(ch, 0.0))
    return max(0.0, cd - (now - last))


def _twitch_extra_mark_sent(channel_lc: str) -> None:
    ch = _twitch_normalize_login(channel_lc)
    if ch not in TWITCH_EXTRA_IRC_CHANNELS:
        return
    with _twitch_extra_send_lock:
        _twitch_extra_last_send_mono[ch] = time.monotonic()


def _twitch_redirect_manage_allowed(*, twitch_login: str | None = None, discord_uid: int | None = None) -> bool:
    if discord_uid is not None and _is_privileged(int(discord_uid)):
        return True
    return _twitch_privileged_irc_sender(twitch_login or "")


def _twitch_exec_chat_redirect(
    arg: str,
    *,
    twitch_login: str | None = None,
    discord_uid: int | None = None,
) -> str:
    if not _twitch_redirect_manage_allowed(twitch_login=twitch_login, discord_uid=discord_uid):
        return "❌ Only the broadcaster (this channel's streamer) or the linked Discord admin can change Twitch redirect."
    parts = (arg or "").strip().split(None, 1)
    target_raw = parts[0].strip() if parts else ""
    home_lc = (TWITCH_CHANNEL or "").strip().lower()
    if not target_raw:
        if TWITCH_OUTBOUND_ONLY_HOME:
            return (
                f"Luna only posts Twitch replies to **#{TWITCH_CHANNEL}** "
                f"(**TWITCH_OUTBOUND_ONLY_HOME** is on)."
            )
        cur = _twitch_outbound_channel_for_send()
        if cur:
            return f"Luna's Twitch **replies** are posting to **#{cur}**. Default home IRC channel is still **#{TWITCH_CHANNEL}** (read/join unchanged). Send **!twitch_chat_redirect default** to reset sends."
        return f"Luna's Twitch replies post to **#{TWITCH_CHANNEL}** (default). Use **!twitch_chat_redirect <channel>** to send replies elsewhere."
    tl = target_raw.lower()
    if tl in ("default", "off", "reset", "main", "home", home_lc):
        _twitch_set_outbound_channel(None)
        return f"✅ Twitch reply sends reset to **#{TWITCH_CHANNEL}**."
    if TWITCH_OUTBOUND_ONLY_HOME:
        return (
            f"❌ Luna only posts Twitch replies to **#{TWITCH_CHANNEL}** "
            f"(**TWITCH_OUTBOUND_ONLY_HOME** is on). Cross-channel redirect is disabled."
        )
    if not _twitch_send_target_allowed(tl):
        return (
            f"❌ **#{tl}** is not allowed. Add it to **TWITCH_ALLOWED_CHAT_TARGETS** in `.env` (comma-separated logins), "
            "then restart Luna."
        )
    _twitch_set_outbound_channel(tl)
    return (
        f"✅ Luna's **generated Twitch replies** will post to **#{_twitch_normalize_login(tl)}** until you send "
        f"**!twitch_chat_redirect default**. (IRC still listens on **#{TWITCH_CHANNEL}**.)"
    )


def _twitch_exec_say(
    arg: str,
    *,
    twitch_login: str | None = None,
    discord_uid: int | None = None,
) -> str:
    if not _twitch_redirect_manage_allowed(twitch_login=twitch_login, discord_uid=discord_uid):
        return "❌ Only the broadcaster or linked Discord admin can use !twitch_say."
    parts = (arg or "").strip().split(None, 1)
    if len(parts) < 2:
        return "Usage: **!twitch_say <channel_login> <message>**"
    chan, body = parts[0].strip(), parts[1].strip()
    c = _twitch_normalize_login(chan)
    home_lc = (TWITCH_CHANNEL or "").strip().lower()
    if TWITCH_OUTBOUND_ONLY_HOME and c != home_lc:
        if c not in TWITCH_EXTRA_IRC_CHANNELS:
            return (
                f"❌ Cross-channel Twitch sends are off — Luna only chats in **#{TWITCH_CHANNEL}** "
                f"(**TWITCH_OUTBOUND_ONLY_HOME**). Extra IRC channels listed in **TWITCH_EXTRA_IRC_CHANNELS** are allowed."
            )
        if TWITCH_EXTRA_IRC_REPLY_COOLDOWN_SEC > 0 and not _twitch_extra_send_cooldown_ok(c):
            rem = _twitch_extra_send_cooldown_remaining_sec(c)
            return f"⏳ Cooldown: wait **~{max(1, int(rem + 0.5))}s** before another send to **#{c}**."
    elif TWITCH_EXTRA_IRC_REPLY_COOLDOWN_SEC > 0 and c in TWITCH_EXTRA_IRC_CHANNELS and not _twitch_extra_send_cooldown_ok(c):
        rem = _twitch_extra_send_cooldown_remaining_sec(c)
        return f"⏳ Cooldown: wait **~{max(1, int(rem + 0.5))}s** before another send to **#{c}**."
    if not _twitch_send_target_allowed(c):
        return (
            f"❌ **#{c}** is not allowed. Set **TWITCH_ALLOWED_CHAT_TARGETS** (or use your home channel **#{TWITCH_CHANNEL}**)."
        )
    if not TWITCH_SEND_CHAT:
        return "❌ Twitch send is disabled (**TWITCH_SEND_CHAT**)."
    ok = send_twitch_chat_message(body[:450], channel=c)
    if ok and c in TWITCH_EXTRA_IRC_CHANNELS:
        _twitch_extra_mark_sent(c)
    return f"✅ Sent to **#{c}**." if ok else "❌ Could not send (IRC not connected?)."


def _twitch_handle_direct_commands(login: str, display: str, text: str) -> str | None:
    """Broadcaster-only IRC shortcuts; bypass LLM. Returns reply text or None."""
    t = (text or "").strip()
    low = t.lower()
    if low.startswith("!twitch_chat_redirect"):
        rest = t[len("!twitch_chat_redirect") :].strip()
        return _twitch_exec_chat_redirect(rest, twitch_login=login)
    if low.startswith("!twitch_say"):
        rest = t[len("!twitch_say") :].strip()
        return _twitch_exec_say(rest, twitch_login=login)
    return None


def _twitch_push_ui_system_reply(display: str, login: str, text: str, reply: str) -> None:
    global _twitch_event_id
    label = (display or login or "viewer").strip() or "viewer"
    with _twitch_lock:
        _twitch_event_id += 1
        eid = _twitch_event_id
        _twitch_ui_events.append({
            "id": eid,
            "user": label,
            "text": text,
            "reply": reply,
            "ts": time.time(),
            "pause_cowatch_video": False,
        })
        while len(_twitch_ui_events) > 100:
            _twitch_ui_events.pop(0)


def _twitch_build_batch_turn(batch: list[dict]) -> tuple[str, str, str]:
    """Return (msg_for_model, ui_user_label, ui_text_label)."""
    if not batch:
        return "", "", ""
    home_lc = (TWITCH_CHANNEL or "").strip().lower()
    ch0 = _twitch_queue_channel_key(batch[0])
    room_tag = f" — #{ch0}" if ch0 != home_lc else ""
    if len(batch) == 1:
        one = batch[0]
        login = (one.get("login") or "").strip().lower()
        display = (one.get("display") or "").strip()
        text = (one.get("text") or "").strip()
        label = display or login or "viewer"
        return f"[Twitch chat{room_tag}] {label}: {text}", label, text
    lines: list[str] = []
    names: list[str] = []
    for it in batch:
        login = (it.get("login") or "").strip().lower()
        display = (it.get("display") or "").strip()
        text = (it.get("text") or "").strip()
        label = display or login or "viewer"
        names.append(label)
        lines.append(f"- {label}: {text}")
    unique_names = list(dict.fromkeys(names))
    who = f"{len(unique_names)} chatters"
    label_preview = ", ".join(unique_names[:3]) + ("…" if len(unique_names) > 3 else "")
    ui_text = f"{len(batch)} msgs ({label_preview})"
    msg = (
        f"[Twitch chat batch{room_tag}]\n"
        "Respond to the recent viewer messages together with one concise, natural reply.\n"
        "If one line is a direct command, prioritize it first.\n\n"
        + "\n".join(lines)
    )
    return msg, who, ui_text


def _twitch_process_turn(scope: str, batch: list[dict]) -> None:
    global _twitch_event_id
    if not batch:
        return
    msg, label, text = _twitch_build_batch_turn(batch)
    if not msg:
        return
    login0 = ((batch[0].get("login") or "").strip().lower() if batch else "")
    display0 = ((batch[0].get("display") or "").strip() if batch else "")
    home_lc = (TWITCH_CHANNEL or "").strip().lower()
    src_ch = _twitch_queue_channel_key(batch[0])
    is_aux_irc = src_ch in TWITCH_EXTRA_IRC_CHANNELS
    if is_aux_irc and TWITCH_EXTRA_IRC_REPLY_COOLDOWN_SEC > 0 and not _twitch_extra_send_cooldown_ok(src_ch):
        return
    try:
        out = _execute_chat_turn(
            scope,
            msg,
            {
                "twitch_login": login0,
                "twitch_display": display0,
                "twitch_source_channel": src_ch,
                "twitch_skip_tts": bool(is_aux_irc),
            },
            chat_source="twitch",
        )
        reply = out.get("reply") or ""
        if out.get("need_feedback") and not reply:
            reply = out.get("message") or "Check the web UI to continue."
        public = _strip_luna_tags_for_twitch(reply) if reply else ""
        # Auxiliary IRC channels always get on-chat text; stream "voice-only" mode skips IRC post on home only.
        skip_chat = bool(
            (not is_aux_irc)
            and TWITCH_VOICE_ONLY_IN_STREAM_MODE
            and TWITCH_TTS
            and _stream_mode_should_apply(msg)
        )
        if TWITCH_SEND_CHAT and public and not skip_chat:
            send_chan = src_ch if src_ch != home_lc else _twitch_outbound_channel_for_send()
            sent_ok = send_twitch_chat_message(public, channel=send_chan)
            if sent_ok and is_aux_irc:
                _twitch_extra_mark_sent(src_ch)
        reply_show = public if public else reply
        pause_cowatch = bool(
            _yt_watch_cowatch_active()
            and reply_show
            and not str(reply_show).lstrip().startswith("⚠")
            and _cowatch_should_pause_for_twitch_reply(str(reply_show), text)
        )
        with _twitch_lock:
            _twitch_event_id += 1
            eid = _twitch_event_id
            _twitch_ui_events.append({
                "id": eid,
                "user": label,
                "text": text,
                "reply": reply_show,
                "ts": time.time(),
                "pause_cowatch_video": pause_cowatch,
            })
            while len(_twitch_ui_events) > 100:
                _twitch_ui_events.pop(0)
    except Exception as e:
        with _twitch_lock:
            _twitch_event_id += 1
            eid = _twitch_event_id
            _twitch_ui_events.append({
                "id": eid,
                "user": label or "viewer",
                "text": text or "",
                "reply": f"⚠ {e}",
                "ts": time.time(),
                "pause_cowatch_video": False,
            })


def _twitch_worker_loop() -> None:
    scope = LINKED_SCOPE or "web"
    while not _twitch_stop_event.is_set():
        try:
            item = _twitch_msg_queue.get(timeout=1.0)
        except queue.Empty:
            continue
        login = (item.get("login") or "").strip().lower()
        display = (item.get("display") or "").strip()
        text = (item.get("text") or "").strip()
        if not text:
            continue
        if TWITCH_BOT_USERNAME and login == TWITCH_BOT_USERNAME.lower():
            continue
        ich = _twitch_queue_channel_key(item)
        if ich in TWITCH_EXTRA_IRC_CHANNELS and TWITCH_EXTRA_IRC_REPLY_TO_LOGINS is not None:
            if login not in TWITCH_EXTRA_IRC_REPLY_TO_LOGINS:
                continue
        early = _twitch_handle_direct_commands(login, display, text)
        if early is not None:
            _twitch_push_ui_system_reply(display, login, text, early)
            continue
        ch0 = ich
        # Priority lane: commands/direct asks go immediately.
        if (not _TWITCH_CHAT_BATCHING) or _twitch_is_priority_text(text):
            _twitch_process_turn(scope, [item])
            continue
        # Batch lane: briefly collect chatter burst into one reply (never mix IRC rooms).
        batch = [item]
        if TWITCH_CHAT_BATCH_WINDOW_SEC > 0 and TWITCH_CHAT_BATCH_MAX_ITEMS > 1:
            deadline = time.time() + TWITCH_CHAT_BATCH_WINDOW_SEC
            while len(batch) < TWITCH_CHAT_BATCH_MAX_ITEMS and time.time() < deadline:
                wait_left = max(0.0, deadline - time.time())
                try:
                    nxt = _twitch_msg_queue.get(timeout=wait_left)
                except queue.Empty:
                    break
                n_login = (nxt.get("login") or "").strip().lower()
                n_text = (nxt.get("text") or "").strip()
                if not n_text:
                    continue
                if TWITCH_BOT_USERNAME and n_login == TWITCH_BOT_USERNAME.lower():
                    continue
                n_ch = _twitch_queue_channel_key(nxt)
                # If a priority message appears during batch window, flush current batch first,
                # then handle that priority message immediately.
                if _twitch_is_priority_text(n_text):
                    _twitch_process_turn(scope, batch)
                    batch = []
                    _twitch_process_turn(scope, [nxt])
                    break
                if n_ch != ch0:
                    _twitch_process_turn(scope, batch)
                    batch = [nxt]
                    ch0 = n_ch
                    deadline = time.time() + TWITCH_CHAT_BATCH_WINDOW_SEC
                    continue
                batch.append(nxt)
        if batch:
            _twitch_process_turn(scope, batch)

def _start_twitch_ingest() -> None:
    if TWITCH_OUTBOUND_ONLY_HOME:
        _twitch_set_outbound_channel(None)
    if not TWITCH_CHAT_ENABLED or not TWITCH_OAUTH_TOKEN or not TWITCH_CHANNEL:
        return
    if not run_twitch_irc_reader:
        print("[Twitch] luna_twitch not available.", flush=True)
        return
    threading.Thread(target=_twitch_worker_loop, daemon=True).start()
    threading.Thread(target=_twitch_auto_ack_loop, daemon=True).start()

    def _irc() -> None:
        extras = sorted(TWITCH_EXTRA_IRC_CHANNELS)
        run_twitch_irc_reader(
            TWITCH_CHANNEL,
            TWITCH_BOT_USERNAME,
            TWITCH_OAUTH_TOKEN,
            _twitch_enqueue_privmsg,
            _twitch_stop_event,
            extra_join_channels=extras,
            log=lambda m: print(m, flush=True),
        )

    threading.Thread(target=_irc, daemon=True).start()
    _vo_twitch = bool(
        TWITCH_VOICE_ONLY_IN_STREAM_MODE
        and TWITCH_TTS
        and _stream_mode_should_apply("[Twitch chat] _: example")
    )
    _chat_post = bool(TWITCH_SEND_CHAT and not _vo_twitch)
    _extras_s = ", ".join(f"#{x}" for x in sorted(TWITCH_EXTRA_IRC_CHANNELS))
    _extra_note = ""
    if _extras_s:
        _extra_note = " Also joined: " + _extras_s + " (chat replies there, no TTS"
        if TWITCH_EXTRA_IRC_REPLY_TO_LOGINS is None:
            _extra_note += "; replies from everyone)."
        else:
            _extra_note += "; replies only from: " + ", ".join(sorted(TWITCH_EXTRA_IRC_REPLY_TO_LOGINS)) + ")."
        if TWITCH_EXTRA_IRC_REPLY_COOLDOWN_SEC > 0:
            _extra_note += f" Min **{TWITCH_EXTRA_IRC_REPLY_COOLDOWN_SEC:g}s** between sends per extra channel (auto + **!twitch_say**)."
    print(
        f"[Twitch] IRC #{TWITCH_CHANNEL} as {TWITCH_BOT_USERNAME} — post replies to chat: "
        f"{'on' if _chat_post else 'off'}, TTS: {'on' if TWITCH_TTS else 'off'}"
        f"{'; voice-only for Twitch lines (set TWITCH_VOICE_ONLY_IN_STREAM_MODE=0 to post text)' if _vo_twitch else ''}. "
        f"{'(Outbound locked to home channel — TWITCH_OUTBOUND_ONLY_HOME.) ' if TWITCH_OUTBOUND_ONLY_HOME else ''}"
        f"{'' if TWITCH_OUTBOUND_ONLY_HOME else '(Redirect: !twitch_chat_redirect <channel> from Twitch broadcaster or Discord admin.)'}"
        f"{_extra_note}",
        flush=True,
    )

@web.route("/api/twitch/pending")
def api_twitch_pending():
    """UI polls for new Twitch chat lines + Luna replies. ?since=<last id seen>"""
    try:
        since = int(request.args.get("since", "0"))
    except ValueError:
        since = 0
    with _twitch_lock:
        events = [e for e in _twitch_ui_events if e.get("id", 0) > since]
    return jsonify({"events": events})


@web.route("/api/twitch/oauth/status", methods=["GET"])
def api_twitch_oauth_status():
    return jsonify(_twitch_oauth_status())


@web.route("/api/twitch/oauth/start", methods=["GET"])
def api_twitch_oauth_start():
    if not TWITCH_CLIENT_ID or not TWITCH_CLIENT_SECRET:
        return jsonify(
            {
                "ok": False,
                "error": "Set TWITCH_CLIENT_ID and TWITCH_CLIENT_SECRET in .env first.",
            }
        ), 400
    state = _twitch_oauth_new_state()
    url = _twitch_oauth_authorize_url(state)
    if request.args.get("redirect", "").strip().lower() in ("1", "true", "yes", "on"):
        return redirect(url, code=302)
    return jsonify({"ok": True, "url": url, "state": state, "redirect_uri": TWITCH_OAUTH_REDIRECT_URI})


@web.route("/api/twitch/oauth/callback", methods=["GET"])
def api_twitch_oauth_callback():
    if request.args.get("error"):
        err = (request.args.get("error_description") or request.args.get("error") or "").strip()
        return Response(
            f"<h3>Twitch OAuth failed</h3><p>{html.escape(err)}</p><p>You can close this tab.</p>",
            status=400,
            mimetype="text/html",
        )
    code = (request.args.get("code") or "").strip()
    state = (request.args.get("state") or "").strip()
    if not code:
        return Response("<h3>Missing OAuth code.</h3><p>You can close this tab.</p>", status=400, mimetype="text/html")
    if not _twitch_oauth_consume_state(state):
        return Response("<h3>Invalid/expired OAuth state.</h3><p>Start again from /api/twitch/oauth/start.</p>", status=400, mimetype="text/html")

    ok, token_data = _twitch_oauth_exchange_code(code)
    if not ok:
        return Response(
            "<h3>Twitch OAuth token exchange failed.</h3><pre>"
            + html.escape(str(token_data))
            + "</pre><p>You can close this tab.</p>",
            status=500,
            mimetype="text/html",
        )
    ok_user, user_data = _twitch_oauth_fetch_user(str((token_data or {}).get("access_token") or ""))
    if not ok_user:
        return Response(
            "<h3>Twitch OAuth succeeded but user lookup failed.</h3><pre>"
            + html.escape(str(user_data))
            + "</pre><p>You can close this tab.</p>",
            status=500,
            mimetype="text/html",
        )
    _twitch_oauth_save(token_data if isinstance(token_data, dict) else {}, user_data if isinstance(user_data, dict) else {})
    login = str((user_data or {}).get("login") or "")
    return Response(
        "<h3>Twitch OAuth connected.</h3>"
        f"<p>Authorized broadcaster: <strong>{html.escape(login or '(unknown)')}</strong></p>"
        "<p>Luna can now use broadcaster API features (title/polls/sub events when enabled).</p>"
        "<p>You can close this tab.</p>",
        mimetype="text/html",
    )


@web.route("/api/twitch/oauth/complete", methods=["GET", "POST"])
def api_twitch_oauth_complete():
    """Manual completion endpoint for flows that redirect to localhost:3000.

    Use this when Twitch redirect URI is not Luna's callback route. Paste code/state from browser URL here.
    """
    if request.method == "GET" and not (request.args.get("code") or "").strip():
        return Response(
            "<h3>Complete Twitch OAuth</h3>"
            "<p>Paste the <code>code</code> and <code>state</code> values from your browser URL after Twitch approval.</p>"
            "<form method='POST'>"
            "<label>code</label><br/><input name='code' style='width:780px;max-width:95vw'/><br/><br/>"
            "<label>state</label><br/><input name='state' style='width:780px;max-width:95vw'/><br/><br/>"
            "<button type='submit'>Complete authorization</button>"
            "</form>",
            mimetype="text/html",
        )
    code = (request.values.get("code") or "").strip()
    state = (request.values.get("state") or "").strip()
    if not code:
        return Response("<h3>Missing code.</h3>", status=400, mimetype="text/html")
    if not _twitch_oauth_consume_state(state):
        return Response(
            "<h3>Invalid/expired OAuth state.</h3><p>Start again from /api/twitch/oauth/start.</p>",
            status=400,
            mimetype="text/html",
        )
    ok, token_data = _twitch_oauth_exchange_code(code)
    if not ok:
        return Response(
            "<h3>Twitch OAuth token exchange failed.</h3><pre>"
            + html.escape(str(token_data))
            + "</pre>",
            status=500,
            mimetype="text/html",
        )
    ok_user, user_data = _twitch_oauth_fetch_user(str((token_data or {}).get("access_token") or ""))
    if not ok_user:
        return Response(
            "<h3>Twitch OAuth succeeded but user lookup failed.</h3><pre>"
            + html.escape(str(user_data))
            + "</pre>",
            status=500,
            mimetype="text/html",
        )
    _twitch_oauth_save(token_data if isinstance(token_data, dict) else {}, user_data if isinstance(user_data, dict) else {})
    login = str((user_data or {}).get("login") or "")
    return Response(
        "<h3>Twitch OAuth connected.</h3>"
        f"<p>Authorized broadcaster: <strong>{html.escape(login or '(unknown)')}</strong></p>"
        "<p>You can close this tab and refresh /api/twitch/oauth/status.</p>",
        mimetype="text/html",
    )


@web.route("/api/lol-chat/pending")
def api_lol_chat_pending():
    """Hub polls for League live-commentary lines Luna generated in the background. ?since=<last id>"""
    try:
        since = int(request.args.get("since", "0"))
    except ValueError:
        since = 0
    with _lol_chat_lock:
        events = [e for e in _lol_chat_events if e.get("id", 0) > since]
    return jsonify({"events": events})


@web.route("/api/stream-solo/pending")
def api_stream_solo_pending():
    """Hub and /vrm/ poll for idle stream-solo banter lines (Edge TTS + avatar when VRM is open). ?since=<last id>"""
    try:
        since = int(request.args.get("since", "0"))
    except ValueError:
        since = 0
    with _stream_solo_chat_lock:
        events = [e for e in _stream_solo_chat_events if e.get("id", 0) > since]
    return jsonify({"events": events})


@web.route("/api/yt-watch-react/pending")
def api_yt_watch_react_pending():
    """Hub and /vrm/ poll for !yt_watch_react timed lines (co-watch avatar + Edge TTS). ?since=<last id>"""
    try:
        since = int(request.args.get("since", "0"))
    except ValueError:
        since = 0
    with _yt_watch_react_ui_lock:
        events = [e for e in _yt_watch_react_ui_events if e.get("id", 0) > since]
    return jsonify({"events": events})


@web.route("/api/yt-watch-react/schedule", methods=["GET"])
def api_yt_watch_react_schedule():
    """Caption-time reaction schedule for Podcast studio (client syncs emits to YouTube currentTime)."""
    with _yt_watch_schedule_lock:
        d = _load_json(_YT_WATCH_SCHEDULE_PATH, {})
    if not isinstance(d, dict):
        d = {}
    items = d.get("items")
    if not isinstance(items, list):
        items = []
    return jsonify({
        "ok": True,
        "ready": bool(d.get("ready")),
        "session_id": float(d.get("session_id") or 0),
        "title": (d.get("title") or "").strip(),
        "url": (d.get("url") or "").strip(),
        "items": items,
        "error": (d.get("error") or "").strip(),
    })


@web.route("/api/yt-watch-react/emit", methods=["POST"])
def api_yt_watch_react_emit():
    """Podcast studio: generate+emit one LIVE reaction when playback crosses a beat time."""
    data = request.get_json(force=True, silent=True) or {}
    try:
        eid = int(data.get("id") or 0)
    except (TypeError, ValueError):
        return jsonify({"ok": False, "error": "bad id"}), 400
    if eid <= 0:
        return jsonify({"ok": False, "error": "bad id"}), 400
    with _yt_watch_schedule_lock:
        sched = _load_json(_YT_WATCH_SCHEDULE_PATH, {})
    if not isinstance(sched, dict) or not sched.get("ready"):
        return jsonify({"ok": False, "error": "schedule not ready"}), 409
    item = None
    for it in sched.get("items") or []:
        if int(it.get("id") or 0) == eid:
            item = it
            break
    if not item:
        return jsonify({"ok": False, "error": "unknown id"}), 404
    with _yt_watch_emitted_lock:
        if eid in _yt_watch_emitted_ids:
            return jsonify({"ok": True, "duplicate": True})
        _yt_watch_emitted_ids.add(eid)
    scope = str(sched.get("scope") or "web").strip() or "web"
    title = str(sched.get("title") or "").strip() or "YouTube video"
    try:
        sid = float(sched.get("session_id") or 0.0)
    except (TypeError, ValueError):
        sid = 0.0
    items = sched.get("items") or []
    beat_total = max(1, len(items))
    try:
        sec = float(item.get("sec") or 0.0)
    except (TypeError, ValueError):
        sec = 0.0
    context = str(item.get("context") or "")
    preceding = str(item.get("preceding") or "")
    video_summary = str(item.get("video_summary") or "")
    if not context.strip():
        with _yt_watch_emitted_lock:
            _yt_watch_emitted_ids.discard(eid)
        return jsonify({"ok": False, "error": "missing context"}), 400
    session_key = f"{scope}|{sid:.6f}"
    with _yt_watch_live_prev_lock:
        prev_line = _yt_watch_live_prev_by_session.get(session_key, "")
    try:
        raw_line = _yt_watch_make_reaction_line(
            title,
            context,
            prev_line,
            beat_index=eid,
            beat_total=beat_total,
            sec=sec,
            preceding=preceding,
            video_summary=video_summary,
        )
        text, pause_before, hold_after = _yt_watch_parse_reaction_controls(raw_line)
        if _yt_watch_force_pause_beats():
            pause_before = True
    except Exception as ex:
        with _yt_watch_emitted_lock:
            _yt_watch_emitted_ids.discard(eid)
        return jsonify({"ok": False, "error": f"live generation failed: {str(ex)[:120]}"}), 500
    if not text.strip():
        # SKIP is valid for live mode; keep id marked emitted so the beat isn't retried.
        return jsonify({"ok": True, "skipped": True, "pause_before": bool(pause_before), "hold_after": bool(hold_after)})
    with _yt_watch_live_prev_lock:
        _yt_watch_live_prev_by_session[session_key] = text
    loop = getattr(bot, "loop", None)
    if loop and loop.is_running():
        asyncio.run_coroutine_threadsafe(_yt_watch_send_scope_line(scope, text, reaction_line=True), loop)
    else:
        try:
            append_exchange(scope or (LINKED_SCOPE or "web"), "[YT watch react]", text)
        except Exception:
            pass
        try:
            _yt_watch_react_ui_enqueue(text)
        except Exception:
            pass
    return jsonify({"ok": True, "line": text, "pause_before": bool(pause_before), "hold_after": bool(hold_after)})


@web.route("/api/yt-watch-react/playback-ended", methods=["POST"])
def api_yt_watch_react_playback_ended():
    """Podcast studio: YouTube IFrame API reported ENDED — optional one-line notice to Discord/Twitch scope."""
    global _yt_watch_playback_ended_for_session
    with _yt_watch_schedule_lock:
        sched = _load_json(_YT_WATCH_SCHEDULE_PATH, {})
        if not isinstance(sched, dict) or not sched.get("ready"):
            return jsonify({"ok": True, "ignored": True})
        try:
            sid = float(sched.get("session_id") or 0.0)
        except (TypeError, ValueError):
            sid = 0.0
        if sid <= 0:
            return jsonify({"ok": True, "ignored": True})
        if sid == _yt_watch_playback_ended_for_session:
            return jsonify({"ok": True, "duplicate": True})
        _yt_watch_playback_ended_for_session = sid
        scope = str(sched.get("scope") or "web").strip() or "web"
    msg = (
        "✅ **Video finished** in Podcast studio. This watch-react run is complete. "
        "Use **!yt_watch_stop** to clear the session, or start another **!yt_watch_react** with a URL."
    )
    loop = getattr(bot, "loop", None)
    if loop and loop.is_running():
        asyncio.run_coroutine_threadsafe(_yt_watch_send_scope_line(scope, msg, reaction_line=False), loop)
    return jsonify({"ok": True})


@web.route("/api/lol-spectator/status")
def api_lol_spectator_status():
    """Hub: whether Riot active-game is visible, last HTTP code, region (see luna_lol_spectator.get_spectator_status)."""
    try:
        if _lol_spectator and hasattr(_lol_spectator, "get_spectator_status"):
            return jsonify(_lol_spectator.get_spectator_status())
    except Exception as e:
        return jsonify({"configured": False, "message": str(e), "task_running": False})
    return jsonify({"configured": False, "message": "Module unavailable", "task_running": False})


@web.route("/api/stream-mode", methods=["GET"])
def api_stream_mode_get():
    """Current streamer persona mode used by chat prompt assembly."""
    with _stream_mode_override_lock:
        ov = _stream_mode_override
    enabled = _stream_mode_env_on()
    source = "runtime" if ov is not None else "env"
    return jsonify({"stream_mode": bool(enabled), "source": source, "override": ov})

@web.route("/api/stream-mode", methods=["POST"])
def api_stream_mode_set():
    """Set runtime stream-mode override (true=streamer, false=normal)."""
    data = request.get_json(force=True, silent=True) or {}
    raw = data.get("stream_mode")
    if raw is None:
        raw = data.get("streamMode")
    if raw is None:
        raw = data.get("enabled")
    if isinstance(raw, str):
        t = raw.strip().lower()
        if t in ("streamer", "stream", "on", "true", "1", "yes"):
            raw = True
        elif t in ("normal", "off", "false", "0", "no"):
            raw = False
    if isinstance(raw, (int, float)):
        raw = bool(raw)
    if not isinstance(raw, bool):
        return jsonify({"error": "Provide stream_mode as true/false (or normal/streamer)."}), 400
    _set_stream_mode_override(raw)
    global _stream_presence_auto_override
    _stream_presence_auto_override = False
    _schedule_discord_presence_for_stream_mode(raw)
    return jsonify({"ok": True, "stream_mode": _stream_mode_env_on(), "source": "runtime"})


@web.route("/api/stream-presence", methods=["GET"])
def api_stream_presence():
    """Current stream/record awareness state used for auto stream persona behavior."""
    return jsonify(_stream_presence_get())


@web.route("/api/studio/watch-target", methods=["GET", "POST"])
def api_studio_watch_target():
    """Podcast studio big-screen URL target for auto-loading watch sessions."""
    if request.method == "GET":
        d = _studio_watch_get()
        return jsonify({
            "ok": True,
            "url": (d.get("url") or "").strip(),
            "title": (d.get("title") or "").strip(),
            "source": (d.get("source") or "").strip(),
            "ts": float(d.get("ts") or 0.0),
        })
    data = request.get_json(force=True, silent=True) or {}
    _studio_watch_set(
        (data.get("url") or "").strip(),
        (data.get("title") or "").strip(),
        (data.get("source") or "manual").strip(),
    )
    d = _studio_watch_get()
    return jsonify({"ok": True, "url": d.get("url") or "", "title": d.get("title") or "", "source": d.get("source") or "manual"})


def _vision_user_question_focus_suffix(user_question: str) -> str:
    """Bias vision-model reads toward what the user asked (single full-frame; no client-side ROI)."""
    q = (user_question or "").strip()
    if not q:
        return ""
    q = q.replace("«", "'").replace("»", "'").strip()
    if len(q) > 700:
        q = q[:697].rstrip() + "…"
    return (
        "\n\n=== USER QUESTION (prioritize this; ignore unrelated clutter when possible) ===\n"
        f"{q}\n"
        "You only receive ONE full-frame snapshot—no crop/zoom. Answer primarily using regions relevant to this question; "
        "name approximate placement (top/middle/bottom × left/center/right, or e.g. top-left HUD, bottom-center chat). "
        "Quote readable text and numbers from those regions.\n"
        "If what they asked about is not visible or unreadable, say so plainly.\n"
        "=== END USER QUESTION ===\n"
    )


@web.route("/api/chat", methods=["POST"])
def api_chat():
    global _last_user_activity, _camera_last_result, _last_web_vision_context
    _last_user_activity = time.time()
    ip = request.remote_addr or "unknown"
    if not _rate_ok(ip):
        return jsonify({"error": "Too many requests. Slow down."}), 429
    data = request.get_json(force=True, silent=True) or {}
    # Optional frame from hub/VRM: caption via OLLAMA_VISION_MODEL then inject text for chat — unless Moondream handles both (multimodal chat).
    try:
        vf = data.get("vision_frame") if isinstance(data, dict) else None
        media_state = data.get("media_state") if isinstance(data, dict) else None
        if (
            isinstance(vf, dict)
            and isinstance(vf.get("b64"), str)
            and vf.get("b64")
            and isinstance(media_state, dict)
            and bool(media_state.get("active"))
        ):
            b64s = (vf.get("b64") or "").strip()
            # Bound payload size (~1.6MB base64) to avoid chat abuse / huge uploads.
            if b64s and len(b64s) <= 1_600_000:
                img_bytes = base64.b64decode(b64s, validate=False)
                if img_bytes:
                    kind = str(media_state.get("type") or "camera").strip().lower()
                    uq_focus = _vision_user_question_focus_suffix(str(data.get("message") or ""))
                    lol_vis_align = ""
                    if kind == "screen" and _lol_spectator:
                        try:
                            fn_lo = getattr(_lol_spectator, "lol_vision_alignment_hint", None)
                            if callable(fn_lo):
                                _lv = (fn_lo() or "").strip()
                                if _lv:
                                    lol_vis_align = "\n\n=== LEAGUE SCREEN PRIORITY ===\n" + _lv + "\n"
                        except Exception:
                            lol_vis_align = ""
                    if _moondream_unified_ollama_chat():
                        data["_moondream_multimodal_bytes"] = img_bytes
                        data["_moondream_multimodal_kind"] = kind
                        uf = (uq_focus or "").strip()
                        if uf:
                            data["_moondream_uq_focus"] = uf
                        if kind == "screen" and (lol_vis_align or "").strip():
                            data["_moondream_screen_hint"] = lol_vis_align.strip()
                    else:
                        if kind == "screen":
                            vp = (
                                "You are reading ONE desktop screen-capture frame for a live assistant. Prioritize identifying exactly WHAT app/game is on screen.\n"
                                "Return this exact structure:\n"
                                "IDENTITY: <exact app/game/site name OR UNCERTAIN: option A | option B>\n"
                                "TYPE: <game / browser / IDE / chat app / video / other>\n"
                                "EVIDENCE: <2-5 short bullet-like clauses with quoted visible text + HUD/UI positions>\n"
                                "SCENE: <1-3 short sentences about current action/context>\n"
                                "CONFIDENCE: <0.00-1.00>\n"
                                "Rules: Wrong IDs are worse than uncertainty. Only name a title if visible text/logo/HUD strongly supports it. "
                                "For similar games, explicitly compare cues (camera perspective, minimap/crosshair, ability bar geometry). "
                                "If a USER QUESTION block appears below, bias EVIDENCE and SCENE toward answering it before generic summary. "
                                "Do not invent off-screen details."
                                + lol_vis_align
                                + uq_focus
                            )
                        else:
                            vp = (
                                "You are reading ONE camera frame for a live assistant.\n"
                                "Describe only what is visible: people, pose, clothing colors, background, readable text/signs. "
                                "If a USER QUESTION block appears below, focus the description on answering it using visible cues. "
                                "Do not invent identity or off-camera context. If faces are unclear, avoid naming individuals. "
                                "Output 3–7 short sentences, no preamble."
                                + uq_focus
                            )
                        # Keep live chat responsive: vision is best-effort and must not stall voice replies.
                        vs = _vision_describe_image(
                            img_bytes,
                            prompt=vp,
                            timeout=max(8, min(120, int(OLLAMA_VISION_TIMEOUT))),
                            wait_for_lock=False,
                        )
                        if vs:
                            vs = str(vs).strip()
                            if vs:
                                vs = vs[:2400]
                                ps = data.get("page_state") if isinstance(data.get("page_state"), dict) else {}
                                ps["vision_context"] = {
                                    "active": True,
                                    "source": kind or "camera",
                                    "summary": vs,
                                }
                                data["page_state"] = ps
                                data["vision_context"] = ps["vision_context"]
                                # Keep /api/camera/status + camera_see aligned with what VRM sent on this chat turn.
                                with _camera_lock:
                                    _camera_last_result = {
                                        "vision_summary": vs,
                                        "summary": vs,
                                        "face_count": 0,
                                        "objects": [],
                                    }
                                    _last_web_vision_context = {
                                        "summary": vs,
                                        "source": kind or "camera",
                                        "ts": time.time(),
                                    }
    except Exception:
        pass
    # If this turn did not get a fresh frame (transient capture failure) but media is still active, reuse recent vision.
    try:
        ms0 = data.get("media_state") if isinstance(data.get("media_state"), dict) else {}
        vc0 = data.get("vision_context") if isinstance(data.get("vision_context"), dict) else {}
        if (
            bool(ms0.get("active"))
            and not data.get("_moondream_multimodal_bytes")
            and not (vc0.get("active") and str(vc0.get("summary") or "").strip())
        ):
            with _camera_lock:
                snap = _last_web_vision_context
            if isinstance(snap, dict) and str(snap.get("summary") or "").strip():
                age = time.time() - float(snap.get("ts") or 0.0)
                if age >= 0.0 and age <= float(LUNA_VISION_CONTEXT_TTL_SEC):
                    src = str(snap.get("source") or "camera").strip().lower() or "camera"
                    summary = str(snap["summary"]).strip()
                    note = (
                        f"\n\n_(Same analyzed snapshot as ~{int(age)}s ago; {src} still marked active.)_"
                        if age > 2.5
                        else ""
                    )
                    data["vision_context"] = {
                        "active": True,
                        "source": src,
                        "summary": summary + note,
                        "from_cache": True,
                    }
    except Exception:
        pass
    # Optional avatar self-state from VRM frontend so Luna can reason about her own live body motion.
    try:
        vrm_state = data.get("vrm_state") if isinstance(data, dict) else None
        if isinstance(vrm_state, dict) and bool(vrm_state.get("active")):
            ps = data.get("page_state") if isinstance(data.get("page_state"), dict) else {}
            ps["vrm_state"] = vrm_state
            data["page_state"] = ps
    except Exception:
        pass
    msg = (data.get("message") or "").strip()
    user_msg_lc = msg.lower()
    if not msg: return jsonify({"error": "No message"}), 400
    # Always expose current media mode (camera/screen) so chat can acknowledge it
    # even when a fresh vision summary is not available yet.
    try:
        ms = data.get("media_state") if isinstance(data.get("media_state"), dict) else {}
        if ms and bool(ms.get("active")):
            mtype = str(ms.get("type") or "camera").strip().lower() or "camera"
            src_plain = (
                "desktop screen share (not the webcam)"
                if mtype == "screen"
                else "webcam/camera"
                if mtype == "camera"
                else mtype.replace("_", " ")
            )
            mm_turn = bool(data.get("_moondream_multimodal_bytes"))
            unified_md = _moondream_unified_ollama_chat()
            # Moondream gets pixels via `images` on /api/chat only when `_moondream_multimodal_bytes` is set.
            # If we kept the old "vision text absent → say not decoded" rule, she'd repeat that every turn—there is no text summary for multimodal.
            if unified_md:
                vision_rule = (
                    (
                        "A JPEG snapshot is attached to this chat turn—answer using what you see in it plus their words. "
                        "Stay in Luna's persona.\n"
                    )
                    if mm_turn
                    else (
                        "Live feed is marked on but **no snapshot image was attached to this message** (intermittent capture). "
                        "Do not claim you see the frame or cite on-screen pixels; keep chatting normally or ask them to try again "
                        "so the next send includes a frame.\n"
                    )
                )
            else:
                vision_rule = (
                    "When [Live vision context] appears below, treat it as ground truth from their "
                    f"{src_plain}. If that block is missing for this message, you only know the feed is on—you "
                    "do not have pixel detail here—do not invent what's on screen.\n"
                )
            msg = (
                f"[Live media mode]\n"
                f"active=true; source={mtype}\n"
                f"[Rule]\n"
                f"The user has selected: **{src_plain}**. Describe it ONLY that way—never call screen share a \"camera\" or \"webcam\". "
                f"{vision_rule}"
                f"[User message]\n{msg}"
            )
    except Exception:
        pass
    # If a fresh vision summary exists (camera/screen share), inject it directly into the
    # user turn text so the active chat model cannot miss visual context.
    try:
        vc = data.get("vision_context") if isinstance(data.get("vision_context"), dict) else {}
        if vc and bool(vc.get("active")) and not data.get("_moondream_multimodal_bytes"):
            src = str(vc.get("source") or "camera").strip().lower() or "camera"
            vsum = str(vc.get("summary") or "").strip()
            if vsum:
                wants_direct_vision_answer = any(
                    k in user_msg_lc
                    for k in (
                        "what do you see",
                        "what are you seeing",
                        "what can you see",
                        "what can u see",
                        "what do u see",
                        "what's on screen",
                        "whats on screen",
                        "what is on screen",
                        "what's on my screen",
                        "whats on my screen",
                        "what is on my screen",
                        "what am i showing",
                        "what do i have open",
                        "what app is this",
                        "what is this app",
                        "what website is this",
                        "identify this",
                    )
                )
                ambient_vision_rule = (
                    "Ambient vision: You have the user's latest camera/screen snapshot below. "
                    "Whenever their message touches what's visible—apps, UI text, errors, games, browser tabs, homework on screen—"
                    "ground your reply in those facts first. They should not need to ask 'what do you see'; infer when it helps. "
                    "For abstract chat clearly unrelated to their display, respond normally without forcing visual detail."
                )
                wants_gameplay_feedback = any(
                    k in user_msg_lc
                    for k in (
                        "my gameplay",
                        "how is my gameplay",
                        "how's my gameplay",
                        "how am i playing",
                        "am i playing good",
                        "am i playing well",
                        "how am i doing",
                        "what do you think of my gameplay",
                        "rate my gameplay",
                        "tips for my gameplay",
                    )
                )
                direct_vision_rule = ambient_vision_rule + (
                    " Here they explicitly asked for identification—lead with concrete visible facts (names, text, UI) before commentary."
                    if wants_direct_vision_answer
                    else ""
                )
                gameplay_feedback_rule = (
                    "For gameplay feedback requests, evaluate performance using only visible evidence: positioning, cooldown/ability use, "
                    "objective timing, map awareness cues, scoreboard/KDA/CS if visible, and current risk. "
                    "Give 1-2 strengths, 1-2 concrete fixes, and one immediate next action. "
                    "If key HUD info is not visible, say what is missing before judging."
                    if wants_gameplay_feedback
                    else ""
                )
                msg = (
                    f"[Live vision context — {src}]\n"
                    f"{vsum[:2400]}\n"
                    f"[Interpretation rule]\n"
                    f"The block above is a precise, model-generated read of ONE current frame (not you guessing from memory). "
                    f"Treat it as primary ground truth for what is on camera/screen. If it says UNCERTAIN between games/apps, "
                    f"do not override it with a single confident guess—reflect the uncertainty. "
                    f"Use the IDENTITY and CONFIDENCE fields when the user cares what is on screen. "
                    f"{direct_vision_rule} "
                    f"{gameplay_feedback_rule} "
                    f"Prioritize this over unrelated telemetry. Stay in Luna's persona. If something is still unclear, say so.\n"
                    f"[User message]\n{msg}"
                )
    except Exception:
        pass
    # Inject concise VRM self-state so chat model is explicitly aware of current avatar motion/mood.
    try:
        vs = data.get("vrm_state") if isinstance(data.get("vrm_state"), dict) else {}
        if vs and bool(vs.get("active")):
            speaking_now = bool(vs.get("speaking"))
            body_mode = str(vs.get("body_mode") or ("talk" if speaking_now else "idle")).strip().lower() or "idle"
            dom = str(vs.get("dominant_emotion") or "neutral").strip().lower() or "neutral"
            pb = 0.0
            try:
                pb = float(vs.get("procedural_blend") or 0.0)
            except Exception:
                pb = 0.0
            intent = vs.get("intent") if isinstance(vs.get("intent"), dict) else {}
            intent_name = str(intent.get("name") or "baseline").strip().lower() or "baseline"
            ex = ge = he = 0.0
            try:
                ex = float(intent.get("expressivity") or 0.0)
                ge = float(intent.get("gesture") or 0.0)
                he = float(intent.get("head") or 0.0)
            except Exception:
                ex = ge = he = 0.0
            summary = (
                f"speaking={speaking_now}; body_mode={body_mode}; dominant_emotion={dom}; "
                f"procedural_blend={max(0.0, min(1.0, pb)):.2f}; intent={intent_name}; "
                f"intent_levels(expressivity={max(0.0, min(1.0, ex)):.2f}, gesture={max(0.0, min(1.0, ge)):.2f}, head={max(0.0, min(1.0, he)):.2f})"
            )
            msg = (
                f"[Live VRM self-state — internal telemetry, never for the user]\n"
                f"{summary}\n"
                f"[Behavior rule]\n"
                f"Use this state only to choose tone and bracket tags. "
                f"Do not repeat, quote, paraphrase, or output this telemetry or any line like \"speaking=\" / \"body_mode=\" / \"intent_levels(\" in your reply.\n"
                f"[User message]\n{msg}"
            )
    except Exception:
        pass
    scope = LINKED_SCOPE or "web"
    out = _execute_chat_turn(scope, msg, data, chat_source="web")
    if out.get("need_feedback"):
        return jsonify({"reply": out.get("message") or "Luna is asking…", "need_feedback": True,
                        "request_id": out["request_id"], "message": out["message"], "options": out.get("options")})
    return jsonify({"reply": out.get("reply", "")})

@web.route("/api/stream", methods=["POST"])
def api_stream():
    """True streaming endpoint — yields chunks as Ollama generates them."""
    global _last_user_activity, _last_streamer_luna_at
    data = request.get_json(force=True, silent=True) or {}
    msg = (data.get("message") or "").strip()
    if not msg: return jsonify({"error": "No message"}), 400
    scope = LINKED_SCOPE or "web"
    _last_user_activity = time.time()
    if (LINKED_SCOPE and (scope or "").strip() == (LINKED_SCOPE or "").strip()) or (
        _linked_int and (scope or "").strip() == f"discord:user:{_linked_int}"
    ):
        _last_streamer_luna_at = time.time()
    fast_override = _chat_fast_from_request(data)
    use_fast = _chat_fast_enabled() if fast_override is None else fast_override
    history = _compact_history(get_recent_conversation(scope, 30), fast=use_fast, scope=scope)
    # Correction detection (same as api_chat)
    if history:
        prev_luna = next((h["content"] for h in reversed(history) if h.get("role") == "assistant"), "")
        correction = _detect_correction(msg, prev_luna)
        if correction:
            _store_correction(correction)
    system = _prepare_main_chat_system(
        scope, msg, fast=use_fast, force_stream_mode=_force_stream_mode_from_request_data(data)
    )
    def _gen():
        full = []
        for chunk in ollama_stream(
            msg, system=system, scope=scope, history=history, compact=use_fast
        ):
            full.append(chunk)
            yield f"data: {json.dumps({'chunk': chunk}, ensure_ascii=False)}\n\n"
        reply = _sanitize_luna_reply("".join(full).strip())
        append_exchange(scope, msg, reply)
        try:
            _vrchat_publish_reply(reply)
        except Exception:
            pass

        def _stream_post_memory():
            try:
                _capture_memory(scope, msg, reply)
                _capture_profile(scope, msg)
            except Exception:
                pass
            try:
                _update_rolling_summary(scope)
            except Exception:
                pass
        threading.Thread(target=_stream_post_memory, daemon=True).start()
        yield f"data: {json.dumps({'done': True})}\n\n"
    return Response(stream_with_context(_gen()), mimetype="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})

@web.route("/api/obs/start", methods=["POST"])
def api_obs_start():
    """Launch OBS Studio from Start Menu shortcut (Windows)."""
    data = request.get_json(force=True, silent=True) or {}
    default_lnk = r"C:\ProgramData\Microsoft\Windows\Start Menu\Programs\OBS Studio.lnk"
    path = os.path.abspath(os.path.expanduser((data.get("path") or default_lnk).strip() or default_lnk))
    if not os.path.isfile(path):
        return jsonify({"error": "OBS shortcut not found", "path": path}), 404
    try:
        if hasattr(os, "startfile"):
            os.startfile(path)  # type: ignore[attr-defined]
        else:
            subprocess.Popen(["cmd", "/c", "start", "", path], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        return jsonify({"ok": True, "path": path})
    except Exception as e:
        return jsonify({"error": f"Could not launch OBS: {e}", "path": path}), 500

@web.route("/api/audio-podcast/start", methods=["POST"])
def api_audio_podcast_start():
    """Launch Audio-Podcast app (launch.py) and optionally open its UI URL."""
    data = request.get_json(force=True, silent=True) or {}
    ok, status, project_dir, url = _start_audio_podcast((data.get("project_dir") or "").strip())
    if not ok:
        code = 404 if "not found" in status.lower() else 500
        return jsonify({"error": status, "project_dir": project_dir, "url": url}), code
    if bool(data.get("open_ui", True)):
        try:
            webbrowser.open(url)
        except Exception:
            pass
    return jsonify({"ok": True, "status": status, "project_dir": project_dir, "url": url})


@web.route("/api/ngrok/authtoken", methods=["GET"])
def api_ngrok_authtoken_status():
    if not _is_local_web_request():
        return jsonify({"error": "Local access only"}), 403
    d = _load_json(_NGROK_STATE_PATH, {})
    if not isinstance(d, dict):
        d = {}
    ngrok_cmd = _resolve_ngrok_cmd()
    return jsonify(
        {
            "ok": True,
            "configured": bool(d.get("configured")),
            "last4": (d.get("last4") or ""),
            "updated_at": float(d.get("updated_at") or 0.0),
            "ngrok_available": bool(ngrok_cmd),
            "ngrok_cmd": (ngrok_cmd or ""),
        }
    )


@web.route("/api/ngrok/authtoken", methods=["POST"])
def api_ngrok_authtoken_set():
    if not _is_local_web_request():
        return jsonify({"error": "Local access only"}), 403
    data = request.get_json(force=True, silent=True) or {}
    token = str(data.get("token") or "").strip()
    if len(token) < 12:
        return jsonify({"error": "Token looks too short"}), 400
    ngrok_cmd = _resolve_ngrok_cmd()
    if not ngrok_cmd:
        return jsonify({"error": "ngrok not found. Set NGROK_EXE in .env or install ngrok in a standard path."}), 500
    try:
        proc = subprocess.run(
            [ngrok_cmd, "config", "add-authtoken", token],
            capture_output=True,
            text=True,
            timeout=25,
        )
    except FileNotFoundError:
        return jsonify({"error": "ngrok executable was not found at runtime"}), 500
    except subprocess.TimeoutExpired:
        return jsonify({"error": "Timed out while applying ngrok token"}), 504
    except Exception as e:
        return jsonify({"error": f"Could not run ngrok: {str(e)[:120]}"}), 500
    if proc.returncode != 0:
        err = (proc.stderr or proc.stdout or "ngrok rejected the token").strip()
        return jsonify({"error": err[:300]}), 400

    _save_json(
        _NGROK_STATE_PATH,
        {
            "configured": True,
            "last4": token[-4:],
            "updated_at": time.time(),
        },
    )
    return jsonify({"ok": True, "configured": True, "last4": token[-4:]})

@web.route("/api/tts", methods=["POST"])
def api_tts():
    text = ((request.get_json(force=True, silent=True) or {}).get("text") or "").strip()
    if not text: return jsonify({"error": "No text"}), 400
    audio = _tts_bytes(text)
    if not audio: return jsonify({"error": "TTS failed"}), 500
    return Response(audio, mimetype="audio/mpeg")

@web.route("/api/tts-stop", methods=["POST"])
def api_tts_stop():
    _stop_tts(); return jsonify({"ok": True})

@web.route("/api/tts-busy")
def api_tts_busy():
    """Returns whether TTS is currently playing or queued. Podcast studio polls this to know when to resume video."""
    with _tts_lock:
        proc_active = _tts_proc is not None and _tts_proc.poll() is None
    queue_pending = not _tts_turn_queue.empty()
    return jsonify({"busy": proc_active or queue_pending})


@web.route("/api/vrm-presence", methods=["POST"])
def api_vrm_presence():
    """Called by vrm_viewer.html on an interval while the 3D tab is open."""
    global _VRM_PRESENCE_TS
    data = request.get_json(force=True, silent=True) or {}
    if data.get("active") is False:
        _VRM_PRESENCE_TS = 0.0
    else:
        _VRM_PRESENCE_TS = time.time()
    return jsonify({"ok": True})


# ── Hub: optional speaker embedding (resemblyzer) — enroll in podcast studio, gate /api/transcribe ──
_VOICE_EMBED_PATH = os.path.join(_DATA, "user_voice_embedding.npy")
try:
    _VOICE_MATCH_MIN = float(_env("VOICE_MATCH_MIN", "0.72") or "0.72")
except Exception:
    _VOICE_MATCH_MIN = 0.72
_VOICE_VERIFY_ON = _env("VOICE_SPEAKER_VERIFY", "1").strip().lower() not in ("0", "false", "no", "off")


def _resemblyzer():
    """Optional dependency — loaded via importlib so static analysis does not require the package."""
    hit = getattr(_resemblyzer, "_mod", None)
    if hit is False:
        return None
    if hit is not None:
        return hit
    try:
        import importlib

        mod = importlib.import_module("resemblyzer")
        _resemblyzer._mod = mod
        return mod
    except Exception:
        _resemblyzer._mod = False
        return None


def _voice_encoder():
    rz = _resemblyzer()
    if rz is None:
        return None
    try:
        enc = getattr(_voice_encoder, "_enc", None)
        if enc is None:
            enc = rz.VoiceEncoder(device="cpu")
            _voice_encoder._enc = enc
        return enc
    except Exception:
        return None


def _ffmpeg_to_wav16_mono_voice(src: str, dst_wav: str) -> bool:
    try:
        subprocess.run(
            ["ffmpeg", "-nostdin", "-y", "-i", src, "-ar", "16000", "-ac", "1", "-f", "wav", dst_wav],
            capture_output=True,
            timeout=120,
            check=True,
            creationflags=_subprocess_no_window_flags(),
        )
        return os.path.isfile(dst_wav) and os.path.getsize(dst_wav) > 200
    except Exception:
        return False


def _embed_from_wav_path(wav_path: str):
    try:
        rz = _resemblyzer()
        if rz is None:
            return None
        enc = _voice_encoder()
        if enc is None:
            return None
        wav = rz.preprocess_wav(wav_path)
        if wav is None or len(wav) < 4000:
            return None
        return enc.embed_utterance(wav)
    except Exception:
        return None


def _voice_profile_load():
    try:
        import numpy as np

        if not os.path.isfile(_VOICE_EMBED_PATH):
            return None
        return np.load(_VOICE_EMBED_PATH)
    except Exception:
        return None


def _voice_similarity_to_profile(wav_path: str) -> float | None:
    import numpy as np

    ref = _voice_profile_load()
    if ref is None:
        return None
    emb = _embed_from_wav_path(wav_path)
    if emb is None:
        return None
    ref = np.asarray(ref).flatten()
    emb = np.asarray(emb).flatten()
    denom = (np.linalg.norm(ref) * np.linalg.norm(emb)) + 1e-9
    return float(np.dot(ref, emb) / denom)


def _voice_verify_ok_for_webm(webm_path: str) -> tuple[bool, float | None]:
    """No profile or encoder off → (True, None). Low match → (False, score)."""
    if not _VOICE_VERIFY_ON or _voice_profile_load() is None or _voice_encoder() is None:
        return True, None
    fd, wav = tempfile.mkstemp(suffix=".wav")
    os.close(fd)
    try:
        if not _ffmpeg_to_wav16_mono_voice(webm_path, wav):
            return True, None
        sim = _voice_similarity_to_profile(wav)
        if sim is None:
            return True, None
        if sim < _VOICE_MATCH_MIN:
            return False, sim
        return True, sim
    finally:
        try:
            if os.path.isfile(wav):
                os.unlink(wav)
        except Exception:
            pass


@web.route("/api/voice-profile", methods=["GET", "DELETE"])
def api_voice_profile():
    if request.method == "DELETE":
        try:
            if os.path.isfile(_VOICE_EMBED_PATH):
                os.remove(_VOICE_EMBED_PATH)
            return jsonify({"ok": True})
        except Exception as e:
            return jsonify({"ok": False, "error": str(e)[:200]}), 500
    return jsonify({
        "enrolled": os.path.isfile(_VOICE_EMBED_PATH),
        "verify_enabled": _VOICE_VERIFY_ON,
        "encoder_ready": _voice_encoder() is not None,
        "match_min": _VOICE_MATCH_MIN,
    })


@web.route("/api/voice-enroll", methods=["POST"])
def api_voice_enroll():
    raw = request.get_data()
    if not raw or len(raw) < 800:
        return jsonify({"ok": False, "error": "Audio too short — speak at least a few seconds."}), 400
    if _voice_encoder() is None:
        return jsonify({
            "ok": False,
            "error": "Voice ID needs Resemblyzer. Windows/Python 3.13: pip install webrtcvad-wheels && pip install resemblyzer --no-deps (then restart Luna). See requirements-ml.txt.",
        }), 503
    fd_webm = path_webm = None
    wav_path = None
    try:
        fd_webm, path_webm = tempfile.mkstemp(suffix=".webm")
        os.write(fd_webm, raw)
        os.close(fd_webm)
        fd_webm = None
        fd_wav, wav_path = tempfile.mkstemp(suffix=".wav")
        os.close(fd_wav)
        if not _ffmpeg_to_wav16_mono_voice(path_webm, wav_path):
            return jsonify({"ok": False, "error": "Could not decode audio (need ffmpeg)."}), 400
        emb = _embed_from_wav_path(wav_path)
        if emb is None:
            return jsonify({"ok": False, "error": "Could not build embedding — try a longer, clearer clip."}), 400
        import numpy as np

        os.makedirs(_DATA, exist_ok=True)
        np.save(_VOICE_EMBED_PATH, emb)
        return jsonify({"ok": True})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)[:200]}), 500
    finally:
        for p in (path_webm, wav_path):
            if p and os.path.isfile(p):
                try:
                    os.unlink(p)
                except Exception:
                    pass
        if fd_webm is not None:
            try:
                os.close(fd_webm)
            except Exception:
                pass


@web.route("/api/transcribe", methods=["POST"])
def api_transcribe():
    raw = request.get_data()
    if not raw or len(raw) < 100: return jsonify({"error": "No audio"}), 400
    fd = path = None
    try:
        fd, path = tempfile.mkstemp(suffix=".webm")
        os.write(fd, raw); os.close(fd); fd = None
        ok_gate, score = _voice_verify_ok_for_webm(path)
        if not ok_gate:
            return jsonify({
                "text": "",
                "speaker_match": False,
                "speaker_score": round(score or 0.0, 4),
            })
        try:
            ok, text_out = _groq_stt_try(path)
            if not ok:
                if not GROQ_STT_LOCAL_FALLBACK:
                    return jsonify({
                        "error": "Transcription failed (GROQ_STT_LOCAL_FALLBACK=0; set GROQ_API_KEY or enable local fallback)",
                    }), 503
                text_out = _api_transcribe_local_whisper(path)
                if text_out is None:
                    return jsonify({
                        "error": "Transcription failed (Groq and local Whisper; install openai-whisper+ffmpeg, check GROQ_API_KEY)",
                    }), 503
            out = {"text": text_out or "", "speaker_match": True}
            emo = _analyze_voice_clip_emotion(path, text_out)
            if isinstance(emo, dict):
                out["emotion"] = emo.get("emotion") or "neutral"
                out["emotion_meta"] = {
                    "volume": emo.get("volume"),
                    "rms": emo.get("rms"),
                    "variation": emo.get("variation"),
                    "zcr": emo.get("zcr"),
                    "voiced_ratio": emo.get("voiced_ratio"),
                    "model": emo.get("model"),
                    "model_emotion": emo.get("model_emotion"),
                    "model_confidence": emo.get("model_confidence"),
                }
            if score is not None:
                out["speaker_score"] = round(score, 4)
            return jsonify(out)
        except ImportError:
            return jsonify({"error": "Install openai-whisper and ffmpeg"}), 503
    except Exception as e:
        return jsonify({"error": str(e)[:200]}), 500
    finally:
        try:
            if fd is not None: os.close(fd)
            if path and os.path.isfile(path): os.unlink(path)
        except Exception: pass


@web.route("/api/translate-voice", methods=["POST"])
def api_translate_voice():
    """Accept uploaded audio (raw body or multipart file); translate to English via Whisper."""
    raw = None
    path = None
    try:
        if request.content_type and "multipart" in str(request.content_type or "").lower() and request.files:
            f = request.files.get("file") or request.files.get("audio")
            if f and f.filename:
                ext = os.path.splitext(f.filename)[1] or ".ogg"
                fd, path = tempfile.mkstemp(suffix=ext)
                try:
                    f.save(path)
                finally:
                    try: os.close(fd)
                    except Exception: pass
        if not path:
            raw = request.get_data()
            if not raw or len(raw) < 100:
                return jsonify({"error": "No audio: send raw body or multipart file"}), 400
            fd, path = tempfile.mkstemp(suffix=".ogg")
            os.write(fd, raw)
            os.close(fd)
        if not path or not os.path.isfile(path):
            return jsonify({"error": "No audio"}), 400
        text = _whisper_translate(path)
        if text is None:
            return jsonify({"error": "Translation failed (install openai-whisper and ffmpeg)"}), 503
        out = {"text": text}
        emo = _analyze_voice_clip_emotion(path, text)
        if isinstance(emo, dict):
            out["emotion"] = emo.get("emotion") or "neutral"
            out["emotion_meta"] = {
                "volume": emo.get("volume"),
                "rms": emo.get("rms"),
                "variation": emo.get("variation"),
                "zcr": emo.get("zcr"),
                "voiced_ratio": emo.get("voiced_ratio"),
                "model": emo.get("model"),
                "model_emotion": emo.get("model_emotion"),
                "model_confidence": emo.get("model_confidence"),
            }
        return jsonify(out)
    except Exception as e:
        return jsonify({"error": str(e)[:200]}), 500
    finally:
        try:
            if path and os.path.isfile(path):
                os.unlink(path)
        except Exception:
            pass


def _handle_bang(msg: str, scope: str, meta: dict | None = None) -> str:
    meta = meta if isinstance(meta, dict) else {}
    da_raw = meta.get("discord_author_id")
    discord_uid = int(da_raw) if da_raw is not None and str(da_raw).strip().isdigit() else None
    tw_login_meta = meta.get("twitch_login")
    tw_login_m = str(tw_login_meta).strip().lower() if tw_login_meta else None
    parts = msg.split(None, 1)
    cmd = parts[0].lower()
    args = (parts[1] if len(parts) > 1 else "").strip()
    use_scope = scope or LINKED_SCOPE or "web"
    if cmd in ("!help","!commands","!files"): return HELP_TEXT
    if cmd == "!rank":
        actor_key, _rest = _relationship_parse_target_arg(args, use_scope)
        row = _relationship_get(actor_key)
        if not row:
            return f"No rank yet for **{actor_key}**. Talk with Luna first."
        pts = int(row.get("points", 0) or 0)
        rank = row.get("rank") or _rel_rank_for_points(pts)
        return f"🏅 **{actor_key}** is **{rank}** with **{pts}** points."
    if cmd == "!bond":
        if args.lower() in ("top", "leaderboard", "lb"):
            return _relationship_top_text(use_scope)
        actor_key, _rest = _relationship_parse_target_arg(args, use_scope)
        row = _relationship_get(actor_key)
        return _relationship_format_summary(actor_key, row, include_notes=True)
    if cmd == "!notes":
        actor_key, _rest = _relationship_parse_target_arg(args, use_scope)
        row = _relationship_get(actor_key)
        if not row:
            return f"No notes yet for **{actor_key}**."
        notes = row.get("notes")
        if not isinstance(notes, list) or not notes:
            return f"No notes yet for **{actor_key}**."
        return "🗒️ **Notes**\n" + "\n".join(f"- {str(n)[:140]}" for n in notes[-12:])
    if cmd == "!note":
        low_args = args.lower().strip()
        if not low_args.startswith("add "):
            return "Usage: !note add [user] <text>"
        body = args[4:].strip()
        actor_key, remainder = _relationship_parse_target_arg(body, use_scope)
        if actor_key == use_scope and remainder == body:
            remainder = body
        ok, m = _relationship_add_note(actor_key, remainder)
        return f"✅ {m}" if ok else f"❌ {m}"
    if cmd in ("!twitch_title", "!title"):
        if not args:
            return "Usage: !twitch_title <new stream title>"
        ok, r = _twitch_update_title(args)
        return f"✅ {r}" if ok else f"❌ {r}"
    if cmd in ("!twitch_poll", "!poll"):
        # Manual: !twitch_poll Title | option1 | option2 [| option3...]
        # Autonomous: !twitch_poll <topic>  -> Luna drafts title/options and creates immediately.
        if not args:
            return "Usage: !twitch_poll <title> | <option1> | <option2> [| <option3> ...] OR !twitch_poll <topic>"
        parts_poll = [p.strip() for p in args.split("|")]
        duration = 120
        q = ""
        opts: list[str] = []
        if len(parts_poll) >= 3:
            q = parts_poll[0]
            opts = parts_poll[1:]
        else:
            q, opts = _twitch_poll_idea_from_topic(args)
        ok, r = _twitch_create_poll(q, opts, duration=duration)
        return f"✅ {r}" if ok else f"❌ {r}"
    if cmd == "!twitch_chat_redirect":
        return _twitch_exec_chat_redirect(args, twitch_login=tw_login_m, discord_uid=discord_uid)
    if cmd == "!twitch_say":
        return _twitch_exec_say(args, twitch_login=tw_login_m, discord_uid=discord_uid)
    if cmd == "!news":
        ok, r = _fetch_news(); return r if ok else f"❌ {r}"
    if cmd == "!search":
        if not args: return "Usage: !search <query>"
        ok, r = _search(args); return f"✅ {r}" if ok else f"❌ {r}"
    if cmd == "!scrape":
        if not args: return "Usage: !scrape <url> <what to extract> [post:#channel-name]"
        # Parse: first token is URL, optional post:#channel at the end, rest is instruction
        tokens = args.split()
        scrape_url = tokens[0]
        post_ch = ""
        rest_tokens = tokens[1:]
        for i, t in enumerate(rest_tokens):
            if t.lower().startswith("post:#") or t.lower().startswith("post:"):
                post_ch = t.split(":", 1)[1].lstrip("#")
                rest_tokens = rest_tokens[:i] + rest_tokens[i+1:]
                break
        instruction = " ".join(rest_tokens).strip()
        ok, r = _scrape_website(scrape_url, instruction, post_ch)
        return f"✅ {r}" if ok else f"❌ {r}"
    if cmd == "!research":
        if not args: return "Usage: !research <topic>"
        ok, r = _research_content(args)
        return f"✅ {r}" if ok else f"❌ {r}"
    if cmd in ("!research_story", "!story_script"):
        if not args: return "Usage: !research_story <topic>"
        ok, r = _research_story_script(args)
        return f"✅ {r}" if ok else f"❌ {r}"
    if cmd == "!audiobook":
        al = args.lower().strip()
        sc = scope or LINKED_SCOPE or "web"
        if not args.strip():
            return (
                "Usage: **!audiobook create** <topic> [duration:30] [--brief|--story|--both], "
                "or **!audiobook continue**, or **!audiobook cancel**"
            )
        if al == "continue" or al.startswith("continue "):
            ok, r = _audiobook_continue_next(sc)
            return f"✅ {r}" if ok else f"❌ {r}"
        if al == "cancel" or al.startswith("cancel "):
            ok, r = _audiobook_cancel_pending(sc)
            return f"✅ {r}" if ok else f"❌ {r}"
        if al.startswith("create "):
            topic = args[7:].strip()
        else:
            topic = args.strip()
        if not topic:
            return "Usage: !audiobook create <topic> [duration:30] [--brief|--story|--both]"
        ok, r = _create_audiobook_from_research(topic, scope=sc)
        return f"✅ {r}" if ok else f"❌ {r}"
    if cmd == "!summarize":
        ok, r = _summarize_input(args)
        return f"✅ {r}" if ok else f"❌ {r}"
    if cmd == "!digest":
        return _daily_digest(use_scope)
    if cmd == "!calendar":
        if not args:
            return _calendar_list(use_scope, mode="upcoming")
        low_args = args.lower().strip()
        if low_args in ("list", "upcoming"):
            return _calendar_list(use_scope, mode="upcoming")
        if low_args == "today":
            return _calendar_list(use_scope, mode="today")
        if low_args in ("week", "this week"):
            return _calendar_list(use_scope, mode="week")
        if low_args.startswith("delete "):
            ok, msg = _calendar_delete(use_scope, args[7:].strip())
            return f"✅ {msg}" if ok else f"❌ {msg}"
        m = re.match(r"^(?:add\s+)?(\d{4}-\d{2}-\d{2})\s+(\d{1,2}:\d{2})\s+(.+)$", args, re.I)
        if m:
            ok, msg = _calendar_add(use_scope, m.group(1), m.group(2), m.group(3).strip())
            return f"✅ {msg}" if ok else f"❌ {msg}"
        return "Usage: !calendar add YYYY-MM-DD HH:MM title | !calendar list | !calendar today | !calendar week | !calendar delete <n>"
    if cmd == "!todo":
        if not args:
            return _todo_list_text(use_scope)
        low_args = args.lower().strip()
        if low_args.startswith("add "):
            return _todo_add(use_scope, args[4:].strip())
        if low_args.startswith("done "):
            return _todo_done(use_scope, args[5:].strip())
        if low_args in ("list", "ls"):
            return _todo_list_text(use_scope)
        return "Usage: !todo add <task> | !todo list | !todo done <number>"
    if cmd in ("!suno_ready", "!suno_logged_in"):
        return f"✅ {_mark_suno_logged_in()}"
    if cmd == "!suno":
        if not args: return "Usage: !suno <description>"
        ok, r = _run_suno(args)
        if not ok: _record_failure("suno", r, {})
        return f"✅ {r}" if ok else f"❌ {r}"
    if cmd in ("!share_song","!share-song"):
        ok, r = _run_x_share()
        if not ok: _record_failure("share_x", r, {})
        return f"✅ {r}" if ok else f"❌ {r}"
    if cmd in ("!share_facebook","!share-facebook"):
        ok, r = _run_fb_share()
        if not ok: _record_failure("share_facebook", r, {})
        return f"✅ {r}" if ok else f"❌ {r}"
    if cmd in ("!distrokid_plan", "!dk_plan", "!release_plan"):
        return _distrokid_plan_text(args)
    if cmd in ("!yt_comment","!youtube_comment"):
        if not args: return "Usage: !yt_comment <url>"
        url = _yt_url_from_freeform_arg(args)
        if not url or not _yt_extract_id(url):
            return "Usage: !yt_comment <YouTube video URL> (paste the full watch, shorts, or youtu.be link)."
        ok, r = _yt_comment(url)
        if not ok: _record_failure("yt_comment", r, {"video_url": url})
        return f"✅ {r}" if ok else f"❌ {r}"
    if cmd in ("!yt_like", "!youtube_like"):
        if not args: return "Usage: !yt_like <url>"
        url = _yt_url_from_freeform_arg(args)
        if not url or not _yt_extract_id(url):
            return "Usage: !yt_like <YouTube video URL> (paste the full link)."
        ok, r = _yt_like_one(url)
        if not ok: _record_failure("yt_like", r, {"video_url": url})
        return f"✅ {r}" if ok else f"❌ {r}"
    if cmd in ("!yt_react", "!youtube_react"):
        if not args: return "Usage: !yt_react <url>"
        url = _yt_url_from_freeform_arg(args)
        if not url or not _yt_extract_id(url):
            return "Usage: !yt_react <YouTube video URL> (paste the full link)."
        ok, r = _yt_react(url)
        if not ok: _record_failure("yt_react", r, {"video_url": url})
        return f"✅ {r}" if ok else f"❌ {r}"
    if cmd in ("!yt_watch_react", "!youtube_watch_react"):
        if not args: return "Usage: !yt_watch_react <url>"
        url = _yt_url_from_freeform_arg(args)
        if not url or not _yt_extract_id(url):
            return "Usage: !yt_watch_react <YouTube video URL> (paste the full link)."
        ok, r = _yt_watch_react_start(url, scope or LINKED_SCOPE or "web")
        if not ok:
            _record_failure("yt_watch_react", r, {"video_url": url})
        return f"✅ {r}" if ok else f"❌ {r}"
    if cmd in ("!yt_watch_stop", "!youtube_watch_stop"):
        ok, r = _yt_watch_stop(scope or LINKED_SCOPE or "web")
        return f"✅ {r}" if ok else f"❌ {r}"
    if cmd in ("!x_react", "!twitter_react"):
        if not args: return "Usage: !x_react <x-post-url>"
        url = (re.search(r"https?://[^\s]+", args) or type("",(), {"group": lambda s,x: args})()).group(0)
        ok, r = _x_react(url)
        if not ok: _record_failure("x_react", r, {"post_url": url})
        return f"✅ {r}" if ok else f"❌ {r}"
    if cmd in ("!yt_analytics", "!youtube_analytics"):
        days, limit = 30, 5
        if args:
            nums = [int(x) for x in re.findall(r"\d+", args)]
            if nums: days = nums[0]
            if len(nums) > 1: limit = nums[1]
        ok, r = _yt_channel_analytics(days=days, limit=limit)
        if not ok: _record_failure("yt_analytics", r, {"days": days, "limit": limit})
        return f"✅ {r}" if ok else f"❌ {r}"
    if cmd in ("!status", "!set_status"):
        if not args:
            return "Usage: !status <text> | !status clear | !status <listening|playing|watching|competing> <text>"
        al = args.strip()
        if al.lower() in ("clear", "off", "none"):
            try:
                loop = getattr(bot, "loop", None)
                if loop and loop.is_running():
                    fut = asyncio.run_coroutine_threadsafe(_set_discord_status("", "listening"), loop)
                    ok, msg = fut.result(timeout=10)
                    return f"{'✅' if ok else '❌'} {msg}"
            except Exception as e:
                return f"❌ {e}"
            return "❌ Discord not ready."
        parts = al.split(None, 1)
        kind = "listening"
        text = al
        if len(parts) == 2 and parts[0].lower() in ("listening", "playing", "watching", "competing"):
            kind = parts[0].lower()
            text = parts[1].strip()
        try:
            loop = getattr(bot, "loop", None)
            if loop and loop.is_running():
                fut = asyncio.run_coroutine_threadsafe(_set_discord_status(text, kind), loop)
                ok, msg = fut.result(timeout=10)
                return f"{'✅' if ok else '❌'} {msg}"
        except Exception as e:
            return f"❌ {e}"
        return "❌ Discord not ready."
    if cmd in ("!pc_vitals","!pc_status","!system"):
        return _pc_vitals()
    if cmd in ("!luna_vitals","!luna_status","!your_status"):
        return _luna_vitals()
    if cmd in ("!ig_dm","!igdm"):
        if not args: return "Usage: !ig_dm <username> [message]"
        ps = args.split(None, 1)
        ok, r = _run_ig_dm(ps[0], ps[1] if len(ps) > 1 else "")
        if not ok: _record_failure("ig_dm", r, {"target": ps[0], "message": ps[1] if len(ps) > 1 else ""})
        return f"✅ {r}" if ok else f"❌ {r}"
    if cmd in ("!fb_msg","!messenger"):
        if not args: return "Usage: !fb_msg <name> [: message] or !fb_msg <name> say <message>"
        # Split target from message at ": " or " say " or " msg "
        split_m = re.split(r'\s*:\s+|\s+(?:say|msg|message)\s+', args, maxsplit=1, flags=re.I)
        if len(split_m) == 2:
            target, msg = split_m[0].strip(), split_m[1].strip()
        else:
            target, msg = args.strip(), ""
        ok, r = _run_messenger_msg(target, msg)
        if not ok: _record_failure("fb_msg", r, {"target": target})
        return f"✅ {r}" if ok else f"❌ {r}"
    if cmd == "!call":
        if not args: return "Usage: !call <Discord username or user ID>"
        ok, r = _run_discord_call(args)
        if not ok: _record_failure("call", r, {"contact": args})
        return f"✅ {r}" if ok else f"❌ {r}"
    if cmd == "!dm":
        if not args: return "Usage: !dm <Discord username or user ID> [message]"
        parts = args.split(None, 1)
        target = parts[0].strip().lstrip("@")
        msg = (parts[1].strip() if len(parts) > 1 else "") or ""
        if not target: return "Usage: !dm <username or user ID> [message]"
        ok, r = _run_discord_dm(target, msg)
        if not ok: _record_failure("dm", r, {"target": target})
        return f"✅ {r}" if ok else f"❌ {r}"
    if cmd == "!msg":
        if not args: return "Usage: !msg <contact> [description]"
        contact, desc = _parse_wa_contact_and_msg(args)
        if not contact: return "Usage: !msg <contact> [description]"
        ok, r = _run_wa_msg(contact, desc)
        if not ok: _record_failure("msg", r, {"contact": contact})
        return f"✅ {r}" if ok else f"❌ {r}"
    if cmd in ("!play", "!music"):
        reply = _run_cmd("play", {"query": args}, scope)
        return reply if reply else "Usage: !play <song or URL>"
    if cmd == "!podcast":
        if args.strip().lower().startswith("create "):
            topic = args[7:].strip()
            ok, r = _create_podcast_from_description(topic)
            return f"✅ {r}" if ok else f"❌ {r}"
        choice = args.strip()
        params = {"choice": choice} if choice else {}
        reply = _run_cmd("podcast", params, scope)
        return reply if reply else "❌ Custom podcast failed. Check CUSTOM_PODCAST_DIR and join a voice channel."
    if cmd == "!skip":
        reply = _run_cmd("skip", {}, scope)
        return reply if reply else "❌ Skip failed."
    if cmd == "!stop":
        reply = _run_cmd("stop", {}, scope)
        return reply if reply else "❌ Stop failed."
    if cmd == "!briefing":
        return _generate_morning_briefing()
    if cmd in ("!analytics_screen", "!dashboard_read", "!read_dashboard"):
        ok, r = _describe_dashboard_screenshot()
        if not ok:
            _record_failure("analytics_screen", r, {})
        return f"✅ {r}" if ok else f"❌ {r}"
    if cmd == "!joinme":
        reply = _run_cmd("joinme", {"message": args}, scope)
        return reply if reply else "❌ Join a Discord voice channel first."
    if cmd.startswith("!") and len(cmd) > 1:
        bare = cmd[1:].split()[0] if cmd[1:] else ""
        if bare in _absorbed_tool_names:
            reply = _run_cmd(bare, {"query": args, "raw": msg}, scope)
            return reply if reply else "❌ No output."
    _unknown_bang = (
        f"Unknown command: {cmd}. Use **!help** for the list.",
        f"I don't have **{cmd}**. Try **!help** to see what I can do.",
        f"**{cmd}** isn't something I know. **!help** for the full list.",
    )
    return random.choice(_unknown_bang)

# ── Discord commands ──────────────────────────────────────────────────────────

@bot.event
async def on_ready():
    print(f"Luna online: {bot.user} — {OLLAMA_BASE} / {OLLAMA_MODEL}")
    default_status = DISCORD_STATUS_TEXT or OLLAMA_CHAT
    await _set_discord_status(default_status, DISCORD_STATUS_TYPE)
    bot.loop.create_task(_reminder_loop())
    bot.loop.create_task(_calendar_notification_loop())
    if LUNA_DM_GREETINGS:
        bot.loop.create_task(_dm_greeting_loop())
        md = (
            f" mid-day {LUNA_DM_MIDDAY_H0}-{LUNA_DM_MIDDAY_H1}h (Ollama + profile)"
            if LUNA_DM_MIDDAY
            else " (mid-day off: LUNA_DM_MIDDAY=0)"
        )
        print(
            f"[DM greetings] ON — morning {LUNA_DM_MORNING_H0}-{LUNA_DM_MORNING_H1}h,{md} night {LUNA_DM_NIGHT_H0}-{LUNA_DM_NIGHT_H1}h "
            f"({LUNA_DM_GREET_TZ}). Add gender/pronouns in profile for tone. Set LUNA_DM_GREETINGS=0 to disable.",
            flush=True,
        )
    bot.loop.create_task(_pc_context_observer_loop())
    bot.loop.create_task(_ml_learning_loop())
    bot.loop.create_task(_proactive_heartbeat_loop())
    if _stream_solo_banter_env_on():
        bot.loop.create_task(_stream_solo_banter_loop())
        print(
            "[Stream solo] TAKEOVER: when live (Twitch/LoL/OBS) or stream mode, Luna can fill air after "
            "LUNA_STREAM_SOLO_IDLE_SEC with *broadcaster* silence (viewers in chat no longer block her). "
            "Set TWITCH_BROADCASTER_LOGIN to your Twitch login. LUNA_STREAM_TAKEOVER=0 to disable. "
            "LUNA_STREAM_TAKEOVER_STREAMER_IDLE=0 uses old global activity. "
            "VRM+TTS; LUNA_STREAM_SOLO_BYPASS_VRM=1 for speaker-only. LUNA_STREAM_SOLO_CHAT for Twitch text.",
            flush=True,
        )
    bot.loop.create_task(_reflection_loop())
    bot.loop.create_task(_evolution_loop())
    bot.loop.create_task(_clipboard_monitor_loop())
    bot.loop.create_task(_knowledge_embedding_loop())
    bot.loop.create_task(_wake_word_loop())
    bot.loop.create_task(_rolling_summary_backfill_task())
    if _PUBLISH_ANNOUNCE_CONFIGURED:
        bot.loop.create_task(_publish_announce_loop())
    elif LUNA_PUBLISH_ANNOUNCE_ENABLED and _poll_publish_announce is None:
        print("[Publish announce] luna_publish_announce.py missing — install module next to bot_main.", flush=True)
    elif LUNA_PUBLISH_ANNOUNCE_ENABLED and not _PUBLISH_ANNOUNCE_HAS_OUTPUT:
        print(
            "[Publish announce] No output configured. Set LUNA_PUBLISH_ANNOUNCE_DISCORD_CHANNEL_IDS, "
            "LUNA_PUBLISH_ANNOUNCE_YOUTUBE_X=1, LUNA_PUBLISH_ANNOUNCE_YOUTUBE_FACEBOOK=1, "
            "or LUNA_PUBLISH_ANNOUNCE_TWITCH_LIVE_FACEBOOK=1.",
            flush=True,
        )
    elif LUNA_PUBLISH_ANNOUNCE_ENABLED and not (
        _PUBLISH_ANNOUNCE_YOUTUBE_IDS or _PUBLISH_ANNOUNCE_YOUTUBE_RSS or _PUBLISH_ANNOUNCE_TWITCH_LOGINS
    ):
        print("[Publish announce] Add LUNA_PUBLISH_ANNOUNCE_YOUTUBE_CHANNEL_IDS, *_RSS_URLS, and/or *_TWITCH_LOGINS.", flush=True)
    _lol_observer_run = getattr(_lol_spectator, "observer_should_run", None)
    if _lol_observer_run is None:
        _lol_observer_run = getattr(_lol_spectator, "is_enabled", lambda: False)
    if _lol_spectator and _lol_observer_run() and _LOL_STANDALONE_COMMENTARY:
        bot.loop.create_task(
            _lol_spectator.commentary_loop(
                ollama_chat=ollama_chat,
                ollama_model=OLLAMA_CHAT,
                on_luna_reply=_lol_chat_enqueue,
                build_chat_system=lambda: _prepare_main_chat_system(
                    LINKED_SCOPE or "web",
                    "[LoL spectator] A JSON snapshot of the user's live League match follows in my next message.",
                    fast=_chat_fast_enabled(),
                    force_stream_mode=_stream_mode_env_on(),
                ),
                build_commentary_system=_build_lol_observer_commentary_system,
                # Only generate a commentary line when the user hasn't been talking recently
                should_emit=lambda: time.time() - _last_user_activity >= _LOL_COMMENTARY_USER_QUIET_SEC,
            )
        )
        print(
            f"[LoL spectator] Observer task started. Commentary ON by default — speaks when user idle >{_LOL_COMMENTARY_USER_QUIET_SEC:.0f}s. "
            "Set RIOT_LOL_EMIT_COMMENTARY=0 to disable, LUNA_LOL_COMMENTARY_QUIET_SEC to tune silence window.",
            flush=True,
        )
    elif _lol_spectator and _lol_observer_run() and not _LOL_STANDALONE_COMMENTARY:
        print(
            "[LoL spectator] Standalone commentary loop OFF (using fused LoL data + vision in normal chat turns). "
            "Set LUNA_LOL_STANDALONE_COMMENTARY=1 to re-enable old JSON-only auto commentary.",
            flush=True,
        )

@bot.event
async def on_message(message: discord.Message):
    if message.author.bot: return

    # Celine: voice clips
    effective = (message.content or "").strip()
    celine_route = None
    voice_from_clip = False
    voice_emotion_meta = None
    voice_text, celine_route = await celine.process_voice_message(
        message, transcribe_fn=_whisper_transcribe,
        route_decider=lambda t: "shadow" if strip_shadow_prefix(t) is not None or _likely_command(t) else "luna",
        run_in_thread=asyncio.to_thread)
    if voice_text:
        effective = voice_text
        voice_from_clip = True
    else:
        clip_text, had_audio_clip, clip_err, clip_meta = await _discord_transcribe_voice_clip(message)
        if clip_text:
            effective = clip_text
            voice_text = clip_text
            voice_from_clip = True
            voice_emotion_meta = clip_meta
        elif had_audio_clip:
            await message.reply(f"<@{message.author.id}> {clip_err or 'I could not transcribe that voice clip.'}")
            await bot.process_commands(message)
            return

    # Conversational music: "Which song?" pick
    if message.guild:
        with _pending_play_lock:
            pending = _pending_play.get(message.guild.id)
        if pending and message.channel.id == pending.get("channel_id") and message.author.id == pending.get("author_id"):
            results = pending.get("results") or []
            idx = None
            low = effective.lower().strip()
            if low.isdigit() and 1 <= int(low) <= len(results): idx = int(low) - 1
            elif low in ("cancel","nevermind"):
                with _pending_play_lock: _pending_play.pop(message.guild.id, None)
                await message.reply("Cancelled.")
                return
            else:
                for i, r in enumerate(results):
                    if low in (r.get("title","")).lower(): idx = i; break
            if idx is not None:
                with _pending_play_lock: _pending_play.pop(message.guild.id, None)
                await _play_from_url(message, (results[idx].get("url") or ""))
                return

    # Conversational "play X" without !
    if message.guild and message.author.voice and message.author.voice.channel:
        if effective.lower().startswith("play ") and not effective.startswith("!"):
            query = effective[5:].strip()
            if query:
                ok, res = await asyncio.to_thread(_yt_search_multi, query, 5)
                if ok and res:
                    if len(res) == 1:
                        await _play_from_url(message, res[0]["url"])
                    else:
                        with _pending_play_lock:
                            _pending_play[message.guild.id] = {"results": res,
                                "channel_id": message.channel.id, "author_id": message.author.id}
                        lines = ["**Which song?** (reply with number or name)"] + \
                            [f"{i}. {r['title'][:80]}" for i, r in enumerate(res[:5], 1)]
                        await message.reply("\n".join(lines))
                return

    if not (
        isinstance(message.channel, discord.DMChannel)
        or (bot.user and bot.user.mentioned_in(message))
        or voice_from_clip
        or re.search(r"^!tts\b", (effective or "").strip(), re.I)
    ):
        await bot.process_commands(message)
        return

    text = effective
    if bot.user: text = text.replace(f"<@{bot.user.id}>","").strip()
    if not text:
        _greet = "Hey! I'm **Luna** — chat or say **Shadow, command**. **!help** for list."
        await message.reply(f"<@{message.author.id}> {_greet}")
        _schedule_discord_vc_tts_reply(message, _greet)
        _schedule_discord_file_tts(message, _greet)
        return

    global _last_user_activity, _last_streamer_luna_at
    _last_user_activity = time.time()
    if _linked_int and message.author.id == _linked_int:
        _last_streamer_luna_at = time.time()
    scope = _scope_for(message.author.id, message.guild.id if message.guild else None)
    mention = f"<@{message.author.id}>"
    tts_match = re.match(r"^!tts(?:\s+(.*))?$", (text or "").strip(), re.IGNORECASE | re.DOTALL)
    if tts_match:
        luna_line = (tts_match.group(1) or "").strip()
        if not luna_line:
            await message.reply(
                f"{mention} Usage: `!tts` **<what I should say>** — you write **my** line and I post it in **my** voice (MP3). "
                "This is for a specific line you want *me* to say, not for turning *your* message into audio. "
                "Web: **Luna says (TTS) → …** in Media (http://127.0.0.1:5050)."
            )
            return
        if not _can_use_tts_exclaim_command(message):
            await message.reply(
                f"{mention} Use `!tts` in DMs, in a channel listed in **DISCORD_TTS_CHANNEL_IDS**, or as the linked/admin user."
            )
            return
        ok = await _discord_post_tts_voice_files(message.channel, luna_line)
        await message.reply(
            f"{mention} {'Done — I posted that in my voice.' if ok else 'I could not generate audio just now.'}"
        )
        return
    if voice_from_clip:
        try:
            shown = textwrap.shorten(text, width=260, placeholder="...")
            emo_suffix = ""
            if isinstance(voice_emotion_meta, dict):
                emo = str(voice_emotion_meta.get("emotion") or "neutral")
                vol = str(voice_emotion_meta.get("volume") or "normal")
                conf = voice_emotion_meta.get("model_confidence")
                if conf is not None:
                    emo_suffix = f" | Emotion: **{emo}** ({vol}, conf {float(conf):.2f})"
                else:
                    emo_suffix = f" | Emotion: **{emo}** ({vol})"
            await message.reply(f"{mention} 📝 Voice clip transcribed: {shown}{emo_suffix}")
            scope_clip = _scope_for(message.author.id, message.guild.id if message.guild else None)
            await asyncio.to_thread(append_exchange, scope_clip, "[voice clip]", shown)
        except Exception:
            pass
        # Continue into normal Luna chat flow so voice clips get full responses.

    # Pending identity file
    if scope in _pending_file_update:
        key = _pending_file_update.pop(scope, None)
        path_map = {"SOUL": _SOUL_PATH, "TOOLS": _TOOLS_PATH, "OBJECTIVES": _OBJECTIVES_PATH}
        if key and key in path_map:
            try: _write_file(path_map[key], text); _invalidate_identity()
            except Exception: pass
            reply = f"Saved to **{key}.md**."
            await asyncio.to_thread(append_exchange, scope, text, reply)
            await message.reply(f"{mention} {reply}")
            _schedule_discord_vc_tts_reply(message, reply)
            _schedule_discord_file_tts(message, reply)
            return

    if _is_retry(text):
        reply = await asyncio.to_thread(_handle_retry)
        await asyncio.to_thread(append_exchange, scope, text, reply)
        await message.reply(f"{mention} {reply}")
        _schedule_discord_vc_tts_reply(message, reply)
        _schedule_discord_file_tts(message, reply)
        return

    if text.strip().lower() in ("!help","!commands","!files"):
        await asyncio.to_thread(append_exchange, scope, text, HELP_TEXT)
        await _discord_reply_split(message, HELP_TEXT, prefix=mention)
        _schedule_discord_vc_tts_reply(message, HELP_TEXT)
        _schedule_discord_file_tts(message, HELP_TEXT)
        return

    tluna = text.strip()
    low_tluna = tluna.lower()
    if low_tluna.startswith("!twitch_chat_redirect"):
        if not _is_privileged(message.author.id):
            await message.reply(f"{mention} Only the linked Discord admin can change Twitch redirect.")
            await bot.process_commands(message)
            return
        rest = tluna[len("!twitch_chat_redirect") :].strip()
        reply = await asyncio.to_thread(_twitch_exec_chat_redirect, rest, discord_uid=message.author.id)
        await asyncio.to_thread(append_exchange, scope, text, reply)
        await message.reply(f"{mention} {reply}")
        _schedule_discord_vc_tts_reply(message, reply)
        _schedule_discord_file_tts(message, reply)
        await bot.process_commands(message)
        return
    if low_tluna.startswith("!twitch_say"):
        if not _is_privileged(message.author.id):
            await message.reply(f"{mention} Only the linked Discord admin can send Twitch chat via Luna.")
            await bot.process_commands(message)
            return
        rest = tluna[len("!twitch_say") :].strip()
        reply = await asyncio.to_thread(_twitch_exec_say, rest, discord_uid=message.author.id)
        await asyncio.to_thread(append_exchange, scope, text, reply)
        await message.reply(f"{mention} {reply}")
        _schedule_discord_vc_tts_reply(message, reply)
        _schedule_discord_file_tts(message, reply)
        await bot.process_commands(message)
        return

    _cf = _chat_fast_enabled()

    # Shadow
    if celine_route == "shadow" or strip_shadow_prefix(text) is not None:
        rest = strip_shadow_prefix(text) or text
        reply = await asyncio.to_thread(shadow_run, rest, scope, _parse_command, _run_cmd,
            permission_fn=_is_privileged, author_id=message.author.id, log_fn=_log_action, user_message=text)
        reply = _normalize_cmd_reply(reply)
        if isinstance(reply, dict) and reply.get("need_feedback"):
            await _discord_reply_split(message, _format_discord_need_feedback(reply), prefix=mention)
            return
        await asyncio.to_thread(append_exchange, scope, text, reply)
        await message.reply(f"{mention} {reply}")
        _schedule_discord_vc_tts_reply(message, reply)
        _schedule_discord_file_tts(message, reply)
        return

    # NL commands. In fast mode we still allow a safe subset
    # so @Luna "search ..." does not silently fall back to plain chat.
    _fast_mode_nl_allow = {
        "search",
        "news",
        "news_topic",
        "summarize",
        "yt_analytics",
        "analytics_screen",
        "briefing",
        "todo",
        "calendar",
    }
    if _likely_command(text):
        parsed = await asyncio.to_thread(_parse_command, text)
        if parsed:
            cmd, params = parsed
            if _cf and cmd not in _fast_mode_nl_allow:
                parsed = None
            if not parsed:
                pass
            else:
                if cmd == "help":
                    await message.reply(f"{mention} {HELP_TEXT}")
                    _schedule_discord_vc_tts_reply(message, HELP_TEXT)
                    _schedule_discord_file_tts(message, HELP_TEXT)
                    return
                if _is_privileged(message.author.id) or cmd not in ("suno","suno_ready","create_code","msg","dm","call","share_x","share_facebook","yt_comment","yt_like","ig_dm","fb_msg","remind"):
                    reply = await asyncio.to_thread(_run_cmd, cmd, params, scope, text)
                    reply = _normalize_cmd_reply(reply)
                    if isinstance(reply, dict) and reply.get("need_feedback"):
                        await _discord_reply_split(message, _format_discord_need_feedback(reply), prefix=mention)
                        return
                    if reply:
                        await asyncio.to_thread(_log_action, cmd, params, reply)
                        await asyncio.to_thread(append_exchange, scope, text, reply)
                        await message.reply(f"{mention} {reply}")
                        _schedule_discord_vc_tts_reply(message, reply)
                        _schedule_discord_file_tts(message, reply)
                        return

    # Luna chat
    chat_text = text
    if voice_from_clip and isinstance(voice_emotion_meta, dict):
        emo = str(voice_emotion_meta.get("emotion") or "neutral")
        vol = str(voice_emotion_meta.get("volume") or "normal")
        rms = voice_emotion_meta.get("rms")
        var = voice_emotion_meta.get("variation")
        zcr = voice_emotion_meta.get("zcr")
        chat_text = (
            f"[Voice clip emotion context]\n"
            f"Detected emotion: {emo}\n"
            f"Volume: {vol}\n"
            f"RMS: {rms}\n"
            f"Energy variation: {var}\n"
            f"Tone roughness (ZCR): {zcr}\n\n"
            f"User transcript: {text}"
        )
    _df = _discord_chat_prompt_fast()
    _hist_n = _discord_chat_history_limit() if _df else 30
    history = await asyncio.to_thread(get_recent_conversation, scope, _hist_n)
    history = _discord_filter_error_history(history)
    history = _compact_history(history, fast=(_df or _cf), scope=scope)
    history = _discord_trim_chat_messages(history) if _df else history
    system = await asyncio.to_thread(
        _prepare_main_chat_system,
        scope,
        chat_text,
        fast=(_df or _cf),
        force_stream_mode=_stream_mode_env_on(),
    )
    disc_model = _discord_ollama_chat_model()
    try:
        reply = await asyncio.to_thread(
            lambda dm=disc_model: ollama_chat(
                chat_text,
                system=system,
                scope=scope,
                history=history,
                model=dm,
                compact=(_df or _cf),
            )
        )
        if _discord_ollama_reply_broken(reply):
            fb = _discord_chat_fallback_model(disc_model)
            if fb:
                reply = await asyncio.to_thread(
                    lambda m=fb: ollama_chat(
                        chat_text,
                        system=system,
                        scope=scope,
                        history=history,
                        model=m,
                        compact=True,
                    )
                )
        if not reply:
            reply = COMMAND_ONLY
        elif _discord_ollama_reply_broken(reply):
            reply = COMMAND_ONLY
        elif reply.startswith("Ollama offline"):
            low_reply = reply.lower()
            if "too many requests" in low_reply or "429" in low_reply:
                reply = "I'm getting rate-limited for a moment. Give me a few seconds and try again."
            else:
                reply = COMMAND_ONLY
    except Exception: reply = COMMAND_ONLY

    await asyncio.to_thread(append_exchange, scope, text, reply)

    def _discord_post_memory():
        try:
            _capture_memory(scope, text, reply)
            _capture_profile(scope, text)
        except Exception:
            pass
        try:
            _update_rolling_summary(scope)
        except Exception:
            pass
    threading.Thread(target=_discord_post_memory, daemon=True).start()

    await _discord_reply_split(message, reply, prefix=mention)
    if reply and reply != COMMAND_ONLY and not _discord_reply_is_error_tts(reply):
        _schedule_discord_vc_tts_reply(message, reply)
        _schedule_discord_file_tts(message, reply)
    await bot.process_commands(message)

async def _play_from_url(message: discord.Message, url: str):
    if not message.author.voice or not message.author.voice.channel:
        await message.reply("Join a voice channel first."); return
    ok, result = await asyncio.to_thread(_resolve_track, url)
    if not ok: await message.reply(f"❌ {result}"); return
    track = result; track["request_channel_id"] = message.channel.id
    target = message.author.voice.channel
    vc = message.guild.voice_client
    try:
        if vc and vc.is_connected():
            if vc.channel.id != target.id: await vc.move_to(target)
        else: vc = await target.connect()
    except Exception as e: await message.reply(f"Voice error: {e}"); return
    state = _music_state(message.guild.id)
    state["queue"].append(track)
    started = _start_track(message.guild.id, vc)
    await message.reply(f"{'▶️ Playing' if started else '➕ Queued'}: **{track['title']}**")


async def _play_in_discord_for_linked_user_async(query: str) -> tuple[bool, str]:
    """Play a track in Discord using the linked user's voice channel (for UI / web)."""
    if not query or not (query := query.strip()):
        return False, "Usage: !play <song or URL>"
    if not _linked_int:
        return False, "Set LINKED_DISCORD_USER_ID in .env and join a Discord voice channel to use Play from the UI."
    guild = None
    target_channel = None
    for g in bot.guilds:
        member = g.get_member(_linked_int)
        if member and member.voice and member.voice.channel:
            guild = g
            target_channel = member.voice.channel
            break
    if not guild or not target_channel:
        return False, "Join a Discord voice channel first, then try **Play** from the UI again."
    vc = guild.voice_client
    try:
        if vc and vc.is_connected():
            if vc.channel.id != target_channel.id:
                await vc.move_to(target_channel)
        else:
            vc = await target_channel.connect()
    except Exception as e:
        return False, f"Voice error: {e}"
    ok, result = await asyncio.to_thread(_resolve_track, query)
    if not ok:
        return False, str(result)
    track = result
    track["request_channel_id"] = 0
    state = _music_state(guild.id)
    state["queue"].append(track)
    started = _start_track(guild.id, vc)
    title = track.get("title", "?")
    return True, f"{'▶️ Playing' if started else '➕ Queued'}: **{title}**"


async def _play_podcast_in_discord_async(choice: str | None = None) -> tuple[bool, str]:
    """Queue and play one (or all) podcast episodes in the linked user's voice channel."""
    if not _linked_int:
        return False, "Set LINKED_DISCORD_USER_ID in .env and join a Discord voice channel to use the custom podcast."
    ok, result = await asyncio.to_thread(_get_podcast_tracks)
    if not ok:
        return False, str(result)
    all_tracks = result
    ok, picked = await asyncio.to_thread(_pick_podcast_tracks, all_tracks, choice)
    if not ok:
        return False, picked  # picked is an error string here
    tracks = picked
    guild = None
    target_channel = None
    for g in bot.guilds:
        member = g.get_member(_linked_int)
        if member and member.voice and member.voice.channel:
            guild = g
            target_channel = member.voice.channel
            break
    if not guild or not target_channel:
        return False, "Join a Discord voice channel first, then try **Custom podcast** again."
    vc = guild.voice_client
    try:
        if vc and vc.is_connected():
            if vc.channel.id != target_channel.id:
                await vc.move_to(target_channel)
        else:
            vc = await target_channel.connect()
    except Exception as e:
        return False, f"Voice error: {e}"
    state = _music_state(guild.id)
    for t in tracks:
        t["request_channel_id"] = 0
        state["queue"].append(t)
    started = _start_track(guild.id, vc)
    n = len(tracks)
    first = tracks[0].get("title", "?") if tracks else "?"
    return True, f"🎙️ Custom podcast: **{n}** episode(s) — {'▶️ Playing' if started else '➕ Queued'}: **{first}**"


async def _skip_in_discord_for_linked_user_async() -> tuple[bool, str]:
    """Skip current track in the linked user's Discord voice channel (for UI)."""
    if not _linked_int:
        return False, "Set LINKED_DISCORD_USER_ID in .env to use Skip from the UI."
    guild = None
    for g in bot.guilds:
        member = g.get_member(_linked_int)
        if member and member.voice and member.voice.channel:
            guild = g
            break
    if not guild:
        return False, "Join a Discord voice channel first, then try **Skip** from the UI."
    vc = guild.voice_client
    if not vc or not vc.is_connected():
        return False, "I'm not in a voice channel. Use **Play** first."
    if vc.is_playing() or vc.is_paused():
        _music_state(guild.id)["manual_skip"] = True
        vc.stop()
        return True, "⏭️ Skipped."
    return False, "Nothing playing."


async def _stop_in_discord_for_linked_user_async() -> tuple[bool, str]:
    """Stop playback and clear queue in the linked user's Discord voice channel (for UI)."""
    if not _linked_int:
        return False, "Set LINKED_DISCORD_USER_ID in .env to use Stop from the UI."
    guild = None
    for g in bot.guilds:
        member = g.get_member(_linked_int)
        if member and member.voice and member.voice.channel:
            guild = g
            break
    if not guild:
        return False, "Join a Discord voice channel first, then try **Stop** from the UI."
    state = _music_state(guild.id)
    state["manual_skip"] = True
    state["queue"].clear()
    state["current"] = None
    vc = guild.voice_client
    if vc and (vc.is_playing() or vc.is_paused()):
        vc.stop()
    return True, "⏹️ Stopped."

def _local_whisper_transcribe_chunked(
    model,
    path: str,
    kwargs: dict,
    *,
    chunk_sec: float,
    overlap_sec: float,
    label: str,
) -> str:
    """Run local Whisper in short overlapping chunks for better long-clip robustness."""
    if not _WHISPER_CHUNKING_ON:
        return (model.transcribe(path, **kwargs).get("text") or "").strip()
    fd = wav_fd = None
    chunk_paths: list[str] = []
    t0 = time.time()
    try:
        fd, wav_fd = tempfile.mkstemp(suffix=".wav")
        os.close(fd)
        fd = None
        if not _ffmpeg_to_wav16_mono_voice(path, wav_fd):
            return (model.transcribe(path, **kwargs).get("text") or "").strip()
        with wave.open(wav_fd, "rb") as wr:
            rate = int(wr.getframerate() or 16000)
            sampwidth = int(wr.getsampwidth() or 2)
            channels = int(wr.getnchannels() or 1)
            frames = int(wr.getnframes() or 0)
            if frames <= 0:
                return ""
            pcm = wr.readframes(frames)
        duration = frames / float(max(1, rate))
        step_sec = max(0.8, float(chunk_sec) - float(overlap_sec))
        if duration <= max(2.0, chunk_sec + 0.25):
            return (model.transcribe(wav_fd, **kwargs).get("text") or "").strip()
        frame_bytes = max(1, sampwidth * channels)
        chunk_frames = max(1, int(chunk_sec * rate))
        step_frames = max(1, int(step_sec * rate))
        parts: list[str] = []
        start = 0
        guard = 0
        while start < frames and guard < 240:
            end = min(frames, start + chunk_frames)
            left = start * frame_bytes
            right = end * frame_bytes
            seg = pcm[left:right]
            if len(seg) < rate * frame_bytes * 0.35:
                break
            fd_c, p = tempfile.mkstemp(suffix=".wav")
            os.close(fd_c)
            with wave.open(p, "wb") as ww:
                ww.setnchannels(channels)
                ww.setsampwidth(sampwidth)
                ww.setframerate(rate)
                ww.writeframes(seg)
            chunk_paths.append(p)
            txt = (model.transcribe(p, **kwargs).get("text") or "").strip()
            if txt:
                parts.append(txt)
            start += step_frames
            guard += 1
        merged = " ".join(parts).strip()
        merged = re.sub(r"\s+", " ", merged)
        if LUNA_CALL_DEBUG:
            print(
                f"[CallDebug {datetime.now().strftime('%H:%M:%S')}] {label}_chunked_done | "
                f"ms={int((time.time()-t0)*1000)}, dur_sec={duration:.1f}, chunks={len(chunk_paths)}, chars={len(merged)}",
                flush=True,
            )
        return merged
    except Exception:
        return (model.transcribe(path, **kwargs).get("text") or "").strip()
    finally:
        for p in chunk_paths:
            try:
                if p and os.path.isfile(p):
                    os.unlink(p)
            except Exception:
                pass
        try:
            if wav_fd and os.path.isfile(wav_fd):
                os.unlink(wav_fd)
        except Exception:
            pass
        try:
            if fd is not None:
                os.close(fd)
        except Exception:
            pass


def _local_whisper_transcribe(path: str) -> str | None:
    """Pre-Groq local STT: model cached on _whisper_transcribe. Used when Groq fails or limit hit."""
    t0 = time.time()
    try:
        import whisper
        model = getattr(_whisper_transcribe, "_model", None)
        if model is None:
            model = whisper.load_model("base")
            _whisper_transcribe._model = model
        out = _local_whisper_transcribe_chunked(
            model,
            path,
            _whisper_transcribe_kwargs(),
            chunk_sec=WHISPER_CHUNK_SEC,
            overlap_sec=WHISPER_CHUNK_OVERLAP_SEC,
            label="whisper",
        )
        if LUNA_CALL_DEBUG:
            print(
                f"[CallDebug {datetime.now().strftime('%H:%M:%S')}] whisper_done | ms={int((time.time()-t0)*1000)}, chars={len(out)}",
                flush=True,
            )
        return out
    except Exception as e:
        if LUNA_CALL_DEBUG:
            print(
                f"[CallDebug {datetime.now().strftime('%H:%M:%S')}] whisper_error | ms={int((time.time()-t0)*1000)}, err={str(e)[:220]}",
                flush=True,
            )
        return None


def _local_whisper_transcribe_relaxed(path: str) -> str | None:
    """Pre-Groq relaxed STT: same shared _whisper_transcribe._model, looser VAD/silence thresholds."""
    t0 = time.time()
    try:
        import whisper
        model = getattr(_whisper_transcribe, "_model", None)
        if model is None:
            model = whisper.load_model("base")
            _whisper_transcribe._model = model
        kw = dict(_whisper_transcribe_kwargs())
        kw["no_speech_threshold"] = 0.82
        kw["logprob_threshold"] = -2.0
        kw["compression_ratio_threshold"] = 2.8
        out = _local_whisper_transcribe_chunked(
            model,
            path,
            kw,
            chunk_sec=WHISPER_CHUNK_SEC,
            overlap_sec=WHISPER_CHUNK_OVERLAP_SEC,
            label="whisper_relaxed",
        )
        if LUNA_CALL_DEBUG:
            print(
                f"[CallDebug {datetime.now().strftime('%H:%M:%S')}] whisper_relaxed_done | ms={int((time.time()-t0)*1000)}, chars={len(out)}",
                flush=True,
            )
        return out
    except Exception as e:
        if LUNA_CALL_DEBUG:
            print(
                f"[CallDebug {datetime.now().strftime('%H:%M:%S')}] whisper_relaxed_error | ms={int((time.time()-t0)*1000)}, err={str(e)[:220]}",
                flush=True,
            )
        return None


def _api_transcribe_local_whisper(path: str) -> str | None:
    """Pre-Groq /api/transcribe path: its own model cache on api_transcribe (separate from _whisper_transcribe)."""
    t0 = time.time()
    try:
        import whisper
        model = getattr(api_transcribe, "_model", None)
        if model is None:
            model = whisper.load_model("base")
            api_transcribe._model = model
        out = _local_whisper_transcribe_chunked(
            model,
            path,
            _whisper_transcribe_kwargs(),
            chunk_sec=WHISPER_CHUNK_SEC,
            overlap_sec=WHISPER_CHUNK_OVERLAP_SEC,
            label="api_transcribe_whisper",
        )
        if LUNA_CALL_DEBUG:
            print(
                f"[CallDebug {datetime.now().strftime('%H:%M:%S')}] api_transcribe_whisper_done | ms={int((time.time()-t0)*1000)}, chars={len(out)}",
                flush=True,
            )
        return out
    except Exception as e:
        if LUNA_CALL_DEBUG:
            print(
                f"[CallDebug {datetime.now().strftime('%H:%M:%S')}] api_transcribe_whisper_error | ms={int((time.time()-t0)*1000)}, err={str(e)[:220]}",
                flush=True,
            )
        return None


def _whisper_transcribe(path: str) -> str | None:
    t0 = time.time()
    ok, groq_text = _groq_stt_try(path)
    if ok:
        if LUNA_CALL_DEBUG:
            print(
                f"[CallDebug {datetime.now().strftime('%H:%M:%S')}] groq_stt_done | ms={int((time.time()-t0)*1000)}, chars={len(groq_text or '')}",
                flush=True,
            )
        return groq_text
    if not GROQ_STT_LOCAL_FALLBACK:
        if LUNA_CALL_DEBUG:
            print(
                f"[CallDebug {datetime.now().strftime('%H:%M:%S')}] stt_groq_only | GROQ_STT_LOCAL_FALLBACK=0",
                flush=True,
            )
        return None
    if LUNA_CALL_DEBUG:
        print(
            f"[CallDebug {datetime.now().strftime('%H:%M:%S')}] stt_fallback_local | reason=groq_skip_or_error",
            flush=True,
        )
    return _local_whisper_transcribe(path)


def _whisper_transcribe_relaxed(path: str) -> str | None:
    """Fallback STT for noisy/Discord: Groq first, then local relaxed Whisper."""
    t0 = time.time()
    ok, groq_text = _groq_stt_try(path)
    if ok:
        if LUNA_CALL_DEBUG:
            print(
                f"[CallDebug {datetime.now().strftime('%H:%M:%S')}] groq_stt_relaxed_done | ms={int((time.time()-t0)*1000)}, chars={len(groq_text or '')}",
                flush=True,
            )
        return groq_text
    if not GROQ_STT_LOCAL_FALLBACK:
        return None
    if LUNA_CALL_DEBUG:
        print(
            f"[CallDebug {datetime.now().strftime('%H:%M:%S')}] stt_fallback_local_relaxed | reason=groq_skip_or_error",
            flush=True,
        )
    return _local_whisper_transcribe_relaxed(path)


def _whisper_translate(path: str) -> str | None:
    """Transcribe audio and translate to English (any source language)."""
    try:
        import whisper
        model = getattr(_whisper_translate, "_model", None)
        if model is None:
            model = whisper.load_model("base")
            _whisper_translate._model = model
        result = model.transcribe(path, task="translate", **_whisper_transcribe_kwargs())
        return (result.get("text") or "").strip()
    except Exception:
        return None

# ── Discord ! commands ────────────────────────────────────────────────────────

def _discord_scope(ctx): return _scope_for(ctx.author.id, ctx.guild.id if ctx.guild else None)

async def _discord_reply_split(reply_target, text: str, prefix: str = "", limit: int = 1900):
    """Reply in multiple messages when content is too long for Discord."""
    full = (text or "").strip()
    if not full:
        await reply_target.reply((prefix or "").strip() or " ")
        return
    if len((prefix + " " + full).strip()) <= limit:
        await reply_target.reply((f"{prefix} {full}" if prefix else full).strip())
        return
    lines = full.splitlines() or [full]
    chunks: list[str] = []
    cur = ""
    for ln in lines:
        cand = (cur + ("\n" if cur else "") + ln).strip()
        if len(cand) <= limit:
            cur = cand
            continue
        if cur:
            chunks.append(cur)
        if len(ln) <= limit:
            cur = ln
        else:
            for i in range(0, len(ln), limit):
                part = ln[i:i + limit]
                if part:
                    chunks.append(part)
            cur = ""
    if cur:
        chunks.append(cur)
    for i, ch in enumerate(chunks):
        msg = (f"{prefix} {ch}" if prefix and i == 0 else ch).strip()
        await reply_target.reply(msg[:limit])

@bot.command(name="help", aliases=["files", "commands"])
async def cmd_help(ctx):
    await _discord_reply_split(ctx, HELP_TEXT)

@bot.command(name="news")
async def cmd_news(ctx):
    ok, r = await asyncio.to_thread(_fetch_news)
    await ctx.reply(r if ok else f"❌ {r}")

@bot.command(name="scrape")
async def cmd_scrape(ctx, *, args: str = ""):
    if not _is_privileged(ctx.author.id): await ctx.reply("Linked user/admin only."); return
    if not args: await ctx.reply("Usage: !scrape <url> <what to extract> [post:#channel-name]"); return
    tokens = args.split()
    scrape_url = tokens[0]
    post_ch = ""
    rest_tokens = tokens[1:]
    for i, t in enumerate(rest_tokens):
        if t.lower().startswith("post:#") or t.lower().startswith("post:"):
            post_ch = t.split(":", 1)[1].lstrip("#")
            rest_tokens = rest_tokens[:i] + rest_tokens[i+1:]
            break
    instruction = " ".join(rest_tokens).strip()
    await ctx.reply("Scraping... this may take a moment.")
    ok, r = await asyncio.to_thread(_scrape_website, scrape_url, instruction, post_ch)
    chunks = [r[i:i+1990] for i in range(0, len(r), 1990)]
    for chunk in chunks:
        await ctx.reply(f"{'✅' if ok else '❌'} {chunk}" if chunk == chunks[0] else chunk)

@bot.command(name="suno_ready", aliases=["suno_logged_in"])
async def cmd_suno_ready(ctx):
    if not _is_privileged(ctx.author.id): await ctx.reply("Linked user/admin only."); return
    msg = _mark_suno_logged_in()
    await ctx.reply(f"✅ {msg}")

@bot.command(name="suno")
async def cmd_suno(ctx, *, description: str = ""):
    if not _is_privileged(ctx.author.id): await ctx.reply("Linked user/admin only."); return
    if not description: await ctx.reply("Usage: !suno <description>"); return
    await ctx.reply("Opening Suno...")
    ok, r = await asyncio.to_thread(_run_suno, description)
    await ctx.reply(f"{'✅' if ok else '❌'} {r}")

@bot.command(name="share_song")
async def cmd_share_song(ctx):
    if not _is_privileged(ctx.author.id): await ctx.reply("Linked user/admin only."); return
    ok, r = await asyncio.to_thread(_run_x_share)
    if not ok: _record_failure("share_x", r, {})
    await ctx.reply(f"{'✅' if ok else '❌'} {r}")

@bot.command(name="share_facebook")
async def cmd_share_fb(ctx):
    if not _is_privileged(ctx.author.id): await ctx.reply("Linked user/admin only."); return
    ok, r = await asyncio.to_thread(_run_fb_share)
    if not ok: _record_failure("share_facebook", r, {})
    await ctx.reply(f"{'✅' if ok else '❌'} {r}")

@bot.command(name="yt_comment")
async def cmd_yt(ctx, *, video_url: str = ""):
    if not _is_privileged(ctx.author.id): await ctx.reply("Linked user/admin only."); return
    if not video_url: await ctx.reply("Usage: !yt_comment <url>"); return
    url = _yt_url_from_freeform_arg(video_url.strip()) or video_url.strip()
    if not _yt_extract_id(url):
        await ctx.reply("Could not parse a YouTube URL. Paste the full watch, Shorts, or youtu.be link."); return
    await ctx.reply("Posting comment...")
    ok, r = await asyncio.to_thread(_yt_comment, url)
    if not ok: _record_failure("yt_comment", r, {"video_url": video_url})
    await ctx.reply(f"{'✅' if ok else '❌'} {r}")

@bot.command(name="yt_like", aliases=["youtube_like"])
async def cmd_yt_like(ctx, *, video_url: str = ""):
    if not _is_privileged(ctx.author.id): await ctx.reply("Linked user/admin only."); return
    if not video_url: await ctx.reply("Usage: !yt_like <url>"); return
    await ctx.reply("Liking video…")
    ok, r = await asyncio.to_thread(_yt_like_one, video_url.strip())
    if not ok: _record_failure("yt_like", r, {"video_url": video_url})
    await ctx.reply(f"{'✅' if ok else '❌'} {r}")

@bot.command(name="yt_analytics", aliases=["youtube_analytics"])
async def cmd_yt_analytics(ctx, days: int = 30, limit: int = 5):
    if not _is_privileged(ctx.author.id): await ctx.reply("Linked user/admin only."); return
    ok, r = await asyncio.to_thread(_yt_channel_analytics, days, limit)
    if not ok: _record_failure("yt_analytics", r, {"days": days, "limit": limit})
    await ctx.reply(f"{'✅' if ok else '❌'} {r}")

@bot.command(name="yt_react", aliases=["youtube_react"])
async def cmd_yt_react(ctx, *, video_url: str = ""):
    if not _is_privileged(ctx.author.id): await ctx.reply("Linked user/admin only."); return
    if not video_url: await ctx.reply("Usage: !yt_react <url>"); return
    await ctx.reply("Watching and reacting…")
    ok, r = await asyncio.to_thread(_yt_react, video_url.strip())
    if not ok: _record_failure("yt_react", r, {"video_url": video_url})
    await _discord_reply_split(ctx, f"{'✅' if ok else '❌'} {r}")

@bot.command(name="yt_watch_react", aliases=["youtube_watch_react"])
async def cmd_yt_watch_react(ctx, *, video_url: str = ""):
    if not _is_privileged(ctx.author.id): await ctx.reply("Linked user/admin only."); return
    if not video_url: await ctx.reply("Usage: !yt_watch_react <url>"); return
    scope = _scope_for(ctx.author.id, ctx.guild.id if ctx.guild else None)
    ok, r = await asyncio.to_thread(_yt_watch_react_start, video_url.strip(), scope)
    if not ok:
        _record_failure("yt_watch_react", r, {"video_url": video_url})
    await _discord_reply_split(ctx, f"{'✅' if ok else '❌'} {r}")

@bot.command(name="yt_watch_stop", aliases=["youtube_watch_stop"])
async def cmd_yt_watch_stop(ctx):
    if not _is_privileged(ctx.author.id): await ctx.reply("Linked user/admin only."); return
    scope = _scope_for(ctx.author.id, ctx.guild.id if ctx.guild else None)
    ok, r = await asyncio.to_thread(_yt_watch_stop, scope)
    await _discord_reply_split(ctx, f"{'✅' if ok else '❌'} {r}")

@bot.command(name="x_react", aliases=["twitter_react"])
async def cmd_x_react(ctx, *, post_url: str = ""):
    if not _is_privileged(ctx.author.id): await ctx.reply("Linked user/admin only."); return
    if not post_url: await ctx.reply("Usage: !x_react <x-post-url>"); return
    await ctx.reply("Reading post and reacting…")
    ok, r = await asyncio.to_thread(_x_react, post_url.strip())
    if not ok: _record_failure("x_react", r, {"post_url": post_url})
    await _discord_reply_split(ctx, f"{'✅' if ok else '❌'} {r}")

@bot.command(name="analytics_screen", aliases=["dashboard_read", "read_dashboard"])
async def cmd_analytics_screen(ctx):
    if not _is_privileged(ctx.author.id): await ctx.reply("Linked user/admin only."); return
    ok, r = await asyncio.to_thread(_describe_dashboard_screenshot)
    if not ok:
        _record_failure("analytics_screen", r, {})
    full = (f"✅ {r}" if ok else f"❌ {r}")
    chunks = [full[i : i + 1990] for i in range(0, len(full), 1990)]
    for chunk in chunks:
        await ctx.reply(chunk)

@bot.command(name="status", aliases=["set_status"])
async def cmd_status(ctx, *, args: str = ""):
    if not _is_privileged(ctx.author.id): await ctx.reply("Linked user/admin only."); return
    if not args.strip():
        await ctx.reply("Usage: !status <text> | !status clear | !status <listening|playing|watching|competing> <text>")
        return
    al = args.strip()
    if al.lower() in ("clear", "off", "none"):
        ok, msg = await _set_discord_status("", "listening")
        await ctx.reply(f"{'✅' if ok else '❌'} {msg}")
        return
    parts = al.split(None, 1)
    kind = "listening"
    text = al
    if len(parts) == 2 and parts[0].lower() in ("listening", "playing", "watching", "competing"):
        kind = parts[0].lower()
        text = parts[1].strip()
    ok, msg = await _set_discord_status(text, kind)
    await ctx.reply(f"{'✅' if ok else '❌'} {msg}")

@bot.command(name="ig_dm")
async def cmd_ig(ctx, *, args: str = ""):
    if not _is_privileged(ctx.author.id): await ctx.reply("Linked user/admin only."); return
    if not args: await ctx.reply("Usage: !ig_dm <username> [message]"); return
    ps = args.split(None, 1)
    ok, r = await asyncio.to_thread(_run_ig_dm, ps[0], ps[1] if len(ps)>1 else "")
    if not ok: _record_failure("ig_dm", r, {"target": ps[0]})
    await ctx.reply(f"{'✅' if ok else '❌'} {r}")

@bot.command(name="fb_msg", aliases=["messenger","fbmsg"])
async def cmd_msg_fb(ctx, *, args: str = ""):
    if not _is_privileged(ctx.author.id): await ctx.reply("Linked user/admin only."); return
    if not args: await ctx.reply("Usage: !fb_msg <name> [: message] or !fb_msg <name> say <message>"); return
    split_m = re.split(r'\s*:\s+|\s+(?:say|msg|message)\s+', args, maxsplit=1, flags=re.I)
    if len(split_m) == 2:
        target, msg = split_m[0].strip(), split_m[1].strip()
    else:
        target, msg = args.strip(), ""
    ok, r = await asyncio.to_thread(_run_messenger_msg, target, msg)
    if not ok: _record_failure("fb_msg", r, {"target": target})
    await ctx.reply(f"{'✅' if ok else '❌'} {r}")

@bot.command(name="joinme", aliases=["join_vc", "luna_join"])
async def cmd_joinme(ctx, *, message: str = ""):
    """Join the linked user's voice channel and inform them with TTS."""
    if not _is_privileged(ctx.author.id): await ctx.reply("Linked user/admin only."); return
    text = (message or "Hey, Luna here! I'm in your voice channel.").strip()
    ok = await _join_linked_user_vc_and_speak(text, disconnect_after=False)
    if ok:
        await ctx.reply(f"✅ Joined your voice channel and said it.")
    else:
        await ctx.reply("❌ Join a voice channel first, then try **!joinme**.")

@bot.command(name="call")
async def cmd_call(ctx, *, contact: str = ""):
    if not _is_privileged(ctx.author.id): await ctx.reply("Linked user/admin only."); return
    if not contact: await ctx.reply("Usage: !call <Discord username or user ID>"); return
    # From Discord: join target's VC with voice receive, transcribe + TTS
    ok, r = await _run_discord_call_bot(ctx, contact)
    if not ok:
        _record_failure("call", r, {"contact": contact})
        await ctx.reply(f"❌ {r}")
    elif r:
        await ctx.reply(r)

@bot.command(name="listen", aliases=["record_listen", "vc_listen"])
async def cmd_listen(ctx):
    await ctx.reply("Use a Discord voice clip attachment instead. `!listen` is disabled.")

@bot.command(name="dm")
async def cmd_dm(ctx, *, args: str = ""):
    if not _is_privileged(ctx.author.id): await ctx.reply("Linked user/admin only."); return
    if not args: await ctx.reply("Usage: !dm <Discord username or user ID> [message]"); return
    parts = args.split(None, 1)
    target = parts[0].strip().lstrip("@")
    msg = (parts[1].strip() if len(parts) > 1 else "") or ""
    if not target: await ctx.reply("Usage: !dm <username or user ID> [message]"); return
    ok, r = await asyncio.to_thread(_run_discord_dm, target, msg)
    if not ok: _record_failure("dm", r, {"target": target})
    await ctx.reply(f"{'✅' if ok else '❌'} {r}")

@bot.command(name="msg")
async def cmd_msg(ctx, *, args: str = ""):
    if not _is_privileged(ctx.author.id): await ctx.reply("Linked user/admin only."); return
    if not args: await ctx.reply("Usage: !msg <contact> [description]"); return
    contact, desc = _parse_wa_contact_and_msg(args)
    if not contact: await ctx.reply("Usage: !msg <contact> [description]"); return
    ok, r = await asyncio.to_thread(_run_wa_msg, contact, desc)
    if not ok: _record_failure("msg", r, {"contact": contact})
    await ctx.reply(r if ok else f"❌ {r}")

@bot.command(name="remember")
async def cmd_remember(ctx, *, fact: str = ""):
    if not fact: await ctx.reply("Usage: !remember <fact>"); return
    await asyncio.to_thread(add_memory, _discord_scope(ctx), fact)
    await ctx.reply("Got it, I'll remember that.")

@bot.command(name="always_remember")
async def cmd_always_remember(ctx, *, fact: str = ""):
    if not fact: await ctx.reply("Usage: !always_remember <fact>"); return
    await asyncio.to_thread(add_core_memory, _discord_scope(ctx), fact)
    await ctx.reply("Got it, always keeping that in mind.")

@bot.command(name="memories")
async def cmd_memories(ctx):
    scope = _discord_scope(ctx)
    core = get_core_memories(scope); lt = get_long_term_memories(scope, 10); st = get_short_term_memories(scope)
    if not core and not lt and not st:
        await ctx.reply("No memories yet. Tell me things or use !remember."); return
    parts = []
    if core: parts.append("**Core:**\n" + "\n".join(f"• {m}" for m in core))
    if lt:   parts.append("**Long-term:**\n" + "\n".join(f"• {m}" for m in lt))
    if st:   parts.append("**Short-term:**\n" + "\n".join(f"• {m}" for m in st[:5]))
    out = "\n\n".join(parts)
    await ctx.reply(out[:1900] + ("..." if len(out)>1900 else ""))

@bot.command(name="forget")
async def cmd_forget(ctx):
    n = await asyncio.to_thread(clear_memories, _discord_scope(ctx))
    await ctx.reply(f"Cleared {n} memory(ies). Use !forget_all to clear everything.")

@bot.command(name="forget_all")
async def cmd_forget_all(ctx):
    nc, nl = await asyncio.to_thread(clear_all_memories, _discord_scope(ctx))
    await ctx.reply(f"Cleared {nc} core + {nl} long-term memory(ies).")

@bot.command(name="profile")
async def cmd_profile(ctx, *, args: str = ""):
    scope = _discord_scope(ctx)
    if args.lower().startswith("set "):
        rest = args[4:].strip()
        if " " not in rest: await ctx.reply("Usage: !profile set <field> <value>"); return
        f, v = rest.split(None, 1)
        if f.lower() not in PROFILE_FIELDS: await ctx.reply(f"Fields: {', '.join(PROFILE_FIELDS)}"); return
        await asyncio.to_thread(set_profile_field, scope, f.lower(), v)
        await ctx.reply(f"Updated **{f}**."); return
    if args.lower() == "clear":
        n = await asyncio.to_thread(clear_profile, scope)
        await ctx.reply(f"Cleared profile ({n} field(s))."); return
    profile = await asyncio.to_thread(get_profile, scope)
    _pf_order = {k: i for i, k in enumerate(PROFILE_FIELDS)}
    filled = sorted([(k, v) for k, v in profile.items() if v], key=lambda x: _pf_order.get(x[0], 99))
    if not filled: await ctx.reply("Profile empty. Tell me your name etc., or use !profile set."); return
    out = "**Profile:**\n" + "\n".join(f"• **{k}**: {v}" for k,v in filled)
    await ctx.reply(out[:1900])

@bot.command(name="play")
async def cmd_play(ctx, *, query: str = ""):
    if not ctx.guild: await ctx.reply("Voice only available in servers."); return
    if not query: await ctx.reply("Usage: !play <url or search>"); return
    if not ctx.author.voice or not ctx.author.voice.channel:
        await ctx.reply("Join a voice channel first."); return
    vc = ctx.voice_client
    target = ctx.author.voice.channel
    try:
        if vc and vc.is_connected():
            if vc.channel.id != target.id: await vc.move_to(target)
        else: vc = await target.connect()
    except Exception as e: await ctx.reply(f"Voice error: {e}"); return
    await ctx.reply(f"Resolving: `{query[:60]}`...")
    ok, result = await asyncio.to_thread(_resolve_track, query)
    if not ok: await ctx.reply(f"❌ {result}"); return
    track = result; track["request_channel_id"] = ctx.channel.id
    state = _music_state(ctx.guild.id)
    state["queue"].append(track)
    started = _start_track(ctx.guild.id, vc)
    await ctx.reply(f"{'▶️ Playing' if started else '➕ Queued'}: **{track['title']}**")

@bot.command(name="podcast")
async def cmd_podcast(ctx, *, args: str = ""):
    """Play custom podcast, or create one: !podcast create <topic>."""
    if not ctx.guild: await ctx.reply("Voice only in servers."); return
    args = (args or "").strip()
    if args.lower().startswith("create "):
        topic = args[7:].strip()
        ok, msg = await asyncio.to_thread(_create_podcast_from_description, topic)
        await ctx.reply(f"{'✅' if ok else '❌'} {msg}")
        return
    # No args → just show the available episodes as a menu.
    if not args:
        ok, result = await asyncio.to_thread(_get_podcast_tracks)
        if not ok:
            await ctx.reply(f"❌ {result}")
            return
        await ctx.reply(_format_podcast_menu(result))
        return
    # With args → treat as choice (index or name substring) and play that one.
    ok, all_tracks = await asyncio.to_thread(_get_podcast_tracks)
    if not ok:
        await ctx.reply(f"❌ {all_tracks}")
        return
    ok, picked = await asyncio.to_thread(_pick_podcast_tracks, all_tracks, args)
    if not ok:
        await ctx.reply(f"❌ {picked}")
        return
    tracks = picked
    if not ctx.author.voice or not ctx.author.voice.channel:
        await ctx.reply("Join a voice channel first."); return
    target = ctx.author.voice.channel
    vc = ctx.voice_client
    try:
        if vc and vc.is_connected():
            if vc.channel.id != target.id: await vc.move_to(target)
        else: vc = await target.connect()
    except Exception as e: await ctx.reply(f"Voice error: {e}"); return
    state = _music_state(ctx.guild.id)
    for t in tracks:
        t["request_channel_id"] = ctx.channel.id
        state["queue"].append(t)
    started = _start_track(ctx.guild.id, vc)
    n = len(tracks)
    first = tracks[0].get("title", "?") if tracks else "?"
    await ctx.reply(f"🎙️ Custom podcast: **{n}** episode(s) — {'▶️ Playing' if started else '➕ Queued'}: **{first}**")

@bot.command(name="pause")
async def cmd_pause(ctx):
    vc = ctx.voice_client
    if vc and vc.is_playing(): vc.pause(); await ctx.reply("⏸️")
    else: await ctx.reply("Nothing playing.")

@bot.command(name="resume")
async def cmd_resume(ctx):
    vc = ctx.voice_client
    if vc and vc.is_paused(): vc.resume(); await ctx.reply("▶️")
    else: await ctx.reply("Not paused.")

@bot.command(name="skip")
async def cmd_skip(ctx):
    vc = ctx.voice_client
    if vc and (vc.is_playing() or vc.is_paused()):
        if ctx.guild: _music_state(ctx.guild.id)["manual_skip"] = True
        vc.stop(); await ctx.reply("⏭️")
    else: await ctx.reply("Nothing playing.")

@bot.command(name="stop")
async def cmd_stop(ctx):
    if not ctx.guild: return
    state = _music_state(ctx.guild.id); state["manual_skip"] = True; state["queue"].clear(); state["current"] = None
    vc = ctx.voice_client
    if vc and (vc.is_playing() or vc.is_paused()): vc.stop()
    await ctx.reply("⏹️ Stopped.")

@bot.command(name="queue")
async def cmd_queue(ctx):
    if not ctx.guild: return
    state = _music_state(ctx.guild.id)
    cur, q = state.get("current"), list(state.get("queue") or [])
    if not cur and not q: await ctx.reply("Queue empty. Use !play."); return
    lines = []
    if cur: lines.append(f"🎵 **Now:** {cur.get('title','?')}")
    if q:
        lines.append("**Up next:**")
        lines += [f"{i}. {t.get('title','?')}" for i, t in enumerate(q[:10], 1)]
        if len(q) > 10: lines.append(f"...and {len(q)-10} more")
    await ctx.reply("\n".join(lines)[:1900])

@bot.command(name="join")
async def cmd_join(ctx):
    if not ctx.guild: return
    if ctx.voice_client and ctx.voice_client.is_connected():
        await ctx.reply(f"Already in **{ctx.voice_client.channel.name}**."); return
    ch = (ctx.author.voice.channel if ctx.author.voice else None) or \
         next((c for c in ctx.guild.voice_channels if any(not m.bot for m in c.members)), None) or \
         (ctx.guild.voice_channels[0] if ctx.guild.voice_channels else None)
    if not ch: await ctx.reply("No voice channel found."); return
    try: await ch.connect(); await ctx.reply(f"Joined **{ch.name}**.")
    except Exception as e: await ctx.reply(f"Error: {e}")

@bot.command(name="leave")
async def cmd_leave(ctx):
    global _call_session
    if not ctx.voice_client: await ctx.reply("Not in a voice channel."); return
    if ctx.guild: _clear_music(ctx.guild.id)
    try:
        if ctx.voice_client.is_playing() or ctx.voice_client.is_paused(): ctx.voice_client.stop()
    except Exception: pass
    name = ctx.voice_client.channel.name
    await ctx.voice_client.disconnect()
    with _call_session_lock:
        _call_session = None
    await ctx.reply(f"Left **{name}**.")

# ── Startup ───────────────────────────────────────────────────────────────────

def _warmup():
    time.sleep(5)
    try:
        body = json.dumps({"model": OLLAMA_MODEL, "messages":[{"role":"user","content":"."}], "stream":False, "think":False}).encode()
        req = urllib.request.Request(f"{OLLAMA_BASE}/api/chat", data=body,
            headers={"Content-Type":"application/json"}, method="POST")
        with urllib.request.urlopen(req, timeout=30): pass
        print("[Luna] Ollama warmed up.", flush=True)
    except Exception: pass

def main():
    _configure_social()
    threading.Thread(target=_warmup, daemon=True).start()
    if LUNA_STREAM_AWARENESS:
        threading.Thread(target=_stream_presence_worker, daemon=True).start()
    threading.Thread(target=lambda: web.run(host="127.0.0.1", port=5050, use_reloader=False, threaded=True), daemon=True).start()
    threading.Thread(target=lambda: (time.sleep(2), webbrowser.open("http://127.0.0.1:5050")), daemon=True).start()
    print("Web UI: http://127.0.0.1:5050")
    _start_twitch_ingest()
    try:
        bot.run(DISCORD_TOKEN)
    except discord.LoginFailure:
        print("Invalid token. Use the BOT token from Developer Portal → Bot tab.")
        raise SystemExit(1)

if __name__ == "__main__":
    main()
