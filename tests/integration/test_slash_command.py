"""POST /slack/commands — the `/vigil` slash command (roadmap R10).

Incidents are seeded with direct INSERTs: the route reads Postgres and funnels
into resolve_incident, so the triage graph is not under test here.
"""

import time
from urllib.parse import urlencode

import pytest

from tests.integration.slack_signing import sign_slack_request
from vigil.slack.blocks import SLASH_HELP

pytestmark = pytest.mark.integration


async def _command(c, text: str, secret: str | None = None, timestamp: int | None = None):
    body = urlencode({"command": "/vigil", "text": text, "user_id": "U000"})
    kwargs = {"timestamp": timestamp} | ({"secret": secret} if secret else {})
    return await c.post("/slack/commands", content=body, headers=sign_slack_request(body, **kwargs))


async def _seed(pool, title: str = "HighErrorRate on checkout", status: str = "open") -> str:
    async with pool.connection() as conn:
        cur = await conn.execute(
            "INSERT INTO incidents (service, title, severity, status)"
            " VALUES ('checkout', %s, 'SEV1', %s) RETURNING id",
            (title, status),
        )
        (incident_id,) = await cur.fetchone()
    return str(incident_id)


async def test_status_with_no_open_incidents(client):
    c, deps = client
    await _seed(deps.pool, status="resolved")
    resp = await _command(c, "status")
    assert resp.status_code == 200
    assert resp.json() == {"response_type": "ephemeral", "text": "No open incidents."}


async def test_status_lists_open_incidents_with_links(client):
    c, deps = client
    incident_id = await _seed(deps.pool)
    body = (await _command(c, "status")).json()
    assert body["response_type"] == "ephemeral"
    assert body["text"].startswith("*1 open incident*")
    link = f"<{deps.settings.dashboard_url}/incidents/{incident_id}|HighErrorRate on checkout>"
    assert link in body["text"]


async def test_empty_text_is_status(client):
    c, deps = client
    await _seed(deps.pool)
    assert (await _command(c, "")).json()["text"].startswith("*1 open incident*")


async def test_resolve_funnels_into_resolve_incident(client, monkeypatch):
    c, deps = client
    kicked: list[str] = []
    monkeypatch.setattr(deps.runner, "kick_postmortem", kicked.append)
    incident_id = await _seed(deps.pool)

    resp = await _command(c, f"RESOLVE {incident_id}")
    assert resp.json()["text"] == "Resolving — postmortem incoming."
    assert kicked == [incident_id]
    async with deps.pool.connection() as conn:
        cur = await conn.execute(
            "SELECT status, resolution_source FROM incidents WHERE id = %s", (incident_id,)
        )
        assert await cur.fetchone() == ("resolved", "slack_command")

    again = await _command(c, f"resolve {incident_id}")
    assert again.json()["text"] == "Not open (already resolved or unknown id)."
    assert kicked == [incident_id]


@pytest.mark.parametrize("text", ["resolve", "resolve not-a-uuid", "frobnicate", "help"])
async def test_bad_input_returns_help(client, text):
    c, _ = client
    assert (await _command(c, text)).json() == {"response_type": "ephemeral", "text": SLASH_HELP}


async def test_bad_signature_is_rejected(client):
    c, _ = client
    assert (await _command(c, "status", secret="wrong")).status_code == 401


async def test_stale_timestamp_is_rejected(client):
    c, _ = client
    assert (await _command(c, "status", timestamp=int(time.time()) - 301)).status_code == 401
