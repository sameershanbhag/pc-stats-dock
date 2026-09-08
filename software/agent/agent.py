#!/usr/bin/env python3
"""PC Stats Panel agent for macOS.

- reads CPU/GPU/RAM/temps/power from macmon (Apple Silicon, no sudo) or basic built-ins
- serves the dashboard on http://localhost:4400 and the button editor on /admin
- opens the dashboard full-screen on the panel the moment it is plugged in
- turns touch buttons into actions: key combos, apps, volume, mic, Shortcuts, AppleScript, shell

Run:  python3 agent.py               normal
      python3 agent.py --dry-run     actions are logged, not executed (safe for testing)
      python3 agent.py --no-kiosk    never open the browser window
"""
import argparse
import json
import math
import os
import platform
import random
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

HERE = Path(__file__).resolve().parent
DASHBOARD = HERE.parent / "dashboard"
# Your buttons live outside the code so updates never overwrite them. Override with PCSTATS_CONFIG.
CONFIG_PATH = Path(os.environ.get("PCSTATS_CONFIG") or (Path.home() / "Library" / "Application Support" / "pc-stats-dock" / "config.json"))
sys.path.insert(0, str(HERE))
import keys_mac  # noqa: E402
from displays import Kiosk, list_displays  # noqa: E402
import arrange  # noqa: E402
import touch as touch_mod
import feeds as feeds_mod  # noqa: E402
from events import EventStore, from_claude_code, from_codex, from_generic  # noqa: E402
from feeds import APP_PRESETS, SOURCES, FeedManager, TeamsFeed  # noqa: E402
from focus import focus as focus_window  # noqa: E402
from sensors_mac import MacSensors, computer_name  # noqa: E402

DRY_RUN = False
OSASCRIPT, OPEN, SHORTCUTS = "/usr/bin/osascript", "/usr/bin/open", "/usr/bin/shortcuts"
M1DDC = shutil.which("m1ddc") or ("/opt/homebrew/bin/m1ddc" if os.path.exists("/opt/homebrew/bin/m1ddc") else None)
ACCESSIBILITY_PANE = "x-apple.systempreferences:com.apple.preference.security?Privacy_Accessibility"
FULL_DISK_PANE = "x-apple.systempreferences:com.apple.preference.security?Privacy_AllFiles"
DEFAULT_FEEDS = [
    {"id": "feed-1", "title": "AI chats", "source": "ai", "limit": 6},
    {"id": "feed-2", "title": "Slack", "source": "slack", "token": "", "channels": ["dm"], "limit": 5},
    {"id": "feed-3", "title": "Teams", "source": "notifications", "apps": ["Microsoft Teams"], "limit": 5},
]


def log(msg):
    print(time.strftime("%H:%M:%S"), msg, flush=True)


def load_config():
    if not CONFIG_PATH.exists():
        CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy(HERE / "config.json", CONFIG_PATH)   # first run: start from the shipped defaults
        os.chmod(CONFIG_PATH, 0o600)
    with open(CONFIG_PATH, encoding="utf-8") as f:
        cfg = json.load(f)
    cfg.setdefault("port", 4400)
    cfg.setdefault("poll_ms", 500)
    cfg.setdefault("panel_resolution", [1540, 720])
    cfg.setdefault("kiosk", True)
    cfg.setdefault("right_side", "three")
    cfg.setdefault("panel_position", "above")       # where the panel sits relative to the main display
    cfg.setdefault("keep_panel_for_dock", True)     # never let the panel become the main display
    if not cfg.get("feeds"):
        cfg["feeds"] = json.loads(json.dumps(DEFAULT_FEEDS))
    cfg["_pc_name"] = cfg.get("pc_name") or computer_name()   # runtime only, never saved
    return cfg


def public_feeds(cfg):
    """Feed settings for the pages: never the token itself, only whether one is stored."""
    out = []
    for f in cfg.get("feeds", []):
        g = {k: v for k, v in f.items() if k != "token"}
        g["has_token"] = bool(f.get("token"))
        out.append(g)
    return out


RIGHT_SIDES = ("three", "three-feeds", "buttons", "feeds", "feeds3", "mixed")


def validate_feeds(right_side, feeds, previous):
    """Returns (right_side, clean_feeds, error). An empty token keeps the stored one."""
    if right_side not in RIGHT_SIDES:
        return None, None, f"unknown layout {right_side!r}"
    if not isinstance(feeds, list) or len(feeds) > 3:
        return None, None, "feeds must be a list of at most 3 entries"
    old = {f["id"]: f for f in previous or []}
    clean = []
    for i, f in enumerate(feeds):
        if not isinstance(f, dict):
            return None, None, f"feed {i + 1}: not an object"
        fid = f"feed-{i + 1}"
        src = f.get("source", "none")
        if src not in SOURCES:
            return None, None, f"feed {i + 1}: unknown source {src!r}"
        try:
            limit = max(1, min(int(f.get("limit") or 8), 20))
        except (TypeError, ValueError):
            limit = 8
        entry = {"id": fid, "title": str(f.get("title", ""))[:24], "source": src, "limit": limit}
        if src == "slack":
            token = str(f.get("token") or "").strip() or (old.get(fid, {}).get("token") if old.get(fid, {}).get("source") == "slack" else "")
            entry["token"] = token or ""
            chans = f.get("channels") or []
            if isinstance(chans, str):
                chans = [c for c in re.split(r"[,\s]+", chans) if c]
            entry["channels"] = [str(c).strip() for c in chans if str(c).strip()][:20]
        elif src == "teams":
            entry["tenant"] = str(f.get("tenant") or "common").strip()[:80]
            entry["client_id"] = str(f.get("client_id") or "").strip()[:80]
        elif src == "notifications":
            apps = f.get("apps") or []
            if isinstance(apps, str):
                apps = [a for a in re.split(r"[,\s]+", apps) if a]
            entry["apps"] = [str(a).strip() for a in apps if str(a).strip()][:12]
        clean.append(entry)
    return right_side, clean, None


def save_config(cfg):
    path = CONFIG_PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".json.tmp")
    data = {k: v for k, v in cfg.items() if not k.startswith("_")}
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
    os.chmod(tmp, 0o600)   # may hold a Slack token: readable by you only
    os.replace(tmp, path)


# ---------------------------------------------------------------- state ----
class State:
    def __init__(self):
        self.lock = threading.Lock()
        self.stats = {}
        self.cfg_version = 1
        self.mic_muted = None
        self.mic_restore = 75
        self.caps = {"accessibility": None, "volume": "unknown", "mic": None}
        self.last_prompt = 0.0
        self.arranged_for = None
        self.kiosk = None       # the dashboard window (set when the kiosk runs)
        self.last_main_cursor = None   # where the mouse last was on the main display
        self.last_front_pid = None     # the app that was active before a tap made the dashboard active
        self.touch = None       # touch mapper supervisor (set when the kiosk runs)

    def set(self, stats):
        with self.lock:
            self.stats = stats

    def get(self):
        with self.lock:
            return dict(self.stats)


def restart_self():
    """Exit cleanly; the login item (KeepAlive) starts a fresh launcher + agent within seconds.
    The dashboard window is a separate process and is adopted by the new agent."""
    threading.Timer(0.5, lambda: os._exit(0)).start()


def refresh_caps(state):
    """What the buttons can do right now: key presses need Accessibility, volume needs an output
    device with a software volume (DisplayPort/HDMI audio has none, DDC is the fallback)."""
    if DRY_RUN:
        state.caps = {"accessibility": True, "volume": "system", "mic": True}
        return
    try:
        ax = keys_mac.ax_state()
        if ax == "restart":
            log("[permission] Accessibility was granted after start; restarting to apply it")
            restart_self()
            return
        state.caps["accessibility"] = ax == "trusted"
    except Exception:
        state.caps["accessibility"] = None
    try:
        vs = volume_settings()
        state.mic_muted = mic_state(state, vs)
        state.caps["mic"] = vs.get("input volume", "").isdigit()
        if vs.get("output volume", "").isdigit():
            state.caps["volume"] = "system"
        elif ddc_volume() is not None:
            state.caps["volume"] = "ddc"
        else:
            state.caps["volume"] = "none"
    except Exception:
        state.caps["mic"] = None
    state.caps["touch"] = state.touch.status()["state"] if state.touch else "off"


def poll_loop(state, cfg, sensors):
    interval = cfg["poll_ms"] / 1000
    last_caps = 0
    while True:
        try:
            payload = sensors.read()
        except Exception as exc:  # never let a sensor hiccup kill the loop
            log(f"[sensors] {type(exc).__name__}: {exc}")
            payload = {"sensors": "error", "cpu": {}, "gpu": {}, "ram": {}, "storage": [], "net": None, "fans": []}
        if time.time() - last_caps > 3:
            refresh_caps(state)
            last_caps = time.time()
        payload.update({"ts": int(time.time() * 1000), "demo": False, "lhm_ok": payload.get("sensors") == "macmon",
                        "pc_name": cfg["_pc_name"], "mic_muted": state.mic_muted, "cfg_version": state.cfg_version,
                        "caps": dict(state.caps)})
        state.set(payload)
        time.sleep(interval)


# -------------------------------------------------------------- actions ----
MODS = keys_mac.MODIFIERS


def osascript(script, timeout=8):
    r = subprocess.run([OSASCRIPT, "-e", script], capture_output=True, text=True, timeout=timeout)
    if r.returncode != 0:
        raise RuntimeError(r.stderr.strip().splitlines()[-1] if r.stderr.strip() else "osascript failed")
    return r.stdout.strip()


def send_hotkey(keys):
    """Posts the combination from this process (CoreGraphics), so the one permission macOS asks
    for belongs to the agent itself. Raises ValueError / PermissionError."""
    keys_mac.post_hotkey(keys)


def request_accessibility(state):
    """Show macOS's own 'allow in Accessibility' dialog (it lists the app there) and open the pane.
    Throttled so a tapped-again button does not spam dialogs."""
    if time.time() - state.last_prompt < 600:
        return
    state.last_prompt = time.time()
    try:
        keys_mac.ax_trusted(prompt=True)
    except Exception as exc:
        log(f"[permission] prompt failed: {exc}")
    subprocess.Popen([OPEN, ACCESSIBILITY_PANE], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def ddc_volume():
    """Current monitor volume over DDC/CI (0-100) via m1ddc, or None if unsupported."""
    if not M1DDC:
        return None
    try:
        r = subprocess.run([M1DDC, "get", "volume"], capture_output=True, text=True, timeout=4)
        return int(r.stdout.strip()) if r.returncode == 0 and r.stdout.strip().isdigit() else None
    except Exception:
        return None


def open_app(target):
    """`open` a path, an app name, or a bundle identifier. Returns (ok, message)."""
    target = target.strip()
    attempts = []
    if target.startswith(("/", "~")):
        attempts.append([OPEN, os.path.expanduser(target)])
    else:
        attempts.append([OPEN, "-a", target])
        if "." in target and " " not in target:
            attempts.append([OPEN, "-b", target])
    err = ""
    for cmd in attempts:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=10)
        if r.returncode == 0:
            return True, "opened " + target
        err = r.stderr.strip() or err
    return False, err or f"could not open {target}"


def volume_settings():
    out = osascript("get volume settings")
    vals = {}
    for part in out.split(","):
        if ":" in part:
            k, v = part.split(":", 1)
            vals[k.strip()] = v.strip()
    return vals


def mic_state(state, settings=None):
    if DRY_RUN:
        return state.mic_muted
    try:
        v = (settings or volume_settings()).get("input volume", "")
        if not v.isdigit():
            return None
        level = int(v)
        if level > 0:
            state.mic_restore = level
        return level == 0
    except Exception:
        return None


def run_action(btn, state):
    """Returns (ok, message)."""
    kind = btn.get("type")
    what = {"hotkey": "+".join(btn.get("keys") or []), "app": btn.get("app"), "volume": btn.get("dir"),
            "shortcut": btn.get("name"), "applescript": (btn.get("command") or "")[:60],
            "shell": (btn.get("command") or "")[:60], "mic": "toggle", "empty": ""}.get(kind, "")
    if DRY_RUN:
        return True, f"dry run: {kind} {what}"
    if kind != "empty":
        park_cursor_on_main(state)
    try:
        if kind == "hotkey":
            keys = [k.lower() for k in btn["keys"]]
            spotlight = "space" in keys and ("cmd" in keys or "command" in keys)   # Spotlight closes if focus moves after it opens
            if spotlight:
                focus_main_display(state)
            try:
                send_hotkey(keys)
            except PermissionError:
                request_accessibility(state)
                state.caps["accessibility"] = False
                return False, ("macOS has not allowed the agent to press keys yet. Open System Settings › Privacy & Security › "
                               "Accessibility (it just opened) and switch on “PC Stats Panel”, then tap again.")
            if not spotlight:
                run_later(focus_main_display, state)     # give the keyboard back to the app you were using, off the tap's path
            return True, "pressed " + keys_mac.describe(btn["keys"])
        if kind == "app":
            return open_app(btn["app"])
        if kind == "volume":
            d = btn.get("dir", "up")
            vs = volume_settings()
            if not vs.get("output volume", "").isdigit():
                cur = ddc_volume()
                if cur is None:
                    return False, ("sound is going to the monitor over DisplayPort/HDMI, which macOS cannot adjust and this monitor "
                                   "does not accept DDC volume commands. Use the monitor's own buttons, pick another output in "
                                   "Sound settings, or use the app-volume buttons (Music/Spotify) from the library.")
                if d == "mute":
                    new = 0 if cur > 0 else 50
                else:
                    new = max(0, min(100, cur + (7 if d == "up" else -7)))
                r = subprocess.run([M1DDC, "set", "volume", str(new)], capture_output=True, text=True, timeout=4)
                return (r.returncode == 0), (f"monitor volume {new}" if r.returncode == 0 else (r.stderr.strip() or "DDC volume failed"))
            if d == "mute":
                muted = vs.get("output muted") == "true"
                osascript("set volume %s output muted" % ("without" if muted else "with"))
                return True, "speakers " + ("unmuted" if muted else "muted")
            cur = int(vs["output volume"])
            new = max(0, min(100, cur + (7 if d == "up" else -7)))
            osascript("set volume output volume %d" % new)
            return True, f"volume {new}"
        if kind == "mic":
            v = volume_settings().get("input volume", "")
            if not v.isdigit():
                return False, "no adjustable microphone found"
            if int(v) > 0:
                state.mic_restore = int(v)
                osascript("set volume input volume 0")
                state.mic_muted = True
                return True, "mic muted"
            osascript("set volume input volume %d" % (state.mic_restore or 75))
            state.mic_muted = False
            return True, "mic live"
        if kind == "shortcut":
            r = subprocess.run([SHORTCUTS, "run", btn["name"]], capture_output=True, text=True, timeout=30)
            return (r.returncode == 0), (r.stderr.strip() or f"ran shortcut {btn['name']}")
        if kind == "applescript":
            out = osascript(btn["command"], timeout=20)
            return True, out[:80] or "ran AppleScript"
        if kind == "shell":
            subprocess.Popen(["/bin/zsh", "-lc", btn["command"]], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            return True, "ran: " + what
        if kind == "empty":
            return True, "blank slot"
        return False, f"unknown action type {kind}"
    except Exception as exc:
        msg = f"{type(exc).__name__}: {exc}"
        if "-1743" in msg or "Not authorized to send Apple events" in msg:
            msg += " — allow “PC Stats Panel” to control that app under Privacy & Security › Automation"
        return False, msg


# ---------------------------------------------------------------- config ----
VALID_TYPES = {"hotkey", "app", "volume", "mic", "shortcut", "applescript", "shell", "empty"}


def validate_buttons(buttons):
    if not isinstance(buttons, list) or len(buttons) > 12:
        return None, "buttons must be a list of at most 12 entries"
    clean, seen = [], set()
    for i, b in enumerate(buttons):
        if not isinstance(b, dict):
            return None, f"slot {i + 1}: not an object"
        kind = b.get("type", "empty")
        if kind not in VALID_TYPES:
            return None, f"slot {i + 1}: unknown type {kind!r}"
        entry = {"id": str(b.get("id") or f"slot-{i + 1}"), "type": kind, "label": str(b.get("label", ""))[:16],
                 "sub": str(b.get("sub", ""))[:20], "glyph": str(b.get("glyph", ""))[:2]}
        if entry["id"] in seen:
            entry["id"] = f"slot-{i + 1}"
        seen.add(entry["id"])
        if kind == "hotkey":
            keys = [str(k).lower() for k in (b.get("keys") or []) if str(k).strip()]
            if not [k for k in keys if k not in MODS]:
                return None, f"slot {i + 1}: pick a key, not only modifiers"
            bad = [k for k in keys if k not in MODS and k not in keys_mac.KEYCODES]
            if bad:
                return None, f"slot {i + 1}: unknown key {bad[0]!r}"
            entry["keys"] = keys
        elif kind == "app":
            if not str(b.get("app", "")).strip():
                return None, f"slot {i + 1}: enter an app name"
            entry["app"] = str(b["app"]).strip()
        elif kind == "volume":
            entry["dir"] = b.get("dir") if b.get("dir") in ("up", "down", "mute") else "up"
        elif kind == "shortcut":
            if not str(b.get("name", "")).strip():
                return None, f"slot {i + 1}: enter the Shortcut's name"
            entry["name"] = str(b["name"]).strip()
        elif kind in ("applescript", "shell"):
            if not str(b.get("command", "")).strip():
                return None, f"slot {i + 1}: enter a command"
            entry["command"] = str(b["command"])
        clean.append(entry)
    return clean, None


# --------------------------------------------------------------- server ----
MIME = {".html": "text/html; charset=utf-8", ".js": "text/javascript", ".css": "text/css",
        ".svg": "image/svg+xml", ".png": "image/png", ".json": "application/json"}


def open_url(url):
    ok_prefixes = ("slack://", "msteams:", "https://teams.microsoft.com/", "https://app.slack.com/", "https://slack.com/")
    if not (url.startswith(ok_prefixes) or re.match(r"https://[a-z0-9-]+\.slack\.com/", url)):
        return False, "only Slack and Teams links are opened"
    subprocess.Popen([OPEN, url], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    return True, "opened"


def make_handler(state, cfg, feeds_mgr=None, events=None):
    events = events or EventStore(Path(tempfile.mkdtemp()) / "events.json")

    def buttons():
        return {b["id"]: b for b in cfg["buttons"]}

    class Handler(BaseHTTPRequestHandler):
        server_version = "pc-stats-panel-mac/2.0"

        def _json(self, code, obj):
            body = json.dumps(obj).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _body(self):
            n = int(self.headers.get("Content-Length") or 0)
            if n <= 0 or n > 200_000:
                return None
            try:
                return json.loads(self.rfile.read(n).decode("utf-8"))
            except Exception:
                return None

        def do_GET(self):
            path = urlparse(self.path).path
            if path == "/api/stats":
                return self._json(200, state.get())
            if path == "/api/health":
                ds = list_displays()
                pw, ph = cfg["panel_resolution"]
                panel = next((d for d in ds if {(d["w"], d["h"]), (d["px_w"], d["px_h"])} & {(pw, ph), (ph, pw)}), None)
                return self._json(200, {"ok": True, "sensors": state.get().get("sensors"), "caps": state.caps, "displays": ds,
                                        "touch": state.touch.status() if state.touch else {"state": "off"},
                                        "panel": {"connected": bool(panel), "main": bool(panel and panel["main"]), "position": cfg.get("panel_position", "above"), "keep": cfg.get("keep_panel_for_dock", True)}})
            if path == "/api/config":
                return self._json(200, {"pc_name": cfg["_pc_name"], "buttons": cfg["buttons"], "cfg_version": state.cfg_version, "platform": "mac",
                                        "right_side": cfg.get("right_side", "feeds"), "feeds": public_feeds(cfg), "app_presets": list(APP_PRESETS),
                                        "panel_position": cfg.get("panel_position", "above"), "keep_panel_for_dock": cfg.get("keep_panel_for_dock", True)})
            if path == "/api/feeds":
                return self._json(200, {"right_side": cfg.get("right_side", "feeds"), "feeds": feeds_mgr.snapshot() if feeds_mgr else []})
            if path == "/api/events":
                q = urllib.parse.parse_qs(urlparse(self.path).query)
                limit = max(1, min(int((q.get("limit") or ["30"])[0]), 100))
                return self._json(200, {"events": events.list(limit), "unseen": events.unseen()})
            m = re.match(r"^/api/feeds/([\w-]+)/login$", path)
            if m and feeds_mgr:
                f = feeds_mgr.get(m.group(1))
                if not isinstance(f, TeamsFeed):
                    return self._json(404, {"ok": False, "message": "that feed is not a Teams feed"})
                try:
                    return self._json(200, {"ok": True, **f.login_poll()})
                except Exception as exc:
                    return self._json(500, {"ok": False, "message": str(exc)})
            if path == "/":
                path = "/index.html"
            if path == "/admin":
                path = "/admin.html"
            file = (DASHBOARD / path.lstrip("/")).resolve()
            if DASHBOARD.resolve() not in file.parents or not file.is_file():
                return self._json(404, {"error": "not found"})
            data = file.read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", MIME.get(file.suffix, "application/octet-stream"))
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def do_POST(self):
            path = urlparse(self.path).path
            if path.startswith("/api/action/"):
                btn = buttons().get(path.rsplit("/", 1)[-1])
                if not btn:
                    return self._json(404, {"ok": False, "message": "no such button"})
                ok, msg = run_action(btn, state)
                log(f"[action] {btn['id']}: {msg}")
                return self._json(200 if ok else 500, {"ok": ok, "message": msg})
            if path == "/api/action":
                body = self._body()
                clean, err = validate_buttons([body] if isinstance(body, dict) else [])
                if err or not clean:
                    return self._json(400, {"ok": False, "message": err or "bad request"})
                ok, msg = run_action(clean[0], state)
                log(f"[action:test] {msg}")
                return self._json(200 if ok else 500, {"ok": ok, "message": msg})
            if path == "/api/admin/feeds":
                body = self._body() or {}
                right, clean, err = validate_feeds(body.get("right_side", cfg.get("right_side", "feeds")), body.get("feeds", []), cfg.get("feeds"))
                if err:
                    return self._json(400, {"ok": False, "message": err})
                cfg["right_side"], cfg["feeds"] = right, clean
                try:
                    save_config(cfg)
                except OSError as exc:
                    return self._json(500, {"ok": False, "message": f"could not write config.json: {exc}"})
                if feeds_mgr:
                    feeds_mgr.reload(clean)
                state.cfg_version += 1
                log(f"[admin] saved right side: {right}, {len(clean)} feeds (version {state.cfg_version})")
                return self._json(200, {"ok": True, "cfg_version": state.cfg_version, "right_side": right, "feeds": public_feeds(cfg)})
            if path in ("/api/events", "/api/events/claude-code", "/api/events/codex"):
                body = self._body()
                if not isinstance(body, dict):
                    return self._json(400, {"ok": False, "message": "JSON body expected"})
                ev = {"/api/events": from_generic, "/api/events/claude-code": from_claude_code, "/api/events/codex": from_codex}[path](body)
                stored = events.add(ev)
                log(f"[event] {stored['tool']} · {stored['state']} · {stored['project']} ({(stored.get('focus') or {}).get('kind')})")
                return self._json(200, {"ok": True, "id": stored["id"]})
            m = re.match(r"^/api/events/([0-9a-f]{10})/(focus|dismiss)$", path)
            if m:
                ev = events.get(m.group(1))
                if not ev:
                    return self._json(404, {"ok": False, "message": "no such event"})
                if m.group(2) == "dismiss":
                    events.dismiss(ev["id"])
                    return self._json(200, {"ok": True})
                ok, msg = focus_window(ev.get("focus") or {}, DRY_RUN)
                events.mark_seen(ev["id"])
                log(f"[focus] {ev['tool']} · {ev['project']}: {msg}")
                return self._json(200 if ok else 500, {"ok": ok, "message": msg})
            if path == "/api/events/clear":
                events.clear()
                return self._json(200, {"ok": True})
            if path == "/api/admin/install-ai-hooks":
                try:
                    r = subprocess.run([sys.executable, str(HERE / "hooks" / "install_hooks.py")] + (["--uninstall"] if (self._body() or {}).get("uninstall") else []),
                                       capture_output=True, text=True, timeout=20)
                    return self._json(200 if r.returncode == 0 else 500, {"ok": r.returncode == 0, "result": json.loads(r.stdout or "{}"), "message": r.stderr.strip()[:300]})
                except Exception as exc:
                    return self._json(500, {"ok": False, "message": str(exc)})
            if path == "/api/admin/arrange":
                body = self._body() or {}
                if body.get("position") in arrange.POSITIONS:
                    cfg["panel_position"] = body["position"]
                if "keep" in body:
                    cfg["keep_panel_for_dock"] = bool(body["keep"])
                if body.get("position") in arrange.POSITIONS or "keep" in body:
                    try:
                        save_config(cfg)
                    except OSError:
                        pass
                ok, msg = keep_panel_for_dock(cfg, state, force=True)
                return self._json(200, {"ok": True, "changed": ok, "message": msg, "position": cfg["panel_position"], "keep": cfg["keep_panel_for_dock"]})
            if path == "/api/admin/open":
                url = f"http://localhost:{self.server.server_address[1]}/admin"
                subprocess.Popen([OPEN, url], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)   # our own page, not a feed link
                return self._json(200, {"ok": True, "message": "admin page opened on your Mac"})
            if path == "/api/admin/open-fda":
                subprocess.Popen([OPEN, FULL_DISK_PANE], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                return self._json(200, {"ok": True, "message": "opened Full Disk Access settings"})
            if path == "/api/feeds/open":
                body = self._body() or {}
                ok, msg = open_url(str(body.get("url", "")))
                return self._json(200 if ok else 400, {"ok": ok, "message": msg})
            m = re.match(r"^/api/feeds/([\w-]+)/(refresh|login)$", path)
            if m and feeds_mgr:
                f = feeds_mgr.get(m.group(1))
                if not f:
                    return self._json(404, {"ok": False, "message": "no such feed"})
                if m.group(2) == "refresh":
                    return self._json(200, {"ok": True, "feed": feeds_mgr.refresh_now(m.group(1))})
                if not isinstance(f, TeamsFeed):
                    return self._json(400, {"ok": False, "message": "sign-in is only for Teams feeds"})
                try:
                    info = f.login_start()
                    if info.get("url"):
                        subprocess.Popen([OPEN, info["url"]], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                    return self._json(200, {"ok": True, **info})
                except Exception as exc:
                    return self._json(500, {"ok": False, "message": str(exc)})
            if path == "/api/admin/config":
                body = self._body()
                clean, err = validate_buttons((body or {}).get("buttons"))
                if err:
                    return self._json(400, {"ok": False, "message": err})
                cfg["buttons"] = clean
                try:
                    save_config(cfg)
                except OSError as exc:
                    return self._json(500, {"ok": False, "message": f"could not write config.json: {exc}"})
                state.cfg_version += 1
                log(f"[admin] saved {len(clean)} buttons (version {state.cfg_version})")
                return self._json(200, {"ok": True, "cfg_version": state.cfg_version, "buttons": clean})
            return self._json(404, {"ok": False})

        def log_message(self, fmt, *args):
            if args and ("/api/stats" in str(args[0]) or "/api/health" in str(args[0])):
                return
            sys.stderr.write("%s - %s\n" % (self.address_string(), fmt % args))

    return Handler


def run_later(fn, *args):
    threading.Thread(target=fn, args=args, daemon=True).start()


def park_cursor_on_main(state):
    """A tap on the panel leaves the mouse cursor there, and macOS aims Spaces, Mission Control and
    Spotlight at the display under the cursor. Put it back where it was on the main display."""
    if not state.kiosk or DRY_RUN:
        return
    try:
        ds = list_displays()
        main = next((d for d in ds if d["main"]), None)
        if not main or len(ds) < 2:
            return
        x, y = arrange.cursor_position()
        if arrange.inside(main, x, y):
            return
        arrange.wait_button_up()      # let the finger's lift land on the panel first
        tx, ty = state.last_main_cursor or (main["x"] + main["w"] / 2, main["y"] + main["h"] / 2)
        arrange.warp_cursor(tx, ty)
    except Exception as exc:
        log(f"[cursor] {type(exc).__name__}: {exc}")


def focus_main_display(state):
    """The tap also makes the dashboard window the active one; put the main display's app back in front
    so keyboard focus returns to what you were using."""
    if not state.kiosk or DRY_RUN:
        return
    try:
        kiosk_pids = state.kiosk._pids()
        front = arrange.front_pid()
        if front is None or front not in kiosk_pids:
            return                                   # the dashboard is not the active app: nothing to undo
        if state.last_front_pid and state.last_front_pid not in kiosk_pids:
            arrange.activate_pid(state.last_front_pid)     # the app that was active before the tap
            return
        main = next((d for d in list_displays() if d["main"]), None)
        if main:
            arrange.activate_main_app(main, kiosk_pids)      # no history yet: whatever is on top of the main display
    except Exception as exc:
        log(f"[focus] {type(exc).__name__}: {exc}")


def keep_panel_for_dock(cfg, state, force=False):
    """If the panel has become the main display (macOS does that on first plug-in), put the big
    monitor back as main, park the panel, and move windows that landed on the panel back."""
    w, h = cfg["panel_resolution"]
    ds = list_displays()
    panel = next((d for d in ds if {(d["w"], d["h"]), (d["px_w"], d["px_h"])} & {(w, h), (h, w)}), None)
    if not panel or len(ds) < 2:
        return False, "panel not connected" if not panel else "only one display"
    if not force and not panel["main"] and state.arranged_for == panel["id"]:
        return False, "already arranged"
    changed, msg = arrange.arrange((w, h), cfg.get("panel_position", "above"), DRY_RUN)
    notes = [msg]
    if changed and not DRY_RUN:
        time.sleep(2.5)
        ds = list_displays()
        panel = next((d for d in ds if d["id"] == panel["id"]), panel)
    main = next((d for d in ds if d["main"] and d["id"] != panel["id"]), None)
    if main and not panel["main"]:
        ok, m = arrange.sweep(panel, main, DRY_RUN, skip_pids=state.kiosk._pids() if state.kiosk else ())
        notes.append(m)
    state.arranged_for = panel["id"]
    log("[display] " + "; ".join(notes))
    return True, "; ".join(notes)


def kiosk_loop(kiosk, cfg, state):
    while True:
        try:
            ds = list_displays()
            w, h = cfg["panel_resolution"]
            panel = next((d for d in ds if {(d["w"], d["h"]), (d["px_w"], d["px_h"])} & {(w, h), (h, w)}), None)
            if cfg.get("keep_panel_for_dock", True) and panel and (panel["main"] or state.arranged_for != panel["id"]) and len(ds) > 1:
                keep_panel_for_dock(cfg, state)
                panel = next((d for d in list_displays() if d["id"] == panel["id"]), panel)
            if not panel:
                state.arranged_for = None
            if state.touch:
                state.touch.tick(panel, DRY_RUN)
            main = next((d for d in ds if d["main"]), None)
            if main and panel and not DRY_RUN:
                x, y = arrange.cursor_position()
                if arrange.inside(main, x, y):
                    state.last_main_cursor = (x, y)
                front = arrange.front_pid()
                if front and front not in kiosk._pids():
                    state.last_front_pid = front
            kiosk.tick()
        except Exception as exc:
            log(f"[kiosk] {type(exc).__name__}: {exc}")
        time.sleep(3)


def main():
    global DRY_RUN
    ap = argparse.ArgumentParser(description="PC Stats Panel agent (macOS)")
    ap.add_argument("--dry-run", action="store_true", help="log actions instead of executing them")
    ap.add_argument("--no-kiosk", action="store_true", help="do not open the browser window on the panel")
    ap.add_argument("--port", type=int)
    ap.add_argument("--grant", action="store_true", help="only ask macOS for the Accessibility permission, then exit")
    args = ap.parse_args()
    DRY_RUN = args.dry_run

    if args.grant:
        trusted = keys_mac.ax_trusted(prompt=True)
        msg = "Accessibility: " + ("granted" if trusted else "not granted yet; the system dialog should be on screen") + \
              (" (via launcher)" if keys_mac.via_launcher() else " (python process)")
        log("[grant] " + msg)
        if not trusted:
            subprocess.Popen([OPEN, ACCESSIBILITY_PANE], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        return

    if platform.system() != "Darwin":
        sys.exit("This agent is for macOS.")
    cfg = load_config()
    port = args.port or cfg["port"]
    state = State()
    sensors = MacSensors(cfg["poll_ms"])
    threading.Thread(target=poll_loop, args=(state, cfg, sensors), daemon=True).start()
    if cfg.get("kiosk", True) and not args.no_kiosk:
        w, h = cfg["panel_resolution"]
        state.kiosk = Kiosk(f"http://127.0.0.1:{port}/", w, h, log)
        state.touch = touch_mod.TouchMapper((w, h), log)
        threading.Thread(target=kiosk_loop, args=(state.kiosk, cfg, state), daemon=True).start()
    events = EventStore()
    feeds_mod.EVENT_STORE = events
    feeds_mgr = FeedManager(cfg["feeds"], log)
    httpd = ThreadingHTTPServer(("127.0.0.1", port), make_handler(state, cfg, feeds_mgr, events))
    log(f"PC Stats Panel agent on http://localhost:{port}  (admin at /admin; sensors: "
        f"{'macmon' if sensors.stream.path else 'basic, install macmon for temperatures'}; "
        f"{'DRY RUN' if DRY_RUN else 'live'}; config {CONFIG_PATH})")
    if not DRY_RUN:
        trusted = keys_mac.ax_trusted(prompt=True)   # asks once at start, lists the app in Accessibility
        state.last_prompt = time.time()
        log("[permission] Accessibility: " + ("granted, key buttons work" if trusted else
            "not granted yet — switch on “PC Stats Panel” under System Settings › Privacy & Security › Accessibility"))
        if not trusted:
            subprocess.Popen([OPEN, ACCESSIBILITY_PANE], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
