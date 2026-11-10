from vigil.llm.prompts import build_brief_prompt, build_commit_ranking_prompt

ALERT = {
    "labels": {"alertname": "HighErrorRate", "service": "checkout", "severity": "critical",
               "scenario": "bad_deploy"},
    "annotations": {"summary": "errors", "description": "many errors"},
    "starts_at": "2026-07-01T12:00:00+00:00",
}


def test_ranking_prompt_truncates_long_diffs():
    patch = "\n".join(f"+line {i}" for i in range(300))
    candidates = [{
        "score": 0.7,
        "commit": {"sha": "a" * 10, "author": "d", "committed_at": "t", "message": "m",
                   "files": [{"path": "x.py", "additions": 300, "deletions": 0}], "patch": patch},
    }]
    _, user = build_commit_ranking_prompt(ALERT, None, candidates)
    assert "[diff truncated at 120 lines]" in user


def test_prompts_never_name_the_scenario():
    """The simulator's label names the planted cause; a model that reads it is being told the answer."""
    candidates = [{
        "score": 0.7,
        "commit": {"sha": "a" * 10, "author": "d", "committed_at": "t", "message": "m",
                   "files": [], "patch": ""},
    }]
    for _, user in (
        build_commit_ranking_prompt(ALERT, None, candidates),
        build_brief_prompt(ALERT, None, None, None, []),
    ):
        assert "bad_deploy" not in user and "scenario" not in user


def test_brief_prompt_states_unavailable_commit_analysis():
    _, user = build_brief_prompt(ALERT, None, None, "GitHub API error", [])
    assert "UNAVAILABLE: GitHub API error" in user
    assert "(none retrieved)" in user


def test_brief_prompt_states_no_culprit():
    analysis = {"verdicts": [], "likely_culprit_sha": None, "no_culprit_reason": "all below floor"}
    _, user = build_brief_prompt(ALERT, None, analysis, None, [])
    assert "NO PLAUSIBLE COMMIT: all below floor" in user
