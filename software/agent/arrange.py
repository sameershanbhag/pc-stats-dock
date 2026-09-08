"""Keep the panel a dock, not a desktop: make sure the big monitor stays the main display, park the
panel next to it, and move any windows that strayed onto the panel back to the main screen.

Arrangement uses displayplacer (Homebrew); window sweeping uses the CoreGraphics window list to find
stray windows and System Events (Accessibility) to move them.
"""
import re
import shutil
import subprocess
import time

OSASCRIPT = "/usr/bin/osascript"
POSITIONS = ("below", "above", "left", "right")


def displayplacer_path():
    return shutil.which("displayplacer") or ("/opt/homebrew/bin/displayplacer" if shutil.which("/opt/homebrew/bin/displayplacer") else None)


def parse_list(text):
    """displayplacer's `list` output -> [{persistent, contextual, res, hz, depth, scaling, origin, main}]."""
    screens, cur = [], None
    for line in text.splitlines():
        line = line.strip()
        if line.startswith("Persistent screen id:"):
            cur = {"persistent": line.split(":", 1)[1].strip()}
            screens.append(cur)
        elif cur is None:
            continue
        elif line.startswith("Contextual screen id:"):
            cur["contextual"] = int(line.split(":", 1)[1].strip() or 0)
        elif line.startswith("Resolution:"):
            m = re.search(r"(\d+)x(\d+)", line)
            cur["res"] = (int(m.group(1)), int(m.group(2))) if m else None
        elif line.startswith("Hertz:"):
            cur["hz"] = line.split(":", 1)[1].strip()
        elif line.startswith("Color Depth:"):
            cur["depth"] = line.split(":", 1)[1].strip()
        elif line.startswith("Scaling:"):
            cur["scaling"] = line.split(":", 1)[1].strip()
        elif line.startswith("Origin:"):
            m = re.search(r"\((-?\d+),(-?\d+)\)", line)
            cur["origin"] = (int(m.group(1)), int(m.group(2))) if m else (0, 0)
            cur["main"] = "main display" in line
    return [s for s in screens if s.get("res")]


def panel_origin(position, main_res, panel_res):
    mw, mh = main_res
    pw, ph = panel_res
    return {"below": ((mw - pw) // 2, mh), "above": ((mw - pw) // 2, -ph),
            "left": (-pw, mh - ph), "right": (mw, mh - ph)}.get(position, ((mw - pw) // 2, -ph))


def plan(screens, panel_res, position="above"):
    """(command argv, panel screen, main screen) making the non-panel screen main and parking the panel."""
    panel = next((s for s in screens if s["res"] in (tuple(panel_res), tuple(reversed(panel_res)))), None)
    others = [s for s in screens if s is not panel]
    if not panel or not others:
        return None, panel, None
    main = next((s for s in others if s.get("main")), None) or max(others, key=lambda s: s["res"][0] * s["res"][1])
    px, py = panel_origin(position, main["res"], panel["res"])
    def spec(s, origin):
        parts = [f"id:{s['persistent']}", f"res:{s['res'][0]}x{s['res'][1]}"]
        if s.get("hz"):
            parts.append(f"hz:{s['hz']}")
        if s.get("depth"):
            parts.append(f"color_depth:{s['depth']}")
        parts += ["enabled:true", f"scaling:{s.get('scaling', 'off')}", f"origin:({origin[0]},{origin[1]})", "degree:0"]
        return " ".join(parts)
    argv = [displayplacer_path() or "displayplacer", spec(main, (0, 0)), spec(panel, (px, py))]
    for s in others:
        if s is not main:
            argv.append(spec(s, s.get("origin", (0, 0))))
    return argv, panel, main


def current():
    dp = displayplacer_path()
    if not dp:
        return None, "displayplacer is not installed (brew install jakehilborn/jakehilborn/displayplacer)"
    r = subprocess.run([dp, "list"], capture_output=True, text=True, timeout=10)
    if r.returncode != 0:
        return None, (r.stderr or r.stdout).strip()[:200]
    return parse_list(r.stdout), ""


def arrange(panel_res, position="above", dry_run=False):
    """Returns (changed, message)."""
    screens, err = current()
    if screens is None:
        return False, err
    argv, panel, main = plan(screens, panel_res, position)
    if not argv:
        return False, "panel not connected" if not panel else "only one display"
    already = (not panel.get("main")) and panel.get("origin") == panel_origin(position, main["res"], panel["res"]) and main.get("main")
    if already:
        return False, "arrangement already right"
    if dry_run:
        return True, "dry run: " + " ".join(argv)
    r = subprocess.run(argv, capture_output=True, text=True, timeout=20)
    if r.returncode != 0:
        return False, (r.stderr or r.stdout).strip()[:200] or "displayplacer failed"
    return True, f"main display is {main['res'][0]}x{main['res'][1]}, panel parked {position}"


SWEEP_ONE = """on run argv
  set thePid to (item 1 of argv) as integer
  set panelX to (item 2 of argv) as integer
  set panelY to (item 3 of argv) as integer
  set panelW to (item 4 of argv) as integer
  set panelH to (item 5 of argv) as integer
  set mainX to (item 6 of argv) as integer
  set mainY to (item 7 of argv) as integer
  set mainW to (item 8 of argv) as integer
  set mainH to (item 9 of argv) as integer
  set movedCount to 0
  tell application "System Events"
    set procs to (every process whose unix id is thePid)
    if (count of procs) is 0 then return 0
    set p to item 1 of procs
    repeat with w in (every window of p)
      try
        set {wx, wy} to position of w
        set {ww, wh} to size of w
        if wx >= panelX and wx < (panelX + panelW) and wy >= panelY and wy < (panelY + panelH) then
          if not (ww >= panelW and wh >= panelH) then
            set nx to mainX + ((wx - panelX) mod (mainW - 200))
            set ny to mainY + 40 + ((wy - panelY) mod (mainH - 200))
            set position of w to {nx, ny}
            set movedCount to movedCount + 1
          end if
        end if
      end try
    end repeat
  end tell
  return movedCount
end run"""

VISIBLE_PIDS = 'tell application "System Events" to get unix id of every process whose visible is true'


def _window_list():
    """On-screen, normal-layer windows front to back -> [{pid, app, x, y, w, h}] via CoreGraphics' window
    list (no Accessibility needed). None if the window server said nothing."""
    try:
        import ctypes
        import ctypes.util
        import plistlib
        cg = ctypes.CDLL(ctypes.util.find_library("CoreGraphics"))
        cf = ctypes.CDLL(ctypes.util.find_library("CoreFoundation"))
        cg.CGWindowListCopyWindowInfo.restype = ctypes.c_void_p
        cg.CGWindowListCopyWindowInfo.argtypes = [ctypes.c_uint32, ctypes.c_uint32]
        cf.CFPropertyListCreateData.restype = ctypes.c_void_p
        cf.CFPropertyListCreateData.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_long, ctypes.c_ulong, ctypes.c_void_p]
        cf.CFDataGetBytePtr.restype = ctypes.c_void_p
        cf.CFDataGetBytePtr.argtypes = [ctypes.c_void_p]
        cf.CFDataGetLength.restype = ctypes.c_long
        cf.CFDataGetLength.argtypes = [ctypes.c_void_p]
        cf.CFRelease.argtypes = [ctypes.c_void_p]
        arr = cg.CGWindowListCopyWindowInfo(1 | 16, 0)        # on screen only, no desktop elements
        if not arr:
            return None
        data = cf.CFPropertyListCreateData(None, arr, 100, 0, None)   # xml plist
        if not data:
            cf.CFRelease(arr)
            return None
        raw = ctypes.string_at(cf.CFDataGetBytePtr(data), cf.CFDataGetLength(data))
        cf.CFRelease(data)
        cf.CFRelease(arr)
        wins = plistlib.loads(raw)
    except Exception:
        return None
    out = []
    for w in wins:
        if w.get("kCGWindowLayer", 0) != 0:
            continue
        b = w.get("kCGWindowBounds") or {}
        x, y, ww, wh = (int(b.get(k, 0)) for k in ("X", "Y", "Width", "Height"))
        out.append({"pid": int(w.get("kCGWindowOwnerPID", 0)), "app": str(w.get("kCGWindowOwnerName", "")),
                    "x": x, "y": y, "w": ww, "h": wh})
    return out


def windows_on(rect):
    """The windows whose top-left corner lies inside rect (None if the window list is unavailable)."""
    wins = _window_list()
    if wins is None:
        return None
    return [w for w in wins if rect["x"] <= w["x"] < rect["x"] + rect["w"] and rect["y"] <= w["y"] < rect["y"] + rect["h"]]


def _overlaps(w, rect):
    return w["x"] < rect["x"] + rect["w"] and w["x"] + w["w"] > rect["x"] and w["y"] < rect["y"] + rect["h"] and w["y"] + w["h"] > rect["y"]


def cursor_position():
    """(x, y) of the mouse cursor in global display coordinates (CoreGraphics, no permission needed)."""
    import ctypes
    import ctypes.util

    class Point(ctypes.Structure):
        _fields_ = [("x", ctypes.c_double), ("y", ctypes.c_double)]
    cg = ctypes.CDLL(ctypes.util.find_library("CoreGraphics"))
    cg.CGEventCreate.restype = ctypes.c_void_p
    cg.CGEventGetLocation.restype = Point
    cg.CGEventGetLocation.argtypes = [ctypes.c_void_p]
    ev = cg.CGEventCreate(None)
    pt = cg.CGEventGetLocation(ev)
    ctypes.CDLL(ctypes.util.find_library("CoreFoundation")).CFRelease(ctypes.c_void_p(ev))
    return pt.x, pt.y


def warp_cursor(x, y):
    import ctypes
    import ctypes.util

    class Point(ctypes.Structure):
        _fields_ = [("x", ctypes.c_double), ("y", ctypes.c_double)]
    cg = ctypes.CDLL(ctypes.util.find_library("CoreGraphics"))
    cg.CGWarpMouseCursorPosition.argtypes = [Point]
    cg.CGWarpMouseCursorPosition(Point(float(x), float(y)))


def inside(rect, x, y):
    return rect["x"] <= x < rect["x"] + rect["w"] and rect["y"] <= y < rect["y"] + rect["h"]


def button_down():
    """True while the left mouse button, or a finger on the touchscreen, is down."""
    import ctypes
    import ctypes.util
    cg = ctypes.CDLL(ctypes.util.find_library("CoreGraphics"))
    cg.CGEventSourceButtonState.restype = ctypes.c_bool
    cg.CGEventSourceButtonState.argtypes = [ctypes.c_int, ctypes.c_uint32]
    return bool(cg.CGEventSourceButtonState(0, 0))      # combined session state, left button


def wait_button_up(max_wait=0.4):
    """Block until the finger has lifted (so its lift lands on the panel, not wherever we move the cursor)."""
    end = time.time() + max_wait
    try:
        while button_down() and time.time() < end:
            time.sleep(0.01)
    except Exception:
        time.sleep(0.1)


def front_pid():
    """pid of the active application (LaunchServices, ~10 ms; AppKit is not loaded into the agent on purpose:
    NSWorkspace stalls for ~30 s in a launchd-started process), or None."""
    try:
        asn = subprocess.run(["/usr/bin/lsappinfo", "front"], capture_output=True, text=True, timeout=5).stdout.strip()
        info = subprocess.run(["/usr/bin/lsappinfo", "info", "-only", "pid", asn], capture_output=True, text=True, timeout=5).stdout
        m = re.search(r'"pid"\s*=\s*(\d+)', info)
        return int(m.group(1)) if m else None
    except (OSError, subprocess.SubprocessError):
        return None


def activate_pid(pid):
    """Make the app with this pid the active one, through System Events. (NSRunningApplication and
    SetFrontProcess report success from a background process but change nothing.) Returns (ok, how)."""
    r = subprocess.run([OSASCRIPT, "-e", f'tell application "System Events" to set frontmost of (first process whose unix id is {int(pid)}) to true'],
                       capture_output=True, text=True, timeout=5)
    return r.returncode == 0, "osascript"


def main_app_to_activate(wins, main_rect, skip_pids, front):
    """When the active app is the dashboard (a skip pid), the pid of the frontmost other app with a window
    on the main display; None when nothing needs doing."""
    skip = set(skip_pids)
    if front is None or front not in skip or not wins:
        return None
    for w in wins:
        if w["pid"] and w["pid"] not in skip and _overlaps(w, main_rect):
            return w["pid"]
    return None


def app_path_of(pid, exe=None):
    """/Applications/Safari.app for the Safari process (None for processes outside an .app bundle)."""
    if exe is None:
        r = subprocess.run(["/bin/ps", "-o", "comm=", "-p", str(pid)], capture_output=True, text=True, timeout=5)
        exe = r.stdout.strip()
    i = exe.find(".app/")
    return exe[:i + 4] if i > 0 else None


def activate_main_app(main_rect, skip_pids, dry_run=False):
    """After a tap on the panel the dashboard is the active app, so macOS would aim Spaces and Spotlight at
    the panel's display. Bring the app that was in front on the main display back first."""
    pid = main_app_to_activate(_window_list(), main_rect, skip_pids, front_pid())
    if pid is None:
        return False, "main display already active"
    path = app_path_of(pid)
    if not path:
        return False, f"pid {pid} is not an app"
    if dry_run:
        return True, f"dry run: activate {path}"
    ok, how = activate_pid(pid)      # by pid: the dashboard's Chrome and the user's Chrome are the same app bundle
    if not ok:
        subprocess.run(["/usr/bin/open", path], capture_output=True, timeout=5)
    return True, f"activated {path.rsplit('/', 1)[-1]} ({how})"


def visible_pids():
    r = subprocess.run([OSASCRIPT, "-e", VISIBLE_PIDS], capture_output=True, text=True, timeout=10)
    return [int(t) for t in re.findall(r"\d+", r.stdout)] if r.returncode == 0 else []


def sweep_pids(panel, wins, skip_pids=()):
    """Processes with a window on the panel that is not the full-panel dashboard window (nor the dashboard's own process)."""
    skip = set(skip_pids)
    return sorted({w["pid"] for w in wins if w["pid"] and w["pid"] not in skip and not (w["w"] >= panel["w"] and w["h"] >= panel["h"])})


def sweep(panel, main, dry_run=False, skip_pids=()):
    """Move windows sitting on the panel back onto the main display. panel/main = {x,y,w,h}.
    Only the processes that actually own a window there are asked (one System Events call each,
    with a short timeout, so one slow app cannot stall the rest)."""
    wins = windows_on(panel)
    pids = sweep_pids(panel, wins, skip_pids) if wins is not None else [p for p in visible_pids() if p not in set(skip_pids)]
    if not pids:
        return True, "no windows on the panel"
    if dry_run:
        return True, f"dry run: sweep {len(pids)} process(es)"
    args = [str(v) for v in (panel["x"], panel["y"], panel["w"], panel["h"], main["x"], main["y"], main["w"], main["h"])]
    moved, errors = 0, []
    for pid in pids:
        try:
            r = subprocess.run([OSASCRIPT, "-e", SWEEP_ONE, str(pid)] + args, capture_output=True, text=True, timeout=8)
        except subprocess.TimeoutExpired:
            errors.append(f"pid {pid} did not answer")
            continue
        if r.returncode != 0:
            out = (r.stderr or r.stdout).strip()
            errors.append(out.splitlines()[-1] if out else f"pid {pid} failed")
            continue
        moved += int(m.group(0)) if (m := re.search(r"-?\d+", r.stdout or "")) else 0
    msg = f"moved {moved} window{'s' if moved != 1 else ''} back to the main display"
    if errors:
        joined = "; ".join(errors[:3])
        hint = " — allow “PC Stats Panel” under Privacy & Security › Accessibility" if "assistive" in joined or "-25211" in joined or "-1719" in joined else ""
        msg += f" ({joined}{hint})"
    return moved > 0 or not errors, msg
