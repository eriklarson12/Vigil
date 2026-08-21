"""Hard-delete one incident and everything hanging off it (operator only).

Counterpart to PRUNE_SQL in graph/runner.py. prune() locates LangGraph
checkpoints by joining back to `incidents`, so dropping an incident row without
its checkpoint rows orphans them permanently on a 0.5 GB free tier. The two
statement sets must be changed together.

Deletes are written out per table rather than left to ON DELETE CASCADE:
`alerts.incident_id` and `postmortems.incident_id` have no cascade (001_init.sql),
so leaning on the FKs would cover only half the children, and rowcounts per table
are what the CLI reports back.
"""

import structlog
from psycopg_pool import AsyncConnectionPool

log = structlog.get_logger()

CHECKPOINT_TABLES = ("checkpoints", "checkpoint_writes", "checkpoint_blobs")

# Children first, parent last. commit_candidates and incident_events would
# cascade on their own; they are listed so this is a complete inventory of what
# an incident owns, independent of how the FKs happen to be declared.
CHILD_TABLES = ("postmortems", "commit_candidates", "incident_events", "alerts")


class IncidentBusy(Exception):
    """Delete refused: work may still be running against this incident."""


async def delete_incident(pool: AsyncConnectionPool, incident_id: str) -> dict[str, int] | None:
    """Return per-table rowcounts, or None when the incident does not exist.

    Raises IncidentBusy when triage may still be in flight. One transaction:
    either every row goes or none does.
    """
    counts: dict[str, int] = {}
    threads = [f"triage:{incident_id}", f"pm:{incident_id}"]

    async with pool.connection() as conn:
        # FOR UPDATE serializes against resolve_incident's UPDATE and against
        # attach_incident's grouping locks, so no alert can be attached to (or a
        # postmortem kicked for) an incident that is being deleted.
        cur = await conn.execute(
            "SELECT status FROM incidents WHERE id = %s FOR UPDATE", (incident_id,)
        )
        row = await cur.fetchone()
        if not row:
            return None
        (status,) = row
        if status == "open":
            raise IncidentBusy("incident is open; resolve it before deleting")

        # An alert can sit at 'claimed' after its incident resolved; the next
        # resume tick reclaims it and re-drives triage against this incident.
        cur = await conn.execute(
            "SELECT 1 FROM alerts WHERE incident_id = %s AND processing_status = 'claimed' LIMIT 1",
            (incident_id,),
        )
        if await cur.fetchone():
            raise IncidentBusy("triage is in flight for this incident; retry after it completes")

        # to_regclass returns NULL for a missing table instead of raising, so the
        # probe cannot abort the transaction the way a DELETE against a missing
        # relation would. The checkpointer creates all three together, so a
        # one-table proxy would lie about a half-applied setup.
        cur = await conn.execute(
            "SELECT %s = (SELECT count(*) FROM unnest(%s::text[]) t"
            " WHERE to_regclass('public.' || t) IS NOT NULL)",
            (len(CHECKPOINT_TABLES), list(CHECKPOINT_TABLES)),
        )
        (checkpoints_exist,) = await cur.fetchone()

        # Table names below are module constants, never request data.
        for table in CHECKPOINT_TABLES:
            if not checkpoints_exist:
                counts[table] = 0
                continue
            cur = await conn.execute(f"DELETE FROM {table} WHERE thread_id = ANY(%s)", (threads,))
            counts[table] = cur.rowcount or 0

        for table in CHILD_TABLES:
            cur = await conn.execute(f"DELETE FROM {table} WHERE incident_id = %s", (incident_id,))
            counts[table] = cur.rowcount or 0

        cur = await conn.execute("DELETE FROM incidents WHERE id = %s", (incident_id,))
        counts["incidents"] = cur.rowcount or 0

    log.info("incident_deleted", incident_id=incident_id, **counts)
    return counts
