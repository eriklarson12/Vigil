"""DELETE /api/incidents/{id} — the operator hard-delete (db/purge.py).

Rows are seeded with direct INSERTs rather than by driving the graph: what is
under test is the six-table transaction, and a full pipeline run per case would
be slow and coupled to triage behaviour.
"""

import uuid

import pytest

pytestmark = pytest.mark.integration

TOKEN = {"Authorization": "Bearer dev-token"}
CHECKPOINT_TABLES = ("checkpoints", "checkpoint_writes", "checkpoint_blobs")


async def _seed(pool, *, status: str = "postmortem_done", claimed: bool = False) -> str:
    """Insert one incident with a row in every table that references it."""
    async with pool.connection() as conn:
        cur = await conn.execute(
            "INSERT INTO incidents (service, title, severity, status, resolved_at)"
            " VALUES ('checkout', 'HighErrorRate on checkout', 'SEV1', %s, now()) RETURNING id",
            (status,),
        )
        (incident_id,) = await cur.fetchone()
        incident_id = str(incident_id)
        await conn.execute(
            "INSERT INTO alerts (fingerprint, alert_name, service, status, starts_at, labels,"
            " annotations, processing_status, incident_id) VALUES (%s, 'HighErrorRate', 'checkout',"
            " 'resolved', now(), '{}', '{}', %s, %s)",
            (f"fp-{incident_id}", "claimed" if claimed else "processed", incident_id),
        )
        for event_type in ("triage_started", "brief_posted"):
            await conn.execute(
                "INSERT INTO incident_events (incident_id, event_type, payload)"
                " VALUES (%s, %s, '{}')",
                (incident_id, event_type),
            )
        await conn.execute(
            "INSERT INTO commit_candidates (incident_id, sha, message, author, committed_at,"
            " files, heuristic_score) VALUES (%s, 'a1b2c3d4e5', 'refactor', 'dev', now(),"
            " '[]', 0.7)",
            (incident_id,),
        )
        await conn.execute(
            "INSERT INTO postmortems (incident_id, markdown, model_used)"
            " VALUES (%s, '# Postmortem', 'fake')",
            (incident_id,),
        )
        for thread in (f"triage:{incident_id}", f"pm:{incident_id}"):
            await conn.execute(
                "INSERT INTO checkpoints (thread_id, checkpoint_id, checkpoint)"
                " VALUES (%s, 'ck-1', '{}')",
                (thread,),
            )
            await conn.execute(
                "INSERT INTO checkpoint_writes (thread_id, checkpoint_id, task_id, idx, channel,"
                " blob) VALUES (%s, 'ck-1', 'task-1', 0, 'messages', '\\x00')",
                (thread,),
            )
            await conn.execute(
                "INSERT INTO checkpoint_blobs (thread_id, channel, version, type)"
                " VALUES (%s, 'messages', '1', 'msgpack')",
                (thread,),
            )
    return incident_id


async def _row_counts(pool, incident_id: str) -> dict[str, int]:
    threads = [f"triage:{incident_id}", f"pm:{incident_id}"]
    counts = {}
    async with pool.connection() as conn:
        for table in ("alerts", "incident_events", "commit_candidates", "postmortems"):
            cur = await conn.execute(
                f"SELECT count(*) FROM {table} WHERE incident_id = %s", (incident_id,)
            )
            (counts[table],) = await cur.fetchone()
        for table in CHECKPOINT_TABLES:
            cur = await conn.execute(
                f"SELECT count(*) FROM {table} WHERE thread_id = ANY(%s)", (threads,)
            )
            (counts[table],) = await cur.fetchone()
        cur = await conn.execute("SELECT count(*) FROM incidents WHERE id = %s", (incident_id,))
        (counts["incidents"],) = await cur.fetchone()
    return counts


async def test_delete_removes_incident_and_all_children(client):
    c, deps = client
    incident_id = await _seed(deps.pool)

    resp = await c.delete(f"/api/incidents/{incident_id}", headers=TOKEN)
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["deleted"] is True
    assert body["counts"] == {
        "checkpoints": 2,
        "checkpoint_writes": 2,
        "checkpoint_blobs": 2,
        "postmortems": 1,
        "commit_candidates": 1,
        "incident_events": 2,
        "alerts": 1,
        "incidents": 1,
    }
    # the checkpoint counts above are also the assertion that the to_regclass
    # probe found the tables; its false branch is deliberately untested, since
    # dropping them would poison the shared integration database.

    assert all(n == 0 for n in (await _row_counts(deps.pool, incident_id)).values())
    assert (await c.get(f"/api/incidents/{incident_id}")).status_code == 404
    listed = [i["id"] for i in (await c.get("/api/incidents")).json()]
    assert incident_id not in listed


async def test_delete_leaves_other_incidents_untouched(client):
    c, deps = client
    doomed = await _seed(deps.pool)
    keeper = await _seed(deps.pool)

    assert (await c.delete(f"/api/incidents/{doomed}", headers=TOKEN)).status_code == 200

    assert await _row_counts(deps.pool, keeper) == {
        "alerts": 1,
        "incident_events": 2,
        "commit_candidates": 1,
        "postmortems": 1,
        "checkpoints": 2,
        "checkpoint_writes": 2,
        "checkpoint_blobs": 2,
        "incidents": 1,
    }


async def test_delete_requires_operator_token(client):
    c, deps = client
    incident_id = await _seed(deps.pool)
    before = await _row_counts(deps.pool, incident_id)

    assert (await c.delete(f"/api/incidents/{incident_id}")).status_code == 401
    bad = {"Authorization": "Bearer nope"}
    assert (await c.delete(f"/api/incidents/{incident_id}", headers=bad)).status_code == 401
    assert await _row_counts(deps.pool, incident_id) == before


async def test_delete_unknown_incident_returns_404(client):
    c, _ = client
    resp = await c.delete(f"/api/incidents/{uuid.uuid4()}", headers=TOKEN)
    assert resp.status_code == 404
    assert resp.json()["detail"] == "incident not found"


async def test_delete_malformed_id_returns_422(client):
    """GET /api/incidents/{id} takes a str and 500s here; this must not."""
    c, _ = client
    assert (await c.delete("/api/incidents/not-a-uuid", headers=TOKEN)).status_code == 422


async def test_delete_open_incident_returns_409(client):
    c, deps = client
    incident_id = await _seed(deps.pool, status="open")
    before = await _row_counts(deps.pool, incident_id)

    resp = await c.delete(f"/api/incidents/{incident_id}", headers=TOKEN)
    assert resp.status_code == 409
    assert "resolve it" in resp.json()["detail"]
    # the guard must abort before any DML, not part-way through it
    assert await _row_counts(deps.pool, incident_id) == before


async def test_delete_conflicts_while_alert_claimed(client):
    """A resolved incident can still have a claimed alert the next tick reclaims."""
    c, deps = client
    incident_id = await _seed(deps.pool, claimed=True)
    before = await _row_counts(deps.pool, incident_id)

    resp = await c.delete(f"/api/incidents/{incident_id}", headers=TOKEN)
    assert resp.status_code == 409
    assert "in flight" in resp.json()["detail"]
    assert await _row_counts(deps.pool, incident_id) == before


async def test_delete_twice_returns_404_second_time(client):
    """Deliberately not idempotent: a second delete must not report success."""
    c, deps = client
    incident_id = await _seed(deps.pool)
    assert (await c.delete(f"/api/incidents/{incident_id}", headers=TOKEN)).status_code == 200
    assert (await c.delete(f"/api/incidents/{incident_id}", headers=TOKEN)).status_code == 404


async def test_delete_covers_every_incident_fk(client):
    """A migration that adds a child table must not silently escape the delete."""
    from vigil.db.purge import CHILD_TABLES

    _, deps = client
    async with deps.pool.connection() as conn:
        cur = await conn.execute(
            "SELECT DISTINCT conrelid::regclass::text FROM pg_constraint"
            " WHERE contype = 'f' AND confrelid = 'incidents'::regclass"
        )
        referencing = {r[0] for r in await cur.fetchall()}
    assert referencing == set(CHILD_TABLES), (
        f"tables referencing incidents: {sorted(referencing)}, but purge.py deletes from"
        f" {sorted(CHILD_TABLES)} - update src/vigil/db/purge.py"
    )
