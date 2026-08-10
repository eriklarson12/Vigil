"""Hybrid retrieval: pgvector cosine + Postgres FTS fused with RRF (spec §7)."""

from typing import Any

from psycopg.rows import dict_row
from psycopg_pool import AsyncConnectionPool

from vigil.rag.embed import Embedder, vec_literal

RRF_K = 60
PER_ARM = 20
FETCH = 8       # fetch a few extra, then apply the 2-runbook cap
TOP_CHUNKS = 4
MAX_RUNBOOKS = 2
TAG_BOOST = 1.3

HYBRID_SQL = """
WITH vec AS (
  SELECT id, row_number() OVER (ORDER BY embedding <=> %(qvec)s::vector) AS r
  FROM runbook_chunks ORDER BY embedding <=> %(qvec)s::vector LIMIT %(per_arm)s
), kw AS (
  SELECT id, row_number() OVER (ORDER BY ts_rank_cd(tsv, q) DESC) AS r
  FROM runbook_chunks, websearch_to_tsquery('english', %(qtext)s) q
  WHERE tsv @@ q LIMIT %(per_arm)s
), fused AS (
  SELECT id, SUM(1.0 / (%(rrf_k)s + r)) AS rrf
  FROM (SELECT * FROM vec UNION ALL SELECT * FROM kw) u GROUP BY id
)
SELECT c.id, c.runbook_id, rb.slug AS runbook_slug, rb.title AS runbook_title,
       c.heading_path, c.content, f.rrf,
       f.rrf * CASE WHEN %(service)s = ANY(rb.service_tags) THEN %(tag_boost)s ELSE 1.0 END AS final
FROM fused f
JOIN runbook_chunks c ON c.id = f.id
JOIN runbooks rb ON rb.id = c.runbook_id
ORDER BY final DESC LIMIT %(fetch)s
"""


def build_query_text(alert: dict[str, Any]) -> str:
    labels = alert.get("labels", {})
    annotations = alert.get("annotations", {})
    parts = [
        labels.get("alertname", ""),
        labels.get("severity", ""),
        labels.get("service", ""),
        annotations.get("summary", ""),
        annotations.get("description", ""),
    ]
    return " ".join(p for p in parts if p)


async def hybrid_search(
    pool: AsyncConnectionPool, embedder: Embedder, alert: dict[str, Any]
) -> list[dict[str, Any]]:
    query_text = build_query_text(alert)
    [query_vec] = await embedder.embed([query_text])
    service = alert.get("labels", {}).get("service") or ""

    async with pool.connection() as conn:
        async with conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(
                HYBRID_SQL,
                {
                    "qvec": vec_literal(query_vec),
                    "qtext": query_text,
                    "per_arm": PER_ARM,
                    "rrf_k": RRF_K,
                    "service": service,
                    "tag_boost": TAG_BOOST,
                    "fetch": FETCH,
                },
            )
            rows = await cur.fetchall()

    # cap: top 4 chunks from at most 2 distinct runbooks
    picked: list[dict[str, Any]] = []
    runbooks_seen: set[str] = set()
    for row in rows:
        rb = str(row["runbook_id"])
        if rb not in runbooks_seen and len(runbooks_seen) >= MAX_RUNBOOKS:
            continue
        runbooks_seen.add(rb)
        picked.append(
            {
                "runbook_slug": row["runbook_slug"],
                "runbook_title": row["runbook_title"],
                "heading_path": row["heading_path"],
                "content": row["content"],
                "rrf_score": float(row["final"]),
            }
        )
        if len(picked) >= TOP_CHUNKS:
            break
    return picked
