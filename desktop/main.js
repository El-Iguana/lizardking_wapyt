// LizardKing as a desktop app: Electron starts service.py on a free loopback
// port, waits for /lk/health, and shows the page in its own window. Quitting
// the window stops the server (and with it every ffmpeg it started).
"use strict";

const { app, BrowserWindow, Menu, dialog, shell } = require("electron");
const { spawn } = require("node:child_process");
const fs = require("node:fs");
const http = require("node:http");
const net = require("node:net");
const os = require("node:os");
const path = require("node:path");

const ROOT = path.resolve(__dirname, "..");
const HOST = "127.0.0.1";
const START_TIMEOUT_MS = 60_000;

let server = null;
let serverLog = "";
let win = null;
let quitting = false;

// Files named on the command line (or by the desktop entry's %F) are handed to
// service.py, which queues them for the first page.
const files = process.argv.slice(app.isPackaged ? 1 : 2)
  .filter((arg) => !arg.startsWith("-") && arg !== "." && fs.existsSync(arg))
  .map((arg) => path.resolve(arg));

function freePort() {
  return new Promise((resolve, reject) => {
    const probe = net.createServer();
    probe.unref();
    probe.on("error", reject);
    probe.listen(0, HOST, () => {
      const { port } = probe.address();
      probe.close(() => resolve(port));
    });
  });
}

// The project's own venv first: signals reach Python directly. A desktop
// launcher has a short PATH, so look for uv where brew and the installer put it.
function pythonCommand() {
  const venv = path.join(ROOT, ".venv", "bin", "python");
  if (fs.existsSync(venv)) return [venv, []];
  const candidates = [
    ...(process.env.PATH || "").split(path.delimiter).map((dir) => path.join(dir, "uv")),
    "/home/linuxbrew/.linuxbrew/bin/uv",
    path.join(os.homedir(), ".local", "bin", "uv"),
    path.join(os.homedir(), ".cargo", "bin", "uv"),
  ];
  const uv = candidates.find((candidate) => fs.existsSync(candidate));
  if (!uv) throw new Error("Neither .venv/bin/python nor uv was found. Run `uv sync` in " + ROOT);
  return [uv, ["run", "python"]];
}

function startServer(port) {
  const [command, prefix] = pythonCommand();
  server = spawn(command, [...prefix, "service.py", "--no-browser", ...files], {
    cwd: ROOT,
    env: { ...process.env, LIZARDKING_PORT: String(port) },
    stdio: ["ignore", "pipe", "pipe"],
  });
  const keep = (chunk) => { serverLog = (serverLog + chunk).slice(-8000); };
  server.stdout.on("data", keep);
  server.stderr.on("data", keep);
  server.on("exit", (code, signal) => {
    server = null;
    if (!quitting) {
      dialog.showErrorBox("LizardKing stopped",
        `The LizardKing server exited (${signal || `code ${code}`}).\n\n${serverLog.slice(-3000)}`);
      app.quit();
    }
  });
}

function healthy(port) {
  return new Promise((resolve) => {
    const request = http.get({ host: HOST, port, path: "/lk/health", timeout: 1000 }, (response) => {
      response.resume();
      resolve(response.statusCode === 200);
    });
    request.on("error", () => resolve(false));
    request.on("timeout", () => { request.destroy(); resolve(false); });
  });
}

async function waitForServer(port) {
  const deadline = Date.now() + START_TIMEOUT_MS;
  while (Date.now() < deadline) {
    if (!server) throw new Error("The server exited while starting.");
    if (await healthy(port)) return;
    await new Promise((resolve) => setTimeout(resolve, 150));
  }
  throw new Error(`The server did not answer within ${START_TIMEOUT_MS / 1000} s.`);
}

function stopServer() {
  if (!server) return;
  const child = server;
  child.kill("SIGINT");  // service.py stops its encoders and empties the cache
  setTimeout(() => { if (child.exitCode === null) child.kill("SIGKILL"); }, 5000).unref();
}

function createWindow(port) {
  const origin = `http://${HOST}:${port}`;
  win = new BrowserWindow({
    width: 1400,
    height: 880,
    minWidth: 720,
    minHeight: 480,
    title: "LizardKing",
    icon: path.join(__dirname, "icon.png"),
    backgroundColor: "#000000",
    show: false,
    webPreferences: { contextIsolation: true, sandbox: true },
  });
  win.once("ready-to-show", () => win.show());
  // The page has no <title> of its own; keep the app's name in the title bar.
  win.on("page-title-updated", (event, title) => {
    if (!title || title.startsWith(`${HOST}:`)) event.preventDefault();
  });

  // Links (the About dialog's repo link) open in the real browser; the window
  // itself never leaves LizardKing.
  win.webContents.setWindowOpenHandler(({ url }) => {
    if (/^https?:\/\//.test(url) && !url.startsWith(origin)) shell.openExternal(url);
    return { action: "deny" };
  });
  win.webContents.on("will-navigate", (event, url) => {
    if (!url.startsWith(origin)) {
      event.preventDefault();
      if (/^https?:\/\//.test(url)) shell.openExternal(url);
    }
  });
  // No Electron menu bar (LizardKing has its own), but keep reload and devtools.
  win.webContents.on("before-input-event", (event, input) => {
    if (input.type !== "keyDown") return;
    if (input.key === "F12" || (input.control && input.shift && input.key.toLowerCase() === "i")) {
      win.webContents.toggleDevTools();
      event.preventDefault();
    } else if (input.key === "F5" || (input.control && !input.shift && input.key.toLowerCase() === "r")) {
      win.webContents.reload();
      event.preventDefault();
    }
  });
  win.on("closed", () => { win = null; });
  win.loadURL(`${origin}/lizardking/`);
}

if (!app.requestSingleInstanceLock()) {
  app.quit();
} else {
  app.on("second-instance", () => {
    if (win) {
      if (win.isMinimized()) win.restore();
      win.focus();
    }
  });

  app.whenReady().then(async () => {
    Menu.setApplicationMenu(null);
    try {
      const port = await freePort();
      startServer(port);
      await waitForServer(port);
      createWindow(port);
    } catch (error) {
      quitting = true;
      stopServer();
      dialog.showErrorBox("LizardKing could not start", `${error.message}\n\n${serverLog.slice(-3000)}`);
      app.quit();
    }
  });

  app.on("window-all-closed", () => app.quit());
  app.on("before-quit", () => { quitting = true; stopServer(); });
}
