"""R9 action items filed as issues, end to end in ISSUES_MODE=mock. Needs Postgres:
    docker compose up -d db
    uv run pytest -m integration
"""

import json

import pytest

from tests.integration.test_revert import TOKEN, _events, _fire, _wait

pytestmark = pytest.mark.integration


async def _resolve(c, deps, incident_id: str) -> None:
    resp = await c.post(f"/api/incidents/{incident_id}/resolve", headers=TOKEN)
    assert resp.json()["resolved"] is True

    async def done():
        async with deps.pool.connection() as conn:
            cur = await conn.execute("SELECT status FROM incidents WHERE id = %s", (incident_id,))
            row = await cur.fetchone()
        return row[0] == "postmortem_done"

    await _wait(done)


async def _postmortem(deps, incident_id: str) -> tuple[list, dict, int]:
    async with deps.pool.connection() as conn:
        cur = await conn.execute(
            "SELECT action_items, issue_urls, issue_attempts FROM postmortems WHERE incident_id = %s",
            (incident_id,),
        )
        return await cur.fetchone()


async def _filed(deps, incident_id: str) -> list[dict]:
    return [p for t, p in await _events(deps, incident_id) if t == "issue_filed"]


async def _settled(deps, incident_id: str):
    async def check():
        items, urls, attempts = await _postmortem(deps, incident_id)
        return (items, urls, attempts) if len(urls) == len(items) or attempts else None

    return await _wait(check)


async def test_resolve_files_one_issue_per_action_item_once(client):
    c, deps = client
    incident_id = await _fire(c, deps, "bad_deploy")
    await _resolve(c, deps, incident_id)

    items, urls, attempts = await _settled(deps, incident_id)
    assert len(items) >= 2 and attempts == 0
    assert sorted(urls) == [str(i) for i in range(len(items))]
    assert all(u.startswith("mock://github.com/eriklarson12/vigil-demo-shop/issues/") for u in urls.values())
    filed = await _filed(deps, incident_id)
    assert len(filed) == len(items)
    assert filed[0]["title"] == f"[Vigil {incident_id[:8]}] {items[0]['description']}"[:256]

    resp = await c.post("/internal/resume", headers=TOKEN)
    assert resp.json()["issues_resumed"] == 0
    assert len(await _filed(deps, incident_id)) == len(items)


async def test_resume_tick_files_what_a_killed_run_left(client):
    c, deps = client
    incident_id = await _fire(c, deps, "bad_deploy")
    await _resolve(c, deps, incident_id)
    items, urls, _ = await _settled(deps, incident_id)

    # A container killed after item 0: only its URL made it to Postgres.
    async with deps.pool.connection() as conn:
        await conn.execute(
            "UPDATE postmortems SET issue_urls = %s, created_at = now() - interval '1 hour'"
            " WHERE incident_id = %s",
            (json.dumps({"0": urls["0"]}), incident_id),
        )
        await conn.execute(
            "DELETE FROM incident_events WHERE incident_id = %s AND event_type = 'issue_filed'"
            " AND (payload->>'index')::int > 0",
            (incident_id,),
        )

    resp = await c.post("/internal/resume", headers=TOKEN)
    assert resp.json()["issues_resumed"] == 1
    _, urls_after, _ = await _postmortem(deps, incident_id)
    assert urls_after == urls
    assert sorted(p["index"] for p in await _filed(deps, incident_id)) == list(range(len(items)))

    again = await c.post("/internal/resume", headers=TOKEN)
    assert again.json()["issues_resumed"] == 0


async def test_live_mode_without_write_token_counts_attempts_then_stops(client):
    c, deps = client
    incident_id = await _fire(c, deps, "bad_deploy")
    deps.settings.issues_mode = "live"
    try:
        await _resolve(c, deps, incident_id)
        _, urls, attempts = await _settled(deps, incident_id)
        assert (urls, attempts) == ({}, 1)
        failed = [p for t, p in await _events(deps, incident_id) if t == "issues_file_failed"]
        assert failed == [{"error": "GITHUB_WRITE_TOKEN is not set"}]

        async with deps.pool.connection() as conn:
            await conn.execute(
                "UPDATE postmortems SET created_at = now() - interval '1 hour' WHERE incident_id = %s",
                (incident_id,),
            )
        for expected in (2, 3):
            resp = await c.post("/internal/resume", headers=TOKEN)
            assert resp.json()["issues_resumed"] == 1
            assert (await _postmortem(deps, incident_id))[2] == expected

        resp = await c.post("/internal/resume", headers=TOKEN)
        assert resp.json()["issues_resumed"] == 0
    finally:
        deps.settings.issues_mode = "mock"
    assert await _filed(deps, incident_id) == []
