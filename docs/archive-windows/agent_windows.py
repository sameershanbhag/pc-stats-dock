#!/usr/bin/env python3
"""PC Stats Panel agent.

- polls LibreHardwareMonitor's web server (http://localhost:8085/data.json)
- serves the dashboard on http://localhost:4400 and the button editor on /admin
- turns touch buttons into actions (hotkeys, app launch, mic mute, volume)

Run:  python agent.py            (on the gaming PC, LibreHardwareMonitor running)
      python agent.py --demo     (anywhere; fake data, actions only logged)
"""
import argparse
import json
import math
import os
import platform
import random
import shlex
import subprocess
import sys
import threading
import time
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

HERE = Path(__file__).resolve().parent
DASHBOARD = HERE.parent / "dashboard"
sys.path.insert(0, str(HERE))
from sensors import parse_lhm  # noqa: E402

IS_WINDOWS = platform.system() == "Windows"


def load_config():
    with open(HERE / "config.json", encoding="utf-8") as f:
        cfg = json.load(f)
    if not cfg.get("pc_name"):
        cfg["pc_name"] = platform.node()
    return cfg


# ---------------------------------------------------------------- state ----
class State:
    def __init__(self):
        self.lock = threading.Lock()
        self.stats = {}
        self.lhm_ok = False
        self.mic_muted = None
        self.cfg_version = 1

    def set(self, stats):
        with self.lock:
            self.stats = stats

    def get(self):
        with self.lock:
            return dict(self.stats)


def demo_stats(t0):
    """Plausible fake numbers so the dashboard can be seen without hardware."""
    t = time.time() - t0
    cpu_load = 22 + 18 * math.sin(t / 7) + random.uniform(-4, 4)
    gpu_load = 55 + 40 * math.sin(t / 11 + 1) + random.uniform(-5, 5)
    cpu_load = max(2, min(100, cpu_load))
    gpu_load = max(0, min(100, gpu_load))
    return {
        "cpu": {"name": "Demo CPU 16-core", "load": round(cpu_load, 1), "temp": round(42 + cpu_load * 0.42, 1),
                "clock": round(4200 + cpu_load * 9), "power": round(35 + cpu_load * 1.1, 1)},
        "gpu": {"name": "Demo GPU 16 GB", "load": round(gpu_load, 1), "temp": round(38 + gpu_load * 0.36, 1),
                "hotspot": round(48 + gpu_load * 0.4, 1), "vram_used_gb": round(3.2 + gpu_load * 0.07, 1),
                "vram_total_gb": 16.0, "fan_rpm": round(700 + gpu_load * 14), "fan_pct": round(25 + gpu_load * 0.5),
                "power": round(40 + gpu_load * 2.6, 1), "clock": round(1400 + gpu_load * 11)},
        "ram": {"used_gb": round(14.6 + math.sin(t / 30) * 1.5, 1), "total_gb": 32.0,
                "load": round((14.6 + math.sin(t / 30) * 1.5) / 32 * 100, 1)},
        "storage": [{"name": "Demo NVMe 2 TB", "temp": round(41 + gpu_load * 0.05, 1)},
                    {"name": "Demo SATA SSD", "temp": 33.0}],
        "net": {"name": "Ethernet", "down_mbps": round(max(0, 12 + 60 * math.sin(t / 5) ** 4), 1),
                "up_mbps": round(max(0, 2 + 6 * random.random()), 1)},
        "fans": [{"name": "CPU Fan", "rpm": round(900 + cpu_load * 9)}, {"name": "Case Fan #1", "rpm": 780}],
    }


def poll_loop(state, cfg, demo):
    t0 = time.time()
    interval = cfg.get("poll_ms", 500) / 1000
    while True:
        payload = None
        if not demo:
            try:
                with urllib.request.urlopen(cfg["lhm_url"], timeout=1.5) as r:
                    payload = parse_lhm(json.load(r))
                state.lhm_ok = True
            except Exception:
                state.lhm_ok = False
        if payload is None:
            payload = demo_stats(t0)
        payload["ts"] = int(time.time() * 1000)
        payload["demo"] = demo or not state.lhm_ok
        payload["lhm_ok"] = state.lhm_ok
        payload["pc_name"] = cfg["pc_name"]
        payload["mic_muted"] = mic_state()
        payload["cfg_version"] = state.cfg_version
        state.set(payload)
        time.sleep(interval)


# -------------------------------------------------------------- actions ----
_mic_cache = {"t": 0, "muted": None}


def _mic_volume():
    from comtypes import CLSCTX_ALL, POINTER, cast  # type: ignore
    from pycaw.pycaw import AudioUtilities, IAudioEndpointVolume  # type: ignore
    mic = AudioUtilities.GetMicrophone()
    iface = mic.Activate(IAudioEndpointVolume._iid_, CLSCTX_ALL, None)
    return cast(iface, POINTER(IAudioEndpointVolume))


def mic_state():
    if not IS_WINDOWS:
        return None
    if time.time() - _mic_cache["t"] < 2:
        return _mic_cache["muted"]
    try:
        _mic_cache["muted"] = bool(_mic_volume().GetMute())
    except Exception:
        _mic_cache["muted"] = None
    _mic_cache["t"] = time.time()
    return _mic_cache["muted"]


def run_action(btn):
    """Returns (ok, message)."""
    kind = btn.get("type")
    if not IS_WINDOWS:
        return True, f"logged only (not Windows): {kind} {btn.get('keys') or btn.get('path') or btn.get('dir') or ''}"
    try:
        if kind == "hotkey":
            import pyautogui  # type: ignore
            pyautogui.hotkey(*btn["keys"])
            return True, "sent " + "+".join(btn["keys"])
        if kind == "volume":
            import pyautogui  # type: ignore
            key = {"up": "volumeup", "down": "volumedown", "mute": "volumemute"}[btn.get("dir", "up")]
            pyautogui.press(key)
            return True, key
        if kind == "launch":
            args = (btn.get("args") or "").strip()
            if args:
                subprocess.Popen([btn["path"]] + shlex.split(args, posix=False))
            else:
                os.startfile(btn["path"])  # type: ignore[attr-defined]
            return True, "launched " + btn["path"]
        if kind == "empty":
            return True, "blank slot"
        if kind == "powershell":
            subprocess.Popen(["powershell", "-NoProfile", "-WindowStyle", "Hidden", "-Command", btn["command"]])
            return True, "ran powershell"
        if kind == "mic":
            vol = _mic_volume()
            vol.SetMute(not vol.GetMute(), None)
            _mic_cache["t"] = 0
            return True, "mic " + ("muted" if vol.GetMute() else "live")
        return False, f"unknown action type {kind}"
    except Exception as exc:  # report, never crash the server
        return False, f"{type(exc).__name__}: {exc}"


# ---------------------------------------------------------------- config ----
VALID_TYPES = {"hotkey", "launch", "volume", "mic", "powershell", "empty"}


def validate_buttons(buttons):
    """Returns (clean_list, error_message)."""
    if not isinstance(buttons, list) or len(buttons) > 12:
        return None, "buttons must be a list of at most 12 entries"
    clean, seen = [], set()
    for i, b in enumerate(buttons):
        if not isinstance(b, dict):
            return None, f"slot {i + 1}: not an object"
        kind = b.get("type", "empty")
        if kind not in VALID_TYPES:
            return None, f"slot {i + 1}: unknown type {kind!r}"
        entry = {"id": str(b.get("id") or f"slot-{i + 1}"), "type": kind,
                 "label": str(b.get("label", ""))[:16], "sub": str(b.get("sub", ""))[:20], "glyph": str(b.get("glyph", ""))[:2]}
        if entry["id"] in seen:
            entry["id"] = f"slot-{i + 1}"
        seen.add(entry["id"])
        if kind == "hotkey":
            keys = [str(k).lower() for k in (b.get("keys") or []) if str(k).strip()]
            if not keys:
                return None, f"slot {i + 1}: hotkey needs at least one key"
            entry["keys"] = keys
        elif kind == "launch":
            if not b.get("path"):
                return None, f"slot {i + 1}: launch needs a path"
            entry["path"] = str(b["path"]); entry["args"] = str(b.get("args", ""))
        elif kind == "volume":
            entry["dir"] = b.get("dir") if b.get("dir") in ("up", "down", "mute") else "up"
        elif kind == "powershell":
            if not str(b.get("command", "")).strip():
                return None, f"slot {i + 1}: PowerShell needs a command"
            entry["command"] = str(b["command"])
        clean.append(entry)
    return clean, None


def save_config(cfg):
    path = HERE / "config.json"
    tmp = path.with_suffix(".json.tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(cfg, f, indent=2, ensure_ascii=False)
    os.replace(tmp, path)


# --------------------------------------------------------------- server ----
MIME = {".html": "text/html; charset=utf-8", ".js": "text/javascript", ".css": "text/css",
        ".svg": "image/svg+xml", ".png": "image/png", ".woff2": "font/woff2", ".json": "application/json"}


def make_handler(state, cfg):
    def buttons():
        return {b["id"]: b for b in cfg["buttons"]}

    class Handler(BaseHTTPRequestHandler):
        server_version = "pc-stats-panel/1.1"

        def _body(self):
            n = int(self.headers.get("Content-Length") or 0)
            if n <= 0 or n > 200_000:
                return None
            try:
                return json.loads(self.rfile.read(n).decode("utf-8"))
            except Exception:
                return None

        def _json(self, code, obj):
            body = json.dumps(obj).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            path = urlparse(self.path).path
            if path == "/api/stats":
                return self._json(200, state.get())
            if path == "/api/health":
                return self._json(200, {"ok": True, "lhm_ok": state.lhm_ok})
            if path == "/api/config":
                return self._json(200, {"pc_name": cfg["pc_name"], "buttons": cfg["buttons"], "cfg_version": state.cfg_version})
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
                ok, msg = run_action(btn)
                print(f"[action] {btn['id']}: {msg}", flush=True)
                return self._json(200 if ok else 500, {"ok": ok, "message": msg})
            if path == "/api/action":  # run an unsaved action once (admin "Test now")
                body = self._body()
                clean, err = validate_buttons([body] if isinstance(body, dict) else [])
                if err or not clean:
                    return self._json(400, {"ok": False, "message": err or "bad request"})
                ok, msg = run_action(clean[0])
                print(f"[action:test] {msg}", flush=True)
                return self._json(200 if ok else 500, {"ok": ok, "message": msg})
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
                print(f"[admin] saved {len(clean)} buttons (version {state.cfg_version})", flush=True)
                return self._json(200, {"ok": True, "cfg_version": state.cfg_version, "buttons": clean})
            return self._json(404, {"ok": False})

        def log_message(self, fmt, *args):  # keep the console quiet
            if "/api/stats" in (args[0] if args else ""):
                return
            sys.stderr.write("%s - %s\n" % (self.address_string(), fmt % args))

    return Handler


def main():
    ap = argparse.ArgumentParser(description="PC Stats Panel agent")
    ap.add_argument("--demo", action="store_true", help="fake sensor data, actions only logged")
    ap.add_argument("--port", type=int, help="override port from config.json")
    args = ap.parse_args()

    cfg = load_config()
    port = args.port or cfg.get("port", 4400)
    state = State()
    threading.Thread(target=poll_loop, args=(state, cfg, args.demo), daemon=True).start()
    httpd = ThreadingHTTPServer(("127.0.0.1", port), make_handler(state, cfg))
    print(f"PC Stats Panel agent on http://localhost:{port}  ({'DEMO' if args.demo else 'live'}; "
          f"sensors from {cfg['lhm_url']})", flush=True)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
