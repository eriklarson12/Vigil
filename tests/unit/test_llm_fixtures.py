"""Every recorded fixture must validate through the REAL Pydantic schemas —
schema drift breaks loudly here (spec §16)."""

import json
import pathlib

import pytest

from tests.conftest import planted_culprit
from vigil.commits.schemas import CommitAnalysis
from vigil.config import get_settings
from vigil.llm.client import FakeLLMClient
from vigil.llm.schemas import BriefContent, Postmortem

FIXTURES = pathlib.Path(__file__).parent.parent / "fixtures" / "llm"
SCHEMAS = {
    "commit_ranking": CommitAnalysis,
    "brief_composition": BriefContent,
    "postmortem": Postmortem,
}


@pytest.mark.parametrize("path", sorted(FIXTURES.glob("*.json")), ids=lambda p: p.name)
def test_fixture_validates_against_schema(path):
    call_type = path.name.split(".")[0]
    schema = SCHEMAS[call_type]
    schema.model_validate(json.loads(path.read_text(encoding="utf-8")))


async def test_fake_client_resolves_scenario_from_fixture_key():
    client = FakeLLMClient(FIXTURES)
    result = await client.generate_structured(
        "system", "## Alert", CommitAnalysis, "commit_ranking", fixture_key="bad_deploy"
    )
    assert result.likely_culprit_sha == planted_culprit("bad_deploy")


async def test_fake_client_ignores_a_scenario_named_in_the_prompt():
    client = FakeLLMClient(FIXTURES)
    result = await client.generate_structured(
        "system", "scenario: bad_deploy", CommitAnalysis, "commit_ranking"
    )
    assert result.likely_culprit_sha is None


@pytest.mark.parametrize("key", ["something_unknown", "../github/bad_deploy", None])
async def test_fake_client_falls_back_to_default(key):
    client = FakeLLMClient(FIXTURES)
    result = await client.generate_structured(
        "system", "## Alert", CommitAnalysis, "commit_ranking", fixture_key=key
    )
    assert result.likely_culprit_sha is None


def test_every_scenario_has_brief_fixture():
    scenarios = {p.stem for p in (FIXTURES.parent / "github").glob("*.json")}
    briefs = {p.name.split(".")[1] for p in FIXTURES.glob("brief_composition.*.json")}
    missing = scenarios - briefs
    assert not missing, f"scenarios without a brief_composition fixture: {missing}"


def test_every_ranking_fixture_names_a_scored_candidate():
    """A verdict for a sha the scorer never surfaced is dropped at runtime (spec §6.3),
    so a fixture that only names such shas would demo an empty candidate list."""
    github = FIXTURES.parent / "github"
    for path in sorted(FIXTURES.glob("commit_ranking.*.json")):
        scenario = path.name.split(".")[1]
        fixture = github / f"{scenario}.json"
        if not fixture.exists():  # commit_ranking.default.json
            continue
        shas = {c["sha"] for c in json.loads(fixture.read_text(encoding="utf-8"))["commits"]}
        analysis = CommitAnalysis.model_validate(json.loads(path.read_text(encoding="utf-8")))
        assert analysis.verdicts, f"{path.name}: no verdicts"
        assert {v.sha for v in analysis.verdicts} <= shas, f"{path.name}: invented shas"
        if analysis.likely_culprit_sha:
            assert analysis.likely_culprit_sha in shas, f"{path.name}: culprit not in the repo"


def test_ambiguous_latency_recording_names_a_false_culprit():
    """A measured failure, pinned so a re-record that changes it is noticed.

    The scenario plants no culprit. Recorded live (scripts/eval_ranking.py), the model
    names one above the confidence floor in every run, so the demo brief blames a commit.
    If a re-record flips this, the README's evaluation numbers change with it. The guard
    itself is tested on synthetic input in test_triage_guards.py.
    """
    assert planted_culprit("ambiguous_latency") is None
    path = FIXTURES / "commit_ranking.ambiguous_latency.json"
    analysis = CommitAnalysis.model_validate(json.loads(path.read_text(encoding="utf-8")))
    named = analysis.likely_culprit_sha
    assert named is not None
    assert max(v.confidence for v in analysis.verdicts if v.sha == named) >= get_settings().confidence_floor
