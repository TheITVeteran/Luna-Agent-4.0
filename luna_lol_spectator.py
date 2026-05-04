"""
League of Legends live game commentary for Luna.

Two data sources (can combine):

1) **League Live Client Data API** — local ``https://127.0.0.1:2999`` while the game client is open
   on the **same machine** as Luna. No Riot cloud key or PUUID. Set ``RIOT_LOL_USE_LIVE_CLIENT=1``.

2) **Riot cloud** — Spectator v5 + optional Summoner v4 (PUUID → encrypted summoner id). Needs
   ``RIOT_API_KEY`` and PUUID or encrypted id.

If both are enabled, the observer tries the live client first, then falls back to Riot.

Env:
  RIOT_LOL_COMMENTARY_CONFIG — optional path to JSON (default: ``data/lol_commentary_config.json``): streamer persona, mains, soundboard lines.
  RIOT_LOL_SOUNDBOARD — ``1`` (default) to run soundboard triggers before LLM commentary; set ``0`` to disable.
  RIOT_LOL_USE_LIVE_CLIENT — ``1`` to read local Live Client Data (same PC as League)
  RIOT_LOL_LIVE_CLIENT_URL — base URL (default: https://127.0.0.1:2999)
  RIOT_API_KEY — Riot API key (only for cloud path)
  RIOT_LOL_REGION — platform routing host (default: eun1)
  RIOT_LOL_PUUID — Riot PUUID; Summoner v4 resolves to spectator id
  RIOT_LOL_ENCRYPTED_SUMMONER_ID — if set, skip Summoner v4 and call Spectator directly
  RIOT_LOL_IDLE_POLL_SEC — seconds between checks when no active game (default: 300)
  RIOT_LOL_MIN_COMMENT_GAP_SEC — min seconds between Luna lines in one game (default: 40, below poll interval)
  RIOT_LOL_EMIT_COMMENTARY — ``1`` to emit standalone live-commentary lines; default ``0`` (context only for chat replies)
  RIOT_LOL_DEBUG — set to 1 for verbose ``[LoL spectator]`` logs every poll
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import random
import re
import ssl
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Callable

A_OllamaChat = Callable[..., str]


def _env(key: str, default: str = "") -> str:
    return (os.environ.get(key) or default).strip()


def _env_truthy(key: str, default: str = "0") -> bool:
    return _env(key, default).lower() in ("1", "true", "yes", "on")


def live_client_enabled() -> bool:
    """Local 127.0.0.1:2999 Live Client Data (same PC as League client)."""
    return _env_truthy("RIOT_LOL_USE_LIVE_CLIENT", "0")


def is_enabled() -> bool:
    """Riot cloud Spectator path: needs API key + PUUID or encrypted summoner id."""
    if not _env("RIOT_API_KEY"):
        return False
    return bool(_env("RIOT_LOL_ENCRYPTED_SUMMONER_ID") or _env("RIOT_LOL_PUUID"))


def observer_should_run() -> bool:
    return live_client_enabled() or is_enabled()


def emit_commentary_enabled() -> bool:
    """If true, observer emits standalone commentary lines; default on."""
    return _env_truthy("RIOT_LOL_EMIT_COMMENTARY", "1")


_ui_lock = threading.Lock()
_ui: dict[str, Any] = {}
_latest_lock = threading.Lock()
_latest_snapshot: dict[str, Any] | None = None
_latest_source: str | None = None
_latest_ts: float = 0.0

# Soundboard: one-shot per game + K/D deltas vs last poll
_soundboard_state: dict[str, Any] = {
    "session_key": None,
    "fired_ids": set(),
    "prev_kills": None,
    "prev_deaths": None,
    "prev_assists": None,
}
_commentary_cfg_cache: dict[str, Any] = {"path": "", "mtime": 0.0, "data": None}


def _commentary_config_path() -> str:
    p = _env(
        "RIOT_LOL_COMMENTARY_CONFIG",
        os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "lol_commentary_config.json"),
    )
    return os.path.normpath(p)


def load_commentary_config() -> dict[str, Any]:
    """JSON: streamer persona, mains, soundboard triggers. Cached by mtime."""
    path = _commentary_config_path()
    try:
        mtime = os.path.getmtime(path)
    except Exception:
        return {}
    if _commentary_cfg_cache.get("path") == path and abs(float(_commentary_cfg_cache.get("mtime") or 0) - mtime) < 0.01:
        d = _commentary_cfg_cache.get("data")
        return d if isinstance(d, dict) else {}
    try:
        with open(path, "r", encoding="utf-8") as f:
            raw = json.load(f)
    except Exception:
        _commentary_cfg_cache["path"] = path
        _commentary_cfg_cache["mtime"] = mtime
        _commentary_cfg_cache["data"] = {}
        return {}
    data = raw if isinstance(raw, dict) else {}
    _commentary_cfg_cache["path"] = path
    _commentary_cfg_cache["mtime"] = mtime
    _commentary_cfg_cache["data"] = data
    return data


def get_lobby_persona_hint() -> str:
    """Short text for stream solo `lol_lobby` prompts (between games)."""
    cfg = load_commentary_config()
    bits: list[str] = []
    sn = (cfg.get("streamer_name") or "").strip()
    if sn:
        bits.append(f"The streamer's name is {sn}.")
    rv = (cfg.get("lol_lobby_voice") or "").strip()
    if rv:
        bits.append(rv)
    mains = cfg.get("mains")
    if isinstance(mains, list) and mains:
        bits.append(f"They often play: {', '.join(str(x) for x in mains[:8])}.")
    return " ".join(bits).strip()


def _game_session_key(snap: dict[str, Any]) -> str:
    gid = snap.get("gameId")
    if gid is not None:
        return str(gid)
    try:
        blob = json.dumps(snap.get("teams") or {}, sort_keys=True, ensure_ascii=False)[:800]
    except Exception:
        blob = str(snap.get("gameTimeMmSs") or "")
    return hashlib.sha256(blob.encode("utf-8", errors="replace")).hexdigest()[:24]


def _fnum(x: Any) -> float | None:
    try:
        if x is None:
            return None
        return float(x)
    except (TypeError, ValueError):
        return None


def _reset_soundboard_session(key: str) -> None:
    global _soundboard_state
    _soundboard_state["session_key"] = key
    _soundboard_state["fired_ids"] = set()
    _soundboard_state["prev_kills"] = None
    _soundboard_state["prev_deaths"] = None
    _soundboard_state["prev_assists"] = None


def _match_soundboard_when(when: dict[str, Any], snap: dict[str, Any]) -> bool:
    if not isinstance(when, dict) or not when:
        return False
    act = snap.get("activeSummoner")
    if not isinstance(act, dict):
        act = {}
    k = _fnum(act.get("kills"))
    d = _fnum(act.get("deaths"))
    a = _fnum(act.get("assists"))
    cs = _fnum(act.get("creepScore"))
    lvl = _fnum(act.get("level"))
    gold = _fnum(act.get("currentGold"))
    champ = ((act.get("champion") or "") if isinstance(act.get("champion"), str) else "") or ""

    def ge(key: str, cur: float | None) -> bool:
        if key not in when:
            return True
        if cur is None:
            return False
        try:
            return cur >= float(when[key])
        except (TypeError, ValueError):
            return False

    def le(key: str, cur: float | None) -> bool:
        if key not in when:
            return True
        if cur is None:
            return False
        try:
            return cur <= float(when[key])
        except (TypeError, ValueError):
            return False

    if not ge("active_kills_gte", k):
        return False
    if not le("active_kills_lte", k):
        return False
    if not ge("active_deaths_gte", d):
        return False
    if not le("active_deaths_lte", d):
        return False
    if not ge("active_assists_gte", a):
        return False
    if not le("active_assists_lte", a):
        return False
    if not ge("active_cs_gte", cs):
        return False
    if not ge("active_level_gte", lvl):
        return False
    if not ge("active_gold_gte", gold):
        return False

    subs = when.get("active_champion_contains")
    if isinstance(subs, str) and subs.strip():
        if subs.lower() not in champ.lower():
            return False
    if isinstance(subs, list):
        ok = False
        for s in subs:
            if isinstance(s, str) and s.strip() and s.lower() in champ.lower():
                ok = True
                break
        if subs and not ok:
            return False

    gt = _fnum(snap.get("gameTimeSeconds"))
    if "game_time_min_gte" in when and gt is not None:
        try:
            if (gt / 60.0) < float(when["game_time_min_gte"]):
                return False
        except (TypeError, ValueError):
            return False
    if "game_time_min_lte" in when and gt is not None:
        try:
            if (gt / 60.0) > float(when["game_time_min_lte"]):
                return False
        except (TypeError, ValueError):
            return False

    msub = (when.get("map_contains") or "").strip()
    if msub:
        mp = str(snap.get("map") or "")
        if msub.lower() not in mp.lower():
            return False

    ev_sub = (when.get("recent_event_contains") or "").strip()
    if ev_sub:
        evs = snap.get("recentEvents")
        if not isinstance(evs, list):
            return False
        ev_l = ev_sub.lower()
        if not any(ev_l in str(x).lower() for x in evs):
            return False

    pk = _soundboard_state.get("prev_kills")
    pd = _soundboard_state.get("prev_deaths")
    pa = _soundboard_state.get("prev_assists")
    if "kills_delta_gte" in when and k is not None:
        try:
            thr = float(when["kills_delta_gte"])
            delta = (k - pk) if pk is not None else k
            if delta < thr:
                return False
        except (TypeError, ValueError):
            return False
    if "deaths_delta_gte" in when and d is not None:
        try:
            thr = float(when["deaths_delta_gte"])
            delta = (d - pd) if pd is not None else d
            if delta < thr:
                return False
        except (TypeError, ValueError):
            return False
    if "assists_delta_gte" in when and a is not None:
        try:
            thr = float(when["assists_delta_gte"])
            delta = (a - pa) if pa is not None else a
            if delta < thr:
                return False
        except (TypeError, ValueError):
            return False

    return True


def _update_soundboard_prev_from_snap(snap: dict[str, Any]) -> None:
    act = snap.get("activeSummoner")
    if not isinstance(act, dict):
        return
    _soundboard_state["prev_kills"] = _fnum(act.get("kills"))
    _soundboard_state["prev_deaths"] = _fnum(act.get("deaths"))
    _soundboard_state["prev_assists"] = _fnum(act.get("assists"))


def try_soundboard_line(snap: dict[str, Any]) -> str | None:
    """Deterministic lines from `soundboard` in commentary config; None if no match."""
    cfg = load_commentary_config()
    sb = cfg.get("soundboard")
    if not isinstance(sb, list) or not sb:
        return None
    key = _game_session_key(snap)
    if _soundboard_state.get("session_key") != key:
        _reset_soundboard_session(key)

    try:
        chance = float(cfg.get("soundboard_priority") or 1.0)
    except (TypeError, ValueError):
        chance = 1.0
    if chance < 1.0 and random.random() > chance:
        _update_soundboard_prev_from_snap(snap)
        return None

    if not isinstance(_soundboard_state.get("fired_ids"), set):
        _soundboard_state["fired_ids"] = set()
    fired = _soundboard_state["fired_ids"]
    for row in sb:
        if not isinstance(row, dict):
            continue
        tid = str(row.get("id") or "").strip() or None
        when = row.get("when")
        if not isinstance(when, dict):
            continue
        once = row.get("once_per_game", True)
        if once is not False and tid and tid in fired:
            continue
        if not _match_soundboard_when(when, snap):
            continue
        lines = row.get("lines")
        if not isinstance(lines, list) or not lines:
            continue
        opts = [str(x).strip() for x in lines if str(x).strip()]
        if not opts:
            continue
        choice = random.choice(opts)
        if tid:
            fired.add(tid)
        _update_soundboard_prev_from_snap(snap)
        return choice[:500]

    _update_soundboard_prev_from_snap(snap)
    return None


def _persona_prompt_block() -> str:
    cfg = load_commentary_config()
    parts: list[str] = []
    sn = (cfg.get("streamer_name") or "").strip()
    if sn:
        parts.append(f"Streamer display name: {sn}.")
    rns = cfg.get("riot_names")
    if isinstance(rns, list) and rns:
        parts.append("Their Riot names in the roster (that's them): " + ", ".join(str(x) for x in rns[:6]) + ".")
    mains = cfg.get("mains")
    if isinstance(mains, list) and mains:
        parts.append("Champions they often play / identify with: " + ", ".join(str(x) for x in mains[:12]) + ".")
    persona = (cfg.get("persona") or "").strip()
    if persona:
        parts.append("Voice and relationship — follow this closely:\n" + persona)
    rules = (cfg.get("commentary_rules") or "").strip()
    if rules:
        parts.append("Extra rules:\n" + rules)
    if not parts:
        return ""
    return "\n\n### Streamer-specific commentary\n" + "\n".join(parts)


def _set_ui(**kwargs: Any) -> None:
    with _ui_lock:
        _ui.update(kwargs)
        _ui["updated_ts"] = time.time()


def _set_latest_snapshot(snap: dict[str, Any], source: str) -> None:
    global _latest_snapshot, _latest_source, _latest_ts
    with _latest_lock:
        _latest_snapshot = dict(snap)
        _latest_source = (source or "unknown").strip()
        _latest_ts = time.time()


def _clear_latest_snapshot() -> None:
    global _latest_snapshot, _latest_source, _latest_ts
    with _latest_lock:
        _latest_snapshot = None
        _latest_source = None
        _latest_ts = 0.0


def get_live_context(max_chars: int = 1500) -> str:
    """Compact LoL context for normal chat replies (empty string when no live game)."""
    with _latest_lock:
        snap = dict(_latest_snapshot) if isinstance(_latest_snapshot, dict) else None
        src = (_latest_source or "").strip()
        ts = _latest_ts
    if not snap:
        return ""
    map_txt = str(snap.get("map") or "")
    queue_txt = str(snap.get("queue") or "")
    gtype_txt = str(snap.get("gameType") or "")
    mode_hint = str(snap.get("modeHint") or "").strip().upper()
    if not mode_hint:
        blob = " ".join((map_txt, queue_txt, gtype_txt)).lower()
        if any(k in blob for k in ("aram", "howling abyss", "howlingabyss")):
            mode_hint = "ARAM"
        elif any(k in blob for k in ("summoner", "rift", "classic")):
            mode_hint = "SR"
    out: dict[str, Any] = {
        "source": src or snap.get("source") or "unknown",
        "map": snap.get("map"),
        "queue": snap.get("queue"),
        "gameTimeMmSs": snap.get("gameTimeMmSs"),
        "gameType": snap.get("gameType"),
        "modeHint": mode_hint or None,
        "activeSummoner": snap.get("activeSummoner"),
    }
    teams = snap.get("teams")
    if isinstance(teams, dict):
        team_view: dict[str, list[str]] = {}
        for key in ("100", "200"):
            arr = teams.get(key)
            if not isinstance(arr, list):
                continue
            rows: list[str] = []
            for p in arr[:5]:
                if not isinstance(p, dict):
                    continue
                name = (p.get("riotName") or "?").strip() if isinstance(p.get("riotName"), str) else "?"
                champ = (p.get("champion") or "?").strip() if isinstance(p.get("champion"), str) else "?"
                k = p.get("kills")
                d = p.get("deaths")
                a = p.get("assists")
                cs = p.get("creepScore")
                kda = f"{k}/{d}/{a}" if k is not None and d is not None and a is not None else None
                extra = []
                if kda:
                    extra.append(f"KDA {kda}")
                if cs is not None:
                    extra.append(f"CS {cs}")
                suffix = f" ({', '.join(extra)})" if extra else ""
                rows.append(f"{name} on {champ}{suffix}")
            if rows:
                team_view[key] = rows
        if team_view:
            out["teams"] = team_view
    if ts > 0:
        out["updatedSecAgo"] = max(0, int(time.time() - ts))
    txt = json.dumps(out, ensure_ascii=False)
    txt = txt[: max(250, int(max_chars))]
    mode_guard = ""
    if mode_hint == "ARAM":
        mode_guard = (
            "\nMode rule: This is ARAM/Howling Abyss. Do NOT call it Summoner's Rift, "
            "and avoid lane/objective language tied to SR (top/jungle/dragon/baron lanes)."
        )
    elif mode_hint == "SR":
        mode_guard = "\nMode rule: This is Summoner's Rift (not ARAM)."
    return (
        "\n\n## Live League context\n"
        "The user is currently in a live League match. Use this as situational context for conversation/coaching. "
        "Don't ignore their direct question; blend game awareness naturally."
        f"{mode_guard}\n"
        f"{txt}"
    )


def lol_vision_alignment_hint() -> str:
    """Short instructions for vision models when screen-sharing League: prioritize local player + allied HUD."""
    with _latest_lock:
        snap = dict(_latest_snapshot) if isinstance(_latest_snapshot, dict) else None
    if not snap:
        return ""
    active = snap.get("activeSummoner")
    champ = ""
    if isinstance(active, dict):
        champ = str(active.get("champion") or "").strip()
        if champ in ("?", ""):
            champ = ""
    teams = snap.get("teams")
    ally_tid = None
    if isinstance(teams, dict) and champ:
        ch_low = champ.lower()
        for tid in ("100", "200"):
            arr = teams.get(tid)
            if not isinstance(arr, list):
                continue
            for p in arr:
                if not isinstance(p, dict):
                    continue
                pc = str(p.get("champion") or "").strip().lower()
                if pc == ch_low:
                    ally_tid = tid
                    break
            if ally_tid:
                break
    side_human = ""
    if ally_tid == "100":
        side_human = "blue side (team id 100)"
    elif ally_tid == "200":
        side_human = "red side (team id 200)"
    lines = [
        "League of Legends: a Live Client snapshot for this PC exists — treat the image as in-game UI unless obviously not.",
        "Prioritize describing elements tied to **the local player's champion and allied teammates** "
        "(their HP/resource bar, cooldown row, portrait strip, ally-centric scoreboard columns, pings on allies). "
        "Deprioritize enemy-only framing unless the USER QUESTION asks about opponents.",
    ]
    if champ:
        lines.insert(1, f"The local player is on **{champ}** (from Live Client).")
    if side_human:
        lines.insert(2 if champ else 1, f"They are **{side_human}** — when a scoreboard is visible, allied rows usually match this side.")
    return "\n".join(lines)


def get_spectator_status() -> dict[str, Any]:
    """Snapshot for hub UI: live client and/or Riot cloud, in-game flag, errors."""
    with _ui_lock:
        snap = dict(_ui)
    if not observer_should_run():
        return {
            "configured": False,
            "task_running": False,
            "message": (
                "Set RIOT_LOL_USE_LIVE_CLIENT=1 (same PC as League) and/or "
                "RIOT_API_KEY + RIOT_LOL_PUUID (or RIOT_LOL_ENCRYPTED_SUMMONER_ID)"
            ),
        }
    if not snap:
        modes = []
        if live_client_enabled():
            modes.append("Live Client (local)")
        if is_enabled():
            modes.append("Riot cloud")
        return {
            "configured": True,
            "task_running": False,
            "message": "Observer task starting…",
            "region": _region_host() if is_enabled() else None,
            "modes": modes,
        }
    snap.setdefault("configured", True)
    return snap


def get_observer_session_flags() -> dict[str, Any]:
    """For stream solo banter: in-match vs between-games (lobby/queue) this observer session."""
    with _ui_lock:
        ui = dict(_ui)
    ig = bool(ui.get("in_game"))
    sm = bool(ui.get("session_saw_match"))
    # Between games: not in a live match but we already saw at least one match this session (client/lobby).
    bg = sm and not ig
    return {
        "in_match": ig,
        "session_saw_match": sm,
        "between_games": bg,
    }


def _region_host() -> str:
    r = _env("RIOT_LOL_REGION", "eun1").lower().rstrip("/")
    if "." in r:
        return r.split("://", 1)[-1].rstrip("/")
    return f"{r}.api.riotgames.com"


def _riot_get(url: str, api_key: str) -> tuple[int, dict[str, Any] | None, str]:
    req = urllib.request.Request(
        url,
        headers={"X-Riot-Token": api_key},
        method="GET",
    )
    try:
        with urllib.request.urlopen(req, timeout=25) as resp:
            body = resp.read().decode("utf-8", errors="replace")
            if not body:
                return resp.getcode() or 200, None, ""
            return resp.getcode() or 200, json.loads(body), ""
    except urllib.error.HTTPError as e:
        try:
            err_body = e.read().decode("utf-8", errors="replace")
        except Exception:
            err_body = ""
        return e.code, None, err_body or str(e.reason)
    except Exception as e:
        return 0, None, str(e)


def _fetch_encrypted_summoner_id(api_key: str, host: str, puuid: str) -> str | None:
    url = f"https://{host}/lol/summoner/v4/summoners/by-puuid/{puuid}"
    code, data, _err = _riot_get(url, api_key)
    if code != 200 or not isinstance(data, dict):
        return None
    sid = data.get("id")
    return sid if isinstance(sid, str) and sid.strip() else None


def _fetch_active_game(api_key: str, host: str, encrypted_summoner_id: str) -> tuple[int, dict[str, Any] | None]:
    enc = urllib.parse.quote(encrypted_summoner_id, safe="-_.~")
    url = f"https://{host}/lol/spectator/v5/active-games/by-summoner/{enc}"
    code, data, _err = _riot_get(url, api_key)
    return code, data if isinstance(data, dict) else None


def _live_client_base_url() -> str:
    u = _env("RIOT_LOL_LIVE_CLIENT_URL", "https://127.0.0.1:2999").strip().rstrip("/")
    return u or "https://127.0.0.1:2999"


def _fetch_live_client_all() -> dict[str, Any] | None:
    """Local HTTPS (self-signed). Unavailable when not in game or client closed."""
    url = f"{_live_client_base_url()}/liveclientdata/allgamedata"
    ctx = ssl._create_unverified_context()
    req = urllib.request.Request(url, method="GET")
    try:
        with urllib.request.urlopen(req, timeout=3, context=ctx) as resp:
            body = resp.read().decode("utf-8", errors="replace")
            if not body.strip():
                return None
            data = json.loads(body)
            return data if isinstance(data, dict) else None
    except Exception:
        return None


def _live_client_in_match(data: dict[str, Any]) -> bool:
    players = data.get("allPlayers")
    if not isinstance(players, list) or len(players) < 1:
        return False
    return isinstance(data.get("gameData"), dict)


_champ_id_to_name: dict[int, str] | None = None
_ddragon_version: str | None = None


def _ensure_champion_map() -> dict[int, str]:
    global _champ_id_to_name, _ddragon_version
    if _champ_id_to_name is not None:
        return _champ_id_to_name
    out: dict[int, str] = {}
    try:
        with urllib.request.urlopen(
            "https://ddragon.leagueoflegends.com/api/versions.json", timeout=15
        ) as r:
            versions = json.loads(r.read().decode())
        if isinstance(versions, list) and versions:
            _ddragon_version = str(versions[0])
        else:
            _ddragon_version = "14.1.1"
        with urllib.request.urlopen(
            f"https://ddragon.leagueoflegends.com/cdn/{_ddragon_version}/data/en_US/champion.json",
            timeout=20,
        ) as r2:
            blob = json.loads(r2.read().decode())
        data = (blob or {}).get("data") or {}
        for _slug, row in data.items():
            if not isinstance(row, dict):
                continue
            try:
                cid = int(row.get("key", "0"))
            except (TypeError, ValueError):
                continue
            name = (row.get("name") or row.get("id") or str(cid)).strip()
            if cid and name:
                out[cid] = name
    except Exception:
        pass
    _champ_id_to_name = out
    return out


def _map_id_name(mid: int) -> str:
    return {
        11: "Summoner's Rift (SR)",
        12: "Howling Abyss (ARAM)",
        21: "Nexus Blitz",
    }.get(mid, f"map {mid}")


def _queue_name(qid: int) -> str:
    # Common queueConfigIds (not exhaustive)
    return {
        420: "Ranked Solo/Duo",
        440: "Ranked Flex",
        430: "Normal Blind",
        400: "Normal Draft",
        450: "ARAM",
        900: "URF",
        1020: "One for All",
        1300: "Nexus Blitz",
        1700: "Arena",
        1900: "Pick URF",
    }.get(qid, f"queue {qid}")


def _participant_snapshot(p: dict[str, Any], names: dict[int, str]) -> dict[str, Any]:
    cid = p.get("championId")
    try:
        cid_int = int(cid) if cid is not None else 0
    except (TypeError, ValueError):
        cid_int = 0
    row: dict[str, Any] = {
        "riotName": (p.get("riotIdGameName") or p.get("summonerName") or "").strip() or None,
        "teamId": p.get("teamId"),
        "championId": cid_int,
        "champion": names.get(cid_int) or (f"champion {cid_int}" if cid_int else "?"),
        "bot": bool(p.get("bot")),
    }
    # Spectator REST lobby snapshot usually has no live K/D/A/gold; pass through if present.
    for k in (
        "kills",
        "deaths",
        "assists",
        "goldEarned",
        "gold",
        "championPoints",
        "level",
        "creepScore",
        "score",
    ):
        if k in p and p.get(k) is not None:
            row[k] = p.get(k)
    return row


def build_game_snapshot(game: dict[str, Any]) -> dict[str, Any]:
    names = _ensure_champion_map()
    start_ms = int(game.get("gameStartTime") or 0)
    elapsed_sec = max(0, int(time.time() * 1000 - start_ms) // 1000) if start_ms else 0
    m = game.get("mapId")
    q = game.get("gameQueueConfigId")
    try:
        mid = int(m) if m is not None else 0
    except (TypeError, ValueError):
        mid = 0
    try:
        qid = int(q) if q is not None else 0
    except (TypeError, ValueError):
        qid = 0
    parts = game.get("participants")
    if not isinstance(parts, list):
        parts = []
    teams: dict[str, list[dict[str, Any]]] = {"100": [], "200": []}
    for p in parts:
        if not isinstance(p, dict):
            continue
        snap = _participant_snapshot(p, names)
        tid = str(p.get("teamId") or "")
        if tid in teams:
            teams[tid].append(snap)
        else:
            teams.setdefault("other", []).append(snap)
    bans_out: list[dict[str, Any]] = []
    bans = game.get("bannedChampions")
    if isinstance(bans, list):
        for b in bans[:20]:
            if not isinstance(b, dict):
                continue
            try:
                cid_b = int(b.get("championId") or 0)
            except (TypeError, ValueError):
                cid_b = 0
            bans_out.append(
                {
                    "teamId": b.get("teamId"),
                    "pickTurn": b.get("pickTurn"),
                    "champion": names.get(cid_b) or cid_b,
                }
            )
    return {
        "gameId": game.get("gameId"),
        "gameType": game.get("gameType"),
        "map": _map_id_name(mid) if mid else "?",
        "queue": _queue_name(qid) if qid else "?",
        "gameTimeSeconds": elapsed_sec,
        "gameTimeMmSs": f"{elapsed_sec // 60}:{elapsed_sec % 60:02d}",
        "teams": teams,
        "bans": bans_out,
        "note": (
            "Riot Spectator REST returns lobby/loadout data; live K/D/A and gold are often absent "
            "until end-of-game. If numeric stats are missing, comment on comps, timer, queue, and vibe."
        ),
    }


def _live_team_key(team_val: Any) -> str:
    t = str(team_val or "").upper()
    if t == "ORDER":
        return "100"
    if t == "CHAOS":
        return "200"
    return t or "other"


def build_snapshot_from_live_client(data: dict[str, Any]) -> dict[str, Any]:
    gd = data["gameData"] if isinstance(data.get("gameData"), dict) else {}
    raw_gt = gd.get("gameTime")
    try:
        gt = float(raw_gt) if raw_gt is not None else 0.0
    except (TypeError, ValueError):
        gt = 0.0
    elapsed_sec = max(0, int(gt))
    map_mode = (gd.get("mapMode") or "").strip() or "?"
    map_terrain = (gd.get("mapTerrain") or "").strip()
    try:
        mnum = int(gd.get("mapNumber") or 0)
    except (TypeError, ValueError):
        mnum = 0
    mode_blob = f"{map_mode} {map_terrain}".lower()
    is_aram = (mnum == 12) or ("aram" in mode_blob) or ("howling" in mode_blob)
    is_sr = (mnum == 11) or ("summoner" in mode_blob) or ("rift" in mode_blob)
    if mnum == 11:
        map_display = "Summoner's Rift"
    elif mnum == 12:
        map_display = "Howling Abyss (ARAM)"
    else:
        map_display = (map_terrain or map_mode or "?").strip() or "?"
    if is_aram and "aram" not in map_display.lower():
        map_display = "Howling Abyss (ARAM)"
    queue_display = "ARAM" if is_aram else ("Summoner's Rift" if is_sr else (map_mode or "?"))
    mode_hint = "ARAM" if is_aram else ("SR" if is_sr else None)

    teams: dict[str, list[dict[str, Any]]] = {"100": [], "200": []}
    players = data.get("allPlayers")
    if isinstance(players, list):
        for p in players:
            if not isinstance(p, dict):
                continue
            team_key = _live_team_key(p.get("team"))
            scores = p.get("scores") if isinstance(p.get("scores"), dict) else {}
            row: dict[str, Any] = {
                "riotName": (
                    (p.get("riotId") or p.get("summonerName") or p.get("rawDisplayName") or "")
                    .strip()
                    or None
                ),
                "teamId": 100 if team_key == "100" else 200 if team_key == "200" else p.get("team"),
                "champion": (p.get("championName") or "?").strip() or "?",
                "bot": bool(p.get("isBot")),
            }
            if p.get("level") is not None:
                row["level"] = p.get("level")
            for k in ("kills", "deaths", "assists", "creepScore"):
                v = scores.get(k)
                if v is not None:
                    row[k] = v
            if team_key in teams:
                teams[team_key].append(row)
            else:
                teams.setdefault("other", []).append(row)

    active_line: dict[str, Any] | None = None
    active = data.get("activePlayer")
    if isinstance(active, dict):
        sc = active.get("scores") if isinstance(active.get("scores"), dict) else {}
        try:
            gold = float(active.get("currentGold") or 0)
        except (TypeError, ValueError):
            gold = 0.0
        active_line = {
            "champion": (active.get("championName") or "?").strip() or "?",
            "currentGold": gold,
            "level": active.get("level"),
            "kills": sc.get("kills"),
            "deaths": sc.get("deaths"),
            "assists": sc.get("assists"),
            "creepScore": sc.get("creepScore"),
        }

    events_short: list[str] = []
    ev = data.get("events")
    if isinstance(ev, dict):
        elist = ev.get("Events")
        if isinstance(elist, list):
            for e in elist[-12:]:
                if not isinstance(e, dict):
                    continue
                ename = (e.get("EventName") or e.get("eventName") or "").strip()
                if ename:
                    events_short.append(ename)

    return {
        "gameId": None,
        "source": "live_client",
        "gameType": map_mode,
        "map": map_display,
        "queue": queue_display,
        "modeHint": mode_hint,
        "gameTimeSeconds": elapsed_sec,
        "gameTimeMmSs": f"{elapsed_sec // 60}:{elapsed_sec % 60:02d}",
        "teams": teams,
        "bans": [],
        "activeSummoner": active_line,
        "recentEvents": events_short or None,
        "note": (
            "From League Live Client Data API (local). Live gold, K/D/A, CS; recentEvents if present. "
            "activeSummoner is the local player's champion when exposed."
        ),
    }


def generate_luna_comment(
    snap: dict[str, Any],
    ollama_chat: A_OllamaChat,
    model: str,
    build_chat_system: Callable[[], str],
    build_commentary_system: Callable[[], str] | None = None,
) -> str:
    if _env_truthy("RIOT_LOL_SOUNDBOARD", "1"):
        sb = try_soundboard_line(snap)
        if sb:
            return sb

    body = json.dumps(snap, indent=2, ensure_ascii=False)[:6000]
    try:
        if build_commentary_system is not None:
            system = build_commentary_system()
        else:
            system = build_chat_system()
    except Exception as ex:
        print(f"[LoL spectator] build_chat_system failed: {ex}", flush=True)
        system = (
            "You are Luna, a warm witty AI companion. The user is in a League of Legends match; "
            "comment briefly on the JSON snapshot."
        )
    persona = _persona_prompt_block()
    prompt = (
        "Here is the current League of Legends match snapshot (Riot cloud and/or local Live Client data).\n"
        "Write **one or two short chill spoken lines** to the player, following your system persona"
        + (" and the streamer-specific block below" if persona else "")
        + ".\n"
        "Speak to **you** (second person). This is a voice line, not a recap article — do **not** narrate the player as"
        " “the user/the player” and do **not** start with “Based on the snapshot / Looking at the data”.\n"
        "Do NOT read stats like a news caster. Use the JSON as background context and react naturally to interesting moments.\n"
        "If there is nothing notable and it would be filler, output exactly: SKIP\n"
        "When there IS something notable, prefer champion names + key moment context. Mention numbers only when truly relevant.\n"
        "No markdown fences, no bullet list; plain prose only."
        f"{persona}\n\n"
        f"{body}"
    )
    out = (ollama_chat(prompt, system=system, model=model, compact=True) or "").strip()
    out = " ".join(out.split())
    return out[:500]


async def commentary_loop(
    *,
    ollama_chat: A_OllamaChat,
    ollama_model: str,
    on_luna_reply: Callable[[str], None],
    build_chat_system: Callable[[], str],
    build_commentary_system: Callable[[], str] | None = None,
    should_emit: Callable[[], bool] | None = None,
) -> None:
    """Background task: Live Client (local) and/or Riot cloud.

    Always refreshes latest snapshot for `get_live_context()`.
    Standalone commentary output is optional via `RIOT_LOL_EMIT_COMMENTARY=1`.
    """
    if not observer_should_run():
        return
    use_riot = is_enabled()
    use_lc = live_client_enabled()
    api_key = _env("RIOT_API_KEY")
    if use_riot and not api_key:
        return

    _clear_latest_snapshot()
    _set_ui(task_running=True, configured=True, message="Starting observer…")
    host = _region_host()
    idle_sec = max(60, int(_env("RIOT_LOL_IDLE_POLL_SEC", "300") or "300"))
    poll_in_game = max(20, int(_env("RIOT_LOL_POLL_IN_GAME_SEC", "45") or "45"))
    min_gap = max(25, int(_env("RIOT_LOL_MIN_COMMENT_GAP_SEC", "40") or "40"))
    enc = _env("RIOT_LOL_ENCRYPTED_SUMMONER_ID")
    puuid = _env("RIOT_LOL_PUUID")
    lc_base = _live_client_base_url()

    in_game = False
    was_ever_in_game = False
    last_comment_ts = 0.0
    last_summoner_fail_log = 0.0
    last_status_log = 0.0
    debug = _env("RIOT_LOL_DEBUG", "0").lower() in ("1", "true", "yes", "on")

    await asyncio.sleep(8)
    _set_ui(
        region=host if use_riot else None,
        live_client_url=lc_base if use_lc else None,
        poll_in_game_sec=poll_in_game,
        idle_poll_sec=idle_sec,
        session_saw_match=False,
        between_games=False,
        message="Polling…",
    )
    bits: list[str] = []
    if use_lc:
        bits.append(f"Live Client `{lc_base}`")
    if use_riot:
        bits.append(f"Riot `{host}`")
    print(
        f"[LoL spectator] Task running — {'; '.join(bits)}. In-game poll every {poll_in_game}s; "
        f"after a match, idle every {idle_sec}s. Hub: /api/lol-chat/pending.",
        flush=True,
    )

    while True:
        try:
            if in_game:
                poll_after = poll_in_game
            elif was_ever_in_game:
                poll_after = idle_sec
            else:
                poll_after = poll_in_game
            await asyncio.sleep(poll_after)

            if use_riot and not _env("RIOT_API_KEY"):
                _set_ui(task_running=False, message="RIOT_API_KEY removed — stopping")
                return

            snap: dict[str, Any] | None = None
            data_src: str | None = None
            last_http: int | str | None = None

            if use_lc:
                lc_raw = await asyncio.to_thread(_fetch_live_client_all)
                if lc_raw and _live_client_in_match(lc_raw):
                    snap = build_snapshot_from_live_client(lc_raw)
                    data_src = "live_client"
                    last_http = "local"
                    if debug or (time.time() - last_status_log > 90):
                        last_status_log = time.time()
                        print(f"[LoL spectator] Live Client OK (in_game={in_game})", flush=True)

            if snap is None and use_riot:
                eid = enc
                if not eid and puuid:
                    eid = await asyncio.to_thread(_fetch_encrypted_summoner_id, api_key, host, puuid)
                    if not eid:
                        nowf = time.time()
                        _set_ui(
                            summoner_ok=False,
                            in_game=False,
                            session_saw_match=was_ever_in_game,
                            between_games=was_ever_in_game,
                            last_riot_http=None,
                            last_error="Summoner v4 by-puuid failed — check RIOT_LOL_PUUID and RIOT_LOL_REGION",
                            message="Cannot resolve summoner id",
                        )
                        if nowf - last_summoner_fail_log > 120:
                            last_summoner_fail_log = nowf
                            print(
                                "[LoL spectator] Summoner v4 lookup returned no id — check RIOT_LOL_PUUID and "
                                f"RIOT_LOL_REGION (host `{host}`).",
                                flush=True,
                            )
                        continue

                _set_ui(summoner_ok=True, last_error=None)
                code, game = await asyncio.to_thread(_fetch_active_game, api_key, host, eid)
                last_http = code
                if debug or (time.time() - last_status_log > 90):
                    last_status_log = time.time()
                    print(f"[LoL spectator] Riot active-game HTTP {code} (in_game={in_game})", flush=True)

                if code == 401 or code == 403:
                    msg = "Riot auth failed — check API key"
                    if use_lc:
                        _set_ui(
                            last_riot_http=code,
                            last_error=msg,
                            message="Riot cloud blocked — Live Client still works when in game",
                        )
                        print(
                            f"[LoL spectator] Riot HTTP {code} — continuing for Live Client path only.",
                            flush=True,
                        )
                        continue
                    _set_ui(last_riot_http=code, last_error=msg, message="Stopped (auth)")
                    print(f"[LoL spectator] Riot API auth error HTTP {code} — stopping.", flush=True)
                    return
                if code == 429:
                    _set_ui(last_riot_http=429, message="Riot rate limit — waiting…")
                    print("[LoL spectator] Riot rate limit (429) — waiting 60s.", flush=True)
                    await asyncio.sleep(60)
                    continue
                elif code == 404 or game is None:
                    pass
                elif code != 200 or not game:
                    _set_ui(last_riot_http=code, last_error=f"Unexpected HTTP {code}", message="Riot error")
                    print(f"[LoL spectator] Unexpected Riot response HTTP {code}, skipping.", flush=True)
                else:
                    snap = build_game_snapshot(game)
                    data_src = "riot"

            if snap is None:
                if in_game:
                    was_ever_in_game = True
                in_game = False
                _clear_latest_snapshot()
                if use_lc and use_riot:
                    idle_msg = "No match via Live Client or Riot (lobby, offline, or wrong region)"
                elif use_lc:
                    idle_msg = "No local live game (League closed or not in match yet)"
                else:
                    idle_msg = "No active game (Riot 404 — not in match or wrong region)"
                http_ui = last_http if isinstance(last_http, int) else None
                _set_ui(
                    data_source=None,
                    last_riot_http=http_ui,
                    in_game=False,
                    session_saw_match=was_ever_in_game,
                    between_games=was_ever_in_game,
                    game_id=None,
                    map_queue=None,
                    game_time_mm_ss=None,
                    message=idle_msg,
                )
                continue

            in_game = True
            was_ever_in_game = True
            _set_latest_snapshot(snap, data_src or "unknown")
            gid = snap.get("gameId")
            mq = f"{snap.get('map') or '?'} · {snap.get('queue') or '?'}"
            http_ui = last_http if isinstance(last_http, int) else None
            _set_ui(
                data_source=data_src,
                last_riot_http=http_ui,
                in_game=True,
                session_saw_match=True,
                between_games=False,
                game_id=gid,
                map_queue=mq,
                game_time_mm_ss=snap.get("gameTimeMmSs"),
                message="Live game visible — Luna can comment",
            )
            src_l = data_src or "?"
            print(f"[LoL spectator] Active game via {src_l} (gameId={gid}).", flush=True)
            if not emit_commentary_enabled():
                _set_ui(message="Live game visible — context synced for chat replies")
                continue
            now = time.time()
            if now - last_comment_ts < min_gap:
                continue
            # Skip generation entirely when the user is actively chatting (caller decides)
            if should_emit is not None and not should_emit():
                continue
            print("[LoL spectator] Generating standalone commentary line…", flush=True)

            comment = await asyncio.to_thread(
                generate_luna_comment,
                snap,
                ollama_chat,
                ollama_model,
                build_chat_system,
                build_commentary_system,
            )
            if not comment:
                _set_ui(message="Model returned empty line (check Ollama)")
                print("[LoL spectator] Model returned empty commentary.", flush=True)
                continue
            if re.fullmatch(r"(?is)\s*(?:SKIP|PASS|NONE|NO_COMMENT|NO COMMENT)\s*", comment):
                _set_ui(message="No notable LoL moment this poll (skipped)")
                continue
            last_comment_ts = now
            on_luna_reply(comment)
            _set_ui(last_comment_ts=now, message="Comment queued to studio/VRM speech")
            print(f"[LoL spectator] Queued studio/VRM line ({len(comment)} chars).", flush=True)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            _set_ui(last_error=str(e), message="Observer error — see last_error")
            print(f"[LoL spectator] loop error: {e}", flush=True)
            await asyncio.sleep(60)
