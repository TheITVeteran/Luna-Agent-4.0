"""
Generic translate module for Luna.
Translate text or uploaded audio into English, independent of WhatsApp.
Mount at /translate (Blueprint).
"""
import os
import tempfile
from flask import Blueprint, request, jsonify, send_from_directory

translate_bp = Blueprint("translate", __name__, url_prefix="/translate")


def _get_bot_deps():
    import bot as _bot
    return {
        "whisper_translate": getattr(_bot, "_whisper_translate", None),
        "ollama_chat": getattr(_bot, "ollama_chat", None),
        "OLLAMA_MODEL": getattr(_bot, "OLLAMA_MODEL", "llama3.2"),
    }


@translate_bp.route("/")
def index():
    """Serve the Translate UI."""
    base = os.path.dirname(os.path.abspath(__file__))
    return send_from_directory(base, "translate.html")


@translate_bp.route("/api/text", methods=["POST"])
def api_translate_text():
    """Translate arbitrary text to English."""
    deps = _get_bot_deps()
    ollama = deps.get("ollama_chat")
    model = deps.get("OLLAMA_MODEL") or "llama3.2"
    if not ollama:
        return jsonify({"error": "Translation not available"}), 503
    data = request.get_json(silent=True) or {}
    text = (data.get("text") or data.get("message") or request.form.get("text") or "").strip()
    if not text:
        return jsonify({"error": "Missing text to translate"}), 400
    prompt = (
        "You are a precise translation assistant.\n"
        "Detect the language of the input and translate it into natural, fluent English.\n"
        "Output only the translation, no explanations."
    )
    try:
        out = ollama(text, system=prompt, model=model)
        return jsonify({"translation": (out or "").strip()})
    except Exception as e:
        return jsonify({"error": str(e)[:200]}), 500


@translate_bp.route("/api/audio", methods=["POST"])
def api_translate_audio():
    """Upload audio → translate speech to English.

    Pipeline:
      1) Whisper translate/transcribe to text.
      2) (Optional) Ollama pass to clean up and ensure natural English.
    """
    deps = _get_bot_deps()
    fn = deps.get("whisper_translate")
    ollama = deps.get("ollama_chat")
    model = deps.get("OLLAMA_MODEL") or "llama3.2"
    if not fn:
        return jsonify({"error": "Audio translation not available"}), 503
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
        # First pass: Whisper translate/transcribe.
        text = fn(path)
        if not text:
            return jsonify({"error": "Translation failed"}), 500
        translated = text.strip()
        # Second pass: use Ollama (if available) to enforce clean English translation.
        if ollama:
            try:
                prompt = (
                    "You are a careful translation assistant.\n"
                    "The following text may be partially translated or still in the original language.\n"
                    "Detect the source language and output a natural, fluent English translation only.\n"
                    "No explanations, no quotes, just the translated English text.\n\n"
                    f"Text:\n{translated}"
                )
                out = ollama(prompt, model=model)
                if out and out.strip():
                    translated = out.strip()
            except Exception:
                # Fall back to Whisper output if Ollama pass fails.
                pass
        return jsonify({"translation": translated})
    except Exception as e:
        return jsonify({"error": str(e)[:200]}), 500
    finally:
        try:
            if path and os.path.isfile(path):
                os.unlink(path)
        except Exception:
            pass


@translate_bp.route("/api/status")
def api_status():
    """Translate module status."""
    deps = _get_bot_deps()
    return jsonify({
        "translate": deps.get("ollama_chat") is not None or deps.get("whisper_translate") is not None,
        "text": deps.get("ollama_chat") is not None,
        "audio": deps.get("whisper_translate") is not None,
    })

