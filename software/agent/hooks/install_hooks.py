#!/usr/bin/env python3
"""Register the panel's hooks with the AI tools found on this Mac. Idempotent. `--uninstall` removes them.
Prints a JSON summary."""
import json
import os
import re
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
HOME = Path(os.environ.get("PCSTATS_HOME") or Path.home())
CLAUDE = HOME / ".claude" / "settings.json"
CODEX = HOME / ".codex" / "config.toml"
CURSOR = HOME / ".cursor" / "hooks.json"
PY = sys.executable or "python3"


def claude_code(uninstall):
    hook = str(HERE / "claude_code_hook.py")
    if not CLAUDE.parent.exists():
        return "skipped (Claude Code not found)"
    try:
        settings = json.loads(CLAUDE.read_text()) if CLAUDE.exists() else {}
    except json.JSONDecodeError:
        return "left alone (settings.json is not valid JSON)"
    hooks = settings.setdefault("hooks", {})
    changed = False
    for event in ("Stop", "Notification"):
        entries = hooks.get(event) or []
        mine = [e for e in entries if any(h.get("command") == hook for h in (e.get("hooks") or []))]
        others = [e for e in entries if e not in mine]
        if uninstall:
            if mine:
                changed = True
            if others:
                hooks[event] = others
            else:
                hooks.pop(event, None)
        elif not mine:
            hooks[event] = others + [{"hooks": [{"type": "command", "command": hook, "timeout": 5}]}]
            changed = True
    if not hooks:
        settings.pop("hooks", None)
    if changed:
        CLAUDE.parent.mkdir(parents=True, exist_ok=True)
        CLAUDE.write_text(json.dumps(settings, indent=2) + "\n")
    return ("removed" if uninstall else "installed") if changed else ("not present" if uninstall else "already installed")


def codex(uninstall):
    hook = str(HERE / "codex_notify.py")
    if not CODEX.parent.exists():
        return "skipped (Codex not found)"
    text = CODEX.read_text() if CODEX.exists() else ""
    line = f'notify = ["{PY}", "{hook}"]'
    has_mine = hook in text
    has_other = bool(re.search(r"^\s*notify\s*=", text, re.M)) and not has_mine
    if uninstall:
        if not has_mine:
            return "not present"
        text = "\n".join(l for l in text.splitlines() if hook not in l) + "\n"
        CODEX.write_text(text)
        return "removed"
    if has_mine:
        return "already installed"
    if has_other:
        return "left alone (config.toml already has a notify program)"
    CODEX.parent.mkdir(parents=True, exist_ok=True)
    CODEX.write_text((text.rstrip("\n") + "\n\n" if text.strip() else "") + "# PC Stats Panel: report finished turns\n" + line + "\n")
    return "installed"


def cursor(uninstall):
    hook = str(HERE / "cursor_hook.py")
    if not CURSOR.parent.exists():
        return "skipped (Cursor not found)"
    try:
        data = json.loads(CURSOR.read_text()) if CURSOR.exists() else {}
    except json.JSONDecodeError:
        return "left alone (hooks.json is not valid JSON)"
    data.setdefault("version", 1)
    hooks = data.setdefault("hooks", {})
    entries = hooks.get("stop") or []
    mine = [e for e in entries if e.get("command") == hook]
    if uninstall:
        if not mine:
            return "not present"
        hooks["stop"] = [e for e in entries if e not in mine]
        CURSOR.write_text(json.dumps(data, indent=2) + "\n")
        return "removed"
    if mine:
        return "already installed"
    hooks["stop"] = entries + [{"command": hook}]
    CURSOR.write_text(json.dumps(data, indent=2) + "\n")
    return "installed (best effort; check Cursor's hooks docs if it does not fire)"


def main():
    uninstall = "--uninstall" in sys.argv
    for f in HERE.glob("*.py"):
        try:
            os.chmod(f, 0o755)
        except OSError:
            pass
    out = {"claude_code": claude_code(uninstall), "codex": codex(uninstall), "cursor": cursor(uninstall),
           "generic": f"any script can call: {HERE / 'notify.py'} --tool X --title Done"}
    print(json.dumps(out, indent=2))


if __name__ == "__main__":
    main()
