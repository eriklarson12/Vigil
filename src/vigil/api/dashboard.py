"""Read-only REST API for the dashboard (and vigil-sim demo polling)."""

from typing import Any

from fastapi import APIRouter, HTTPException, Request
from psycopg.rows import dict_row

router = APIRouter(prefix="/api")


@router.get("/incidents")
async def list_incidents(request: Request) -> list[dict[str, Any]]:
    pool = request.app.state.deps.pool
    async with pool.connection() as conn:
        async with conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(
                """
                SELECT i.id, i.service, i.title, i.severity, i.status, i.slack_message_ts,
                       i.created_at, i.resolved_at, (p.id IS NOT NULL) AS has_postmortem
                FROM incidents i LEFT JOIN postmortems p ON p.incident_id = i.id
                ORDER BY i.created_at DESC LIMIT 100
                """
            )
            rows = await cur.fetchall()
    return [_jsonable(r) for r in rows]


@router.get("/incidents/{incident_id}")
async def get_incident(incident_id: str, request: Request) -> dict[str, Any]:
    pool = request.app.state.deps.pool
    async with pool.connection() as conn:
        async with conn.cursor(row_factory=dict_row) as cur:
            await cur.execute("SELECT * FROM incidents WHERE id = %s", (incident_id,))
            incident = await cur.fetchone()
            if not incident:
                raise HTTPException(status_code=404, detail="incident not found")
            await cur.execute(
                "SELECT event_type, payload, created_at FROM incident_events"
                " WHERE incident_id = %s ORDER BY created_at",
                (incident_id,),
            )
            events = await cur.fetchall()
            await cur.execute(
                "SELECT sha, message, author, committed_at, files, heuristic_score,"
                " feature_scores, llm_rank, llm_confidence, llm_rationale FROM commit_candidates"
                " WHERE incident_id = %s ORDER BY heuristic_score DESC",
                (incident_id,),
            )
            candidates = await cur.fetchall()
            await cur.execute(
                "SELECT markdown, model_used, created_at FROM postmortems WHERE incident_id = %s",
                (incident_id,),
            )
            postmortem = await cur.fetchone()
    return {
        "incident": _jsonable(incident),
        "events": [_jsonable(e) for e in events],
        "commit_candidates": [_jsonable(c) for c in candidates],
        "postmortem": _jsonable(postmortem) if postmortem else None,
    }


def _jsonable(row: dict[str, Any]) -> dict[str, Any]:
    import datetime
    import decimal
    import uuid

    out = {}
    for k, v in row.items():
        if isinstance(v, (uuid.UUID, datetime.datetime, datetime.date)):
            out[k] = str(v)
        elif isinstance(v, decimal.Decimal):
            out[k] = float(v)
        else:
            out[k] = v
    return out

# Every number below is computed on read from rows that already exist: incident
# timestamps, the brief_posted event, and the triage_finalized payload written by
# graph/triage.py. No stats tables, no migration, no write path to keep in sync.

# The hard constraint from the spec: rank + brief in triage, postmortem on resolve.
# An architectural invariant, not a free-tier quota, so it is a literal here while
# the daily budget stays in Settings.
LLM_CALL_CEILING = 3

STATS_SQL = """
WITH durations AS (
    SELECT i.id, i.severity, i.status,
           date_trunc('week', i.created_at) AS week,
           EXTRACT(EPOCH FROM (
               (SELECT min(e.created_at) FROM incident_events e
                 WHERE e.incident_id = i.id AND e.event_type = 'brief_posted')
               - i.created_at
           ))::float8 AS mtta,
           EXTRACT(EPOCH FROM (i.resolved_at - i.created_at))::float8 AS mttr
    FROM incidents i
),
-- A resumed graph could in principle write finalize twice; the later row wins.
finalized AS (
    SELECT DISTINCT ON (e.incident_id)
           e.incident_id,
           e.payload ->> 'culprit'          AS culprit,
           (e.payload ->> 'scored')::int    AS scored,
           (e.payload ->> 'chunks')::int    AS chunks,
           (e.payload ->> 'llm_calls')::int AS triage_calls,
           e.payload -> 'errors'            AS errors
    FROM incident_events e
    WHERE e.event_type = 'triage_finalized'
    ORDER BY e.incident_id, e.created_at DESC
),
-- triage_finalized counts only the triage graph (rank + brief). The postmortem is
-- a separate graph on a separate trigger, so the per-incident total that the
-- 3-call ceiling actually governs is triage plus one if a postmortem was written.
spend AS (
    SELECT f.triage_calls,
           f.triage_calls + (CASE WHEN p.incident_id IS NULL THEN 0 ELSE 1 END) AS total_calls
    FROM finalized f LEFT JOIN postmortems p ON p.incident_id = f.incident_id
),
weeks AS (
    SELECT generate_series(
        date_trunc('week', now()) - interval '7 weeks',
        date_trunc('week', now()),
        interval '1 week'
    ) AS week
),
degraded_nodes AS (
    SELECT node, count(*) AS count
    FROM finalized f, LATERAL jsonb_object_keys(f.errors) AS node
    GROUP BY node
)
SELECT json_build_object(
    'overall', (
        SELECT json_build_object(
            'open',     count(*) FILTER (WHERE status = 'open'),
            'resolved', count(*) FILTER (WHERE status <> 'open'),
            'mtta_n',   count(mtta),
            'mttr_n',   count(mttr),
            'mtta_p50', percentile_cont(0.5) WITHIN GROUP (ORDER BY mtta),
            'mtta_p90', percentile_cont(0.9) WITHIN GROUP (ORDER BY mtta),
            'mttr_p50', percentile_cont(0.5) WITHIN GROUP (ORDER BY mttr),
            'mttr_p90', percentile_cont(0.9) WITHIN GROUP (ORDER BY mttr)
        ) FROM durations
    ),
    'by_severity', COALESCE((
        SELECT json_agg(s ORDER BY s.severity) FROM (
            SELECT severity,
                   count(*) FILTER (WHERE status = 'open')  AS open,
                   count(*) FILTER (WHERE status <> 'open') AS resolved,
                   percentile_cont(0.5) WITHIN GROUP (ORDER BY mtta) AS mtta_p50,
                   percentile_cont(0.9) WITHIN GROUP (ORDER BY mtta) AS mtta_p90,
                   percentile_cont(0.5) WITHIN GROUP (ORDER BY mttr) AS mttr_p50,
                   percentile_cont(0.9) WITHIN GROUP (ORDER BY mttr) AS mttr_p90
            FROM durations GROUP BY severity
        ) s
    ), '[]'::json),
    -- LEFT JOIN against generate_series: an empty week keeps its bucket with null
    -- percentiles instead of vanishing, so the trend cannot silently compress.
    'by_week', (
        SELECT json_agg(w ORDER BY w.week) FROM (
            SELECT to_char(wk.week, 'YYYY-MM-DD') AS week,
                   count(d.id) AS incidents,
                   percentile_cont(0.5) WITHIN GROUP (ORDER BY d.mtta) AS mtta_p50,
                   percentile_cont(0.5) WITHIN GROUP (ORDER BY d.mttr) AS mttr_p50
            FROM weeks wk LEFT JOIN durations d ON d.week = wk.week
            GROUP BY wk.week
        ) w
    ),
    'triage', (
        SELECT json_build_object(
            'triaged',        count(*),
            'culprit_named',  count(culprit),
            'scored_p50',     percentile_disc(0.5) WITHIN GROUP (ORDER BY scored),
            'chunks_p50',     percentile_disc(0.5) WITHIN GROUP (ORDER BY chunks),
            'degraded',       count(*) FILTER (WHERE errors <> '{}'::jsonb),
            'degraded_nodes', COALESCE(
                (SELECT json_agg(json_build_object('node', node, 'count', count)
                                 ORDER BY count DESC, node)
                   FROM degraded_nodes), '[]'::json)
        ) FROM finalized
    ),
    'llm', (
        SELECT json_build_object(
            'triage_mean',  round(avg(triage_calls), 2),
            'total_mean',   round(avg(total_calls), 2),
            'total_max',    max(total_calls),
            'over_ceiling', count(*) FILTER (WHERE total_calls > %s),
            'today_used',   (SELECT calls_used FROM llm_budget WHERE day = CURRENT_DATE)
        ) FROM spend
    )
) AS stats
"""


@router.get("/stats")
async def get_stats(request: Request) -> dict[str, Any]:
    """MTTA/MTTR, triage quality, and LLM spend, computed on read (roadmap R8)."""
    deps = request.app.state.deps
    async with deps.pool.connection() as conn:
        cur = await conn.execute(STATS_SQL, (LLM_CALL_CEILING,))
        (stats,) = await cur.fetchone()
    stats["llm"]["ceiling"] = LLM_CALL_CEILING
    stats["llm"]["daily_budget"] = deps.settings.llm_daily_budget
    stats["llm"]["today_used"] = stats["llm"]["today_used"] or 0
    return stats
