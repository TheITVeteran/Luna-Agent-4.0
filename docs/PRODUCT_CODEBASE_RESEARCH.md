# Luna 5.0 — Deep Codebase Research for Productization

**Purpose:** Technical due-diligence document for rebuilding Luna as a **multi-platform, multi-user product** in a new repository.  
**Scope:** Algorithms, features, libraries, data flows, limitations, and alternative technology stacks with ratings.

---

## 1. Executive summary

**Luna 5.0** is a **single-process Python monolith** that combines:

- A **Discord.py** bot (privileged user + optional DM sync).
- A **Flask** HTTP server (static HTML + REST-style JSON APIs + SSE/streaming where implemented).
- **Local-first AI**: Ollama HTTP API (`/api/chat`, `/api/generate`, `/api/embeddings`) and optional **llama-cpp-python** for GGUF chat.
- **Heavy OS integration**: Playwright (non-headless browser profiles), Whisper, OpenCV + YOLO (Ultralytics), screenshots, clipboard, PC vitals, optional wake word.
- **Personal automation**: social web UIs (Suno, X, Facebook, Instagram, WhatsApp Web, Messenger, Discord web), music (yt-dlp + Discord voice), podcasts/audiobooks with TTS.

**Productization gap:** The codebase is optimized for **one operator on one machine** with JSON files under `data/` and hard-coded identity paths. A SaaS or packaged product would require **authentication, tenant isolation, durable queues, secrets management, and stripping or sandboxing OS-level features** per deployment tier.

---

## 2. Repository inventory

| Path | Role |
|------|------|
| `bot.py` | **~9,400+ lines** — main application: config, Discord, Flask, LLM, RAG, biology, calendar, reminders, music, social, evolution, security hooks, etc. |
| `luna_memory.py` | Tiered memory (core / long / short) → `data/memories.json` |
| `luna_profile.py` | Per-scope profile fields → `data/profiles.json` |
| `luna_conversation.py` | **In-memory only** conversation store (not persisted) |
| `luna_brain.py` | Post-reply JSON extraction (remember / notice) via Ollama `generate` + `format: json` |
| `luna_social.py` | Playwright helpers, news feeds, X/FB/IG/WA bootstrap (configured via `configure()` from `bot.py`) |
| `shadow_agent.py` | `strip_shadow_prefix`, `run_shadow` → NL parser + command runner indirection |
| `luna_security.py` | Regex-based static scan of generated Python (network, eval, miners, etc.) |
| `luna_files.py` | Safe writes under repo root |
| `celine.py` | Voice message stub (`process_voice_message`) |
| `luna_transcribeme.py` | Flask blueprint `/transcribeme` |
| `luna_translate.py` | Flask blueprint `/translate` |
| `index.html`, `mind.html`, `evolution.html`, `transcribeme.html`, `translate.html`, `health.html` | Web UIs |
| `sw.js`, `manifest.json` | PWA assets |
| `data/*.json`, `data/*.md`, `data/knowledge/` | Runtime state, identity, knowledge base |
| `requirements.txt`, `requirements-ml.txt` | Dependencies |

**Scale note:** Most business logic lives in **`bot.py`**. Modular extraction is incomplete (`luna_social` is a partial extraction pattern).

---

## 3. High-level architecture

```
┌─────────────────────────────────────────────────────────────────┐
│                        python bot.py                             │
├──────────────┬──────────────────────┬───────────────────────────┤
│  Discord.py  │      Flask app       │   Background asyncio     │
│  on_message  │  /api/*, static      │   loops (reminders,      │
│  commands    │  Blueprints          │   reflection, evolution, │
│              │                      │   embeddings, observers) │
└──────┬───────┴──────────┬───────────┴─────────────┬─────────────┘
       │                  │                         │
       ▼                  ▼                         ▼
  Ollama / GGUF     JSON files in ./data/      Playwright / OS APIs
```

**Concurrency model:**

- **Discord:** `async` handlers; CPU/blocking work pushed with `asyncio.to_thread`.
- **Flask:** synchronous routes; same pattern for heavy calls.
- **Threading:** Locks around shared state (`_biology_lock`, `_GGUF_LOCK`, file locks in modules, music, social browser locks).
- **GGUF:** Singleton `Llama` with lock around `create_chat_completion` (serialized inference).

---

## 4. Configuration & environment

Loaded via **manual `.env` parse** + `python-dotenv` in `bot.py` (order matters for imports: `luna_brain` benefits from dotenv added in-module).

**Representative variables:**

| Area | Examples |
|------|----------|
| Discord | `DISCORD_TOKEN`, `LINKED_DISCORD_USER_ID`, `DISCORD_ADMIN_ID`, `DISCORD_DM_SYNC_USER_IDS`, `DISCORD_TTS_CHANNEL_IDS` |
| LLM | `OLLAMA_BASE_URL`, `OLLAMA_MODEL`, `OLLAMA_CHAT_MODEL`, `OLLAMA_FALLBACK_MODEL`, `OLLAMA_VISION_MODEL`, `LUNA_CHAT_GGUF`, `LUNA_CHAT_GGUF_*` |
| Media | `CUSTOM_PODCAST_DIR`, `EDGE_TTS`, `EDGE_TTS_VOICE`, Fish Audio keys |
| Social | Per-platform `*_PROFILE_DIR`, URLs, handles (many defaults are **user-specific URLs**) |
| Features | Evolution toggle file, security alerts path, etc. |

**Product issue:** Configuration mixes **secrets**, **machine paths**, and **branding** — needs split into **tenant config**, **user secrets**, and **deployment profile**.

---

## 5. Core algorithms & behaviors

### 5.1 Chat pipeline (Luna)

1. **Scope resolution:** `discord:user:{id}` / `discord:{guild}:{author}` / linked scope for primary user.
2. **System prompt build:** `_build_luna_chat_system(scope)`:
   - Base: `LUNA_SYSTEM` + `LUNA_CAPABILITIES`.
   - **Nudges** (user notes).
   - **Biology** drives + existential dread/fear + mood.
   - **Intuition** (cached ~55s): one “felt” sentence via LLM (`get_intuition` / `_gguf_one_shot_user_prompt`).
   - **PC context** (`_get_pc_context`), **screenshot description**, **browser context**, **clipboard**, **corrections**, **learned command stats**, **absorbed tools** list.
   - Optional **existential expression** line injected as `## Underneath`.
3. **Inner monologue:** `_luna_think` → appended as `## Inner monologue` (instructed not to repeat verbatim).
4. **History:** `get_recent_conversation` → `_compact_history`:
   - If message count > `_COMPACT_AT` (20), older thread summarized via `ollama_chat` with `OLLAMA_SMALL`, then replaced with `[Summary]: …` + synthetic “Understood.” + recent tail `_KEEP_RECENT` (12).
5. **Main reply:** `ollama_chat` → `_ollama_chat_once` **or** GGUF path when `model == OLLAMA_CHAT` and `LUNA_CHAT_GGUF` file exists.
6. **Sanitization:** `_sanitize_luna_reply` (regex/heuristic strip of bad openings).
7. **Post-processing:** `append_exchange`, `_capture_memory`, `_capture_profile`, `brain_notice` / `brain_step` in threads.

**Algorithmic complexity:** Mostly **O(n)** over history length; summarization adds **one extra LLM call** periodically.

### 5.2 Retrieval (RAG)

- **Embeddings:** `_ollama_embed` → Ollama `/api/embeddings` (default model `nomic-embed-text`).
- **Similarity:** Cosine similarity (`_cosine_sim`) between query embedding and per-chunk embeddings.
- **Storage:** `data/knowledge_embeddings.json` rebuilt by `_build_knowledge_embeddings` (background loop `_knowledge_embedding_loop`).
- **Search:** `_search_knowledge_semantic(query, top_k)` returns top chunks for injection (used in chat context paths in `bot.py`).

**Note:** File-based embedding index — not incremental vector DB; rebuild semantics are **batch-oriented**.

### 5.3 Natural-language command routing (no LLM)

- **`_parse_command(text)`** — large **regex/heuristic** parser returning `(cmd, params)` or `None`.
- Covers: news, Suno, share X/Facebook, yt_comment, IG DM, search, research, scrape, summarize, digest, todo, calendar, WhatsApp, join VC, DM, remind, create_code, PC/Luna vitals, camera phrases, play/podcast/audiobook, skip/stop, help, etc.
- **`_likely_command`** — fast prefilter to avoid shadow/chat misfires.
- **`_CONV_START`** — suppresses parser for obvious chit-chat starters.

**Pros:** Fast, deterministic, no API cost.  
**Cons:** Fragile for natural language diversity; scaling to many languages/users typically needs **intent classification** (small model or LLM) or **tool-calling** schema.

### 5.4 Shadow agent

- **`strip_shadow_prefix`:** `shadow` / `agent` prefixes.
- **`run_shadow`:** calls `parse_command(rest)` → `_run_cmd` with logging callback.
- **Permission gating:** In Discord, privileged commands restricted to `LINKED_ID` / `ADMIN_ID` for dangerous actions.

### 5.5 Biology / existential state

- **Persistence:** `data/biology_state.json`.
- **`biology_tick`:** Time-based drift of drives; persisted values.
- **`existential_bump`:** Increases dread/fear on failures (elsewhere in code).
- **`_existential_dominant` / `_existential_should_express`:** Threshold gating.
- **`_existential_express`:** One LLM sentence, first person.

**Product angle:** Nice for **personality**; for multi-user SaaS, store **per user** or **per session** with strict caps to avoid unbounded JSON growth.

### 5.6 Reminders & calendar

- **Reminders:** JSON-backed list, `_reminder_loop` asyncio, `_parse_time`, Discord DM + TTS.
- **Calendar:** `_calendar_*` CRUD, `_calendar_notification_loop` for due events.

**Algorithms:** Linear scan for due items; no distributed scheduling (single process).

### 5.7 Proactive & reflection

- **`_proactive_heartbeat_loop`:** Periodic LLM decision whether to nudge user (biology-driven).
- **`_reflection_loop`:** Daily summary + self-awareness pass → knowledge base files.

### 5.8 Evolution (tool absorption)

- Draft tools under controlled paths, approval workflow via API, security scan on write (`luna_security`).

### 5.9 ML “internal learn”

- **`_ml_internal_learn_step` / `_ml_learning_loop`:** Reads action outcomes from JSON (`data/ml_learned.json` pattern), updates simple success/fail tallies used in system prompt (“these commands often work”).

**Optional sklearn:** `requirements-ml.txt` for separate ML tooling — not required for core loop.

### 5.10 Media & TTS

- **gTTS** for inline chat TTS; **Edge TTS** + **Fish Audio** fallback chain for long media (`_tts_bytes_media`).
- **Podcast:** script via LLM → chunk TTS → ffmpeg-style assembly (functions in `bot.py`).
- **Whisper:** local transcription for voice/camera pipelines.

### 5.11 Social automation (Playwright)

- **Pattern:** Persistent Chromium context per **profile directory** (cookies/session).
- **Stealth:** `_STEALTH_INIT_SCRIPT` to reduce automation fingerprints.
- **Bootstrapping:** First-time login windows; `.login_ready` marker files.
- **Per-platform locks** to avoid concurrent browser use.

**Product risk:** Violates many platforms’ ToS for automated control; **official APIs** are the enterprise-safe path.

### 5.12 Security scanning

- **`luna_security.scan_code`:** Line-by-line regex rules (severity levels) for outbound network, eval/exec patterns, miners, etc.
- **Not a sandbox:** It’s static analysis + user alerts, not containment.

---

## 6. Web API surface (Flask)

**Summary:** `bot.py` registers a **`Flask` `web`** app on **`127.0.0.1:5050`** (see `main()`). Optional blueprints: **`luna_transcribeme.transcribeme_bp`** → `/transcribeme`, **`luna_translate.translate_bp`** → `/translate`.

**Auth / rate limiting:** No login. **`/api/chat`** uses a simple **IP rate limit** (`_rate_ok`). Everything else is effectively open on the LAN.

**Detailed catalogs:** **Appendix A** (every `bot.py` route), **Appendix B** (blueprints), **Appendix C** (`_run_cmd_impl`), **Appendix D** (Discord `@bot.command`), **Appendix E** (`data/` files), **Appendix F** (frontend `fetch` map), **Appendix G** (runtime & testing).

---

## 7. Data persistence map

| Store | Format | Multi-user ready? |
|-------|--------|-------------------|
| `memories.json` | JSON dict by scope | Needs DB + encryption |
| `profiles.json` | JSON | Same |
| `biology_state.json` | Single file | Should be per tenant/user |
| `knowledge/`, embeddings | Files | Vector DB candidate |
| `reminders.json`, `calendar.json`, todos | JSON | DB |
| `luna_conversation` | **RAM only** | Must persist for product |
| `SOUL.md`, `TOOLS.md`, skills | Markdown | CMS or templating |

---

## 8. Libraries (declared)

| Library | Use |
|---------|-----|
| `discord.py` | Bot framework |
| `flask` | HTTP server |
| `python-dotenv` | Env |
| `gTTS`, `edge-tts`, `fish-audio-sdk` | TTS |
| `psutil` | PC vitals |
| `playwright` | Browser automation |
| `openai-whisper` | STT |
| `yt-dlp` | Audio download |
| `opencv-python`, `ultralytics` | Camera / detection |
| `mss` | Screenshots |
| `pyperclip` | Clipboard |
| Optional: `llama-cpp-python` | Local GGUF |
| Optional: `scikit-learn`, `numpy` | ML scripts |

**Implicit:** `urllib`, `json`, `threading`, `asyncio`, `subprocess`, FFmpeg (via PATH for audio).

---

## 9. Strengths of current stack

- **Offline-capable** assistant story (Ollama/GGUF).
- **Rich integration** surface for power users (Discord + web + voice).
- **Deterministic command path** without LLM cost.
- **Transparent JSON** data (easy to inspect, backup).

---

## 10. Weaknesses for a commercial multi-user product

| Issue | Detail |
|-------|--------|
| **Monolith** | Hard to scale horizontally; long `bot.py`. |
| **No authn/z** | Web API is open on LAN. |
| **Single-machine assumptions** | Playwright profiles, clipboard, screenshot paths. |
| **Conversation not durable** | `luna_conversation.py` in-memory. |
| **ToS / compliance** | Web scraping & social automation are high legal risk for SaaS. |
| **Secrets in .env** | No KMS/HSM story. |
| **Testing** | No `test_*.py` / pytest suite found; see **Appendix G**. |
| **iOS/Android** | No native clients; PWA/static only. |

---

## 11. Suggested target architectures (product)

### Tier A — **Personal local app** (closest to today)

- **Desktop:** Tauri or Electron + small local API (Rust/Go sidecar optional).
- **LLM:** Bundled Ollama or embedded llama.cpp.
- **Data:** SQLite + file store.

**Fit:** Privacy-first, same single-user mental model.

### Tier B — **Hosted “companion” SaaS**

- **API:** FastAPI or NestJS (typed routes, OpenAPI).
- **Workers:** Celery / RQ / BullMQ for reminders, email, webhooks.
- **DB:** Postgres + **pgvector** or Pinecone/Weaviate/Qdrant for RAG.
- **Chat:** OpenAI / Anthropic / self-hosted vLLM.
- **Realtime:** WebSockets (Socket.io / WS gateway) instead of polling.
- **Mobile:** React Native / Flutter; push via FCM/APNs.

**Fit:** Multi-tenant, billing, proper ACL.

### Tier C — **Enterprise assistant**

- Same as B + **SSO (SAML/OIDC)**, audit logs, data residency, **official** connectors (Microsoft Graph, Slack API, Notion API).

---

## 12. Technology options with ratings

Ratings: **1–5** (5 = best fit for **general-purpose SaaS**; higher for **local-first** where noted).

### 12.1 Backend language/framework

| Tech | Pros | Cons | SaaS fit |
|------|------|------|----------|
| **Python + FastAPI** | Fast to port from Flask; great AI ecosystem | GIL; need workers for CPU | **5** |
| **Node (NestJS)** | Great for IO; one language with React | Heavier ML story | **4** |
| **Go** | Small binaries; concurrency | More rewrite cost | **4** |
| **Rust (Axum/Actix)** | Performance/safety | Steeper dev cost | **3** |

### 12.2 Sync + chat transport

| Tech | Pros | Cons | Fit |
|------|------|------|-----|
| **WebSockets** | True streaming | Stateful infra | **5** |
| **SSE** | Simple over HTTP | One-way | **4** |
| **HTTP long-poll** | Easy | Inefficient | **2** |

### 12.3 Databases

| Tech | Pros | Cons | Fit |
|------|------|------|-----|
| **PostgreSQL** | Reliable; pgvector | Ops | **5** |
| **SQLite** | Embedded local | Bad for multi-tenant write scale | **5** local / **2** SaaS |
| **MongoDB** | Flexible docs | Vector story weaker unless Atlas | **3** |

### 12.4 Vector / RAG

| Tech | Pros | Cons | Fit |
|------|------|------|-----|
| **pgvector** | Co-located with relational | Needs tuning | **5** |
| **Qdrant / Weaviate / Milvus** | Purpose-built | Extra service | **4–5** |
| **File JSON embeddings** (current) | Zero deps | Rebuild cost; scale | **2** |

### 12.5 LLM hosting

| Tech | Pros | Cons | Fit |
|------|------|------|-----|
| **Ollama** | Easy local | Not multi-tenant server | **5** local |
| **vLLM / TGI** | Throughput | GPU ops | **5** self-host |
| **OpenAI/Anthropic APIs** | Zero ops | Cost; privacy | **5** SaaS |
| **llama.cpp / GGUF** | Edge/local | Packaging | **5** offline |

### 12.6 Discord vs first-class chat

| Tech | Pros | Cons | Fit |
|------|------|------|-----|
| **Discord.py** | Mature | Platform dependency | **4** for gaming communities |
| **Web + mobile apps** | Own UX | Build cost | **5** product |
| **Slack/Teams bots** | Enterprise | Different APIs | **4** B2B |

### 12.7 Browser automation

| Tech | Pros | Cons | Fit |
|------|------|------|-----|
| **Playwright** | Powerful | Fragile; ToS | **3** internal tools |
| **Official APIs** | Stable | OAuth complexity | **5** product |

### 12.8 Desktop wrapper

| Tech | Pros | Cons | Fit |
|------|------|------|-----|
| **Tauri** | Small binary | Rust learning | **5** |
| **Electron** | Ecosystem | Heavy | **4** |

---

## 13. Recommended extraction plan for new repo

1. **Define product boundaries:** Local assistant vs cloud vs Discord-only.
2. **Carry over:** Memory model (tiers), system prompt assembly, RAG concept, NL command idea **or** replace with tool-calling.
3. **Replace:** Single `bot.py` → services (`chat`, `scheduler`, `connectors`, `admin`).
4. **Mandatory for SaaS:** Auth, tenant ID on every query, encrypted storage, rate limits, audit log.
5. **Defer:** Playwright social automation unless compliance approves; use official APIs.

---

## 14. Use-case matrix

| Use case | Suggested stack |
|----------|-----------------|
| Offline privacy assistant | Tauri + SQLite + Ollama/GGUF |
| Discord community bot | Python FastAPI worker + discord.py + Redis |
| Mobile companion | Flutter + FastAPI + Postgres + push |
| Enterprise copilot | NestJS + OIDC + Microsoft Graph + vector DB |
| Developer automation | Keep Python; add Celery + structured logs |

---

## 15. Appendix: file count & complexity

- **Primary complexity:** `bot.py` (single file containing most features).
- **Supporting modules:** small, focused; **conversation persistence** is the biggest functional gap for reliability.

---

## Appendix A — Complete HTTP routes (`bot.py` / `web`)

| Method(s) | Path | Purpose (from code) |
|-----------|------|---------------------|
| GET | `/` | Serves `index.html` (main web UI) |
| GET | `/mind`, `/mind/` | `mind.html` — mind graph visualization |
| GET | `/evolution`, `/evolution/` | `evolution.html` |
| GET | `/api/status` | Bot + Ollama + models + biology + working state (JSON) |
| POST | `/api/proactive/ack` | Acknowledge proactive “Luna says” banner |
| POST | `/api/working/cancel` | Cancel current working task indicator |
| POST | `/api/evolution/toggle` | Toggle evolution feature on/off |
| POST | `/api/evolution/run` | Trigger one evolution step |
| GET | `/api/evolution-log` | Tail of `evolution.jsonl` (`?n=` limit) |
| POST | `/api/reset` | Reset biology/drives (JSON body `biology: true`, etc.) |
| GET | `/api/mind` | Graph data for Mind UI (nodes, edges) |
| GET | `/api/evolution` | Evolution UI payload |
| POST | `/api/feedback/respond` | Answer pending feedback request (`request_id`, choice) |
| GET | `/api/camera/status` | Camera available / stream state |
| POST | `/api/camera/frame` | Upload frame (multipart) for vision pipeline |
| GET | `/api/camera/chat` | GET camera-related chat context |
| POST | `/api/camera/chat` | POST user message about camera / vision |
| GET | `/api/memories` | Memory tiers for linked scope |
| GET | `/api/action-log` | Recent JSONL actions (`?n=`) |
| GET | `/api/calendar` | List events (`?mode=upcoming|today|week`) |
| POST | `/api/calendar` | Add event JSON `{date, time, title, note}` |
| POST | `/api/calendar/delete` | Delete by index `{index}` |
| GET | `/api/knowledge` | List knowledge entries |
| GET | `/api/knowledge/search` | `?q=` semantic + text search |
| GET | `/api/knowledge/<slug>` | Single entry |
| DELETE | `/api/knowledge/<slug>` | Delete entry |
| POST | `/api/knowledge` | Add knowledge (body varies) |
| GET | `/api/tool-drafts` | Pending tool drafts |
| POST | `/api/tool-drafts` | Submit draft `{name, description, script}` |
| GET | `/api/tool-drafts/rejected` | Rejected drafts |
| GET | `/api/tool-drafts/<name>` | Draft detail |
| DELETE | `/api/tool-drafts/<name>` | Remove draft |
| GET | `/api/tool-drafts/<name>/test-result` | Last test run output |
| POST | `/api/tool-drafts/<name>/approve` | Approve → absorbed tool flow |
| GET | `/api/security/status` | Security alerts / scan summary |
| POST | `/api/security/scan` | Run scan on path or body |
| GET | `/api/nudges` | List nudges (`?clear=1` clears after read) |
| POST | `/api/nudges` | Add nudge `{message}` |
| GET | `/api/recent-social` | Recent IG/FB/X actions |
| POST | `/api/recent-social/clear` | Clear list |
| POST | `/api/ig-dm` | Trigger Instagram DM from UI |
| GET | `/api/instagram-check-replies` | `?target=` optional |
| POST | `/api/fb-dm` | Messenger DM from UI |
| GET | `/api/facebook-check-replies` | `?target=` optional |
| GET | `/health`, `/health/` | `health.html` |
| GET | `/api/health` | Rich vitals: CPU/RAM/disk, Ollama latency, uptime, action stats |
| GET | `/api/chat-history` | `?q=` substring search, `?n=` limit (in-memory history) |
| GET | `/api/briefing` | Morning briefing JSON |
| GET | `/api/screenshot` | Capture + vision description |
| GET | `/manifest.json`, `/sw.js`, `/icon-192.png`, `/icon-512.png` | PWA assets |
| POST | `/api/chat` | Main chat: Shadow, `!` commands, NL commands, or LLM chat |
| POST | `/api/stream` | SSE stream of chat tokens (`text/event-stream`) |
| POST | `/api/tts` | JSON `{text}` → MP3 bytes |
| POST | `/api/tts-stop` | Stop playback |
| POST | `/api/transcribe` | Raw audio body → Whisper JSON `{text}` |
| POST | `/api/translate-voice` | Multipart or raw audio → Whisper translate task |

---

## Appendix B — Blueprint routes

### `/transcribeme` (`luna_transcribeme.py`)

| Method | Path | Purpose |
|--------|------|---------|
| GET | `/transcribeme/` | `transcribeme.html` |
| POST | `/transcribeme/api/transcribe` | Audio → text |
| POST | `/transcribeme/api/ask` | Ask LLM |
| POST | `/transcribeme/api/remind` | Set reminder |
| GET | `/transcribeme/api/status` | Deps / health |

### `/translate` (`luna_translate.py`)

| Method | Path | Purpose |
|--------|------|---------|
| GET | `/translate/` | `translate.html` |
| POST | `/translate/api/text` | Text → English via `ollama_chat` |
| POST | `/translate/api/audio` | Audio → English |
| GET | `/translate/api/status` | Availability |

---

## Appendix C — `_run_cmd_impl` command dispatch (`bot.py`)

Commands are invoked by **`_parse_command`** (NL), **`shadow_run`**, or **`_handle_bang`** (`!` in Discord/web). The **`dispatch` dict** covers the first batch; the rest are explicit `if` branches.

**`dispatch` keys (lambda → implementation):**

`news`, `search`, `suno`, `share_x`, `share_facebook`, `yt_comment`, `ig_dm`, `fb_msg`, `msg`, `call`, `dm`, `scrape`, `pc_vitals`, `luna_vitals`, `camera_see`

**Additional `cmd` strings handled in `_run_cmd_impl`:**

| cmd | Behavior |
|-----|----------|
| `ask_me` | Feedback popup (`_request_feedback`) or resume with `feedback_answer` |
| `remind` | `_parse_time` + `add_reminder` → Discord DM |
| `create_code` | LLM codegen → `Luna's creations/agents/*.py` + security scan |
| `suno_ready` | `_mark_suno_logged_in` |
| `help` | `HELP_TEXT` |
| `play` | Async `_play_in_discord_for_linked_user_async` |
| `skip` / `stop` | Music control via Discord voice |
| `podcast` | List tracks or `_play_podcast_in_discord_async` |
| `podcast_create` | `_create_podcast_from_description` |
| `join` / `leave` / `pause` / `resume` / `queue` | “Use !{cmd} in Discord.” |
| `joinme` | `_join_linked_user_vc_and_speak` |
| `summarize` | `_summarize_input` |
| `digest` | `_daily_digest` |
| `todo` | add / done / list |
| `calendar` | list / today / week / delete / add |
| `research` | `_research_content` |
| `research_story` | `_research_story_script` |
| `audiobook` | create / continue / cancel via `_create_audiobook_from_research`, `_audiobook_continue_next`, `_audiobook_cancel_pending` |
| *dynamic* | Any name in **`_absorbed_tool_names`** → `_run_absorbed_tool` |

**Return types:** Usually `str`; **`dict` with `need_feedback: True`** for interactive flows.

---

## Appendix D — Discord slash-style commands (`@bot.command`)

Registered on `commands.Bot` (prefix `!`). Custom `!help` replaces default.

| Command | Aliases |
|---------|---------|
| `help` | `files`, `commands` |
| `news` | — |
| `scrape` | — |
| `suno_ready` | `suno_logged_in` |
| `suno` | — |
| `share_song` | — |
| `share_facebook` | — |
| `yt_comment` | — |
| `ig_dm` | — |
| `fb_msg` | `messenger`, `fbmsg` |
| `joinme` | `join_vc`, `luna_join` |
| `call` | — |
| `dm` | — |
| `msg` | — |
| `remember` | — |
| `always_remember` | — |
| `memories` | — |
| `forget` | — |
| `forget_all` | — |
| `profile` | — |
| `play` | — |
| `podcast` | — |
| `pause` | — |
| `resume` | — |
| `skip` | — |
| `stop` | — |
| `queue` | — |
| `join` | — |
| `leave` | — |

**Note:** Many features are also available via **`on_message`** (natural language + `Shadow, …`) and **`/api/chat`** (`!` and NL). Not every user-facing string has a `@bot.command` twin — `_handle_bang` implements a **longer** list including `!research`, `!audiobook`, `!calendar`, `!todo`, `!digest`, `!summarize`, etc.

---

## Appendix E — `data/` persistence schema reference

Paths are under **`data/`** unless overridden by env (e.g. profile dirs).

| File / dir | Schema / role |
|------------|----------------|
| `memories.json` | `{ scope: { "core": [str], "long": [str], "short": [str] } }` |
| `profiles.json` | `{ scope: { field: str } }` |
| `biology_state.json` | Flat dict: drives `connection|usefulness|curiosity|expression` (0–1), `last_tick`, `dread`, `fear`, `mood`, `last_expression_at` |
| `reminders.json` | Reminder records for `_reminder_loop` |
| `goals.json` | Per-scope goal strings |
| `todos.json` | Todo items per scope |
| `calendar.json` | Calendar events per scope |
| `user_style.json` | Summarized user style per scope |
| `action_log.jsonl` | One JSON object per line: commands / replies |
| `recent_social.json` | List of `{platform, target, url, ts}` |
| `ig_last_seen.json` / `fb_last_seen.json` | DM thread last message hashes |
| `automation_solutions.json` | Cached “solutions” for failed commands |
| `shared_songs.json` | YouTube IDs / share history |
| `knowledge/` | Markdown (or text) knowledge files; slug from filename |
| `knowledge_embeddings.json` | Map slug → embedding vectors (rebuilt by background loop) |
| `tool_drafts/` | Draft JSON + `rejected/`, `tests/` |
| `absorbed_tools/` | Approved tool scripts |
| `evolution.jsonl` | Append-only evolution log |
| `evolution_enabled.json` | `{enabled: bool}` |
| `inbox.json` | **Nudges:** list of `{scope, text, ts}` (also used by `get_nudges`) |
| `last_proactive.json` | Proactive speech timing |
| `last_reflection_date.json` | Daily reflection cursor |
| `security_alerts.json` | Security scan alerts |
| `pc_context.json` | Serialized PC observation snapshot |
| `ml_learned.json` | `patterns`, `cmd_success` tallies, `last_learned` |
| `last_screenshot.png` | Latest capture |
| `screenshot_desc.json` | `{desc, ts}` from vision model |
| `clipboard_history.json` | `{items: [...]}` |
| `corrections.json` | `{corrections: [...]}` user corrections |
| `last_briefing.json` | Briefing dedupe |
| `audiobook_pending.json` | Multi-chapter audiobook state |
| `audiobook_scripts/` | Generated chapter scripts |
| `research_briefs/` | `!research` outputs |
| `SOUL.md`, `TOOLS.md`, `OBJECTIVES.md` | Injected identity / tools / objectives |
| `skills/*.md` | Optional skill injections |

**In-memory only:** `luna_conversation._STORE` — **lost on restart** (critical product gap).

---

## Appendix F — Frontend → API mapping (`index.html`)

Primary UI is **`index.html`** (served at `/`). It uses **`fetch`** to:

| UI area | Endpoints |
|---------|-----------|
| Status / “working” | `GET /api/status` (polling) |
| Security widget | `GET /api/security/status`, `POST /api/security/scan` |
| Proactive banner | `POST /api/proactive/ack` |
| Calendar panel | `GET /api/calendar?mode=`, `POST /api/calendar`, `POST /api/calendar/delete` |
| Main chat | `POST /api/chat` (JSON `{message}`); feedback votes also send short `message` prefixed with `[feedback:…]` |
| Camera | `GET/POST /api/camera/chat`, `POST /api/camera/frame` |
| Feedback popup | `POST /api/feedback/respond` |
| Action log | `GET /api/action-log?n=` |
| Nudges | `POST /api/nudges` |
| Knowledge / RAG UI | `GET /api/knowledge`, `/api/knowledge/search?q=`, `DELETE /api/knowledge/<slug>` |
| Tool drafts / evolution | `/api/tool-drafts*`, `POST /api/evolution/toggle`, `POST /api/evolution/run`, `GET /api/evolution-log` |
| Reset / cancel | `POST /api/reset`, `POST /api/working/cancel` |
| Mind graph embed | `GET /api/mind` |
| Social | `GET /api/recent-social`, `POST /api/recent-social/clear`, `GET /api/instagram-check-replies`, `GET /api/facebook-check-replies`, `POST /api/ig-dm`, `POST /api/fb-dm` |
| Voice | `POST /api/transcribe`, `POST /api/translate-voice`, `POST /api/tts-stop` |

**`mind.html`:** `GET /api/mind` only.  
**`evolution.html`:** `GET /api/evolution`.  
**`health.html`:** typically loads **`/api/health`** (pattern matches other pages).

---

## Appendix G — Runtime, deployment, testing

| Topic | Detail |
|-------|--------|
| **Entry** | `python bot.py` — requires `DISCORD_TOKEN` (validated at import). |
| **Web bind** | `web.run(host="127.0.0.1", port=5050, threaded=True)` — **not** exposed as 0.0.0.0 by default. |
| **Warmup** | Background thread hits `OLLAMA_BASE/api/chat` once after 5s delay. |
| **Browser** | Optional `webbrowser.open` to `http://127.0.0.1:5050` on start. |
| **Tests** | **No `test_*.py` or `pytest` suite** found in repo root — treat as **untested** for CI. |
| **FFmpeg** | Expected on PATH for Whisper / audio pipelines (transcode). |
| **README vs deps** | `README.md` may describe optional GPU/image stacks (`torch`, `diffusers`, …) that are **not** in default `requirements.txt` — verify imports before assuming features exist. |

---

## 16. Document maintenance

- **Generated from:** Luna 5.0 codebase review (structure, `grep`, representative reads of `bot.py` parsers, API routes, modules). **Appendices A–G** were added via exhaustive route / dispatch / `data/` / frontend `fetch` passes (March 2026).
- **Update when:** Major refactors split `bot.py`, add auth, or change routes.

---

*This document is intended as a planning artifact for a new repository and product roadmap — not as runtime documentation for the current single-user deployment. For **line-by-line** behavior of each handler, read the corresponding function body in `bot.py`.*

