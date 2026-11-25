"""`annotations.runbook_url` pins its runbook through the real SQL (roadmap R20).

Needs Postgres:  docker compose up -d db && uv run pytest -m integration
"""

import json
import pathlib

import pytest

from vigil.rag.retrieve import hybrid_search

pytestmark = pytest.mark.integration

ROOT = pathlib.Path(__file__).parent.parent.parent


def _alert(scenario: str, **annotations) -> dict:
    alert = json.loads((ROOT / "simulator" / "scenarios" / f"{scenario}.json").read_text())["alert"]
    alert["annotations"] = {k: v for k, v in alert["annotations"].items() if k != "runbook_url"}
    alert["annotations"].update(annotations)
    return {"labels": alert["labels"], "annotations": alert["annotations"]}


async def test_a_known_slug_is_pinned_first(client):
    _, deps = client
    alert = _alert("bad_deploy", runbook_url="https://runbooks.example.com/tls-certificates")
    chunks = await hybrid_search(deps.pool, deps.embedder, alert)
    assert chunks[0]["runbook_slug"] == "tls-certificates"
    assert chunks[0]["source"] == "annotation"
    assert all(c["source"] == "retrieval" for c in chunks[1:])
    assert len({c["runbook_slug"] for c in chunks}) <= 2


@pytest.mark.parametrize("url", ["https://runbooks.example.com/no-such-runbook", "", None])
async def test_a_missing_or_unknown_url_changes_nothing(client, url):
    _, deps = client
    baseline = await hybrid_search(deps.pool, deps.embedder, _alert("bad_deploy"))
    annotations = {} if url is None else {"runbook_url": url}
    chunks = await hybrid_search(deps.pool, deps.embedder, _alert("bad_deploy", **annotations))
    assert chunks == baseline
    assert {c["source"] for c in chunks} == {"retrieval"}


async def test_the_cert_expiry_scenario_carries_its_runbook_link(client):
    _, deps = client
    scenario = json.loads((ROOT / "simulator" / "scenarios" / "cert_expiry.json").read_text())
    alert = {"labels": scenario["alert"]["labels"], "annotations": scenario["alert"]["annotations"]}
    chunks = await hybrid_search(deps.pool, deps.embedder, alert)
    assert (chunks[0]["runbook_slug"], chunks[0]["source"]) == ("tls-certificates", "annotation")
