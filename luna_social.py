"""
luna_social.py — Social automation helpers extracted from bot.py
Browser helpers, news, search, YouTube utilities.

Usage: bot.py calls luna_social.configure(...) at startup to pass function references,
then imports the functions it needs.

To continue the extraction, move more social functions here following the same pattern:
1. Read config from os.environ (bot.py loads .env before importing this module)
2. Use module-level variables set by configure() for function references
3. Keep social-specific state (locks, boot flags) as module-level variables here
"""
import json, os, re, sys, threading, time
import urllib.parse, urllib.request, urllib.error
import tempfile, shutil, webbrowser
import xml.etree.ElementTree as ET
from datetime import datetime

# ── Config (from environment — bot.py loads .env before importing us) ────────

_BASE = os.path.dirname(os.path.abspath(__file__))
_DATA = os.path.join(_BASE, "data")

def _env(key, default=""):
    return os.environ.get(key, default).strip()

BROWSER_CHANNEL = _env("SUNO_BROWSER_CHANNEL", "chrome")
BROWSER_PATH    = _env("SUNO_BROWSER_PATH")
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
YT_PROFILE_DIR   = _env("YOUTUBE_PROFILE_DIR", os.path.join(_DATA, "youtube_profile"))
YT_FEED_URL      = f"https://www.youtube.com/feeds/videos.xml?channel_id={YT_CHANNEL_ID}"
IG_BASE          = _env("INSTAGRAM_BASE_URL", "https://www.instagram.com").rstrip("/")
IG_PROFILE_DIR   = _env("INSTAGRAM_PROFILE_DIR", os.path.join(_DATA, "instagram_profile"))
WA_PROFILE_DIR   = _env("WHATSAPP_WEB_PROFILE_DIR", os.path.join(_DATA, "whatsapp_web_profile"))
WA_WEB_URL       = _env("WHATSAPP_WEB_URL", "https://web.whatsapp.com")
MSG_PROFILE_DIR  = _env("MESSENGER_PROFILE_DIR", os.path.join(_DATA, "messenger_profile"))
MESSENGER_URL    = _env("MESSENGER_URL", FACEBOOK_HOME)
DISCORD_WEB_PROFILE_DIR = _env("DISCORD_WEB_PROFILE_DIR", os.path.join(_DATA, "discord_web_profile"))
DISCORD_APP_URL  = _env("DISCORD_APP_URL", "https://discord.com/app")
MUSIC_DL_DIR     = _env("LUNA_MUSIC_DOWNLOAD_DIR")

WORLD_NEWS_FEEDS = [
    "https://feeds.bbci.co.uk/news/world/rss.xml",
    "https://rss.nytimes.com/services/xml/rss/nyt/World.xml",
    "https://www.aljazeera.com/xml/rss/all.xml",
]

_SOLUTIONS_PATH = os.path.join(_DATA, "automation_solutions.json")

# ── Function references (set by configure()) ─────────────────────────────────

ollama_chat = None          # bot.ollama_chat
_load_json  = None          # bot._load_json
_save_json  = None          # bot._save_json
existential_bump = None     # bot.existential_bump

def configure(**kwargs):
    """Called by bot.py to inject function references."""
    g = globals()
    for k, v in kwargs.items():
        g[k] = v

# ── Social-specific state ────────────────────────────────────────────────────

_suno_boot = _x_boot = _fb_boot = _yt_boot = _ig_boot = _wa_boot = _discord_web_boot = _msg_boot = False
_suno_run_lock = threading.Lock()
_x_lock        = threading.Lock()
_fb_lock       = threading.Lock()
_yt_lock       = threading.Lock()
_ig_lock       = threading.Lock()
_wa_lock       = threading.Lock()
_discord_web_lock = threading.Lock()
_msg_lock      = threading.Lock()
_solutions_lock = threading.Lock()

# ── Browser helpers ──────────────────────────────────────────────────────────

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
    """Launch Chrome for social/Suno so it looks like a user session."""
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
    global _suno_boot
    _mark_ready(SUNO_PROFILE_DIR)
    _suno_boot = False
    return "Marked Suno as logged in. Try **!suno** again."

def _bootstrap_window(profile_dir: str, url: str, flag_name: str, marker_fn=None) -> tuple[bool, str]:
    """Open a browser window for first-time login and keep it open."""
    g = globals()
    if g.get(flag_name): return False, "Login window already open. Finish login and close it, then try again."
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
    if not _load_json: return False
    data = _load_json(_SOLUTIONS_PATH, {})
    return len(data.get(cmd, [])) > 0

def _save_solution(cmd: str, err: str):
    if not _load_json or not _save_json: return
    data = _load_json(_SOLUTIONS_PATH, {})
    entries = data.setdefault(cmd, [])
    entries.append({"error": err[:120], "hint": "extra_wait_and_alternate_selectors"})
    if len(entries) > 20: data[cmd] = entries[-15:]
    with _solutions_lock: _save_json(_SOLUTIONS_PATH, data)

def _record_failure(cmd: str, err: str, params: dict | None = None):
    """Record a social automation failure. Updates bot.py's last-error state."""
    main = sys.modules.get("__main__")
    if main:
        main._last_cmd = cmd
        main._last_err = err
        main._last_params = dict(params or {})
    if existential_bump:
        try: existential_bump(dread=0.12)
        except Exception: pass

# ── News ─────────────────────────────────────────────────────────────────────

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
        try: return datetime.fromisoformat(pub.replace("Z", "+00:00")).timestamp()
        except Exception: return 0.0
    uniq.sort(key=lambda x: _ts(x.get("published", "")), reverse=True)
    today = datetime.now().date()
    todays = [it for it in uniq if _ts(it.get("published", "")) and
              datetime.fromtimestamp(_ts(it["published"])).date() == today]
    top = (todays or uniq)[:limit]
    headlines_only = [it["title"] for it in top]
    if ollama_chat:
        try:
            OLLAMA_MODEL = _env("OLLAMA_MODEL", "llama3.2:latest")
            summary = ollama_chat(
                "Summarize these world news headlines in a short, readable paragraph (2–4 sentences). "
                "Do not include any URLs, links, or sources. Just the summary.\n\nHeadlines:\n"
                + "\n".join(f"• {h}" for h in headlines_only),
                system="You are a news summarizer. Output only the summary, no preamble or bullets.",
                model=OLLAMA_MODEL,
            )
            summary = (summary or "").strip()
            if summary:
                return True, "📰 **World news:**\n\n" + summary
        except Exception: pass
    return True, "📰 **World news:**\n\n" + "\n\n".join(f"{i}. {it['title']}" for i, it in enumerate(top, 1))

# ── Search ───────────────────────────────────────────────────────────────────

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

# ── YouTube helpers ──────────────────────────────────────────────────────────

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
    return u

def _yt_video_id(url: str) -> str:
    """Extract YouTube video ID from a URL."""
    return _yt_extract_id(url)
