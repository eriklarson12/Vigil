"""R9 issue text, built from the stored action items with no LLM call."""

from vigil.graph.action_items import issue_body, issue_title, marker, mock_url

INCIDENT = "0b6f2a4e-1111-4000-8000-000000000001"
ITEM = {"description": "Add a canary stage for checkout deploys", "owner": "@erik", "priority": "P1"}


def test_title_carries_the_incident_short_id():
    assert issue_title(INCIDENT, ITEM) == "[Vigil 0b6f2a4e] Add a canary stage for checkout deploys"


def test_title_is_truncated_to_githubs_limit():
    assert len(issue_title(INCIDENT, {**ITEM, "description": "x" * 400})) == 256


def test_body_carries_priority_owner_link_and_marker():
    body = issue_body(INCIDENT, 2, ITEM, "https://dash.example")
    assert "**Priority:** P1 · **Owner:** @erik" in body
    assert f"https://dash.example/incidents/{INCIDENT}" in body
    assert body.endswith(f"<!-- vigil:{INCIDENT}:2 -->")
    assert marker(INCIDENT, 2) in body


def test_mock_url():
    assert mock_url("github.com/o/r", INCIDENT, 0) == "mock://github.com/o/r/issues/0b6f2a4e-0"
