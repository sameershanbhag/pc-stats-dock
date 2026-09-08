#!/usr/bin/env python3
"""Tell the panel anything finished. Use from any script or tool:
   notify.py --tool Gemini --title "Finished" --text "3 files changed"
   some-long-command && notify.py --tool "make" --title "Build done"
"""
import argparse
import os
import sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _post import context, post  # noqa: E402

ap = argparse.ArgumentParser()
ap.add_argument("--tool", default="AI")
ap.add_argument("--title", default="Finished")
ap.add_argument("--text", default="")
ap.add_argument("--state", default="done", choices=["done", "needs_input", "info"])
ap.add_argument("--project", default="")
ap.add_argument("--session", default="")
a = ap.parse_args()
body = context()
body.update({"tool": a.tool, "title": a.title, "text": a.text, "state": a.state, "project": a.project, "session": a.session})
post("/api/events", body)
