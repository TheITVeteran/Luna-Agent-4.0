# Luna — Buttons, links and steps

Configure URLs in **`.env`**. Steps are what Luna does when you use each button or command.

---

## Quick Actions (Web UI)

| Button       | Sends / Command        | Link(s) | Steps |
|-------------|-------------------------|--------|-------|
| **News**    | `Shadow, news`         | RSS feeds (see below) | 1. Fetch BBC, NYT, Al Jazeera RSS. 2. Return headlines. |
| **Share Song** | `Shadow, share song` | X compose + YouTube feed | 1. Pick a song from your channel (not shared in last 7 days). 2. Open X (Twitter) compose. 3. Type message + link. 4. Click Post. |
| **Facebook** | `Shadow, share facebook` | Facebook profile URL | See [Facebook steps](#facebook) below. |
| **Commands** | (opens help)           | — | Show `!help` list. |

### Media (side panel — `index.html`)

| Button | Sends / fills |
|--------|----------------|
| Translate | Opens `/translate/` |
| Upload voice | File picker → translate pipeline |
| TranscribeMe | Opens `/transcribeme/` |
| Play | `!play ` |
| Custom podcast | `!podcast` |
| Create podcast | `!podcast create ` |
| **Create audiobook** | Popup (brief / story / both / auto). Multi-chapter text → **chapter 1 MP3 first**, then say **`!audiobook continue`** for each next file, or **`!audiobook cancel`** to stop. |
| **Research brief** | `!research ` |
| **Story script** | `!research_story ` |
| Skip | `!skip` |
| Stop | `!stop` |

---

### ⋯ More commands (popups)

Each of **Social**, **Media**, **System**, and **Tools** has a **⋯** control on the **right of the section header**. It opens a popup with **extra commands** for that category (so they stay grouped with the main strip). **Escape** or click outside closes it.

| Section | Examples in ⋯ menu |
|---------|---------------------|
| **Social** | `!suno_ready`, `!share_song`, `!share_facebook` |
| **Media** | `!join`, `!leave`, `!pause`, `!queue`, `!joinme` |
| **System** | `!todo`, `!summarize`, `!digest`, remind me, `!scrape`, `retry`, `!pc_vitals`, `!luna_vitals` |
| **Tools** | `!remember`, `!always_remember`, `!profile`, **ask me**, note about reflection |

---

## Still not a top-strip button (by design)

| Command / feature | Notes |
|-------------------|--------|
| **Reflection** | Runs automatically daily — see Tools ⋯ hint |
| **Nudge**, **Luna says**, **View action log**, **Tool drafts**, **Knowledge**, **Memory**, **Security scan**, **IG replies**, etc. | **Lower** panel sections only |

---

## Links (set in `.env`)

| Purpose        | .env variable           | Default |
|----------------|-------------------------|---------|
| Facebook profile (where post opens) | `FACEBOOK_PROFILE_URL` | `https://www.facebook.com/solonaras` |
| Facebook home  | `FACEBOOK_HOME_URL`    | `https://www.facebook.com/` |
| X (Twitter) compose | `X_COMPOSE_URL`   | `https://x.com/compose/post` |
| X profile      | `X_PROFILE_URL`        | `https://x.com/ChrisSolonos` |
| YouTube channel feed | (uses `YOUTUBE_CHANNEL_ID`) | `https://www.youtube.com/feeds/videos.xml?channel_id=UCqIjEHOABb8fwbKbjDhVRuA` |
| Suno create    | `SUNO_CREATE_URL`      | `https://suno.com/create` |
| News RSS       | (hardcoded in bot)     | BBC, NYT, Al Jazeera world RSS |

---

## Steps by action

### Facebook (Share to Facebook)

**Link:** `FACEBOOK_PROFILE_URL` (e.g. `https://www.facebook.com/solonaras`)

1. Open Chromium; go to your Facebook profile.
2. If login page → leave window open until you log in and close it.
3. Open **Create post**: click “What’s on your mind?” / “Create post” (multiple selectors).
4. Type the post (song title + YouTube link).
5. Click **Next** → opens **Post settings** (audience) menu.
6. Wait for **Post settings** dialog to be visible.
7. (Optional) Click **Public** inside the dialog to set audience.
8. Click the **blue Post button** (only blue button in the menu; Save is gray):
   - Find button by blue background in dialog, or rightmost button, or `button:has-text('Post')`.
9. Done → “Shared to Facebook: ‘Song title’”.

---

### Share Song (X / Twitter)

**Link:** `X_COMPOSE_URL` (e.g. `https://x.com/compose/post`)

1. Pick a song from YouTube channel feed (prefer not shared in last 7 days).
2. Open X compose in Chromium.
3. If not logged in → browser opens for login; close when done.
4. Find tweet textbox; type message + song link.
5. Click **Post** (tweet button).
6. Record song as shared so it’s not picked again soon.

---

### News

**Links (RSS):**

- `https://feeds.bbci.co.uk/news/world/rss.xml`
- `https://rss.nytimes.com/services/xml/rss/nyt/World.xml`
- `https://www.aljazeera.com/xml/rss/all.xml`

**Steps:** Fetch RSS, parse items, return formatted headlines (no browser).

---

### Suno (create song)

**Link:** `SUNO_CREATE_URL` (e.g. `https://suno.com/create`)

1. Open Suno in Chromium.
2. If not logged in → browser opens for login; close when done.
3. Use Suno UI to create song from description (bot does not automate the full Suno flow beyond opening and login).

---

### Other commands (Discord / Shadow)

- **!suno** `<description>` — open Suno, same link as above.
- **!yt_comment** `<url>` — YouTube; uses `YOUTUBE_PROFILE_DIR` for login.
- **!ig_dm**, **!fb_msg**, **!msg**, **!call** — Instagram, Messenger, WhatsApp; each has a profile URL and browser profile dir in `bot.py` / `.env`.

---

## Session / UI buttons

| Button       | Action              | Link / steps |
|-------------|---------------------|--------------|
| **Clear Chat** | Clear chat history | Local only. |
| **Stop Voice** | Stop TTS playback | Local only. |
| **SEND**     | Send message or command | POST to `/api/chat`. |

---

## Example `.env` overrides

```env
# Facebook — your profile (where the post composer opens)
FACEBOOK_PROFILE_URL=https://www.facebook.com/solonaras

# X (Twitter) — compose URL
X_COMPOSE_URL=https://x.com/compose/post

# YouTube channel for Share Song (channel ID only)
YOUTUBE_CHANNEL_ID=UCqIjEHOABb8fwbKbjDhVRuA

# Suno
SUNO_CREATE_URL=https://suno.com/create
```

Edit `BUTTONS.md` to change steps or add new buttons; edit `.env` to change links.
