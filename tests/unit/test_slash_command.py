"""`/vigil status` rendering (roadmap R10)."""

from datetime import UTC, datetime, timedelta

import pytest

from vigil.slack.blocks import STATUS_LIMIT, age_label, build_status_message

NOW = datetime(2026, 9, 30, 12, 0, tzinfo=UTC)
DASH = "https://dash.example"


def _row(i: int = 0, severity: str | None = "SEV1", title: str = "HighErrorRate on checkout") -> dict:
    return {"id": f"id-{i}", "title": title, "severity": severity, "created_at": NOW - timedelta(minutes=5)}


def test_empty_is_ephemeral_no_incidents():
    assert build_status_message([], DASH, NOW) == {"response_type": "ephemeral", "text": "No open incidents."}


def test_one_row_renders_emoji_link_and_age():
    msg = build_status_message([_row()], DASH, NOW)
    assert msg["response_type"] == "ephemeral"
    assert msg["text"] == (
        "*1 open incident*\n"
        "\U0001f534 *SEV1* <https://dash.example/incidents/id-0|HighErrorRate on checkout> · 5m"
    )
    assert msg["blocks"][0]["text"]["text"] == msg["text"]


@pytest.mark.parametrize(
    ("severity", "emoji", "label"),
    [
        ("SEV2", "\U0001f7e0", "SEV2"),
        ("SEV3", "\U0001f7e1", "SEV3"),
        ("SEV4", "⚪", "SEV4"),
        (None, "⚪", "SEV?"),
    ],
)
def test_severity_emoji_and_fallback(severity, emoji, label):
    line = build_status_message([_row(severity=severity)], DASH, NOW)["text"].splitlines()[1]
    assert line.startswith(f"{emoji} *{label}* ")


def test_title_is_escaped_so_link_survives():
    text = build_status_message([_row(title="p99 > 2s & <rising>")], DASH, NOW)["text"]
    assert "|p99 &gt; 2s &amp; &lt;rising&gt;>" in text


def test_extra_row_signals_cap():
    msg = build_status_message([_row(i) for i in range(STATUS_LIMIT + 1)], DASH, NOW)
    lines = msg["text"].splitlines()
    assert lines[0] == f"*{STATUS_LIMIT} newest open incidents*"
    assert len(lines) == STATUS_LIMIT + 1


def test_exactly_at_cap_is_not_capped():
    msg = build_status_message([_row(i) for i in range(STATUS_LIMIT)], DASH, NOW)
    assert msg["text"].splitlines()[0] == f"*{STATUS_LIMIT} open incidents*"


@pytest.mark.parametrize(
    ("delta", "label"),
    [
        (timedelta(seconds=0), "0s"),
        (timedelta(seconds=59), "59s"),
        (timedelta(seconds=60), "1m"),
        (timedelta(minutes=59, seconds=59), "59m"),
        (timedelta(hours=1), "1h"),
        (timedelta(hours=23, minutes=59), "23h"),
        (timedelta(days=2, hours=5), "2d"),
        (timedelta(seconds=-5), "0s"),
    ],
)
def test_age_label(delta, label):
    assert age_label(NOW - delta, NOW) == label
