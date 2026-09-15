"""Shared by the hook scripts: gather where we are and POST to the agent, never blocking the tool."""
import json
import os
import subprocess
import sys
import urllib.request

AGENT = os.environ.get("PCSTATS_AGENT", "http://127.0.0.1:4400")


ENV_KEYS = ("TERM_PROGRAM", "TERM_SESSION_ID", "ITERM_SESSION_ID", "TMUX_PANE", "__CFBundleIdentifier", "TERM",
            "CLAUDE_CODE_ENTRYPOINT", "VSCODE_PID", "VSCODE_CWD", "VSCODE_GIT_ASKPASS_MAIN", "CURSOR_TRACE_ID")


def ancestors(limit=12):
    """Names of the processes above this one (nearest first): the editor or app that hosts the chat shows up
    here even when the environment says nothing (VS Code's extension host is 'Code Helper (Plugin)')."""
    try:
        out = subprocess.run(["ps", "-axo", "pid=,ppid=,comm="], capture_output=True, text=True, timeout=2).stdout
    except Exception:
        return []
    parent, name = {}, {}
    for line in out.splitlines():
        parts = line.split(None, 2)
        if len(parts) == 3 and parts[0].isdigit() and parts[1].isdigit():
            parent[int(parts[0])] = int(parts[1])
            name[int(parts[0])] = os.path.basename(parts[2].strip())
    chain, pid = [], os.getppid()
    while pid > 1 and pid in name and len(chain) < limit:
        chain.append(name[pid])
        pid = parent.get(pid, 0)
    return chain


def context():
    try:
        tty = subprocess.run(["ps", "-o", "tty=", "-p", str(os.getppid())], capture_output=True, text=True, timeout=2).stdout.strip()
    except Exception:
        tty = ""
    env = {k: os.environ.get(k) for k in ENV_KEYS if os.environ.get(k)}
    return {"env": env, "tty": ("/dev/" + tty) if tty and tty != "??" else "", "cwd": os.getcwd(), "ancestors": ancestors()}


def post(path, body):
    try:
        req = urllib.request.Request(AGENT + path, data=json.dumps(body).encode(), headers={"Content-Type": "application/json"}, method="POST")
        urllib.request.urlopen(req, timeout=2).read()
    except Exception:
        pass  # the panel being down must never break the tool


def read_stdin_json():
    try:
        if sys.stdin.isatty():
            return {}
        raw = sys.stdin.read()
        return json.loads(raw) if raw.strip() else {}
    except Exception:
        return {}
