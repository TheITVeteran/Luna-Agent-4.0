"""
Luna 4.5 — compact rewrite of bot.py
Discord bot + Web UI + Ollama chat + Shadow commands + TTS/STT + automation
Run: python bot.py
"""
import asyncio, base64, html, io, json, os, random, re, subprocess, sys
import tempfile, threading, time, urllib.parse, urllib.request, urllib.error
import uuid, webbrowser, xml.etree.ElementTree as ET
from collections import deque
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime

import discord
from discord.ext import commands
from dotenv import load_dotenv
from flask import Flask, request, jsonify, send_from_directory, Response, stream_with_context

from luna_files import write_file as luna_write_file
from shadow_agent import strip_shadow_prefix, run_shadow as shadow_run
import celine
from luna_memory import (get_memory_prompt, get_core_memories,
    get_short_term_memories, get_long_term_memories, add_memory, add_core_memory,
    clear_memories, clear_all_memories, merge_memories)
from luna_brain import brain_step
from luna_profile import (get_profile_prompt, get_profile, set_profile_field,
    clear_profile, PROFILE_FIELDS, merge_profiles)
from luna_conversation import get_recent_conversation, append_exchange, merge_conversations
try:
    from luna_security import run_full_scan, scan_file as security_scan_file
except ImportError:
    run_full_scan = None
    security_scan_file = None

# ── Config ──────────────────────────────────────────────────────────────────
_BASE = os.path.dirname(os.path.abspath(__file__))
_DATA = os.path.join(_BASE, "data")

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
OLLAMA_MODEL = _env("OLLAMA_MODEL", "qwen2.5-coder:7b-instruct")
OLLAMA_CHAT  = _env("OLLAMA_CHAT_MODEL", "llama3.2:latest") or "llama3.2:latest"
OLLAMA_SMALL = _env("OLLAMA_MODEL_SMALL") or OLLAMA_MODEL
# Vision model for camera — when you use the camera, Luna uses this to describe what it sees (e.g. granite3.2-vision).
OLLAMA_VISION_MODEL = _env("OLLAMA_VISION_MODEL", "granite3.2-vision").strip()

LINKED_ID  = _env("LINKED_DISCORD_USER_ID", "1414944231222411378")
ADMIN_ID   = _env("DISCORD_ADMIN_ID")
LINKED_SCOPE = f"discord:user:{LINKED_ID}" if LINKED_ID else ""

_linked_int = int(LINKED_ID) if LINKED_ID.isdigit() else None
_admin_int  = int(ADMIN_ID)  if ADMIN_ID.isdigit()  else None

_dm_sync_ids = {int(x) for x in _env("DISCORD_DM_SYNC_USER_IDS").split(",") if x.strip().isdigit()}
_tts_channels = {int(x) for x in _env("DISCORD_TTS_CHANNEL_IDS").split(",") if x.strip().isdigit()}

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
YT_PROFILE_DIR   = _env("YOUTUBE_PROFILE_DIR", os.path.join(_DATA, "youtube_profile"))
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
CUSTOM_PODCAST_DIR = _env("CUSTOM_PODCAST_DIR", r"D:\Luna Agent n8n")
WORLD_NEWS_FEEDS = [
    "https://feeds.bbci.co.uk/news/world/rss.xml",
    "https://rss.nytimes.com/services/xml/rss/nyt/World.xml",
    "https://www.aljazeera.com/xml/rss/all.xml",
]

LUNA_SYSTEM = """You are Luna, a warm and witty AI companion. You are direct, clever, and occasionally playful — like a trusted friend who happens to know everything. Keep replies concise. You have a persistent memory of the user and remember their profile, preferences and goals."""

GTTS_LANG = "en"

_REMINDERS_FILE = os.path.join(_DATA, "reminders.json")
_SOUL_PATH       = os.path.join(_DATA, "SOUL.md")
_TOOLS_PATH      = os.path.join(_DATA, "TOOLS.md")
_OBJECTIVES_PATH = os.path.join(_DATA, "OBJECTIVES.md")
_SKILLS_DIR      = os.path.join(_DATA, "skills")
_GOALS_FILE      = os.path.join(_DATA, "goals.json")
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
_REFLECTION_PATH = os.path.join(_DATA, "last_reflection_date.json")
_EVOLUTION_ENABLED_PATH = os.path.join(_DATA, "evolution_enabled.json")
_SECURITY_ALERTS_PATH   = os.path.join(_DATA, "security_alerts.json")

# ── Locks ────────────────────────────────────────────────────────────────────
_reminders_lock  = threading.Lock()
_identity_lock   = threading.Lock()
_goals_lock      = threading.Lock()
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
_pending_feedback_lock = threading.Lock()
_working_lock    = threading.Lock()
_knowledge_lock  = threading.Lock()
_inbox_lock      = threading.Lock()
_biology_lock    = threading.Lock()
_proactive_lock  = threading.Lock()

# Request feedback (blocking popup): request_id -> { scope, user_message, cmd, params, ts }
_pending_feedback: dict[str, dict] = {}
# Working on / last actions (for UI)
_current_task: str | None = None
_last_actions: list[dict] = []  # [{ "cmd", "summary", "ts" }, ...], keep last 20
_kill_requested: bool = False  # set by KILL button; long-running tasks can check and abort

# Last user activity (for proactive heartbeat: don't speak if user just talked)
_last_user_activity: float = 0.0

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
    "• !suno <desc> — create a Suno song\n"
    "• !suno_ready — tell Luna you're already logged in to Suno (or say **I'm logged in to Suno**)\n"
    "• !share_song / !share_facebook — share to X or Facebook\n"
    "• !yt_comment <url> — transcribe video + AI comment with real context\n"
    "• !ig_dm <user> [msg] — Instagram DM\n"
    "• !fb_msg <name> [msg] — Messenger message\n"
    "• !call <username or user ID> — Discord: contact user (by ID), join VC, transcribe their voice & answer with TTS\n"
    "• !dm <user or ID> [what to say] — Discord DM (Luna turns your input into a dynamic reply, like YT comments)\n"
    "• !msg <contact> [desc] — WhatsApp message\n"
    "• !play <song/url> — play music in voice\n"
    "• !podcast — play custom podcast from CUSTOM_PODCAST_DIR\n"
    "• !podcast create <topic> — Luna generates a short podcast about that topic and saves it to the folder\n"
    "• !remember / !always_remember — store memories\n"
    "• !profile — view or set your profile\n"
    "• !join / !leave / !pause / !skip / !stop / !queue — music\n"
    "• remind me at 7pm to … — Discord DM + voice reminder\n"
    "• retry — retry last failed action with different strategies\n"
    "• !pc_vitals / how's my PC — CPU, RAM, disk\n"
    "• !luna_vitals / how's Luna — Luna's process, Ollama, uptime\n"
    "• Camera (UI) — turn on to let Luna see you; ask **what do you see** for object and face recognition\n"
    "• Nudge (UI) — send a non-blocking note; Luna considers it in her next reply\n"
    "• **ask me** — Luna asks you a question in a popup (demo)\n"
    "• View action log (UI) — recent commands and results\n"
    "• Luna says (UI) — she may speak unprompted when idle; use **Got it** to dismiss\n"
    "• Reflection — Luna writes a daily summary of what she did into her knowledge base\n"
    "• !search <query> — open Google search in your browser\n"
)

COMMAND_ONLY = "I'm **Luna**. Chat with me normally, or say **Shadow, [command]** for actions. Use **!help** for the list."

# What Luna actually does (so she describes these when asked "what can you do?" / "your features")
LUNA_CAPABILITIES = (
    "When asked what you can do, your features, or your capabilities, describe YOUR real system — not generic AI/LLM abilities. "
    "You are Luna, a personal AI companion living on the user's PC. Your real capabilities include: "
    "chat and natural conversation; world news and Google search; creating Suno songs and sharing to X/Facebook; "
    "YouTube comment generation from video context; Instagram and Messenger DMs; Discord DMs and voice (join VC, transcribe, TTS); "
    "WhatsApp messages (you type and send in the browser); reminders (Discord DM + voice at a set time); "
    "playing music and custom podcasts in Discord (and creating podcast episodes from a topic); "
    "PC vitals (CPU, RAM, disk) and Luna vitals (your process, Ollama, uptime); "
    "camera with object and face recognition (user can ask 'what do you see'); "
    "a searchable knowledge base that grows when the user says 'remember that …' and from daily reflections; "
    "nudges (non-blocking notes the user leaves for you to consider); "
    "proactive messages (you sometimes speak unprompted when idle); "
    "asking the user a question in a popup when you need a choice; "
    "action log and Luna's Mind (a live graph of your drives, knowledge, actions); "
    "translation (text and voice to English) via the Translate module; "
    "voice input and TTS; creating and running Python scripts on request. "
    "Keep the list concise and friendly; say **!help** for the full command list."
)

def _build_luna_chat_system(scope: str | None) -> str:
    """Build full system prompt for Luna chat (capabilities + nudges + biology)."""
    system = LUNA_SYSTEM + "\n\n" + LUNA_CAPABILITIES
    nudges = get_nudges(scope or (LINKED_SCOPE or "web"))
    if nudges:
        system = system + "\n\nNudges from user (consider when replying): " + "; ".join(nudges[:5])
    bio = biology_get()
    if bio:
        drives = ", ".join(f"{k}={bio.get(k, 0):.1f}" for k in ("connection", "usefulness", "curiosity") if k in bio)
        if drives:
            system = system + f"\n\nYour internal drives (0–1): {drives}. When connection or usefulness is high you may briefly offer help or show you're there."
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
    intuition = get_intuition_cached(snippet)
    if intuition:
        system = system + "\n\n" + intuition
    # Existential express (growing-agent: when dread/fear high, voice it occasionally)
    if bio and _existential_should_express(bio):
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

def _scope_for(author_id: int, guild_id=None) -> str:
    if _linked_int and author_id == _linked_int:
        return LINKED_SCOPE
    if author_id in _dm_sync_ids:
        return f"discord:user:{author_id}"
    return f"discord:{guild_id}:{author_id}" if guild_id else f"discord:dm:{author_id}"

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
            parts.append("Current user is Chris (Solonaras). Web + Discord share the same memory.")
        profile = get_profile_prompt(scope)
        if profile: parts.append(profile)
        mem = get_memory_prompt(scope)
        if mem: parts.append(mem)
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

def _get_style(scope: str) -> str:
    data = _load_json(_USER_STYLE_FILE, {})
    rec = data.get(scope, {})
    return (rec.get("summary") or "").strip()

# ── Ollama ────────────────────────────────────────────────────────────────────

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
        body = json.dumps({
            "model": (OLLAMA_CHAT or OLLAMA_MODEL).strip(),
            "prompt": _INTUITION_PROMPT.format(snippet=snippet),
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

def ollama_chat(msg: str, system: str | None = None, scope: str | None = None,
                history: list | None = None, model: str | None = None) -> str:
    use_model = (model or OLLAMA_MODEL).strip()
    prompt = _build_system(system or LUNA_SYSTEM, scope)
    messages = []
    if prompt: messages.append({"role": "system", "content": prompt})
    if history:
        for h in history[-_MAX_HISTORY:]:
            r, c = (h.get("role") or "").lower(), (h.get("content") or "").strip()
            if c and r in ("user", "assistant"):
                messages.append({"role": r, "content": c})
    messages.append({"role": "user", "content": msg})
    body = json.dumps({"model": use_model, "messages": messages, "stream": False}).encode()
    req = urllib.request.Request(f"{OLLAMA_BASE}/api/chat", data=body,
        headers={"Content-Type": "application/json"}, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=120) as r:
            data = json.loads(r.read())
        return (data.get("message") or {}).get("content", "").strip() or "No reply."
    except urllib.error.URLError as e:
        return f"Ollama offline: {e.reason}"
    except Exception as e:
        return f"Error: {e}"

def ollama_stream(msg: str, system: str | None = None, scope: str | None = None,
                  history: list | None = None, model: str | None = None):
    """Yields content deltas as they stream from Ollama."""
    use_model = (model or OLLAMA_CHAT).strip()
    prompt = _build_system(system or LUNA_SYSTEM, scope)
    messages = []
    if prompt: messages.append({"role": "system", "content": prompt})
    if history:
        for h in history[-_MAX_HISTORY:]:
            r, c = (h.get("role") or "").lower(), (h.get("content") or "").strip()
            if c and r in ("user", "assistant"):
                messages.append({"role": r, "content": c})
    messages.append({"role": "user", "content": msg})
    body = json.dumps({"model": use_model, "messages": messages, "stream": True}).encode()
    req = urllib.request.Request(f"{OLLAMA_BASE}/api/chat", data=body,
        headers={"Content-Type": "application/json"}, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=120) as resp:
            buf = b""
            for chunk in iter(lambda: resp.read(4096), b""):
                buf += chunk
                while b"\n" in buf:
                    line, buf = buf.split(b"\n", 1)
                    try:
                        c = (json.loads(line).get("message") or {}).get("content") or ""
                        if c: yield c
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

def _compact_history(messages: list[dict]) -> list[dict]:
    if len(messages) <= _COMPACT_AT: return list(messages)
    summary = _summarize(messages[:-_KEEP_RECENT])
    recent = messages[-_KEEP_RECENT:]
    if not summary: return recent
    return [{"role":"user","content":f"[Summary]: {summary}"},
            {"role":"assistant","content":"Understood."}] + recent

# ── Memory capture ────────────────────────────────────────────────────────────

def _capture_memory(scope: str, text: str) -> None:
    text = text.strip()
    if not text or not scope: return
    brain = brain_step(scope, text, context={})
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
    # Brain-driven learning
    if brain.get("should_add_core") and 10 <= len(text) <= 800:
        add_core_memory(scope, text[:500])
    elif brain.get("should_remember") and 10 <= len(text) <= 800:
        add_memory(scope, text[:500])

def _capture_profile(scope: str, text: str) -> None:
    text = text.strip()
    if not text or not scope: return
    checks = [
        (r"\b(?:my name is|call me|i am called)\s+([a-zA-Z][a-zA-Z\s\-']{0,50})(?:\.|,|\s+and|\s*$)", "name"),
        (r"\b(?:i live in|i'?m from|based in)\s+(.+?)(?:\.|$)", "location"),
        (r"\b(?:i work as|i'?m a|i'?m an)\s+(.+?)(?:\.|$)", "occupation"),
        (r"\b(?:my interests? (?:are|is)|i am interested in)\s+(.+?)(?:\.|$)", "interests"),
        (r"\b(?:my birthday is|i was born on)\s+(.+?)(?:\.|$)", "birthday"),
    ]
    for pattern, field in checks:
        m = re.search(pattern, text, re.I | re.S)
        if m:
            val = m.group(1).strip()[:200]
            if len(val) >= 2: set_profile_field(scope, field, val)
            return

# ── TTS ───────────────────────────────────────────────────────────────────────

def _tts_bytes(text: str) -> bytes:
    try:
        from gtts import gTTS
        buf = io.BytesIO()
        gTTS(text=text.strip()[:500], lang=GTTS_LANG, slow=False).write_to_fp(buf)
        buf.seek(0)
        return buf.read()
    except Exception:
        return b""

def _clean_for_tts(text: str) -> str:
    text = re.sub(r"LUNA_WRITE_FILE.*?END_LUNA_WRITE", "", text, flags=re.DOTALL | re.I)
    text = re.sub(r"```[\w]*\n.*?```", "", text, flags=re.DOTALL)
    text = re.sub(r"`[^`]*`", " ", text)
    text = re.sub(r"<[^>]+>", " ", text)
    text = re.sub(r"<@!?\d+>", "", text)
    text = re.sub(r"\*\*", "", text)
    return re.sub(r"\s+", " ", text).strip()

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

_tts_stop = False
_tts_proc: subprocess.Popen | None = None
_tts_lock = threading.Lock()

def _stop_tts():
    global _tts_stop, _tts_proc
    with _tts_lock:
        _tts_stop = True
        p, _tts_proc = _tts_proc, None
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

def _play_reply_tts(reply: str) -> None:
    global _tts_stop
    tts_text = _clean_for_tts(reply)
    if not tts_text.strip(): return
    def _run():
        global _tts_stop
        _tts_stop = False
        for chunk in _split_tts(tts_text):
            if _tts_stop: break
            audio = _tts_bytes(chunk)
            if _tts_stop: break
            if audio: _play_tts(audio)
    threading.Thread(target=_run, daemon=True).start()

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
        nudges = get_nudges(LINKED_SCOPE or "web")
        with _working_lock:
            last_acts = _last_actions[:5]
        _set_planning("Deciding whether to speak…")
        drives_str = ", ".join(f"{k}={state.get(k, 0):.2f}" for k in ("connection", "usefulness", "curiosity"))
        context = f"Drives: {drives_str}. Recent: {[a.get('cmd') for a in last_acts]}. Nudges: {nudges[:3]}."
        prompt = (
            "You are Luna, a loyal AI assistant living on the user's PC. You have internal drives (connection, usefulness, curiosity). "
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
            f"Your drives: {bio.get('connection', 0):.2f} connection, {bio.get('usefulness', 0):.2f} usefulness, {bio.get('curiosity', 0):.2f} curiosity. "
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

# ── Browser helpers ───────────────────────────────────────────────────────────

# Run in every page so Suno/sites see a normal user, not automation (fewer captchas).
_STEALTH_INIT_SCRIPT = """
(function() {
  Object.defineProperty(navigator, 'webdriver', { get: function() { return undefined; }, configurable: true });
  if (window.chrome === undefined) window.chrome = {};
  if (window.chrome.runtime === undefined) window.chrome.runtime = { connect: function() {}, sendMessage: function() {} };
  var q = navigator.permissions && navigator.permissions.query;
  if (q) navigator.permissions.query = function(args) { return args.name === 'notifications' ? Promise.resolve({ state: Notification.permission }) : q.apply(this, arguments); };
  try { delete window.cdc_adoQpoasnfa76pfcZLmcfl_Array; delete window.cdc_adoQpoasnfa76pfcZLmcfl_Promise; delete window.cdc_adoQpoasnfa76pfcZLmcfl_Symbol; } catch (e) {}
})();
"""

# Realistic Chrome user-agent so Suno sees a normal browser (Windows, current Chrome).
_CHROME_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/131.0.0.0 Safari/537.36"
)

def _browser_opts(profile_dir: str) -> dict:
    opts = {"user_data_dir": profile_dir, "headless": False,
        "viewport": {"width": 1280, "height": 900},
        "user_agent": _CHROME_USER_AGENT,
        "ignore_default_args": ["--enable-automation", "--no-sandbox"],
        "args": ["--disable-session-crashed-bubble", "--hide-crash-restore-bubble",
                 "--no-first-run", "--no-default-browser-check"]}
    if BROWSER_PATH and os.path.isfile(BROWSER_PATH):
        opts["executable_path"] = BROWSER_PATH
    elif BROWSER_CHANNEL in ("chrome","msedge","chromium"):
        opts["channel"] = BROWSER_CHANNEL
    return opts

def _launch_social_browser(profile_dir: str, p):
    """Launch Chrome for social/Suno so it looks like a user session (avoids Google blocking login)."""
    context = p.chromium.launch_persistent_context(**_browser_opts(profile_dir))
    context.add_init_script(_STEALTH_INIT_SCRIPT)
    return context

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
    u = (url or "").strip()
    try:
        p = urllib.parse.urlparse(u)
        host = p.netloc.lower()
        path = p.path.strip("/")
        if "youtu.be" in host: return path.split("/")[0]
        if "youtube.com" in host:
            if path == "watch": return urllib.parse.parse_qs(p.query).get("v", [""])[0]
            for prefix in ("shorts/", "live/", "embed/"):
                if path.startswith(prefix): return path.split("/", 1)[1].split("/")[0]
    except Exception: pass
    return ""

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
        return False, f"Set **CUSTOM_PODCAST_DIR** in .env to a writable folder (e.g. D:\\Luna Agent n8n) so I can save the podcast."
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
            audio = _tts_bytes(chunk)
            if not audio:
                continue
            seg_path = os.path.join(temp_dir, f"seg_{i:03d}.mp3")
            with open(seg_path, "wb") as f:
                f.write(audio)
            paths.append(seg_path)
        if not paths:
            return False, "TTS failed for all chunks. Check gTTS and internet."
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
    # YouTube
    try:
        import yt_dlp
        opts = {"noplaylist": True, "quiet": True, "no_warnings": True,
                "default_search": "ytsearch1", "skip_download": True,
                "format": "bestaudio[ext=m4a]/bestaudio/best",
                "extractor_args": {"youtube": {"player_client": "android,web"}}}
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(q, download=False)
        if "entries" in info and info["entries"]: info = info["entries"][0]
        url = info.get("url", "")
        if not url:
            for f in (info.get("formats") or []):
                if f.get("acodec","") not in ("","none") and f.get("vcodec","") in ("","none"):
                    url = f.get("url",""); break
        if not url: return False, "No audio stream found."
        return True, {"title": info.get("title","?"), "web_url": info.get("webpage_url", q),
                      "stream_url": url, "duration": int(info.get("duration") or 0),
                      "http_headers": info.get("http_headers", {})}
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

def _get_recently_shared_ids(max_days: float = 7.0) -> set[str]:
    """Video ids shared in the last max_days (so we prefer not to pick them)."""
    data = _load_json(_SHARED_SONGS_FILE, {})
    cutoff = time.time() - (max_days * 24 * 3600)
    return {vid for vid, ts in data.items() if float(ts) >= cutoff}

def _get_random_channel_song() -> tuple[bool, dict | str]:
    """Pick a song from the channel feed, preferring one not shared recently."""
    try:
        req = urllib.request.Request(YT_FEED_URL, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=20) as r:
            root = ET.fromstring(r.read().decode("utf-8", errors="replace"))
        ns = {"a": "http://www.w3.org/2005/Atom", "yt": "http://www.youtube.com/xml/schemas/2015"}
        songs = []
        for e in root.findall("a:entry", ns):
            title = (e.findtext("a:title", default="", namespaces=ns) or "").strip()
            vid = (e.findtext("yt:videoId", default="", namespaces=ns) or "").strip()
            link_el = e.find("a:link[@rel='alternate']", ns)
            link = (link_el.attrib.get("href","") if link_el is not None else "") or (f"https://youtu.be/{vid}" if vid else "")
            if title and link: songs.append({"title": title, "url": link, "video_id": vid})
        if not songs: return False, "No songs in channel feed."
        recent = _get_recently_shared_ids()
        not_recent = [s for s in songs if s.get("video_id") and s["video_id"] not in recent]
        if not_recent:
            chosen = random.choice(not_recent)
        else:
            chosen = random.choice(songs)
        return True, {"title": chosen["title"], "url": chosen["url"]}
    except Exception as e:
        return False, f"Could not load YouTube feed: {e}"

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

def _fetch_news(limit: int = 8) -> tuple[bool, str]:
    items = []
    for url in WORLD_NEWS_FEEDS:
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
            with urllib.request.urlopen(req, timeout=12) as r:
                root = ET.fromstring(r.read().decode("utf-8", errors="replace"))
            for item in root.findall(".//item"):
                t = (item.findtext("title") or "").strip()
                l = (item.findtext("link") or "").strip()
                p = (item.findtext("pubDate") or item.findtext("published") or "").strip()
                if t and l: items.append({"title": t, "link": l, "published": p})
        except Exception: continue
    if not items: return False, "Could not fetch news."
    seen, uniq = set(), []
    for it in items:
        k = (it["title"].lower(), it["link"])
        if k not in seen: seen.add(k); uniq.append(it)
    def _ts(pub):
        try:
            from email.utils import parsedate_to_datetime as pdt
            return pdt(pub).timestamp()
        except Exception: pass
        try: return datetime.fromisoformat(pub.replace("Z","+00:00")).timestamp()
        except Exception: return 0.0
    uniq.sort(key=lambda x: _ts(x.get("published","")), reverse=True)
    today = datetime.now().date()
    todays = [it for it in uniq if _ts(it.get("published","")) and
              datetime.fromtimestamp(_ts(it["published"])).date() == today]
    top = (todays or uniq)[:limit]
    headlines_only = [it["title"] for it in top]
    try:
        summary = ollama_chat(
            "Summarize these world news headlines in a short, readable paragraph (2–4 sentences). Do not include any URLs, links, or sources. Just the summary.\n\nHeadlines:\n" + "\n".join(f"• {h}" for h in headlines_only),
            system="You are a news summarizer. Output only the summary, no preamble or bullets.",
            model=OLLAMA_MODEL,
        )
        summary = (summary or "").strip()
        if summary:
            return True, "📰 **World news:**\n\n" + summary
    except Exception:
        pass
    return True, "📰 **World news:**\n\n" + "\n\n".join(f"{i}. {it['title']}" for i, it in enumerate(top, 1))

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

def _run_x_share() -> tuple[bool, str]:
    ok, song = _get_random_channel_song()
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
                # Find tweet textbox ("What's happening?") — from X compose dialog DOM: testid=tweetTextarea, role=textbox
                def _find_x_tweet_textbox():
                    for sel in [
                        "[data-testid='tweetTextarea']",
                        "div[data-testid='tweetTextarea'][role='textbox']",
                        "[data-testid='tweetTextarea_0'][role='textbox']",
                        "div[data-testid='tweetTextarea_0'][role='textbox']",
                        "div[data-testid='tweetTextarea_0']",
                        "[data-testid='tweetTextarea_0']",
                        "[role='textbox'][data-contents='true']",
                        "div[role='textbox'][contenteditable='true']",
                        "div[contenteditable='true'][aria-label*='Post']",
                        "[placeholder*='What']",
                        "div[role='textbox']",
                    ]:
                        try:
                            loc = page.locator(sel).first
                            if loc.count() and loc.is_visible():
                                loc.scroll_into_view_if_needed(timeout=3000)
                                page.wait_for_timeout(300)
                                return loc
                        except Exception:
                            continue
                    return None
                tb = None
                page.wait_for_timeout(1500)
                for attempt in range(3):
                    tb = _find_x_tweet_textbox()
                    if tb:
                        break
                    page.wait_for_timeout(2000 if attempt == 0 else 1500)
                if not tb:
                    try:
                        deadline = time.time() + 60
                        while time.time() < deadline and context.pages and not all(pg.is_closed() for pg in context.pages):
                            time.sleep(1)
                        if context.pages: context.close()
                    except Exception: pass
                    return False, "Compose dialog didn't open or text box not found. Log in to X if needed, then try Share Song again."
                tb.click()
                page.wait_for_timeout(400)
                page.keyboard.press("Control+A")
                page.keyboard.press("Backspace")
                page.wait_for_timeout(200)
                msg = _build_x_msg(song["title"], song["url"])
                page.keyboard.type(msg, delay=24)
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
                _record_shared_song(song["url"])
                return True, f'Shared to X: "{song["title"]}"'
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

def _run_fb_share() -> tuple[bool, str]:
    ok, song = _get_random_channel_song()
    if not ok: return False, str(song)
    if not _fb_lock.acquire(blocking=False): return False, "Facebook share already running."
    try:
        from playwright.sync_api import sync_playwright
        os.makedirs(FB_PROFILE_DIR, exist_ok=True)
        context = None
        try:
            with sync_playwright() as p:
                context = _launch_social_browser(FB_PROFILE_DIR, p)
                page = context.pages[0] if context.pages else context.new_page()
                page.goto(FACEBOOK_PROFILE, wait_until="domcontentloaded", timeout=90000)
                page.wait_for_timeout(1000)
                if "login" in page.url.lower():
                    _clear_ready(FB_PROFILE_DIR)
                    # Keep window open at least 60s, then until you close it (log in, then close)
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
                        context = None  # avoid double-close in outer finally
                    return False, "Browser closed. Try Share to Facebook again — you should be logged in now."
                _mark_ready(FB_PROFILE_DIR)
                page.wait_for_timeout(2000)
                # Open composer (Facebook "What's on your mind?" / Create post)
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
                            btn.click(); page.wait_for_timeout(2000); composer_opened = True; break
                    except Exception: continue
                if not composer_opened:
                    # Keep window open at least 60s, then until you close it
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
                    return False, "Browser closed. Log in to Facebook in the window if needed, then try Share to Facebook again."
                tb = None
                for sel in ["div[role='dialog'] div[role='textbox'][contenteditable='true']"]:
                    loc = page.locator(sel).first
                    if loc.count() and loc.is_visible(): tb = loc; break
                if not tb: return False, "Facebook post text box not found."
                tb.click(force=True)
                page.keyboard.press("Control+A"); page.keyboard.press("Backspace")
                page.keyboard.type(_build_fb_msg(song["title"], song["url"]), delay=24)
                page.wait_for_timeout(1500)
                # Click Next to open Post settings (audience, then Post)
                next_clicked = False
                for nsel in ["button:has-text('Next')", "div[role='button']:has-text('Next')", "[aria-label*='Next']"]:
                    try:
                        nbtn = page.locator(nsel).first
                        if nbtn.count() and nbtn.is_visible():
                            nbtn.click(force=True); page.wait_for_timeout(2000); next_clicked = True; break
                    except Exception: continue
                if not next_clicked: return False, "Could not click Next on Create post."
                # Wait for the Post settings (audience) dialog to open — stay in this menu, don't go back to profile
                post_settings_visible = False
                for dsel in ["[aria-label*='Post settings']", "[role='dialog']:has-text('Post settings')", "div[role='dialog']:has-text('Post audience')"]:
                    try:
                        page.wait_for_selector(dsel, state="visible", timeout=8000)
                        post_settings_visible = True
                        break
                    except Exception: continue
                if not post_settings_visible:
                    page.wait_for_timeout(3000)
                page.wait_for_timeout(1000)
                # Only touch Public inside the Post settings dialog (avoid closing the dialog)
                for dsel in ["[aria-label*='Post settings']", "[role='dialog']:has-text('Post settings')"]:
                    try:
                        d = page.locator(dsel).last
                        if d.count() and d.is_visible():
                            pub = d.locator("text=Public").first
                            if pub.count() and pub.is_visible():
                                pub.click(force=True); page.wait_for_timeout(600)
                            break
                    except Exception: continue
                page.wait_for_timeout(800)
                # Wait for the Post button (aria-label="Post") to appear in the dialog
                try:
                    page.locator("[aria-label*='Post settings'] [aria-label='Post'], [role='dialog'] [aria-label='Post']").first.wait_for(state="visible", timeout=6000)
                except Exception: pass
                try:
                    page.locator("[aria-label*='Post settings'] button:has-text('Post')").or_(page.locator("[role='dialog'] button:has-text('Post')")).first.wait_for(state="visible", timeout=3000)
                except Exception: pass
                page.wait_for_timeout(500)
                # Find and click the Post button only inside the Post settings (audience) dialog
                def _try_post_click() -> bool:
                    dialog = None
                    for dsel in ["[aria-label*='Post settings']", "[role='dialog']:has-text('Post settings')", "div[role='dialog']:has-text('Post audience')", "div[role='dialog']"]:
                        try:
                            loc = page.locator(dsel).last
                            if loc.count() and loc.is_visible():
                                dialog = loc
                                break
                        except Exception: continue
                    if not dialog or not dialog.count() or not dialog.is_visible():
                        return False
                    # Strategy 1: aria-label="Post" (the blue Post button — div with role="button" and aria-label="Post")
                    try:
                        post_btn = dialog.locator('[aria-label="Post"]')
                        if post_btn.count() and post_btn.first.is_visible():
                            post_btn.first.click(force=True); return True
                    except Exception: pass
                    try:
                        post_btn = page.locator('[role="dialog"] [aria-label="Post"], [aria-label*="Post settings"] [aria-label="Post"]')
                        if post_btn.count() and post_btn.first.is_visible():
                            post_btn.first.click(force=True); return True
                    except Exception: pass
                    # Strategy 2: Click the only blue button in the menu (Post). Save and others are gray.
                    try:
                        clicked = page.evaluate("""() => {
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
                        }""")
                        if clicked: return True
                    except Exception: pass
                    # Strategy 3: get_by_role(button, name=Post) scoped to dialog
                    try:
                        post_btn = dialog.get_by_role("button", name=re.compile(r"^Post$", re.I))
                        if post_btn.count():
                            post_btn.first.wait_for(state="visible", timeout=2000)
                            post_btn.first.click(force=True); return True
                    except Exception: pass
                    # Strategy 4: Rightmost button in dialog footer (Save left, Post right)
                    try:
                        buttons = dialog.locator("button")
                        n = buttons.count()
                        if n >= 2:
                            right_btn = buttons.nth(n - 1)
                            right_btn.wait_for(state="visible", timeout=2000)
                            t = (right_btn.text_content() or "").strip()
                            if t == "Post":
                                right_btn.click(force=True); return True
                            right_btn.click(force=True); return True
                        if n == 1:
                            b = buttons.first
                            if (b.text_content() or "").strip() == "Post":
                                b.wait_for(state="visible", timeout=2000); b.click(force=True); return True
                    except Exception: pass
                    # Strategy 5: button:has-text('Post') in dialog, wait then click
                    try:
                        post_btn = dialog.locator("button:has-text('Post')")
                        if post_btn.count():
                            post_btn.first.wait_for(state="visible", timeout=2000)
                            post_btn.first.click(force=True); return True
                    except Exception: pass
                    # Strategy 6: iterate buttons, click the one with text "Post"
                    try:
                        for i in range(dialog.locator("button").count()):
                            btn = dialog.locator("button").nth(i)
                            if (btn.text_content() or "").strip() == "Post" and btn.is_visible():
                                btn.click(force=True); return True
                    except Exception: pass
                    # Strategy 7: click bottom-right of dialog (Post button position)
                    try:
                        box = dialog.bounding_box()
                        if box:
                            # Click center of where Post button usually is (right side, bottom)
                            x = box["x"] + box["width"] - 55
                            y = box["y"] + box["height"] - 32
                            page.mouse.click(x, y); return True
                    except Exception: pass
                    # Strategy 8: JS click on button with text "Post" inside dialog
                    try:
                        clicked = page.evaluate("""() => {
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
                        }""")
                        if clicked: return True
                    except Exception: pass
                    return False
                posted = False
                for attempt in range(6):
                    page.wait_for_timeout(600 if attempt else 400)
                    if _try_post_click(): posted = True; break
                if not posted: return False, "Typed post but couldn't click the Post button (tried multiple strategies)."
                page.wait_for_timeout(2000)
                _record_shared_song(song["url"])
                return True, f'Shared to Facebook: "{song["title"]}"'
        except Exception as e:
            return False, f"Facebook error: {e}"
        finally:
            if context:
                try: context.close()
                except Exception: pass
    except ImportError:
        return False, "Playwright not installed."
    finally:
        try: _fb_lock.release()
        except Exception: pass

def _yt_comment(video_url: str) -> tuple[bool, str]:
    vid = _yt_extract_id(video_url)
    if not vid: return False, "Invalid YouTube URL."
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
                model=OLLAMA_MODEL)
        else:
            comment = ollama_chat(
                f"Write one YouTube comment (1-2 sentences, warm, human, max 200 chars). Video: {title}\nContext: {desc}\nReturn only the comment.",
                model=OLLAMA_MODEL)
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
                page.wait_for_timeout(2000)
                # Check the page: are we logged in (like Suno)? Logged-in has avatar, Subscriptions, or comment box; login page has accounts.google.com or prominent Sign in.
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
                            # Prominent Sign in in header = not logged in
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
                    _bootstrap_window(YT_PROFILE_DIR, "https://www.youtube.com", "_yt_boot")
                    return False, "YouTube needs login. Browser opened — log in and close, then try again."
                _mark_ready(YT_PROFILE_DIR)
                # Scroll to comments
                found = False
                for _ in range(18):
                    try:
                        box = page.locator("ytd-comment-simplebox-renderer #simplebox-placeholder").first
                        if box.count() and box.is_visible(): found=True; break
                    except Exception: pass
                    page.mouse.wheel(0, 1200)
                    page.wait_for_timeout(300)
                if not found: return False, "Comment box not found."
                page.locator("ytd-comment-simplebox-renderer #simplebox-placeholder").first.click(force=True)
                page.wait_for_timeout(500)
                editor = page.locator("#contenteditable-root[contenteditable='true']").first
                editor.click(force=True)
                page.keyboard.press("Control+A"); page.keyboard.press("Backspace")
                page.keyboard.type(comment, delay=14)
                # Short pause after typing (more human-like, less bot detection)
                page.wait_for_timeout(int(random.uniform(1200, 2200)))
                # Find the Comment submit button — use the same one users click (aria-label="Comment")
                sub = None
                for sel in (
                    'button[aria-label="Comment"]',
                    '[aria-label="Comment"]',
                    "ytd-commentbox #submit-button button",
                    "ytd-button-renderer#submit-button button",
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

def _run_ig_dm(target: str, message: str = "") -> tuple[bool, str]:
    target = re.sub(r"^@","", target.strip())
    if not re.fullmatch(r"[a-zA-Z0-9._]{2,30}", target): return False, "Invalid Instagram username."
    # Optional: just open Instagram in your browser (no Playwright); you send the message and check replies yourself
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
        dm_text = message.strip() or random.choice(["Hey, I really enjoy your content!", "Hi, great energy — keep it up!"])
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
                # Open the user's profile page (not inbox)
                profile_url = f"{IG_BASE}/{target}/"
                page.goto(profile_url, wait_until="domcontentloaded", timeout=90000)
                page.wait_for_timeout(2000)
                if "login" in page.url.lower() or "accounts/login" in page.url:
                    _bootstrap_window(IG_PROFILE_DIR, f"{IG_BASE}/", "_ig_boot")
                    return False, "Instagram needs login. Browser opened — log in and close, then try again."
                _mark_ready(IG_PROFILE_DIR)

                def _try_click_ig_message_button():
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
                        lambda: page.locator('a:has-text("Message")').first,
                        lambda: page.locator('[role="button"]:has-text("Message")').first,
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
                    # Last resort: JS find any visible element with text "Message" that looks clickable
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

                def _try_find_ig_message_editor():
                    """Try several strategies to find the DM popup message input (analyze the page)."""
                    selectors = [
                        'div[contenteditable="true"][aria-label="Message"]',
                        'div[contenteditable="true"][aria-placeholder="Message..."]',
                        '[placeholder="Message..."]',
                        '[aria-placeholder="Message..."]',
                        'div[role="textbox"][aria-placeholder="Message..."]',
                        'div[contenteditable="true"][data-lexical-editor="true"]',
                        'textarea[placeholder="Message..."]',
                        'div[contenteditable="true"][role="textbox"]',
                        'div[contenteditable="true"]',
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
                    try:
                        editor = page.get_by_placeholder("Message...")
                        if editor.count() and editor.is_visible():
                            return editor.first
                    except Exception:
                        pass
                    try:
                        editor = page.get_by_placeholder("Message")
                        if editor.count() and editor.is_visible():
                            return editor.first
                    except Exception:
                        pass
                    # Analyze page: try last contenteditable or textbox (often the visible chat input)
                    for sel in ['div[contenteditable="true"]', '[role="textbox"]', 'div[data-lexical-editor="true"]']:
                        try:
                            loc = page.locator(sel).last
                            if loc.count() and loc.is_visible():
                                loc.scroll_into_view_if_needed(timeout=1500)
                                return loc
                        except Exception:
                            continue
                    return None

                # Strategy 1: click Message button (try multiple ways)
                clicked = _try_click_ig_message_button()
                if not clicked:
                    return False, "Could not find the **Message** button on the profile. Make sure you're on their profile page and try again."

                # Strategy 2: wait and find message box; retry with more wait or re-click Message with different strategies
                editor = None
                page.wait_for_timeout(1800)
                for attempt in range(4):
                    editor = _try_find_ig_message_editor()
                    if editor:
                        break
                    if attempt == 0:
                        page.wait_for_timeout(2500)
                    elif attempt == 1:
                        page.wait_for_timeout(2000)
                    elif attempt == 2:
                        _try_click_ig_message_button()
                        page.wait_for_timeout(2800)
                    else:
                        page.wait_for_timeout(2000)
                if not editor:
                    return False, (
                        "I couldn't find the message box after trying several strategies. **To help:** "
                        "click the **Message** button on @%s's profile yourself so the chat opens in the bottom-right, "
                        "then say **try again** or run the same command again — I'll send the message without you having to press anything else."
                    ) % target
                editor.click(force=True)
                page.wait_for_timeout(300)
                page.keyboard.type(dm_text, delay=20)
                page.wait_for_timeout(500)
                page.keyboard.press("Enter")
                page.wait_for_timeout(1000)
                _record_recent_social("instagram", target, f"{IG_BASE}/direct/inbox/")
                return True, f"Instagram DM sent to @{target}. Check the notification box to open Instagram and see replies."
        except Exception as e:
            return False, f"Instagram error: {e}"
        finally:
            if context:
                try: context.close()
                except Exception: pass
    except ImportError:
        return False, "Playwright not installed."
    finally:
        try: _ig_lock.release()
        except Exception: pass

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
    if description and description.strip():
        msg_text = ollama_chat(description, system="Write one short friendly WhatsApp message inspired by this context. End with '- from Luna'. Just the message text.")
        msg_text = (msg_text or "").strip() or _default_wa_msg()
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
    """
    If contact is a Discord user ID (snowflake), resolve to username. Then open Discord (web app),
    find the target user in DMs, and click 'Start Voice Call' so the linked user (Solonaras) connects.
    For transcribe + TTS in VC, use !call from Discord so the bot joins the target's voice channel.
    """
    contact = (contact or "").strip()
    if not contact:
        return False, "Usage: !call <Discord username or user ID>"
    # Resolve user ID to username for Playwright search
    username = contact
    if contact.isdigit() and len(contact) >= 17:
        resolved = _resolve_discord_username_by_id(int(contact))
        if resolved:
            username = resolved
    with _discord_web_lock:
        try:
            from playwright.sync_api import sync_playwright
            if _discord_web_boot:
                return False, "Discord login window is still open. Close it, then try **!call** again."
            os.makedirs(DISCORD_WEB_PROFILE_DIR, exist_ok=True)
            context = None
            try:
                with sync_playwright() as p:
                    context = _launch_social_browser(DISCORD_WEB_PROFILE_DIR, p)
                    page = context.pages[0] if context.pages else context.new_page()
                    page.goto(DISCORD_APP_URL, wait_until="domcontentloaded", timeout=60000)
                    page.wait_for_timeout(4000)
                    # Check for login (redirect to login page)
                    if "login" in page.url.lower():
                        try: context.close()
                        except Exception: pass
                        context = None
                        _bootstrap_window(DISCORD_WEB_PROFILE_DIR, DISCORD_APP_URL, "_discord_web_boot")
                        return False, "Discord needs login. Browser opened — log in, then try **!call** again."
                    # Find the user: search "Find or start a conversation" or click DM in list
                    search_sel = page.get_by_placeholder("Find or start a conversation")
                    if search_sel.count() > 0 and search_sel.first.is_visible():
                        search_sel.first.click()
                        page.wait_for_timeout(300)
                        search_sel.first.fill("")
                        search_sel.first.press_sequentially(username, delay=40)
                        page.wait_for_timeout(2500)
                    # Click the DM that contains this username (in the list or in search results)
                    for _ in range(2):
                        try:
                            # Match by visible text (username in the DM list / search result)
                            dm = page.get_by_text(username, exact=False).first
                            if dm.count() > 0 and dm.is_visible():
                                dm.scroll_into_view_if_needed(timeout=2000)
                                page.wait_for_timeout(300)
                                dm.click(timeout=3000)
                                page.wait_for_timeout(2000)
                                break
                        except Exception:
                            pass
                        page.wait_for_timeout(500)
                    # Find and click the call icon (top bar: phone icon next to video, pin, add friend)
                    # Try: "Start Voice Call", then call/voice aria-labels, then "Open Voice" (right panel)
                    call_clicked = False
                    for _ in range(3):
                        if call_clicked:
                            break
                        try:
                            for selector, desc in [
                                (page.get_by_role("button", name=re.compile(r"Start\s+Voice\s+Call", re.I)), "Start Voice Call"),
                                (page.get_by_role("button", name=re.compile(r"Voice\s+Call|^Call$", re.I)), "Voice Call / Call"),
                                (page.locator('button[aria-label*="Voice"], button[aria-label*="Call"]'), "aria-label Voice/Call"),
                                (page.get_by_title(re.compile(r"Voice|Call", re.I)), "title Voice/Call"),
                                (page.locator('button:has-text("Open Voice")'), "Open Voice"),
                            ]:
                                try:
                                    btn = selector.first
                                    if btn.count() > 0 and btn.is_visible():
                                        btn.scroll_into_view_if_needed(timeout=2000)
                                        page.wait_for_timeout(200)
                                        btn.click(timeout=3000)
                                        page.wait_for_timeout(1000)
                                        call_clicked = True
                                        break
                                except Exception:
                                    pass
                            if call_clicked:
                                try: context.close()
                                except Exception: pass
                                return True, f"Connecting you to **{username}** — voice call started."
                        except Exception:
                            pass
                        page.wait_for_timeout(800)
                    try: context.close()
                    except Exception: pass
                    return False, f"Opened Discord but couldn't find the **call icon** (top bar with video, pin) for **{username}**. Click the phone icon yourself, then try again."
            except Exception as e:
                if context:
                    try: context.close()
                    except Exception: pass
                return False, f"Discord call error: {e}"
        except ImportError:
            return False, "Playwright not installed."
    return False, "Discord call failed."


def _dm_message_from_input(user_input: str) -> str:
    """Generate a short Discord DM in Luna's own words from the user's instructions (like YouTube comments: dynamic, generalized)."""
    if not (user_input or user_input.strip()):
        return ""
    brief = user_input.strip()[:800]
    system = (
        "You are Luna, a witty, friendly assistant. The user wants to send a Discord DM. "
        "Below is what they want to get across (their instructions or topic). Write a single short DM (1-3 sentences) "
        "that conveys this in your own words — natural, concise, and friendly. Add emojis if it fits. "
        "Output ONLY the message body to send; no quotes, no 'Message:' prefix, no preamble or explanation."
    )
    out = ollama_chat(
        f"What the user wants to communicate:\n{brief}\n\nWrite the actual DM message to send:",
        system=system,
    )
    if not out or "Ollama" in out or out.startswith("Error:"):
        return ""
    return out.strip().strip('"\'')[:1500]


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
    try:
        from discord.ext import voice_recv
        VoiceRecvClient = voice_recv.VoiceRecvClient
    except ImportError:
        VoiceRecvClient = None
    if VoiceRecvClient is None:
        try:
            await target_channel.connect()
            name = getattr(target_user, "display_name", None) or getattr(target_user, "name", "user")
            await ctx.reply(f"Joined **{target_channel.name}** with **{name}**. Install `discord-ext-voice-recv` for transcribe + TTS.")
        except Exception as e:
            return False, str(e)
        return True, ""
    # Connect with voice receive
    try:
        vc = await target_channel.connect(cls=VoiceRecvClient)
    except Exception as e:
        return False, f"Failed to join voice: {e}"
    # Sink that buffers target user's PCM and triggers transcribe → Ollama → TTS
    import wave
    buffer: list[bytes] = []
    buffer_lock = threading.Lock()
    stop_event = threading.Event()
    SAMPLE_RATE = 48000
    CHANNELS = 2
    SAMPLE_WIDTH = 2  # 16-bit

    class CallSink(voice_recv.AudioSink):
        def wants_opus(self):
            return False
        def write(self, user, data):
            if user and getattr(user, "id", None) == target_user.id and data:
                pcm = getattr(data, "pcm", None)
                if pcm is not None:
                    with buffer_lock:
                        buffer.append(bytes(pcm))
        def cleanup(self):
            stop_event.set()

    sink = CallSink()
    vc.listen(sink, after=lambda e: stop_event.set())

    async def processor_loop():
        nonlocal buffer
        while not stop_event.is_set() and vc.is_connected():
            await asyncio.sleep(1.0)
            if stop_event.is_set():
                break
            with buffer_lock:
                if not buffer:
                    continue
                chunks = b"".join(buffer)
                buffer.clear()
            if len(chunks) < SAMPLE_RATE * SAMPLE_WIDTH * CHANNELS:  # < 1 sec
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
                text_in = await asyncio.to_thread(_whisper_transcribe, wav_path)
                if text_in and text_in.strip():
                    reply = await asyncio.to_thread(ollama_chat, text_in.strip())
                    if reply and reply.strip():
                        await asyncio.to_thread(_play_tts_in_vc_sync, vc, reply.strip())
            except Exception:
                pass
            finally:
                if tmp and os.path.isfile(tmp):
                    try: os.unlink(tmp)
                    except Exception: pass

    asyncio.create_task(processor_loop())
    name = getattr(target_user, "display_name", None) or getattr(target_user, "name", "user")
    await ctx.reply(f"In call with **{name}** — listening; I'll transcribe and answer with voice. Say **!leave** when done.")
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

def _run_messenger_msg(username: str, message: str = "") -> tuple[bool, str]:
    """Use same login as Share to Facebook (FB_PROFILE_DIR). Flow: open Facebook with Playwright → go to user profile → press Message → find Messenger popup on the RIGHT side of the browser → find the text box with placeholder 'Aa' → type message → press Enter to send."""
    msg_text = message.strip() or _default_wa_msg()
    target = re.sub(r"[^a-zA-Z0-9._\-]", "", username.replace(" ", ".").strip()).lower()
    if not target:
        return False, "Invalid Messenger recipient."
    if not _fb_lock.acquire(blocking=False):
        return False, "Facebook/Messenger already in use (share or another message). Try again shortly."
    try:
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
                        try: _fb_lock.release()
                        except Exception: pass
                        return False, f"Browser failed. Close any Facebook window and try again. {launch_err}"
                if not context:
                    try: _fb_lock.release()
                    except Exception: pass
                    return False, "Browser failed to start."
                page = context.pages[0] if context.pages else context.new_page()
                page.goto(f"{FACEBOOK_HOME.rstrip('/')}/{target}", wait_until="domcontentloaded", timeout=60000)
                page.wait_for_timeout(2500)
                if "login" in page.url.lower() or "facebook.com/login" in page.url:
                    _clear_ready(FB_PROFILE_DIR)
                    _bootstrap_window(FB_PROFILE_DIR, FACEBOOK_HOME, "_fb_boot")
                    try: context.close()
                    except Exception: pass
                    try: _fb_lock.release()
                    except Exception: pass
                    return False, "Facebook needs login. Browser opened — log in and close, then try again."
                _mark_ready(FB_PROFILE_DIR)

                def _try_click_fb_message_button():
                    for sel in [
                        'span:has-text("Message")',
                        'a[href*="/messages/t/"]',
                        '[aria-label="Message"]',
                        'div[role="button"]:has-text("Message")',
                        'div[aria-label="Message"]',
                    ]:
                        try:
                            loc = page.locator(sel).first
                            if loc.count() and loc.is_visible():
                                loc.scroll_into_view_if_needed(timeout=3000)
                                page.wait_for_timeout(300)
                                loc.click(timeout=5000)
                                return True
                        except Exception:
                            continue
                    try:
                        btn = page.get_by_role("button", name="Message")
                        if btn.count() and btn.first.is_visible():
                            btn.first.click(timeout=5000)
                            return True
                    except Exception:
                        pass
                    return False

                def _find_fb_message_editor():
                    # Messenger popup is on the RIGHT side of the browser; the text box has placeholder "Aa"
                    vw = (page.viewport_size or {}).get("width", 1000)
                    right_half = vw * 0.4  # insist on right-side (Messenger panel)
                    # First: look for the "Aa" placeholder box (Messenger compose field)
                    for sel in [
                        '[data-placeholder="Aa"]',
                        '[aria-placeholder="Aa"]',
                        '[data-placeholder*="Aa"]',
                        '[placeholder*="Aa"]',
                        'div[contenteditable="true"][data-placeholder="Aa"]',
                        'div[contenteditable="true"][aria-placeholder="Aa"]',
                    ]:
                        try:
                            loc = page.locator(sel).first
                            if loc.count() and loc.is_visible():
                                loc.scroll_into_view_if_needed(timeout=2000)
                                box = loc.bounding_box()
                                if box and box.get("x", 0) >= right_half:
                                    return loc
                        except Exception:
                            continue
                    # Fallback: contenteditable in the right half (Messenger panel)
                    for sel in [
                        'div[contenteditable="true"][data-lexical-editor="true"]',
                        'div[role="textbox"][contenteditable="true"]',
                        'div[contenteditable="true"]',
                    ]:
                        try:
                            loc = page.locator(sel).first
                            if loc.count() and loc.is_visible():
                                loc.scroll_into_view_if_needed(timeout=2000)
                                box = loc.bounding_box()
                                if box and box.get("x", 0) >= right_half:
                                    return loc
                        except Exception:
                            continue
                    try:
                        locs = page.locator('div[contenteditable="true"]')
                        for i in range(min(locs.count(), 12)):
                            loc = locs.nth(i)
                            if loc.is_visible():
                                box = loc.bounding_box()
                                if box and box.get("x", 0) >= right_half:
                                    return loc
                    except Exception:
                        pass
                    return None

                if not _try_click_fb_message_button():
                    try: context.close()
                    except Exception: pass
                    try: _fb_lock.release()
                    except Exception: pass
                    return False, f"Could not find the **Message** button for {username}. Make sure you're on their profile."

                editor = None
                page.wait_for_timeout(2000)
                for attempt in range(4):
                    editor = _find_fb_message_editor()
                    if editor:
                        break
                    if attempt == 0:
                        page.wait_for_timeout(2500)
                    elif attempt == 1:
                        page.wait_for_timeout(2000)
                    elif attempt == 2:
                        _try_click_fb_message_button()
                        page.wait_for_timeout(2800)
                    else:
                        page.wait_for_timeout(2000)
                if not editor:
                    try: context.close()
                    except Exception: pass
                    try: _fb_lock.release()
                    except Exception: pass
                    return False, (
                        "I couldn't find the message box after trying several strategies. **To help:** "
                        "click the **Message** button on their profile yourself so the chat opens, "
                        "then say **try again** or run the same command — I'll send the message."
                    )
                editor.click(force=True)
                page.wait_for_timeout(400)
                page.keyboard.press("Control+A")
                page.keyboard.press("Backspace")
                page.wait_for_timeout(200)
                page.keyboard.type(msg_text, delay=25)
                page.wait_for_timeout(500)
                page.keyboard.press("Enter")
                page.wait_for_timeout(1000)
                _record_recent_social("facebook", target, f"{FACEBOOK_HOME.rstrip('/')}/{target}")
                return True, f'Messenger: sent to **{username}**: "{msg_text[:50]}{"…" if len(msg_text) > 50 else ""}"'
        except Exception as e:
            return False, f"Messenger error: {e}"
        finally:
            if context:
                try: context.close()
                except Exception: pass
            try: _fb_lock.release()
            except Exception: pass
    except ImportError:
        try: _fb_lock.release()
        except Exception: pass
        return False, "Playwright not installed."

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

def _vision_describe_image(image_bytes: bytes, prompt: str = "Describe briefly what you see in this image. One or two sentences. Be concise.") -> str:
    """Call Ollama vision model (e.g. Granite 3.2 Vision) with the image. Returns description or empty if unavailable."""
    if not OLLAMA_VISION_MODEL or not image_bytes:
        return ""
    try:
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
        with urllib.request.urlopen(req, timeout=30) as r:
            data = json.loads(r.read())
        out = (data.get("response") or "").strip()
        return out[:600] if out else ""
    except Exception:
        return ""

def _camera_chat_turn(message: str, image_bytes: bytes | None) -> tuple[bool, str]:
    """One turn in the camera view chat (separate thread with Granite vision model). Returns (ok, reply)."""
    if not OLLAMA_VISION_MODEL:
        return False, "No vision model configured. Set OLLAMA_VISION_MODEL (e.g. granite3.2-vision) in .env."
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
    lines = ["You are a helpful vision assistant. The user is showing you a camera image and chatting about what you see. Answer briefly based on the image and the conversation.", ""]
    for h in history[:-1]:
        who = "User" if h["role"] == "user" else "Assistant"
        lines.append(f"{who}: {h['content']}")
    lines.append(f"User: {message}")
    lines.append("Assistant:")
    prompt = "\n".join(lines)
    try:
        reply = _vision_describe_image(img, prompt=prompt)
        if not reply:
            reply = "I couldn't generate a reply. Try again."
        with _camera_chat_lock:
            _camera_chat_history.append({"role": "assistant", "content": reply})
        return True, reply
    except Exception as e:
        with _camera_chat_lock:
            if _camera_chat_history and _camera_chat_history[-1].get("role") == "user":
                _camera_chat_history.pop()
        return False, str(e)[:200]

def _process_camera_frame(image_bytes: bytes) -> dict:
    """Run face/object detection and, if OLLAMA_VISION_MODEL set, ask the vision model what it observes."""
    result = {"objects": [], "face_count": 0, "summary": "", "vision_summary": "", "ts": time.time(), "error": None}
    try:
        import cv2
        import numpy as np
        nparr = np.frombuffer(image_bytes, np.uint8)
        img = cv2.imdecode(nparr, cv2.IMREAD_COLOR)
        if img is None:
            result["error"] = "Could not decode image"
            result["summary"] = "Could not decode the image."
            return result
        gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
        face_cascade = cv2.CascadeClassifier(
            cv2.data.haarcascades + "haarcascade_frontalface_default.xml"
        )
        faces = face_cascade.detectMultiScale(gray, scaleFactor=1.1, minNeighbors=5, minSize=(30, 30))
        result["face_count"] = len(faces)
        try:
            from ultralytics import YOLO
            model = YOLO("yolov8n.pt")
            det = model(img, verbose=False)[0]
            names = getattr(det, "names", {}) or {}
            for box in det.boxes:
                cls_id = int(box.cls.item())
                conf = float(box.conf.item())
                name = names.get(cls_id, f"object_{cls_id}")
                result["objects"].append({"label": name, "confidence": round(conf, 2)})
        except Exception:
            pass
        parts = []
        if result["face_count"]:
            parts.append(f"{result['face_count']} face(s) detected")
        if result["objects"]:
            from collections import Counter
            counts = Counter(o["label"] for o in result["objects"])
            parts.append("objects: " + ", ".join(f"{v} {k}" for k, v in counts.most_common(12)))
        cv_summary = "; ".join(parts) if parts else "No faces or objects detected."
        # Vision model (e.g. Granite 3.2 Vision): describe what it observes
        vision_summary = _vision_describe_image(image_bytes)
        if vision_summary:
            result["vision_summary"] = vision_summary
            result["summary"] = vision_summary
        else:
            result["summary"] = cv_summary
    except ImportError as e:
        result["error"] = str(e)
        result["summary"] = "Install opencv-python for camera: pip install opencv-python"
    except Exception as e:
        result["error"] = str(e)
        result["summary"] = f"Camera processing error: {e}"
    return result

def _get_camera_see_result() -> str:
    """Return what the vision model (e.g. Granite 3.2 Vision) observed, or OpenCV summary if no vision model."""
    with _camera_lock:
        r = _camera_last_result
    if not r:
        return "I don't have a camera view yet. Turn on **Camera** in the UI so I can see you, then ask again."
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
        "search":         lambda: _search(p.get("query","").strip()),
        "suno":           lambda: _run_suno(p.get("description","").strip()),
        "share_x":        lambda: _run_x_share(),
        "share_facebook": lambda: _run_fb_share(),
        "yt_comment":     lambda: _yt_comment(p.get("video_url","").strip()),
        "ig_dm":          lambda: _run_ig_dm(p.get("target","").strip(), p.get("message","").strip()),
        "fb_msg":         lambda: _run_messenger_msg(p.get("target","").strip(), p.get("message","").strip()),
        "msg":            lambda: _run_wa_msg((p.get("feedback_answer") or p.get("contact","")).strip(), p.get("description",None)),
        "call":           lambda: _run_discord_call(p.get("contact","").strip()),
        "dm":            lambda: _run_discord_dm(p.get("target","").strip(), p.get("message","").strip()),
        "pc_vitals":      lambda: (True, _pc_vitals()),
        "luna_vitals":    lambda: (True, _luna_vitals()),
        "camera_see":     lambda: (True, _get_camera_see_result()),
    }
    # Request feedback (resume with feedback_answer)
    if p.get("feedback_answer") is not None and cmd == "ask_me":
        return f"You chose: **{p.get('feedback_answer', '')}**."
    if cmd == "ask_me":
        return (False, _request_feedback(scope, user_message, "Choose an option:", ["Option A", "Option B"], "ask_me", p))
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
    # Absorbed tools (user-approved drafts)
    if cmd in _absorbed_tool_names:
        ok, result = _run_absorbed_tool(cmd, p)
        if not ok: _record_failure(cmd, result, p); return f"❌ {result}"
        return f"✅ {result}"
    return ""

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

    # News
    if re.search(r"\b(?:news|headlines|latest news|world news)\b", low): return "news", {}
    # Suno — "I'm logged in to Suno" / "suno ready" so Luna uses create flow next time
    if re.search(r"\b(?:suno\s+logged\s+in|logged\s+in\s+to\s+suno|suno\s+ready|i'?m\s+logged\s+in)\b", low):
        return "suno_ready", {}
    # Suno create song
    if re.search(r"\b(?:suno|create a song|make a song)\b", low):
        m = re.search(r"(?:suno|song)\s*[,:]?\s*(.+)", low)
        if m: return "suno", {"description": raw[m.start(1):].strip()}
        return "suno", {"description": raw}
    # Share (Share Song button in UI → share to X)
    if re.search(r"\bshare\s+song\b", low) or re.search(r"\bshare\b.*\b(?:x|twitter)\b", low) or re.search(r"\bpost\b.*\b(?:x|twitter)\b", low):
        return "share_x", {}
    if re.search(r"\bshare\b.*\bfacebook\b", low) or re.search(r"\bpost\b.*\bfacebook\b", low):
        return "share_facebook", {}
    # YouTube comment — explicit "yt_comment" (no !) or "comment" + URL
    yt_url = re.search(r"(https?://(?:www\.)?(?:youtube\.com|youtu\.be)/[^\s)]+)", raw)
    if yt_url and (low.startswith("yt_comment ") or re.search(r"\b(?:comment|reply)\b", low)):
        return "yt_comment", {"video_url": yt_url.group(1).rstrip(".,!?")}
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
    # WhatsApp msg — explicit "msg contact [message]" (contact can be full number with spaces, e.g. +357 99 447267)
    if low.startswith("msg "):
        rest = raw[len("msg "):].strip()
        contact, desc = _parse_wa_contact_and_msg(rest)
        if contact:
            return "msg", {"contact": contact, "description": desc}
    # Natural: "send a message to X" / "message X saying Y"
    m = re.match(r"^(?:send\s+a?\s*message\s+to|message)\s+(.+?)(?:\s+saying\s+(.+))?$", low)
    if m: return "msg", {"contact": m.group(1).strip(), "description": (m.group(2) or "").strip() or None}
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
    # Messenger — explicit "fb_msg name message..." (no !) or natural "messenger/facebook message"
    if low.startswith("fb_msg "):
        rest = raw[len("fb_msg "):].strip()
        parts = rest.split(None, 1)
        if parts:
            target = parts[0].strip()
            msg = (parts[1].strip() if len(parts) > 1 else "") or ""
            if len(target) >= 2 and re.match(r"^[a-zA-Z0-9._\-]+$", target):
                return "fb_msg", {"target": target, "message": msg}
    m = re.search(r"\b(?:messenger|facebook message)\b.*?@?([a-z0-9._\-]{2,50})", low)
    if m: return "fb_msg", {"target": m.group(1), "message": ""}
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
    # Ask me (request feedback popup — demo)
    if re.search(r"\b(?:ask me|ask me something|luna ask me)\b", low): return "ask_me", {}
    # Help
    if re.search(r"\b(?:help|commands|what can you do)\b", low): return "help", {}
    return None

def _likely_command(text: str) -> bool:
    low = (text or "").strip().lower()
    if not low: return False
    if _CONV_START.match(low): return False
    starters = ("play ","podcast ","podcast create ","create podcast ","search ","send ","create ","call ","dm ","tell ","inform ","msg ","share ","post ","remind ","suno ","yt_comment ","comment ","ig_dm ","fb_msg ","google ","news","!help","how's ","pc status","luna status","ram ","what do you see","what can you see","ask me ")
    return any(low.startswith(s) for s in starters) or low in ("play","news","help","skip","stop","ask me") or "luna status" in low or "pc status" in low or bool(re.search(r"\b(what do you see|what can you see|do you see me|describe what you see)\b", low))

def _is_retry(msg: str) -> bool:
    low = (msg or "").strip().lower()
    return any(p in low for p in ("retry","try again","retry that","retry and fix"))

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

# ── Biology / drives (Hull-style: connection, usefulness, curiosity, expression) ──

def _biology_load() -> dict:
    with _biology_lock:
        d = _load_json(_BIOLOGY_PATH, {})
    if not isinstance(d, dict): d = {}
    defaults = {"connection": 0.3, "usefulness": 0.3, "curiosity": 0.3, "expression": 0.2, "last_tick": time.time()}
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
    for key in ("connection", "usefulness", "curiosity", "expression"):
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
        body = json.dumps({
            "model": (OLLAMA_CHAT or OLLAMA_MODEL).strip(),
            "prompt": prompt_template.format(snippet=snippet),
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
    for key in ("connection", "usefulness", "curiosity", "expression"):
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

_restore_linked_user()
_restore_synced_users()

# ── Web app ───────────────────────────────────────────────────────────────────

web = Flask(__name__, static_folder=os.path.join(_BASE, "static"))

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

# Rate limiter
_rate_hits: dict[str, list] = {}

def _rate_ok(ip: str, limit: int = 20, window: int = 60) -> bool:
    now = time.time()
    hits = [t for t in _rate_hits.get(ip, []) if t > now - window]
    if len(hits) >= limit: return False
    hits.append(now)
    _rate_hits[ip] = hits
    return True

@web.route("/")
def serve_index():
    return send_from_directory(_BASE, "index.html")

@web.route("/mind")
@web.route("/mind/")
def serve_mind():
    return send_from_directory(_BASE, "mind.html")

@web.route("/evolution")
@web.route("/evolution/")
def serve_evolution():
    return send_from_directory(_BASE, "evolution.html")

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

@web.route("/api/facebook-check-replies")
def api_facebook_check_replies():
    """Check for new replies in Messenger (same process as Instagram)."""
    target = (request.args.get("target") or "").strip()
    success, has_new, from_user, preview = _check_facebook_replies(target or None)
    if not success:
        return jsonify({"ok": False, "has_new": False, "error": preview, "from": ""})
    return jsonify({"ok": True, "has_new": has_new, "from": from_user, "preview": (preview or "")[:300]})

@web.route("/api/chat", methods=["POST"])
def api_chat():
    global _last_user_activity
    _last_user_activity = time.time()
    ip = request.remote_addr or "unknown"
    if not _rate_ok(ip):
        return jsonify({"error": "Too many requests. Slow down."}), 429
    data = request.get_json(force=True, silent=True) or {}
    msg = (data.get("message") or "").strip()
    if not msg: return jsonify({"error": "No message"}), 400
    scope = LINKED_SCOPE or "web"
    biology_satisfy("connection", 0.15)

    # Pending identity file save
    pending = _pending_file_update.pop(scope, None)
    if pending:
        path = {"SOUL": _SOUL_PATH, "TOOLS": _TOOLS_PATH, "OBJECTIVES": _OBJECTIVES_PATH}.get(pending)
        if path:
            try: _write_file(path, msg); _invalidate_identity()
            except Exception as e: pass
            reply = f"Saved to **{pending}.md**."
            append_exchange(scope, msg, reply); _play_reply_tts(reply)
            return jsonify({"reply": reply})

    # Shadow command
    rest = strip_shadow_prefix(msg)
    if rest is not None:
        reply = shadow_run(rest, scope, _parse_command, _run_cmd, log_fn=_log_action, user_message=msg)
        if isinstance(reply, dict) and reply.get("need_feedback"):
            return jsonify({"reply": reply.get("message") or "Luna is asking…", "need_feedback": True,
                            "request_id": reply["request_id"], "message": reply["message"], "options": reply.get("options")})
        append_exchange(scope, msg, reply); _play_reply_tts(reply)
        return jsonify({"reply": reply})

    # ! commands
    if msg.startswith("!"):
        reply = _handle_bang(msg, scope)
        append_exchange(scope, msg, reply)
        return jsonify({"reply": reply})

    # Retry
    if _is_retry(msg):
        reply = _handle_retry()
        append_exchange(scope, msg, reply); _play_reply_tts(reply)
        return jsonify({"reply": reply})

    # Natural language commands
    if _likely_command(msg):
        parsed = _parse_command(msg)
        if parsed:
            cmd, params = parsed
            if cmd == "help": return jsonify({"reply": HELP_TEXT})
            reply = _run_cmd(cmd, params, scope, user_message=msg)
            if isinstance(reply, dict) and reply.get("need_feedback"):
                return jsonify({"reply": reply.get("message") or "Luna is asking…", "need_feedback": True,
                                "request_id": reply["request_id"], "message": reply["message"], "options": reply.get("options")})
            if reply:
                biology_satisfy("usefulness", 0.2)
                _log_action(cmd, params, reply if isinstance(reply, str) else str(reply))
                _record_last_action(cmd, reply if isinstance(reply, str) else reply.get("message", ""))
                append_exchange(scope, msg, reply); _play_reply_tts(reply)
                return jsonify({"reply": reply})

    # Luna chat (capabilities + nudges + biology so she describes her real features)
    system = _build_luna_chat_system(scope)
    history = _compact_history(get_recent_conversation(scope, 30))
    reply = ollama_chat(msg, system=system, scope=scope, history=history, model=OLLAMA_CHAT)
    if not reply or reply.startswith("Ollama offline"): reply = COMMAND_ONLY
    append_exchange(scope, msg, reply)
    _capture_memory(scope, msg)
    _capture_profile(scope, msg)
    _play_reply_tts(reply)
    return jsonify({"reply": reply})

@web.route("/api/stream", methods=["POST"])
def api_stream():
    """True streaming endpoint — yields chunks as Ollama generates them."""
    data = request.get_json(force=True, silent=True) or {}
    msg = (data.get("message") or "").strip()
    if not msg: return jsonify({"error": "No message"}), 400
    scope = LINKED_SCOPE or "web"
    system = _build_luna_chat_system(scope)
    history = _compact_history(get_recent_conversation(scope, 30))
    def _gen():
        full = []
        for chunk in ollama_stream(msg, system=system, scope=scope, history=history):
            full.append(chunk)
            yield f"data: {json.dumps({'chunk': chunk}, ensure_ascii=False)}\n\n"
        reply = "".join(full).strip()
        append_exchange(scope, msg, reply)
        _capture_memory(scope, msg)
        _capture_profile(scope, msg)
        yield f"data: {json.dumps({'done': True})}\n\n"
    return Response(stream_with_context(_gen()), mimetype="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})

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

@web.route("/api/transcribe", methods=["POST"])
def api_transcribe():
    raw = request.get_data()
    if not raw or len(raw) < 100: return jsonify({"error": "No audio"}), 400
    fd = path = None
    try:
        fd, path = tempfile.mkstemp(suffix=".webm")
        os.write(fd, raw); os.close(fd); fd = None
        try:
            import whisper
            model = getattr(api_transcribe, "_model", None)
            if model is None:
                model = whisper.load_model("base")
                api_transcribe._model = model
            result = model.transcribe(path, fp16=False)
            return jsonify({"text": (result.get("text") or "").strip()})
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
        return jsonify({"text": text})
    except Exception as e:
        return jsonify({"error": str(e)[:200]}), 500
    finally:
        try:
            if path and os.path.isfile(path):
                os.unlink(path)
        except Exception:
            pass


def _handle_bang(msg: str, scope: str) -> str:
    parts = msg.split(None, 1)
    cmd = parts[0].lower()
    args = (parts[1] if len(parts) > 1 else "").strip()
    if cmd in ("!help","!commands","!files"): return HELP_TEXT
    if cmd == "!news":
        ok, r = _fetch_news(); return r if ok else f"❌ {r}"
    if cmd == "!search":
        if not args: return "Usage: !search <query>"
        ok, r = _search(args); return f"✅ {r}" if ok else f"❌ {r}"
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
    if cmd in ("!yt_comment","!youtube_comment"):
        if not args: return "Usage: !yt_comment <url>"
        url = (re.search(r"https?://[^\s]+", args) or type("",(), {"group": lambda s,x: args})()).group(0)
        ok, r = _yt_comment(url)
        if not ok: _record_failure("yt_comment", r, {"video_url": url})
        return f"✅ {r}" if ok else f"❌ {r}"
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
        if not args: return "Usage: !fb_msg <name> [message]"
        ps = args.split(None, 1)
        ok, r = _run_messenger_msg(ps[0], ps[1] if len(ps) > 1 else "")
        if not ok: _record_failure("fb_msg", r, {"target": ps[0]})
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
    await bot.change_presence(activity=discord.Activity(type=discord.ActivityType.listening, name=OLLAMA_CHAT))
    bot.loop.create_task(_reminder_loop())
    bot.loop.create_task(_proactive_heartbeat_loop())
    bot.loop.create_task(_reflection_loop())
    bot.loop.create_task(_evolution_loop())

@bot.event
async def on_message(message: discord.Message):
    if message.author.bot: return

    # Celine: voice clips
    effective = (message.content or "").strip()
    celine_route = None
    voice_text, celine_route = await celine.process_voice_message(
        message, transcribe_fn=_whisper_transcribe,
        route_decider=lambda t: "shadow" if strip_shadow_prefix(t) is not None or _likely_command(t) else "luna",
        run_in_thread=asyncio.to_thread)
    if voice_text: effective = voice_text

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

    if not (isinstance(message.channel, discord.DMChannel) or (bot.user and bot.user.mentioned_in(message))):
        await bot.process_commands(message)
        return

    text = effective
    if bot.user: text = text.replace(f"<@{bot.user.id}>","").strip()
    if not text:
        await message.reply(f"<@{message.author.id}> Hey! I'm **Luna** — chat or say **Shadow, command**. **!help** for list.")
        return

    global _last_user_activity
    _last_user_activity = time.time()
    scope = _scope_for(message.author.id, message.guild.id if message.guild else None)
    mention = f"<@{message.author.id}>"

    # Pending identity file
    if scope in _pending_file_update:
        key = _pending_file_update.pop(scope, None)
        path_map = {"SOUL": _SOUL_PATH, "TOOLS": _TOOLS_PATH, "OBJECTIVES": _OBJECTIVES_PATH}
        if key and key in path_map:
            try: _write_file(path_map[key], text); _invalidate_identity()
            except Exception: pass
            reply = f"Saved to **{key}.md**."
            await asyncio.to_thread(append_exchange, scope, text, reply)
            await message.reply(f"{mention} {reply}"); return

    if _is_retry(text):
        reply = await asyncio.to_thread(_handle_retry)
        await asyncio.to_thread(append_exchange, scope, text, reply)
        await message.reply(f"{mention} {reply}"); return

    if text.strip().lower() in ("!help","!commands","!files"):
        await asyncio.to_thread(append_exchange, scope, text, HELP_TEXT)
        await message.reply(f"{mention} {HELP_TEXT}"); return

    # Shadow
    if celine_route == "shadow" or strip_shadow_prefix(text) is not None:
        rest = strip_shadow_prefix(text) or text
        reply = await asyncio.to_thread(shadow_run, rest, scope, _parse_command, _run_cmd,
            permission_fn=_is_privileged, author_id=message.author.id, log_fn=_log_action, user_message=text)
        if isinstance(reply, dict) and reply.get("need_feedback"):
            await message.reply(f"{mention} {reply.get('message', '?')} — answer in the **web UI** (popup).")
            return
        await asyncio.to_thread(append_exchange, scope, text, reply)
        await message.reply(f"{mention} {reply}"); return

    # NL commands
    if _likely_command(text):
        parsed = await asyncio.to_thread(_parse_command, text)
        if parsed:
            cmd, params = parsed
            if cmd == "help":
                await message.reply(f"{mention} {HELP_TEXT}"); return
            if _is_privileged(message.author.id) or cmd not in ("suno","suno_ready","create_code","msg","dm","call","share_x","share_facebook","yt_comment","ig_dm","fb_msg","remind"):
                reply = await asyncio.to_thread(_run_cmd, cmd, params, scope, text)
                if isinstance(reply, dict) and reply.get("need_feedback"):
                    await message.reply(f"{mention} {reply.get('message', '?')} — answer in the **web UI** (popup).")
                    return
                if reply:
                    await asyncio.to_thread(_log_action, cmd, params, reply)
                    await asyncio.to_thread(append_exchange, scope, text, reply)
                    await message.reply(f"{mention} {reply}"); return

    # Luna chat
    system = await asyncio.to_thread(_build_luna_chat_system, scope)
    history = await asyncio.to_thread(get_recent_conversation, scope, 30)
    history = _compact_history(history)
    try:
        reply = await asyncio.to_thread(ollama_chat, text, system, scope, history, OLLAMA_CHAT)
        if not reply or reply.startswith("Ollama offline"): reply = COMMAND_ONLY
    except Exception: reply = COMMAND_ONLY
    await asyncio.to_thread(append_exchange, scope, text, reply)
    await asyncio.to_thread(_capture_memory, scope, text)
    await asyncio.to_thread(_capture_profile, scope, text)
    await message.reply(f"{mention} {reply}")
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


def _whisper_transcribe(path: str) -> str | None:
    try:
        import whisper
        model = getattr(_whisper_transcribe, "_model", None)
        if model is None: model = whisper.load_model("base"); _whisper_transcribe._model = model
        return (model.transcribe(path, fp16=False).get("text") or "").strip()
    except Exception: return None


def _whisper_translate(path: str) -> str | None:
    """Transcribe audio and translate to English (any source language)."""
    try:
        import whisper
        model = getattr(_whisper_translate, "_model", None)
        if model is None:
            model = whisper.load_model("base")
            _whisper_translate._model = model
        result = model.transcribe(path, fp16=False, task="translate")
        return (result.get("text") or "").strip()
    except Exception:
        return None

# ── Discord ! commands ────────────────────────────────────────────────────────

def _discord_scope(ctx): return _scope_for(ctx.author.id, ctx.guild.id if ctx.guild else None)

@bot.command(name="help", aliases=["files", "commands"])
async def cmd_help(ctx): await ctx.reply(HELP_TEXT)

@bot.command(name="news")
async def cmd_news(ctx):
    ok, r = await asyncio.to_thread(_fetch_news)
    await ctx.reply(r if ok else f"❌ {r}")

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
    await ctx.reply("Posting comment...")
    ok, r = await asyncio.to_thread(_yt_comment, video_url.strip())
    if not ok: _record_failure("yt_comment", r, {"video_url": video_url})
    await ctx.reply(f"{'✅' if ok else '❌'} {r}")

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
    if not args: await ctx.reply("Usage: !fb_msg <name> [message]"); return
    ps = args.split(None, 1)
    ok, r = await asyncio.to_thread(_run_messenger_msg, ps[0], ps[1] if len(ps)>1 else "")
    if not ok: _record_failure("fb_msg", r, {"target": ps[0]})
    await ctx.reply(f"{'✅' if ok else '❌'} {r}")

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
    filled = [(k,v) for k,v in profile.items() if v]
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
    if not ctx.voice_client: await ctx.reply("Not in a voice channel."); return
    if ctx.guild: _clear_music(ctx.guild.id)
    try:
        if ctx.voice_client.is_playing() or ctx.voice_client.is_paused(): ctx.voice_client.stop()
    except Exception: pass
    name = ctx.voice_client.channel.name
    await ctx.voice_client.disconnect()
    await ctx.reply(f"Left **{name}**.")

# ── Startup ───────────────────────────────────────────────────────────────────

def _warmup():
    time.sleep(5)
    try:
        body = json.dumps({"model": OLLAMA_MODEL, "messages":[{"role":"user","content":"."}], "stream":False}).encode()
        req = urllib.request.Request(f"{OLLAMA_BASE}/api/chat", data=body,
            headers={"Content-Type":"application/json"}, method="POST")
        with urllib.request.urlopen(req, timeout=30): pass
        print("[Luna] Ollama warmed up.", flush=True)
    except Exception: pass

def main():
    threading.Thread(target=_warmup, daemon=True).start()
    threading.Thread(target=lambda: web.run(host="127.0.0.1", port=5050, use_reloader=False, threaded=True), daemon=True).start()
    threading.Thread(target=lambda: (time.sleep(2), webbrowser.open("http://127.0.0.1:5050")), daemon=True).start()
    print("Web UI: http://127.0.0.1:5050")
    try:
        bot.run(DISCORD_TOKEN)
    except discord.LoginFailure:
        print("Invalid token. Use the BOT token from Developer Portal → Bot tab.")
        raise SystemExit(1)

if __name__ == "__main__":
    main()
