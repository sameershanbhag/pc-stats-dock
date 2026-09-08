"""Find the panel among the Mac's displays and keep a Chrome kiosk window on it.

Uses CoreGraphics through ctypes, so no Python packages are needed.
"""
import ctypes
import os
import re
import subprocess
import time
from pathlib import Path

_cg = ctypes.CDLL("/System/Library/Frameworks/CoreGraphics.framework/CoreGraphics")


class _CGPoint(ctypes.Structure):
    _fields_ = [("x", ctypes.c_double), ("y", ctypes.c_double)]


class _CGSize(ctypes.Structure):
    _fields_ = [("width", ctypes.c_double), ("height", ctypes.c_double)]


class _CGRect(ctypes.Structure):
    _fields_ = [("origin", _CGPoint), ("size", _CGSize)]


_cg.CGGetActiveDisplayList.argtypes = [ctypes.c_uint32, ctypes.POINTER(ctypes.c_uint32), ctypes.POINTER(ctypes.c_uint32)]
_cg.CGDisplayBounds.restype = _CGRect
_cg.CGDisplayBounds.argtypes = [ctypes.c_uint32]
_cg.CGMainDisplayID.restype = ctypes.c_uint32
_cg.CGDisplayIsBuiltin.restype = ctypes.c_uint32
_cg.CGDisplayIsBuiltin.argtypes = [ctypes.c_uint32]
_cg.CGDisplayPixelsWide.restype = ctypes.c_size_t
_cg.CGDisplayPixelsWide.argtypes = [ctypes.c_uint32]
_cg.CGDisplayPixelsHigh.restype = ctypes.c_size_t
_cg.CGDisplayPixelsHigh.argtypes = [ctypes.c_uint32]

CHROME_CANDIDATES = [
    "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
    "/Applications/Chromium.app/Contents/MacOS/Chromium",
    "/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge",
    "/Applications/Brave Browser.app/Contents/MacOS/Brave Browser",
]


def list_displays():
    ids = (ctypes.c_uint32 * 16)()
    n = ctypes.c_uint32(0)
    _cg.CGGetActiveDisplayList(16, ids, ctypes.byref(n))
    main = _cg.CGMainDisplayID()
    out = []
    for i in range(n.value):
        d = ids[i]
        b = _cg.CGDisplayBounds(d)
        out.append({"id": int(d), "x": int(b.origin.x), "y": int(b.origin.y),
                    "w": int(b.size.width), "h": int(b.size.height),
                    "px_w": int(_cg.CGDisplayPixelsWide(d)), "px_h": int(_cg.CGDisplayPixelsHigh(d)),
                    "main": d == main, "builtin": bool(_cg.CGDisplayIsBuiltin(d))})
    return out


def find_panel(width, height):
    """The display whose size matches the panel (points or pixels, either orientation)."""
    for d in list_displays():
        sizes = {(d["w"], d["h"]), (d["px_w"], d["px_h"])}
        if (width, height) in sizes or (height, width) in sizes:
            return d
    return None


def find_browser():
    for p in CHROME_CANDIDATES:
        if os.path.exists(p):
            return p
    return None


class Kiosk:
    """Opens the dashboard full-screen on the panel when it is plugged in, closes it when unplugged.

    The browser runs with its own profile folder; that unique path is how the window is
    recognised (even one left over from a previous agent run) and closed.
    """

    REOPEN_EVERY = 5      # seconds between attempts if the window keeps disappearing

    def __init__(self, url, width, height, log=print, profile=None):
        self.url, self.width, self.height, self.log = url, width, height, log
        self.proc = None
        self.profile = Path(profile) if profile else Path.home() / "Library" / "Application Support" / "pc-stats-dock" / "chrome"
        self.browser = find_browser()
        self.opened_at = 0.0

    def _pids(self):
        """The browser process started with our profile folder (also from an earlier agent run). Anchored on the
        browser executable so that a shell or pgrep whose command line merely quotes the profile path is not counted."""
        if not self.browser:
            return []
        try:
            pattern = "^%s .*--user-data-dir=%s" % (re.escape(self.browser), re.escape(str(self.profile)))
            out = subprocess.run(["pgrep", "-f", pattern], capture_output=True, text=True, timeout=3).stdout
            return [int(x) for x in out.split()]
        except Exception:
            return []

    def running(self):
        if self.proc is not None:
            if self.proc.poll() is None:
                return True
            self.proc = None  # exited (the user quit it): forget it
        return bool(self._pids())

    def tick(self):
        """Panel connected and no dashboard window: open one. Panel gone: close it. Nothing else."""
        panel = find_panel(self.width, self.height)
        running = self.running()
        if panel and not running and time.time() - self.opened_at >= self.REOPEN_EVERY:
            self.open(panel)
        elif not panel and running:
            self.log("[kiosk] panel unplugged, closing the dashboard window")
            self.close()

    def open(self, panel):
        if not self.browser:
            self.log("[kiosk] no Chrome/Chromium/Edge/Brave found; open %s yourself" % self.url)
            return
        self.profile.mkdir(parents=True, exist_ok=True)
        args = [self.browser, "--kiosk", "--user-data-dir=%s" % self.profile, "--no-first-run",
                "--noerrdialogs", "--disable-session-crashed-bubble", "--disable-features=Translate",
                "--window-position=%d,%d" % (panel["x"], panel["y"]),
                "--window-size=%d,%d" % (panel["w"], panel["h"]), self.url]
        self.proc = subprocess.Popen(args, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        self.opened_at = time.time()
        self.log("[kiosk] dashboard opened on display %d at %d,%d (%dx%d)" % (panel["id"], panel["x"], panel["y"], panel["w"], panel["h"]))

    def close(self):
        if self.proc is not None and self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(timeout=3)
            except subprocess.TimeoutExpired:
                self.proc.kill()
        self.proc = None
        for pid in self._pids():
            try:
                os.kill(pid, 15)
            except OSError:
                pass


if __name__ == "__main__":
    for d in list_displays():
        print(d)
