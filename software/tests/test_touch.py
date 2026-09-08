"""Touch mapper supervisor: status parsing and the start/stop rules (a fake launcher stands in for the real one)."""
import os
import stat
import sys
import tempfile
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "agent"))
import touch  # noqa: E402

FAKE = """#!/bin/sh
echo "12:00:00 touch mapper running for a $2x$3 panel"
echo "status devices=1 panel=1 mode=repost seen=4 mapped=2 main=3840x1080 name=wcidtest"
sleep 30
"""
FAKE_DENIED = """#!/bin/sh
echo "12:00:00 could not create the event tap: allow PC Stats Panel under Accessibility"
exit 3
"""


def script(body):
    d = tempfile.mkdtemp()
    p = os.path.join(d, "launcher")
    with open(p, "w") as f:
        f.write(body)
    os.chmod(p, os.stat(p).st_mode | stat.S_IEXEC)
    return p


def wait_for(pred, timeout=5):
    end = time.time() + timeout
    while time.time() < end:
        if pred():
            return True
        time.sleep(0.05)
    return pred()


class ParseTest(unittest.TestCase):
    def test_parse_status(self):
        d = touch.parse_status("status devices=1 panel=1 follows=1 warp=0 seen=12 mapped=3 main=3840x1080 name=wcidtest touch")
        self.assertEqual((d["devices"], d["panel"], d["follows"], d["mapped"], d["main"], d["name"]), (1, 1, 1, 3, "3840x1080", "wcidtest touch"))
        self.assertEqual(touch.parse_status("status devices=0 panel=0"), {"devices": 0, "panel": 0})


class MapperTest(unittest.TestCase):
    PANEL = {"id": 1, "main": False}
    MAIN_PANEL = {"id": 1, "main": True}

    def setUp(self):
        self.logs = []
        self.logfile = Path(tempfile.mkdtemp()) / "touch.log"

    def make(self, body):
        return touch.TouchMapper((1540, 720), log=self.logs.append, logfile=self.logfile, launcher=script(body))

    def test_runs_only_while_panel_is_not_main(self):
        m = self.make(FAKE)
        m.tick(None); self.assertEqual(m.status(), {"state": "off", "note": "waiting for panel"}); self.assertFalse(m.running())
        m.tick(self.MAIN_PANEL); self.assertEqual(m.status()["note"], "panel is the main display"); self.assertFalse(m.running())
        m.tick(self.PANEL); self.assertTrue(m.running())
        self.assertTrue(wait_for(lambda: m.status()["state"] == "mapped"))
        self.assertEqual(m.status()["device"], "wcidtest"); self.assertEqual(m.status()["touches"], 2)
        m.tick(self.PANEL); m._announce(); self.assertTrue(any("[touch] mapped" in l for l in self.logs), self.logs)
        m.tick(self.MAIN_PANEL); self.assertFalse(m.running()); self.assertEqual(m.status()["state"], "off")
        self.assertIn("touch mapper running for a 1540x720 panel", self.logfile.read_text())

    def test_permission_missing_is_reported_and_retried_later(self):
        m = self.make(FAKE_DENIED)
        m.tick(self.PANEL)
        self.assertTrue(wait_for(lambda: m.status()["state"] == "needs accessibility"))
        self.assertEqual(m.exit_code, 3)
        m.tick(self.PANEL)                       # too soon: no restart storm
        self.assertFalse(m.running())
        m.exit_at -= touch.RETRY_AFTER + 1
        m.tick(self.PANEL)                       # retry window passed: tries again
        self.assertTrue(wait_for(lambda: m.exit_code == 3 and not m.running()))
        m.tick(self.PANEL); m._announce(); self.assertTrue(any("needs accessibility" in l for l in self.logs))

    def test_dry_run_and_missing_launcher(self):
        m = touch.TouchMapper((1540, 720), log=self.logs.append, logfile=self.logfile, launcher=script(FAKE))
        m.tick(self.PANEL, dry_run=True); self.assertFalse(m.running())
        none = touch.TouchMapper((1540, 720), log=self.logs.append, logfile=self.logfile, launcher="")
        none.tick(self.PANEL); self.assertFalse(none.running()); self.assertEqual(none.status()["state"], "off"); self.assertIn("install.sh", none.status()["note"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
