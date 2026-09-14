"""Keeps the menu bar item (mac/menubar.m, built as PCStatsMenu next to the launcher) running while the agent runs."""
import os
import subprocess
import time
from pathlib import Path

RETRY_AFTER = 30      # seconds before starting it again after it died


def helper_path():
    """The helper lives next to the launcher executable (inside the app bundle); None when there is no launcher."""
    launcher = os.environ.get("PCSTATS_LAUNCHER")
    if not launcher:
        return None
    p = Path(launcher).parent / "PCStatsMenu"
    return p if p.exists() else None


class MenuBar:
    def __init__(self, port, log=print, exe=None):
        self.port, self.log = port, log
        self.exe = exe if exe is not None else helper_path()
        self.proc = None
        self.started_at = 0.0
        self.announced = False

    def running(self):
        return self.proc is not None and self.proc.poll() is None

    def tick(self, enabled=True):
        if not enabled:
            self.stop()
            return
        if self.running():
            return
        if self.exe is None:
            if not self.announced:
                self.announced = True
                self.log("[menu] no menu bar helper next to the launcher; skipping the menu bar item")
            return
        if time.time() - self.started_at < RETRY_AFTER:
            return
        try:
            env = dict(os.environ, PCSTATS_PORT=str(self.port))
            self.proc = subprocess.Popen([str(self.exe)], env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            self.started_at = time.time()
            if not self.announced:
                self.announced = True
                self.log("[menu] menu bar item started")
        except OSError as exc:
            self.started_at = time.time()
            self.log(f"[menu] could not start the menu bar item: {exc}")

    def stop(self):
        if self.running():
            self.proc.terminate()
            try:
                self.proc.wait(timeout=3)
            except subprocess.TimeoutExpired:
                self.proc.kill()
        self.proc = None
