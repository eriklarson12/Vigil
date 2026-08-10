"""POST /slack/interactions — the "Mark resolved" button (spec §9, §14).

Slack signs requests with v0 HMAC over "v0:{timestamp}:{raw_body}". Verify the
signature and a 5-minute timestamp window BEFORE trusting anything in the payload.
"""

import hashlib
import hmac
import json
import time
from urllib.parse import parse_qs

import structlog
from fastapi import APIRouter, HTTPException, Request

from vigil.config import get_settings
from vigil.ingest.resolve import resolve_incident

log = structlog.get_logger()
router = APIRouter()

TIMESTAMP_WINDOW_SECONDS = 300


def verify_slack_signature(raw_body: bytes, timestamp: str, signature: str, signing_secret: str) -> bool:
    if not timestamp or abs(time.time() - float(timestamp)) > TIMESTAMP_WINDOW_SECONDS:
        return False
    basestring = f"v0:{timestamp}:{raw_body.decode()}"
    expected = "v0=" + hmac.new(signing_secret.encode(), basestring.encode(), hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, signature or "")


@router.post("/slack/interactions")
async def slack_interactions(request: Request) -> dict[str, str]:
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

    form = parse_qs(raw.decode())
    payload = json.loads(form.get("payload", ["{}"])[0])
    for action in payload.get("actions", []):
        if action.get("action_id") == "resolve_incident":
            incident_id = action.get("value")
            resolved = await resolve_incident(request.app, incident_id, "slack_button")
            log.info("slack_resolve_click", incident_id=incident_id, resolved=resolved)
            return {"text": "Resolving — postmortem incoming." if resolved else "Already resolved."}
    return {"text": "No action taken."}
