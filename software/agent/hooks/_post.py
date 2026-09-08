"""Shared by the hook scripts: gather where we are and POST to the agent, never blocking the tool."""
import json
import os
import subprocess
import sys
import urllib.request

AGENT = os.environ.get("PCSTATS_AGENT", "http://127.0.0.1:4400")


def context():
    try:
        tty = subprocess.run(["ps", "-o", "tty=", "-p", str(os.getppid())], capture_output=True, text=True, timeout=2).stdout.strip()
    except Exception:
        tty = ""
    env = {k: os.environ.get(k) for k in ("TERM_PROGRAM", "TERM_SESSION_ID", "ITERM_SESSION_ID", "TMUX_PANE", "__CFBundleIdentifier", "TERM") if os.environ.get(k)}
    return {"env": env, "tty": ("/dev/" + tty) if tty and tty != "??" else "", "cwd": os.getcwd()}


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
