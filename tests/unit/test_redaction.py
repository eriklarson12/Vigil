"""Secret-redaction processor tests (roadmap R13).

No timing assertion: a microsecond budget asserted in pytest is flake, not coverage.
The performance requirement is met by construction — patterns compiled at import, the
secret tuple cached for the process.

Assertions check structure and the absence of secrets, never literal token text:
tests/conftest.py pins both webhook tokens to "dev-token", which is long enough that
the value pass legitimately scrubs it.
"""

import json

import httpx
import pytest
import structlog

from vigil.config import get_settings
from vigil.logging_utils import _secret_values, configure_logging, redact

WEBHOOK = "https://hooks.slack.com/services/T01ABCDEF/B02GHIJKL/zZ9secretpathvalue"


@pytest.fixture(autouse=True)
def _clean_caches():
    """Both caches are process-global; a test that repoints env must leave neither behind."""
    _secret_values.cache_clear()
    get_settings.cache_clear()
    yield
    _secret_values.cache_clear()
    get_settings.cache_clear()


@pytest.fixture
def live_webhook(monkeypatch):
    monkeypatch.setenv("SLACK_WEBHOOK_URL", WEBHOOK)
    get_settings.cache_clear()
    _secret_values.cache_clear()
    return WEBHOOK


def _apply(event_dict: dict) -> dict:
    return redact(None, "info", event_dict)


def test_httpx_status_error_does_not_leak_the_webhook(live_webhook):
    """The leak this module exists to close: raise_for_status puts the URL in str(exc),
    and a Slack incoming webhook URL is the credential."""
    request = httpx.Request("POST", live_webhook)
    exc = httpx.HTTPStatusError(
        f"Client error '403 Forbidden' for url '{live_webhook}'",
        request=request,
        response=httpx.Response(403, request=request),
    )
    detail = f"{type(exc).__name__}: {exc}"  # exactly what degrading() builds
    assert live_webhook in detail

    out = _apply({"event": "node_degraded", "node": "post_brief", "error": detail})
    assert live_webhook not in out["error"]
    assert "zZ9secretpathvalue" not in out["error"]
    assert "***" in out["error"]
    assert out["node"] == "post_brief"


def test_masks_secret_key_names_at_any_depth():
    out = _apply(
        {
            "event": "config_dump",
            "cfg": {"slack_bot_token": "xoxb-not-a-real-token", "slack_channel": "#incidents"},
            "headers": [{"Authorization": "Bearer abc"}, {"Accept": "application/json"}],
            "counts": {"api_key_rotations": 3},
        }
    )
    assert out["cfg"]["slack_bot_token"] == "***"
    assert out["cfg"]["slack_channel"] == "#incidents"
    assert out["headers"][0]["Authorization"] == "***"
    assert out["headers"][1]["Accept"] == "application/json"
    assert out["counts"]["api_key_rotations"] == "***"  # masked whatever the type


def test_scrubs_secret_values_under_harmless_keys(live_webhook):
    # neither key matches the key pattern; both values are caught by the value pass
    out = _apply({"event": f"posting to {live_webhook} failed", "target": live_webhook})
    assert live_webhook not in out["event"]
    assert out["target"] == "***"


def test_short_and_empty_secrets_never_scrub(monkeypatch):
    monkeypatch.setenv("SLACK_WEBHOOK_URL", "")
    monkeypatch.setenv("GITHUB_TOKEN", "abc")
    get_settings.cache_clear()
    _secret_values.cache_clear()

    text = "abc password reset instructions sent"
    out = _apply({"event": text, "runbook": "rotate the abc key"})
    assert out["event"] == text
    assert out["runbook"] == "rotate the abc key"


def test_rewrites_connection_string_passwords():
    dsn = "postgresql://vigil:hunter2@db.example.com:5432/vigil"
    out = _apply({"event": "db_open_retry", "error": f"could not connect to {dsn}"})
    assert "hunter2" not in out["error"]
    assert "postgresql://vigil:***@db.example.com:5432/vigil" in out["error"]
    assert out["error"].startswith("could not connect to ")


def test_redact_runs_first_in_the_configured_chain():
    # same chain main.py installs at import, so re-configuring here changes nothing
    configure_logging()
    assert structlog.get_config()["processors"][0] is redact


def test_full_chain_renders_a_redacted_json_line(live_webhook):
    configure_logging()
    event_dict = {"event": "node_degraded", "error": f"boom {live_webhook}"}
    for processor in structlog.get_config()["processors"]:
        event_dict = processor(None, "error", event_dict)

    payload = json.loads(event_dict)
    assert live_webhook not in payload["error"]
    assert payload["event"] == "node_degraded"
    assert payload["level"] == "error"
