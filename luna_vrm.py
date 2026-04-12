"""
VRM avatar viewer for Luna — Three.js r180 + @pixiv/three-vrm (see https://github.com/pixiv/three-vrm ).

Animations (default layout next to this repo):
  - Body / motion VRMA + FBX: LUNA_ANIMATIONS_DIR (default: <project>/Luna animations/) — files in that folder
    and in motion/ only. Subfolders such as expressions/ are not scanned for body motion.
  - Expression VRMA (face / blendshapes): LUNA_EXPRESSIONS_DIR or, by default,
    <Luna animations>/expressions/ — plus optional legacy folders recordings/, expression/, mixamo/ under LUNA_ANIMATIONS_DIR.
  - Legacy body: data/vrm/animations/, data/vrm/idle.vrma

Recording your own face for clips (offline, then export VRMA):
  - Capture: iPhone ARKit (Live Link Face) → Unity + UniVRM, webcam + OpenSeeFace / VSeeFace, or VRoid Studio tools.
  - Export expression clips to Luna animations/expressions/ (or set LUNA_EXPRESSIONS_DIR).

Set LUNA_VRM_PATH for the .vrm model. VRMA files are VRM Animation format (convert pipeline for Mixamo FBX → VRMA as needed).

Optional motion_roles.json in LUNA_ANIMATIONS_DIR or data/vrm/: map basename to "talk" or "idle" when filenames do not match keyword rules (e.g. VRMA_03.vrma → talk).

Mount at /vrm (Blueprint).
"""
from __future__ import annotations

import asyncio
import json
import math
import os
import re
import struct
from typing import Any, Optional

from flask import Blueprint, Response, jsonify, request, send_file, send_from_directory

vrm_bp = Blueprint("vrm", __name__, url_prefix="/vrm")


def _env_str(key: str, default: str = "") -> str:
    return (os.environ.get(key) or default).strip()


def _edge_tts_enabled() -> bool:
    return _env_str("EDGE_TTS", "1").lower() not in ("0", "false", "no", "off")


def _edge_tts_import_ok() -> bool:
    try:
        import edge_tts  # noqa: F401
    except ImportError:
        return False
    return True


def _edge_tts_voice_default() -> str:
    """Match bot_main: EDGE_TTS_VOICE or Ava Multilingual Neural."""
    return _env_str("EDGE_TTS_VOICE", "en-US-AvaMultilingualNeural") or "en-US-AvaMultilingualNeural"


def _edge_tts_bytes(text: str, voice: str | None = None) -> bytes | None:
    """MP3 bytes via Microsoft Edge online TTS (same stack as Luna’s Discord/UI TTS)."""
    if not _edge_tts_enabled():
        return None
    text = (text or "").strip()
    if not text:
        return None
    text = text[:10000]
    v = (voice or "").strip() or _edge_tts_voice_default()
    try:
        import edge_tts
    except ImportError:
        return None

    async def _stream() -> bytes:
        communicate = edge_tts.Communicate(text, v)
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
        print(f"[Luna VRM] Edge TTS failed: {e}")
        return None


def _project_base() -> str:
    return os.path.dirname(os.path.abspath(__file__))


def _luna_animations_root() -> str:
    """Default: <project>/Luna animations — e.g. D:\\Luna 5.0\\Luna animations when the repo lives there."""
    env = (os.environ.get("LUNA_ANIMATIONS_DIR") or "").strip()
    if env:
        return os.path.abspath(os.path.expanduser(env))
    return os.path.join(_project_base(), "Luna animations")


def _expressions_dir() -> str:
    """Expression VRMA folder: LUNA_EXPRESSIONS_DIR, else <Luna animations>/expressions/."""
    env = (os.environ.get("LUNA_EXPRESSIONS_DIR") or "").strip()
    if env:
        return os.path.abspath(os.path.expanduser(env))
    return os.path.join(_luna_animations_root(), "expressions")


def _list_vrma_files_in_dir(d: str) -> list[str]:
    if not os.path.isdir(d):
        return []
    out: list[str] = []
    for fn in sorted(os.listdir(d), key=str.lower):
        if not fn.lower().endswith(".vrma"):
            continue
        p = os.path.join(d, fn)
        if os.path.isfile(p):
            out.append(os.path.abspath(p))
    return out


def _list_fbx_files_in_dir(d: str) -> list[str]:
    if not os.path.isdir(d):
        return []
    out: list[str] = []
    for fn in sorted(os.listdir(d), key=str.lower):
        if not fn.lower().endswith(".fbx"):
            continue
        p = os.path.join(d, fn)
        if os.path.isfile(p):
            out.append(os.path.abspath(p))
    return out


def _unique_paths(paths: list[str]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for p in paths:
        k = os.path.normcase(os.path.abspath(p))
        if k in seen:
            continue
        seen.add(k)
        out.append(os.path.abspath(p))
    return out


def _read_glb_json_and_bin(path: str) -> tuple[Optional[dict], bytes]:
    """Return (gltf JSON dict, BIN chunk bytes) or (None, b'') if not a readable GLB."""
    try:
        with open(path, "rb") as f:
            if f.read(4) != b"glTF":
                return None, b""
            f.read(8)
            json_len, json_type = struct.unpack("<I4s", f.read(8))
            if json_type != b"JSON":
                return None, b""
            json_bytes = f.read(json_len)
            rest = f.read(8)
            if len(rest) < 8:
                return json.loads(json_bytes.decode("utf-8")), b""
            bin_len, bin_type = struct.unpack("<I4s", rest)
            bin_chunk = f.read(bin_len) if bin_type == b"BIN\x00" else b""
        return json.loads(json_bytes.decode("utf-8")), bin_chunk
    except Exception:
        return None, b""


def _gltf_accessor_items(j: dict, buf: bytes, accessor_index: int) -> list[tuple[float, ...]]:
    accessors = j.get("accessors") or []
    views = j.get("bufferViews") or []
    if accessor_index < 0 or accessor_index >= len(accessors):
        return []
    acc = accessors[accessor_index]
    bv_i = acc.get("bufferView")
    if bv_i is None or bv_i >= len(views):
        return []
    bv = views[bv_i]
    offset = bv.get("byteOffset", 0) + acc.get("byteOffset", 0)
    count = int(acc.get("count") or 0)
    atype = acc.get("type") or "SCALAR"
    elts = {"SCALAR": 1, "VEC2": 2, "VEC3": 3, "VEC4": 4}.get(atype, 1)
    stride = elts * 4
    out: list[tuple[float, ...]] = []
    for i in range(count):
        off = offset + i * stride
        if off + elts * 4 > len(buf):
            break
        tup = struct.unpack_from("<" + "f" * elts, buf, off)
        out.append(tup)
    return out


def _vrma_motion_stats(path: str) -> Optional[dict[str, Any]]:
    """
    Inspect a .vrma (GLB) animation: detect pose-only clips (all rotation keys identical)
    and max joint motion. Used to label clips in /vrm/api/animations.
    """
    if not path.lower().endswith(".vrma") or not os.path.isfile(path):
        return None
    j, buf = _read_glb_json_and_bin(path)
    if not j or not buf:
        return None
    anims = j.get("animations") or []
    if not anims:
        return None
    a0 = anims[0]
    channels = a0.get("channels") or []
    samplers = a0.get("samplers") or []
    rot_ch = 0
    all_const = True
    max_delta = 0.0
    for ch in channels:
        if (ch.get("target") or {}).get("path") != "rotation":
            continue
        si = ch.get("sampler")
        if si is None or si >= len(samplers):
            continue
        samp = samplers[si]
        out_i = samp.get("output")
        if out_i is None:
            continue
        rots = _gltf_accessor_items(j, buf, out_i)
        if len(rots) < 1:
            continue
        rot_ch += 1
        q0 = rots[0]
        for q in rots[1:]:
            if any(abs(a - b) > 1e-7 for a, b in zip(q0, q)):
                all_const = False
                break
        for i in range(1, len(rots)):
            q_a, q_b = rots[i - 1], rots[i]
            dot = abs(sum(a * b for a, b in zip(q_a, q_b)))
            dot = min(1.0, max(-1.0, dot))
            ang = 2.0 * math.acos(dot)
            max_delta = max(max_delta, ang)
    if rot_ch == 0:
        return None
    return {
        "rotation_channels": rot_ch,
        "static_pose": all_const,
        "max_rotation_delta_rad": round(max_delta, 5),
    }


def motion_vrma_paths() -> list[str]:
    """Body / locomotion clips: Luna animations root + motion/ only (not expressions/). Legacy data paths."""
    base = _project_base()
    chunks: list[str] = []
    envp = (os.environ.get("LUNA_VRMA_IDLE_PATH") or "").strip()
    if envp:
        p = os.path.abspath(os.path.expanduser(envp))
        if os.path.isfile(p) and p.lower().endswith(".vrma"):
            chunks.append(p)
    root = _luna_animations_root()
    chunks.extend(_list_vrma_files_in_dir(root))
    chunks.extend(_list_vrma_files_in_dir(os.path.join(root, "motion")))
    chunks.extend(_list_vrma_files_in_dir(os.path.join(base, "data", "vrm", "animations")))
    idle_legacy = os.path.join(base, "data", "vrm", "idle.vrma")
    if os.path.isfile(idle_legacy):
        chunks.append(os.path.abspath(idle_legacy))
    return _unique_paths(chunks)


def motion_fbx_paths() -> list[str]:
    """Mixamo / FBX clips: Luna animations root + motion/ (same folders as VRMA)."""
    root = _luna_animations_root()
    chunks: list[str] = []
    chunks.extend(_list_fbx_files_in_dir(root))
    chunks.extend(_list_fbx_files_in_dir(os.path.join(root, "motion")))
    return _unique_paths(chunks)


def expression_vrma_paths() -> list[str]:
    """Face / expression clips: primary LUNA_EXPRESSIONS_DIR or Luna animations/expressions/, plus legacy subfolders."""
    root = _luna_animations_root()
    chunks: list[str] = []
    chunks.extend(_list_vrma_files_in_dir(_expressions_dir()))
    for sub in ("recordings", "expression", "mixamo"):
        chunks.extend(_list_vrma_files_in_dir(os.path.join(root, sub)))
    return _unique_paths(chunks)


def _animation_paths_ordered() -> list[str]:
    """Backward-compatible name: same as motion list."""
    return motion_vrma_paths()


def _label_for_vrma_path(path: str) -> str:
    stem = os.path.splitext(os.path.basename(path))[0]
    return stem.replace("_", " ").replace("-", " ").strip().title() or "Animation"


# Tokens → talking pool (speech / mouth / dialog cues)
_MOTION_TALK_TOKENS = frozenset(
    {
        "talk",
        "talking",
        "speak",
        "speaking",
        "speech",
        "chat",
        "chats",
        "dialog",
        "dialogs",
        "dialogue",
        "conversation",
        "voice",
        "voices",
        "tts",
        "lipsync",
        "phoneme",
        "phonemes",
        "viseme",
        "visemes",
        "say",
        "saying",
        "narrate",
        "narration",
        "phone",
        "mic",
        "mics",
        "microphone",
        "lip",
        "lips",
    }
)
# Tokens → idle pool (body / pose / locomotion — checked after talk)
_MOTION_IDLE_TOKENS = frozenset(
    {
        "idle",
        "stand",
        "standing",
        "sit",
        "sitting",
        "lay",
        "laying",
        "walk",
        "walking",
        "run",
        "running",
        "jog",
        "jogging",
        "catwalk",
        "bend",
        "bending",
        "crouch",
        "kneel",
        "jump",
        "dance",
        "wave",
        "bow",
        "stretch",
        "yoga",
        "sleep",
        "rest",
        "locomotion",
        "pose",
        "poses",
        "body",
        "motion",
        "motions",
        "loco",
    }
)
# Substrings (lowercase) when tokens are ambiguous e.g. TalkativeClip
_MOTION_TALK_SUBSTRINGS = (
    "talk",
    "speak",
    "speech",
    "chat",
    "dialog",
    "voice",
    "conversation",
    "lipsync",
    "phoneme",
    "viseme",
    "narrat",
    "say",
)
_MOTION_IDLE_SUBSTRINGS = (
    "idle",
    "stand",
    "sit",
    "lay",
    "walk",
    "run",
    "catwalk",
    "bend",
    "crouch",
    "kneel",
    "dance",
    "wave",
    "locomotion",
)

_role_override_path: str | None = None
_role_override_mtime: float = -1.0
_role_override_map: dict[str, str] = {}


def _motion_role_overrides() -> dict[str, str]:
    """Optional Luna animations/motion_roles.json (or data/vrm/motion_roles.json): {\"file.vrma\": \"talk\"|\"idle\"}."""
    global _role_override_path, _role_override_mtime, _role_override_map
    root = _luna_animations_root()
    candidates = [
        os.path.join(root, "motion_roles.json"),
        os.path.join(_project_base(), "data", "vrm", "motion_roles.json"),
    ]
    path = next((p for p in candidates if os.path.isfile(p)), None)
    if path is None:
        _role_override_path = None
        _role_override_mtime = -1.0
        _role_override_map = {}
        return {}
    try:
        mtime = os.path.getmtime(path)
        if path == _role_override_path and mtime == _role_override_mtime:
            return _role_override_map
        with open(path, encoding="utf-8") as f:
            raw = json.load(f)
        out: dict[str, str] = {}
        for k, v in (raw or {}).items():
            if k is None or v is None:
                continue
            kn = os.path.basename(str(k).strip()).lower()
            if not kn:
                continue
            vn = str(v).strip().lower()
            if vn in ("talk", "speaking", "speech"):
                out[kn] = "talk"
            elif vn in ("idle", "body", "locomotion", "pose"):
                out[kn] = "idle"
        _role_override_path = path
        _role_override_mtime = mtime
        _role_override_map = out
        return out
    except Exception:
        return _role_override_map


def _stem_alnum_tokens(stem: str) -> list[str]:
    return re.findall(r"[a-z0-9]+", stem.lower())


def _motion_role_for_path(path: str) -> str:
    """
    Classify a clip for the Talking vs Idle pool: overrides file, then tokens/substrings, else idle.
    Add motion_roles.json to force names like VRMA_03.vrma → talk.
    """
    base = os.path.basename(path)
    base_l = base.lower()
    ov = _motion_role_overrides()
    if base_l in ov:
        return ov[base_l]

    stem = os.path.splitext(base_l)[0]
    stem_spaced = stem.replace("_", " ").replace("-", " ")
    tokens = set(_stem_alnum_tokens(stem_spaced))

    if tokens & _MOTION_TALK_TOKENS:
        return "talk"
    if tokens & _MOTION_IDLE_TOKENS:
        return "idle"

    s = stem_spaced.replace(" ", "")
    for sub in _MOTION_TALK_SUBSTRINGS:
        if sub in s:
            return "talk"
    for sub in _MOTION_IDLE_SUBSTRINGS:
        if sub in s:
            return "idle"

    return "idle"


def _label_for_anim_path(path: str) -> str:
    return _label_for_vrma_path(path)


def _vrm_path() -> str:
    """Resolved path to the VRM file (single allowed file for /vrm/model)."""
    override = (os.environ.get("LUNA_VRM_PATH") or "").strip()
    if override:
        return os.path.abspath(os.path.expanduser(override))
    base = _project_base()
    data_fallback = os.path.join(base, "data", "Luna.vrm")
    if os.path.isfile(data_fallback):
        return os.path.abspath(data_fallback)
    return os.path.abspath(os.path.join(os.path.expanduser("~"), "Downloads", "Luna.vrm"))


def _vrma_idle_path() -> Optional[str]:
    paths = motion_vrma_paths()
    return paths[0] if paths else None


def _send_vrma_file(path: str):
    return send_file(
        path,
        mimetype="model/gltf-binary",
        as_attachment=False,
        download_name=os.path.basename(path),
        max_age=0,
    )


def _send_fbx_file(path: str):
    return send_file(
        path,
        mimetype="application/octet-stream",
        as_attachment=False,
        download_name=os.path.basename(path),
        max_age=0,
    )


@vrm_bp.route("/")
def index():
    """Serve the VRM viewer UI."""
    base = _project_base()
    return send_from_directory(base, "vrm_viewer.html")


@vrm_bp.route("/model")
def serve_vrm():
    """Serve the configured VRM for the viewer (same-origin fetch)."""
    path = _vrm_path()
    if not os.path.isfile(path):
        return (
            jsonify(
                {
                    "error": "VRM file not found",
                    "path": path,
                    "hint": "Set LUNA_VRM_PATH or place data/Luna.vrm next to Luna.",
                }
            ),
            404,
        )
    return _send_vrma_file(path)


@vrm_bp.route("/animation")
def serve_vrma():
    """Legacy: first motion VRMA."""
    paths = motion_vrma_paths()
    if not paths:
        return (
            jsonify(
                {
                    "error": "No local VRMA files",
                    "hint": "Add .vrma under Luna animations/ or LUNA_ANIMATIONS_DIR. See /vrm/api/animations.",
                }
            ),
            404,
        )
    return _send_vrma_file(paths[0])


@vrm_bp.route("/animation/motion/<int:index>")
def serve_vrma_motion(index: int):
    paths = motion_vrma_paths()
    if index < 0 or index >= len(paths):
        return jsonify({"error": "Invalid motion index", "count": len(paths)}), 404
    return _send_vrma_file(paths[index])


@vrm_bp.route("/animation/fbx/<int:index>")
def serve_fbx_motion(index: int):
    paths = motion_fbx_paths()
    if index < 0 or index >= len(paths):
        return jsonify({"error": "Invalid FBX index", "count": len(paths)}), 404
    return _send_fbx_file(paths[index])


@vrm_bp.route("/animation/expression/<int:index>")
def serve_vrma_expression(index: int):
    paths = expression_vrma_paths()
    if index < 0 or index >= len(paths):
        return jsonify({"error": "Invalid expression index", "count": len(paths)}), 404
    return _send_vrma_file(paths[index])


@vrm_bp.route("/animation/<int:index>")
def serve_vrma_by_index(index: int):
    """Legacy alias: motion index (same as /animation/motion/<index>)."""
    paths = motion_vrma_paths()
    if index < 0 or index >= len(paths):
        return jsonify({"error": "Invalid animation index", "count": len(paths)}), 404
    return _send_vrma_file(paths[index])


def _motion_items_for_api() -> list[dict]:
    """Body dropdown: VRMA + Mixamo FBX, sorted by file stem so names match the on-disk files."""
    vrma_paths = motion_vrma_paths()
    fbx_paths = motion_fbx_paths()
    rows: list[tuple[str, int, str]] = []
    for i, p in enumerate(vrma_paths):
        rows.append(("vrma", i, p))
    for i, p in enumerate(fbx_paths):
        rows.append(("fbx", i, p))

    def sort_key(row: tuple[str, int, str]) -> tuple[str, int, int]:
        kind, idx, path = row
        stem = os.path.splitext(os.path.basename(path))[0]
        tie = 0 if kind == "vrma" else 1
        return (stem.lower(), tie, idx)

    rows.sort(key=sort_key)
    items: list[dict] = []
    for kind, idx, p in rows:
        base = os.path.basename(p)
        stem = os.path.splitext(base)[0]
        label = _label_for_anim_path(p)
        if kind == "fbx":
            label = f"{label} (FBX)"
        entry: dict[str, Any] = {
            "kind": kind,
            "index": idx,
            "label": label,
            "name": base,
            "stem": stem,
            "role": _motion_role_for_path(p),
        }
        if kind == "vrma":
            stats = _vrma_motion_stats(p)
            if stats:
                entry["vrma_stats"] = stats
                if stats.get("static_pose"):
                    entry["label"] = f"{label} (static pose)"
        items.append(entry)
    return items


@vrm_bp.route("/api/animations")
def api_animations():
    """Motion clips (vrma + fbx) + expression VRMA; legacy `local` = vrma-only list."""
    expr = expression_vrma_paths()
    vrma_only = motion_vrma_paths()
    motion_items = _motion_items_for_api()
    motion_idle = [x for x in motion_items if x.get("role") == "idle"]
    motion_talk = [x for x in motion_items if x.get("role") == "talk"]
    expression_items = [
        {"index": i, "label": _label_for_vrma_path(p), "name": os.path.basename(p)}
        for i, p in enumerate(expr)
    ]
    return jsonify(
        {
            "motion": motion_items,
            "motion_idle": motion_idle,
            "motion_talk": motion_talk,
            "expression": expression_items,
            "local": [
                {"index": i, "label": _label_for_vrma_path(p), "name": os.path.basename(p)}
                for i, p in enumerate(vrma_only)
            ],
            "motion_count": len(motion_items),
            "expression_count": len(expression_items),
            "local_count": len(vrma_only),
            "fbx_count": len(motion_fbx_paths()),
            "animations_root": _luna_animations_root(),
            "expressions_root": _expressions_dir(),
            "sample": {
                "label": "Sample — Pixiv test.vrma (CDN)",
                "url": "https://raw.githubusercontent.com/pixiv/three-vrm/dev/packages/three-vrm-animation/examples/models/test.vrma",
            },
        }
    )


@vrm_bp.route("/api/status")
def api_status():
    """VRM path and animation file counts."""
    path = _vrm_path()
    motion_v = motion_vrma_paths()
    motion_f = motion_fbx_paths()
    expr = expression_vrma_paths()
    vrma_path = motion_v[0] if motion_v else None
    edge_ok = _edge_tts_enabled() and _edge_tts_import_ok()
    return jsonify(
        {
            "ok": bool(os.path.isfile(path)),
            "path": path,
            "vrma_ok": bool(vrma_path),
            "vrma_path": vrma_path or "",
            "vrma_source": "local" if vrma_path else "remote_fallback",
            "animation_count": len(motion_v) + len(motion_f),
            "motion_count": len(motion_v) + len(motion_f),
            "fbx_count": len(motion_f),
            "expression_count": len(expr),
            "animations_root": _luna_animations_root(),
            "expressions_root": _expressions_dir(),
            "edge_tts": edge_ok,
            "edge_tts_voice": _edge_tts_voice_default() if edge_ok else "",
        }
    )


@vrm_bp.route("/api/tts", methods=["POST"])
def api_tts():
    """Synthesize speech with Edge TTS (e.g. Ava); returns MP3 for the VRM viewer Web Audio path."""
    data = request.get_json(force=True, silent=True) or {}
    text = (data.get("text") or "").strip()
    if not text:
        return jsonify({"error": "Missing or empty text"}), 400
    voice = (data.get("voice") or "").strip() or None
    raw = _edge_tts_bytes(text, voice)
    if not raw:
        return (
            jsonify(
                {
                    "error": "Edge TTS unavailable",
                    "hint": "Install edge-tts (pip install edge-tts), ensure EDGE_TTS is not 0, and check network.",
                }
            ),
            503,
        )
    return Response(raw, mimetype="audio/mpeg")
