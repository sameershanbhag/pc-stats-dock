"""Jump to the window / tab / project an event came from. Everything is AppleScript or `open`."""
import os
import shutil
import subprocess

OSASCRIPT, OPEN = "/usr/bin/osascript", "/usr/bin/open"

TERMINAL_TTY = '''tell application "Terminal"
  repeat with w in windows
    repeat with t in tabs of w
      if tty of t is "%s" then
        set selected tab of w to t
        set index of w to 1
        activate
        return "ok"
      end if
    end repeat
  end repeat
end tell
return "not found"'''

ITERM_SESSION = '''tell application "iTerm2"
  repeat with w in windows
    repeat with t in tabs of w
      repeat with s in sessions of t
        if id of s is "%s" then
          select s
          select t
          set index of w to 1
          activate
          return "ok"
        end if
      end repeat
    end repeat
  end repeat
end tell
return "not found"'''

RAISE_WINDOW = '''tell application "System Events"
  tell process "%s"
    set wins to (every window whose name contains "%s")
    if (count of wins) > 0 then
      perform action "AXRaise" of item 1 of wins
      return "ok"
    end if
  end tell
end tell
return "not found"'''


def _q(s):
    return str(s).replace("\\", "\\\\").replace('"', '\\"')


def plan(spec):
    """The steps for a focus spec, as ('osascript', script) or ('cmd', [argv]) tuples."""
    steps = []
    kind = spec.get("kind", "none")
    if spec.get("tmux_pane"):
        steps += [("cmd", ["tmux", "select-window", "-t", spec["tmux_pane"]]), ("cmd", ["tmux", "select-pane", "-t", spec["tmux_pane"]])]
    if kind == "terminal_tty" and spec.get("tty"):
        steps.append(("osascript", TERMINAL_TTY % _q(spec["tty"])))
    elif kind == "iterm" and spec.get("session"):
        steps.append(("osascript", ITERM_SESSION % _q(spec["session"])))
    elif kind == "editor" and spec.get("folder"):
        app = spec.get("app") or "VS Code"
        cli = {"VS Code": "code", "Cursor": "cursor", "Windsurf": "windsurf"}.get(app)
        name = {"VS Code": "Visual Studio Code", "Cursor": "Cursor", "Windsurf": "Windsurf"}.get(app, app)
        if cli and shutil.which(cli):
            steps.append(("cmd", [shutil.which(cli), "--reuse-window", spec["folder"]]))
        else:
            steps.append(("cmd", [OPEN, "-a", name, spec["folder"]]))
    elif kind == "app" and spec.get("bundle"):
        if spec.get("hint") and spec.get("app"):
            steps.append(("osascript", RAISE_WINDOW % (_q(spec["app"]), _q(spec["hint"]))))
        steps.append(("cmd", [OPEN, "-b", spec["bundle"]]))
    elif spec.get("bundle"):
        steps.append(("cmd", [OPEN, "-b", spec["bundle"]]))
    return steps


def focus(spec, dry_run=False):
    """Returns (ok, message)."""
    steps = plan(spec or {})
    if not steps:
        return False, "no window information was recorded for this one"
    if dry_run:
        return True, "dry run: " + "; ".join(s[1] if s[0] == "cmd" and isinstance(s[1], str) else (" ".join(s[1]) if s[0] == "cmd" else "AppleScript") for s in steps)
    notes = []
    for kind, what in steps:
        try:
            if kind == "osascript":
                r = subprocess.run([OSASCRIPT, "-e", what], capture_output=True, text=True, timeout=10)
                out = (r.stdout or r.stderr).strip()
                if r.returncode != 0:
                    notes.append(out.splitlines()[-1] if out else "AppleScript failed")
                    if "-1743" in out or "Not authorized" in out:
                        notes.append("allow “PC Stats Panel” to control that app under Privacy & Security › Automation")
                elif out and out != "ok":
                    notes.append(out)
            else:
                r = subprocess.run(what, capture_output=True, text=True, timeout=10)
                if r.returncode != 0:
                    notes.append((r.stderr or r.stdout).strip().splitlines()[-1] if (r.stderr or r.stdout).strip() else f"{what[0]} failed")
        except Exception as exc:
            notes.append(f"{type(exc).__name__}: {exc}")
    ok = not notes or all(n in ("not found",) for n in notes)
    return ok, ("jumped to " + (spec.get("app") or "it")) if ok else "; ".join(notes)
