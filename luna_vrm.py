"""
VRM avatar viewer for Luna — Three.js r180 + @pixiv/three-vrm (see https://github.com/pixiv/three-vrm ).

Animations (default layout next to this repo):
  - Idle body: LUNA_ANIMATIONS_DIR (default: <project>/Luna animations/) — .vrma / .fbx in that folder only (not subfolders).
  - Talking body: <Luna animations>/talking/ (or LUNA_TALKING_DIR).
  - Expression VRMA: LUNA_EXPRESSIONS_DIR or <Luna animations>/expressions/.
  - Idle sequence (viewer + podcast): <Luna animations>/fbx animations/ — ``.fbx`` and ``.vrma`` ordered by filename (one at a time, looped). Override with LUNA_FBX_ANIMATIONS_DIR.
  - Legacy body: data/vrm/animations/, data/vrm/idle.vrma

Recording your own face for clips (offline, then export VRMA):
  - Capture: iPhone ARKit (Live Link Face) → Unity + UniVRM, webcam + OpenSeeFace / VSeeFace, or VRoid Studio tools.
  - Export expression clips to Luna animations/expressions/ (or set LUNA_EXPRESSIONS_DIR).

Set LUNA_VRM_PATH for the .vrm model. VRMA files are VRM Animation format (convert pipeline for Mixamo FBX → VRMA as needed).

Body-motion .vrma with almost no joint movement (T-pose / frozen) are omitted unless LUNA_VRMA_MIN_MOTION_RAD=0 (default min peak delta ~0.035 rad between keyframes).

Mount at /vrm (Blueprint).
"""
from __future__ import annotations

import asyncio
import json
import math
import os
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


def _talking_dir() -> str:
    """Talking body clips: LUNA_TALKING_DIR or <Luna animations>/talking/."""
    env = (os.environ.get("LUNA_TALKING_DIR") or "").strip()
    if env:
        return os.path.abspath(os.path.expanduser(env))
    return os.path.join(_luna_animations_root(), "talking")


def _fbx_animations_dir() -> str:
    """Ordered idle sequence: LUNA_FBX_ANIMATIONS_DIR or ``<Luna animations>/fbx animations``."""
    env = (os.environ.get("LUNA_FBX_ANIMATIONS_DIR") or "").strip()
    if env:
        return os.path.abspath(os.path.expanduser(env))
    return os.path.join(_luna_animations_root(), "fbx animations")


def idle_sequence_folder_paths_ordered() -> list[str]:
    """``.fbx`` and ``.vrma`` under ``fbx animations/``, sorted by filename (case-insensitive)."""
    d = _fbx_animations_dir()
    if not os.path.isdir(d):
        return []
    names = sorted(
        [f for f in os.listdir(d) if f.lower().endswith((".fbx", ".vrma"))],
        key=str.lower,
    )
    out: list[str] = []
    for fn in names:
        p = os.path.join(d, fn)
        if os.path.isfile(p):
            out.append(os.path.abspath(p))
    return out


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


def _min_body_motion_radians() -> float:
    """Skip VRMA that barely move (look like T-pose / frozen). Set LUNA_VRMA_MIN_MOTION_RAD=0 to disable."""
    raw = (os.environ.get("LUNA_VRMA_MIN_MOTION_RAD") or "").strip()
    if raw.lower() in ("0", "0.0", "off", "false", "no"):
        return 0.0
    try:
        return float(raw) if raw else 0.035
    except ValueError:
        return 0.035


def _vrma_passes_body_motion_minimum(path: str) -> bool:
    """
    Exclude .vrma with no per-frame joint motion (static pose clips) or peak motion below threshold
    (consecutive-keyframe max rotation delta across channels).
    """
    if not path.lower().endswith(".vrma"):
        return True
    stats = _vrma_motion_stats(path)
    if not stats:
        return True
    if stats.get("static_pose"):
        return False
    min_rad = _min_body_motion_radians()
    if min_rad <= 0.0:
        return True
    peak = float(stats.get("max_rotation_delta_rad") or 0.0)
    return peak >= min_rad


def _filter_body_vrma_paths(paths: list[str]) -> list[str]:
    out = [p for p in paths if _vrma_passes_body_motion_minimum(p)]
    if not out and paths:
        print(
            "[Luna VRM] All .vrma were below LUNA_VRMA_MIN_MOTION_RAD; using full list. "
            "Set LUNA_VRMA_MIN_MOTION_RAD=0 to always include low-motion clips.",
        )
        return paths
    return out


def idle_vrma_paths() -> list[str]:
    """Idle body: .vrma in Luna animations root only + legacy data paths (not talking/, expressions/, etc.)."""
    base = _project_base()
    chunks: list[str] = []
    envp = (os.environ.get("LUNA_VRMA_IDLE_PATH") or "").strip()
    if envp:
        p = os.path.abspath(os.path.expanduser(envp))
        if os.path.isfile(p) and p.lower().endswith(".vrma"):
            chunks.append(p)
    root = _luna_animations_root()
    chunks.extend(_list_vrma_files_in_dir(root))
    chunks.extend(_list_vrma_files_in_dir(os.path.join(base, "data", "vrm", "animations")))
    idle_legacy = os.path.join(base, "data", "vrm", "idle.vrma")
    if os.path.isfile(idle_legacy):
        chunks.append(os.path.abspath(idle_legacy))
    chunks = _unique_paths(chunks)
    return _filter_body_vrma_paths(chunks)


def talk_vrma_paths() -> list[str]:
    """Talking body: .vrma in talking/ only."""
    chunks = _list_vrma_files_in_dir(_talking_dir())
    return _filter_body_vrma_paths(_unique_paths(chunks))


def motion_vrma_paths() -> list[str]:
    """All body VRMA (idle + talking); for legacy routes."""
    return _unique_paths(idle_vrma_paths() + talk_vrma_paths())


def idle_fbx_paths() -> list[str]:
    """Idle body FBX in Luna animations root only."""
    root = _luna_animations_root()
    return _unique_paths(_list_fbx_files_in_dir(root))


def talk_fbx_paths() -> list[str]:
    """Talking body FBX in talking/."""
    return _unique_paths(_list_fbx_files_in_dir(_talking_dir()))


def motion_fbx_paths() -> list[str]:
    """All body FBX (idle + talking)."""
    return _unique_paths(idle_fbx_paths() + talk_fbx_paths())


def expression_vrma_paths() -> list[str]:
    """Expression / face VRMA in expressions/ only."""
    return _unique_paths(_list_vrma_files_in_dir(_expressions_dir()))


def _animation_paths_ordered() -> list[str]:
    """Backward-compatible name: same as motion list."""
    return motion_vrma_paths()


def _label_for_vrma_path(path: str) -> str:
    stem = os.path.splitext(os.path.basename(path))[0]
    return stem.replace("_", " ").replace("-", " ").strip().title() or "Animation"


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
    paths = idle_vrma_paths()
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
    """Legacy: first idle VRMA."""
    paths = idle_vrma_paths()
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


@vrm_bp.route("/animation/idle/vrma/<int:index>")
def serve_idle_vrma(index: int):
    paths = idle_vrma_paths()
    if index < 0 or index >= len(paths):
        return jsonify({"error": "Invalid idle VRMA index", "count": len(paths)}), 404
    return _send_vrma_file(paths[index])


@vrm_bp.route("/animation/idle/fbx/<int:index>")
def serve_idle_fbx(index: int):
    paths = idle_fbx_paths()
    if index < 0 or index >= len(paths):
        return jsonify({"error": "Invalid idle FBX index", "count": len(paths)}), 404
    return _send_fbx_file(paths[index])


@vrm_bp.route("/animation/talking/vrma/<int:index>")
def serve_talking_vrma(index: int):
    paths = talk_vrma_paths()
    if index < 0 or index >= len(paths):
        return jsonify({"error": "Invalid talking VRMA index", "count": len(paths)}), 404
    return _send_vrma_file(paths[index])


@vrm_bp.route("/animation/talking/fbx/<int:index>")
def serve_talking_fbx(index: int):
    paths = talk_fbx_paths()
    if index < 0 or index >= len(paths):
        return jsonify({"error": "Invalid talking FBX index", "count": len(paths)}), 404
    return _send_fbx_file(paths[index])


@vrm_bp.route("/animation/motion/<int:index>")
def serve_vrma_motion(index: int):
    paths = idle_vrma_paths()
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


@vrm_bp.route("/animation/idle/sequence/<int:index>")
def serve_idle_sequence(index: int):
    """Single ordered list: ``fbx animations/*.fbx`` and ``*.vrma`` (see ``idle_sequence_folder_paths_ordered``)."""
    paths = idle_sequence_folder_paths_ordered()
    if index < 0 or index >= len(paths):
        return jsonify({"error": "Invalid idle sequence index", "count": len(paths)}), 404
    path = paths[index]
    if path.lower().endswith(".fbx"):
        return _send_fbx_file(path)
    return _send_vrma_file(path)


@vrm_bp.route("/animation/<int:index>")
def serve_vrma_by_index(index: int):
    """Legacy alias: idle VRMA index (same as /animation/motion/<index>)."""
    paths = idle_vrma_paths()
    if index < 0 or index >= len(paths):
        return jsonify({"error": "Invalid animation index", "count": len(paths)}), 404
    return _send_vrma_file(paths[index])


def _build_motion_pool_items(vrma_paths: list[str], fbx_paths: list[str], pool: str) -> list[dict[str, Any]]:
    """One pool (idle or talk): VRMA + FBX rows sorted by stem."""
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
    items: list[dict[str, Any]] = []
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
            "pool": pool,
            "role": pool,
        }
        if kind == "vrma":
            stats = _vrma_motion_stats(p)
            if stats:
                entry["vrma_stats"] = stats
                if stats.get("static_pose"):
                    entry["label"] = f"{label} (static pose)"
        items.append(entry)
    return items


def _expression_items_for_api() -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for i, p in enumerate(expression_vrma_paths()):
        base = os.path.basename(p)
        stem = os.path.splitext(base)[0]
        out.append(
            {
                "kind": "vrma",
                "index": i,
                "label": _label_for_vrma_path(p),
                "name": base,
                "stem": stem,
                "pool": "expression",
                "role": "expression",
            }
        )
    return out


def _idle_sequence_items_for_api() -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for i, p in enumerate(idle_sequence_folder_paths_ordered()):
        base = os.path.basename(p)
        stem = os.path.splitext(base)[0]
        low = base.lower()
        kind: str = "fbx" if low.endswith(".fbx") else "vrma"
        label = _label_for_anim_path(p)
        if kind == "fbx":
            label = f"{label} (FBX · idle seq)"
        else:
            label = f"{label} (VRMA · idle seq)"
        out.append(
            {
                "kind": kind,
                "index": i,
                "label": label,
                "name": base,
                "stem": stem,
                "pool": "idle_sequence",
                "role": "idle_sequence",
            }
        )
    return out


@vrm_bp.route("/api/animations")
def api_animations():
    """Idle / talking / expression pools; legacy `motion` = idle + talk; `local` = all body VRMA paths."""
    vrma_only = motion_vrma_paths()
    motion_idle = _build_motion_pool_items(idle_vrma_paths(), idle_fbx_paths(), "idle")
    motion_talk = _build_motion_pool_items(talk_vrma_paths(), talk_fbx_paths(), "talk")
    motion_expression = _expression_items_for_api()
    motion_idle_sequence = _idle_sequence_items_for_api()
    motion_items = motion_idle + motion_talk
    expression_items = motion_expression
    return jsonify(
        {
            "motion": motion_items,
            "motion_idle": motion_idle,
            "motion_idle_sequence": motion_idle_sequence,
            "motion_talk": motion_talk,
            "motion_expression": motion_expression,
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
            "talking_root": _talking_dir(),
            "expressions_root": _expressions_dir(),
            "fbx_animations_root": _fbx_animations_dir(),
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
    idle_v = idle_vrma_paths()
    vrma_path = idle_v[0] if idle_v else None
    edge_ok = _edge_tts_enabled() and _edge_tts_import_ok()
    _chat = (_env_str("OLLAMA_CHAT_MODEL") or _env_str("OLLAMA_MODEL") or "llama3.2:latest").strip() or "llama3.2:latest"
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
            "talking_root": _talking_dir(),
            "expressions_root": _expressions_dir(),
            "edge_tts": edge_ok,
            "edge_tts_voice": _edge_tts_voice_default() if edge_ok else "",
            "chat_model": _chat,
        }
    )


def _is_gemma4_family_model_id(model_id: str) -> bool:
    m = (model_id or "").strip().lower().replace(" ", "")
    if not m:
        return False
    if m.startswith("gemma4"):
        return True
    return "gemma-4" in m or "gemma4" in m


def _strip_emojis_for_edge_tts(text: str) -> str:
    if not text:
        return text
    out: list[str] = []
    for ch in text:
        o = ord(ch)
        if o in (0x200D, 0xFE0F, 0x20E3):
            continue
        if 0x1F3FB <= o <= 0x1F3FF:
            continue
        if (
            0x1F300 <= o <= 0x1FAFF
            or 0x2600 <= o <= 0x26FF
            or 0x2700 <= o <= 0x27BF
            or 0x1F600 <= o <= 0x1F64F
            or 0x1F680 <= o <= 0x1F6FF
            or 0x1F1E6 <= o <= 0x1F1FF
        ):
            continue
        out.append(ch)
    s = "".join(out)
    return " ".join(s.split()).strip()


@vrm_bp.route("/api/tts", methods=["POST"])
def api_tts():
    """Synthesize speech with Edge TTS (e.g. Ava); returns MP3 for the VRM viewer Web Audio path."""
    data = request.get_json(force=True, silent=True) or {}
    text = (data.get("text") or "").strip()
    chat = (_env_str("OLLAMA_CHAT_MODEL") or _env_str("OLLAMA_MODEL") or "").strip()
    if _is_gemma4_family_model_id(chat):
        text = _strip_emojis_for_edge_tts(text)
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
