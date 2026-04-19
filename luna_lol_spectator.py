"""
League of Legends live game commentary for Luna.

Two data sources (can combine):

1) **League Live Client Data API** — local ``https://127.0.0.1:2999`` while the game client is open
   on the **same machine** as Luna. No Riot cloud key or PUUID. Set ``RIOT_LOL_USE_LIVE_CLIENT=1``.

2) **Riot cloud** — Spectator v5 + optional Summoner v4 (PUUID → encrypted summoner id). Needs
   ``RIOT_API_KEY`` and PUUID or encrypted id.

If both are enabled, the observer tries the live client first, then falls back to Riot.

Env:
  RIOT_LOL_USE_LIVE_CLIENT — ``1`` to read local Live Client Data (same PC as League)
  RIOT_LOL_LIVE_CLIENT_URL — base URL (default: https://127.0.0.1:2999)
  RIOT_API_KEY — Riot API key (only for cloud path)
  RIOT_LOL_REGION — platform routing host (default: eun1)
  RIOT_LOL_PUUID — Riot PUUID; Summoner v4 resolves to spectator id
  RIOT_LOL_ENCRYPTED_SUMMONER_ID — if set, skip Summoner v4 and call Spectator directly
  RIOT_LOL_IDLE_POLL_SEC — seconds between checks when no active game (default: 300)
  RIOT_LOL_MIN_COMMENT_GAP_SEC — min seconds between Luna lines in one game (default: 40, below poll interval)
  RIOT_LOL_DEBUG — set to 1 for verbose ``[LoL spectator]`` logs every poll
"""

from __future__ import annotations

import asyncio
import json
import os
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


_ui_lock = threading.Lock()
_ui: dict[str, Any] = {}
_latest_lock = threading.Lock()
_latest_snapshot: dict[str, Any] | None = None
_latest_source: str | None = None
_latest_ts: float = 0.0


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
    out: dict[str, Any] = {
        "source": src or snap.get("source") or "unknown",
        "map": snap.get("map"),
        "queue": snap.get("queue"),
        "gameTimeMmSs": snap.get("gameTimeMmSs"),
        "gameType": snap.get("gameType"),
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
    return (
        "\n\n## Live League context\n"
        "The user is currently in a live League match. Use this as situational context for conversation/coaching. "
        "Don't ignore their direct question; blend game awareness naturally.\n"
        f"{txt}"
    )


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
    try:
        mnum = int(gd.get("mapNumber") or 0)
    except (TypeError, ValueError):
        mnum = 0
    if mnum == 11:
        map_display = "Summoner's Rift"
    elif mnum == 12:
        map_display = "Howling Abyss (ARAM)"
    else:
        map_display = (gd.get("mapTerrain") or map_mode or "?").strip() or "?"

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
        "queue": map_mode,
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
) -> str:
    body = json.dumps(snap, indent=2, ensure_ascii=False)[:6000]
    try:
        system = build_chat_system()
    except Exception as ex:
        print(f"[LoL spectator] build_chat_system failed: {ex}", flush=True)
        system = (
            "You are Luna, a warm witty AI companion. The user is in a League of Legends match; "
            "comment briefly on the JSON snapshot."
        )
    prompt = (
        "Here is the current League of Legends match snapshot (Riot cloud and/or local Live Client data).\n"
        "Write **one or two short sentences** of in-character commentary, following the persona in your system message.\n"
        "React to the moment: game clock, map/queue, rosters, bans, and any numeric stats present in the JSON.\n"
        "No markdown fences, no bullet list; plain prose only.\n\n"
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
) -> None:
    """Background task: Live Client (local) and/or Riot cloud; push lines via *on_luna_reply*."""
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
            print(f"[LoL spectator] Active game via {src_l} (gameId={gid}). Generating line…", flush=True)
            now = time.time()
            if now - last_comment_ts < min_gap:
                continue

            comment = await asyncio.to_thread(
                generate_luna_comment, snap, ollama_chat, ollama_model, build_chat_system
            )
            if not comment:
                _set_ui(message="Model returned empty line (check Ollama)")
                print("[LoL spectator] Model returned empty commentary.", flush=True)
                continue
            last_comment_ts = now
            on_luna_reply(comment)
            _set_ui(last_comment_ts=now, message="Commented in hub chat")
            print(f"[LoL spectator] Queued hub chat line ({len(comment)} chars).", flush=True)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            _set_ui(last_error=str(e), message="Observer error — see last_error")
            print(f"[LoL spectator] loop error: {e}", flush=True)
            await asyncio.sleep(60)
