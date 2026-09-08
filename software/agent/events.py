"""'An AI finished / needs you' events from any tool, with enough context to jump back to it.

Each event carries a `focus` spec that focus.py knows how to act on: a Terminal tab (by tty), an
iTerm session (by id), an editor window (by folder), a tmux pane, or just an app to activate.
Events are kept per chat session (the newest state replaces the older one) and persisted.
"""
import json
import os
import re
import threading
import time
import uuid
from pathlib import Path

STORE_PATH = Path.home() / "Library" / "Application Support" / "pc-stats-dock" / "events.json"

# how a shell tells us which app it lives in: __CFBundleIdentifier (set by macOS for app children) or TERM_PROGRAM
HOSTS = {
    "com.apple.Terminal": ("Terminal", "com.apple.Terminal"), "Apple_Terminal": ("Terminal", "com.apple.Terminal"),
    "com.googlecode.iterm2": ("iTerm", "com.googlecode.iterm2"), "iTerm.app": ("iTerm", "com.googlecode.iterm2"),
    "com.mitchellh.ghostty": ("Ghostty", "com.mitchellh.ghostty"), "ghostty": ("Ghostty", "com.mitchellh.ghostty"),
    "dev.warp.Warp-Stable": ("Warp", "dev.warp.Warp-Stable"), "WarpTerminal": ("Warp", "dev.warp.Warp-Stable"),
    "net.kovidgoyal.kitty": ("kitty", "net.kovidgoyal.kitty"), "io.alacritty": ("Alacritty", "io.alacritty"),
    "com.github.wez.wezterm": ("WezTerm", "com.github.wez.wezterm"), "WezTerm": ("WezTerm", "com.github.wez.wezterm"),
    "com.microsoft.VSCode": ("VS Code", "com.microsoft.VSCode"), "vscode": ("VS Code", "com.microsoft.VSCode"),
    "com.todesktop.230313mzl4w4u92": ("Cursor", "com.todesktop.230313mzl4w4u92"),
    "com.exafunction.windsurf": ("Windsurf", "com.exafunction.windsurf"),
}
EDITORS = {"VS Code", "Cursor", "Windsurf"}


def host_from_env(env):
    bundle = (env or {}).get("__CFBundleIdentifier") or ""
    tp = (env or {}).get("TERM_PROGRAM") or ""
    if bundle in HOSTS:
        return HOSTS[bundle]
    if tp in HOSTS:
        return HOSTS[tp]
    term = (env or {}).get("TERM") or ""
    if "ghostty" in term:
        return HOSTS["ghostty"]
    if "kitty" in term:
        return HOSTS["net.kovidgoyal.kitty"]
    return ("", "")


def focus_spec_from_env(env, cwd="", tty=""):
    """Where to jump back to, derived from the environment the hook ran in."""
    env = env or {}
    name, bundle = host_from_env(env)
    spec = {"kind": "none", "app": name, "bundle": bundle, "cwd": cwd or "", "hint": os.path.basename((cwd or "").rstrip("/"))}
    if env.get("TMUX_PANE"):
        spec["tmux_pane"] = env["TMUX_PANE"]
    if name == "Terminal" and tty:
        spec.update(kind="terminal_tty", tty=tty)
    elif name == "iTerm" and env.get("ITERM_SESSION_ID"):
        spec.update(kind="iterm", session=env["ITERM_SESSION_ID"].split(":")[-1])
    elif name in EDITORS:
        spec.update(kind="editor", folder=cwd or "")
    elif bundle:
        spec.update(kind="app")
    return spec


def transcript_preview(path, limit=140):
    """Last assistant text in a Claude Code transcript (JSONL), collapsed to one line."""
    try:
        p = Path(path)
        size = p.stat().st_size
        with open(p, "rb") as f:
            f.seek(max(0, size - 200_000))
            tail = f.read().decode("utf-8", "replace")
    except OSError:
        return ""
    for line in reversed(tail.splitlines()):
        try:
            entry = json.loads(line)
        except json.JSONDecodeError:
            continue
        if entry.get("type") != "assistant":
            continue
        content = (entry.get("message") or {}).get("content")
        texts = []
        if isinstance(content, str):
            texts.append(content)
        elif isinstance(content, list):
            texts += [c.get("text", "") for c in content if isinstance(c, dict) and c.get("type") == "text"]
        text = re.sub(r"\s+", " ", " ".join(t for t in texts if t)).strip()
        if text:
            return text[:limit]
    return ""


def from_claude_code(body):
    """body = {"payload": <hook stdin JSON>, "env": {...}, "tty": "/dev/ttys003", "cwd": "..."}"""
    payload = body.get("payload") or {}
    cwd = body.get("cwd") or payload.get("cwd") or ""
    event = payload.get("hook_event_name") or ""
    needs = event == "Notification"
    title = (payload.get("message") or "Needs your attention") if needs else "Finished"
    preview = "" if needs else transcript_preview(payload.get("transcript_path", ""))
    focus = focus_spec_from_env(body.get("env"), cwd, body.get("tty", ""))
    return {"tool": "Claude Code", "state": "needs_input" if needs else "done", "title": title[:120], "text": preview,
            "project": focus["hint"] or "somewhere", "cwd": cwd, "session": payload.get("session_id") or "", "focus": focus}


def from_codex(body):
    payload = body.get("payload") or {}
    cwd = body.get("cwd") or ""
    focus = focus_spec_from_env(body.get("env"), cwd, body.get("tty", ""))
    kind = payload.get("type") or "agent-turn-complete"
    text = re.sub(r"\s+", " ", str(payload.get("last-assistant-message") or "")).strip()[:140]
    return {"tool": "Codex", "state": "done", "title": "Finished" if kind.endswith("complete") else kind, "text": text,
            "project": focus["hint"] or "somewhere", "cwd": cwd, "session": str(payload.get("turn-id") or payload.get("thread-id") or ""), "focus": focus}


def from_generic(body):
    """Anything can post here: {tool, title, text, state, project, cwd, session, env, tty, focus}."""
    cwd = body.get("cwd") or ""
    focus = body.get("focus") if isinstance(body.get("focus"), dict) else focus_spec_from_env(body.get("env"), cwd, body.get("tty", ""))
    state = body.get("state") if body.get("state") in ("done", "needs_input", "info") else "done"
    project = body.get("project") or focus.get("hint") or os.path.basename((cwd or focus.get("folder") or "").rstrip("/"))
    return {"tool": str(body.get("tool") or "AI")[:30], "state": state, "title": str(body.get("title") or "Finished")[:120],
            "text": re.sub(r"\s+", " ", str(body.get("text") or "")).strip()[:140], "project": str(project)[:40] or "somewhere",
            "cwd": cwd, "session": str(body.get("session") or "")[:80], "focus": focus}


class EventStore:
    def __init__(self, path=STORE_PATH, keep=100):
        self.path = Path(path)
        self.keep = keep
        self.lock = threading.Lock()
        self.events = []
        try:
            self.events = json.loads(self.path.read_text())[: keep]
        except Exception:
            self.events = []

    def _save(self):
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(".json.tmp")
            tmp.write_text(json.dumps(self.events[: self.keep]))
            os.chmod(tmp, 0o600)
            os.replace(tmp, self.path)
        except OSError:
            pass

    def add(self, event):
        ev = dict(event)
        ev.update({"id": uuid.uuid4().hex[:10], "ts": time.time(), "seen": False})
        with self.lock:
            if ev.get("session"):   # one entry per chat session: the latest state replaces the older one
                self.events = [e for e in self.events if not (e.get("session") == ev["session"] and e.get("tool") == ev["tool"])]
            self.events.insert(0, ev)
            self.events = self.events[: self.keep]
            self._save()
        return ev

    def list(self, limit=20):
        with self.lock:
            return [dict(e) for e in self.events[:limit]]

    def get(self, eid):
        with self.lock:
            return next((dict(e) for e in self.events if e["id"] == eid), None)

    def mark_seen(self, eid, seen=True):
        with self.lock:
            for e in self.events:
                if e["id"] == eid:
                    e["seen"] = seen
            self._save()

    def dismiss(self, eid):
        with self.lock:
            self.events = [e for e in self.events if e["id"] != eid]
            self._save()

    def clear(self):
        with self.lock:
            self.events = []
            self._save()

    def unseen(self):
        with self.lock:
            return sum(1 for e in self.events if not e.get("seen"))
