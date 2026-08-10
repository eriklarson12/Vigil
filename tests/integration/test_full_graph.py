"""Full-pipeline integration test (spec §16). Needs Postgres:
    docker compose up -d db
    uv run pytest -m integration

Uses FakeLLM/FakeEmbedder/mock Slack via conftest env — no network calls.
"""

import asyncio
import json
import pathlib
from datetime import UTC, datetime

import httpx
import pytest
from asgi_lifespan import LifespanManager

pytestmark = pytest.mark.integration

ROOT = pathlib.Path(__file__).parent.parent.parent
SCENARIO = json.loads((ROOT / "simulator" / "scenarios" / "bad_deploy.json").read_text())
TOKEN = {"Authorization": "Bearer dev-token"}


def _payload(status: str, starts_at: datetime) -> dict:
    alert = SCENARIO["alert"]
    return {
        "version": "4",
        "status": status,
        "alerts": [
            {
                "status": status,
                "labels": alert["labels"],
                "annotations": alert["annotations"],
                "startsAt": starts_at.isoformat(),
                "endsAt": starts_at.isoformat() if status == "resolved" else "0001-01-01T00:00:00Z",
                "fingerprint": alert["fingerprint"] + f"-{starts_at.timestamp()}",
            }
        ],
    }


async def _wait(check, timeout=60.0):
    deadline = asyncio.get_event_loop().time() + timeout
    while asyncio.get_event_loop().time() < deadline:
        result = await check()
        if result:
            return result
        await asyncio.sleep(0.5)
    raise AssertionError("condition not met in time")


@pytest.fixture()
async def client():
    from vigil.main import create_app
    from vigil.rag.embed import get_embedder, ingest_runbook

    app = create_app()
    async with LifespanManager(app, startup_timeout=60):
        deps = app.state.deps
        # clean slate: previous runs leave incidents/checkpoints behind
        async with deps.pool.connection() as conn:
            await conn.execute(
                "TRUNCATE alerts, incidents, incident_events, commit_candidates,"
                " deploy_events, postmortems, runbooks, runbook_chunks, llm_budget CASCADE"
            )
            for t in ("checkpoints", "checkpoint_blobs", "checkpoint_writes"):
                await conn.execute(f"TRUNCATE {t}")
        # seed runbooks + plant the scenario deploy relative to "now"
        for path in sorted((ROOT / "simulator" / "runbooks").glob("*.md")):
            await ingest_runbook(deps.pool, get_embedder(deps.settings), path)
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
            yield c, deps


async def test_fire_brief_resolve_postmortem(client):
    c, deps = client
    now = datetime.now(UTC)
    for d in SCENARIO.get("deploys", []):
        async with deps.pool.connection() as conn:
            await conn.execute(
                "INSERT INTO deploy_events (service, commit_shas, finished_at)"
                " VALUES (%s, %s, now() - make_interval(mins => %s))",
                (d["service"], d["commit_shas"], d["minutes_before_alert"]),
            )

    resp = await c.post("/webhooks/alertmanager", json=_payload("firing", now), headers=TOKEN)
    assert resp.status_code == 200
    assert resp.json()["queued"] == 1

    # duplicate delivery is a no-op
    resp = await c.post("/webhooks/alertmanager", json=_payload("firing", now), headers=TOKEN)
    assert resp.json()["duplicates"] == 1

    async def brief_posted():
        incidents = (await c.get("/api/incidents")).json()
        inc = next((i for i in incidents if i["service"] == "checkout"), None)
        return inc if inc and inc.get("slack_message_ts") else None

    incident = await _wait(brief_posted)
    assert incident["severity"] == "SEV1"

    detail = (await c.get(f"/api/incidents/{incident['id']}")).json()
    culprits = [cc for cc in detail["commit_candidates"] if cc["llm_rank"] == 1]
    assert culprits and culprits[0]["sha"] == "a1b2c3d4e5"
    briefs = [e for e in detail["events"] if e["event_type"] == "brief_posted"]
    assert len(briefs) == 1

    # resume tick is idempotent: no double-posting after completion
    resp = await c.post("/internal/resume", headers=TOKEN)
    assert resp.status_code == 200
    detail = (await c.get(f"/api/incidents/{incident['id']}")).json()
    assert len([e for e in detail["events"] if e["event_type"] == "brief_posted"]) == 1

    # resolve via the alertmanager path -> postmortem generates
    resp = await c.post("/webhooks/alertmanager", json=_payload("resolved", now), headers=TOKEN)
    assert resp.status_code == 200

    async def postmortem_ready():
        d = (await c.get(f"/api/incidents/{incident['id']}")).json()
        return d if d.get("postmortem") else None

    detail = await _wait(postmortem_ready)
    assert "Postmortem" in detail["postmortem"]["markdown"]
    assert detail["incident"]["status"] == "postmortem_done"

    # double-resolve is a no-op
    resp = await c.post(f"/api/incidents/{incident['id']}/resolve")
    assert resp.json()["resolved"] is False


async def test_retrieval_hits_expected_runbook(client):
    c, deps = client
    from vigil.rag.retrieve import hybrid_search

    expected = {
        "bad_deploy": "checkout-service",
        "db_migration_lock": "orders-database",
        "cert_expiry": "tls-certificates",
    }
    hits = 0
    for name, slug in expected.items():
        scenario = json.loads((ROOT / "simulator" / "scenarios" / f"{name}.json").read_text())
        alert = {"labels": scenario["alert"]["labels"], "annotations": scenario["alert"]["annotations"]}
        chunks = await hybrid_search(deps.pool, deps.embedder, alert)
        if any(ch["runbook_slug"] == slug for ch in chunks[:3]):
            hits += 1
    assert hits >= 2, f"hit@3 too low with FakeEmbedder: {hits}/3"
