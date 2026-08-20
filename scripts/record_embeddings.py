"""Record real Gemini embeddings for the retrieval eval (roadmap R3).

Run once, with a live GEMINI_API_KEY; the JSON output is committed and every
later eval run replays it offline with no key and no API calls:

    uv run python scripts/record_embeddings.py

Both sides of the hybrid search are recorded — the runbook chunk vectors and the
six scenario query vectors — because measuring one against fake vectors for the
other measures nothing.

Re-run this after editing a runbook or `build_query_text`: the eval keys chunks
by content_hash and asserts the recorded query text, so stale fixtures fail loudly
rather than silently scoring the wrong thing.
"""

import asyncio
import json
import math
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from vigil.config import get_settings  # noqa: E402
from vigil.rag.chunker import chunk_markdown  # noqa: E402
from vigil.rag.embed import GeminiEmbedder  # noqa: E402
from vigil.rag.retrieve import build_query_text  # noqa: E402

RUNBOOKS_DIR = ROOT / "simulator" / "runbooks"
SCENARIOS_DIR = ROOT / "simulator" / "scenarios"
OUT_DIR = ROOT / "tests" / "fixtures" / "embeddings"

# Matches vec_literal() in rag/embed.py, so the fixture holds exactly the precision
# Postgres stores — the eval and production see identical vectors.
PRECISION = 7
NORM_TOL = 1e-6


def _round(vec: list[float]) -> list[float]:
    return [round(x, PRECISION) for x in vec]


def _check_norm(label: str, vec: list[float]) -> None:
    norm = math.sqrt(sum(x * x for x in vec))
    if abs(norm - 1.0) > NORM_TOL:
        raise SystemExit(
            f"{label}: L2 norm {norm:.9f} != 1.0. Matryoshka-truncated vectors must be"
            " normalized (CLAUDE.md); refusing to record."
        )


async def main() -> None:
    settings = get_settings()
    # A FakeEmbedder recording would look identical on disk and prove nothing, so
    # the two ways of getting one are both refused rather than warned about.
    if not settings.gemini_api_key:
        raise SystemExit("GEMINI_API_KEY is not set — this script needs the real embedder.")
    if settings.embeddings_mode == "fake":
        raise SystemExit("EMBEDDINGS_MODE=fake — refusing to record fake vectors.")

    embedder = GeminiEmbedder(settings)

    chunk_texts: list[str] = []
    chunk_meta: list[dict] = []
    for path in sorted(RUNBOOKS_DIR.glob("*.md")):
        meta, chunks = chunk_markdown(path.read_text(encoding="utf-8"))
        for c in chunks:
            chunk_texts.append(c["embed_text"])
            chunk_meta.append(
                {
                    "slug": meta.get("slug", path.stem),
                    "chunk_index": c["chunk_index"],
                    "heading_path": c["heading_path"],
                    "content_hash": c["content_hash"],
                }
            )

    scenarios = sorted(SCENARIOS_DIR.glob("*.json"))
    query_texts: list[str] = []
    query_meta: list[dict] = []
    for path in scenarios:
        alert = json.loads(path.read_text(encoding="utf-8"))["alert"]
        text = build_query_text(alert)
        query_texts.append(text)
        query_meta.append(
            {
                "scenario": path.stem,
                "query_text": text,
                "service": alert.get("labels", {}).get("service", ""),
            }
        )

    print(f"embedding {len(chunk_texts)} chunks + {len(query_texts)} queries "
          f"with {settings.embedding_model} @ {settings.embedding_dims}d…")
    chunk_vecs = await embedder.embed(chunk_texts)
    query_vecs = await embedder.embed(query_texts)

    header = {"model": settings.embedding_model, "dims": settings.embedding_dims}

    chunks_out: dict = dict(header, vectors={})
    for m, vec in zip(chunk_meta, chunk_vecs, strict=True):
        _check_norm(f"{m['slug']}#{m['chunk_index']}", vec)
        chunks_out["vectors"][m["content_hash"]] = dict(m, vector=_round(vec))

    queries_out: dict = dict(header, vectors={})
    for m, vec in zip(query_meta, query_vecs, strict=True):
        _check_norm(m["scenario"], vec)
        queries_out["vectors"][m["scenario"]] = dict(m, vector=_round(vec))

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    for name, data in (("chunks", chunks_out), ("queries", queries_out)):
        out = OUT_DIR / f"{name}.json"
        out.write_text(json.dumps(data, indent=1, sort_keys=True) + "\n", encoding="utf-8")
        print(f"  wrote {out.relative_to(ROOT)} ({len(data['vectors'])} vectors,"
              f" {out.stat().st_size // 1024} KB)")

    print("\nchunks recorded:")
    for m in chunk_meta:
        print(f"  {m['slug']:22} #{m['chunk_index']}  {m['heading_path']}")
    print("\nqueries recorded:")
    for m in query_meta:
        print(f"  {m['scenario']:20} {m['query_text'][:70]}…")


if __name__ == "__main__":
    if sys.platform == "win32":
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    asyncio.run(main())
