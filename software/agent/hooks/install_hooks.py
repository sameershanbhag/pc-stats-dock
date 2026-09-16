#!/usr/bin/env python3
"""Register the panel's hooks with the AI tools found on this Mac. Idempotent.

The hook scripts are copied to a stable folder (~/Library/Application Support/pc-stats-dock/hooks) and the tools
are pointed there, so the registrations survive app upgrades and moves. Modes:
  (default)     install: copy the scripts, register them (a stale registration is re-pointed)
  --repair      only re-point registrations that already exist; never add new ones (run on every start/upgrade)
  --uninstall   remove the registrations
Prints a JSON summary."""
import json
import os
import re
import shutil
import stat
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
HOME = Path(os.environ.get("PCSTATS_HOME") or Path.home())
STABLE = HOME / "Library" / "Application Support" / "pc-stats-dock" / "hooks"
CLAUDE = HOME / ".claude" / "settings.json"
CODEX = HOME / ".codex" / "config.toml"
CURSOR = HOME / ".cursor" / "hooks.json"
PY = sys.executable or "python3"
MARK = "pc-stats-dock"      # every path of ours contains it: the old app folder, the app bundle, the stable folder
MANAGED = Path(os.environ.get("PCSTATS_MANAGED") or "/Library/Application Support/ClaudeCode/managed-settings.json")


def hook_policy():
    """A note when a Claude Code policy stops user hooks from running (company-managed Macs), else ''."""
    notes = []
    for label, path in (("company policy (managed-settings.json)", MANAGED), ("your settings.json", CLAUDE)):
        try:
            data = json.loads(path.read_text()) if path.exists() else {}
        except (OSError, json.JSONDecodeError):
            continue
        for key in ("disableAllHooks", "allowManagedHooksOnly"):
            if data.get(key) is True:
                notes.append(f"{label} sets {key}: user hooks will not run")
    return "; ".join(notes)


def sync_stable():
    """Copy the hook scripts to the stable folder; returns it. The copies are executable and never quarantined."""
    STABLE.mkdir(parents=True, exist_ok=True)
    for f in HERE.glob("*.py"):
        target = STABLE / f.name
        if target.resolve() != f.resolve():
            shutil.copyfile(f, target)
        target.chmod(target.stat().st_mode | stat.S_IRWXU | stat.S_IRGRP | stat.S_IXGRP | stat.S_IROTH | stat.S_IXOTH)
    try:
        subprocess.run(["/usr/bin/xattr", "-dr", "com.apple.quarantine", str(STABLE)], capture_output=True, timeout=5)
    except (OSError, subprocess.SubprocessError):
        pass
    return STABLE


def ours(command, script):
    """Is this hook command one of ours (any version, any past location)?"""
    return isinstance(command, str) and MARK in command and command.rstrip().endswith(script)


def claude_code(mode):
    hook = str(STABLE / "claude_code_hook.py")
    if not CLAUDE.parent.exists():
        return "skipped (Claude Code not found)"
    try:
        settings = json.loads(CLAUDE.read_text()) if CLAUDE.exists() else {}
    except json.JSONDecodeError:
        return "left alone (settings.json is not valid JSON)"
    hooks = settings.setdefault("hooks", {})
    changed = repaired = False
    registered = any(any(ours(h.get("command"), "claude_code_hook.py") for h in (e.get("hooks") or [])) for ev in ("Stop", "Notification") for e in (hooks.get(ev) or []))
    if mode == "repair" and not registered:
        return "not registered"
    for event in ("Stop", "Notification"):
        entries = hooks.get(event) or []
        mine = [e for e in entries if any(ours(h.get("command"), "claude_code_hook.py") for h in (e.get("hooks") or []))]
        others = [e for e in entries if e not in mine]
        if mode == "uninstall":
            if mine:
                changed = True
            if others:
                hooks[event] = others
            else:
                hooks.pop(event, None)
            continue
        current = [e for e in mine if all(h.get("command") == hook for h in (e.get("hooks") or []))]
        if mine and not current:
            repaired = True
        if not current:
            hooks[event] = others + [{"hooks": [{"type": "command", "command": hook, "timeout": 5}]}]
            changed = True
    if not hooks:
        settings.pop("hooks", None)
    if changed:
        CLAUDE.parent.mkdir(parents=True, exist_ok=True)
        CLAUDE.write_text(json.dumps(settings, indent=2) + "\n")
    if mode == "uninstall":
        return "removed" if changed else "not present"
    result = ("repaired" if repaired else "installed") if changed else "already installed"
    policy = hook_policy()
    return result + (" · but " + policy if policy else "")


def codex(mode):
    hook = str(STABLE / "codex_notify.py")
    if not CODEX.parent.exists():
        return "skipped (Codex not found)"
    text = CODEX.read_text() if CODEX.exists() else ""
    line = f'notify = ["{PY}", "{hook}"]'
    mine = [l for l in text.splitlines() if re.match(r"^\s*notify\s*=", l) and MARK in l and "codex_notify.py" in l]
    has_other = bool(re.search(r"^\s*notify\s*=", text, re.M)) and not mine
    if mode == "uninstall":
        if not mine:
            return "not present"
        text = "\n".join(l for l in text.splitlines() if l not in mine and l.strip() != "# PC Stats Panel: report finished turns") + "\n"
        CODEX.write_text(text)
        return "removed"
    if mine and mine[0].strip() == line:
        return "already installed"
    if mode == "repair" and not mine:
        return "not registered"
    if has_other:
        return "left alone (config.toml already has a notify program)"
    if mine:
        text = "\n".join(line if l in mine else l for l in text.splitlines()) + "\n"
        CODEX.write_text(text)
        return "repaired"
    CODEX.parent.mkdir(parents=True, exist_ok=True)
    CODEX.write_text((text.rstrip("\n") + "\n\n" if text.strip() else "") + "# PC Stats Panel: report finished turns\n" + line + "\n")
    return "installed"


def cursor(mode):
    hook = str(STABLE / "cursor_hook.py")
    if not CURSOR.parent.exists():
        return "skipped (Cursor not found)"
    try:
        data = json.loads(CURSOR.read_text()) if CURSOR.exists() else {}
    except json.JSONDecodeError:
        return "left alone (hooks.json is not valid JSON)"
    data.setdefault("version", 1)
    hooks = data.setdefault("hooks", {})
    entries = hooks.get("stop") or []
    mine = [e for e in entries if ours(e.get("command"), "cursor_hook.py")]
    others = [e for e in entries if e not in mine]
    if mode == "uninstall":
        if not mine:
            return "not present"
        hooks["stop"] = others
        CURSOR.write_text(json.dumps(data, indent=2) + "\n")
        return "removed"
    if any(e.get("command") == hook for e in mine):
        return "already installed"
    if mode == "repair" and not mine:
        return "not registered"
    hooks["stop"] = others + [{"command": hook}]
    CURSOR.write_text(json.dumps(data, indent=2) + "\n")
    return "repaired" if mine else "installed"


def main():
    mode = "uninstall" if "--uninstall" in sys.argv else "repair" if "--repair" in sys.argv else "install"
    if mode != "uninstall":
        sync_stable()
    out = {"claude_code": claude_code(mode), "codex": codex(mode), "cursor": cursor(mode),
           "generic": f"any script can call: {STABLE / 'notify.py'} --tool X --title Done"}
    print(json.dumps(out, indent=2))


if __name__ == "__main__":
    main()
