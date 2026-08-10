"""Alert fingerprinting + incident grouping (spec: tasks/todo.md §5)."""

import hashlib
import json
from typing import Any

from psycopg import AsyncConnection


def alert_fingerprint(alert: dict[str, Any]) -> str:
    """Alertmanager's own fingerprint when present; else a stable label hash."""
    fp = alert.get("fingerprint")
    if fp:
        return str(fp)
    labels = alert.get("labels", {})
    canonical = json.dumps(sorted((str(k), str(v)) for k, v in labels.items()))
    return hashlib.sha256(canonical.encode()).hexdigest()[:16]


async def attach_incident(
    conn: AsyncConnection,
    *,
    fingerprint: str,
    service: str | None,
    alert_name: str,
    grouping_minutes: int = 60,
) -> str:
    """Return the incident id this alert belongs to, creating one if needed.

    Must run inside the same transaction as the alert insert. Row locks on the
    matched incident prevent duplicate incidents from concurrent webhooks.
    """
    # Rule 1: same fingerprint already attached to an open incident.
    cur = await conn.execute(
        """
        SELECT i.id FROM incidents i
        JOIN alerts a ON a.incident_id = i.id
        WHERE a.fingerprint = %s AND i.status = 'open'
        LIMIT 1 FOR UPDATE OF i
        """,
        (fingerprint,),
    )
    row = await cur.fetchone()
    if row:
        return str(row[0])

    # Rule 2: open incident on the same service within the grouping window.
    if service:
        cur = await conn.execute(
            """
            SELECT id FROM incidents
            WHERE service = %s AND status = 'open'
              AND created_at > now() - make_interval(mins => %s)
            ORDER BY created_at DESC
            LIMIT 1 FOR UPDATE
            """,
            (service, grouping_minutes),
        )
        row = await cur.fetchone()
        if row:
            return str(row[0])

    # Rule 3: new incident.
    cur = await conn.execute(
        "INSERT INTO incidents (service, title) VALUES (%s, %s) RETURNING id",
        (service or "unknown", f"{alert_name} on {service or 'unknown service'}"),
    )
    row = await cur.fetchone()
    return str(row[0])
