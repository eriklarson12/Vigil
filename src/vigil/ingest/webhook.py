"""POST /webhooks/alertmanager — validate, dedup, group, enqueue, ACK fast (spec §5).

Everything slow happens *after* the 200 via the runner's inline task.
"""

import secrets
from datetime import datetime
from typing import Any

import structlog
from fastapi import APIRouter, Depends, HTTPException, Request
from psycopg.types.json import Json

from vigil.config import get_settings
from vigil.ingest.fingerprint import alert_fingerprint, attach_incident
from vigil.ingest.resolve import resolve_alert_by_fingerprint

log = structlog.get_logger()
router = APIRouter()


def require_webhook_token(request: Request) -> None:
    settings = get_settings()
    auth = request.headers.get("authorization", "")
    expected = f"Bearer {settings.alertmanager_webhook_token}"
    if not secrets.compare_digest(auth, expected):
        raise HTTPException(status_code=401, detail="invalid token")


def _parse_ts(value: str | None) -> datetime | None:
    if not value or value.startswith("0001-01-01"):
        return None
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


@router.post("/webhooks/alertmanager")
async def alertmanager_webhook(
    request: Request, payload: dict[str, Any], _: None = Depends(require_webhook_token)
) -> dict[str, Any]:
    settings = get_settings()
    pool = request.app.state.deps.pool
    queued, resolved, duplicates = 0, 0, 0

    for alert in payload.get("alerts", []):
        labels = alert.get("labels", {})
        alert_name = labels.get("alertname")
        starts_at = _parse_ts(alert.get("startsAt"))
        if not alert_name or not starts_at:
            raise HTTPException(status_code=422, detail="alert missing alertname or startsAt")
        fp = alert_fingerprint(alert)

        if alert.get("status") == "resolved":
            if await resolve_alert_by_fingerprint(
                request.app, fingerprint=fp, ends_at=_parse_ts(alert.get("endsAt"))
            ):
                resolved += 1
            continue

        async with pool.connection() as conn:  # one tx: insert + group
            cur = await conn.execute(
                """
                INSERT INTO alerts (fingerprint, alert_name, service, status, starts_at,
                                    labels, annotations, raw_payload)
                VALUES (%s, %s, %s, 'firing', %s, %s, %s, %s)
                ON CONFLICT (fingerprint, starts_at) DO NOTHING
                RETURNING id
                """,
                (
                    fp,
                    alert_name,
                    labels.get("service"),
                    starts_at,
                    Json(labels),
                    Json(alert.get("annotations", {})),
                    Json(alert),
                ),
            )
            row = await cur.fetchone()
            if not row:  # Alertmanager re-send within repeat_interval
                duplicates += 1
                continue
            incident_id = await attach_incident(
                conn,
                fingerprint=fp,
                service=labels.get("service"),
                alert_name=alert_name,
                grouping_minutes=settings.incident_grouping_minutes,
            )
            await conn.execute(
                "UPDATE alerts SET incident_id = %s WHERE id = %s", (incident_id, row[0])
            )
            queued += 1

    if queued:
        # Inline-after-ACK (ADR-006): FastAPI sends the response; the task
        # claims queued alerts and runs the triage graph in this process.
        request.app.state.deps.runner.kick()
    log.info("webhook_ingested", queued=queued, resolved=resolved, duplicates=duplicates)
    return {"queued": queued, "resolved": resolved, "duplicates": duplicates}
