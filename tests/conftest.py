import pytest


@pytest.fixture(autouse=True)
def no_real_slack(monkeypatch):
    """Tests must never reach a real Slack workspace, even if the developer has tokens exported."""
    for var in ("SLACK_BOT_TOKEN", "SLACK_WEBHOOK_URL", "AEEO_SLACK_IDS", "AEEO_PUBLIC_URL"):
        monkeypatch.delenv(var, raising=False)
