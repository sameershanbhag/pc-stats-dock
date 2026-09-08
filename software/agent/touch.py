"""Keep touches aimed at the panel.

macOS points a USB touchscreen at the main display. While the panel is not the main display the
launcher's ``--touch-map`` mode (an event tap, so it needs the same Accessibility permission as the
key buttons) relocates the touchscreen's events onto the panel. This module keeps that helper
running while a non-main panel is connected and reports what it says.
"""
import os
import subprocess
import threading
import time
from datetime import datetime
from pathlib import Path

DEFAULT_LAUNCHER = Path.home() / "Applications" / "PC Stats Panel.app" / "Contents" / "MacOS" / "PCStatsPanel"
LOG_PATH = Path.home() / "Library" / "Logs" / "pc-stats-dock" / "touch.log"
RETRY_AFTER = 30          # seconds between attempts after the helper exits
NEEDS_PERMISSION = 3      # helper exit code when the event tap could not be created


def launcher_path():
    p = os.environ.get("PCSTATS_LAUNCHER")
    if p and os.path.exists(p):
        return p
    return str(DEFAULT_LAUNCHER) if DEFAULT_LAUNCHER.exists() else None


def parse_status(line):
    """'status devices=1 panel=1 ... name=wcidtest' -> dict (ints where they parse; name keeps spaces)."""
    out = {}
    body = line[len("status "):] if line.startswith("status ") else line
    if " name=" in body or body.startswith("name="):
        body, name = body.split("name=", 1)
        out["name"] = name.strip()
    for tok in body.split():
        if "=" in tok:
            k, v = tok.split("=", 1)
            out[k] = int(v) if v.lstrip("-").isdigit() else v
    return out


class TouchMapper:
    def __init__(self, panel_res, log=print, logfile=LOG_PATH, launcher=None):
        self.w, self.h = panel_res
        self.log = log
        self.logfile = Path(logfile) if logfile else None
        self.launcher = launcher if launcher is not None else launcher_path()
        self.proc = None
        self.last = {}
        self.exit_code = None
        self.exit_at = 0.0
        self.reason = "waiting for panel"
        self._state = None
        self.lock = threading.Lock()

    # -- lifecycle -------------------------------------------------------------------------
    def tick(self, panel, dry_run=False):
        """Called every few seconds with the panel display (or None)."""
        if not panel:
            self.reason = "waiting for panel"
        elif panel.get("main"):
            self.reason = "panel is the main display"
        else:
            self.reason = None
        if self.reason or dry_run or not self.launcher:
            self.stop()
        elif not self.running() and time.time() - self.exit_at >= RETRY_AFTER:
            self._start()
        self._announce()

    def running(self):
        return self.proc is not None and self.proc.poll() is None

    def _start(self):
        try:
            self.proc = subprocess.Popen([self.launcher, "--touch-map", str(self.w), str(self.h)],
                                         stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
        except OSError as exc:
            self.exit_code, self.exit_at = -1, time.time()
            self._write(f"could not start the touch mapper: {exc}")
            return
        self.exit_code, self.last = None, {}
        threading.Thread(target=self._pump, args=(self.proc,), daemon=True).start()

    def _pump(self, proc):
        for line in proc.stdout:
            line = line.rstrip("\n")
            if line.startswith("status "):
                self.last = parse_status(line)
            elif line:
                self._write(line)
        proc.wait()
        with self.lock:
            if proc is self.proc:
                self.exit_code, self.exit_at = proc.returncode, time.time()
        if proc.returncode:
            self._write(f"touch mapper exited with code {proc.returncode}")

    def stop(self):
        if self.running():
            self.proc.terminate()
            try:
                self.proc.wait(timeout=3)
            except subprocess.TimeoutExpired:
                self.proc.kill()
        self.exit_code = None

    def _write(self, line):
        if not self.logfile:
            return
        try:
            self.logfile.parent.mkdir(parents=True, exist_ok=True)
            with open(self.logfile, "a", encoding="utf-8") as f:
                f.write(f"{datetime.now():%Y-%m-%d} {line}\n")
        except OSError:
            pass

    # -- reporting -------------------------------------------------------------------------
    def status(self):
        if not self.launcher:
            return {"state": "off", "note": "launcher not installed; run mac/install.sh"}
        if self.reason:
            return {"state": "off", "note": self.reason}
        if self.running():
            s = self.last
            if not s:
                return {"state": "starting"}
            if not s.get("devices"):
                return {"state": "no touchscreen", "note": "no USB touchscreen is connected"}
            if not s.get("panel"):
                return {"state": "waiting for panel"}
            return {"state": "mapped", "device": s.get("name", ""), "touches": s.get("touches", s.get("mapped", 0))}
        if self.exit_code == NEEDS_PERMISSION:
            return {"state": "needs accessibility", "note": "allow PC Stats Panel under Privacy & Security > Accessibility"}
        if self.exit_code:
            return {"state": "error", "note": f"touch mapper exited with code {self.exit_code}; see touch.log"}
        return {"state": "starting"}

    def _announce(self):
        st = self.status()["state"]
        if st != self._state and st not in ("starting",):
            self._state = st
            note = self.status().get("note") or self.status().get("device") or ""
            self.log(f"[touch] {st}" + (f" ({note})" if note else ""))
