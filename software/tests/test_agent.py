"""Function-by-function tests for the Mac agent. Run:  python3 -m unittest -v tests.test_agent
Nothing here touches the real system: osascript, open, shortcuts and macmon are all mocked."""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "agent"))
import agent  # noqa: E402
import displays  # noqa: E402
import keys_mac  # noqa: E402
import sensors_mac  # noqa: E402

MACMON_SAMPLE = {
    "cpu_active_ratio": 0.115, "gpu_active_ratio": 0.047, "cpu_power": 0.62, "gpu_power": 0.11, "sys_power": 19.4,
    "ane_power": 0.0, "pcpu_freq_mhz": 1619, "gpu_freq_mhz": 338, "gpu_usage": [338, 0.0158],
    "temp": {"cpu_temp_avg": 41.9, "gpu_temp_avg": 44.9}, "fans": [{"name": "fan0", "rpm": 1000.0, "max_rpm": 4900}],
    "memory": {"ram_total": 68719476736, "ram_usage": 17611227136, "swap_total": 1073741824, "swap_usage": 262144},
}
NETSTAT_1 = """Name       Mtu   Network       Address            Ipkts Ierrs     Ibytes    Opkts Oerrs     Obytes  Coll
lo0        16384 <Link#1>                          100     0      10000      100     0      10000     0
en0        1500  <Link#4>    aa:bb:cc:dd:ee:ff     500     0    1000000      400     0     200000     0
en0        1500  192.168.1     192.168.1.10         500     -    1000000      400     -     200000     -
utun3      1400  <Link#20>                          10     0       5000        9     0       4000     0
awdl0      1500  <Link#12>   02:00:00:00:00:00       0     0          0        0     0          0     0
"""
NETSTAT_2 = NETSTAT_1.replace("1000000", "3000000").replace("200000", "300000")


# ------------------------------------------------------------------ sensors ----
class TestNetwork(unittest.TestCase):
    def test_parse_handles_missing_address_column_and_skips_loopback(self):
        t = sensors_mac.Network.parse_netstat(NETSTAT_1)
        self.assertEqual(t["en0"], (1000000, 200000))
        self.assertEqual(t["utun3"], (5000, 4000))
        self.assertNotIn("lo0", t)
        self.assertNotIn("awdl0", t)

    def test_delta_gives_megabits_per_second_on_busiest_interface(self):
        n = sensors_mac.Network()
        with mock.patch.object(sensors_mac, "_run", return_value=NETSTAT_1):
            self.assertIsNone(n.read())          # first sample has no delta yet
        n.prev = (n.prev[0] - 1.0, n.prev[1])    # pretend a second passed
        with mock.patch.object(sensors_mac, "_run", return_value=NETSTAT_2):
            r = n.read()
        self.assertEqual(r["name"], "en0")
        self.assertAlmostEqual(r["down_mbps"], 2_000_000 * 8 / 1e6, delta=0.5)
        self.assertAlmostEqual(r["up_mbps"], 100_000 * 8 / 1e6, delta=0.1)

    def test_counter_reset_does_not_go_negative(self):
        n = sensors_mac.Network()
        with mock.patch.object(sensors_mac, "_run", return_value=NETSTAT_2):
            n.read()
        n.prev = (n.prev[0] - 1.0, n.prev[1])
        with mock.patch.object(sensors_mac, "_run", return_value=NETSTAT_1):
            r = n.read()
        self.assertGreaterEqual(r["down_mbps"], 0)


class TestMacSensors(unittest.TestCase):
    def make(self):
        with mock.patch.object(sensors_mac, "chip_name", return_value="Apple M4 Pro"):
            s = sensors_mac.MacSensors.__new__(sensors_mac.MacSensors)
            s.chip = "Apple M4 Pro"
            s.stream = sensors_mac.MacmonStream(start=False)
            s.net = sensors_mac.Network()
            s._basic_t = 0
            s._basic = (None, None, None)
        return s

    def test_macmon_sample_maps_to_dashboard_fields(self):
        s = self.make()
        s.stream.latest = MACMON_SAMPLE
        s.stream.last_ts = time.time()
        with mock.patch.object(sensors_mac, "_run", return_value=NETSTAT_1):
            d = s.read()
        self.assertEqual(d["sensors"], "macmon")
        self.assertEqual(d["cpu"], {"name": "Apple M4 Pro", "load": 11.5, "temp": 41.9, "clock": 1619, "power": 0.6})
        self.assertEqual(d["gpu"]["temp"], 44.9)
        self.assertEqual(d["gpu"]["fan_rpm"], 1000)
        self.assertEqual(d["gpu"]["clock"], 338)
        self.assertEqual(d["ram"]["total_gb"], 64.0)
        self.assertEqual(d["ram"]["used_gb"], 16.4)
        self.assertEqual(d["sys_power"], 19.4)
        self.assertEqual(d["fans"], [{"name": "fan0", "rpm": 1000}])
        self.assertEqual(d["storage"][0]["name"], "Macintosh HD")
        self.assertIn("used_pct", d["storage"][0])

    def test_stale_macmon_falls_back_to_basic(self):
        s = self.make()
        s.stream.latest = MACMON_SAMPLE
        s.stream.last_ts = time.time() - 30
        self.assertFalse(s.stream.ok)
        with mock.patch.object(sensors_mac, "basic_cpu_mem", return_value=(12.5, 8 * 2**30, 16 * 2**30)), \
             mock.patch.object(sensors_mac, "_run", return_value=NETSTAT_1):
            d = s.read()
        self.assertEqual(d["sensors"], "basic")
        self.assertEqual(d["cpu"]["load"], 12.5)
        self.assertIsNone(d["cpu"]["temp"])
        self.assertEqual(d["ram"], {"used_gb": 8.0, "total_gb": 16.0, "load": 50.0})

    def test_basic_cpu_mem_parses_top_and_vm_stat(self):
        outputs = {
            ("/usr/bin/top", "-l", "1", "-n", "0"): "Processes: 600 total\nCPU usage: 6.69% user, 7.67% sys, 85.62% idle \n",
            ("/usr/bin/vm_stat",): "Mach Virtual Memory Statistics: (page size of 16384 bytes)\nPages free: 100.\nPages active: 1000.\nPages wired down: 500.\nPages occupied by compressor: 250.\n",
            ("/usr/sbin/sysctl", "-n", "hw.memsize"): "68719476736\n",
        }
        with mock.patch.object(sensors_mac, "_run", side_effect=lambda cmd, timeout=3: outputs[tuple(cmd)]):
            cpu, used, total = sensors_mac.basic_cpu_mem()
        self.assertEqual(cpu, 14.4)
        self.assertEqual(used, 1750 * 16384)
        self.assertEqual(total, 68719476736)


# ----------------------------------------------------------------- displays ----
FAKE_DISPLAYS = [
    {"id": 3, "x": 0, "y": 0, "w": 3840, "h": 1080, "px_w": 3840, "px_h": 1080, "main": True, "builtin": False},
    {"id": 7, "x": 3840, "y": 200, "w": 1540, "h": 720, "px_w": 1540, "px_h": 720, "main": False, "builtin": False},
]


class TestDisplays(unittest.TestCase):
    def test_real_display_list_has_shape(self):
        ds = displays.list_displays()
        self.assertTrue(ds)
        for k in ("id", "x", "y", "w", "h", "px_w", "px_h", "main", "builtin"):
            self.assertIn(k, ds[0])
        self.assertEqual(sum(d["main"] for d in ds), 1)

    def test_find_panel_matches_points_pixels_and_rotation(self):
        with mock.patch.object(displays, "list_displays", return_value=FAKE_DISPLAYS):
            self.assertEqual(displays.find_panel(1540, 720)["id"], 7)
            self.assertEqual(displays.find_panel(720, 1540)["id"], 7)
            self.assertIsNone(displays.find_panel(1920, 1080))
        hidpi = [dict(FAKE_DISPLAYS[1], w=770, h=360)]
        with mock.patch.object(displays, "list_displays", return_value=hidpi):
            self.assertEqual(displays.find_panel(1540, 720)["id"], 7)

    def test_kiosk_opens_reopens_and_closes(self):
        logs = []
        k = displays.Kiosk("http://127.0.0.1:4400/", 1540, 720, log=logs.append, profile="/tmp/kiosk-test-profile")
        k.browser = "/fake/chrome"
        with mock.patch.object(displays.subprocess, "Popen") as popen, mock.patch.object(k, "_pids", return_value=[]):
            proc = mock.Mock(); proc.poll.return_value = None; popen.return_value = proc
            with mock.patch.object(displays, "find_panel", return_value=FAKE_DISPLAYS[1]):
                k.tick()                                     # panel appears -> open
            self.assertTrue(popen.called)
            args = popen.call_args[0][0]
            self.assertIn("--kiosk", args); self.assertIn("--window-position=3840,200", args); self.assertIn("--window-size=1540,720", args)
            self.assertEqual(args[-1], "http://127.0.0.1:4400/")
            popen.reset_mock()
            with mock.patch.object(displays, "find_panel", return_value=FAKE_DISPLAYS[1]):
                k.tick()                                     # still plugged, still running -> nothing
            self.assertFalse(popen.called)
            proc.poll.return_value = 0                       # window gone (closed, crashed, killed): comes back, rate limited
            with mock.patch.object(displays, "find_panel", return_value=FAKE_DISPLAYS[1]):
                k.tick()
                self.assertFalse(popen.called)
                k.opened_at -= displays.Kiosk.REOPEN_EVERY
                k.tick()
            self.assertTrue(popen.called)
            proc.poll.return_value = None
            with mock.patch.object(displays, "find_panel", return_value=None):
                k.tick()                                     # unplug while running -> terminate
            self.assertTrue(proc.terminate.called)

    def test_kiosk_adopts_window_from_previous_run(self):
        k = displays.Kiosk("http://127.0.0.1:4400/", 1540, 720, log=lambda m: None, profile="/tmp/kiosk-test-profile")
        with mock.patch.object(k, "_pids", return_value=[4242]):
            self.assertTrue(k.running())
        with mock.patch.object(k, "_pids", return_value=[]):
            self.assertFalse(k.running())


# ------------------------------------------------------------------ actions ----
class TestActions(unittest.TestCase):
    def setUp(self):
        agent.DRY_RUN = False
        self.state = agent.State()

    def test_hotkey_event_sequence(self):
        posted = []
        with mock.patch.object(keys_mac, "_post", side_effect=lambda code, down, flags: posted.append((code, down, flags))), \
             mock.patch.object(keys_mac, "ax_trusted", return_value=True), mock.patch.object(keys_mac.time, "sleep"):
            agent.send_hotkey(["ctrl", "left"])
            CTRL, FN = 1 << 18, 1 << 23     # arrows get fn pressed around them: macOS matches its own shortcuts on the fn state
            self.assertEqual(posted, [(63, True, FN), (59, True, FN | CTRL), (123, True, FN | CTRL), (123, False, FN | CTRL), (59, False, FN), (63, False, 0)])
            posted.clear(); agent.send_hotkey(["cmd", "shift", "4"])
            CMD, SHIFT = 1 << 20, 1 << 17
            self.assertEqual(posted, [(55, True, CMD), (56, True, CMD | SHIFT), (21, True, CMD | SHIFT), (21, False, CMD | SHIFT),
                                      (56, False, CMD), (55, False, 0)])
            posted.clear(); agent.send_hotkey(["f11"])
            self.assertEqual(posted, [(63, True, 1 << 23), (103, True, 1 << 23), (103, False, 1 << 23), (63, False, 0)])
        self.assertEqual(keys_mac.parse(["cmd", "space"]), (1 << 20, [55], 49))
        self.assertEqual(keys_mac.parse(["alt", "\\"])[2], 42)
        with self.assertRaises(ValueError):
            keys_mac.parse(["cmd"])
        with self.assertRaises(ValueError):
            keys_mac.parse(["ctrl", "nosuchkey"])
        self.assertEqual(keys_mac.describe(["ctrl", "left"]), "⌃ ←")

    def test_hotkey_without_permission_explains_and_prompts(self):
        with mock.patch.object(keys_mac, "ax_trusted", return_value=False) as ax, mock.patch.object(keys_mac, "_post") as post, \
             mock.patch.object(agent.subprocess, "Popen") as popen:
            ok, msg = agent.run_action({"type": "hotkey", "keys": ["ctrl", "left"]}, self.state)
        self.assertFalse(ok); self.assertIn("Accessibility", msg); self.assertIn("PC Stats Panel", msg)
        self.assertFalse(post.called)
        self.assertTrue(any(c.kwargs.get("prompt") for c in ax.call_args_list), "system prompt requested")
        self.assertEqual(popen.call_args[0][0], [agent.OPEN, agent.ACCESSIBILITY_PANE])
        self.assertFalse(self.state.caps["accessibility"])
        # a second failure within the throttle window does not open the pane again
        with mock.patch.object(keys_mac, "ax_trusted", return_value=False), mock.patch.object(agent.subprocess, "Popen") as popen2:
            agent.run_action({"type": "hotkey", "keys": ["ctrl", "left"]}, self.state)
        self.assertFalse(popen2.called)

    def test_automation_hint_for_applescript(self):
        with mock.patch.object(agent, "osascript", side_effect=RuntimeError("Not authorized to send Apple events to Music. (-1743)")):
            ok, msg = agent.run_action({"type": "applescript", "command": 'tell application "Music" to playpause'}, self.state)
        self.assertFalse(ok); self.assertIn("Automation", msg)

    def test_open_app_tries_name_then_bundle_id_then_paths(self):
        def fake_run(cmd, **kw):
            r = mock.Mock(); r.returncode = 0 if cmd == [agent.OPEN, "-b", "com.apple.Terminal"] or cmd == [agent.OPEN, "-a", "Safari"] or cmd[0:1] == [agent.OPEN] and cmd[1].startswith("/Applications") else 1
            r.stderr = "" if r.returncode == 0 else "Unable to find application"
            return r
        with mock.patch.object(agent.subprocess, "run", side_effect=fake_run) as run:
            self.assertEqual(agent.open_app("Safari"), (True, "opened Safari"))
            self.assertEqual(agent.open_app("com.apple.Terminal")[0], True)
            self.assertEqual(run.call_args_list[-2][0][0], [agent.OPEN, "-a", "com.apple.Terminal"])   # tried as a name first
            self.assertEqual(run.call_args_list[-1][0][0], [agent.OPEN, "-b", "com.apple.Terminal"])
            self.assertEqual(agent.open_app("/Applications/Slack.app")[0], True)
            ok, msg = agent.open_app("Nope")
            self.assertFalse(ok); self.assertIn("Unable to find", msg)

    def test_volume_and_mute(self):
        scripts = []
        settings = {"output volume": "50", "input volume": "60", "alert volume": "100", "output muted": "false"}
        with mock.patch.object(agent, "volume_settings", return_value=settings), mock.patch.object(agent, "osascript", side_effect=lambda s, timeout=8: scripts.append(s) or ""):
            self.assertEqual(agent.run_action({"type": "volume", "dir": "up"}, self.state), (True, "volume 57"))
            self.assertEqual(agent.run_action({"type": "volume", "dir": "down"}, self.state), (True, "volume 43"))
            self.assertEqual(agent.run_action({"type": "volume", "dir": "mute"}, self.state), (True, "speakers muted"))
        self.assertEqual(scripts, ["set volume output volume 57", "set volume output volume 43", "set volume with output muted"])
        with mock.patch.object(agent, "volume_settings", return_value={"output volume": "missing value"}), mock.patch.object(agent, "ddc_volume", return_value=None):
            ok, msg = agent.run_action({"type": "volume", "dir": "up"}, self.state)
        self.assertFalse(ok); self.assertIn("DisplayPort", msg); self.assertIn("Spotify", msg)
        with mock.patch.object(agent, "volume_settings", return_value={"output volume": "missing value"}), mock.patch.object(agent, "ddc_volume", return_value=40), \
             mock.patch.object(agent, "M1DDC", "/fake/m1ddc"), mock.patch.object(agent.subprocess, "run", return_value=mock.Mock(returncode=0, stderr="")) as run:
            self.assertEqual(agent.run_action({"type": "volume", "dir": "up"}, self.state), (True, "monitor volume 47"))
            self.assertEqual(run.call_args[0][0], ["/fake/m1ddc", "set", "volume", "47"])
            self.assertEqual(agent.run_action({"type": "volume", "dir": "mute"}, self.state), (True, "monitor volume 0"))

    def test_ddc_volume_probe(self):
        with mock.patch.object(agent, "M1DDC", "/fake/m1ddc"), mock.patch.object(agent.subprocess, "run", return_value=mock.Mock(returncode=0, stdout="35\n")):
            self.assertEqual(agent.ddc_volume(), 35)
        with mock.patch.object(agent, "M1DDC", "/fake/m1ddc"), mock.patch.object(agent.subprocess, "run", return_value=mock.Mock(returncode=1, stdout="", stderr="DDC communication failure")):
            self.assertIsNone(agent.ddc_volume())
        with mock.patch.object(agent, "M1DDC", None):
            self.assertIsNone(agent.ddc_volume())

    def test_refresh_caps(self):
        agent.DRY_RUN = False
        with mock.patch.object(keys_mac, "ax_state", return_value="trusted"), mock.patch.object(agent, "volume_settings", return_value={"output volume": "missing value", "input volume": "55"}), \
             mock.patch.object(agent, "ddc_volume", return_value=None):
            agent.refresh_caps(self.state)
        self.assertEqual(self.state.caps, {"accessibility": True, "volume": "none", "mic": True, "touch": "off"}); self.assertFalse(self.state.mic_muted)
        with mock.patch.object(keys_mac, "ax_state", return_value="untrusted"), mock.patch.object(agent, "volume_settings", return_value={"output volume": "50", "input volume": "missing value"}):
            agent.refresh_caps(self.state)
        self.assertEqual(self.state.caps, {"accessibility": False, "volume": "system", "mic": False, "touch": "off"})

    def test_mic_toggle_restores_previous_level(self):
        scripts = []
        level = {"v": "60"}
        with mock.patch.object(agent, "volume_settings", side_effect=lambda: {"input volume": level["v"]}), \
             mock.patch.object(agent, "osascript", side_effect=lambda s, timeout=8: scripts.append(s) or ""):
            self.assertEqual(agent.run_action({"type": "mic"}, self.state), (True, "mic muted"))
            self.assertTrue(self.state.mic_muted); self.assertEqual(self.state.mic_restore, 60)
            level["v"] = "0"
            self.assertEqual(agent.run_action({"type": "mic"}, self.state), (True, "mic live"))
            self.assertFalse(self.state.mic_muted)
        self.assertEqual(scripts, ["set volume input volume 0", "set volume input volume 60"])
        with mock.patch.object(agent, "volume_settings", return_value={"input volume": "missing value"}):
            self.assertFalse(agent.run_action({"type": "mic"}, self.state)[0])

    def test_mic_state_reads_level(self):
        with mock.patch.object(agent, "volume_settings", return_value={"input volume": "0"}):
            self.assertTrue(agent.mic_state(self.state))
        with mock.patch.object(agent, "volume_settings", return_value={"input volume": "72"}):
            self.assertFalse(agent.mic_state(self.state)); self.assertEqual(self.state.mic_restore, 72)
        with mock.patch.object(agent, "volume_settings", return_value={"input volume": "missing value"}):
            self.assertIsNone(agent.mic_state(self.state))

    def test_shortcut_applescript_shell_empty_unknown(self):
        r_ok = mock.Mock(returncode=0, stderr=""); r_bad = mock.Mock(returncode=1, stderr="No shortcut named X")
        with mock.patch.object(agent.subprocess, "run", return_value=r_ok):
            self.assertEqual(agent.run_action({"type": "shortcut", "name": "Focus"}, self.state), (True, "ran shortcut Focus"))
        with mock.patch.object(agent.subprocess, "run", return_value=r_bad):
            self.assertEqual(agent.run_action({"type": "shortcut", "name": "X"}, self.state), (False, "No shortcut named X"))
        with mock.patch.object(agent, "osascript", return_value="playing") as osa:
            self.assertEqual(agent.run_action({"type": "applescript", "command": 'tell application "Music" to playpause'}, self.state), (True, "playing"))
            self.assertEqual(osa.call_args[1]["timeout"], 20)
        with mock.patch.object(agent.subprocess, "Popen") as popen:
            ok, msg = agent.run_action({"type": "shell", "command": "screencapture -i -c"}, self.state)
            self.assertTrue(ok); self.assertEqual(popen.call_args[0][0], ["/bin/zsh", "-lc", "screencapture -i -c"])
        self.assertEqual(agent.run_action({"type": "empty"}, self.state), (True, "blank slot"))
        self.assertFalse(agent.run_action({"type": "teleport"}, self.state)[0])

    def test_dry_run_never_executes(self):
        agent.DRY_RUN = True
        with mock.patch.object(agent, "osascript") as osa, mock.patch.object(agent.subprocess, "run") as run, mock.patch.object(keys_mac, "_post") as post:
            for b in ({"type": "hotkey", "keys": ["ctrl", "left"]}, {"type": "app", "app": "Safari"}, {"type": "shell", "command": "rm -rf /"}):
                ok, msg = agent.run_action(b, self.state)
                self.assertTrue(ok); self.assertTrue(msg.startswith("dry run"))
        self.assertFalse(osa.called); self.assertFalse(run.called); self.assertFalse(post.called)
        agent.DRY_RUN = False

    def test_volume_settings_parse(self):
        with mock.patch.object(agent, "osascript", return_value="output volume:50, input volume:missing value, alert volume:100, output muted:false"):
            self.assertEqual(agent.volume_settings(), {"output volume": "50", "input volume": "missing value", "alert volume": "100", "output muted": "false"})

    def test_osascript_raises_on_failure(self):
        with mock.patch.object(agent.subprocess, "run", return_value=mock.Mock(returncode=1, stderr="x\nreal error", stdout="")):
            with self.assertRaises(RuntimeError) as cm:
                agent.osascript("boom")
        self.assertEqual(str(cm.exception), "real error")


# --------------------------------------------------------------- validation ----
class TestConfigLocation(unittest.TestCase):
    def test_first_run_seeds_config_from_defaults_and_saves_there(self):
        tmp = Path(tempfile.mkdtemp()) / "nested" / "config.json"
        with mock.patch.object(agent, "CONFIG_PATH", tmp):
            cfg = agent.load_config()
            self.assertTrue(tmp.exists()); self.assertEqual(len(cfg["buttons"]), 12)
            cfg["buttons"] = cfg["buttons"][:2]; agent.save_config(cfg)
            self.assertEqual(len(json.loads(tmp.read_text())["buttons"]), 2)
            self.assertEqual(len(agent.load_config()["buttons"]), 2)   # user's edits win over shipped defaults
        shutil.rmtree(tmp.parents[1], ignore_errors=True)


class TestLauncherTransport(unittest.TestCase):
    """keys_mac talks to the compiled launcher over two pipes; fake the launcher end here."""

    def _with_fake_launcher(self, responder):
        req_r, req_w = os.pipe(); rep_r, rep_w = os.pipe()
        def serve():
            buf = b""
            while True:
                chunk = os.read(req_r, 4096)
                if not chunk:
                    break
                buf += chunk
                while b"\n" in buf:
                    line, buf = buf.split(b"\n", 1)
                    os.write(rep_w, (responder(line.decode()) + "\n").encode())
        t = threading.Thread(target=serve, daemon=True); t.start()
        return mock.patch.multiple(keys_mac, _REQ=str(req_w), _REP=str(rep_r)), (req_w, rep_r)

    def test_ax_and_events_go_through_launcher(self):
        seen = []
        def responder(line):
            seen.append(line)
            return {"ax": "1", "prompt": "1"}.get(line, "ok" if line.startswith("events ") else "err unknown request")
        patcher, fds = self._with_fake_launcher(responder)
        with patcher, mock.patch.object(keys_mac, "_post") as post:
            self.assertTrue(keys_mac.via_launcher())
            self.assertTrue(keys_mac.ax_trusted()); self.assertTrue(keys_mac.ax_trusted(prompt=True))
            keys_mac.post_hotkey(["ctrl", "left"])
        self.assertEqual(seen[:2], ["ax", "prompt"])
        self.assertEqual(seen[2], "ax")                                   # trust re-checked before posting
        self.assertEqual(seen[3], "events 63:1:8388608,59:1:8650752,123:1:8650752,123:0:8650752,59:0:8388608,63:0:0")
        self.assertFalse(post.called, "nothing is posted from the python process when the launcher is present")
        for fd in fds: os.close(fd)

    def test_ax_state_and_restart_on_late_grant(self):
        patcher, fds = self._with_fake_launcher(lambda line: "restart" if line == "ax" else "0")
        with patcher:
            self.assertEqual(keys_mac.ax_state(), "restart")
            st = agent.State(); agent.DRY_RUN = False
            with mock.patch.object(agent, "restart_self") as restart, mock.patch.object(agent, "volume_settings", return_value={}):
                agent.refresh_caps(st)
            self.assertTrue(restart.called)
        for fd in fds: os.close(fd)
        patcher, fds = self._with_fake_launcher(lambda line: "1")
        with patcher:
            self.assertEqual(keys_mac.ax_state(), "trusted")
        for fd in fds: os.close(fd)
        patcher, fds = self._with_fake_launcher(lambda line: "0")
        with patcher:
            self.assertEqual(keys_mac.ax_state(), "untrusted")
        for fd in fds: os.close(fd)

    def test_launcher_refusal_and_untrusted(self):
        patcher, fds = self._with_fake_launcher(lambda line: "0" if line == "ax" else "err not trusted")
        with patcher:
            with self.assertRaises(PermissionError):
                keys_mac.post_hotkey(["ctrl", "left"])
        for fd in fds: os.close(fd)
        patcher, fds = self._with_fake_launcher(lambda line: "1" if line == "ax" else "err no events")
        with patcher:
            with self.assertRaises(RuntimeError) as cm:
                keys_mac.post_hotkey(["ctrl", "left"])
            self.assertEqual(str(cm.exception), "no events")
        for fd in fds: os.close(fd)

    def test_launcher_gone_reports_untrusted_not_crash(self):
        req_r, req_w = os.pipe(); rep_r, rep_w = os.pipe(); os.close(rep_w); os.close(req_r)
        with mock.patch.multiple(keys_mac, _REQ=str(req_w), _REP=str(rep_r)):
            with mock.patch.object(keys_mac.os, "write"):
                self.assertFalse(keys_mac.ax_trusted())
        os.close(req_w); os.close(rep_r)

    def test_event_sequence_shapes(self):
        self.assertEqual(keys_mac.event_sequence(["f11"]), [(63, True, 1 << 23), (103, True, 1 << 23), (103, False, 1 << 23), (63, False, 0)])
        CMD, SHIFT = 1 << 20, 1 << 17
        self.assertEqual(keys_mac.event_sequence(["cmd", "shift", "4"]),
                         [(55, True, CMD), (56, True, CMD | SHIFT), (21, True, CMD | SHIFT), (21, False, CMD | SHIFT), (56, False, CMD), (55, False, 0)])


class TestValidate(unittest.TestCase):
    def test_accepts_every_type_and_normalises(self):
        clean, err = agent.validate_buttons([
            {"label": "x", "type": "hotkey", "keys": ["CTRL", "Left"]}, {"label": "y", "type": "app", "app": " Safari "},
            {"label": "z", "type": "shortcut", "name": "Focus"}, {"label": "w", "type": "applescript", "command": "beep"},
            {"label": "v", "type": "shell", "command": "true"}, {"type": "empty"}, {"label": "m", "type": "mic"},
            {"label": "vol", "type": "volume", "dir": "sideways"}, {"id": "dup", "label": "a", "type": "mic"}, {"id": "dup", "label": "b", "type": "mic"},
        ])
        self.assertIsNone(err)
        self.assertEqual(clean[0]["keys"], ["ctrl", "left"]); self.assertEqual(clean[1]["app"], "Safari")
        self.assertEqual(clean[7]["dir"], "up"); self.assertEqual(clean[5]["id"], "slot-6")
        self.assertEqual(clean[9]["id"], "slot-10")   # duplicate id renamed

    def test_rejects_bad_input(self):
        for bad, frag in ((None, "list"), ([1], "not an object"), ([{"type": "launch"}], "unknown type"),
                          ([{"label": "x", "type": "hotkey", "keys": ["cmd"]}], "pick a key"),
                          ([{"label": "x", "type": "hotkey", "keys": ["ctrl", "teleport"]}], "unknown key"),
                          ([{"label": "x", "type": "app", "app": ""}], "app name"), ([{"label": "x", "type": "shortcut"}], "Shortcut"),
                          ([{"label": "x", "type": "shell", "command": " "}], "command"), ([{}] * 13, "at most 12")):
            clean, err = agent.validate_buttons(bad)
            self.assertIsNone(clean); self.assertIn(frag, err)

    def test_truncates_long_text(self):
        clean, _ = agent.validate_buttons([{"label": "x" * 40, "sub": "y" * 40, "glyph": "abc", "type": "mic"}])
        self.assertEqual((len(clean[0]["label"]), len(clean[0]["sub"]), clean[0]["glyph"]), (16, 20, "ab"))


# ------------------------------------------------------------------- server ----
class FakeSensors:
    stream = mock.Mock(path="/opt/homebrew/bin/macmon")

    def read(self):
        return {"sensors": "macmon", "cpu": {"name": "Apple M4 Pro", "load": 10.0, "temp": 40.0, "clock": 1600, "power": 1.0},
                "gpu": {"name": "GPU", "load": 5.0, "temp": 42.0, "hotspot": None, "vram_used_gb": None, "vram_total_gb": None,
                        "fan_rpm": 1000, "fan_pct": None, "power": 0.2, "clock": 338},
                "ram": {"used_gb": 16.0, "total_gb": 64.0, "load": 25.0}, "sys_power": 19.0, "storage": [], "net": None, "fans": []}


class TestServer(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = Path(tempfile.mkdtemp())
        shutil.copy(ROOT / "agent" / "config.json", cls.tmp / "config.json")
        cls.here_patch = mock.patch.object(agent, "CONFIG_PATH", cls.tmp / "config.json"); cls.here_patch.start()
        cls.mic_patch = mock.patch.object(agent, "mic_state", return_value=None); cls.mic_patch.start()
        agent.DRY_RUN = True
        cls.cfg = agent.load_config()
        cls.state = agent.State()
        threading.Thread(target=agent.poll_loop, args=(cls.state, cls.cfg, FakeSensors()), daemon=True).start()
        cls.events = agent.EventStore(cls.tmp / "events.json")
        agent.feeds_mod.EVENT_STORE = cls.events
        with mock.patch.object(agent.FeedManager.__init__.__globals__["threading"].Thread, "start"):
            cls.feeds = agent.FeedManager(cls.cfg["feeds"], lambda m: None)
        cls.httpd = ThreadingHTTPServer(("127.0.0.1", 0), agent.make_handler(cls.state, cls.cfg, cls.feeds, cls.events))
        cls.port = cls.httpd.server_address[1]
        threading.Thread(target=cls.httpd.serve_forever, daemon=True).start()
        time.sleep(0.8)

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown(); cls.here_patch.stop(); cls.mic_patch.stop(); agent.DRY_RUN = False
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def setUp(self):
        # every test starts from the shipped 12-button layout
        shipped = json.loads((ROOT / "agent" / "config.json").read_text())
        self.req("/api/admin/config", "POST", {"buttons": shipped["buttons"]})
        self.req("/api/admin/feeds", "POST", {"right_side": shipped["right_side"], "feeds": shipped["feeds"]})

    def req(self, path, method="GET", body=None):
        data = json.dumps(body).encode() if body is not None else None
        r = urllib.request.Request(f"http://127.0.0.1:{self.port}{path}", data=data, method=method,
                                   headers={"Content-Type": "application/json"} if data else {})
        try:
            with urllib.request.urlopen(r, timeout=5) as resp:
                return resp.status, resp.headers.get("Content-Type", ""), resp.read()
        except urllib.error.HTTPError as e:
            return e.code, e.headers.get("Content-Type", ""), e.read()

    def test_pages_and_static_protection(self):
        for path in ("/", "/index.html", "/admin", "/admin.html"):
            code, ctype, body = self.req(path)
            self.assertEqual(code, 200, path); self.assertIn("text/html", ctype); self.assertIn(b"<title>", body)
        self.assertEqual(self.req("/nope.html")[0], 404)
        self.assertEqual(self.req("/../agent/config.json")[0], 404)
        self.assertEqual(self.req("/%2e%2e/agent/config.json")[0], 404)

    def test_stats_health_config(self):
        code, _, body = self.req("/api/stats"); d = json.loads(body)
        self.assertEqual(code, 200)
        for k in ("cpu", "gpu", "ram", "ts", "demo", "pc_name", "mic_muted", "cfg_version", "sensors", "caps"):
            self.assertIn(k, d)
        self.assertEqual(d["caps"], {"accessibility": True, "volume": "system", "mic": True})   # dry-run capabilities
        self.assertFalse(d["demo"]); self.assertEqual(d["cpu"]["temp"], 40.0)
        code, _, body = self.req("/api/health"); h = json.loads(body)
        self.assertTrue(h["ok"]); self.assertEqual(h["sensors"], "macmon"); self.assertTrue(h["displays"]); self.assertIn("caps", h)
        code, _, body = self.req("/api/config"); c = json.loads(body)
        self.assertEqual(c["platform"], "mac"); self.assertEqual(len(c["buttons"]), 12); self.assertIn("pc_name", c)

    def test_actions_by_id_and_inline(self):
        code, _, body = self.req("/api/action/space-prev", "POST"); self.assertEqual(code, 200); self.assertIn("dry run", json.loads(body)["message"])
        self.assertEqual(self.req("/api/action/nope", "POST")[0], 404)
        code, _, body = self.req("/api/action", "POST", {"label": "t", "type": "app", "app": "Safari"}); self.assertEqual(code, 200)
        self.assertEqual(self.req("/api/action", "POST", {"label": "t", "type": "hotkey", "keys": ["cmd"]})[0], 400)
        self.assertEqual(self.req("/api/action", "POST", "garbage")[0], 400)
        self.assertEqual(self.req("/api/unknown", "POST", {})[0], 404)

    def test_save_config_roundtrip_and_hot_reload(self):
        v0 = json.loads(self.req("/api/config")[2])["cfg_version"]   # /api/stats lags by one poll interval
        new = [{"label": "Space", "glyph": "◀", "type": "hotkey", "keys": ["ctrl", "left"]}, {"type": "empty"}, {"label": "Safari", "type": "app", "app": "Safari"}]
        code, _, body = self.req("/api/admin/config", "POST", {"buttons": new}); d = json.loads(body)
        self.assertEqual(code, 200); self.assertTrue(d["ok"]); self.assertEqual(d["cfg_version"], v0 + 1)
        on_disk = json.loads((self.tmp / "config.json").read_text())
        self.assertEqual(len(on_disk["buttons"]), 3); self.assertEqual(on_disk["panel_resolution"], [1540, 720])
        self.assertNotIn("_pc_name", on_disk); self.assertNotIn("pc_name", on_disk)
        self.assertEqual(json.loads(self.req("/api/config")[2])["buttons"][2]["app"], "Safari")
        time.sleep(0.6)
        self.assertEqual(json.loads(self.req("/api/stats")[2])["cfg_version"], v0 + 1)
        self.assertEqual(self.req("/api/action/slot-3", "POST")[0], 200)     # new id works immediately
        self.assertEqual(self.req("/api/action/space-next", "POST")[0], 404)  # old id is gone
        self.assertEqual(self.req("/api/admin/config", "POST", {"buttons": [{"label": "x", "type": "hotkey", "keys": ["cmd"]}]})[0], 400)
        self.assertEqual(self.req("/api/admin/config", "POST", {"nope": 1})[0], 400)

    def test_feeds_endpoints(self):
        code, _, body = self.req("/api/config"); c = json.loads(body)
        self.assertEqual(c["right_side"], "three"); self.assertEqual(len(c["feeds"]), 3); self.assertEqual(c["feeds"][0]["source"], "ai")
        self.assertNotIn("token", c["feeds"][1]); self.assertIn("has_token", c["feeds"][1]); self.assertIn("Slack", c["app_presets"])
        code, _, body = self.req("/api/feeds"); d = json.loads(body)
        self.assertEqual(code, 200); self.assertEqual([f["id"] for f in d["feeds"]], ["feed-1", "feed-2", "feed-3"])
        # save: token stored but never echoed; empty token keeps the stored one; channels as text
        code, _, body = self.req("/api/admin/feeds", "POST", {"right_side": "mixed", "feeds": [
            {"title": "Work Slack", "source": "slack", "token": "xoxp-secret", "channels": "dm, platform", "limit": 30},
            {"title": "T", "source": "teams", "tenant": "", "client_id": "abc"}]})
        d = json.loads(body); self.assertEqual(code, 200, body)
        self.assertEqual(d["right_side"], "mixed"); self.assertEqual(d["feeds"][0]["channels"], ["dm", "platform"]); self.assertEqual(d["feeds"][0]["limit"], 20)
        self.assertTrue(d["feeds"][0]["has_token"]); self.assertNotIn("token", d["feeds"][0]); self.assertEqual(d["feeds"][1]["tenant"], "common")
        on_disk = json.loads((self.tmp / "config.json").read_text())
        self.assertEqual(on_disk["feeds"][0]["token"], "xoxp-secret")
        self.req("/api/admin/feeds", "POST", {"right_side": "feeds", "feeds": [{"title": "Work Slack", "source": "slack", "token": "", "channels": ["dm"]}, {"source": "none"}]})
        self.assertEqual(json.loads((self.tmp / "config.json").read_text())["feeds"][0]["token"], "xoxp-secret", "empty token keeps the stored one")
        self.assertEqual(self.req("/api/admin/feeds", "POST", {"right_side": "sideways", "feeds": []})[0], 400)
        self.assertEqual(self.req("/api/admin/feeds", "POST", {"right_side": "feeds", "feeds": [{"source": "pigeon"}]})[0], 400)
        # refresh + login routing
        code, _, body = self.req("/api/feeds/feed-2/refresh", "POST", {}); self.assertEqual(code, 200); self.assertEqual(json.loads(body)["feed"]["status"], "needs_setup")
        self.assertEqual(self.req("/api/admin/feeds", "POST", {"right_side": "feeds3", "feeds": [{"source": "ai"}, {"source": "none"}, {"source": "none"}]})[0], 200)
        self.assertEqual(self.req("/api/admin/feeds", "POST", {"right_side": "three-feeds", "feeds": [{"source": "ai"}, {"source": "slack"}, {"source": "none"}]})[0], 200)
        self.assertEqual(self.req("/api/feeds/nope/refresh", "POST", {})[0], 404)
        self.assertEqual(self.req("/api/feeds/feed-2/login", "POST", {})[0], 400)   # slack feed has no sign-in
        # deep links: only Slack/Teams schemes are opened
        with mock.patch.object(agent.subprocess, "Popen") as popen:
            self.assertEqual(self.req("/api/feeds/open", "POST", {"url": "slack://channel?team=T&id=C"})[0], 200)
            self.assertEqual(popen.call_args[0][0], [agent.OPEN, "slack://channel?team=T&id=C"])
            self.assertEqual(self.req("/api/feeds/open", "POST", {"url": "file:///etc/passwd"})[0], 400)
            self.assertEqual(self.req("/api/feeds/open", "POST", {"url": "https://evil.com"})[0], 400)

    def test_arrange_endpoint_and_health_panel(self):
        h = json.loads(self.req("/api/health")[2]); self.assertIn("panel", h); self.assertIn("main", h["panel"])
        self.assertIn("touch", h); self.assertIn(h["touch"]["state"], ("off", "starting", "mapped", "needs accessibility", "no touchscreen", "waiting for panel", "error"))
        with mock.patch.object(agent.arrange, "arrange", return_value=(True, "main display is 3840x1080, panel parked below")), \
             mock.patch.object(agent.arrange, "sweep", return_value=(True, "moved 2 windows back to the main display")), \
             mock.patch.object(agent, "list_displays", return_value=[{"id": 1, "x": 1150, "y": 1080, "w": 1540, "h": 720, "px_w": 1540, "px_h": 720, "main": False, "builtin": False},
                                                                     {"id": 2, "x": 0, "y": 0, "w": 3840, "h": 1080, "px_w": 3840, "px_h": 1080, "main": True, "builtin": False}]):
            code, _, body = self.req("/api/admin/arrange", "POST", {"position": "left", "keep": True}); d = json.loads(body)
        self.assertEqual(code, 200); self.assertTrue(d["changed"]); self.assertIn("moved 2 windows", d["message"]); self.assertEqual(d["position"], "left")
        self.assertEqual(json.loads((self.tmp / "config.json").read_text())["panel_position"], "left")
        with mock.patch.object(agent, "list_displays", return_value=[{"id": 2, "x": 0, "y": 0, "w": 3840, "h": 1080, "px_w": 3840, "px_h": 1080, "main": True, "builtin": False}]):
            d = json.loads(self.req("/api/admin/arrange", "POST", {})[2]); self.assertFalse(d["changed"]); self.assertEqual(d["message"], "panel not connected")

    def test_park_cursor_on_main_before_actions(self):
        state = agent.State(); state.kiosk = mock.Mock(); state.kiosk._pids.return_value = [10]; state.last_main_cursor = (700, 400)
        displays_ = [{"id": 1, "x": 1150, "y": -720, "w": 1540, "h": 720, "px_w": 1540, "px_h": 720, "main": False, "builtin": False},
                     {"id": 2, "x": 0, "y": 0, "w": 3840, "h": 1080, "px_w": 3840, "px_h": 1080, "main": True, "builtin": False}]
        with mock.patch.object(agent, "DRY_RUN", False), mock.patch.object(agent, "list_displays", return_value=displays_), \
             mock.patch.object(agent.arrange, "cursor_position", return_value=(1900, -360)), mock.patch.object(agent.arrange, "warp_cursor") as warp, \
             mock.patch.object(agent.arrange, "wait_button_up") as wait:
            agent.park_cursor_on_main(state)
            wait.assert_called_once()                                    # after the finger has lifted
            warp.assert_called_once_with(700, 400)                       # back to where it was on the main display
            warp.reset_mock(); state.last_main_cursor = None
            agent.park_cursor_on_main(state)
            warp.assert_called_once_with(1920.0, 540.0)                  # or the middle of it
        with mock.patch.object(agent, "DRY_RUN", False), mock.patch.object(agent, "list_displays", return_value=displays_), \
             mock.patch.object(agent.arrange, "cursor_position", return_value=(500, 500)), mock.patch.object(agent.arrange, "warp_cursor") as warp:
            agent.park_cursor_on_main(state)
            warp.assert_not_called()                                     # already on the main display
        with mock.patch.object(agent, "DRY_RUN", False), mock.patch.object(agent, "park_cursor_on_main") as park, \
             mock.patch.object(agent, "focus_main_display") as focus, mock.patch.object(agent, "send_hotkey") as send, \
             mock.patch.object(agent, "run_later", side_effect=lambda fn, *a: fn(*a)) as later:
            agent.run_action({"type": "hotkey", "keys": ["ctrl", "right"]}, state)
            park.assert_called_once(); send.assert_called_once(); later.assert_called_once(); focus.assert_called_once()   # focus after the key
            later.reset_mock(); focus.reset_mock(); agent.run_action({"type": "hotkey", "keys": ["cmd", "space"]}, state)
            later.assert_not_called(); focus.assert_called_once()                                                           # Spotlight: focus first
            park.reset_mock(); agent.run_action({"type": "empty"}, state); park.assert_not_called()

    def test_focus_main_display_restores_the_previous_app(self):
        state = agent.State(); state.kiosk = mock.Mock(); state.kiosk._pids.return_value = [10, 11]; state.last_front_pid = 20
        with mock.patch.object(agent, "DRY_RUN", False), mock.patch.object(agent.arrange, "front_pid", return_value=10), \
             mock.patch.object(agent.arrange, "activate_pid", return_value=(True, "objc")) as act, mock.patch.object(agent.arrange, "activate_main_app") as fallback:
            agent.focus_main_display(state); act.assert_called_once_with(20); fallback.assert_not_called()
        with mock.patch.object(agent, "DRY_RUN", False), mock.patch.object(agent.arrange, "front_pid", return_value=20), \
             mock.patch.object(agent.arrange, "activate_pid") as act:
            agent.focus_main_display(state); act.assert_not_called()          # the user's app is already active
        state.last_front_pid = None
        with mock.patch.object(agent, "DRY_RUN", False), mock.patch.object(agent.arrange, "front_pid", return_value=10), \
             mock.patch.object(agent, "list_displays", return_value=[{"id": 2, "x": 0, "y": 0, "w": 3840, "h": 1080, "px_w": 3840, "px_h": 1080, "main": True, "builtin": False}]), \
             mock.patch.object(agent.arrange, "activate_main_app", return_value=(True, "activated X")) as fallback:
            agent.focus_main_display(state); fallback.assert_called_once()

    def test_admin_open_opens_the_admin_page(self):
        with mock.patch.object(agent.subprocess, "Popen") as popen, mock.patch.object(agent, "open_url") as ou:
            code, _, body = self.req("/api/admin/open", "POST", {})
        self.assertEqual(code, 200); self.assertTrue(json.loads(body)["ok"])
        self.assertEqual(popen.call_args[0][0][0], agent.OPEN); self.assertTrue(popen.call_args[0][0][1].endswith("/admin"), popen.call_args)
        ou.assert_not_called()                                           # the feed-link allowlist must not apply to our own page

    def test_ai_events_flow(self):
        self.req("/api/events/clear", "POST", {})
        code, _, body = self.req("/api/events/claude-code", "POST", {"payload": {"hook_event_name": "Stop", "session_id": "sess-1", "cwd": "/Users/me/proj"}, "env": {"__CFBundleIdentifier": "com.apple.Terminal"}, "tty": "/dev/ttys003", "cwd": "/Users/me/proj"})
        self.assertEqual(code, 200); eid = json.loads(body)["id"]
        self.req("/api/events", "POST", {"tool": "Gemini", "title": "Done", "text": "ok", "state": "needs_input", "cwd": "/w/x"})
        code, _, body = self.req("/api/events?limit=10"); d = json.loads(body)
        self.assertEqual(code, 200); self.assertEqual(d["unseen"], 2); self.assertEqual([e["tool"] for e in d["events"]], ["Gemini", "Claude Code"])
        # the AI feed shows them, newest first, with focus links
        self.req("/api/admin/feeds", "POST", {"right_side": "feeds3", "feeds": [{"title": "AI", "source": "ai", "limit": 6}, {"source": "none"}, {"source": "none"}]})
        code, _, body = self.req("/api/feeds/feed-1/refresh", "POST", {}); f = json.loads(body)["feed"]
        self.assertEqual(f["status"], "ok"); self.assertEqual(f["items"][1]["link"], "focus:" + eid); self.assertEqual(f["items"][1]["where"], "proj · Terminal"); self.assertFalse(f["items"][1]["seen"])
        self.assertEqual(f["items"][0]["state"], "needs_input")
        # focus (dry run in tests) marks it seen
        code, _, body = self.req(f"/api/events/{eid}/focus", "POST", {}); self.assertEqual(code, 200); self.assertIn("dry run", json.loads(body)["message"])
        self.assertEqual(json.loads(self.req("/api/events")[2])["unseen"], 1)
        self.assertEqual(self.req("/api/events/0123456789/focus", "POST", {})[0], 404)
        self.assertEqual(self.req(f"/api/events/{eid}/dismiss", "POST", {})[0], 200)
        self.assertEqual(len(json.loads(self.req("/api/events")[2])["events"]), 1)
        self.assertEqual(self.req("/api/events", "POST", "junk")[0], 400)


if __name__ == "__main__":
    unittest.main(verbosity=2)
