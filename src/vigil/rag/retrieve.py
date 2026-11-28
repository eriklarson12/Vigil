"""Hybrid retrieval: pgvector cosine + Postgres FTS fused with RRF (spec §7)."""

from typing import Any, Literal
from urllib.parse import urlparse

from psycopg.rows import dict_row
from psycopg_pool import AsyncConnectionPool

from vigil.rag.embed import Embedder, vec_literal

RRF_K = 60
PER_ARM = 20
FETCH = 8       # fetch a few extra, then apply the 2-runbook cap
TOP_CHUNKS = 4
MAX_RUNBOOKS = 2
TAG_BOOST = 1.3

# The keyword arm ORs the alert's terms. plainto_tsquery and websearch_to_tsquery AND
# them, and no chunk holds every word of an alert, so an AND query matches nothing and
# the fusion silently degrades to vector-only. Going through plainto_tsquery keeps the
# query on the same parser and stemmer as the indexed `tsv`.
KEYWORD_TSQUERY = "replace(plainto_tsquery('english', %(qtext)s)::text, ' & ', ' | ')::tsquery"

HYBRID_SQL = f"""
WITH vec AS (
  SELECT id, row_number() OVER (ORDER BY embedding <=> %(qvec)s::vector) AS r
  FROM runbook_chunks ORDER BY embedding <=> %(qvec)s::vector LIMIT %(vec_limit)s
), kw AS (
  SELECT id, row_number() OVER (ORDER BY ts_rank_cd(tsv, q) DESC) AS r
  FROM runbook_chunks, (SELECT {KEYWORD_TSQUERY} AS q) kwq
  WHERE tsv @@ q ORDER BY r LIMIT %(kw_limit)s
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

# The alert's runbook_url names one runbook. Its chunk nearest the query leads the list.
PINNED_SQL = """
SELECT c.id, c.runbook_id, rb.slug AS runbook_slug, rb.title AS runbook_title,
       c.heading_path, c.content
FROM runbook_chunks c
JOIN runbooks rb ON rb.id = c.runbook_id
WHERE rb.slug = %(slug)s
ORDER BY c.embedding <=> %(qvec)s::vector, c.chunk_index
LIMIT 1
"""

Arms = Literal["vector", "keyword", "both"]


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


async def fused_rows(
    pool: AsyncConnectionPool,
    query_vec: list[float],
    query_text: str,
    service: str,
    fetch: int = FETCH,
    arms: Arms = "both",
    tag_boost: float = TAG_BOOST,
) -> list[dict[str, Any]]:
    """Fused ranking before the runbook cap, ordered by `final` DESC.

    Split out of hybrid_search so the retrieval eval (tests/retrieval/) scores the
    same SQL the brief consumes, rather than keeping a second copy of it. `fetch`,
    `arms` and `tag_boost` are knobs for the eval only: production always uses the
    defaults. A disabled arm gets LIMIT 0, so RRF over the other arm alone keeps its order.
    """
    async with pool.connection() as conn:
        async with conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(
                HYBRID_SQL,
                {
                    "qvec": vec_literal(query_vec),
                    "qtext": query_text,
                    "vec_limit": PER_ARM if arms in ("vector", "both") else 0,
                    "kw_limit": PER_ARM if arms in ("keyword", "both") else 0,
                    "rrf_k": RRF_K,
                    "service": service,
                    "tag_boost": tag_boost,
                    "fetch": fetch,
                },
            )
            return await cur.fetchall()


def runbook_slug_from_url(url: Any) -> str | None:
    """The last path segment of an Alertmanager `runbook_url`, or None."""
    if not isinstance(url, str) or not url.strip():
        return None
    segment = urlparse(url.strip()).path.rstrip("/").rsplit("/", 1)[-1]
    return segment or None


async def pinned_chunk(
    pool: AsyncConnectionPool, slug: str, query_vec: list[float]
) -> dict[str, Any] | None:
    """The chunk of runbook `slug` nearest the query, or None when no runbook has that slug."""
    async with pool.connection() as conn:
        async with conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(PINNED_SQL, {"slug": slug, "qvec": vec_literal(query_vec)})
            return await cur.fetchone()


def select_chunks(
    rows: list[dict[str, Any]], pinned: dict[str, Any] | None = None
) -> list[dict[str, Any]]:
    """Top 4 chunks from at most 2 distinct runbooks, the pinned chunk first.

    The pinned chunk counts toward both caps. Retrieval fills the rest, skipping the
    pinned chunk if it ranked too.
    """
    candidates = ([dict(pinned, source="annotation")] if pinned else []) + [
        dict(row, source="retrieval") for row in rows if not pinned or row["id"] != pinned["id"]
    ]
    picked: list[dict[str, Any]] = []
    runbooks_seen: set[str] = set()
    for row in candidates:
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
                # A pinned chunk was looked up, not ranked, so it has no fused score.
                "rrf_score": float(row["final"]) if "final" in row else None,
                "source": row["source"],
            }
        )
        if len(picked) >= TOP_CHUNKS:
            break
    return picked


async def hybrid_search(
    pool: AsyncConnectionPool, embedder: Embedder, alert: dict[str, Any]
) -> list[dict[str, Any]]:
    query_text = build_query_text(alert)
    [query_vec] = await embedder.embed([query_text])
    service = alert.get("labels", {}).get("service") or ""
    rows = await fused_rows(pool, query_vec, query_text, service)

    slug = runbook_slug_from_url(alert.get("annotations", {}).get("runbook_url"))
    pinned = await pinned_chunk(pool, slug, query_vec) if slug else None
    return select_chunks(rows, pinned)
