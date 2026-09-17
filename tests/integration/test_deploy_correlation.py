"""Deploy correlation end to end (roadmap R6). Needs Postgres:
    docker compose up -d db
    uv run pytest -m integration

Unlike test_full_graph, nothing is pre-planted here: the deploy_events rows under test
are the ones the fetch_commits node wrote from the GitHub fixture's `deployments` array.
"""

import asyncio
import json
import pathlib
from datetime import UTC, datetime

import pytest
from psycopg.rows import dict_row

from tests.conftest import planted_culprit

pytestmark = pytest.mark.integration

ROOT = pathlib.Path(__file__).parent.parent.parent
SCENARIO = json.loads((ROOT / "simulator" / "scenarios" / "bad_deploy.json").read_text())
FIXTURE = json.loads((ROOT / "tests" / "fixtures" / "github" / "bad_deploy.json").read_text())
TOKEN = {"Authorization": "Bearer dev-token"}
SERVICES = ["auth", "checkout", "inventory", "orders", "payments-db"]


def _payload(starts_at: datetime, suffix: str) -> dict:
    alert = SCENARIO["alert"]
    return {
        "version": "4",
        "status": "firing",
        "alerts": [
            {
                "status": "firing",
                "labels": alert["labels"],
                "annotations": alert["annotations"],
                "startsAt": starts_at.isoformat(),
                "endsAt": "0001-01-01T00:00:00Z",
                "fingerprint": f"{alert['fingerprint']}-{suffix}",
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


async def _deploy_rows(deps) -> list[dict]:
    async with deps.pool.connection() as conn:
        async with conn.cursor(row_factory=dict_row) as cur:
            await cur.execute("SELECT service, commit_shas, finished_at FROM deploy_events")
            return await cur.fetchall()


async def test_deployments_land_and_reach_the_scorer(client):
    c, deps = client
    now = datetime.now(UTC)
    shas = [d["sha"] for d in FIXTURE["deployments"]]
    assert len(shas) == 2  # the fixture this test reasons about

    resp = await c.post("/webhooks/alertmanager", json=_payload(now, "r6"), headers=TOKEN)
    assert resp.json()["queued"] == 1

    async def brief_posted():
        incidents = (await c.get("/api/incidents")).json()
        inc = next((i for i in incidents if i["service"] == "checkout"), None)
        return inc if inc and inc.get("slack_message_ts") else None

    incident = await _wait(brief_posted)

    # one row per (service sharing the demo repo, deployed sha) — nothing was pre-planted
    rows = await _deploy_rows(deps)
    assert {(r["service"], tuple(r["commit_shas"])) for r in rows} == {
        (service, (sha,)) for service in SERVICES for sha in shas
    }
    assert len(rows) == 10

    # the payoff: those rows reached the scorer
    detail = (await c.get(f"/api/incidents/{incident['id']}")).json()
    by_sha = {cc["sha"]: cc for cc in detail["commit_candidates"]}
    assert by_sha[planted_culprit("bad_deploy")]["feature_scores"]["f_deploy"] == 1.0
    assert all(by_sha[sha]["feature_scores"]["f_deploy"] == 1.0 for sha in shas)

    # a deployments failure would degrade separately; neither key may be present
    finalized = [e for e in detail["events"] if e["event_type"] == "triage_finalized"]
    assert finalized and not (finalized[0]["payload"].get("errors") or {})

    # A second triage at the same starts_at recomputes identical finished_at values, so the
    # upsert no-ops instead of doubling the rows. That determinism is the whole reason
    # _record_deployments anchors on the alert rather than now(), and it is what lets a
    # resumed graph replay fetch_commits safely (see test_kill_resume).
    # The first incident has to be closed first, or grouping folds the new alert into it.
    async with deps.pool.connection() as conn:
        await conn.execute(
            "UPDATE incidents SET status = 'postmortem_done' WHERE id = %s", (incident["id"],)
        )
    resp = await c.post("/webhooks/alertmanager", json=_payload(now, "r6-again"), headers=TOKEN)
    assert resp.json()["queued"] == 1

    async def second_brief():
        incidents = (await c.get("/api/incidents")).json()
        others = [i for i in incidents if i["id"] != incident["id"] and i.get("slack_message_ts")]
        return others[0] if others else None

    second = await _wait(second_brief)
    assert len(await _deploy_rows(deps)) == 10

    # Leave nothing open behind: the local db is shared with `vigil-sim demo`, and an open
    # checkout incident inside the grouping window swallows the demo's alert. Hygiene only.
    async with deps.pool.connection() as conn:
        await conn.execute(
            "UPDATE incidents SET status = 'postmortem_done' WHERE id = %s", (second["id"],)
        )
