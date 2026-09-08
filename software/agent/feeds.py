"""Recent-message feeds for the right side of the panel.

Three sources, all optional:
  slack          Slack Web API with a token you create for your workspace (channels + DMs).
  teams          Microsoft Teams through Microsoft Graph, signed in with a device code.
  notifications  What macOS Notification Center showed for chosen apps (Slack, Teams, Messages,
                 Mail…). No API setup, but the app needs Full Disk Access to read the store.
Nothing here needs Python packages.
"""
import html
import json
import os
import plistlib
import re
import sqlite3
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

SUPPORT = Path.home() / "Library" / "Application Support" / "pc-stats-dock"
TOKENS_PATH = SUPPORT / "tokens.json"
NOTIF_DB = Path.home() / "Library" / "Group Containers" / "group.com.apple.usernoted" / "db2" / "db"
COCOA_EPOCH = 978307200  # 2001-01-01 in Unix seconds

APP_PRESETS = {  # name -> bundle identifiers seen in the wild
    "Slack": ["com.tinyspeck.slackmacgap"],
    "Microsoft Teams": ["com.microsoft.teams2", "com.microsoft.teams"],
    "Messages": ["com.apple.MobileSMS", "com.apple.iChat"],
    "Mail": ["com.apple.mail"],
    "Discord": ["com.hnc.Discord"],
    "WhatsApp": ["net.whatsapp.WhatsApp"],
    "Telegram": ["ru.keepcoder.Telegram"],
}
SOURCES = ("none", "ai", "slack", "teams", "notifications")
EVENT_STORE = None   # set by the agent; shared with the AI feed


def http_json(url, data=None, headers=None, timeout=15, form=False):
    """GET/POST returning parsed JSON. Raises RuntimeError with a readable message."""
    body = None
    hdrs = {"User-Agent": "pc-stats-panel/1.0"}
    hdrs.update(headers or {})
    if data is not None:
        if form:
            body = urllib.parse.urlencode(data).encode()
            hdrs["Content-Type"] = "application/x-www-form-urlencoded"
        else:
            body = json.dumps(data).encode()
            hdrs["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=body, headers=hdrs, method="POST" if body is not None else "GET")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read().decode("utf-8") or "{}")
    except urllib.error.HTTPError as e:
        try:
            payload = json.loads(e.read().decode("utf-8"))
        except Exception:
            payload = {}
        if isinstance(payload, dict) and payload.get("error"):
            err = payload["error"]
            desc = err.get("message") if isinstance(err, dict) else payload.get("error_description") or err
            return {"_http_error": e.code, "error": desc, "raw": payload}
        raise RuntimeError(f"HTTP {e.code} from {urllib.parse.urlparse(url).netloc}")
    except urllib.error.URLError as e:
        raise RuntimeError(f"network: {e.reason}")


def strip_html(s):
    s = re.sub(r"<br\s*/?>", " ", s or "", flags=re.I)
    s = re.sub(r"<[^>]+>", "", s)
    return re.sub(r"\s+", " ", html.unescape(s)).strip()


def load_tokens():
    try:
        return json.loads(TOKENS_PATH.read_text())
    except Exception:
        return {}


def save_tokens(tokens):
    TOKENS_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = TOKENS_PATH.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(tokens, indent=2))
    os.chmod(tmp, 0o600)
    os.replace(tmp, TOKENS_PATH)


# ------------------------------------------------------------------ base ----
class Feed:
    interval = 30

    def __init__(self, cfg):
        self.cfg = cfg
        self.id = cfg["id"]
        self.title = cfg.get("title") or self.default_title
        self.limit = max(1, min(int(cfg.get("limit") or 8), 20))
        self.items = []
        self.status = "idle"      # idle | ok | needs_setup | needs_login | needs_fda | error
        self.error = ""
        self.updated = 0.0
        self.lock = threading.Lock()

    default_title = "Messages"

    def refresh(self):
        try:
            items, status = self.poll()
            with self.lock:
                self.items = sorted(items, key=lambda i: i["ts"], reverse=True)[: self.limit]
                self.status = status
                self.error = ""
                self.updated = time.time()
        except Exception as exc:
            with self.lock:
                self.status = "error"
                self.error = f"{type(exc).__name__}: {exc}"[:200]
                self.updated = time.time()

    def poll(self):
        return [], "needs_setup"

    def snapshot(self):
        with self.lock:
            return {"id": self.id, "title": self.title, "source": self.cfg.get("source", "none"), "status": self.status,
                    "error": self.error, "updated": self.updated, "items": list(self.items)}


class NoFeed(Feed):
    default_title = "Not configured"

    def poll(self):
        return [], "needs_setup"


# ----------------------------------------------------------------- slack ----
class SlackFeed(Feed):
    interval = 30
    default_title = "Slack"
    API = "https://slack.com/api/"

    def __init__(self, cfg):
        super().__init__(cfg)
        self.team = None
        self.names = {}         # channel id -> display name
        self.users = {}         # user id -> display name
        self.channel_ids = []   # resolved channel ids to read
        self.dm_ids = []
        self.resolved_at = 0

    def call(self, method, **params):
        token = self.cfg.get("token", "")
        url = self.API + method + ("?" + urllib.parse.urlencode(params) if params else "")
        r = http_json(url, headers={"Authorization": "Bearer " + token})
        if not r.get("ok"):
            raise RuntimeError("Slack: " + str(r.get("error", "request failed")))
        return r

    def resolve(self):
        auth = self.call("auth.test")
        self.team = auth.get("team_id")
        wanted = [c.strip().lstrip("#").lower() for c in (self.cfg.get("channels") or []) if c.strip()]
        want_dms = "dm" in wanted or "dms" in wanted
        wanted = [c for c in wanted if c not in ("dm", "dms")]
        ids, dms, cursor = [], [], ""
        for _ in range(10):
            r = self.call("conversations.list", types="public_channel,private_channel,im,mpim", exclude_archived="true",
                          limit="500", cursor=cursor)
            for c in r.get("channels", []):
                if c.get("is_im"):
                    self.names[c["id"]] = "@" + c.get("user", "")
                    dms.append((c.get("updated") or c.get("created") or 0, c["id"]))
                elif c.get("is_mpim"):
                    self.names[c["id"]] = c.get("name", "group")
                else:
                    self.names[c["id"]] = "#" + c.get("name", "")
                    if c.get("name", "").lower() in wanted:
                        ids.append(c["id"])
            cursor = (r.get("response_metadata") or {}).get("next_cursor") or ""
            if not cursor:
                break
        self.channel_ids = ids
        self.dm_ids = [cid for _, cid in sorted(dms, reverse=True)[:8]] if want_dms else []
        self.resolved_at = time.time()
        if not self.users:
            try:
                r = self.call("users.list", limit="500")
                for u in r.get("members", []):
                    p = u.get("profile") or {}
                    self.users[u["id"]] = p.get("display_name") or p.get("real_name") or u.get("real_name") or u.get("name") or u["id"]
            except RuntimeError:
                pass
        for cid, name in list(self.names.items()):
            if name.startswith("@") and name[1:] in self.users:
                self.names[cid] = "@" + self.users[name[1:]]

    def clean(self, text):
        text = re.sub(r"<@([A-Z0-9]+)(?:\|[^>]*)?>", lambda m: "@" + self.users.get(m.group(1), m.group(1)), text or "")
        text = re.sub(r"<#([A-Z0-9]+)\|([^>]*)>", r"#\2", text)
        text = re.sub(r"<(https?://[^|>]+)\|([^>]*)>", r"\2", text)
        text = re.sub(r"<(https?://[^>]+)>", r"\1", text)
        return html.unescape(text).strip()

    def poll(self):
        if not self.cfg.get("token"):
            return [], "needs_setup"
        if time.time() - self.resolved_at > 600:
            self.resolve()
        items = []
        for cid in self.channel_ids + self.dm_ids:
            r = self.call("conversations.history", channel=cid, limit=str(min(self.limit, 10)))
            for m in r.get("messages", []):
                if m.get("subtype") in ("channel_join", "channel_leave", "bot_add"):
                    continue
                ts = float(m.get("ts", 0))
                items.append({
                    "who": self.users.get(m.get("user"), m.get("username") or m.get("user") or "someone"),
                    "where": self.names.get(cid, cid), "text": self.clean(m.get("text", ""))[:300], "ts": ts,
                    "link": f"slack://channel?team={self.team}&id={cid}&message={m.get('ts', '')}",
                })
        return items, "ok"


# ----------------------------------------------------------------- teams ----
class TeamsFeed(Feed):
    interval = 60
    default_title = "Teams"
    SCOPE = "Chat.Read User.Read offline_access"

    def __init__(self, cfg):
        super().__init__(cfg)
        self.device = None      # pending device-code login

    @property
    def tenant(self):
        return (self.cfg.get("tenant") or "common").strip()

    @property
    def client_id(self):
        return (self.cfg.get("client_id") or "").strip()

    def login_start(self):
        if not self.client_id:
            raise RuntimeError("enter the app's client id first")
        r = http_json(f"https://login.microsoftonline.com/{self.tenant}/oauth2/v2.0/devicecode",
                      {"client_id": self.client_id, "scope": self.SCOPE}, form=True)
        if "device_code" not in r:
            raise RuntimeError("Microsoft: " + str(r.get("error", "could not start sign-in")))
        self.device = {"device_code": r["device_code"], "user_code": r["user_code"], "url": r.get("verification_uri"),
                       "interval": int(r.get("interval", 5)), "expires": time.time() + int(r.get("expires_in", 900)), "next": 0}
        return {"user_code": r["user_code"], "url": r.get("verification_uri"), "message": r.get("message", "")}

    def login_poll(self):
        d = self.device
        if not d:
            return {"state": "signed_in" if self.token() else "idle"}
        if time.time() > d["expires"]:
            self.device = None
            return {"state": "expired"}
        if time.time() < d["next"]:
            return {"state": "pending", "user_code": d["user_code"], "url": d["url"]}
        d["next"] = time.time() + d["interval"]
        r = http_json(f"https://login.microsoftonline.com/{self.tenant}/oauth2/v2.0/token",
                      {"grant_type": "urn:ietf:params:oauth:grant-type:device_code", "client_id": self.client_id,
                       "device_code": d["device_code"]}, form=True)
        if "access_token" in r:
            self.store(r)
            self.device = None
            return {"state": "signed_in"}
        err = r.get("error") if isinstance(r.get("error"), str) else str(r.get("error"))
        if err in ("authorization_pending", "slow_down"):
            return {"state": "pending", "user_code": d["user_code"], "url": d["url"]}
        self.device = None
        return {"state": "failed", "error": r.get("error_description") or err or "sign-in failed"}

    def store(self, r):
        tokens = load_tokens()
        tokens[self.id] = {"access_token": r["access_token"], "refresh_token": r.get("refresh_token"),
                           "expires": time.time() + int(r.get("expires_in", 3600)) - 60}
        save_tokens(tokens)

    def token(self):
        t = load_tokens().get(self.id)
        if not t:
            return None
        if time.time() < t.get("expires", 0):
            return t["access_token"]
        if not t.get("refresh_token"):
            return None
        r = http_json(f"https://login.microsoftonline.com/{self.tenant}/oauth2/v2.0/token",
                      {"grant_type": "refresh_token", "client_id": self.client_id, "refresh_token": t["refresh_token"],
                       "scope": self.SCOPE}, form=True)
        if "access_token" not in r:
            return None
        self.store(r)
        return r["access_token"]

    def poll(self):
        if not self.client_id:
            return [], "needs_setup"
        tok = self.token()
        if not tok:
            return [], "needs_login"
        url = ("https://graph.microsoft.com/v1.0/me/chats?$expand=lastMessagePreview&$top=" + str(min(self.limit * 2, 50))
               + "&$orderby=lastMessagePreview/createdDateTime%20desc")
        r = http_json(url, headers={"Authorization": "Bearer " + tok})
        if r.get("_http_error"):
            raise RuntimeError("Microsoft Graph: " + str(r.get("error")))
        items = []
        for chat in r.get("value", []):
            p = chat.get("lastMessagePreview") or {}
            if not p:
                continue
            body = p.get("body") or {}
            who = ((p.get("from") or {}).get("user") or {}).get("displayName") or ((p.get("from") or {}).get("application") or {}).get("displayName") or "someone"
            where = chat.get("topic") or {"oneOnOne": "direct message", "group": "group chat", "meeting": "meeting chat"}.get(chat.get("chatType"), "chat")
            ts = _iso_to_ts(p.get("createdDateTime"))
            items.append({"who": who, "where": where, "text": strip_html(body.get("content"))[:300], "ts": ts,
                          "link": chat.get("webUrl") or ""})
        return items, "ok"


def _iso_to_ts(s):
    if not s:
        return 0.0
    from datetime import datetime, timezone
    s = s.replace("Z", "+00:00")
    if "." in s:
        head, tail = s.split(".", 1)
        frac = re.match(r"\d+", tail).group(0) if re.match(r"\d+", tail) else ""
        rest = tail[len(frac):]
        s = f"{head}.{frac[:6].ljust(6, '0')}{rest}" if frac else head + rest
    try:
        return datetime.fromisoformat(s).astimezone(timezone.utc).timestamp()
    except ValueError:
        return 0.0


# --------------------------------------------------------- notifications ----
class NotificationFeed(Feed):
    interval = 5
    default_title = "Notifications"

    def apps(self):
        ids = []
        for entry in self.cfg.get("apps") or []:
            ids += APP_PRESETS.get(entry, [entry])
        return ids

    def poll(self):
        ids = self.apps()
        if not ids:
            return [], "needs_setup"
        if not NOTIF_DB.exists() and not os.access(NOTIF_DB.parent, os.R_OK):
            return [], "needs_fda"
        try:
            con = sqlite3.connect(f"file:{NOTIF_DB}?mode=ro", uri=True, timeout=2)
        except sqlite3.OperationalError as exc:
            if "unable to open" in str(exc) or "authorization" in str(exc):
                return [], "needs_fda"
            raise
        try:
            marks = ",".join("?" * len(ids))
            rows = con.execute(
                f"select r.delivered_date, r.data, a.identifier from record r join app a on a.app_id = r.app_id "
                f"where a.identifier in ({marks}) order by r.delivered_date desc limit ?", (*ids, self.limit * 3)).fetchall()
        except sqlite3.OperationalError as exc:
            if "unable to open" in str(exc) or "authorization" in str(exc):
                return [], "needs_fda"
            raise RuntimeError(f"notification store layout not understood ({exc})")
        finally:
            con.close()
        items = []
        for delivered, blob, ident in rows:
            item = parse_notification(blob, delivered, ident)
            if item and item["text"] or (item and item["who"]):
                items.append(item)
        return items, "ok"


def parse_notification(blob, delivered, identifier):
    """The `data` column is a binary plist: {'app': bundle id, 'req': {'titl','subt','body',...}, 'date': ...}."""
    try:
        d = plistlib.loads(bytes(blob))
    except Exception:
        return None
    req = d.get("req") or {}
    ts = (delivered or d.get("date") or 0)
    ts = float(ts) + COCOA_EPOCH if ts and float(ts) < 1e11 else float(ts or 0)
    app = {"com.tinyspeck.slackmacgap": "Slack", "com.microsoft.teams2": "Teams", "com.microsoft.teams": "Teams",
           "com.apple.MobileSMS": "Messages", "com.apple.mail": "Mail", "com.hnc.Discord": "Discord"}.get(identifier, identifier)
    return {"who": str(req.get("titl") or "").strip(), "where": (str(req.get("subt") or "").strip() or app),
            "text": str(req.get("body") or "").strip()[:300], "ts": ts, "link": "", "app": app}


# -------------------------------------------------------------- ai chats ----
class AiFeed(Feed):
    """Chats that finished or need you, reported by the tools' hooks (see agent/hooks)."""
    interval = 2
    default_title = "AI chats"

    def poll(self):
        store = EVENT_STORE
        if store is None:
            return [], "needs_setup"
        items = []
        for e in store.list(self.limit):
            where = e.get("project") or "somewhere"
            app = (e.get("focus") or {}).get("app")
            if app:
                where += " · " + app
            text = e.get("title") or ""
            if e.get("text"):
                text += " — " + e["text"]
            items.append({"id": e["id"], "who": e.get("tool") or "AI", "where": where, "text": text[:300], "ts": e.get("ts", 0),
                          "link": "focus:" + e["id"], "state": e.get("state", "done"), "seen": bool(e.get("seen"))})
        return items, "ok"


# --------------------------------------------------------------- manager ----
def make_feed(cfg):
    src = cfg.get("source", "none")
    return {"ai": AiFeed, "slack": SlackFeed, "teams": TeamsFeed, "notifications": NotificationFeed}.get(src, NoFeed)(cfg)


class FeedManager:
    def __init__(self, feed_cfgs, log=print):
        self.log = log
        self.feeds = {}
        self.lock = threading.Lock()
        self.reload(feed_cfgs)
        threading.Thread(target=self._loop, daemon=True).start()

    def reload(self, feed_cfgs):
        with self.lock:
            self.feeds = {c["id"]: make_feed(c) for c in (feed_cfgs or []) if c.get("id")}
            self.due = {fid: 0 for fid in self.feeds}

    def _loop(self):
        while True:
            with self.lock:
                due = [(fid, f) for fid, f in self.feeds.items() if time.time() >= self.due.get(fid, 0)]
            for fid, f in due:
                f.refresh()
                with self.lock:
                    self.due[fid] = time.time() + f.interval
            time.sleep(1)

    def get(self, fid):
        with self.lock:
            return self.feeds.get(fid)

    def refresh_now(self, fid):
        f = self.get(fid)
        if f:
            f.refresh()
            with self.lock:
                self.due[fid] = time.time() + f.interval
        return f.snapshot() if f else None

    def snapshot(self):
        with self.lock:
            feeds = list(self.feeds.values())
        return [f.snapshot() for f in feeds]
