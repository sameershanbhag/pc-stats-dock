#!/usr/bin/env python3
"""Cursor `stop` hook: the agent finished a turn."""
import os
import sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _post import context, post, read_stdin_json  # noqa: E402

payload = read_stdin_json()
body = context()
roots = payload.get("workspace_roots") or []
if roots:
    body["cwd"] = roots[0]
body.update({"tool": "Cursor", "title": "Finished", "state": "done", "session": str(payload.get("conversation_id") or ""),
             "focus": {"kind": "editor", "app": "Cursor", "bundle": "com.todesktop.230313mzl4w4u92", "folder": body["cwd"], "hint": os.path.basename(body["cwd"].rstrip("/"))}})
post("/api/events", body)
