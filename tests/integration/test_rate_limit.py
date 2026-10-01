"""429 on the public write routes past RATE_LIMIT_PER_MIN (roadmap R12, ADR-014).

Pinned to 60 in tests/conftest.py. Each test builds its own app, so each starts with a
fresh window. Needs Postgres:  docker compose up -d db && uv run pytest -m integration
"""

from urllib.parse import urlencode

import pytest

from tests.integration.slack_signing import sign_slack_request

pytestmark = pytest.mark.integration

LIMIT = 60
TOKEN = {"Authorization": "Bearer dev-token"}
EMPTY = {"version": "4", "status": "firing", "alerts": []}


async def _webhook(c, headers=TOKEN):
    return await c.post("/webhooks/alertmanager", json=EMPTY, headers=headers)


async def _command(c, team_id: str):
    body = urlencode({"command": "/vigil", "text": "status", "team_id": team_id})
    return await c.post("/slack/commands", content=body, headers=sign_slack_request(body))


async def test_webhook_rejects_the_61st_request_in_a_minute(client):
    c, _ = client
    for _ in range(LIMIT):
        assert (await _webhook(c)).status_code == 200
    resp = await _webhook(c)
    assert resp.status_code == 429
    assert 1 <= int(resp.headers["retry-after"]) <= 60


async def test_bad_token_never_spends_the_budget(client):
    c, _ = client
    for _ in range(LIMIT + 1):
        assert (await _webhook(c, {"Authorization": "Bearer wrong"})).status_code == 401
    assert (await _webhook(c)).status_code == 200


async def test_slash_command_limited_per_team(client):
    c, _ = client
    for _ in range(LIMIT):
        assert (await _command(c, "T1")).status_code == 200
    assert (await _command(c, "T1")).status_code == 429
    assert (await _command(c, "T2")).status_code == 200


async def test_resume_tick_is_never_limited(client):
    c, _ = client
    for _ in range(LIMIT + 1):
        assert (await c.post("/internal/resume", headers=TOKEN)).status_code == 200
