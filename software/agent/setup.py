#!/usr/bin/env python3
"""Login-item setup for the packaged app. The app runs this itself:

  open "PC Stats Panel.app"              -> setup.py install --app <the app>   (first launch, and any later launch)
  ".../PCStatsPanel" --uninstall         -> setup.py uninstall --app <the app>

install writes ~/Library/LaunchAgents/com.pcstatsdock.agent.plist pointing at the app, starts the agent,
adds the "Stats Dock Admin" Dock icon and tells you what is left to do (the Accessibility switch).
Everything is idempotent. --dry-run prints the steps instead of running them.
"""
import argparse
import json
import os
import plistlib
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

LABEL = "com.pcstatsdock.agent"
HOME = Path(os.environ.get("PCSTATS_HOME") or Path.home())          # tests point this at a scratch folder
PLIST = HOME / "Library" / "LaunchAgents" / f"{LABEL}.plist"
LOGS = HOME / "Library" / "Logs" / "pc-stats-dock"
SUPPORT = HOME / "Library" / "Application Support" / "pc-stats-dock"
PORT = 4400
ACCESSIBILITY_PANE = "x-apple.systempreferences:com.apple.preference.security?Privacy_Accessibility"
PATH = "/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"


def plist_body(app):
    app = Path(app)
    return {
        "Label": LABEL,
        "ProgramArguments": [str(app / "Contents" / "MacOS" / "PCStatsPanel"), "--service"],
        "WorkingDirectory": str(app / "Contents" / "Resources" / "app" / "agent"),
        "RunAtLoad": True,
        "KeepAlive": True,
        "EnvironmentVariables": {"PATH": PATH},
        "StandardOutPath": str(LOGS / "agent.log"),
        "StandardErrorPath": str(LOGS / "agent.err.log"),
    }


def run(argv, dry_run=False, timeout=60):
    """Run a command, never raising; returns (returncode, output)."""
    if dry_run:
        print("   would run:", " ".join(argv))
        return 0, ""
    try:
        r = subprocess.run(argv, capture_output=True, text=True, timeout=timeout)
        return r.returncode, (r.stdout + r.stderr).strip()
    except (OSError, subprocess.SubprocessError) as exc:
        return 1, str(exc)


def domain():
    return f"gui/{os.getuid()}"


def health(timeout=20):
    """The agent's /api/health once it answers, or None."""
    end = time.time() + timeout
    while time.time() < end:
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{PORT}/api/health", timeout=2) as r:
                return json.load(r)
        except Exception:
            time.sleep(0.5)
    return None


def dialog(message, button="Open Accessibility Settings"):
    """A plain macOS dialog; returns the button pressed ('' when dialogs are unavailable)."""
    script = (f'display dialog {json.dumps(message)} with title "PC Stats Panel" '
              f'buttons {{"{button}", "OK"}} default button "OK" with icon note')
    code, out = run(["/usr/bin/osascript", "-e", script], timeout=600)
    return out.split("button returned:")[-1].strip() if code == 0 else ""


def install(app, dry_run=False, quiet=False):
    app = Path(app).resolve()
    exe = app / "Contents" / "MacOS" / "PCStatsPanel"
    if not exe.exists():
        print(f"no launcher at {exe}")
        return 2
    print(f"== PC Stats Panel: setting up the login item for {app}")
    # A copy that came from a download carries macOS's quarantine flag; launched by launchd instead of by you,
    # such an app hangs on the Gatekeeper prompt nobody can see. You opened it (or installed it) already, so clear it.
    run(["/usr/bin/xattr", "-dr", "com.apple.quarantine", str(app)], dry_run)
    if not dry_run:
        LOGS.mkdir(parents=True, exist_ok=True)
        SUPPORT.mkdir(parents=True, exist_ok=True)
        PLIST.parent.mkdir(parents=True, exist_ok=True)
        with open(PLIST, "wb") as f:
            plistlib.dump(plist_body(app), f)
    print(f"   login item: {PLIST}")
    run(["/bin/launchctl", "bootout", domain(), str(PLIST)], dry_run)                 # an earlier copy, if any
    run(["/usr/bin/pkill", "-9", "-f", f"--user-data-dir={SUPPORT / 'chrome'}"], dry_run)   # its dashboard window
    code, out = run(["/bin/launchctl", "bootstrap", domain(), str(PLIST)], dry_run)
    if code != 0 and not dry_run:
        print(f"   launchctl bootstrap failed: {out}")
        run(["/bin/launchctl", "kickstart", "-k", f"{domain()}/{LABEL}"])
    h = None if dry_run else health()
    print("   agent: " + (f"running on http://localhost:{PORT}" if h else "did not answer yet (see " + str(LOGS / "agent.err.log") + ")"))
    dock = app / "Contents" / "Resources" / "mac" / "dock-admin.sh"
    if dock.exists():
        run(["/bin/zsh", str(dock)], dry_run, timeout=120)
        print("   Dock: Stats Dock Admin icon " + ("would be added" if dry_run else "added"))
    trusted = bool(h and h.get("caps", {}).get("accessibility"))
    lines = ["PC Stats Panel is installed and starts at login.", ""]
    lines.append("• Plug in the panel: the dashboard opens on it by itself.")
    if not trusted:
        lines.append("• Key buttons and touch need one switch: System Settings › Privacy & Security › Accessibility › PC Stats Panel.")
    lines.append("• Temperatures and power need Homebrew's macmon (brew install macmon); the automatic screen arrangement needs displayplacer.")
    lines.append("• Buttons and feeds: the Stats Dock Admin icon in your Dock, or http://localhost:4400/admin.")
    message = "\n".join(lines)
    print(message)
    if not quiet and not dry_run:
        if dialog(message) == "Open Accessibility Settings":
            run(["/usr/bin/open", ACCESSIBILITY_PANE])
    return 0


def uninstall(app=None, dry_run=False):
    print("== PC Stats Panel: removing the login item")
    run(["/bin/launchctl", "bootout", domain(), str(PLIST)], dry_run)
    run(["/usr/bin/pkill", "-9", "-f", f"--user-data-dir={SUPPORT / 'chrome'}"], dry_run)
    if PLIST.exists() and not dry_run:
        PLIST.unlink()
    dock = Path(app) / "Contents" / "Resources" / "mac" / "dock-admin.sh" if app else None
    if dock and dock.exists():
        run(["/bin/zsh", str(dock), "--remove"], dry_run, timeout=120)
    else:
        run(["/bin/rm", "-rf", str(HOME / "Applications" / "Stats Dock Admin.app")], dry_run)
    run(["/usr/bin/tccutil", "reset", "Accessibility", LABEL], dry_run)
    print(f"   done. Your buttons and feeds stay in {SUPPORT / 'config.json'}; delete that folder to remove them too,"
          " and drag the app to the Trash.")
    return 0


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("verb", choices=["install", "uninstall"])
    ap.add_argument("--app", help="path of PC Stats Panel.app")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--quiet", action="store_true", help="no dialog")
    a = ap.parse_args(argv)
    if a.verb == "install":
        if not a.app:
            ap.error("--app is required for install")
        return install(a.app, a.dry_run, a.quiet)
    return uninstall(a.app, a.dry_run)


if __name__ == "__main__":
    sys.exit(main())
