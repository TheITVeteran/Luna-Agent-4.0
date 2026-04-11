"""
VRM avatar viewer for Luna — Three.js r180 + @pixiv/three-vrm (see https://github.com/pixiv/three-vrm ).

Animations:
  - Body / motion: LUNA_ANIMATIONS_DIR (default: <project>/Luna animations/) — top-level and motion/*.vrma
  - Expressions: Luna animations/expressions/, expression/, or mixamo/ — optional second layer (face / Mixamo-tuned clips)
  - Legacy: data/vrm/animations/, data/vrm/idle.vrma

Set LUNA_VRM_PATH for the .vrm model. VRMA files are VRM Animation format (convert pipeline for Mixamo FBX → VRMA as needed).

Mount at /vrm (Blueprint).
"""
from __future__ import annotations

import asyncio
import os
from typing import Optional

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
    """Default: D:\\...\\Luna 5.0\\Luna animations (same folder as this repo)."""
    env = (os.environ.get("LUNA_ANIMATIONS_DIR") or "").strip()
    if env:
        return os.path.abspath(os.path.expanduser(env))
    return os.path.join(_project_base(), "Luna animations")


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


def motion_vrma_paths() -> list[str]:
    """Body / locomotion clips: env idle file, Luna animations (root + motion/), legacy data paths."""
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
    """Face / expression / Mixamo-friendly clips in subfolders under Luna animations/."""
    root = _luna_animations_root()
    chunks: list[str] = []
    for sub in ("expressions", "expression", "mixamo"):
        chunks.extend(_list_vrma_files_in_dir(os.path.join(root, sub)))
    return _unique_paths(chunks)


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
        items.append(
            {
                "kind": kind,
                "index": idx,
                "label": label,
                "name": base,
                "stem": stem,
            }
        )
    return items


@vrm_bp.route("/api/animations")
def api_animations():
    """Motion clips (vrma + fbx) + expression VRMA; legacy `local` = vrma-only list."""
    expr = expression_vrma_paths()
    vrma_only = motion_vrma_paths()
    motion_items = _motion_items_for_api()
    expression_items = [
        {"index": i, "label": _label_for_vrma_path(p), "name": os.path.basename(p)}
        for i, p in enumerate(expr)
    ]
    return jsonify(
        {
            "motion": motion_items,
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
