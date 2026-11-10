"""A Slack outage at post time must stall the run, not finish it without a brief.

If post_slack degraded like the other nodes, the graph would reach END, the alert would be
marked processed, and every later claim would take the "already complete" branch of
Runner.run_triage. The brief would be lost for good after a few seconds of Slack errors.

Needs Postgres:  docker compose up -d db && uv run pytest -m integration
"""

import json
import pathlib
import types
from datetime import UTC, datetime
from typing import Any

import httpx
import pytest

from vigil.graph import nodes_util

pytestmark = pytest.mark.integration

ROOT = pathlib.Path(__file__).parent.parent.parent
SCENARIO = json.loads((ROOT / "simulator" / "scenarios" / "bad_deploy.json").read_text())
TOKEN = {"Authorization": "Bearer dev-token"}


class _CountingLLM:
    def __init__(self, inner: Any):
        self._inner = inner
        self.calls: list[str] = []

    async def generate_structured(
        self, system: str, user: str, schema: Any, call_type: str, fixture_key: str | None = None
    ) -> Any:
        self.calls.append(call_type)
        return await self._inner.generate_structured(
            system, user, schema, call_type, fixture_key=fixture_key
        )


async def _events(c, incident_id: str) -> list[str]:
    detail = (await c.get(f"/api/incidents/{incident_id}")).json()
    return [e["event_type"] for e in detail["events"]]


async def test_slack_outage_stalls_the_run_and_the_reclaim_posts_once(client, monkeypatch):
    c, deps = client
    runner = deps.runner
    runner.kick = lambda: None  # the test owns the drain

    async def no_sleep(_seconds: float) -> None:
        return None

    monkeypatch.setattr(nodes_util, "asyncio", types.SimpleNamespace(sleep=no_sleep))
    llm = _CountingLLM(deps.llm)
    deps.llm = llm

    real_post = deps.slack.post_brief
    attempts = 0

    async def slack_down(incident_id: str, payload: dict[str, Any]) -> str:
        nonlocal attempts
        attempts += 1
        raise httpx.ConnectError("slack unreachable")

    deps.slack.post_brief = slack_down

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
                "fingerprint": "brief-retry",
            }
        ],
    }
    resp = await c.post("/webhooks/alertmanager", json=payload, headers=TOKEN)
    assert resp.json()["queued"] == 1
    await runner._drain()

    async with deps.pool.connection() as conn:
        cur = await conn.execute("SELECT id, processing_status, incident_id FROM alerts")
        alert_id, status, incident_id = await cur.fetchone()
    incident_id = str(incident_id)

    # -- outage: every retry spent, nothing posted, the run still owes post_slack ----
    assert attempts == 4
    assert status == "claimed"
    config = {"configurable": {"thread_id": f"triage:{incident_id}"}}
    assert (await runner._triage.aget_state(config)).next == ("post_slack",)
    events = await _events(c, incident_id)
    assert "brief_posted" not in events and "triage_finalized" not in events

    # -- Slack is back: the stale claim is reclaimed and only post_slack re-runs -----
    deps.slack.post_brief = real_post
    async with deps.pool.connection() as conn:
        await conn.execute(
            "UPDATE alerts SET claimed_at = now() - make_interval(mins => %s) WHERE id = %s",
            (deps.settings.stale_claim_minutes + 1, alert_id),
        )
    assert (await runner.resume_tick())["alerts_drained"] == 1

    events = await _events(c, incident_id)
    assert events.count("brief_posted") == 1
    assert events.count("triage_finalized") == 1
    assert llm.calls == ["commit_ranking", "brief_composition"]  # the retry spent no model call
    async with deps.pool.connection() as conn:
        cur = await conn.execute(
            "SELECT a.processing_status, i.slack_message_ts FROM alerts a"
            " JOIN incidents i ON i.id = a.incident_id WHERE a.id = %s",
            (alert_id,),
        )
        assert await cur.fetchone() == ("processed", f"mock-{incident_id}")

    # An open checkout incident would swallow the next local demo alert by grouping.
    async with deps.pool.connection() as conn:
        await conn.execute(
            "UPDATE incidents SET status = 'postmortem_done' WHERE id = %s", (incident_id,)
        )
