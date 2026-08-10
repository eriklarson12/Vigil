"""The single idempotent resolution funnel (spec §9).

Three entry points converge here: Alertmanager `resolved` webhooks,
the Slack button, and POST /api/incidents/{id}/resolve.
"""

from datetime import datetime
from typing import Any

import structlog

from vigil.ingest.queue import add_event

log = structlog.get_logger()


async def resolve_incident(app: Any, incident_id: str, source: str) -> bool:
    """Mark resolved and kick the postmortem graph. No-op if already resolved."""
    pool = app.state.deps.pool
    async with pool.connection() as conn:
        cur = await conn.execute(
            """
            UPDATE incidents
            SET status = 'resolved', resolved_at = now(), resolution_source = %s
            WHERE id = %s AND status = 'open'
            RETURNING id
            """,
            (source, incident_id),
        )
        row = await cur.fetchone()
    if not row:
        return False
    await add_event(pool, incident_id, "resolved", {"source": source})
    log.info("incident_resolved", incident_id=incident_id, source=source)
    app.state.deps.runner.kick_postmortem(incident_id)
    return True


async def resolve_alert_by_fingerprint(app: Any, fingerprint: str, ends_at: datetime | None) -> bool:
    """Alertmanager `resolved` path: mark the alert; resolve the incident once
    ALL of its alerts are resolved."""
    pool = app.state.deps.pool
    async with pool.connection() as conn:
        cur = await conn.execute(
            """
            UPDATE alerts SET status = 'resolved', ends_at = COALESCE(%s, now())
            WHERE fingerprint = %s AND status = 'firing'
            RETURNING incident_id
            """,
            (ends_at, fingerprint),
        )
        rows = await cur.fetchall()
    incident_ids = {str(r[0]) for r in rows if r[0]}
    resolved_any = False
    for incident_id in incident_ids:
        async with pool.connection() as conn:
            cur = await conn.execute(
                "SELECT count(*) FROM alerts WHERE incident_id = %s AND status = 'firing'",
                (incident_id,),
            )
            (firing,) = await cur.fetchone()
        if firing == 0:
            resolved_any = await resolve_incident(app, incident_id, "alertmanager") or resolved_any
    return resolved_any or bool(rows)
