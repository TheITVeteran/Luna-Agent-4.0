# Luna VRM Electron Wrapper

Runs the existing `http://127.0.0.1:5050/vrm/` page in a standalone desktop app.

This helps with OBS capture workflows where a browser tab/window is awkward.

## Setup

1. Keep Luna backend running (`python bot_main.py`).
2. In this folder:

```powershell
npm install
```

## Run

Default (transparent OBS-style URL):

```powershell
npm start
```

Or explicit:

```powershell
npm run start:obs
```

Custom URL:

```powershell
npm start -- --url=http://127.0.0.1:5050/vrm/?transparent=1
```

## Notes

- Mic permissions are allowed by the Electron permission handler.
- This is a wrapper only; all avatar logic stays in `vrm_viewer.html`.
- For OBS, test both Game Capture and Window Capture. Results vary by GPU/OBS build.
