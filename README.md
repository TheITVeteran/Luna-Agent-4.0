# Luna 5.0

**Your personal AI companion** — Discord bot, web chat, voice, and automation. One assistant for conversation, commands, and control.

[![Python 3.10+](https://img.shields.io/badge/python-3.10+-blue.svg)](https://www.python.org/downloads/)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://opensource.org/licenses/MIT)

---

## What is Luna?

Luna runs as a **Discord bot** and a **web UI** on your machine. Chat with her, trigger actions with **!commands** or **Shadow**, and use the same memory and profile everywhere. She can play music, send DMs, create Suno songs, comment on YouTube, remind you with a voice note, and more — all powered by **Ollama** (local LLMs).

| Where you are | What you do |
|---------------|-------------|
| **Web UI** (browser) | Chat, quick-action buttons, news, play/podcast, DMs, TranscribeMe |
| **Discord** | Same Luna; `!play`, `!dm`, `!podcast`, reminders, create code, automation |

---

## Features

### Chat & memory
- **4-layer memory** — Core, long-term, short-term, working; survives restarts.
- **Per-user profile** — Name, preferences, goals; synced between Discord and web.
- **Luna brain** — Learns from conversation; no need to say “remember this” every time.

### Commands & automation
- **!news** — World headlines (BBC, NYT, Al Jazeera).
- **!search** — Google search.
- **!suno** &lt;desc&gt; — Create a Suno song (browser automation).
- **!share_song / !share_facebook** — Share to X (Twitter) or Facebook.
- **!yt_comment** &lt;url&gt; — Transcribe video and post an AI comment with real context.
- **!ig_dm**, **!fb_msg**, **!msg** — Instagram DM, Messenger, WhatsApp.
- **!dm** &lt;user&gt; [what to say] — Discord DM; Luna rephrases in her own words.
- **!play** &lt;song/url&gt;, **!podcast** — Music in voice; custom podcast from a folder (e.g. Luna Agent n8n).
- **!pc_vitals**, **!luna_vitals** — PC and Luna process status.
- **!analyze_website** &lt;url&gt; — Summarize a site.
- **Reminders** — “Remind me at 7pm to …” → Discord DM + optional voice note (TTS).
- **Retry** — Say “retry” after a failed command; Luna tries again with different strategies.

### Voice & media
- **TTS** — Server-side speech (gTTS); optional TTS in Discord voice channels.
- **Discord voice** — `!join` / `!leave`; `!play` / `!skip` / `!stop` / `!queue`; **!podcast** for a custom folder.

### TranscribeMe module
- **Web page** at `/transcribeme/` — Transcribe audio, translate WhatsApp voice to English, GPT-style Q&A, reminders from text/voice.

### Identity & config
- **SOUL.md, TOOLS.md, OBJECTIVES.md** in `data/` — Edit or tell Luna to set them.
- **Skills** — `.md` files in `data/skills/` are injected when relevant.
- **Linked user** — `LINKED_DISCORD_USER_ID` in `.env`; that user gets reminders, DMs, and UI-triggered play/podcast.

---

## Quick start

1. **Ollama** — [Install Ollama](https://ollama.ai) and pull a model:
   ```bash
   ollama pull llama3.2:latest
   ```

2. **Discord bot** — [Discord Developer Portal](https://discord.com/developers/applications) → create app → Bot → enable **Message Content Intent** → copy token.

3. **Dependencies**
   ```bash
   pip install -r requirements.txt
   python -m playwright install chromium
   ```
   **FFmpeg** in PATH (for TTS and music).

4. **Config** — Copy `.env.example` to `.env`. Set at least:
   - `DISCORD_TOKEN`
   - `LINKED_DISCORD_USER_ID` (your Discord user ID)

5. **Run**
   ```bash
   python bot.py
   ```
   Web UI opens at **http://127.0.0.1:5050**; Discord bot connects.

---

## Configuration

| Variable | Purpose |
|----------|---------|
| `DISCORD_TOKEN` | Bot token from Developer Portal |
| `LINKED_DISCORD_USER_ID` | User who gets reminders, DMs, and UI play/podcast |
| `DISCORD_ADMIN_ID` | Optional admin override |
| `OLLAMA_BASE_URL` | Default `http://127.0.0.1:11434` |
| `OLLAMA_MODEL` | Default `llama3.2:latest` |
| `CUSTOM_PODCAST_DIR` | Folder for **!podcast** (e.g. `D:\Luna Agent n8n`) |
| Profile dirs | `SUNO_PROFILE_DIR`, `YT_PROFILE_DIR`, `IG_PROFILE_DIR`, etc. for browser sessions |

See **BUTTONS.md** for per-action steps and URLs.

---

## Project layout

| Path | Role |
|------|------|
| `bot.py` | Main entry: Discord bot, Flask app, Luna chat, commands, TTS, automation, music, podcast |
| `luna_memory.py` | 4-layer memory |
| `luna_brain.py` | Digital neuron layer for what to store |
| `luna_conversation.py` | Recent conversation for context |
| `luna_profile.py` | Per-user profile |
| `luna_files.py` | Safe file writes (e.g. agents) |
| `shadow_agent.py` | Shadow command parsing and execution |
| `luna_transcribeme.py` | TranscribeMe blueprint (transcribe, translate, ask, remind) |
| `celine.py` | Utilities |
| `index.html` | Web chat UI |
| `transcribeme.html` | TranscribeMe page |
| `data/` | SOUL.md, TOOLS.md, OBJECTIVES.md, skills, memory, profile, reminders |

---

## License

MIT — see [LICENSE](LICENSE) in the repo.

---

*Luna 5.0 — one assistant, Discord + web, your words and your automation.*
