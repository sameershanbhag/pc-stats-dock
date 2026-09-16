"""Tests for AI-chat events: normalizing hook payloads, the store, focus plans, and the hook installer."""
import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "agent"))
import events  # noqa: E402
import focus  # noqa: E402


class TestFocusSpec(unittest.TestCase):
    def test_terminal_iterm_editor_ghostty_tmux(self):
        s = events.focus_spec_from_env({"__CFBundleIdentifier": "com.apple.Terminal"}, "/Users/me/proj", "/dev/ttys003")
        self.assertEqual((s["kind"], s["tty"], s["app"], s["hint"]), ("terminal_tty", "/dev/ttys003", "Terminal", "proj"))
        s = events.focus_spec_from_env({"TERM_PROGRAM": "iTerm.app", "ITERM_SESSION_ID": "w0t1p0:ABC-123"}, "/x/y", "")
        self.assertEqual((s["kind"], s["session"]), ("iterm", "ABC-123"))
        s = events.focus_spec_from_env({"TERM_PROGRAM": "vscode", "__CFBundleIdentifier": "com.todesktop.230313mzl4w4u92"}, "/x/y", "")
        self.assertEqual((s["kind"], s["app"], s["folder"]), ("editor", "Cursor", "/x/y"))
        s = events.focus_spec_from_env({"TERM_PROGRAM": "vscode"}, "/x/y", "")
        self.assertEqual((s["kind"], s["app"]), ("editor", "VS Code"))
        s = events.focus_spec_from_env({"TERM": "xterm-ghostty", "TMUX_PANE": "%3"}, "/x/y", "/dev/ttys009")
        self.assertEqual((s["kind"], s["app"], s["tmux_pane"]), ("app", "Ghostty", "%3"))
        self.assertEqual(events.focus_spec_from_env({}, "", "")["kind"], "none")
        # an editor's own chat panel: no terminal variables, but the helper path or the parent processes tell
        s = events.focus_spec_from_env({"VSCODE_GIT_ASKPASS_MAIN": "/Applications/Visual Studio Code.app/Contents/Resources/app/extensions/git/dist/askpass-main.js"}, "/p/q", "")
        self.assertEqual((s["kind"], s["app"], s["folder"]), ("editor", "VS Code", "/p/q"))
        s = events.focus_spec_from_env({}, "/p/q", "", ancestors=["node", "Code Helper (Plugin)", "Electron", "launchd"])
        self.assertEqual((s["kind"], s["app"]), ("editor", "VS Code"))
        s = events.focus_spec_from_env({"TERM_PROGRAM": "vscode"}, "/p/q", "", ancestors=["zsh", "Cursor Helper (Plugin)", "Cursor"])
        self.assertEqual((s["kind"], s["app"], s["bundle"]), ("editor", "Cursor", "com.todesktop.230313mzl4w4u92"), "Cursor's terminal says vscode; the process tree wins")
        s = events.focus_spec_from_env({"CLAUDE_CODE_ENTRYPOINT": "local-agent"}, "/p/q", "", ancestors=["claude", "disclaimer", "Claude"])
        self.assertEqual((s["kind"], s["app"], s["bundle"]), ("app", "Claude", "com.anthropic.claudefordesktop"))
        s = events.focus_spec_from_env({"__CFBundleIdentifier": "com.mitchellh.ghostty"}, "/p/q", "", ancestors=["zsh", "ghostty"])
        self.assertEqual(s["app"], "Ghostty", "a real terminal variable beats the process tree")


class TestNormalizers(unittest.TestCase):
    def test_claude_code_stop_with_transcript_preview(self):
        t = Path(tempfile.mkdtemp()) / "t.jsonl"
        t.write_text("\n".join([json.dumps({"type": "user", "message": {"content": "hi"}}),
                                json.dumps({"type": "assistant", "message": {"content": [{"type": "text", "text": "All  47 tests\npass. Deployed."}]}}),
                                json.dumps({"type": "assistant", "message": {"content": [{"type": "tool_use", "name": "Bash"}]}})]))
        ev = events.from_claude_code({"payload": {"hook_event_name": "Stop", "session_id": "s1", "transcript_path": str(t), "cwd": "/Users/me/pc-stats-dock"},
                                      "env": {"__CFBundleIdentifier": "com.mitchellh.ghostty"}, "tty": "/dev/ttys002", "cwd": "/Users/me/pc-stats-dock"})
        self.assertEqual((ev["tool"], ev["state"], ev["title"], ev["project"], ev["session"]), ("Claude Code", "done", "Finished", "pc-stats-dock", "s1"))
        self.assertEqual(ev["text"], "All 47 tests pass. Deployed.")
        self.assertEqual(ev["focus"]["app"], "Ghostty")

    def test_claude_code_notification_needs_input(self):
        ev = events.from_claude_code({"payload": {"hook_event_name": "Notification", "message": "Claude needs your permission to use Bash", "session_id": "s2", "cwd": "/p/q"}, "env": {}, "tty": ""})
        self.assertEqual((ev["state"], ev["title"], ev["project"]), ("needs_input", "Claude needs your permission to use Bash", "q"))

    def test_codex_and_generic(self):
        ev = events.from_codex({"payload": {"type": "agent-turn-complete", "turn-id": "t9", "last-assistant-message": "Done:\n refactored"}, "env": {"TERM_PROGRAM": "Apple_Terminal"}, "tty": "/dev/ttys004", "cwd": "/w/billing"})
        self.assertEqual((ev["tool"], ev["text"], ev["session"], ev["focus"]["kind"]), ("Codex", "Done: refactored", "t9", "terminal_tty"))
        ev = events.from_generic({"tool": "Gemini", "title": "Done", "state": "bogus", "cwd": "/w/x", "focus": {"kind": "editor", "app": "VS Code", "folder": "/w/x"}})
        self.assertEqual((ev["tool"], ev["state"], ev["project"], ev["focus"]["kind"]), ("Gemini", "done", "x", "editor"))


class TestStore(unittest.TestCase):
    def test_add_dedupe_seen_persist(self):
        path = Path(tempfile.mkdtemp()) / "events.json"
        st = events.EventStore(path)
        a = st.add({"tool": "Claude Code", "state": "needs_input", "title": "Needs you", "project": "p", "session": "s1", "focus": {}})
        st.add({"tool": "Codex", "state": "done", "title": "Finished", "project": "q", "session": "", "focus": {}})
        b = st.add({"tool": "Claude Code", "state": "done", "title": "Finished", "project": "p", "session": "s1", "focus": {}})
        self.assertEqual(len(st.list()), 2, "same session replaced")
        self.assertEqual(st.list()[0]["id"], b["id"]); self.assertIsNone(st.get(a["id"]))
        self.assertEqual(st.unseen(), 2)
        st.mark_seen(b["id"]); self.assertEqual(st.unseen(), 1)
        st2 = events.EventStore(path)
        self.assertEqual([e["id"] for e in st2.list()], [e["id"] for e in st.list()], "persisted")
        st2.dismiss(b["id"]); self.assertEqual(len(st2.list()), 1); st2.clear(); self.assertEqual(st2.list(), [])
        self.assertEqual(oct(path.stat().st_mode & 0o777), "0o600")


class TestFocusPlan(unittest.TestCase):
    def test_plans(self):
        p = focus.plan({"kind": "terminal_tty", "tty": "/dev/ttys003"})
        self.assertEqual(p[0][0], "osascript"); self.assertIn('"/dev/ttys003"', p[0][1]); self.assertIn("selected tab", p[0][1])
        p = focus.plan({"kind": "iterm", "session": "ABC"})
        self.assertIn('id of s is "ABC"', p[0][1])
        with mock.patch.object(focus.shutil, "which", return_value=None):
            p = focus.plan({"kind": "editor", "app": "VS Code", "folder": "/w/x"})
        self.assertEqual(p[0], ("cmd", [focus.OPEN, "-a", "Visual Studio Code", "/w/x"]))
        with mock.patch.object(focus.shutil, "which", return_value="/usr/local/bin/code"):
            p = focus.plan({"kind": "editor", "app": "VS Code", "folder": "/w/x"})
        self.assertEqual(p[0], ("cmd", ["/usr/local/bin/code", "--reuse-window", "/w/x"]))
        p = focus.plan({"kind": "app", "app": "Ghostty", "bundle": "com.mitchellh.ghostty", "hint": "proj", "tmux_pane": "%2"})
        self.assertEqual(p[0], ("cmd", ["tmux", "select-window", "-t", "%2"])); self.assertIn('process "Ghostty"', p[2][1]); self.assertEqual(p[3], ("cmd", [focus.OPEN, "-b", "com.mitchellh.ghostty"]))
        self.assertEqual(focus.plan({"kind": "none"}), [])

    def test_focus_runs_steps_and_reports(self):
        ok, msg = focus.focus({"kind": "none"}); self.assertFalse(ok)
        ok, msg = focus.focus({"kind": "terminal_tty", "tty": "/dev/ttys003", "app": "Terminal"}, dry_run=True); self.assertTrue(ok); self.assertTrue(msg.startswith("dry run"))
        with mock.patch.object(focus.subprocess, "run", return_value=mock.Mock(returncode=0, stdout="ok", stderr="")):
            self.assertEqual(focus.focus({"kind": "terminal_tty", "tty": "/dev/ttys003", "app": "Terminal"}), (True, "jumped to Terminal"))
        with mock.patch.object(focus.subprocess, "run", return_value=mock.Mock(returncode=1, stdout="", stderr="execution error: Not authorized to send Apple events to Terminal. (-1743)")):
            ok, msg = focus.focus({"kind": "terminal_tty", "tty": "/dev/ttys003", "app": "Terminal"})
        self.assertFalse(ok); self.assertIn("Automation", msg)


class TestToolOfHook(unittest.TestCase):
    def test_copilot_versus_claude_code(self):
        stop = {"hook_event_name": "Stop", "session_id": "s", "cwd": "/p/q"}
        ev = events.from_claude_code({"payload": stop, "env": {}, "ancestors": ["node", "Code Helper (Plugin)", "Electron"]})
        self.assertEqual((ev["tool"], ev["focus"]["app"]), ("Copilot", "VS Code"), "VS Code's own agent hooks without claude in the tree = Copilot Chat")
        ev = events.from_claude_code({"payload": stop, "env": {"CLAUDE_CODE_ENTRYPOINT": "cli"}, "ancestors": ["claude", "node", "Code Helper (Plugin)"]})
        self.assertEqual(ev["tool"], "Claude Code", "Claude Code inside VS Code stays Claude Code")
        ev = events.from_claude_code({"payload": dict(stop, transcript_path="/Users/x/.claude/projects/p/s.jsonl"), "env": {}, "ancestors": ["zsh", "ghostty"]})
        self.assertEqual(ev["tool"], "Claude Code")
        ev = events.from_claude_code({"payload": stop, "env": {"TERM_PROGRAM": "ghostty"}, "ancestors": ["copilot", "zsh", "ghostty"]})
        self.assertEqual((ev["tool"], ev["focus"]["app"]), ("Copilot", "Ghostty"), "the Copilot CLI in a terminal")
        ev = events.from_claude_code({"payload": stop, "env": {"TERM_PROGRAM": "ghostty"}, "ancestors": ["zsh", "ghostty"]})
        self.assertEqual(ev["tool"], "Claude Code", "nothing says Copilot: Claude Code")

    def test_store_keeps_one_entry_per_session_and_tool(self):
        store = events.EventStore(Path(tempfile.mkdtemp()) / "e.json")
        store.add({"tool": "Copilot", "session": "s1", "title": "Finished", "state": "done"})
        store.add({"tool": "Copilot", "session": "s1", "title": "Finished", "state": "done"})   # the second hook file firing for the same chat
        self.assertEqual(len(store.list()), 1)


class TestInstaller(unittest.TestCase):
    def run_installer(self, home, *args):
        env = dict(os.environ, PCSTATS_HOME=str(home))
        r = subprocess.run([sys.executable, str(ROOT / "agent" / "hooks" / "install_hooks.py"), *args], capture_output=True, text=True, env=env, timeout=20)
        self.assertEqual(r.returncode, 0, r.stderr)
        return json.loads(r.stdout)

    def test_claude_codex_cursor_merge_and_uninstall(self):
        home = Path(tempfile.mkdtemp())
        (home / ".claude").mkdir(); (home / ".claude" / "settings.json").write_text(json.dumps({"permissions": {"allow": ["Bash(ls)"]}, "hooks": {"Stop": [{"hooks": [{"type": "command", "command": "say done"}]}]}}))
        (home / ".codex").mkdir(); (home / ".codex" / "config.toml").write_text('model = "o3"\n')
        (home / ".cursor").mkdir()
        out = self.run_installer(home)
        self.assertEqual(out["claude_code"], "installed"); self.assertEqual(out["codex"], "installed"); self.assertTrue(out["cursor"].startswith("installed"))
        self.assertTrue(out["copilot"].startswith("skipped"), "no VS Code or Copilot CLI in this fake home")
        s = json.loads((home / ".claude" / "settings.json").read_text())
        self.assertEqual(s["permissions"], {"allow": ["Bash(ls)"]}, "other settings untouched")
        self.assertEqual(len(s["hooks"]["Stop"]), 2, "existing Stop hook kept"); self.assertEqual(len(s["hooks"]["Notification"]), 1)
        cmd = s["hooks"]["Stop"][1]["hooks"][0]["command"]
        self.assertEqual(cmd, str(home / "Library" / "Application Support" / "pc-stats-dock" / "hooks" / "claude_code_hook.py"), "registered at the stable folder")
        self.assertTrue(os.access(cmd, os.X_OK), "stable copy is executable")
        self.assertIn("codex_notify.py", (home / ".codex" / "config.toml").read_text()); self.assertIn('model = "o3"', (home / ".codex" / "config.toml").read_text())
        self.assertTrue(json.loads((home / ".cursor" / "hooks.json").read_text())["hooks"]["stop"][0]["command"].endswith("cursor_hook.py"))
        out = self.run_installer(home)
        self.assertEqual(out["claude_code"], "already installed"); self.assertEqual(out["codex"], "already installed")
        out = self.run_installer(home, "--uninstall")
        self.assertEqual(out["claude_code"], "removed"); self.assertEqual(out["codex"], "removed"); self.assertEqual(out["cursor"], "removed")
        s = json.loads((home / ".claude" / "settings.json").read_text())
        self.assertEqual(len(s["hooks"]["Stop"]), 1); self.assertNotIn("Notification", s["hooks"])
        self.assertNotIn("codex_notify", (home / ".codex" / "config.toml").read_text())

    def test_repair_repoints_a_stale_registration_and_adds_nothing(self):
        home = Path(tempfile.mkdtemp()); (home / ".claude").mkdir()
        stale = "/Users/x/Library/Application Support/pc-stats-dock/app/agent/hooks/claude_code_hook.py"     # the app folder of an old install
        (home / ".claude" / "settings.json").write_text(json.dumps({"hooks": {"Stop": [{"hooks": [{"type": "command", "command": stale, "timeout": 5}]}]}}))
        out = self.run_installer(home, "--repair")
        self.assertEqual(out["claude_code"], "repaired")
        s = json.loads((home / ".claude" / "settings.json").read_text())
        stable = str(home / "Library" / "Application Support" / "pc-stats-dock" / "hooks" / "claude_code_hook.py")
        self.assertEqual([h["command"] for e in s["hooks"]["Stop"] for h in e["hooks"]], [stable])
        self.assertEqual([h["command"] for e in s["hooks"]["Notification"] for h in e["hooks"]], [stable], "the missing event is registered too")
        self.assertEqual(self.run_installer(home, "--repair")["claude_code"], "already installed")
        fresh = Path(tempfile.mkdtemp()); (fresh / ".claude").mkdir(); (fresh / ".claude" / "settings.json").write_text("{}")
        out = self.run_installer(fresh, "--repair")
        self.assertEqual(out["claude_code"], "not registered"); self.assertEqual(json.loads((fresh / ".claude" / "settings.json").read_text()), {})

    def test_policy_note_when_a_company_policy_disables_hooks(self):
        home = Path(tempfile.mkdtemp()); (home / ".claude").mkdir(); (home / ".claude" / "settings.json").write_text("{}")
        managed = home / "managed-settings.json"; managed.write_text(json.dumps({"disableAllHooks": True}))
        env = dict(os.environ, PCSTATS_HOME=str(home), PCSTATS_MANAGED=str(managed))
        r = subprocess.run([sys.executable, str(ROOT / "agent" / "hooks" / "install_hooks.py")], capture_output=True, text=True, env=env, timeout=20)
        out = json.loads(r.stdout)
        self.assertTrue(out["claude_code"].startswith("installed · but company policy"), out["claude_code"]); self.assertIn("disableAllHooks", out["claude_code"])
        managed.write_text("{}")
        r = subprocess.run([sys.executable, str(ROOT / "agent" / "hooks" / "install_hooks.py")], capture_output=True, text=True, env=env, timeout=20)
        self.assertEqual(json.loads(r.stdout)["claude_code"], "already installed")

    def test_copilot_hook_file_for_vscode_and_the_cli(self):
        home = Path(tempfile.mkdtemp()); (home / ".vscode").mkdir()                 # VS Code present, no Copilot CLI, no Claude Code
        out = self.run_installer(home)
        self.assertEqual(out["copilot"], "installed"); self.assertTrue(out["claude_code"].startswith("skipped"))
        f = home / ".copilot" / "hooks" / "pc-stats-panel.json"
        data = json.loads(f.read_text())
        self.assertEqual(data["version"], 1); self.assertEqual(data["hooks"]["Stop"][0]["type"], "command")
        self.assertEqual(data["hooks"]["Stop"][0]["command"], str(home / "Library" / "Application Support" / "pc-stats-dock" / "hooks" / "claude_code_hook.py"))
        self.assertEqual(self.run_installer(home)["copilot"], "already installed")
        stale = json.loads(f.read_text()); stale["hooks"]["Stop"][0]["command"] = "/old/pc-stats-dock/app/agent/hooks/claude_code_hook.py"; f.write_text(json.dumps(stale))
        self.assertEqual(self.run_installer(home, "--repair")["copilot"], "repaired")
        self.assertEqual(self.run_installer(home, "--uninstall")["copilot"], "removed"); self.assertFalse(f.exists())
        bare = Path(tempfile.mkdtemp())
        self.assertTrue(self.run_installer(bare)["copilot"].startswith("skipped"))

    def test_missing_tools_are_skipped(self):
        home = Path(tempfile.mkdtemp())
        out = self.run_installer(home)
        self.assertTrue(out["claude_code"].startswith("skipped")); self.assertTrue(out["codex"].startswith("skipped")); self.assertTrue(out["cursor"].startswith("skipped"))


class TestHookScript(unittest.TestCase):
    def test_claude_hook_posts_payload_env_and_tty(self):
        posted = {}
        class FakeServer:
            pass
        import http.server, threading
        class H(http.server.BaseHTTPRequestHandler):
            def do_POST(self):
                n = int(self.headers.get("Content-Length") or 0); posted["path"] = self.path; posted["body"] = json.loads(self.rfile.read(n))
                self.send_response(200); self.send_header("Content-Length", "2"); self.end_headers(); self.wfile.write(b"{}")
            def log_message(self, *a): pass
        srv = http.server.HTTPServer(("127.0.0.1", 0), H); threading.Thread(target=srv.serve_forever, daemon=True).start()
        env = dict(os.environ, PCSTATS_AGENT=f"http://127.0.0.1:{srv.server_address[1]}", TERM_PROGRAM="Apple_Terminal")
        r = subprocess.run([sys.executable, str(ROOT / "agent" / "hooks" / "claude_code_hook.py")], input=json.dumps({"hook_event_name": "Stop", "session_id": "abc", "cwd": "/tmp/proj"}), capture_output=True, text=True, env=env, timeout=10)
        srv.shutdown()
        self.assertEqual(r.returncode, 0); self.assertEqual(r.stdout, "", "hooks must not print (Claude Code reads stdout)")
        self.assertEqual(posted["path"], "/api/events/claude-code")
        self.assertIsInstance(posted["body"]["ancestors"], list); self.assertTrue(posted["body"]["ancestors"], "parent processes reported")
        self.assertEqual(posted["body"]["payload"]["session_id"], "abc"); self.assertEqual(posted["body"]["cwd"], "/tmp/proj"); self.assertEqual(posted["body"]["env"]["TERM_PROGRAM"], "Apple_Terminal")

    def test_hook_survives_agent_down(self):
        env = dict(os.environ, PCSTATS_AGENT="http://127.0.0.1:1")
        r = subprocess.run([sys.executable, str(ROOT / "agent" / "hooks" / "claude_code_hook.py")], input="{}", capture_output=True, text=True, env=env, timeout=10)
        self.assertEqual((r.returncode, r.stdout), (0, ""))


if __name__ == "__main__":
    unittest.main(verbosity=2)
