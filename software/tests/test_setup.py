"""Login-item setup used by the packaged app (agent/setup.py)."""
import os
import plistlib
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent / "agent"))
import setup  # noqa: E402


class SetupTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.app = self.tmp / "PC Stats Panel.app"
        (self.app / "Contents" / "MacOS").mkdir(parents=True)
        (self.app / "Contents" / "MacOS" / "PCStatsPanel").write_text("#!/bin/sh\n")
        (self.app / "Contents" / "Resources" / "mac").mkdir(parents=True)
        (self.app / "Contents" / "Resources" / "mac" / "dock-admin.sh").write_text("echo dock\n")
        self.home = self.tmp / "home"
        self.patches = [mock.patch.object(setup, "HOME", self.home),
                        mock.patch.object(setup, "PLIST", self.home / "Library" / "LaunchAgents" / "com.pcstatsdock.agent.plist"),
                        mock.patch.object(setup, "LOGS", self.home / "Library" / "Logs" / "pc-stats-dock"),
                        mock.patch.object(setup, "SUPPORT", self.home / "Library" / "Application Support" / "pc-stats-dock")]
        for p in self.patches:
            p.start()

    def tearDown(self):
        for p in self.patches:
            p.stop()

    def test_plist_points_at_the_app_in_service_mode(self):
        body = setup.plist_body(self.app)
        self.assertEqual(body["Label"], "com.pcstatsdock.agent")
        self.assertEqual(body["ProgramArguments"], [str(self.app / "Contents/MacOS/PCStatsPanel"), "--service"])
        self.assertTrue(body["RunAtLoad"] and body["KeepAlive"])
        self.assertTrue(body["WorkingDirectory"].endswith("Resources/app/agent"))
        self.assertIn("/opt/homebrew/bin", body["EnvironmentVariables"]["PATH"])

    def test_install_writes_the_plist_and_loads_it(self):
        calls = []

        def fake_run(argv, dry_run=False, timeout=60):
            calls.append(argv)
            return 0, ""
        with mock.patch.object(setup, "run", side_effect=fake_run), \
             mock.patch.object(setup, "health", return_value={"ok": True, "caps": {"accessibility": False}}):
            rc = setup.install(self.app, quiet=True)
        self.assertEqual(rc, 0)
        with open(setup.PLIST, "rb") as f:
            body = plistlib.load(f)
        self.assertEqual(body["ProgramArguments"][1], "--service")
        self.assertTrue(setup.LOGS.is_dir())
        verbs = [c[1] for c in calls if c[0] == "/bin/launchctl"]
        self.assertEqual(verbs[:2], ["bootout", "bootstrap"])                 # replace an earlier copy, then load
        self.assertTrue(any(c[0] == "/bin/zsh" and c[1].endswith("dock-admin.sh") for c in calls))   # Dock icon

    def test_install_dry_run_touches_nothing(self):
        with mock.patch.object(setup, "health", return_value=None):
            rc = setup.install(self.app, dry_run=True, quiet=True)
        self.assertEqual(rc, 0)
        self.assertFalse(setup.PLIST.exists())

    def test_install_refuses_a_folder_without_the_launcher(self):
        self.assertEqual(setup.install(self.tmp / "nope.app", quiet=True), 2)

    def test_uninstall_removes_the_plist_and_the_dock_icon(self):
        setup.PLIST.parent.mkdir(parents=True)
        setup.PLIST.write_bytes(plistlib.dumps(setup.plist_body(self.app)))
        calls = []
        with mock.patch.object(setup, "run", side_effect=lambda argv, dry_run=False, timeout=60: (calls.append(argv), (0, ""))[1]):
            rc = setup.uninstall(self.app)
        self.assertEqual(rc, 0)
        self.assertFalse(setup.PLIST.exists())
        self.assertTrue(any(c[0] == "/bin/launchctl" and c[1] == "bootout" for c in calls))
        self.assertTrue(any(c[-1] == "--remove" for c in calls))
        self.assertTrue(any(c[:3] == ["/usr/bin/tccutil", "reset", "Accessibility"] for c in calls))

    def test_cli(self):
        with mock.patch.object(setup, "install", return_value=0) as inst:
            self.assertEqual(setup.main(["install", "--app", str(self.app), "--quiet"]), 0)
            inst.assert_called_once_with(str(self.app), False, True)
        with mock.patch.object(setup, "uninstall", return_value=0) as un:
            self.assertEqual(setup.main(["uninstall"]), 0)
            un.assert_called_once_with(None, False)


if __name__ == "__main__":
    unittest.main()
