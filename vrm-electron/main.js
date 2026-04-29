const { app, BrowserWindow, session, Menu } = require("electron");

const DEFAULT_URL = "http://127.0.0.1:5050/vrm/?transparent=1&obs=1";

function parseArgUrl() {
  const raw = process.argv.find((a) => a.startsWith("--url="));
  if (!raw) return DEFAULT_URL;
  const val = raw.slice("--url=".length).trim();
  return val || DEFAULT_URL;
}

function createWindow() {
  const win = new BrowserWindow({
    width: 900,
    height: 1200,
    minWidth: 500,
    minHeight: 700,
    backgroundColor: "#00000000",
    transparent: true,
    frame: true,
    autoHideMenuBar: true,
    webPreferences: {
      contextIsolation: true,
      sandbox: true,
      backgroundThrottling: false,
      spellcheck: false,
      devTools: true,
      autoplayPolicy: "no-user-gesture-required",
    },
  });

  Menu.setApplicationMenu(null);
  win.loadURL(parseArgUrl());
}

app.commandLine.appendSwitch("disable-renderer-backgrounding");
app.commandLine.appendSwitch("enable-transparent-visuals");
app.commandLine.appendSwitch("autoplay-policy", "no-user-gesture-required");

app.whenReady().then(() => {
  session.defaultSession.setPermissionRequestHandler((wc, permission, callback) => {
    if (permission === "media" || permission === "microphone" || permission === "camera") {
      callback(true);
      return;
    }
    callback(false);
  });

  createWindow();
  app.on("activate", () => {
    if (BrowserWindow.getAllWindows().length === 0) createWindow();
  });
});

app.on("window-all-closed", () => {
  if (process.platform !== "darwin") app.quit();
});
