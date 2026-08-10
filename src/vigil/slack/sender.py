"""Slack delivery (spec §9, §15).

SLACK_MODE=mock    -> write payload to incident_events (console + dashboard render);
                      fabricates a ts so threading logic is exercised.
SLACK_MODE=webhook -> Incoming Webhook. NOTE: incoming webhooks return no message
                      ts, so the postmortem posts as a separate message. Provide
                      SLACK_BOT_TOKEN to use chat.postMessage instead, which
                      returns ts and enables real threaded postmortems.
"""

from typing import Any

import httpx
import structlog
from psycopg_pool import AsyncConnectionPool

from vigil.config import Settings
from vigil.ingest.queue import add_event

log = structlog.get_logger()


class SlackSender:
    def __init__(self, settings: Settings, pool: AsyncConnectionPool):
        self._settings = settings
        self._pool = pool

    async def post_brief(self, incident_id: str, payload: dict[str, Any]) -> str:
        """Post the brief; return a message ts (fabricated in mock/webhook modes)."""
        mode = self._settings.slack_mode
        if mode == "mock":
            await add_event(self._pool, incident_id, "brief_posted", {"slack_payload": payload})
            log.info("slack_mock_brief", incident_id=incident_id, text=payload.get("text"))
            return f"mock-{incident_id}"
        if self._settings.slack_bot_token:
            ts = await self._post_api(payload)
            await add_event(self._pool, incident_id, "brief_posted", {"ts": ts})
            return ts
        await self._post_webhook(payload)
        await add_event(self._pool, incident_id, "brief_posted", {"via": "webhook"})
        return f"webhook-{incident_id}"

    async def post_thread(self, incident_id: str, thread_ts: str, payload: dict[str, Any]) -> None:
        mode = self._settings.slack_mode
        if mode == "mock":
            await add_event(self._pool, incident_id, "postmortem_posted", {"slack_payload": payload})
            log.info("slack_mock_thread", incident_id=incident_id)
            return
        if self._settings.slack_bot_token and not thread_ts.startswith(("mock-", "webhook-")):
            await self._post_api(payload, thread_ts=thread_ts)
        else:
            await self._post_webhook(payload)  # no threading without a bot token
        await add_event(self._pool, incident_id, "postmortem_posted", {})

    async def _post_webhook(self, payload: dict[str, Any]) -> None:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.post(self._settings.slack_webhook_url, json=payload)
            resp.raise_for_status()

    async def _post_api(self, payload: dict[str, Any], thread_ts: str | None = None) -> str:
        body = {"channel": self._settings.slack_channel, **payload}
        if thread_ts:
            body["thread_ts"] = thread_ts
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.post(
                "https://slack.com/api/chat.postMessage",
                json=body,
                headers={"Authorization": f"Bearer {self._settings.slack_bot_token}"},
            )
            resp.raise_for_status()
            data = resp.json()
            if not data.get("ok"):
                raise RuntimeError(f"slack api error: {data.get('error')}")
            return data["ts"]
