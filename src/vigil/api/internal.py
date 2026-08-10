"""Operational endpoints: health, resume tick, manual resolve (spec §13.4, §9)."""

import secrets
from typing import Any

from fastapi import APIRouter, HTTPException, Request

from vigil.config import get_settings
from vigil.ingest.resolve import resolve_incident

router = APIRouter()


@router.get("/healthz")
async def healthz(request: Request) -> dict[str, str]:
    async with request.app.state.deps.pool.connection() as conn:
        await conn.execute("SELECT 1")
    return {"status": "ok"}


@router.post("/internal/resume")
async def resume(request: Request) -> dict[str, Any]:
    settings = get_settings()
    auth = request.headers.get("authorization", "")
    if not secrets.compare_digest(auth, f"Bearer {settings.resume_token}"):
        raise HTTPException(status_code=401, detail="invalid token")
    # The GitHub Actions cron hits this every 15 min; the request itself wakes
    # a scaled-to-zero app (ADR-006). Runs synchronously so the tick's response
    # reports what it recovered.
    return await request.app.state.deps.runner.resume_tick()


@router.post("/api/incidents/{incident_id}/resolve")
async def resolve_via_api(incident_id: str, request: Request) -> dict[str, Any]:
    resolved = await resolve_incident(request.app, incident_id, "api")
    return {"resolved": resolved}
