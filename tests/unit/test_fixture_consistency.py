"""Every sha named by a recorded LLM response must exist in the commit fixture (roadmap R2).

`rank_commits_llm` drops verdicts whose sha is not among the scored candidates
(graph/triage.py, the anti-hallucination guard from spec §6.3). That guard cannot tell a
hallucinated sha from a fixture set that drifted out of sync: both produce an empty verdict
list and `culprit=None`, which reads exactly like an honest "no culprit found". R2 rewrites
every sha in the repo when the demo repo's real 40-character shas replace the invented ones,
so the drift has to be loud.
"""

import json
import pathlib

import pytest

ROOT = pathlib.Path(__file__).parent.parent.parent
GITHUB_DIR = ROOT / "tests" / "fixtures" / "github"
LLM_DIR = ROOT / "tests" / "fixtures" / "llm"
SCENARIOS_DIR = ROOT / "simulator" / "scenarios"

SCENARIOS = sorted(p.stem for p in GITHUB_DIR.glob("*.json"))


def _shas(scenario: str) -> set[str]:
    data = json.loads((GITHUB_DIR / f"{scenario}.json").read_text(encoding="utf-8"))
    return {c["sha"] for c in data["commits"]}


@pytest.mark.parametrize("scenario", SCENARIOS)
def test_ranking_verdicts_name_only_real_commits(scenario):
    path = LLM_DIR / f"commit_ranking.{scenario}.json"
    if not path.exists():
        pytest.skip(f"{scenario} falls back to commit_ranking.default.json")
    ranking = json.loads(path.read_text(encoding="utf-8"))
    known = _shas(scenario)

    unknown = {v["sha"] for v in ranking["verdicts"]} - known
    assert not unknown, f"{scenario}: verdicts name shas absent from the commit fixture: {unknown}"

    culprit = ranking["likely_culprit_sha"]
    assert culprit is None or culprit in known, f"{scenario}: culprit {culprit} is not a real commit"


@pytest.mark.parametrize("scenario", SCENARIOS)
def test_planted_deploys_ship_only_real_commits(scenario):
    """f_deploy matches deploy_events.commit_shas against candidate shas; a stale sha there
    silently zeroes the strongest signal the scorer has."""
    path = SCENARIOS_DIR / f"{scenario}.json"
    if not path.exists():
        pytest.skip(f"no simulator scenario for {scenario}")
    known = _shas(scenario)
    for deploy in json.loads(path.read_text(encoding="utf-8")).get("deploys", []):
        unknown = set(deploy["commit_shas"]) - known
        assert not unknown, f"{scenario}: deploy ships shas absent from the commit fixture: {unknown}"


def test_every_commit_fixture_has_a_simulator_scenario():
    assert SCENARIOS == sorted(p.stem for p in SCENARIOS_DIR.glob("*.json"))
