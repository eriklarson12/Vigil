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
