"""R5 revert-PR click, end to end in ROLLBACK_MODE=mock. Needs Postgres:
    docker compose up -d db
    uv run pytest -m integration
"""

import asyncio
import json
import pathlib
from datetime import UTC, datetime
from urllib.parse import urlencode

import pytest

from tests.integration.slack_signing import SIGNING_SECRET, sign_slack_request

pytestmark = pytest.mark.integration

ROOT = pathlib.Path(__file__).parent.parent.parent
TOKEN = {"Authorization": "Bearer dev-token"}
RESPONSE_URL = "https://hooks.slack.com/actions/T000/1/abc"


def _click_body(incident_id: str) -> str:
    payload = {
        "type": "block_actions",
        "response_url": RESPONSE_URL,
        "actions": [{"action_id": "propose_revert", "value": incident_id}],
    }
    return urlencode({"payload": json.dumps(payload)})


async def _click(c, incident_id: str, secret: str = SIGNING_SECRET):
    body = _click_body(incident_id)
    return await c.post("/slack/interactions", content=body, headers=sign_slack_request(body, secret))


async def _wait(check, timeout=60.0):
    deadline = asyncio.get_event_loop().time() + timeout
    while asyncio.get_event_loop().time() < deadline:
        result = await check()
        if result:
            return result
        await asyncio.sleep(0.5)
    raise AssertionError("condition not met in time")


async def _fire(c, deps, scenario_name: str) -> str:
    scenario = json.loads((ROOT / "simulator" / "scenarios" / f"{scenario_name}.json").read_text())
    now = datetime.now(UTC)
    for d in scenario.get("deploys", []):
        async with deps.pool.connection() as conn:
            await conn.execute(
                "INSERT INTO deploy_events (service, commit_shas, finished_at)"
                " VALUES (%s, %s, now() - make_interval(mins => %s))",
                (d["service"], d["commit_shas"], d["minutes_before_alert"]),
            )
    alert = scenario["alert"]
    resp = await c.post(
        "/webhooks/alertmanager",
        json={
            "version": "4",
            "status": "firing",
            "alerts": [
                {
                    "status": "firing",
                    "labels": alert["labels"],
                    "annotations": alert["annotations"],
                    "startsAt": now.isoformat(),
                    "endsAt": "0001-01-01T00:00:00Z",
                    "fingerprint": alert["fingerprint"] + f"-{now.timestamp()}",
                }
            ],
        },
        headers=TOKEN,
    )
    assert resp.status_code == 200

    async def triage_finalized():
        async with deps.pool.connection() as conn:
            cur = await conn.execute(
                "SELECT incident_id FROM incident_events WHERE event_type = 'triage_finalized'"
            )
            row = await cur.fetchone()
        return str(row[0]) if row else None

    return await _wait(triage_finalized)


async def _events(deps, incident_id: str) -> list[tuple[str, dict]]:
    async with deps.pool.connection() as conn:
        cur = await conn.execute(
            "SELECT event_type, payload FROM incident_events WHERE incident_id = %s ORDER BY id",
            (incident_id,),
        )
        return await cur.fetchall()


async def _state(deps, incident_id: str) -> tuple[str | None, str | None]:
    async with deps.pool.connection() as conn:
        cur = await conn.execute(
            "SELECT revert_pr_state, revert_pr_url FROM incidents WHERE id = %s", (incident_id,)
        )
        return await cur.fetchone()


async def test_click_proposes_mock_revert_once(client):
    c, deps = client
    incident_id = await _fire(c, deps, "bad_deploy")

    brief = next(p for t, p in await _events(deps, incident_id) if t == "brief_posted")
    assert "propose_revert" in json.dumps(brief["slack_payload"])

    resp = await _click(c, incident_id)
    assert resp.status_code == 200
    assert resp.json()["text"] == "Proposing a revert PR."

    async def proposed():
        state = await _state(deps, incident_id)
        return state if state[0] == "proposed" else None

    state, url = await _wait(proposed)
    assert url.startswith("mock://github.com/eriklarson12/vigil-demo-shop/tree/vigil/revert-")

    types = [t for t, _ in await _events(deps, incident_id)]
    assert types.count("revert_pr_requested") == 1
    assert types.count("revert_pr_proposed") == 1
    response = next(p for t, p in await _events(deps, incident_id) if t == "slack_response_posted")
    assert "proposed" in response["slack_payload"]["text"]

    again = await _click(c, incident_id)
    assert again.json()["text"] == "A revert PR is already requested or proposed."
    types = [t for t, _ in await _events(deps, incident_id)]
    assert types.count("revert_pr_requested") == 1


async def test_click_refused_when_gate_fails(client):
    c, deps = client
    incident_id = await _fire(c, deps, "config_typo")

    resp = await _click(c, incident_id)
    assert resp.json()["text"] == "This incident's culprit does not qualify for a revert PR."
    assert await _state(deps, incident_id) == (None, None)


async def test_click_with_bad_signature_is_rejected(client):
    c, deps = client
    resp = await _click(c, "0b6f2a4e-0000-4000-8000-000000000001", secret="wrong")
    assert resp.status_code == 401


async def test_resume_tick_finishes_a_stranded_request(client):
    c, deps = client
    incident_id = await _fire(c, deps, "bad_deploy")
    # A click whose background task died with the container: claimed, never finished.
    async with deps.pool.connection() as conn:
        await conn.execute(
            "UPDATE incidents SET revert_pr_state = 'requested' WHERE id = %s", (incident_id,)
        )
        await conn.execute(
            "INSERT INTO incident_events (incident_id, event_type, payload, created_at)"
            " VALUES (%s, 'revert_pr_requested', '{}', now() - interval '1 hour')",
            (incident_id,),
        )

    resp = await c.post("/internal/resume", headers=TOKEN)
    assert resp.status_code == 200
    assert resp.json()["reverts_resumed"] == 1
    state, url = await _state(deps, incident_id)
    assert state == "proposed" and url.startswith("mock://")

    again = await c.post("/internal/resume", headers=TOKEN)
    assert again.json()["reverts_resumed"] == 0


async def test_live_mode_without_write_token_fails_cleanly(client):
    c, deps = client
    incident_id = await _fire(c, deps, "bad_deploy")
    deps.settings.rollback_mode = "live"
    try:
        await _click(c, incident_id)

        async def settled():
            state = await _state(deps, incident_id)
            return state if state[0] in ("proposed", "failed") else None

        state, url = await _wait(settled)
    finally:
        deps.settings.rollback_mode = "mock"
    assert (state, url) == ("failed", None)
    failed = next(p for t, p in await _events(deps, incident_id) if t == "revert_pr_failed")
    assert failed["error"] == "GITHUB_WRITE_TOKEN is not set"

    # failed is retryable: the next click claims again
    retry = await _click(c, incident_id)
    assert retry.json()["text"] == "Proposing a revert PR."
