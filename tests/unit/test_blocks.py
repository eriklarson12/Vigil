"""The brief must render under every degradation combination (spec §13.2)."""

import json

from vigil.slack.blocks import build_brief, confidence_bar

ALERT = {
    "labels": {"alertname": "HighErrorRate", "service": "checkout", "severity": "critical"},
    "annotations": {"summary": "errors up"},
    "starts_at": "2026-07-01T12:00:00+00:00",
}
IMPACT = {
    "severity": "SEV1", "est_requests_affected": 12000, "baseline_rpm": 1200,
    "minutes_open": 10, "blast_radius": ["orders"], "unknown_service": False,
}


def _texts(payload):
    return json.dumps(payload)


def test_full_brief_renders():
    payload = build_brief(
        incident={"id": "i-1"}, alert=ALERT, impact=IMPACT,
        commit_analysis={
            "verdicts": [{"sha": "a1b2c3d4e5", "rank": 1, "confidence": 0.86,
                          "rationale": "removed validation", "suggested_action": "rollback_deploy"}],
            "likely_culprit_sha": "a1b2c3d4e5", "no_culprit_reason": None,
        },
        commit_scores=[], runbook_chunks=[],
        brief_text={"impact_narrative": "n", "cause_summary": "c",
                    "runbook_citation": {"slug": "checkout-service", "heading_path": "A > B", "why": "w"},
                    "next_steps": ["s1"]},
        errors={}, repo="github.com/o/r", dashboard_url="http://d",
    )
    text = _texts(payload)
    assert "Likely culprit" in text
    assert "a1b2c3d4e5"[:10] in text
    assert "Mark resolved" in text
    assert payload["attachments"][0]["color"] == "#E01E5A"


def test_deterministic_fallback_when_llm_failed():
    payload = build_brief(
        incident={"id": "i-2"}, alert=ALERT, impact=IMPACT,
        commit_analysis=None, commit_scores=[], runbook_chunks=[],
        brief_text=None, errors={"compose_brief": "LLM down", "rank_commits_llm": "quota"},
        repo=None, dashboard_url="http://d",
    )
    text = _texts(payload)
    assert "commit analysis unavailable" in text
    assert "no matching runbook found" in text
    assert "12,000" in text  # deterministic impact numbers still render
    assert "degraded" in text


def test_no_culprit_wording():
    payload = build_brief(
        incident={"id": "i-3"}, alert=ALERT, impact=IMPACT,
        commit_analysis={"verdicts": [], "likely_culprit_sha": None,
                         "no_culprit_reason": "nothing scored above threshold"},
        commit_scores=[], runbook_chunks=[], brief_text=None, errors={},
        repo=None, dashboard_url="http://d",
    )
    assert "no likely commit identified" in _texts(payload)


def test_possible_culprit_hedged_wording():
    payload = build_brief(
        incident={"id": "i-4"}, alert=ALERT, impact=IMPACT,
        commit_analysis={
            "verdicts": [{"sha": "abc", "rank": 1, "confidence": 0.5,
                          "rationale": "maybe", "suggested_action": "investigate"}],
            "likely_culprit_sha": "abc", "no_culprit_reason": None,
        },
        commit_scores=[], runbook_chunks=[], brief_text=None, errors={},
        repo=None, dashboard_url="http://d",
    )
    text = _texts(payload)
    assert "Possible culprit" in text
    assert "Likely culprit" not in text


def test_confidence_bar():
    assert confidence_bar(0.78) == "▓▓▓▓░ 78%"
    assert confidence_bar(0.0) == "░░░░░ 0%"
    assert confidence_bar(1.0) == "▓▓▓▓▓ 100%"
