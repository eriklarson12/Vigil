"""Guards on what the ranking model is allowed to name (spec §6.3).

The regression these cover: the prompt shows `sha[:SHORT_SHA]`, so a verdict comes back
naming a prefix while the candidates carry full 40-character shas. Comparing the two for
equality dropped every verdict as invented and reported no culprit — and reported it
silently, since that is the same output as an honest "nothing plausible here". Fixture
mode could not catch it: `record_fixtures.py` writes full-width shas into the recorded
responses, so only a live model ever produced the short form.
"""

import pytest

from vigil.commits.schemas import CommitAnalysis, CommitVerdict
from vigil.graph.triage import apply_sha_guards
from vigil.llm.prompts import SHORT_SHA

CULPRIT = "71cd87314d4ab16755b3b8f721372a5b1e1fdab1"
OTHER = "520191a2fa9c0e2a6d5f8b3c1e4a7d9b2c6f0e83"
TOP = [{"sha": CULPRIT}, {"sha": OTHER}]


def _verdict(sha: str, confidence: float = 0.86, rank: int = 1) -> CommitVerdict:
    return CommitVerdict(
        sha=sha, rank=rank, confidence=confidence,
        rationale="removed the REQUIRES_CONFIRMATION state check", suggested_action="revert",
    )


def _analysis(sha: str, **kw) -> CommitAnalysis:
    return CommitAnalysis(verdicts=[_verdict(sha, **kw)], likely_culprit_sha=sha)


def test_short_sha_verdict_resolves_to_the_full_candidate():
    """The live-model shape: everything it says is SHORT_SHA wide."""
    result = apply_sha_guards(_analysis(CULPRIT[:SHORT_SHA]), TOP, confidence_floor=0.4)
    assert result.likely_culprit_sha == CULPRIT, "prefix must resolve to the full sha"
    assert [v.sha for v in result.verdicts] == [CULPRIT]
    assert result.no_culprit_reason is None


def test_full_sha_verdict_still_resolves():
    """The recorded-fixture shape, which must keep working."""
    result = apply_sha_guards(_analysis(CULPRIT), TOP, confidence_floor=0.4)
    assert result.likely_culprit_sha == CULPRIT
    assert [v.sha for v in result.verdicts] == [CULPRIT]


def test_resolved_sha_is_full_width_so_the_commit_link_works():
    """blocks.py builds /commit/{sha}; a 10-character sha there is a 404."""
    result = apply_sha_guards(_analysis(CULPRIT[:SHORT_SHA]), TOP, confidence_floor=0.4)
    assert len(result.verdicts[0].sha) == len(CULPRIT)


@pytest.mark.parametrize("invented", ["deadbeefcafe", "0" * 40, "", "71cd87314e"])
def test_invented_sha_is_still_dropped(invented):
    """Resolving by prefix must not weaken the guard. Note the last case: one character
    off the real culprit, which a substring match would wrongly accept."""
    result = apply_sha_guards(_analysis(invented), TOP, confidence_floor=0.4)
    assert result.verdicts == []
    assert result.likely_culprit_sha is None


def test_a_verdict_naming_an_unscored_commit_is_dropped_but_others_survive():
    analysis = CommitAnalysis(
        verdicts=[_verdict("ffffffffff", rank=1), _verdict(OTHER[:SHORT_SHA], rank=2)],
        likely_culprit_sha=OTHER[:SHORT_SHA],
    )
    result = apply_sha_guards(analysis, TOP, confidence_floor=0.4)
    assert [v.sha for v in result.verdicts] == [OTHER]
    assert result.likely_culprit_sha == OTHER


def test_confidence_floor_still_nulls_the_culprit():
    analysis = CommitAnalysis(
        verdicts=[_verdict(CULPRIT[:SHORT_SHA], confidence=0.39)],
        likely_culprit_sha=CULPRIT[:SHORT_SHA],
        no_culprit_reason="nothing conclusive",
    )
    result = apply_sha_guards(analysis, TOP, confidence_floor=0.4)
    assert result.likely_culprit_sha is None
    assert result.no_culprit_reason == "nothing conclusive"
    assert [v.sha for v in result.verdicts] == [CULPRIT], "the verdict survives, the culprit does not"


def test_null_culprit_passes_through():
    analysis = CommitAnalysis(verdicts=[], likely_culprit_sha=None, no_culprit_reason="no match")
    result = apply_sha_guards(analysis, TOP, confidence_floor=0.4)
    assert result.likely_culprit_sha is None
    assert result.no_culprit_reason == "no match"
