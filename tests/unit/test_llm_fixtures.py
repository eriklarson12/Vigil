"""Every recorded fixture must validate through the REAL Pydantic schemas —
schema drift breaks loudly here (spec §16)."""

import json
import pathlib

import pytest

from vigil.commits.schemas import CommitAnalysis
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


async def test_fake_client_resolves_scenario_from_prompt():
    client = FakeLLMClient(FIXTURES)
    result = await client.generate_structured(
        "system", "## Alert\nscenario: bad_deploy\n…", CommitAnalysis, "commit_ranking"
    )
    assert result.likely_culprit_sha == "a1b2c3d4e5"


async def test_fake_client_falls_back_to_default():
    client = FakeLLMClient(FIXTURES)
    result = await client.generate_structured(
        "system", "scenario: something_unknown", CommitAnalysis, "commit_ranking"
    )
    assert result.likely_culprit_sha is None


def test_every_scenario_has_brief_fixture():
    scenarios = {p.stem for p in (FIXTURES.parent / "github").glob("*.json")}
    briefs = {p.name.split(".")[1] for p in FIXTURES.glob("brief_composition.*.json")}
    missing = scenarios - briefs
    assert not missing, f"scenarios without a brief_composition fixture: {missing}"
