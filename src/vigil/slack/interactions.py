"""POST /slack/interactions — "Mark resolved" (spec §9, §14) and "Propose revert PR" (R5).
POST /slack/commands — the `/vigil` slash command (R10).

Slack signs requests with v0 HMAC over "v0:{timestamp}:{raw_body}". Verify the
signature and a 5-minute timestamp window BEFORE trusting anything in the payload.
"""

import hashlib
import hmac
import json
import time
import uuid
from datetime import UTC, datetime
from typing import Any
from urllib.parse import parse_qs

import structlog
from fastapi import APIRouter, HTTPException, Request
from psycopg.rows import dict_row

from vigil.commits.rollback import claim_revert, load_revert_context
from vigil.config import get_settings
from vigil.ingest.queue import add_event
from vigil.ingest.resolve import resolve_incident
from vigil.slack.blocks import SLASH_HELP, STATUS_LIMIT, build_status_message

log = structlog.get_logger()
router = APIRouter()

TIMESTAMP_WINDOW_SECONDS = 300
# The follow-up POST goes wherever response_url points; only Slack's own host is accepted.
SLACK_RESPONSE_URL_PREFIX = "https://hooks.slack.com/"


def verify_slack_signature(raw_body: bytes, timestamp: str, signature: str, signing_secret: str) -> bool:
    if not timestamp or abs(time.time() - float(timestamp)) > TIMESTAMP_WINDOW_SECONDS:
        return False
    basestring = f"v0:{timestamp}:{raw_body.decode()}"
    expected = "v0=" + hmac.new(signing_secret.encode(), basestring.encode(), hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, signature or "")


async def _require_signed(request: Request) -> bytes:
    settings = get_settings()
    raw = await request.body()
    if not settings.slack_signing_secret:
        raise HTTPException(status_code=503, detail="slack interactions not configured")
    if not verify_slack_signature(
        raw,
        request.headers.get("x-slack-request-timestamp", ""),
        request.headers.get("x-slack-signature", ""),
        settings.slack_signing_secret,
    ):
        raise HTTPException(status_code=401, detail="bad signature")
    return raw


@router.post("/slack/interactions")
async def slack_interactions(request: Request) -> dict[str, str]:
    raw = await _require_signed(request)
    form = parse_qs(raw.decode())
    payload = json.loads(form.get("payload", ["{}"])[0])
    for action in payload.get("actions", []):
        if action.get("action_id") == "resolve_incident":
            incident_id = action.get("value")
            resolved = await resolve_incident(request.app, incident_id, "slack_button")
            log.info("slack_resolve_click", incident_id=incident_id, resolved=resolved)
            return {"text": "Resolving — postmortem incoming." if resolved else "Already resolved."}
        if action.get("action_id") == "propose_revert":
            return await _propose_revert_click(request, action.get("value"), payload.get("response_url"))
    return {"text": "No action taken."}


async def _propose_revert_click(
    request: Request, incident_id: str | None, response_url: str | None
) -> dict[str, str]:
    deps = request.app.state.deps
    try:
        uuid.UUID(str(incident_id))
    except ValueError:
        return {"text": "No action taken."}
    if response_url and not response_url.startswith(SLACK_RESPONSE_URL_PREFIX):
        response_url = None
    if await load_revert_context(deps.pool, incident_id) is None:
        log.info("slack_revert_refused", incident_id=incident_id, reason="gate")
        return {"text": "This incident's culprit does not qualify for a revert PR."}
    if not await claim_revert(deps.pool, incident_id):
        return {"text": "A revert PR is already requested or proposed."}
    await add_event(deps.pool, incident_id, "revert_pr_requested", {"response_url": response_url})
    deps.runner.kick_revert(incident_id, response_url)
    log.info("slack_revert_click", incident_id=incident_id)
    return {"text": "Proposing a revert PR."}


@router.post("/slack/commands")
async def slack_commands(request: Request) -> dict[str, Any]:
    # Slack drops the reply after 3s, so this reads Postgres directly and never touches the graph.
    raw = await _require_signed(request)
    words = parse_qs(raw.decode()).get("text", [""])[0].split()
    subcommand = words[0].lower() if words else "status"
    log.info("slack_command", subcommand=subcommand)
    if subcommand == "status":
        return await _status(request)
    if subcommand == "resolve":
        return await _resolve(request, words[1] if len(words) == 2 else None)
    return _ephemeral(SLASH_HELP)


def _ephemeral(text: str) -> dict[str, Any]:
    return {"response_type": "ephemeral", "text": text}


async def _status(request: Request) -> dict[str, Any]:
    deps = request.app.state.deps
    async with deps.pool.connection() as conn:
        async with conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(
                "SELECT id, title, severity, created_at FROM incidents"
                " WHERE status = 'open' ORDER BY created_at DESC LIMIT %s",
                (STATUS_LIMIT + 1,),
            )
            rows = await cur.fetchall()
    return build_status_message(rows, deps.settings.dashboard_url, datetime.now(UTC))


async def _resolve(request: Request, incident_id: str | None) -> dict[str, Any]:
    try:
        uuid.UUID(str(incident_id))
    except ValueError:
        return _ephemeral(SLASH_HELP)
    resolved = await resolve_incident(request.app, incident_id, "slack_command")
    log.info("slack_command_resolve", incident_id=incident_id, resolved=resolved)
    if resolved:
        return _ephemeral("Resolving — postmortem incoming.")
    return _ephemeral("Not open (already resolved or unknown id).")
