"""R5 revert gate, button, and PR payload (roadmap R5, ADR-012)."""

import json
import pathlib

import pytest

from vigil.commits.rollback import branch_name, build_pr_payload
from vigil.slack.blocks import build_brief, build_revert_result_message, revert_gate

LLM_FIXTURES = pathlib.Path(__file__).parent.parent / "fixtures" / "llm"
ALERT = {"labels": {"service": "checkout", "alertname": "HighErrorRate"}, "starts_at": "t"}
INCIDENT_ID = "0b6f2a4e-0000-4000-8000-000000000001"
SHA = "a1b2c3d4e5f60718293a4b5c6d7e8f9012345678"


@pytest.mark.parametrize(
    ("confidence", "action", "expected"),
    [
        (0.65, "revert", True),
        (0.64, "revert", False),
        (0.86, "rollback_deploy", True),
        (0.93, "config_fix", False),
        (0.99, "investigate", False),
        (None, "revert", False),
        (0.9, None, False),
    ],
)
def test_revert_gate(confidence, action, expected):
    assert revert_gate(confidence, action) is expected


def _brief(commit_analysis):
    return build_brief(
        incident={"id": INCIDENT_ID}, alert=ALERT, impact=None,
        commit_analysis=commit_analysis, commit_scores=[], runbook_chunks=[],
        brief_text=None, errors={}, repo=None, dashboard_url="http://d",
    )


def _action_ids(payload):
    actions = next(b for b in payload["attachments"][0]["blocks"] if b["type"] == "actions")
    return [e.get("action_id") for e in actions["elements"]]


@pytest.mark.parametrize(
    ("scenario", "shows"),
    [
        ("bad_deploy", True),
        ("hotfix_regression", True),
        ("dependency_bump", True),
        ("config_typo", False),
        ("ambiguous_latency", False),
    ],
)
def test_button_follows_recorded_verdicts(scenario, shows):
    analysis = json.loads((LLM_FIXTURES / f"commit_ranking.{scenario}.json").read_text())
    assert ("propose_revert" in _action_ids(_brief(analysis))) is shows


def test_button_absent_without_commit_analysis():
    assert _action_ids(_brief(None)) == ["resolve_incident", None]


def test_button_carries_incident_and_confirm_dialog():
    analysis = {
        "verdicts": [{"sha": SHA, "rank": 1, "confidence": 0.8, "rationale": "r",
                      "suggested_action": "revert"}],
        "likely_culprit_sha": SHA,
    }
    actions = next(b for b in _brief(analysis)["attachments"][0]["blocks"] if b["type"] == "actions")
    button = next(e for e in actions["elements"] if e.get("action_id") == "propose_revert")
    assert button["value"] == INCIDENT_ID
    assert button["style"] == "danger"
    assert SHA[:10] in button["confirm"]["text"]["text"]


def test_branch_name_is_deterministic():
    assert branch_name(SHA, INCIDENT_ID) == f"vigil/revert-a1b2c3d-{INCIDENT_ID}"


def test_pr_payload_is_built_from_stored_rows():
    ctx = {
        "id": INCIDENT_ID, "service": "checkout", "sha": SHA,
        "message": "feat: skip cart validation\n\nlong body", "llm_confidence": 0.86,
        "llm_rationale": "removed the null check", "llm_suggested_action": "rollback_deploy",
    }
    pr = build_pr_payload(ctx, "github.com/o/r", "http://d")
    assert pr["title"] == 'Revert "feat: skip cart validation"'
    assert pr["branch"] == branch_name(SHA, INCIDENT_ID)
    assert f"Reverts {SHA}." in pr["body"]
    assert "86% confidence" in pr["body"]
    assert "removed the null check" in pr["body"]
    assert f"http://d/incidents/{INCIDENT_ID}" in pr["body"]
    assert "never merges" in pr["body"]


def test_result_message_never_replaces_the_brief():
    ok = build_revert_result_message(SHA, "https://github.com/o/r/pull/1", None)
    failed = build_revert_result_message(SHA, None, "merge commit")
    assert ok["replace_original"] is False and failed["replace_original"] is False
    assert "pull/1" in ok["text"]
    assert "merge commit" in failed["text"]
