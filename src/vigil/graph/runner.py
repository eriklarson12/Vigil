"""Execution under scale-to-zero (spec §13.4, ADR-006).

- Inline-after-ACK: the webhook handler kicks a drain task in-process.
- Claiming: SKIP LOCKED with stale-claim reclaim (ingest/queue.py).
- Resume: /internal/resume reclaims stranded work; a graph thread with an
  existing checkpoint is RESUMED (ainvoke(None, config)) — no duplicate LLM
  spend — then pending postmortems run, then prune() trims the database.
"""

import asyncio
from typing import Any

import structlog
from psycopg.rows import dict_row
from psycopg_pool import AsyncConnectionPool

from vigil.graph.deps import Deps
from vigil.graph.postmortem import build_postmortem_graph
from vigil.graph.triage import build_triage_graph
from vigil.ingest.queue import claim_next, mark_alert

log = structlog.get_logger()

PRUNE_SQL = [
    # 1. Checkpoints are the #1 growth vector: drop them 7 days post-resolution.
    """
    DELETE FROM checkpoints WHERE thread_id IN (
      SELECT 'triage:' || id::text FROM incidents
      WHERE status <> 'open' AND resolved_at < now() - interval '7 days'
      UNION
      SELECT 'pm:' || id::text FROM incidents
      WHERE status <> 'open' AND resolved_at < now() - interval '7 days')
    """,
    """
    DELETE FROM checkpoint_writes WHERE thread_id IN (
      SELECT 'triage:' || id::text FROM incidents
      WHERE status <> 'open' AND resolved_at < now() - interval '7 days'
      UNION
      SELECT 'pm:' || id::text FROM incidents
      WHERE status <> 'open' AND resolved_at < now() - interval '7 days')
    """,
    """
    DELETE FROM checkpoint_blobs WHERE thread_id IN (
      SELECT 'triage:' || id::text FROM incidents
      WHERE status <> 'open' AND resolved_at < now() - interval '7 days'
      UNION
      SELECT 'pm:' || id::text FROM incidents
      WHERE status <> 'open' AND resolved_at < now() - interval '7 days')
    """,
    # 2. Raw webhook payloads: null after 30 days.
    "UPDATE alerts SET raw_payload = NULL WHERE received_at < now() - interval '30 days'"
    " AND raw_payload IS NOT NULL",
    # 3. Unranked candidates for old incidents.
    """
    DELETE FROM commit_candidates WHERE llm_rank IS NULL AND incident_id IN (
      SELECT id FROM incidents WHERE created_at < now() - interval '90 days')
    """,
]


class Runner:
    def __init__(self, deps: Deps):
        self._deps = deps
        self._triage = None
        self._postmortem = None
        self._checkpointer = None
        self._cp_pool: AsyncConnectionPool | None = None
        self._drain_task: asyncio.Task | None = None
        self._bg: set[asyncio.Task] = set()

    async def setup(self) -> None:
        """Build the checkpointer (needs its own autocommit/dict_row pool) and graphs."""
        from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver

        self._cp_pool = AsyncConnectionPool(
            self._deps.settings.database_url,
            min_size=1,
            max_size=3,
            open=False,
            kwargs={"autocommit": True, "row_factory": dict_row, "prepare_threshold": 0},
        )
        await self._cp_pool.open(wait=True, timeout=15)
        self._checkpointer = AsyncPostgresSaver(self._cp_pool)
        await self._checkpointer.setup()
        self._triage = build_triage_graph(self._deps, self._checkpointer)
        self._postmortem = build_postmortem_graph(self._deps, self._checkpointer)

    async def shutdown(self) -> None:
        for task in (self._drain_task, *self._bg):
            if task and not task.done():
                task.cancel()
        if self._cp_pool:
            await self._cp_pool.close()

    # -- kicks (inline-after-ACK) --------------------------------------------

    def kick(self) -> None:
        if self._drain_task is None or self._drain_task.done():
            self._drain_task = asyncio.create_task(self._drain())

    def kick_postmortem(self, incident_id: str) -> None:
        task = asyncio.create_task(self.run_postmortem(incident_id))
        self._bg.add(task)
        task.add_done_callback(self._bg.discard)

    # -- work ------------------------------------------------------------------

    async def _drain(self) -> None:
        while True:
            row = await claim_next(self._deps.pool, self._deps.settings.stale_claim_minutes)
            if row is None:
                return
            try:
                await self.run_triage(row)
                await mark_alert(self._deps.pool, row["id"], "processed")
            except Exception as exc:  # noqa: BLE001
                log.error("triage_run_failed", alert_id=row["id"], error=str(exc))
                await mark_alert(self._deps.pool, row["id"], "failed")

    async def run_triage(self, alert_row: dict[str, Any]) -> dict[str, Any]:
        incident_id = alert_row["incident_id"]
        config = {"configurable": {"thread_id": f"triage:{incident_id}"}}
        snapshot = await self._triage.aget_state(config)
        if snapshot and snapshot.next:
            log.info("triage_resume", incident_id=incident_id, next=snapshot.next)
            return await self._triage.ainvoke(None, config)  # resume — no duplicate LLM spend
        if snapshot and snapshot.values and not snapshot.next:
            log.info("triage_already_complete", incident_id=incident_id)
            return snapshot.values
        initial = {
            "incident_id": incident_id,
            "alert_id": alert_row["id"],
            "alert": {
                "labels": alert_row["labels"],
                "annotations": alert_row["annotations"],
                "starts_at": alert_row["starts_at"].isoformat(),
            },
            "errors": {},
            "llm_calls_used": 0,
        }
        return await self._triage.ainvoke(initial, config)

    async def run_postmortem(self, incident_id: str) -> None:
        async with self._deps.pool.connection() as conn:
            cur = await conn.execute(
                "SELECT status FROM incidents WHERE id = %s", (incident_id,)
            )
            row = await cur.fetchone()
        if row and row[0] == "postmortem_done":
            return
        config = {"configurable": {"thread_id": f"pm:{incident_id}"}}
        try:
            snapshot = await self._postmortem.aget_state(config)
            if snapshot and snapshot.next:
                await self._postmortem.ainvoke(None, config)
            else:
                await self._postmortem.ainvoke({"incident_id": incident_id, "errors": {}}, config)
        except Exception as exc:  # noqa: BLE001
            log.error("postmortem_run_failed", incident_id=incident_id, error=str(exc))

    # -- resume tick (spec §13.4) ----------------------------------------------

    async def resume_tick(self) -> dict[str, int]:
        """Reclaim stranded work, run missing postmortems, prune. Idempotent."""
        drained = 0
        while True:
            row = await claim_next(self._deps.pool, self._deps.settings.stale_claim_minutes)
            if row is None:
                break
            drained += 1
            try:
                await self.run_triage(row)
                await mark_alert(self._deps.pool, row["id"], "processed")
            except Exception as exc:  # noqa: BLE001
                log.error("resume_triage_failed", alert_id=row["id"], error=str(exc))
                await mark_alert(self._deps.pool, row["id"], "failed")

        async with self._deps.pool.connection() as conn:
            cur = await conn.execute(
                """
                SELECT i.id FROM incidents i
                WHERE i.status = 'resolved'
                """
            )
            pending_pm = [str(r[0]) for r in await cur.fetchall()]
        for incident_id in pending_pm:
            await self.run_postmortem(incident_id)

        pruned = await self.prune()
        return {"alerts_drained": drained, "postmortems_run": len(pending_pm), "rows_pruned": pruned}

    async def prune(self) -> int:
        total = 0
        for sql in PRUNE_SQL:
            try:
                async with self._deps.pool.connection() as conn:
                    cur = await conn.execute(sql)
                    total += cur.rowcount or 0
            except Exception as exc:  # noqa: BLE001 - e.g. checkpoint tables not yet created
                log.warning("prune_step_skipped", error=str(exc))
        return total
