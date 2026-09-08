"""Display arrangement: parsing displayplacer, planning, sweeping (all subprocesses mocked)."""
import sys
import unittest
from pathlib import Path
from unittest import mock
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "agent"))
import arrange  # noqa: E402

LIST = """Persistent screen id: AAA
Contextual screen id: 1
Type: 24 inch external screen
Resolution: 1540x720
Hertz: 60
Color Depth: 8
Scaling: off
Origin: (0,0) - main display
Rotation: 0
Resolutions for rotation 0: mode 0: res:1540x720
Persistent screen id: BBB
Contextual screen id: 2
Type: 49 inch external screen
Resolution: 3840x1080
Hertz: 120
Color Depth: 8
Scaling: off
Origin: (1540,0)
Rotation: 0
"""


class TestArrange(unittest.TestCase):
    def test_parse_and_plan_below(self):
        screens = arrange.parse_list(LIST)
        self.assertEqual([(s["persistent"], s["res"], s["origin"], s["main"]) for s in screens], [("AAA", (1540, 720), (0, 0), True), ("BBB", (3840, 1080), (1540, 0), False)])
        argv, panel, main = arrange.plan(screens, (1540, 720), "below")
        self.assertEqual(panel["persistent"], "AAA"); self.assertEqual(main["persistent"], "BBB")
        self.assertIn("id:BBB res:3840x1080 hz:120 color_depth:8 enabled:true scaling:off origin:(0,0) degree:0", argv[1])
        self.assertIn("id:AAA res:1540x720 hz:60 color_depth:8 enabled:true scaling:off origin:(1150,1080) degree:0", argv[2])

    def test_origins(self):
        self.assertEqual(arrange.panel_origin("above", (3840, 1080), (1540, 720)), (1150, -720))
        self.assertEqual(arrange.panel_origin("left", (3840, 1080), (1540, 720)), (-1540, 360))
        self.assertEqual(arrange.panel_origin("right", (3840, 1080), (1540, 720)), (3840, 360))

    def test_arrange_states(self):
        with mock.patch.object(arrange, "displayplacer_path", return_value=None):
            self.assertEqual(arrange.arrange((1540, 720))[0], False)
        with mock.patch.object(arrange, "displayplacer_path", return_value="/dp"), mock.patch.object(arrange.subprocess, "run") as run:
            run.return_value = mock.Mock(returncode=0, stdout=LIST, stderr="")
            changed, msg = arrange.arrange((1540, 720), "below", dry_run=True)
            self.assertTrue(changed); self.assertIn("origin:(1150,1080)", msg)
            done = LIST.replace("Origin: (0,0) - main display", "Origin: (1150,1080)").replace("Origin: (1540,0)", "Origin: (0,0) - main display")
            run.return_value = mock.Mock(returncode=0, stdout=done, stderr="")
            self.assertEqual(arrange.arrange((1540, 720), "below"), (False, "arrangement already right"))
            run.return_value = mock.Mock(returncode=0, stdout=LIST.split("Persistent screen id: BBB")[0], stderr="")
            self.assertEqual(arrange.arrange((1540, 720), "below")[1], "only one display")

    def test_sweep_script_compiles(self):
        import shutil, subprocess, tempfile, os
        if not shutil.which("osacompile"):
            self.skipTest("macOS only")
        out = os.path.join(tempfile.mkdtemp(), "sweep.scpt")
        r = subprocess.run(["osacompile", "-o", out, "-e", arrange.SWEEP_ONE], capture_output=True, text=True, timeout=20)
        self.assertEqual(r.returncode, 0, r.stderr)

    def test_sweep_pids_skip_dashboard(self):
        panel = {"w": 1540, "h": 720}
        wins = [{"pid": 10, "app": "Chrome", "x": 1150, "y": 1080, "w": 1540, "h": 720},   # the dashboard: full panel size
                {"pid": 10, "app": "Chrome", "x": 1150, "y": 1080, "w": 1540, "h": 115},   # dashboard's own popup: same pid
                {"pid": 20, "app": "Slack", "x": 1200, "y": 1100, "w": 900, "h": 600},
                {"pid": 30, "app": "Notes", "x": 1300, "y": 1150, "w": 500, "h": 400},
                {"pid": 0, "app": "?", "x": 1300, "y": 1150, "w": 500, "h": 400}]
        self.assertEqual(arrange.sweep_pids(panel, wins), [10, 20, 30])
        self.assertEqual(arrange.sweep_pids(panel, wins, skip_pids=[10]), [20, 30])

    def test_sweep(self):
        panel = {"x": 1150, "y": 1080, "w": 1540, "h": 720}; main = {"x": 0, "y": 0, "w": 3840, "h": 1080}
        wins = [{"pid": 20, "app": "Slack", "x": 1200, "y": 1100, "w": 900, "h": 600}, {"pid": 30, "app": "Notes", "x": 1300, "y": 1150, "w": 500, "h": 400}]
        with mock.patch.object(arrange, "windows_on", return_value=[]):
            self.assertEqual(arrange.sweep(panel, main), (True, "no windows on the panel"))
        with mock.patch.object(arrange, "windows_on", return_value=wins):
            ok, msg = arrange.sweep(panel, main, dry_run=True); self.assertTrue(ok); self.assertIn("2 process", msg)
            with mock.patch.object(arrange.subprocess, "run", side_effect=[mock.Mock(returncode=0, stdout="2\n", stderr=""), mock.Mock(returncode=0, stdout="1\n", stderr="")]) as run:
                self.assertEqual(arrange.sweep(panel, main), (True, "moved 3 windows back to the main display"))
                self.assertEqual(run.call_count, 2)
                self.assertEqual(run.call_args_list[0][0][0][3:], ["20", "1150", "1080", "1540", "720", "0", "0", "3840", "1080"])
                self.assertEqual(run.call_args_list[1][0][0][3], "30")
            with mock.patch.object(arrange.subprocess, "run", side_effect=[mock.Mock(returncode=1, stdout="", stderr="osascript is not allowed assistive access (-25211)"), mock.Mock(returncode=0, stdout="0\n", stderr="")]):
                ok, msg = arrange.sweep(panel, main); self.assertFalse(ok); self.assertIn("Accessibility", msg)
            with mock.patch.object(arrange.subprocess, "run", side_effect=[arrange.subprocess.TimeoutExpired("osascript", 8), mock.Mock(returncode=0, stdout="1\n", stderr="")]):
                ok, msg = arrange.sweep(panel, main); self.assertTrue(ok); self.assertIn("moved 1 window ", msg); self.assertIn("did not answer", msg)
        with mock.patch.object(arrange, "windows_on", return_value=None), mock.patch.object(arrange, "visible_pids", return_value=[5, 6, 7]):
            ok, msg = arrange.sweep(panel, main, dry_run=True, skip_pids=[6]); self.assertIn("2 process", msg)

    def test_default_position_is_above(self):
        self.assertEqual(arrange.panel_origin("above", (3840, 1080), (1540, 720)), (1150, -720))
        self.assertEqual(arrange.panel_origin("weird", (3840, 1080), (1540, 720)), (1150, -720))

    def test_main_app_to_activate(self):
        main = {"x": 0, "y": 0, "w": 3840, "h": 1080}
        wins = [{"pid": 10, "app": "Chrome", "x": 0, "y": -763, "w": 1540, "h": 41},        # dashboard helper window (off screen)
                {"pid": 10, "app": "Chrome", "x": 1150, "y": -720, "w": 1540, "h": 720},    # the dashboard
                {"pid": 20, "app": "Safari", "x": 300, "y": 100, "w": 1200, "h": 800},
                {"pid": 30, "app": "Notes", "x": 900, "y": 200, "w": 500, "h": 400}]
        self.assertEqual(arrange.main_app_to_activate(wins, main, [10], front=10), 20)
        self.assertIsNone(arrange.main_app_to_activate(wins, main, [10], front=20))        # Safari already active
        self.assertIsNone(arrange.main_app_to_activate(wins, main, [10], front=None))
        self.assertIsNone(arrange.main_app_to_activate(wins[:2], main, [10], front=10))    # nothing else on the main display
        self.assertIsNone(arrange.main_app_to_activate(None, main, [10], front=10))

    def test_app_path_of(self):
        self.assertEqual(arrange.app_path_of(1, exe="/Applications/Safari.app/Contents/MacOS/Safari"), "/Applications/Safari.app")
        self.assertEqual(arrange.app_path_of(1, exe="/System/Applications/System Settings.app/Contents/MacOS/System Settings"), "/System/Applications/System Settings.app")
        self.assertIsNone(arrange.app_path_of(1, exe="/usr/bin/python3"))

    def test_activate_main_app(self):
        main = {"x": 0, "y": 0, "w": 3840, "h": 1080}
        wins = [{"pid": 10, "app": "Chrome", "x": 1150, "y": -720, "w": 1540, "h": 720}, {"pid": 20, "app": "Safari", "x": 300, "y": 100, "w": 1200, "h": 800}]
        with mock.patch.object(arrange, "_window_list", return_value=wins), mock.patch.object(arrange, "front_pid", return_value=10), \
             mock.patch.object(arrange, "app_path_of", return_value="/Applications/Safari.app"):
            self.assertEqual(arrange.activate_main_app(main, [10], dry_run=True), (True, "dry run: activate /Applications/Safari.app"))
            with mock.patch.object(arrange, "activate_pid", return_value=(True, "objc")) as act:
                self.assertEqual(arrange.activate_main_app(main, [10]), (True, "activated Safari.app (objc)")); act.assert_called_once_with(20)
            with mock.patch.object(arrange, "activate_pid", return_value=(False, "osascript")), mock.patch.object(arrange.subprocess, "run") as run:
                self.assertEqual(arrange.activate_main_app(main, [10]), (True, "activated Safari.app (osascript)"))
                self.assertEqual(run.call_args[0][0], ["/usr/bin/open", "/Applications/Safari.app"])   # last resort
        with mock.patch.object(arrange, "_window_list", return_value=wins), mock.patch.object(arrange, "front_pid", return_value=20):
            self.assertEqual(arrange.activate_main_app(main, [10]), (False, "main display already active"))

if __name__ == "__main__":
    unittest.main(verbosity=2)
