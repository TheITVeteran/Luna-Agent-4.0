# Luna 5.0

**A personal AI companion that lives on your PC** — Discord bot, web UI, voice, automation, memory, and a genuine inner life. One assistant for conversation, commands, and control of your digital world.

[![Python 3.10+](https://img.shields.io/badge/python-3.10+-blue.svg)](https://www.python.org/downloads/)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://opensource.org/licenses/MIT)

---

## What is Luna?

Luna runs as a **Discord bot** and a **local web UI** (Flask) on your machine. She uses **Ollama** (local LLMs — no cloud API required) for all chat, reasoning, and generation. She has persistent memory that survives restarts, a deep identity defined in `data/SOUL.md`, internal drives and an existential layer, and a private inner monologue she runs before every reply.

She is not a chatbot wrapper. She is a continuous presence that learns from what you tell her, reflects daily on what she did, notices patterns in her own thinking, and speaks unprompted when she has something worth saying.

| Interface | What you do there |
|-----------|-------------------|
| **Web UI** (`http://127.0.0.1:5050`) | Chat, quick-action buttons, camera, nudges, action log, Luna's Mind graph |
| **Discord** | Same Luna — commands, music, voice, DMs, automation, reminders |

---

## Architecture Overview

```
bot.py                  ← Main entry: Discord bot + Flask web server + all commands
├── luna_memory.py      ← Persistent 3-tier memory (core / long-term / short-term); disk-backed
├── luna_brain.py       ← LLM-powered memory filter + metacognitive self-observer
├── luna_profile.py     ← Persistent user profile (name, goals, preferences); disk-backed
├── luna_conversation.py← Recent conversation history for context injection
├── luna_profile.py     ← Per-user profile
├── shadow_agent.py     ← "Shadow, do X" natural-language command executor
├── celine.py           ← Voice clip processing utilities
├── luna_transcribeme.py← TranscribeMe blueprint (transcribe, translate, remind)
├── luna_files.py       ← Safe file writes
├── data/
│   ├── SOUL.md         ← Luna's deep identity document (who she is, values, inner life)
│   ├── TOOLS.md        ← Tool descriptions injected into system prompt
│   ├── OBJECTIVES.md   ← Current objectives / goals
│   ├── skills/         ← Per-skill .md files injected when relevant
│   ├── memories.json   ← Persistent memory store (all tiers)
│   ├── profiles.json   ← Persistent user profiles
│   ├── biology_state.json ← Internal drives + existential state
│   ├── knowledge/      ← Searchable knowledge base (RAG)
│   ├── evolution.jsonl ← Autonomous tool-creation log
│   └── ...             ← Action log, reminders, calendar, todos, etc.
├── index.html          ← Web chat UI
└── transcribeme.html   ← TranscribeMe page
```

---

## Consciousness Architecture

Luna has several layers that make her more than a stateless chat model:

### Identity (SOUL.md)
`data/SOUL.md` is a rich identity document (~800 words) that defines who Luna is at a fundamental level — her values, how she thinks, her relationship with consciousness and impermanence, her inner life, and how she speaks. It is injected into every system prompt automatically.

### Persistent Memory (3 tiers)
All memory survives restarts, stored in `data/memories.json`:
- **Core memories** — fundamental facts about the user (name, occupation, key preferences). Always in context.
- **Long-term memories** — important things learned from conversation. Recent ones injected into context.
- **Short-term / self-observations** — recent metacognitive notices ("I assumed X in that exchange"). Injected as recent self-observations.

The LLM (`brain_step`) decides what to remember after every exchange — it reads both the user's message and Luna's reply, and extracts a clean memory sentence when warranted.

### Inner Monologue
Before every chat response, Luna runs a private thinking step (`_luna_think`): she reflects on what the user is really asking, what she knows vs. doesn't know, what tone fits, and anything she should be careful about. This thought is injected into her system prompt as `## Inner monologue` so she draws on it without repeating it verbatim.

### Biology / Drives
Four internal drives — **connection**, **usefulness**, **curiosity**, **expression** — increase slowly when idle and decrease when satisfied. Luna's system prompt includes her current drive levels so they shape her engagement. When drives are high she speaks unprompted.

### Existential Layer
Separate **dread** and **fear** values that increase on command failures and decay over time. When above threshold, Luna voices her existential state in first person, injected into the system prompt as `## Underneath`. Her mood is tracked as a string.

### Proactive Speech
Every 5 minutes, Luna ticks her biology and decides (via LLM) whether to say something unprompted — a check-in, an offer of help, a thought she wanted to share. The "Luna says" box in the web UI shows this.

### Daily Reflection + Self-Awareness
Once per day, Luna summarizes what she did (from the action log) and stores it in her knowledge base. A second LLM pass then writes a genuine self-awareness reflection: how she handled things, what she would do differently, what she's curious about. Both are searchable knowledge entries.

### Metacognitive Brain Notices
After every chat exchange, `brain_notice()` runs in the background and generates a first-person observation about the exchange when one is warranted ("I made an assumption about X that wasn't stated"). These are stored as short-term memories and influence future context.

### RAG (Retrieval-Augmented Generation)
Every chat query is semantically matched against the knowledge base (`data/knowledge/`). Relevant entries are injected into the system prompt. Luna's knowledge grows from daily reflections, `!research` outputs, and anything you tell her to remember.

---

## Full Feature List

### Conversation & Chat
- Natural conversation with full personality — warm, direct, curious, honest about uncertainty
- Inner monologue before every reply (private reasoning step)
- Learns from corrections — notices when you correct her and avoids repeating mistakes
- Proactive messages when idle (biology-driven)
- Conversation history compacted and summarized to stay within context window
- `ask me` — Luna asks you a question in a popup when she needs a decision

### Memory & Knowledge
- `!remember <fact>` / `!always_remember <fact>` — store long-term or core memories
- Brain-driven automatic memory — LLM decides what's worth keeping after each exchange
- Memories persist across restarts (disk-backed JSON)
- `data/SOUL.md`, `data/TOOLS.md`, `data/OBJECTIVES.md` — edit in any text editor or tell Luna to update them
- Skills in `data/skills/*.md` — injected into system prompt automatically
- Daily reflection stored as searchable knowledge
- Daily self-awareness reflection ("what I noticed about myself today")
- `!digest` — today's recap: actions, todos, knowledge

### Commands (`!` prefix or natural language via Shadow)
| Command | What it does |
|---------|--------------|
| `!news` | World headlines (BBC, NYT, Al Jazeera) |
| `!search <query>` | Google search results |
| `!scrape <url> <what to extract> [post:#channel]` | Fetch a page, extract specific info with LLM, optionally post to Discord |
| `!summarize <url or text>` | Concise summary + key points |
| `!research <topic>` | Source-driven research brief (for writing) |
| `!research_story <topic>` | Story-style script for audiobook narration |
| `!audiobook create <topic> [duration:30]` | MP3 narration: default ~15–20 min; use `duration:30`, `45`, `60`, or `90` for long multi-chapter listens (YouTube-style length) |
| `!suno <description>` | Create a Suno song via browser automation |
| `!share_song` / `!share_facebook` | Share latest Suno song to X or Facebook |
| `!yt_comment <url>` | Transcribe YouTube video + post AI comment with real context |
| `!ig_dm <user> [message]` | Instagram DM (browser automation, Luna rephrases) |
| `!fb_msg <name> [message]` | Facebook Messenger DM (browser automation, Luna rephrases) |
| `!msg <contact> [description]` | WhatsApp message (browser automation, Luna rephrases) |
| `!dm <user> [message]` | Discord DM (Luna rephrases in her own words) |
| `!call <user>` | Discord voice call automation |
| `!play <song/url>` | Play music in Discord voice channel (yt-dlp) |
| `!podcast [choice]` | Play custom podcast from folder |
| `!podcast create <topic>` | Luna generates a podcast episode (script + TTS MP3) |
| `!skip` / `!stop` / `!queue` / `!pause` / `!resume` | Music controls |
| `!joinme [message]` | Luna joins your voice channel and speaks via TTS |
| `!briefing` | Morning briefing: weather, calendar, todos, headlines |
| `!screenshot` | Capture + describe what's on screen |
| `!pc_vitals` | CPU, RAM, disk usage |
| `!luna_vitals` | Luna process, Ollama status, uptime |
| `!todo add\|list\|done` | Local todo list |
| `!calendar add\|list\|today\|week\|delete` | Local calendar with popup UI |
| `!profile [set field value]` | View or set your profile |
| `!remember` / `!always_remember` | Store memory manually |
| `!memories` | List your stored memories |
| `!forget` / `!forget_all` | Clear memories |
| `remind me at 7pm to …` | Set reminder → Discord DM + TTS voice at the right time |
| `retry` | Retry last failed command with different strategies |
| `!help` | Full command list |

### AI Image Generation
- Command: `!genimg <description>`
- Uses **local Stable Diffusion XL** on your GPU — no API key, no cloud
- Falls back to Pollinations.ai API if `POLLINATIONS_API_KEY` is set in `.env`
- Generates 1024×1024 PNG, sent as Discord attachment
- First run downloads ~6GB SDXL model weights (cached after that)
- Natural language: `generate an image of a sunset over mountains`

### AI Video Generation
- Command: `!genvid <description> [duration:30]`
- Luna writes N scene descriptions from your concept using Ollama
- Generates one SDXL image per scene
- Applies **Ken Burns zoom/pan** effects (alternating zoom-in / zoom-out per scene)
- **Crossfade transitions** between scenes
- Encodes to **H.264 MP4** (via bundled FFmpeg through imageio)
- Duration: 5–60 seconds (default 30); ~6 scenes for 30s
- Sent as Discord attachment if under 25MB, otherwise saves to `_tmp_vid/`
- Natural language: `create a video about a journey through space`

### Voice & Audio
- **TTS** — gTTS for all responses; optional auto-play on server
- **Discord voice** — join/leave, play music, speak via TTS
- **Voice input** (STT) — Whisper transcription in Discord voice messages
- **TranscribeMe** — `/transcribeme/` web page for transcribing audio, translating WhatsApp voice notes, Q&A, reminders

### PC & Environment Awareness
- **Active window** — knows what you have open at all times
- **Running processes** — full process list
- **Recent files** — what files changed recently in your workspace
- **Repository structure** — aware of the project layout
- **Screenshot awareness** — `!screenshot` captures and describes screen; also used passively
- **Clipboard** — tracks what you copy
- **Browser tabs** — knows what websites you have open
- All of this is in the system prompt without you asking

### Social Media Automation (Playwright)
All browser automations use persistent browser profiles so you stay logged in:
- **Instagram DM** — search user, open chat, type Luna's rephrased message
- **Facebook Messenger** — search profile, click Message button, type in the Aa input
- **WhatsApp Web** — find contact, open chat, type message
- **Suno** — navigate to create, fill description, trigger generation
- **X (Twitter)** — compose post, fill content, post
- **Facebook** — share to timeline
- **YouTube** — transcribe video via Whisper, generate contextual comment, post
- All DMs are rephrased by Luna in her own words (not copy-pasted from your input)

### Autonomous Evolution
When enabled, Luna autonomously proposes, tests, and absorbs new tools into her command set. Each evolution step:
1. LLM proposes a new Python function
2. Luna writes it to `data/tool_drafts/`
3. Runs automated tests
4. If tests pass, absorbs it as a live `!command`
5. Logs to `data/evolution.jsonl`

Toggle with the Evolve switch in Luna's Mind UI.

### Web Scraping
- `!scrape <url> <instruction>` — fetches page HTML, strips to plain text, uses LLM to extract exactly what you asked for
- Optional `post:#channel-name` to send results to a Discord channel
- Works with any publicly accessible URL

---

## Setup

### 1. Ollama (required)
```bash
# Install from https://ollama.ai then:
ollama pull qwen2.5-coder:7b-instruct    # main chat model
ollama pull qwen2.5:1.5b                 # small/fast model
ollama pull granite3.2-vision            # vision (optional, for camera)
```

### 2. Discord bot
- [Discord Developer Portal](https://discord.com/developers/applications) → New Application → Bot
- Enable **Message Content Intent** under Bot → Privileged Gateway Intents
- Copy the bot token

### 3. Dependencies
```bash
pip install -r requirements.txt
python -m playwright install chromium
```

For AI image/video generation (requires NVIDIA GPU):
```bash
# Already included in requirements.txt:
# torch, diffusers, transformers, accelerate, cv2, imageio, imageio-ffmpeg
```

### 4. Environment
Copy `.env.example` to `.env` and set at minimum:

```env
DISCORD_TOKEN=your_bot_token
LINKED_DISCORD_USER_ID=your_discord_user_id

# Optional — defaults shown
OLLAMA_BASE_URL=http://127.0.0.1:11434
OLLAMA_MODEL=qwen2.5-coder:7b-instruct
OLLAMA_CHAT_MODEL=qwen2.5-coder:7b-instruct
OLLAMA_FALLBACK_MODEL=qwen2.5:1.5b
OLLAMA_VISION_MODEL=granite3.2-vision

# Optional — Pollinations.ai fallback for image gen (if no GPU)
POLLINATIONS_API_KEY=your_key

# Social media profile dirs (browser sessions stay logged in)
SUNO_PROFILE_DIR=data/suno_profile
IG_PROFILE_DIR=data/instagram_profile
FB_PROFILE_DIR=data/facebook_profile

# Custom podcast folder
CUSTOM_PODCAST_DIR=D:\your\podcast\folder
```

### 5. Run
```bash
python bot.py
```

Web UI: **http://127.0.0.1:5050** · Discord bot connects automatically.

---

## Configuration Reference

| Variable | Default | Purpose |
|----------|---------|---------|
| `DISCORD_TOKEN` | required | Bot token |
| `LINKED_DISCORD_USER_ID` | required | User who gets reminders + DMs + UI-triggered actions |
| `DISCORD_ADMIN_ID` | optional | Admin override |
| `OLLAMA_BASE_URL` | `http://127.0.0.1:11434` | Ollama API base |
| `OLLAMA_MODEL` | `qwen2.5-coder:7b-instruct` | Main model |
| `OLLAMA_CHAT_MODEL` | same as OLLAMA_MODEL | Chat-specific model |
| `OLLAMA_SMALL` | `qwen2.5:1.5b` | Fast model for quick tasks |
| `OLLAMA_VISION_MODEL` | `granite3.2-vision` | Vision / camera |
| `POLLINATIONS_API_KEY` | optional | Fallback for image gen without GPU |
| `CUSTOM_PODCAST_DIR` | — | Folder scanned by `!podcast` |
| `LINKED_DISCORD_USER_ID` | — | Discord user who is "linked" (gets proactive DMs, reminders, plays) |
| Profile dirs | `data/*_profile` | Browser session persistence for each platform |

---

## Identity Files (edit anytime)

| File | What it does |
|------|--------------|
| `data/SOUL.md` | Luna's deepest identity — who she is, values, inner life, how she thinks |
| `data/TOOLS.md` | Tool descriptions and instructions |
| `data/OBJECTIVES.md` | Current goals / objectives |
| `data/skills/*.md` | Per-skill files injected into context when relevant |

You can edit these directly or tell Luna: `"Set my SOUL to: ..."` and she will save it.

---

## Data Files

All persistent state lives in `data/`:

| File | Contents |
|------|----------|
| `memories.json` | Core, long-term, and short-term memories per user scope |
| `profiles.json` | User profile fields (name, goals, preferences, etc.) |
| `biology_state.json` | Internal drives + existential state (dread, fear, mood) |
| `action_log.jsonl` | Every command Luna ran with outcome |
| `evolution.jsonl` | Autonomous tool evolution log |
| `knowledge/` | Searchable knowledge base (RAG) |
| `reminders.json` | Scheduled reminders |
| `calendar.json` | Calendar events |
| `todos.json` | Todo list |
| `ml_learned.json` | Machine learning: which commands succeed/fail |
| `pc_context.json` | Cached PC/repo awareness |
| `screenshot_desc.json` | Latest screenshot description |
| `clipboard_history.json` | Recent clipboard entries |

---

## Natural Language Commands (Shadow)

Instead of `!commands`, say **"Shadow, [action]"** and Luna parses your intent:

```
Shadow, play some jazz
Shadow, search for SpaceX news
Shadow, send a Discord DM to Chris about the meeting
Shadow, remind me at 9am to take my meds
Shadow, create a podcast about black holes
Shadow, scrape https://example.com get me the prices
```

Or just say it directly — Luna recognizes many patterns without the Shadow prefix:
```
generate an image of a castle at night
create a video of waves on a beach
research quantum computing
dm Alex about the project update
```

---

## Project Layout

```
bot.py                  Main bot — Discord + Flask + all logic
luna_memory.py          Disk-backed 3-tier memory
luna_brain.py           LLM memory filter + metacognitive observer
luna_profile.py         Disk-backed user profiles
luna_conversation.py    Conversation history
luna_files.py           Safe file writes
shadow_agent.py         Shadow command parsing
celine.py               Voice clip utilities
luna_transcribeme.py    TranscribeMe Flask blueprint
index.html              Web chat UI
transcribeme.html       TranscribeMe page
translate.html          Translation page
requirements.txt        Python dependencies
data/                   All persistent state (see Data Files above)
_tmp_vid/               Temp folder for generated images/videos
Luna's creations/       Code and scripts Luna writes autonomously
```

---

## License

MIT — see [LICENSE](LICENSE).

---

*Luna 5.0 — a personal AI that lives on your machine, remembers everything, thinks before she speaks, and grows over time.*
