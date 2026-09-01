"""GET /api/stats — MTTA/MTTR, triage quality, and LLM spend (roadmap R8).

Rows are seeded with direct INSERTs at pinned timestamps rather than by driving
the graph: what is under test is one read-only query, and exact duration
assertions are only possible when the test owns the clock.

Needs Postgres:  docker compose up -d db && uv run pytest -m integration
"""

import json
from datetime import UTC, datetime, timedelta

import pytest

pytestmark = pytest.mark.integration

# Wednesday, so the incident and its week bucket never straddle the ISO Monday boundary.
BASE = datetime(2026, 8, 26, 12, 0, 0, tzinfo=UTC)


async def _incident(
    pool,
    *,
    severity: str = "SEV1",
    status: str = "resolved",
    created_at: datetime = BASE,
    brief_after: float | None = 40.0,
    resolved_after: float | None = 900.0,
    finalized: dict | None = None,
    postmortem: bool = False,
) -> str:
    """One incident with the rows /api/stats reads. None means 'never happened'."""
    resolved_at = created_at + timedelta(seconds=resolved_after) if resolved_after else None
    async with pool.connection() as conn:
        cur = await conn.execute(
            "INSERT INTO incidents (service, title, severity, status, created_at, resolved_at)"
            " VALUES ('checkout', 'HighErrorRate on checkout', %s, %s, %s, %s) RETURNING id",
            (severity, status, created_at, resolved_at),
        )
        (incident_id,) = await cur.fetchone()
        incident_id = str(incident_id)
        if brief_after is not None:
            await conn.execute(
                "INSERT INTO incident_events (incident_id, event_type, payload, created_at)"
                " VALUES (%s, 'brief_posted', '{}', %s)",
                (incident_id, created_at + timedelta(seconds=brief_after)),
            )
        if finalized is not None:
            await conn.execute(
                "INSERT INTO incident_events (incident_id, event_type, payload, created_at)"
                " VALUES (%s, 'triage_finalized', %s, %s)",
                (incident_id, json.dumps(finalized), created_at + timedelta(seconds=1)),
            )
        if postmortem:
            await conn.execute(
                "INSERT INTO postmortems (incident_id, markdown, model_used)"
                " VALUES (%s, '# Postmortem', 'fake')",
                (incident_id,),
            )
    return incident_id


def _final(culprit=None, scored=12, chunks=4, llm_calls=2, errors=None) -> dict:
    return {
        "scored": scored,
        "culprit": culprit,
        "chunks": chunks,
        "severity": "SEV1",
        "llm_calls": llm_calls,
        "errors": errors or {},
    }


async def _stats(c) -> dict:
    resp = await c.get("/api/stats")
    assert resp.status_code == 200
    return resp.json()


async def test_empty_database_returns_a_usable_shape(client):
    c, _ = client
    stats = await _stats(c)

    assert stats["overall"]["open"] == 0
    assert stats["overall"]["mtta_p50"] is None  # no samples, not zero
    assert stats["by_severity"] == []
    assert stats["triage"]["triaged"] == 0
    assert stats["triage"]["degraded_nodes"] == []
    assert stats["llm"]["total_max"] is None
    assert stats["llm"]["today_used"] == 0
    assert len(stats["by_week"]) == 8


async def test_exact_mtta_and_mttr(client):
    c, deps = client
    await _incident(deps.pool, brief_after=40.0, resolved_after=900.0)
    stats = await _stats(c)

    assert stats["overall"]["mtta_p50"] == 40.0
    assert stats["overall"]["mttr_p50"] == 900.0
    assert stats["overall"]["resolved"] == 1
    assert stats["overall"]["mtta_n"] == 1


async def test_percentiles_over_a_known_spread(client):
    c, deps = client
    for seconds in (10.0, 20.0, 30.0, 40.0, 50.0):
        await _incident(deps.pool, brief_after=seconds, resolved_after=None, status="open")
    stats = await _stats(c)

    assert stats["overall"]["mtta_p50"] == 30.0
    assert stats["overall"]["mtta_p90"] == 46.0  # interpolated: 40 + 0.6 * (50 - 40)
    assert stats["overall"]["mtta_n"] == 5


async def test_open_incident_has_mtta_but_no_mttr(client):
    c, deps = client
    await _incident(deps.pool, status="open", brief_after=25.0, resolved_after=None)
    stats = await _stats(c)

    assert stats["overall"]["open"] == 1
    assert stats["overall"]["resolved"] == 0
    assert stats["overall"]["mtta_p50"] == 25.0
    assert stats["overall"]["mttr_p50"] is None
    assert stats["overall"]["mttr_n"] == 0


async def test_incident_without_a_brief_contributes_no_mtta(client):
    c, deps = client
    await _incident(deps.pool, brief_after=None, resolved_after=600.0)
    stats = await _stats(c)

    assert stats["overall"]["mtta_n"] == 0
    assert stats["overall"]["mtta_p50"] is None
    assert stats["overall"]["mttr_p50"] == 600.0  # still counts for MTTR


async def test_severity_split(client):
    c, deps = client
    await _incident(deps.pool, severity="SEV1", brief_after=10.0)
    await _incident(deps.pool, severity="SEV1", brief_after=30.0)
    await _incident(deps.pool, severity="SEV3", brief_after=90.0, status="open", resolved_after=None)
    stats = await _stats(c)

    rows = {r["severity"]: r for r in stats["by_severity"]}
    assert set(rows) == {"SEV1", "SEV3"}  # severities with no incidents are absent
    assert rows["SEV1"]["resolved"] == 2
    assert rows["SEV1"]["mtta_p50"] == 20.0
    assert rows["SEV3"]["open"] == 1
    assert rows["SEV3"]["mttr_p50"] is None
    assert stats["overall"]["open"] + stats["overall"]["resolved"] == 3


async def test_empty_weeks_keep_their_bucket(client):
    c, deps = client
    await _incident(deps.pool, created_at=datetime.now(UTC), brief_after=15.0)
    stats = await _stats(c)

    weeks = stats["by_week"]
    assert len(weeks) == 8
    assert [w["week"] for w in weeks] == sorted(w["week"] for w in weeks)
    populated = [w for w in weeks if w["incidents"] > 0]
    assert len(populated) == 1 and populated[0]["mtta_p50"] == 15.0
    # the other seven are present with nulls, not omitted
    assert [w["mtta_p50"] for w in weeks if w["incidents"] == 0] == [None] * 7


async def test_triage_quality(client):
    c, deps = client
    await _incident(deps.pool, finalized=_final(culprit="a1b2c3d4e5", scored=10, chunks=4))
    await _incident(deps.pool, finalized=_final(culprit="f6g7h8i9j0", scored=14, chunks=6))
    await _incident(deps.pool, finalized=_final(culprit=None, scored=12, chunks=4))
    stats = await _stats(c)

    assert stats["triage"]["triaged"] == 3
    assert stats["triage"]["culprit_named"] == 2  # cert_expiry-shaped run names none
    assert stats["triage"]["scored_p50"] == 12
    assert stats["triage"]["chunks_p50"] == 4
    assert stats["triage"]["degraded"] == 0


async def test_degraded_nodes_are_counted_per_node(client):
    c, deps = client
    await _incident(deps.pool, finalized=_final(errors={"fetch_commits": "boom"}))
    await _incident(deps.pool, finalized=_final(errors={"fetch_commits": "boom"}))
    await _incident(deps.pool, finalized=_final(errors={"retrieve_runbooks": "boom"}))
    await _incident(deps.pool, finalized=_final())
    stats = await _stats(c)

    assert stats["triage"]["degraded"] == 3
    assert stats["triage"]["degraded_nodes"] == [
        {"node": "fetch_commits", "count": 2},
        {"node": "retrieve_runbooks", "count": 1},
    ]


async def test_llm_spend_counts_the_postmortem_call(client):
    c, deps = client
    # triage_finalized records only the triage graph's two calls; the postmortem is
    # a third call on a separate graph, and the ceiling governs the sum.
    await _incident(deps.pool, finalized=_final(llm_calls=2), postmortem=True)
    await _incident(deps.pool, finalized=_final(llm_calls=1), postmortem=False)
    stats = await _stats(c)

    assert stats["llm"]["triage_mean"] == 1.5
    assert stats["llm"]["total_mean"] == 2.0
    assert stats["llm"]["total_max"] == 3
    assert stats["llm"]["over_ceiling"] == 0
    assert stats["llm"]["ceiling"] == 3
    assert stats["llm"]["daily_budget"] == deps.settings.llm_daily_budget


async def test_a_ceiling_breach_is_reported(client):
    c, deps = client
    await _incident(deps.pool, finalized=_final(llm_calls=3), postmortem=True)
    stats = await _stats(c)

    assert stats["llm"]["total_max"] == 4
    assert stats["llm"]["over_ceiling"] == 1


async def test_only_the_latest_finalize_counts(client):
    c, deps = client
    incident_id = await _incident(deps.pool, finalized=_final(culprit=None, llm_calls=1))
    async with deps.pool.connection() as conn:
        await conn.execute(
            "INSERT INTO incident_events (incident_id, event_type, payload, created_at)"
            " VALUES (%s, 'triage_finalized', %s, %s)",
            (incident_id, json.dumps(_final(culprit="a1b2c3d4e5", llm_calls=2)),
             BASE + timedelta(seconds=90)),
        )
    stats = await _stats(c)

    # DISTINCT ON keeps the later row: one incident, and its resumed values win
    assert stats["triage"]["triaged"] == 1
    assert stats["triage"]["culprit_named"] == 1
    assert stats["llm"]["triage_mean"] == 2.0


async def test_severity_is_nullable_and_survives_grouping(client):
    c, deps = client
    # incidents.severity is nullable: attach_incident opens the row before
    # estimate_impact runs, so a crash in between leaves one behind.
    await _incident(deps.pool, severity=None, brief_after=12.0)
    await _incident(deps.pool, severity="SEV2", brief_after=20.0)
    stats = await _stats(c)

    rows = {r["severity"]: r for r in stats["by_severity"]}
    assert None in rows
    assert rows[None]["resolved"] == 1
    assert rows[None]["mtta_p50"] == 12.0
    assert stats["overall"]["resolved"] == 2
