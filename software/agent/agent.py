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
from displays import Kiosk, list_displays, find_panel as displays_find_panel, unassigned_displays, give_desktop  # noqa: E402
import arrange  # noqa: E402
import touch as touch_mod
import idle as idle_mod
import menubar as menubar_mod
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


FACE_DEFAULTS = {"enabled": True, "idle_min": 3, "on_lock": True, "follow_mouse": True, "color": "#6FBFC6",
                 "on_video": True, "video_min": 1,      # a playing video keeps the display awake: the face comes sooner
                 "style": "cute"}                       # cute (glowing robot eyes) · glass (realistic) · classic (flat shapes)
FACE_STYLES = ("cute", "glass", "classic")


def face_settings(body, current):
    """Validated face settings from an admin request merged over the current ones."""
    out = dict(current)
    if "enabled" in body:
        out["enabled"] = bool(body["enabled"])
    if "idle_min" in body:
        try:
            out["idle_min"] = max(1, min(180, int(body["idle_min"])))
        except (TypeError, ValueError):
            pass
    if "on_lock" in body:
        out["on_lock"] = bool(body["on_lock"])
    if "follow_mouse" in body:
        out["follow_mouse"] = bool(body["follow_mouse"])
    if "on_video" in body:
        out["on_video"] = bool(body["on_video"])
    if "video_min" in body:
        try:
            out["video_min"] = max(0.5, min(60.0, round(float(body["video_min"]) * 2) / 2))
        except (TypeError, ValueError):
            pass
    if isinstance(body.get("color"), str) and re.fullmatch(r"#[0-9a-fA-F]{6}", body["color"]):
        out["color"] = body["color"]
    if body.get("style") in FACE_STYLES:
        out["style"] = body["style"]
    return out


def idle_fields(state):
    """What the face needs from the agent: seconds idle, lock state, where the mouse is on the main display."""
    now = time.time()
    if now - state.displays_at > 3:
        try:
            state.displays_cache = list_displays()
        except Exception:
            state.displays_cache = []
        state.displays_at = now
    gaze = None
    main = next((d for d in state.displays_cache if d.get("main")), None)
    if main and not DRY_RUN:
        try:
            x, y = arrange.cursor_position()
            if arrange.inside(main, x, y):
                gaze = {"x": round((x - main["x"]) / max(1, main["w"]), 3), "y": round((y - main["y"]) / max(1, main["h"]), 3)}
        except Exception:
            gaze = None
    if now - state.video_at > 5:                        # pmset is a helper process: not on every poll
        state.video_cache = idle_mod.display_awake_holder()
        state.video_at = now
    return {"idle_s": idle_mod.seconds_idle(), "locked": idle_mod.screen_locked(), "gaze": gaze,
            "video": state.video_cache, "face_preview": now < state.face_preview_until}


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
    cfg.setdefault("menu_bar", True)                # the gauge icon in the menu bar (admin page, face preview, restart)
    cfg.setdefault("panel_id", "")                  # the panel's display identity, learned on first sight (survives wrong resolutions)
    cfg.setdefault("panel_name", "T101F")           # what macOS calls the panel (its EDID name); the Magedok T101F by default
    face = cfg.setdefault("face", {})               # the idle face: eyes on the panel when the Mac is left alone
    for k, v in FACE_DEFAULTS.items():
        face.setdefault(k, v)
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
        self.face_preview_until = 0.0  # admin page: show the idle face on the panel for a moment
        self.displays_cache, self.displays_at = [], 0.0
        self.video_cache, self.video_at = None, 0.0   # who keeps the display awake (a playing video)
        self.menu = None               # the menu bar item supervisor (set in main)
        self.panel = None              # the panel display as last located (for the kiosk)
        self.unassigned = None         # a connected display without a desktop of its own (id) and whether we tried
        self.unassigned_tried = set()
        self.panel_screens = ([], None)   # displayplacer screens for a given set of display ids (cache)
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
        try:
            payload.update(idle_fields(state))
        except Exception as exc:
            log(f"[idle] {type(exc).__name__}: {exc}")
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
                panel = locate_panel(cfg, state, ds)
                return self._json(200, {"ok": True, "sensors": state.get().get("sensors"), "caps": state.caps, "displays": ds,
                                        "touch": state.touch.status() if state.touch else {"state": "off"},
                                        "panel": {"connected": bool(panel), "main": bool(panel and panel["main"]), "position": cfg.get("panel_position", "above"), "keep": cfg.get("keep_panel_for_dock", True),
                                                  "resolution": [panel["w"], panel["h"]] if panel else None, "native": [pw, ph], "remembered": bool(cfg.get("panel_id")),
                                                  "unassigned": bool(state.unassigned) and not panel}})
            if path == "/api/config":
                return self._json(200, {"pc_name": cfg["_pc_name"], "buttons": cfg["buttons"], "cfg_version": state.cfg_version, "platform": "mac",
                                        "right_side": cfg.get("right_side", "feeds"), "feeds": public_feeds(cfg), "app_presets": list(APP_PRESETS),
                                        "panel_position": cfg.get("panel_position", "above"), "keep_panel_for_dock": cfg.get("keep_panel_for_dock", True),
                                        "face": dict(cfg.get("face", FACE_DEFAULTS)), "menu_bar": bool(cfg.get("menu_bar", True))})
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
                uninstall = bool((self._body() or {}).get("uninstall"))
                try:
                    r = subprocess.run([sys.executable, str(HERE / "hooks" / "install_hooks.py")] + (["--uninstall"] if uninstall else []),
                                       capture_output=True, text=True, timeout=20)
                    result = json.loads(r.stdout or "{}")
                    if r.returncode == 0 and not uninstall and events is not None:
                        result["test"] = hook_self_test(events, self.server.server_address[1])
                    return self._json(200 if r.returncode == 0 else 500, {"ok": r.returncode == 0, "result": result, "message": r.stderr.strip()[:300]})
                except Exception as exc:
                    return self._json(500, {"ok": False, "message": str(exc)})
            if path == "/api/admin/face":
                body = self._body() or {}
                cfg["face"] = face_settings(body, cfg.get("face", FACE_DEFAULTS))
                try:
                    save_config(cfg)
                except OSError as exc:
                    return self._json(500, {"ok": False, "message": f"could not save: {exc}"})
                state.cfg_version += 1
                return self._json(200, {"ok": True, "message": "saved", "face": cfg["face"]})
            if path == "/api/admin/menu":
                body = self._body() or {}
                cfg["menu_bar"] = bool(body.get("enabled", True))
                try:
                    save_config(cfg)
                except OSError as exc:
                    return self._json(500, {"ok": False, "message": f"could not save: {exc}"})
                state.cfg_version += 1
                return self._json(200, {"ok": True, "message": "menu bar icon " + ("on" if cfg["menu_bar"] else "off"), "menu_bar": cfg["menu_bar"]})
            if path == "/api/admin/face/preview":
                state.face_preview_until = time.time() + 20
                return self._json(200, {"ok": True, "message": "the face is on the panel for 20 seconds"})
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


def hook_self_test(events, port):
    """Send one event through the installed hook script, the way a tool would, and see whether it arrives."""
    home = Path(os.environ.get("PCSTATS_HOME") or Path.home())
    script = home / "Library" / "Application Support" / "pc-stats-dock" / "hooks" / "notify.py"
    if not script.exists():
        return "not delivered (hook scripts missing)"
    stamp = f"test-{int(time.time())}"
    env = dict(os.environ, PCSTATS_AGENT=f"http://127.0.0.1:{port}")
    try:
        subprocess.run([sys.executable, str(script), "--tool", "Hook test", "--title", "Hooks are working", "--text", "Chats will appear here when they finish", "--session", stamp],
                       env=env, capture_output=True, timeout=15)
    except (OSError, subprocess.SubprocessError) as exc:
        return f"not delivered ({exc})"
    for _ in range(30):
        if any(e.get("session") == stamp for e in events.list(50)):
            return "delivered"
        time.sleep(0.1)
    return "not delivered"


def repair_ai_hooks():
    """Re-point Claude Code / Codex / Cursor at the hook scripts' stable folder if a registration went stale
    (the app moved or was upgraded). Adds nothing on its own."""
    try:
        r = subprocess.run([sys.executable, str(HERE / "hooks" / "install_hooks.py"), "--repair"], capture_output=True, text=True, timeout=30)
        out = json.loads(r.stdout or "{}") if r.returncode == 0 else {}
        fixed = [k for k, v in out.items() if v == "repaired"]
        if fixed:
            log("[hooks] re-pointed the AI chat hooks for " + ", ".join(fixed) + " at their stable folder")
    except Exception as exc:
        log(f"[hooks] {type(exc).__name__}: {exc}")


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


def check_unassigned(cfg, state, ds):
    """A display is connected but shows macOS's "Choose to Mirror or Extend Display" placeholder, or mirrors another
    display: try once to give it a desktop of its own; say so either way."""
    try:
        pending = unassigned_displays() if not DRY_RUN else []
    except Exception:
        pending = []
    if not pending:
        state.unassigned = None
        return
    d = pending[0]
    state.unassigned = d["id"]
    if d["id"] in state.unassigned_tried:
        return
    state.unassigned_tried.add(d["id"])
    w, h = cfg["panel_resolution"]
    main = next((x for x in ds if x["main"]), None)
    origin = arrange.panel_origin(cfg.get("panel_position", "above"), (main["w"], main["h"]), (w, h)) if main else None
    what = "mirroring another display" if d["mirrors"] else "connected but not assigned a desktop"
    ok, msg = give_desktop(d["id"], (w, h), origin)
    log(f"[display] a display is {what}: {msg}" + ("" if ok else ". If it is the panel: System Settings › Displays › select it › Use as › Extended display"
                                                    " (a MacBook with a base M-series chip drives only one external display with the lid open)"))


def locate_panel(cfg, state, ds=None):
    """The panel among the displays: by its size, or by its identity when macOS gave it another resolution.
    Learns the identity (displayplacer's persistent id) the first time the panel is seen by size."""
    w, h = cfg["panel_resolution"]
    ds = list_displays() if ds is None else ds
    panel = displays_find_panel(w, h, displays=ds)
    ids = tuple(sorted(d["id"] for d in ds))
    if panel or len(ds) < 2 or DRY_RUN:
        if panel and not cfg.get("panel_id") and not DRY_RUN:
            screens = panel_screens(state, ids)
            match = next((sc for sc in screens if sc.get("contextual") == panel["id"]), None)
            if match:
                cfg["panel_id"] = match["persistent"]
                try:
                    save_config(cfg)
                    log(f"[display] remembered the panel: {match.get('type', 'display')} ({match['persistent'][:8]}…)")
                except OSError:
                    pass
        return panel
    screens = panel_screens(state, ids)
    sc = arrange.identify_panel(screens, (w, h), cfg.get("panel_id", ""))
    if not sc:
        sc = arrange.identify_panel(screens, (w, h), "", cfg.get("panel_name", ""), panel_names(state, ids))
    if sc and sc.get("contextual"):
        found = displays_find_panel(w, h, ids=(sc["contextual"],), displays=ds)
        if found and not cfg.get("panel_id") and sc.get("persistent"):
            cfg["panel_id"] = sc["persistent"]                # remember it: next time the identity alone is enough
            try:
                save_config(cfg)
                log(f"[display] remembered the panel: {sc.get('type', 'display')} ({sc['persistent'][:8]}…), running at {found['w']}x{found['h']}")
            except OSError:
                pass
        return found
    return None


def panel_names(state, ids):
    """system_profiler's display names, fetched once per set of connected displays (it takes a second)."""
    cached_ids, names = getattr(state, "panel_names_cache", (None, None))
    if cached_ids == ids and names is not None:
        return names
    names = arrange.display_names()
    state.panel_names_cache = (ids, names)
    return names


def panel_screens(state, ids):
    """displayplacer's view of the screens, fetched once per set of connected displays."""
    cached_ids, screens = state.panel_screens
    if cached_ids == ids and screens is not None:
        return screens
    try:
        screens, _ = arrange.current()
    except Exception:
        screens = None
    state.panel_screens = (ids, screens or [])
    return screens or []


def keep_panel_for_dock(cfg, state, force=False):
    """If the panel has become the main display (macOS does that on first plug-in), put the big
    monitor back as main, park the panel, and move windows that landed on the panel back."""
    w, h = cfg["panel_resolution"]
    ds = list_displays()
    panel = locate_panel(cfg, state, ds)
    if not panel or len(ds) < 2:
        return False, "panel not connected" if not panel else "only one display"
    if not force and not panel["main"] and state.arranged_for == panel["id"]:
        return False, "already arranged"
    changed, msg = arrange.arrange((w, h), cfg.get("panel_position", "above"), DRY_RUN, cfg.get("panel_id", ""))
    if changed:
        state.panel_screens = ([], None)                  # the mode may have changed: re-read next time
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
            panel = locate_panel(cfg, state, ds)
            if cfg.get("keep_panel_for_dock", True) and panel and (panel["main"] or state.arranged_for != panel["id"]) and len(ds) > 1:
                keep_panel_for_dock(cfg, state)
                panel = next((d for d in list_displays() if d["id"] == panel["id"]), panel)
            if not panel:
                state.arranged_for = None
                state.panel_screens = ([], None)
                check_unassigned(cfg, state, ds)
            else:
                state.unassigned = None
            state.panel = panel
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
            if state.menu and not DRY_RUN:
                state.menu.tick(cfg.get("menu_bar", True))
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
        state.kiosk = Kiosk(f"http://127.0.0.1:{port}/", w, h, log, finder=lambda: state.panel)
        state.touch = touch_mod.TouchMapper((w, h), log)
        state.menu = menubar_mod.MenuBar(cfg["port"], log)
        threading.Thread(target=kiosk_loop, args=(state.kiosk, cfg, state), daemon=True).start()
    events = EventStore()
    feeds_mod.EVENT_STORE = events
    feeds_mgr = FeedManager(cfg["feeds"], log)
    httpd = ThreadingHTTPServer(("127.0.0.1", port), make_handler(state, cfg, feeds_mgr, events))
    log(f"PC Stats Panel agent on http://localhost:{port}  (admin at /admin; sensors: "
        f"{'macmon' if sensors.stream.path else 'basic, install macmon for temperatures'}; "
        f"{'DRY RUN' if DRY_RUN else 'live'}; config {CONFIG_PATH})")
    if not DRY_RUN:
        threading.Thread(target=repair_ai_hooks, daemon=True).start()
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
