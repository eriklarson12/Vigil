"""Kill the triage graph mid-flight, then prove the resume is exactly-once (roadmap R11).

ADR-006 claims a container killed mid-incident resumes without re-posting the brief and
without re-spending LLM calls on completed nodes. test_full_graph.py only fires
/internal/resume *after* a run finishes, which exercises the "already complete" branch of
Runner.run_triage; the resume branch and the stale-claim reclaim in CLAIM_SQL were untested.

The kill point is deterministic: rank_commits_llm is alone in its superstep, so the graph is
parked on an asyncio.Event inside the node body with no DB connection and no checkpoint write
in flight. CancelledError is a BaseException, so `degrading`'s `except Exception` does not
swallow it and the cancel propagates exactly as a real kill would.

Needs Postgres:  docker compose up -d db && uv run pytest -m integration
"""

import asyncio
import json
import pathlib
from datetime import UTC, datetime
from typing import Any

import pytest

from vigil.ingest.queue import claim_next

pytestmark = pytest.mark.integration

ROOT = pathlib.Path(__file__).parent.parent.parent
SCENARIO = json.loads((ROOT / "simulator" / "scenarios" / "bad_deploy.json").read_text())
TOKEN = {"Authorization": "Bearer dev-token"}


def _payload(starts_at: datetime, fingerprint: str) -> dict:
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
                "fingerprint": fingerprint,
            }
        ],
    }


class _GateLLM:
    """Wraps the FakeLLMClient: parks the FIRST commit_ranking call until released.

    `calls` records every call_type in order, including the parked one that never
    returned — that is what separates "the fake was entered twice" from "the incident
    was charged for two calls".
    """

    def __init__(self, inner: Any):
        self._inner = inner
        self._gated = True
        self.calls: list[str] = []
        self.parked = asyncio.Event()
        self.release = asyncio.Event()

    async def generate_structured(self, system: str, user: str, schema: Any, call_type: str) -> Any:
        self.calls.append(call_type)
        if call_type == "commit_ranking" and self._gated:
            self._gated = False
            self.parked.set()
            await self.release.wait()
        return await self._inner.generate_structured(system, user, schema, call_type)


async def _plant_deploys(deps) -> None:
    for d in SCENARIO.get("deploys", []):
        async with deps.pool.connection() as conn:
            await conn.execute(
                "INSERT INTO deploy_events (service, commit_shas, finished_at)"
                " VALUES (%s, %s, now() - make_interval(mins => %s))",
                (d["service"], d["commit_shas"], d["minutes_before_alert"]),
            )


async def _alert_row(deps, alert_id: str) -> dict[str, Any]:
    async with deps.pool.connection() as conn:
        cur = await conn.execute(
            "SELECT processing_status, incident_id FROM alerts WHERE id = %s", (alert_id,)
        )
        row = await cur.fetchone()
    return {"status": row[0], "incident_id": str(row[1])}


async def _summary(c, incident_id: str) -> dict[str, Any]:
    """Everything about a finished incident except ids and wall-clock times."""
    detail = (await c.get(f"/api/incidents/{incident_id}")).json()
    final = next(e for e in detail["events"] if e["event_type"] == "triage_finalized")
    return {
        "events": [e["event_type"] for e in detail["events"]],
        "severity": detail["incident"]["severity"],
        "status": detail["incident"]["status"],
        "candidates": [
            (cc["sha"], cc["llm_rank"], cc["llm_confidence"], cc["feature_scores"])
            for cc in detail["commit_candidates"]
        ],
        "finalized": {k: final["payload"][k] for k in ("scored", "culprit", "llm_calls", "errors")},
    }


async def test_kill_mid_rank_resumes_exactly_once(client):
    c, deps = client
    runner = deps.runner
    gate = _GateLLM(deps.llm)
    deps.llm = gate  # triage nodes read deps.llm at call time, so no graph rebuild
    runner.kick = lambda: None  # the test owns the drain task; no racing inline-after-ACK

    now = datetime.now(UTC)
    await _plant_deploys(deps)
    resp = await c.post("/webhooks/alertmanager", json=_payload(now, "kill-run"), headers=TOKEN)
    assert resp.json()["queued"] == 1

    row = await claim_next(deps.pool, deps.settings.stale_claim_minutes)
    assert row is not None
    incident_id = row["incident_id"]
    task = asyncio.create_task(runner.run_triage(row))

    # -- kill the graph while it is parked inside rank_commits_llm --------------
    await asyncio.wait_for(gate.parked.wait(), timeout=60)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    # the kill landed where this test claims it did; without this a refactor that
    # moves the park point would leave the test passing vacuously
    config = {"configurable": {"thread_id": f"triage:{incident_id}"}}
    snapshot = await runner._triage.aget_state(config)
    assert snapshot.next == ("rank_commits_llm",)

    detail = (await c.get(f"/api/incidents/{incident_id}")).json()
    assert [e for e in detail["events"] if e["event_type"] == "brief_posted"] == []
    assert (await _alert_row(deps, row["id"]))["status"] == "claimed"

    # -- restart: the stale claim is reclaimed and the graph resumes ------------
    gate.release.set()
    async with deps.pool.connection() as conn:
        await conn.execute(
            "UPDATE alerts SET claimed_at = now() - make_interval(mins => %s) WHERE id = %s",
            (deps.settings.stale_claim_minutes + 1, row["id"]),
        )
    assert (await runner.resume_tick())["alerts_drained"] == 1

    # ranking re-ran because it never completed; everything past the barrier join ran once.
    # Order also guards the list-form join in triage.py: separate add_edge calls would let
    # compose_brief fire before ranking.
    assert gate.calls == ["commit_ranking", "commit_ranking", "brief_composition"]

    killed = await _summary(c, incident_id)
    assert killed["events"].count("brief_posted") == 1
    assert killed["events"].count("triage_finalized") == 1
    # llm_calls_used is an operator.add reducer: the abandoned call never committed a
    # write, so the incident is charged 2 even though the fake was entered 3 times.
    assert killed["finalized"]["llm_calls"] == 2
    assert killed["finalized"]["errors"] == {}
    assert (await _alert_row(deps, row["id"]))["status"] == "processed"

    # -- control: the same alert, never killed, must land in the same state ----
    # attach_incident Rule 2 folds a second checkout alert into the still-open first
    # incident, so close that one first. Nothing below asserts on this status.
    async with deps.pool.connection() as conn:
        await conn.execute(
            "UPDATE incidents SET status = 'postmortem_done' WHERE id = %s", (incident_id,)
        )
    resp = await c.post("/webhooks/alertmanager", json=_payload(now, "control-run"), headers=TOKEN)
    assert resp.json()["queued"] == 1
    control = await claim_next(deps.pool, deps.settings.stale_claim_minutes)
    assert control is not None and control["incident_id"] != incident_id
    await runner.run_triage(control)

    assert await _summary(c, control["incident_id"]) == killed

    # Leave nothing open behind: the local db is shared with `vigil-sim demo`, and an
    # open checkout incident inside the grouping window swallows the demo's alert by
    # Rule 2 — the demo then waits forever for a postmortem on an incident whose other
    # alerts never resolved. Not asserted on; hygiene only.
    async with deps.pool.connection() as conn:
        await conn.execute(
            "UPDATE incidents SET status = 'postmortem_done' WHERE id = %s",
            (control["incident_id"],),
        )
