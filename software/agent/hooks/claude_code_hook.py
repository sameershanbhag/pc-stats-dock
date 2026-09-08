#!/usr/bin/env python3
"""Claude Code hook (Stop + Notification): tell the panel this chat finished or needs you."""
import os
import sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _post import context, post, read_stdin_json  # noqa: E402

payload = read_stdin_json()
body = context()
body["payload"] = payload
if payload.get("cwd"):
    body["cwd"] = payload["cwd"]
post("/api/events/claude-code", body)
