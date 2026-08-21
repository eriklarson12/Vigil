"""Operational endpoints: health, resume tick, manual resolve, delete (spec §13.4, §9)."""

import secrets
import uuid
from typing import Any

from fastapi import APIRouter, HTTPException, Request

from vigil.config import get_settings
from vigil.db.purge import IncidentBusy, delete_incident
from vigil.ingest.resolve import resolve_incident

router = APIRouter()


@router.get("/healthz")
async def healthz(request: Request) -> dict[str, str]:
    async with request.app.state.deps.pool.connection() as conn:
        await conn.execute("SELECT 1")
    return {"status": "ok"}


def _require_operator_token(request: Request) -> None:
    """Shared gate for the operator endpoints (resume, manual resolve).

    Both are state-changing and both are reachable from the public internet,
    so neither may rely on obscurity: GET /api/incidents lists every incident id.
    """
    settings = get_settings()
    auth = request.headers.get("authorization", "")
    if not secrets.compare_digest(auth, f"Bearer {settings.resume_token}"):
        raise HTTPException(status_code=401, detail="invalid token")


@router.post("/internal/resume")
async def resume(request: Request) -> dict[str, Any]:
    _require_operator_token(request)
    # The GitHub Actions cron hits this every 15 min; the request itself wakes
    # a scaled-to-zero app (ADR-006). Runs synchronously so the tick's response
    # reports what it recovered.
    return await request.app.state.deps.runner.resume_tick()


@router.post("/api/incidents/{incident_id}/resolve")
async def resolve_via_api(incident_id: str, request: Request) -> dict[str, Any]:
    # Resolving kicks the postmortem graph: one Gemini call plus a Slack post.
    # Unauthenticated, that let anyone spend the budget and write to the channel.
    _require_operator_token(request)
    resolved = await resolve_incident(request.app, incident_id, "api")
    return {"resolved": resolved}


@router.delete("/api/incidents/{incident_id}")
async def delete_via_api(incident_id: uuid.UUID, request: Request) -> dict[str, Any]:
    # Irreversible, and it removes checkpoint rows that prune() could never reach
    # once the incident row is gone. Same gate as resolve. Typing the path param
    # as UUID is deliberate: GET /api/incidents/{id} takes a str and 500s on a
    # malformed one.
    _require_operator_token(request)
    try:
        counts = await delete_incident(request.app.state.deps.pool, str(incident_id))
    except IncidentBusy as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    if counts is None:
        raise HTTPException(status_code=404, detail="incident not found")
    return {"deleted": True, "incident_id": str(incident_id), "counts": counts}
