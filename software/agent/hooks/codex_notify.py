#!/usr/bin/env python3
"""Codex CLI `notify` program: receives one JSON argument per event."""
import json
import os
import sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _post import context, post  # noqa: E402

try:
    payload = json.loads(sys.argv[1]) if len(sys.argv) > 1 else {}
except Exception:
    payload = {}
body = context()
body["payload"] = payload
post("/api/events/codex", body)
