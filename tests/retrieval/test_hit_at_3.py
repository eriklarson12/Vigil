"""Retrieval quality regression against recorded real embeddings (roadmap R3).

The vectors in tests/fixtures/embeddings/ came from one live Gemini run
(scripts/record_embeddings.py). Replaying them means retrieval *quality* — not just
the SQL plumbing that FakeEmbedder exercises in tests/integration — is regression
tested everywhere, including CI, with no API key.

Needs Postgres:  docker compose up -d db && uv run pytest -m retrieval_live -s

Two gates, both measured on the production fetch depth:
  hit@3  >= 10/11 — the right runbook reaches the brief at all
  rank@1 >= 9/11  — it reaches the brief *first*

rank@1 exists because hit@3 alone is blind to the failure actually observed in
production: an unrelated runbook outranking the right one while both sit in the
top 3. Five of the seven runbooks are tagged `checkout`, so TAG_BOOST cannot
separate them.
"""

import json
import math
import pathlib

import pytest

from vigil.config import get_settings
from vigil.db.pool import apply_migrations, create_pool, open_pool_with_retry
from vigil.rag.chunker import chunk_markdown
from vigil.rag.embed import vec_literal
from vigil.rag.retrieve import build_query_text, fused_rows

ROOT = pathlib.Path(__file__).parent.parent.parent
FIXTURES = ROOT / "tests" / "fixtures" / "embeddings"
RUNBOOKS_DIR = ROOT / "simulator" / "runbooks"
SCENARIOS_DIR = ROOT / "simulator" / "scenarios"

# Every demo scenario is scored here. Adding one means recording its query vector
# (scripts/record_embeddings.py) and adding its row; a scenario with no row is
# silently unmeasured, which is why the recorded-vs-mapping check below exists.
EXPECTED_RUNBOOK = {
    "bad_deploy": "checkout-service",
    "cert_expiry": "tls-certificates",
    "config_typo": "deploy-config",
    "db_migration_lock": "orders-database",
    "dependency_bump": "dependency-upgrades",
    "memory_leak": "inventory-service",
    "ambiguous_latency": "checkout-service",
    "auth_key_rotation": "auth-sessions",
    "hotfix_regression": "orders-database",
    "partial_revert": "checkout-service",
    "shared_db_saturation": "orders-database",
}

HIT_AT_3_MIN = 10
RANK_AT_1_MIN = 9
# Diagnostic only: when a runbook misses the production window, re-query deep enough
# to report where it actually landed instead of just "absent".
DEEP_FETCH = 50

_fixtures_present = (FIXTURES / "chunks.json").exists() and (FIXTURES / "queries.json").exists()

pytestmark = [
    pytest.mark.retrieval_live,
    pytest.mark.skipif(
        not _fixtures_present,
        reason="no recorded embeddings — run: uv run python scripts/record_embeddings.py",
    ),
]


def _load(name: str) -> dict:
    return json.loads((FIXTURES / f"{name}.json").read_text(encoding="utf-8"))


async def _seed(pool, chunk_vectors: dict[str, dict]) -> None:
    """Insert every runbook with its recorded chunk vectors.

    Chunks are re-derived with the real chunker and matched by content_hash, so an
    edited runbook fails here with a clear 're-record' message rather than being
    scored against a vector for text that no longer exists.
    """
    async with pool.connection() as conn:
        await conn.execute("TRUNCATE runbooks, runbook_chunks CASCADE")
        for path in sorted(RUNBOOKS_DIR.glob("*.md")):
            raw = path.read_text(encoding="utf-8")
            meta, chunks = chunk_markdown(raw)
            slug = meta.get("slug", path.stem)
            cur = await conn.execute(
                "INSERT INTO runbooks (slug, title, service_tags, content_hash, raw_markdown)"
                " VALUES (%s, %s, %s, %s, %s) RETURNING id",
                (slug, meta.get("title", slug), meta.get("service_tags", []), "recorded", raw),
            )
            runbook_id = str((await cur.fetchone())[0])
            for c in chunks:
                recorded = chunk_vectors.get(c["content_hash"])
                assert recorded, (
                    f"{slug} chunk {c['chunk_index']} ({c['heading_path']}) has no recorded"
                    " vector — the runbook changed since recording."
                    " Re-run: uv run python scripts/record_embeddings.py"
                )
                await conn.execute(
                    "INSERT INTO runbook_chunks (runbook_id, chunk_index, heading_path, content,"
                    " token_count, content_hash, embedding) VALUES (%s, %s, %s, %s, %s, %s, %s::vector)",
                    (
                        runbook_id,
                        c["chunk_index"],
                        c["heading_path"],
                        c["content"],
                        c["token_count"],
                        c["content_hash"],
                        vec_literal(recorded["vector"]),
                    ),
                )


@pytest.fixture()
async def pool():
    chunks = _load("chunks")
    settings = get_settings()
    assert chunks["dims"] == settings.embedding_dims, (
        f"fixture recorded at {chunks['dims']}d but EMBEDDING_DIMS is"
        f" {settings.embedding_dims} — re-record."
    )
    p = create_pool(settings.database_url)
    await open_pool_with_retry(p)
    await apply_migrations(p)
    await _seed(p, chunks["vectors"])
    yield p
    await p.close()


def _distinct_slugs(rows: list[dict]) -> list[str]:
    seen: list[str] = []
    for row in rows:
        if row["runbook_slug"] not in seen:
            seen.append(row["runbook_slug"])
    return seen


async def _rank_of(pool, scenario: str, recorded: dict, fetch: int | None = None) -> tuple[int, list[str]]:
    """Return (1-based rank of the expected runbook, distinct slugs in fused order).

    Rank is 0 when the runbook does not appear at the given depth.
    """
    kwargs = {"fetch": fetch} if fetch else {}
    rows = await fused_rows(
        pool, recorded["vector"], recorded["query_text"], recorded["service"], **kwargs
    )
    slugs = _distinct_slugs(rows)
    expected = EXPECTED_RUNBOOK[scenario]
    return (slugs.index(expected) + 1 if expected in slugs else 0), slugs


async def test_recorded_queries_match_the_current_query_builder(pool):
    """A change to build_query_text silently invalidates every recorded query vector."""
    queries = _load("queries")["vectors"]
    missing = set(EXPECTED_RUNBOOK) - set(queries)
    assert not missing, f"eval scenarios with no recorded query vector: {sorted(missing)}"
    for scenario, recorded in sorted(queries.items()):
        alert = json.loads((SCENARIOS_DIR / f"{scenario}.json").read_text(encoding="utf-8"))["alert"]
        assert recorded["query_text"] == build_query_text(alert), (
            f"{scenario}: build_query_text changed since recording."
            " Re-run: uv run python scripts/record_embeddings.py"
        )


async def test_recorded_vectors_are_normalized():
    """Matryoshka truncation breaks unit norm, and cosine distance assumes it (CLAUDE.md)."""
    for name in ("chunks", "queries"):
        for key, entry in _load(name)["vectors"].items():
            norm = math.sqrt(sum(x * x for x in entry["vector"]))
            assert norm == pytest.approx(1.0, abs=1e-5), f"{name}/{key}: norm {norm}"


async def test_retrieval_hits_the_right_runbook(pool):
    queries = _load("queries")["vectors"]
    hits, firsts, report = 0, 0, []

    for scenario in sorted(EXPECTED_RUNBOOK):
        rank, slugs = await _rank_of(pool, scenario, queries[scenario])
        if rank == 1:
            firsts += 1
        if 1 <= rank <= 3:
            hits += 1
        deep = ""
        if rank == 0:
            deep_rank, _ = await _rank_of(pool, scenario, queries[scenario], fetch=DEEP_FETCH)
            deep = f" (deep rank {deep_rank or '>' + str(DEEP_FETCH)})"
        report.append(
            f"  {scenario:20} want {EXPECTED_RUNBOOK[scenario]:20}"
            f" rank {rank or '-'}{deep}  top3={slugs[:3]}"
        )

    total = len(EXPECTED_RUNBOOK)
    print(f"\nretrieval eval — hit@3 {hits}/{total}, rank@1 {firsts}/{total}")
    print("\n".join(report))

    assert hits >= HIT_AT_3_MIN, f"hit@3 {hits}/{total} < {HIT_AT_3_MIN}\n" + "\n".join(report)
    assert firsts >= RANK_AT_1_MIN, f"rank@1 {firsts}/{total} < {RANK_AT_1_MIN}\n" + "\n".join(report)


async def test_corrupting_a_vector_breaks_its_retrieval(pool):
    """Negative control: proves the eval scores the stored vectors.

    The FTS arm is silenced with a query that matches no document, so what is measured
    is the pgvector arm alone. The assertion is that the rank *worsens*, not that it
    hits any particular value — the control must fail only when the harness is broken,
    never because retrieval quality is poor.
    """
    scenario = "memory_leak"
    slug = EXPECTED_RUNBOOK[scenario]
    recorded = dict(_load("queries")["vectors"][scenario], query_text="zzqqxx")

    rank_before, _ = await _rank_of(pool, scenario, recorded, fetch=DEEP_FETCH)
    assert rank_before, f"{slug} is unreachable by vector alone — cannot run the control"

    async with pool.connection() as conn:
        # Orthogonal to any real embedding: every recorded vector is dense, this is a
        # single-axis unit vector, so cosine distance to the query goes to ~1.
        await conn.execute(
            "UPDATE runbook_chunks c SET embedding = %s::vector FROM runbooks rb"
            " WHERE rb.id = c.runbook_id AND rb.slug = %s",
            (vec_literal([0.0] * (get_settings().embedding_dims - 1) + [1.0]), slug),
        )

    rank_after, slugs = await _rank_of(pool, scenario, recorded, fetch=DEEP_FETCH)
    assert rank_after == 0 or rank_after > rank_before, (
        f"{slug} ranked {rank_before} before corruption and {rank_after} after"
        f" — the eval is not reading the stored vectors. top3={slugs[:3]}"
    )
