"""Slack notifications for whoever needs to act next.

Whenever a run changes state, the orchestrator asks the notifier to tell the
people who now have something to do (and tells the requester when their
request ships or is closed). Every notification is written to an outbox table
first, so the dashboard can show it and nothing is lost if Slack is down;
delivery then happens in the background.

Delivery backends (set by environment variables, both optional):
  SLACK_BOT_TOKEN    xoxb-... bot token with chat:write. Sends a direct message to
                     each user's Slack member ID (users.json "slack_id", or AEEO_SLACK_IDS).
  SLACK_WEBHOOK_URL  incoming webhook for a team channel. Used for users without a
                     Slack ID, or when no bot token is set; the person is @-mentioned.
  AEEO_SLACK_IDS     "asha=U012AB3CD,priya=U045EF6GH" maps usernames to Slack member IDs.
  AEEO_PUBLIC_URL    base URL for links in messages (default http://127.0.0.1:8000).

With neither token set the notifier runs in outbox-only mode: notifications are
recorded and visible in the dashboard but not sent anywhere.
"""

from __future__ import annotations

import json
import os
import threading
import time
import urllib.error
import urllib.request
from typing import Callable

SLACK_POST = "https://slack.com/api/chat.postMessage"

SCHEMA = """
CREATE TABLE IF NOT EXISTS notifications (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    dedupe_key TEXT UNIQUE NOT NULL,
    run_id TEXT NOT NULL,
    username TEXT NOT NULL,
    kind TEXT NOT NULL,
    text TEXT NOT NULL,
    channel TEXT NOT NULL,          -- slack_dm | slack_webhook | outbox
    status TEXT NOT NULL,           -- pending | sent | failed | recorded
    error TEXT,
    created_at REAL NOT NULL,
    sent_at REAL
);
CREATE INDEX IF NOT EXISTS ix_notif_user ON notifications(username, id);
"""


def _http_post(url: str, payload: dict, headers: dict[str, str]) -> dict:
    req = urllib.request.Request(url, data=json.dumps(payload).encode(), method="POST",
                                 headers={"content-type": "application/json; charset=utf-8", **headers})
    with urllib.request.urlopen(req, timeout=8) as r:  # noqa: S310 - fixed Slack URLs / configured webhook
        body = r.read().decode() or "{}"
    try:
        return json.loads(body)
    except json.JSONDecodeError:
        return {"ok": body.strip() == "ok", "raw": body[:200]}


class Notifier:
    def __init__(self, store, *, bot_token: str | None = None, webhook_url: str | None = None,
                 slack_ids: dict[str, str] | None = None, public_url: str | None = None,
                 http_post: Callable[[str, dict, dict], dict] = _http_post, background: bool = True):
        self.store = store
        self.bot_token = bot_token if bot_token is not None else os.environ.get("SLACK_BOT_TOKEN", "")
        self.webhook_url = webhook_url if webhook_url is not None else os.environ.get("SLACK_WEBHOOK_URL", "")
        env_ids = dict(p.split("=", 1) for p in os.environ.get("AEEO_SLACK_IDS", "").split(",") if "=" in p)
        self.slack_ids = {**env_ids, **(slack_ids or {})}
        self.public_url = (public_url or os.environ.get("AEEO_PUBLIC_URL", "http://127.0.0.1:8000")).rstrip("/")
        self.http_post = http_post
        self.background = background
        with store._conn() as c:
            c.executescript(SCHEMA)

    # ------------------------------------------------------------ status
    def mode(self) -> dict:
        return {"bot": bool(self.bot_token), "webhook": bool(self.webhook_url),
                "mode": "slack" if (self.bot_token or self.webhook_url) else "outbox-only"}

    def link(self, run_id: str) -> str:
        return f"{self.public_url}/#/run/{run_id}"

    # -------------------------------------------------------------- send
    def notify(self, *, dedupe_key: str, run_id: str, user, kind: str, headline: str, detail: str,
               slack_id: str | None = None) -> bool:
        """Queue one notification. Returns False if this exact notification was already sent."""
        slack_id = self.slack_ids.get(user.username) or slack_id or ""
        if self.bot_token and slack_id:
            channel = "slack_dm"
        elif self.webhook_url:
            channel = "slack_webhook"
        else:
            channel = "outbox"
        text = f"{headline}\n{detail}"
        with self.store._conn() as c:
            cur = c.execute(
                "INSERT OR IGNORE INTO notifications (dedupe_key, run_id, username, kind, text, channel, status, "
                "created_at) VALUES (?,?,?,?,?,?,?,?)",
                (dedupe_key, run_id, user.username, kind, text, channel,
                 "recorded" if channel == "outbox" else "pending", time.time()),
            )
            if cur.rowcount == 0:
                return False
            notif_id = cur.lastrowid
        if channel != "outbox":
            payload = self._payload(channel, user, slack_id, run_id, headline, detail)
            if self.background:
                threading.Thread(target=self._deliver, args=(notif_id, channel, payload), daemon=True).start()
            else:
                self._deliver(notif_id, channel, payload)
        return True

    def _payload(self, channel: str, user, slack_id: str, run_id: str, headline: str, detail: str) -> dict:
        who = f"<@{slack_id}>" if slack_id else f"*{user.name}*"
        lead = headline if channel == "slack_dm" else f"{who} {headline[0].lower()}{headline[1:]}"
        blocks = [
            {"type": "section", "text": {"type": "mrkdwn", "text": f"{lead}\n{detail}"}},
            {"type": "actions", "elements": [{"type": "button", "text": {"type": "plain_text", "text": "Open run"},
                                              "url": self.link(run_id), "style": "primary"}]},
        ]
        body = {"text": f"{lead} {detail}", "blocks": blocks}
        if channel == "slack_dm":
            body["channel"] = slack_id
        return body

    def _deliver(self, notif_id: int, channel: str, payload: dict) -> None:
        try:
            if channel == "slack_dm":
                resp = self.http_post(SLACK_POST, payload, {"authorization": f"Bearer {self.bot_token}"})
            else:
                resp = self.http_post(self.webhook_url, payload, {})
            ok, err = bool(resp.get("ok")), resp.get("error")
        except (urllib.error.URLError, TimeoutError, OSError, ValueError) as e:
            ok, err = False, str(e)[:300]
        with self.store._conn() as c:
            c.execute("UPDATE notifications SET status=?, error=?, sent_at=? WHERE id=?",
                      ("sent" if ok else "failed", None if ok else (err or "unknown error"), time.time(), notif_id))

    # -------------------------------------------------------------- read
    def for_user(self, username: str | None, limit: int = 50) -> list[dict]:
        q = "SELECT n.*, r.title FROM notifications n JOIN runs r ON r.id=n.run_id"
        args: tuple = ()
        if username:
            q += " WHERE n.username=?"
            args = (username,)
        with self.store._conn() as c:
            rows = c.execute(q + " ORDER BY n.id DESC LIMIT ?", (*args, limit)).fetchall()
        return [dict(r) for r in rows]
