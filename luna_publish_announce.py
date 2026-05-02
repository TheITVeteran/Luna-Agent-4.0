"""
Poll YouTube Atom feeds and Twitch Helix live status; emit short Discord-ready strings when
there is a new upload or a channel goes live. Twitch go-live metadata is returned for optional
Facebook posts (see ``LUNA_PUBLISH_ANNOUNCE_TWITCH_LIVE_FACEBOOK`` in bot_main).

Configure via bot_main env (`LUNA_PUBLISH_ANNOUNCE_*`). One or more Discord text channels via
`LUNA_PUBLISH_ANNOUNCE_DISCORD_CHANNEL_IDS` (comma-separated). State file avoids duplicate announcements.
"""

from __future__ import annotations

import hashlib
import json
import os
import threading
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET

_state_lock = threading.Lock()


def _load_json(path: str, default: dict) -> dict:
    try:
        if os.path.isfile(path):
            with open(path, encoding="utf-8") as f:
                d = json.load(f)
            return d if isinstance(d, dict) else dict(default)
    except Exception:
        pass
    return dict(default)


def _save_json(path: str, data: dict) -> None:
    try:
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        os.replace(tmp, path)
    except Exception:
        pass


def _parse_atom_latest(xml_text: str) -> tuple[str, str, str] | None:
    """First (newest) YouTube Atom entry -> (video_id, title, url) or None."""
    try:
        root = ET.fromstring(xml_text)
    except Exception:
        return None
    ns = {"a": "http://www.w3.org/2005/Atom", "yt": "http://www.youtube.com/xml/schemas/2015"}
    e = root.find("a:entry", ns)
    if e is None:
        return None
    title = (e.findtext("a:title", default="", namespaces=ns) or "").strip()
    vid = (e.findtext("yt:videoId", default="", namespaces=ns) or "").strip()
    link_el = e.find("a:link[@rel='alternate']", ns)
    link = (link_el.attrib.get("href", "") if link_el is not None else "") or (f"https://youtu.be/{vid}" if vid else "")
    if not vid or not title:
        return None
    return vid, title, link


def _fetch_text(url: str, timeout: float = 20.0) -> str | None:
    try:
        req = urllib.request.Request(
            url,
            headers={"User-Agent": "Mozilla/5.0 (compatible; LunaPublishAnnounce/1.0)"},
        )
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return (r.read() or b"").decode("utf-8", errors="replace")
    except Exception:
        return None


def _normalize_bearer(token: str) -> str:
    t = (token or "").strip()
    if not t:
        return ""
    if t.lower().startswith("oauth:"):
        t = t.split(":", 1)[1].strip()
    if not t.lower().startswith("bearer "):
        t = "Bearer " + t
    return t


def _helix_stream_row(client_id: str, bearer: str, login: str) -> dict | None:
    cid = (client_id or "").strip()
    tok = _normalize_bearer(bearer)
    user = (login or "").strip().lstrip("#").lower()
    if not cid or not tok or not user:
        return None
    try:
        q = urllib.parse.urlencode({"user_login": user})
        req = urllib.request.Request(
            "https://api.twitch.tv/helix/streams?" + q,
            headers={"Client-Id": cid, "Authorization": tok},
            method="GET",
        )
        with urllib.request.urlopen(req, timeout=12) as r:
            data = json.loads(r.read() or b"{}")
        arr = data.get("data")
        if isinstance(arr, list) and arr:
            row = arr[0]
            return row if isinstance(row, dict) else None
    except Exception:
        return None
    return None


def poll_publish_announce(
    *,
    state_path: str,
    youtube_channel_ids: list[str],
    youtube_rss_urls: list[str],
    twitch_logins: list[str],
    twitch_client_id: str,
    twitch_app_token: str,
    skip_twitch: bool = False,
) -> tuple[list[str], list[dict[str, str]]]:
    """
    Single poll tick. Updates state file.

    Returns a tuple ``(discord_messages, twitch_go_live)`` where ``twitch_go_live`` is one dict per
    channel that **just** transitioned offline→live, with keys: login, display_name, title, url, started_at.
    """
    ids = [x.strip() for x in youtube_channel_ids if x.strip()]
    rss_extra = [x.strip() for x in youtube_rss_urls if x.strip()]
    twitch_l = [x.strip().lstrip("#").lower() for x in twitch_logins if x.strip()]

    feeds: list[tuple[str, str]] = []
    for cid in ids:
        c = cid.strip()
        if not c.startswith("UC") or len(c) < 10:
            continue
        key = f"yt_channel:{c}"
        feeds.append((key, f"https://www.youtube.com/feeds/videos.xml?channel_id={urllib.parse.quote(c)}"))
    for i, url in enumerate(rss_extra):
        h = hashlib.sha256(url.encode("utf-8")).hexdigest()[:16]
        key = f"yt_rss:{i}:{h}"
        feeds.append((key, url))

    messages: list[str] = []
    twitch_go_live: list[dict[str, str]] = []

    with _state_lock:
        state = _load_json(
            state_path,
            {"youtube": {}, "twitch": {}},
        )
        yt_state = state["youtube"] if isinstance(state.get("youtube"), dict) else {}
        tw_state = state["twitch"] if isinstance(state.get("twitch"), dict) else {}
        state["youtube"] = yt_state
        state["twitch"] = tw_state
        ann_ids = state.get("yt_announced_ids")
        if not isinstance(ann_ids, list):
            ann_ids = []
        ann_set = {str(x).strip() for x in ann_ids if str(x).strip()}

        for key, feed_url in feeds:
            xml = _fetch_text(feed_url)
            if not xml:
                continue
            parsed = _parse_atom_latest(xml)
            if not parsed:
                continue
            vid, title, link = parsed
            prev = yt_state.get(key) if isinstance(yt_state.get(key), dict) else {}
            prev_vid = str(prev.get("video_id") or "").strip()
            if not prev_vid:
                yt_state[key] = {"video_id": vid}
                continue
            if vid != prev_vid:
                yt_state[key] = {"video_id": vid}
                if vid in ann_set:
                    continue
                hint = key.replace("yt_channel:", "") if key.startswith("yt_channel:") else "RSS feed"
                messages.append(
                    f"📺 **New YouTube video** ({hint})\n**{title[:240]}**\n{link}"
                )
                ann_ids.append(vid)
                ann_set.add(vid)

        state["yt_announced_ids"] = ann_ids[-80:]

        if not skip_twitch:
            for login in twitch_l:
                row = _helix_stream_row(twitch_client_id, twitch_app_token, login)
                live_now = row is not None
                prev = tw_state.get(login) if isinstance(tw_state.get(login), dict) else {}
                was_live = bool(prev.get("live"))
                if not prev:
                    tw_state[login] = {"live": live_now}
                    continue
                tw_state[login] = {"live": live_now}
                if live_now and not was_live:
                    disp = str(row.get("user_name") or login).strip() or login
                    ttl = str(row.get("title") or "Live").strip() or "Live"
                    url = f"https://www.twitch.tv/{urllib.parse.quote(login)}"
                    started = str(row.get("started_at") or "")
                    messages.append(f"🔴 **{disp}** is **live** on Twitch!\n**{ttl[:240]}**\n{url}")
                    twitch_go_live.append(
                        {
                            "login": login,
                            "display_name": disp,
                            "title": ttl,
                            "url": url,
                            "started_at": started,
                        }
                    )

        _save_json(state_path, state)

    return messages, twitch_go_live
