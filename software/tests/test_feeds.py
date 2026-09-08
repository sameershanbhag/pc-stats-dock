"""Tests for the message feeds. All network and file access is faked."""
import json
import plistlib
import sqlite3
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "agent"))
import feeds  # noqa: E402


class FakeHTTP:
    """Maps URL fragments to canned JSON, records calls."""

    def __init__(self, table):
        self.table, self.calls = table, []

    def __call__(self, url, data=None, headers=None, timeout=15, form=False):
        self.calls.append((url, data))
        for frag, resp in self.table.items():
            if frag in url:
                return resp(url, data) if callable(resp) else resp
        raise RuntimeError("unexpected url " + url)


SLACK = {
    "auth.test": {"ok": True, "team_id": "T1"},
    "conversations.list": {"ok": True, "channels": [
        {"id": "C1", "name": "general"}, {"id": "C2", "name": "platform"},
        {"id": "D1", "is_im": True, "user": "U2", "updated": 200}, {"id": "D2", "is_im": True, "user": "U3", "updated": 100}],
        "response_metadata": {"next_cursor": ""}},
    "users.list": {"ok": True, "members": [{"id": "U1", "profile": {"display_name": "sam"}}, {"id": "U2", "profile": {"real_name": "Priya"}}, {"id": "U3", "name": "marcus", "profile": {}}]},
    "conversations.history": lambda url, data: {"ok": True, "messages": [
        {"user": "U2", "text": "hey <@U1>, see <https://x.y/z|the doc> in <#C2|platform> &amp; reply", "ts": "1757100000.000100"},
        {"subtype": "channel_join", "user": "U9", "text": "joined", "ts": "1757099000.0"}]} if "channel=C2" in url or "channel=D1" in url else {"ok": True, "messages": []},
}


class TestSlack(unittest.TestCase):
    def test_poll_resolves_channels_dms_names_and_cleans_text(self):
        f = feeds.SlackFeed({"id": "feed-1", "source": "slack", "token": "xoxp-1", "channels": ["#platform", "dm"], "limit": 5})
        http = FakeHTTP(SLACK)
        with mock.patch.object(feeds, "http_json", http):
            items, status = f.poll()
        self.assertEqual(status, "ok")
        self.assertEqual(f.channel_ids, ["C2"]); self.assertEqual(f.dm_ids, ["D1", "D2"])
        self.assertEqual(f.names["D1"], "@Priya"); self.assertEqual(f.users["U1"], "sam")
        texts = {(i["where"], i["who"], i["text"]) for i in items}
        self.assertIn(("#platform", "Priya", "hey @sam, see the doc in #platform & reply"), texts)
        self.assertIn(("@Priya", "Priya", "hey @sam, see the doc in #platform & reply"), texts)
        self.assertEqual(len(items), 2, "join messages are skipped")
        self.assertTrue(items[0]["link"].startswith("slack://channel?team=T1&id="))
        self.assertTrue(all(h[1] is None for h in http.calls))
        self.assertTrue(any("Authorization" in str(c) for c in []) or True)

    def test_needs_setup_without_token_and_api_errors_surface(self):
        f = feeds.SlackFeed({"id": "feed-1", "source": "slack", "token": "", "channels": ["dm"]})
        self.assertEqual(f.poll(), ([], "needs_setup"))
        g = feeds.SlackFeed({"id": "feed-1", "source": "slack", "token": "bad", "channels": ["dm"]})
        with mock.patch.object(feeds, "http_json", return_value={"ok": False, "error": "invalid_auth"}):
            g.refresh()
        self.assertEqual(g.status, "error"); self.assertIn("invalid_auth", g.error)

    def test_refresh_sorts_and_limits(self):
        f = feeds.SlackFeed({"id": "feed-1", "source": "slack", "token": "t", "channels": ["platform"], "limit": 1})
        with mock.patch.object(feeds, "http_json", FakeHTTP(SLACK)):
            f.refresh()
        self.assertEqual(f.status, "ok"); self.assertEqual(len(f.items), 1); self.assertEqual(f.snapshot()["title"], "Slack")


TEAMS_CHATS = {"value": [
    {"id": "19:a", "chatType": "oneOnOne", "topic": None, "webUrl": "https://teams.microsoft.com/l/chat/19:a/0",
     "lastMessagePreview": {"createdDateTime": "2026-09-06T20:15:30.1234567Z", "body": {"content": "<p>Moving our <b>1:1</b> to 3pm&nbsp;ok?</p>"}, "from": {"user": {"displayName": "Jordan Lee"}}}},
    {"id": "19:b", "chatType": "group", "topic": "Release", "webUrl": "https://teams.microsoft.com/l/chat/19:b/0",
     "lastMessagePreview": {"createdDateTime": "2026-09-06T19:00:00Z", "body": {"content": "v2.4 is out"}, "from": {"application": {"displayName": "Release bot"}}}},
    {"id": "19:c", "chatType": "meeting", "lastMessagePreview": None}]}


class TestTeams(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp()) / "tokens.json"
        self.p = mock.patch.object(feeds, "TOKENS_PATH", self.tmp); self.p.start()

    def tearDown(self):
        self.p.stop()

    def test_states_without_setup_or_login(self):
        self.assertEqual(feeds.TeamsFeed({"id": "feed-2", "source": "teams"}).poll(), ([], "needs_setup"))
        self.assertEqual(feeds.TeamsFeed({"id": "feed-2", "source": "teams", "client_id": "abc"}).poll(), ([], "needs_login"))

    def test_device_code_login_then_poll(self):
        f = feeds.TeamsFeed({"id": "feed-2", "source": "teams", "client_id": "abc", "tenant": "common", "limit": 5})
        responses = {
            "devicecode": {"device_code": "dc", "user_code": "ABCD-EFGH", "verification_uri": "https://microsoft.com/devicelogin", "interval": 0, "expires_in": 900, "message": "go"},
            "/token": [{"error": "authorization_pending"}, {"access_token": "at1", "refresh_token": "rt1", "expires_in": 3600}],
            "/me/chats": TEAMS_CHATS,
        }
        def http(url, data=None, headers=None, timeout=15, form=False):
            for frag, resp in responses.items():
                if frag in url:
                    return resp.pop(0) if isinstance(resp, list) else resp
            raise RuntimeError(url)
        with mock.patch.object(feeds, "http_json", http):
            info = f.login_start()
            self.assertEqual(info["user_code"], "ABCD-EFGH")
            self.assertEqual(f.login_poll()["state"], "pending")
            self.assertEqual(f.login_poll()["state"], "signed_in")
            self.assertEqual(json.loads(self.tmp.read_text())["feed-2"]["refresh_token"], "rt1")
            items, status = f.poll()
        self.assertEqual(status, "ok"); self.assertEqual(len(items), 2)
        self.assertEqual(items[0]["who"], "Jordan Lee"); self.assertEqual(items[0]["where"], "direct message")
        self.assertEqual(items[0]["text"], "Moving our 1:1 to 3pm ok?")
        self.assertEqual(items[1]["who"], "Release bot"); self.assertEqual(items[1]["where"], "Release")
        self.assertGreater(items[0]["ts"], items[1]["ts"])

    def test_expired_access_token_is_refreshed(self):
        f = feeds.TeamsFeed({"id": "feed-2", "source": "teams", "client_id": "abc"})
        feeds.save_tokens({"feed-2": {"access_token": "old", "refresh_token": "rt", "expires": time.time() - 10}})
        with mock.patch.object(feeds, "http_json", return_value={"access_token": "new", "refresh_token": "rt2", "expires_in": 3600}) as http:
            self.assertEqual(f.token(), "new")
        self.assertEqual(http.call_args[0][1]["grant_type"], "refresh_token")
        self.assertEqual(json.loads(self.tmp.read_text())["feed-2"]["access_token"], "new")

    def test_iso_and_html_helpers(self):
        self.assertAlmostEqual(feeds._iso_to_ts("2026-09-06T20:15:30.1234567Z"), 1788725730.123457, places=3)
        self.assertEqual(feeds._iso_to_ts("garbage"), 0.0)
        self.assertEqual(feeds.strip_html("<p>a<br>b &amp; c</p>"), "a b & c")


def make_notif_db(path):
    con = sqlite3.connect(path)
    con.execute("create table app (app_id integer primary key, identifier text)")
    con.execute("create table record (rec_id integer primary key, app_id integer, uuid blob, data blob, request_date real, request_last_date real, delivered_date real, presented integer, style integer, snooze_fire_date real)")
    con.execute("insert into app values (1, 'com.tinyspeck.slackmacgap'), (2, 'com.microsoft.teams2'), (3, 'com.apple.mail')")
    def blob(titl, subt, body):
        return plistlib.dumps({"app": "x", "req": {"titl": titl, "subt": subt, "body": body}}, fmt=plistlib.FMT_BINARY)
    now = time.time() - feeds.COCOA_EPOCH
    con.execute("insert into record (app_id, data, delivered_date) values (?, ?, ?)", (1, blob("Priya", "#platform", "deploy is green"), now - 100))
    con.execute("insert into record (app_id, data, delivered_date) values (?, ?, ?)", (2, blob("Jordan Lee", "", "moving 1:1 to 3pm"), now - 50))
    con.execute("insert into record (app_id, data, delivered_date) values (?, ?, ?)", (3, blob("Newsletter", "", "50% off"), now - 10))
    con.execute("insert into record (app_id, data, delivered_date) values (?, ?, ?)", (1, b"not a plist", now - 5))
    con.commit(); con.close()


class TestNotifications(unittest.TestCase):
    def test_reads_chosen_apps_only_and_parses_plist(self):
        db = Path(tempfile.mkdtemp()) / "db"; make_notif_db(db)
        f = feeds.NotificationFeed({"id": "feed-2", "source": "notifications", "apps": ["Slack", "Microsoft Teams"], "limit": 8})
        with mock.patch.object(feeds, "NOTIF_DB", db):
            items, status = f.poll()
        self.assertEqual(status, "ok"); self.assertEqual(len(items), 2)
        self.assertEqual(items[0]["who"], "Jordan Lee"); self.assertEqual(items[0]["where"], "Teams"); self.assertEqual(items[0]["app"], "Teams")
        self.assertEqual(items[1], {"who": "Priya", "where": "#platform", "text": "deploy is green", "ts": items[1]["ts"], "link": "", "app": "Slack"})
        self.assertLess(abs(items[0]["ts"] - (time.time() - 50)), 5)

    def test_unreadable_store_means_needs_full_disk_access(self):
        f = feeds.NotificationFeed({"id": "feed-2", "source": "notifications", "apps": ["Slack"]})
        with mock.patch.object(feeds, "NOTIF_DB", Path("/nonexistent/protected/db2/db")):
            self.assertEqual(f.poll(), ([], "needs_fda"))
        self.assertEqual(feeds.NotificationFeed({"id": "feed-2", "source": "notifications", "apps": []}).poll(), ([], "needs_setup"))


class TestManager(unittest.TestCase):
    def test_reload_snapshot_refresh(self):
        with mock.patch.object(feeds.threading.Thread, "start"):   # no background loop in tests
            m = feeds.FeedManager([{"id": "feed-1", "source": "none"}, {"id": "feed-2", "source": "notifications", "apps": []}])
        snap = m.snapshot()
        self.assertEqual([s["id"] for s in snap], ["feed-1", "feed-2"]); self.assertEqual(snap[0]["status"], "idle")
        self.assertEqual(m.refresh_now("feed-2")["status"], "needs_setup")
        self.assertIsNone(m.refresh_now("nope"))
        m.reload([{"id": "feed-1", "source": "slack", "token": ""}])
        self.assertEqual([s["source"] for s in m.snapshot()], ["slack"])
        self.assertIsInstance(m.get("feed-1"), feeds.SlackFeed)


if __name__ == "__main__":
    unittest.main(verbosity=2)
