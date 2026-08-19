"""The dashboard's brief panel reads slack_payload off the brief_posted event,
so every SLACK_MODE must record it — not just mock (roadmap R1 phase 0)."""

from typing import Any

import pytest

from vigil.config import Settings
from vigil.slack import sender as sender_mod
from vigil.slack.sender import SlackSender

PAYLOAD = {"text": "hi", "attachments": [{"color": "#E01E5A", "blocks": []}]}


@pytest.fixture
def recorded(monkeypatch) -> list[tuple[str, dict[str, Any]]]:
    events: list[tuple[str, dict[str, Any]]] = []

    async def fake_add_event(pool, incident_id, event_type, payload=None):
        events.append((event_type, payload or {}))

    monkeypatch.setattr(sender_mod, "add_event", fake_add_event)
    return events


def _sender(**overrides: Any) -> SlackSender:
    # _env_file=None: the developer's real .env holds a live SLACK_BOT_TOKEN, and
    # inheriting it would make these tests post to a real Slack workspace.
    settings = Settings(
        _env_file=None,
        **{"slack_webhook_url": "https://hooks.example/x", "slack_bot_token": "", **overrides},
    )
    return SlackSender(settings, pool=None)  # type: ignore[arg-type]


async def test_mock_mode_records_payload(recorded):
    ts = await _sender(slack_mode="mock").post_brief("inc-1", PAYLOAD)
    assert ts == "mock-inc-1"
    assert recorded == [("brief_posted", {"slack_payload": PAYLOAD})]


async def test_webhook_mode_records_payload(recorded, monkeypatch):
    async def fake_post_webhook(self, payload):
        return None

    monkeypatch.setattr(SlackSender, "_post_webhook", fake_post_webhook)
    ts = await _sender(slack_mode="webhook").post_brief("inc-2", PAYLOAD)
    assert ts == "webhook-inc-2"
    assert recorded == [("brief_posted", {"via": "webhook", "slack_payload": PAYLOAD})]


async def test_bot_token_mode_records_payload(recorded, monkeypatch):
    async def fake_post_api(self, payload, thread_ts=None):
        return "1720000000.000100"

    monkeypatch.setattr(SlackSender, "_post_api", fake_post_api)
    ts = await _sender(slack_mode="webhook", slack_bot_token="xoxb-test").post_brief("inc-3", PAYLOAD)
    assert ts == "1720000000.000100"
    assert recorded == [
        ("brief_posted", {"ts": "1720000000.000100", "slack_payload": PAYLOAD})
    ]


async def test_thread_records_payload(recorded, monkeypatch):
    async def fake_post_api(self, payload, thread_ts=None):
        return "x"

    monkeypatch.setattr(SlackSender, "_post_api", fake_post_api)
    s = _sender(slack_mode="webhook", slack_bot_token="xoxb-test")
    await s.post_thread("inc-4", "1720000000.000100", PAYLOAD)
    assert recorded == [("postmortem_posted", {"slack_payload": PAYLOAD})]
