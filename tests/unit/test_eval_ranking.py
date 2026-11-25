"""`scripts/eval_ranking.py --fill` re-runs only what failed (roadmap R17).

Every call it makes spends one of 20 free-tier requests a day, shared with production, so
re-running a kept run costs quota, and silently keeping a run made under another prompt
corrupts the report.
"""

import importlib.util
import pathlib

import pytest

from vigil.commits.schemas import CommitAnalysis, CommitVerdict
from vigil.config import get_settings
from vigil.impact.catalog import ServiceCatalog

ROOT = pathlib.Path(__file__).parent.parent.parent
_spec = importlib.util.spec_from_file_location("eval_ranking", ROOT / "scripts" / "eval_ranking.py")
eval_ranking = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(eval_ranking)


class CountingClient:
    def __init__(self, sha: str):
        self.calls = 0
        self.sha = sha

    async def generate_structured(self, system, user, schema, call_type, fixture_key=None):
        self.calls += 1
        verdict = CommitVerdict(
            sha=self.sha[:10], rank=1, confidence=0.9, rationale="r", suggested_action="revert"
        )
        return CommitAnalysis(verdicts=[verdict], likely_culprit_sha=self.sha[:10])


@pytest.fixture()
def case():
    settings = get_settings()
    return eval_ranking._case("bad_deploy", settings, ServiceCatalog.load(ROOT / "services.yaml"))


async def _fill(case, prior, runs=3):
    client = CountingClient(case["planted"])
    result = await eval_ranking._run_case(
        case, client, get_settings(), runs, eval_ranking._Pacer(0), prior
    )
    return client.calls, result


async def test_a_fresh_run_makes_every_call(case):
    calls, result = await _fill(case, None)
    assert calls == 3
    assert all(r["culprit"] == case["planted"] for r in result["runs"])


async def test_fill_redoes_only_the_errored_runs(case):
    _, first = await _fill(case, None)
    kept = first["runs"][0]
    prior = dict(first, runs=[kept, {"date": "2026-11-24", "error": "429"}, {"error": "429"}])
    calls, result = await _fill(case, prior)
    assert calls == 2
    assert result["runs"][0] is kept
    assert not any("error" in r for r in result["runs"])


async def test_fill_adds_missing_runs(case):
    _, first = await _fill(case, None, runs=1)
    calls, result = await _fill(case, first, runs=3)
    assert calls == 2
    assert len(result["runs"]) == 3


async def test_fill_refuses_runs_made_under_another_prompt(case):
    _, first = await _fill(case, None)
    with pytest.raises(SystemExit, match="prompt changed"):
        await _fill(case, dict(first, prompt_sha256="0" * 16, runs=[{"error": "429"}]))


def test_pending_counts_what_a_fill_would_spend(case):
    prior = {"runs": [{"culprit": None}, {"error": "429"}]}
    assert eval_ranking._pending(case, 3, prior) == 2
    assert eval_ranking._pending(dict(case, top=[]), 3, None) == 0
