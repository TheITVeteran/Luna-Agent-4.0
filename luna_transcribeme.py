"""
TranscribeMe-style module for Luna.
Clone of https://transcribeme.app/ — voice to text, translation, ask (GPT-style), reminders.
Mount at /transcribeme (Blueprint).
"""
import os
import re
import tempfile
import time
from datetime import datetime, timedelta
from flask import Blueprint, request, jsonify, send_from_directory

transcribeme_bp = Blueprint("transcribeme", __name__, url_prefix="/transcribeme")

# Lazy imports from bot to avoid circular import
def _get_bot_deps():
    import bot as _bot
    return {
        "whisper_transcribe": getattr(_bot, "_whisper_transcribe", None),
        "ollama_chat": getattr(_bot, "ollama_chat", None),
        "add_reminder": getattr(_bot, "add_reminder", None),
        "_parse_time": getattr(_bot, "_parse_time", None),
        "LINKED_ID": getattr(_bot, "LINKED_ID", None),
        "OLLAMA_MODEL": getattr(_bot, "OLLAMA_MODEL", "llama3.2"),
    }


def _parse_remind_in_minutes(text: str) -> tuple[str, str] | None:
    """Parse 'remind me to X in N minutes/hours' or 'X in N hours/minutes'. Returns (message, time_str) or None."""
    text = (text or "").strip()
    message = None
    delta_min = None
    # "remind me to call mom in 30 minutes" / "recordame llamar a mamá en 30 minutos"
    m = re.search(
        r"(?:remind\s+me\s+to\s+|recordame\s+|recordar\s+)(.+?)\s+in\s+(\d+)\s*(?:minutes?|mins?|min)\b",
        text,
        re.I | re.DOTALL,
    )
    if m:
        message = m.group(1).strip()
        try:
            delta_min = int(m.group(2))
        except ValueError:
            pass
    if not m:
        m = re.search(
            r"(?:remind\s+me\s+to\s+|recordame\s+|recordar\s+)(.+?)\s+en\s+(\d+)\s*(?:minutos?|min)\b",
            text,
            re.I | re.DOTALL,
        )
        if m:
            message = m.group(1).strip()
            try:
                delta_min = int(m.group(2))
            except ValueError:
                pass
    # "in N hours" (English or Spanish)
    if not m:
        m = re.search(
            r"(?:remind\s+me\s+to\s+|recordame\s+|recordar\s+)(.+?)\s+in\s+(\d+)\s*(?:hours?|hrs?|h)\b",
            text,
            re.I | re.DOTALL,
        )
        if m:
            message = m.group(1).strip()
            try:
                delta_min = int(m.group(2)) * 60
            except ValueError:
                pass
    if not m:
        m = re.search(
            r"(?:remind\s+me\s+to\s+|recordame\s+|recordar\s+)(.+?)\s+en\s+(\d+)\s*(?:horas?|hrs?|h)\b",
            text,
            re.I | re.DOTALL,
        )
        if m:
            message = m.group(1).strip()
            try:
                delta_min = int(m.group(2)) * 60
            except ValueError:
                pass
    # Looser: "[task] in N minutes" or "[task] in N hours" (no "remind me to" prefix)
    if not m:
        m = re.search(r"^(.+?)\s+in\s+(\d+)\s*(?:minutes?|mins?|min)\s*$", text, re.I | re.DOTALL)
        if m:
            message = m.group(1).strip()
            try:
                delta_min = int(m.group(2))
            except ValueError:
                pass
    if not m:
        m = re.search(r"^(.+?)\s+in\s+(\d+)\s*(?:hours?|hrs?|h)\s*$", text, re.I | re.DOTALL)
        if m:
            message = m.group(1).strip()
            try:
                delta_min = int(m.group(2)) * 60
            except ValueError:
                pass
    if message is None or delta_min is None:
        return None
    message = message[:500]
    if delta_min <= 0 or delta_min > 60 * 24 * 7:  # max 1 week
        return None
    when = datetime.now() + timedelta(minutes=delta_min)
    time_str = when.strftime("%H:%M")
    return message, time_str


@transcribeme_bp.route("/")
def index():
    """Serve the TranscribeMe-style UI."""
    base = os.path.dirname(os.path.abspath(__file__))
    return send_from_directory(base, "transcribeme.html")


@transcribeme_bp.route("/api/transcribe", methods=["POST"])
def api_transcribe():
    """Upload audio → transcribe to text (original language)."""
    deps = _get_bot_deps()
    fn = deps.get("whisper_transcribe")
    if not fn:
        return jsonify({"error": "Transcription not available"}), 503
    path = None
    try:
        if request.files:
            f = request.files.get("file") or request.files.get("audio")
            if f and f.filename:
                ext = os.path.splitext(f.filename)[1] or ".ogg"
                fd, path = tempfile.mkstemp(suffix=ext)
                try:
                    f.save(path)
                finally:
                    try:
                        os.close(fd)
                    except Exception:
                        pass
        if not path:
            raw = request.get_data()
            if not raw or len(raw) < 100:
                return jsonify({"error": "No audio: send file or raw body"}), 400
            fd, path = tempfile.mkstemp(suffix=".ogg")
            os.write(fd, raw)
            os.close(fd)
        text = fn(path)
        if text is None:
            return jsonify({"error": "Transcription failed"}), 500
        return jsonify({"text": text})
    except Exception as e:
        return jsonify({"error": str(e)[:200]}), 500
    finally:
        try:
            if path and os.path.isfile(path):
                os.unlink(path)
        except Exception:
            pass


@transcribeme_bp.route("/api/ask", methods=["POST"])
def api_ask():
    """Ask a question (GPT-style) → get answer."""
    deps = _get_bot_deps()
    ollama = deps.get("ollama_chat")
    model = deps.get("OLLAMA_MODEL") or "llama3.2"
    if not ollama:
        return jsonify({"error": "Ask not available"}), 503
    try:
        data = request.get_json(silent=True) or {}
        question = (data.get("question") or data.get("q") or request.form.get("question") or request.form.get("q") or "").strip()
        if not question:
            return jsonify({"error": "Missing question"}), 400
        answer = ollama(question, system="Answer concisely and helpfully.", model=model)
        return jsonify({"answer": (answer or "").strip()})
    except Exception as e:
        return jsonify({"error": str(e)[:200]}), 500


@transcribeme_bp.route("/api/remind", methods=["POST"])
def api_remind():
    """Parse 'remind me to X in N minutes' (text or voice transcript) and create reminder."""
    deps = _get_bot_deps()
    add_reminder = deps.get("add_reminder")
    linked_id = deps.get("LINKED_ID")
    if not add_reminder or not linked_id:
        return jsonify({"error": "Reminders require LINKED_DISCORD_USER_ID in .env"}), 503
    try:
        data = request.get_json(silent=True) or {}
        text = (data.get("text") or data.get("message") or request.form.get("text") or request.form.get("message") or "").strip()
        if not text:
            return jsonify({"error": "Missing text (e.g. 'remind me to call mom in 30 minutes')"}), 400
        parsed = _parse_remind_in_minutes(text)
        if not parsed:
            # Fallback: try clock time "remind me at 7pm to X"
            import bot as _bot
            m = re.search(r"remind\s+me\s+at\s+(\S+)\s+to\s+(.+)", text, re.I | re.S)
            if m:
                time_str = _bot._parse_time(m.group(1).strip())
                if time_str:
                    add_reminder(time_str, m.group(2).strip()[:500], linked_id)
                    return jsonify({"ok": True, "message": f"Reminder set for {time_str}"})
            return jsonify({"error": "Use: '[task] in [N] minutes/hours' or 'remind me at 7pm to [task]'"}), 400
        message, time_str = parsed
        add_reminder(time_str, message, linked_id)
        return jsonify({"ok": True, "message": f"Reminder set for {time_str} ({message[:50]}…)"})
    except Exception as e:
        return jsonify({"error": str(e)[:200]}), 500


@transcribeme_bp.route("/api/status")
def api_status():
    """TranscribeMe module status."""
    deps = _get_bot_deps()
    return jsonify({
        "transcribeme": "ok",
        "transcribe": deps.get("whisper_transcribe") is not None,
        "ask": deps.get("ollama_chat") is not None,
        "remind": deps.get("add_reminder") is not None and deps.get("LINKED_ID") is not None,
    })
