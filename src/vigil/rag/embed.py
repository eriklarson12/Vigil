"""Embeddings: gemini-embedding-001 @ 768 Matryoshka dims, or deterministic
fake vectors for offline/test runs (spec §7).

CORRECTNESS TRAP: Matryoshka-truncated vectors are NOT unit-norm. Cosine
distance assumes normalized vectors — always L2-normalize after truncation,
on BOTH the store and query sides. Both embedders here return normalized
vectors, so downstream code never has to think about it.
"""

import hashlib
import math
import pathlib
from typing import Any, Protocol

import structlog

from vigil.config import Settings
from vigil.rag.chunker import chunk_markdown

log = structlog.get_logger()

EMBED_BATCH = 100


def l2_normalize(vec: list[float]) -> list[float]:
    norm = math.sqrt(sum(x * x for x in vec))
    return [x / norm for x in vec] if norm > 0 else vec


class Embedder(Protocol):
    async def embed(self, texts: list[str]) -> list[list[float]]: ...


class GeminiEmbedder:
    def __init__(self, settings: Settings):
        from google import genai

        self._client = genai.Client(api_key=settings.gemini_api_key)
        self._model = settings.embedding_model
        self._dims = settings.embedding_dims

    async def embed(self, texts: list[str]) -> list[list[float]]:
        from google.genai import types

        out: list[list[float]] = []
        for i in range(0, len(texts), EMBED_BATCH):
            batch = texts[i : i + EMBED_BATCH]
            resp = await self._client.aio.models.embed_content(
                model=self._model,
                contents=batch,
                config=types.EmbedContentConfig(output_dimensionality=self._dims),
            )
            out.extend(l2_normalize(list(e.values)) for e in resp.embeddings)
        return out


class FakeEmbedder:
    """Deterministic hash-derived vectors — exercises all SQL/RRF plumbing
    offline. Retrieval *quality* is only meaningful with real embeddings."""

    def __init__(self, dims: int = 768):
        self._dims = dims

    async def embed(self, texts: list[str]) -> list[list[float]]:
        vectors = []
        for text in texts:
            # 768 floats from an expanding hash; word-level salting gives
            # related texts related vectors (shared words -> shared components).
            vec = [0.0] * self._dims
            for token in text.lower().split():
                h = int.from_bytes(hashlib.sha256(token.encode()).digest()[:8], "big")
                vec[h % self._dims] += 1.0
                vec[(h >> 16) % self._dims] += 0.5
            vectors.append(l2_normalize(vec))
        return vectors


def get_embedder(settings: Settings) -> Embedder:
    mode = settings.embeddings_mode
    if mode == "auto":
        mode = "gemini" if settings.gemini_api_key else "fake"
    if mode == "gemini":
        return GeminiEmbedder(settings)
    log.info("embedder_fake_mode")
    return FakeEmbedder(settings.embedding_dims)


def vec_literal(vec: list[float]) -> str:
    """pgvector text literal; cast with %s::vector in SQL."""
    return "[" + ",".join(f"{x:.7f}" for x in vec) + "]"


async def ingest_runbook(pool: Any, embedder: Embedder, path: pathlib.Path) -> dict[str, Any]:
    """Idempotent runbook ingestion: doc + per-chunk content_hash gate re-embedding."""
    raw = path.read_text(encoding="utf-8")
    doc_hash = hashlib.sha256(raw.encode()).hexdigest()
    meta, chunks = chunk_markdown(raw)
    slug = meta.get("slug", path.stem)
    title = meta.get("title", path.stem)
    tags = meta.get("service_tags", [])

    async with pool.connection() as conn:
        cur = await conn.execute("SELECT id, content_hash FROM runbooks WHERE slug = %s", (slug,))
        row = await cur.fetchone()
        if row and row[1] == doc_hash:
            return {"slug": slug, "status": "unchanged", "chunks": len(chunks)}

        if row:
            runbook_id = str(row[0])
            cur = await conn.execute(
                "SELECT chunk_index, content_hash, embedding::text FROM runbook_chunks WHERE runbook_id = %s",
                (runbook_id,),
            )
            existing = {r[0]: (r[1], r[2]) for r in await cur.fetchall()}
            await conn.execute(
                "UPDATE runbooks SET title=%s, service_tags=%s, content_hash=%s, raw_markdown=%s,"
                " updated_at=now() WHERE id=%s",
                (title, tags, doc_hash, raw, runbook_id),
            )
            await conn.execute("DELETE FROM runbook_chunks WHERE runbook_id = %s", (runbook_id,))
        else:
            existing = {}
            cur = await conn.execute(
                "INSERT INTO runbooks (slug, title, service_tags, content_hash, raw_markdown)"
                " VALUES (%s, %s, %s, %s, %s) RETURNING id",
                (slug, title, tags, doc_hash, raw),
            )
            runbook_id = str((await cur.fetchone())[0])

        to_embed = [
            c for c in chunks
            if not (c["chunk_index"] in existing and existing[c["chunk_index"]][0] == c["content_hash"])
        ]
        embeddings = await embedder.embed([c["embed_text"] for c in to_embed]) if to_embed else []
        embedded_iter = iter(embeddings)
        for c in chunks:
            if c["chunk_index"] in existing and existing[c["chunk_index"]][0] == c["content_hash"]:
                vec_text = existing[c["chunk_index"]][1]  # reuse stored vector
            else:
                vec_text = vec_literal(next(embedded_iter))
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
                    vec_text,
                ),
            )
    log.info("runbook_ingested", slug=slug, chunks=len(chunks), re_embedded=len(to_embed))
    return {"slug": slug, "status": "ingested", "chunks": len(chunks), "re_embedded": len(to_embed)}
