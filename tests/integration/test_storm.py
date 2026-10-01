"""Alert storms coalesce into one incident, one brief, one triage's LLM spend (roadmap R12).

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
STORM = 50


class _GateLLM:
    """Records every call_type; parks the first commit_ranking until released when gated."""

    def __init__(self, inner: Any, gated: bool):
        self._inner = inner
        self._gated = gated
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


async def _post_alert(c, fingerprint: str) -> None:
    alert = SCENARIO["alert"]
    payload = {
        "version": "4",
        "status": "firing",
        "alerts": [
            {
                "status": "firing",
                "labels": alert["labels"],
                "annotations": alert["annotations"],
                "startsAt": datetime.now(UTC).isoformat(),
                "endsAt": "0001-01-01T00:00:00Z",
                "fingerprint": fingerprint,
            }
        ],
    }
    resp = await c.post("/webhooks/alertmanager", json=payload, headers=TOKEN)
    assert resp.json()["queued"] == 1


async def _scalar(deps, sql: str, *params) -> Any:
    async with deps.pool.connection() as conn:
        cur = await conn.execute(sql, params)
        return (await cur.fetchone())[0]


async def _close_all(deps) -> None:
    # Hygiene: an open checkout incident on the shared local db swallows the next
    # `vigil-sim demo` alert by grouping Rule 2 (see test_kill_resume.py).
    async with deps.pool.connection() as conn:
        await conn.execute("UPDATE incidents SET status = 'postmortem_done' WHERE status = 'open'")


async def test_storm_of_50_alerts_is_one_incident_one_brief(client):
    c, deps = client
    gate = _GateLLM(deps.llm, gated=False)
    deps.llm = gate

    for i in range(STORM):
        await _post_alert(c, f"storm-{i}")

    deadline = asyncio.get_event_loop().time() + 120
    while await _scalar(deps, "SELECT count(*) FROM alerts WHERE processing_status = 'processed'") < STORM:
        assert asyncio.get_event_loop().time() < deadline, "storm not drained in time"
        await asyncio.sleep(0.5)

    assert await _scalar(deps, "SELECT count(*) FROM incidents") == 1
    assert await _scalar(
        deps, "SELECT count(*) FROM incident_events WHERE event_type = 'brief_posted'"
    ) == 1
    assert gate.calls == ["commit_ranking", "brief_composition"]
    await _close_all(deps)


async def test_drain_and_tick_cannot_triage_one_incident_twice(client):
    """Two loops, two alerts, one incident: the second run waits, then finds triage done.

    Without the per-incident lock in Runner.run_triage, the second run reads the parked
    thread's checkpoint, resumes it concurrently, and the incident is charged two ranking
    calls and two brief compositions.
    """
    c, deps = client
    runner = deps.runner
    gate = _GateLLM(deps.llm, gated=True)
    deps.llm = gate
    runner.kick = lambda: None  # the test plays both loops

    await _post_alert(c, "race-1")
    await _post_alert(c, "race-2")
    first = await claim_next(deps.pool, deps.settings.stale_claim_minutes)
    second = await claim_next(deps.pool, deps.settings.stale_claim_minutes)
    assert first["incident_id"] == second["incident_id"]

    drain = asyncio.create_task(runner.run_triage(first))
    await asyncio.wait_for(gate.parked.wait(), timeout=60)
    tick = asyncio.create_task(runner.run_triage(second))
    await asyncio.sleep(0.3)
    assert not tick.done(), "the second run must block on the incident lock"

    gate.release.set()
    await asyncio.wait_for(asyncio.gather(drain, tick), timeout=60)

    assert gate.calls == ["commit_ranking", "brief_composition"]
    assert await _scalar(
        deps,
        "SELECT count(*) FROM incident_events WHERE event_type = 'brief_posted' AND incident_id = %s",
        first["incident_id"],
    ) == 1
    await _close_all(deps)
