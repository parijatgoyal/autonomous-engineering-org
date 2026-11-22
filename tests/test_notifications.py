from aeeo.config import ROOT, Settings
from aeeo.notifications import SLACK_POST, Notifier
from aeeo.orchestrator import Orchestrator
from aeeo.store import Store

REQ = (ROOT / "requirements" / "discount_codes.md").read_text()


class FakeSlack:
    def __init__(self, fail=False):
        self.calls, self.fail = [], fail

    def __call__(self, url, payload, headers):
        self.calls.append((url, payload, headers))
        return {"ok": False, "error": "channel_not_found"} if self.fail else {"ok": True}


def make(tmp_path, **kw):
    s = Settings(data_dir=tmp_path / "data", provider="mock")
    s.provider_backoff_s = 0.0
    store = Store(s.db_path)
    notifier = Notifier(store, public_url="http://ctl.test", background=False, **kw)
    return Orchestrator(settings=s, store=store, notifier=notifier)


def inbox(o, user):
    return [(n["kind"], n["status"]) for n in o.notifier.for_user(user)]


def test_only_the_next_owner_is_notified_through_the_whole_journey(tmp_path):
    o = make(tmp_path, bot_token="", webhook_url="")
    rid = o.start(REQ, "shopkit", created_by="asha", plan_review=True)
    assert inbox(o, "asha") == [("confirm_plan", "recorded")]
    assert inbox(o, "sam") == [] and inbox(o, "priya") == []

    o.confirm_plan(rid, "asha")
    o.execute(rid)
    assert inbox(o, "sam") == [("signoff", "recorded")]
    assert inbox(o, "priya") == []  # not their turn until security signs off
    assert inbox(o, "dev") == []  # engineers aren't pinged for routine approvals

    o.signoff(rid, "sam", "security")
    assert inbox(o, "priya") == [("approve", "recorded")]
    o.approve(rid, "priya")
    assert inbox(o, "asha")[0] == ("shipped", "recorded")

    o._notify(rid)  # re-checking the same state never sends duplicates
    assert len(o.notifier.for_user(None)) == 4


def test_bot_token_sends_direct_messages(tmp_path, monkeypatch):
    slack = FakeSlack()
    monkeypatch.setenv("AEEO_SLACK_IDS", "sam=U0SAM")
    o = make(tmp_path, bot_token="xoxb-test", webhook_url="", http_post=slack)
    rid = o.start(REQ, "shopkit", created_by="asha")
    url, payload, headers = slack.calls[-1]
    assert url == SLACK_POST and payload["channel"] == "U0SAM"
    assert headers["authorization"] == "Bearer xoxb-test"
    assert "Security sign-off needed" in payload["text"]
    assert payload["blocks"][1]["elements"][0]["url"] == f"http://ctl.test/#/run/{rid}"
    assert inbox(o, "sam") == [("signoff", "sent")]


def test_webhook_mentions_people_without_slack_ids(tmp_path):
    slack = FakeSlack()
    o = make(tmp_path, bot_token="", webhook_url="https://hooks.slack.test/x", http_post=slack)
    o.start(REQ, "shopkit", created_by="asha", faults=["disagreement_deadlock"])
    texts = [p["text"] for _, p, _ in slack.calls]
    assert any(t.startswith("*Dev Mehta* design decision needed") for t in texts)
    assert any(t.startswith("*Priya Nair* design decision needed") for t in texts)


def test_slack_failure_is_recorded_and_never_breaks_the_run(tmp_path, monkeypatch):
    monkeypatch.setenv("AEEO_SLACK_IDS", "sam=U0SAM")
    o = make(tmp_path, bot_token="xoxb-test", webhook_url="", http_post=FakeSlack(fail=True))
    rid = o.start(REQ, "shopkit", created_by="asha")
    assert o.store.get_run(rid)["status"] == "AWAITING_APPROVAL"
    n = o.notifier.for_user("sam")[0]
    assert n["status"] == "failed" and n["error"] == "channel_not_found"
