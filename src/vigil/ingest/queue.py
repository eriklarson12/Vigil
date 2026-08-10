"""Postgres-as-queue: claim/complete with FOR UPDATE SKIP LOCKED (spec §5).

The stale-claim clause is the crash-recovery hook: a container killed
mid-triage leaves a claimed row that the next tick reclaims; the LangGraph
checkpoint means the reclaim resumes rather than restarts.
"""

import json
from typing import Any

from psycopg.rows import dict_row
from psycopg_pool import AsyncConnectionPool

CLAIM_SQL = """
UPDATE alerts SET processing_status = 'claimed', claimed_at = now()
WHERE id = (
  SELECT id FROM alerts
  WHERE processing_status = 'queued'
     OR (processing_status = 'claimed' AND claimed_at < now() - make_interval(mins => %s))
  ORDER BY received_at
  FOR UPDATE SKIP LOCKED
  LIMIT 1)
RETURNING id, fingerprint, alert_name, service, status, starts_at, labels, annotations, incident_id
"""


async def claim_next(pool: AsyncConnectionPool, stale_minutes: int = 10) -> dict[str, Any] | None:
    async with pool.connection() as conn:
        # row factory at CURSOR level only — pooled connections are reused, so
        # mutating conn.row_factory would leak dict rows into other call sites
        async with conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(CLAIM_SQL, (stale_minutes,))
            row = await cur.fetchone()
    if not row:
        return None
    row["id"] = str(row["id"])
    row["incident_id"] = str(row["incident_id"]) if row["incident_id"] else None
    return row


async def mark_alert(pool: AsyncConnectionPool, alert_id: str, status: str) -> None:
    async with pool.connection() as conn:
        await conn.execute(
            "UPDATE alerts SET processing_status = %s WHERE id = %s", (status, alert_id)
        )


async def add_event(
    pool: AsyncConnectionPool, incident_id: str, event_type: str, payload: dict[str, Any] | None = None
) -> None:
    """Append to the incident timeline; payloads capped at 8KB at write time (spec §11)."""
    body = json.dumps(payload or {})
    if len(body) > 8192:
        body = json.dumps({"truncated": True, "head": body[:8000]})
    async with pool.connection() as conn:
        await conn.execute(
            "INSERT INTO incident_events (incident_id, event_type, payload) VALUES (%s, %s, %s)",
            (incident_id, event_type, body),
        )
