"""Shared integration fixtures. Needs Postgres: docker compose up -d db

Uses FakeLLM/FakeEmbedder/mock Slack via tests/conftest.py env - no network calls.
"""

import pathlib

import httpx
import pytest
from asgi_lifespan import LifespanManager

ROOT = pathlib.Path(__file__).parent.parent.parent


@pytest.fixture()
async def client():
    from vigil.main import create_app
    from vigil.rag.embed import get_embedder, ingest_runbook

    app = create_app()
    async with LifespanManager(app, startup_timeout=60):
        deps = app.state.deps
        # clean slate: previous runs leave incidents/checkpoints behind
        async with deps.pool.connection() as conn:
            await conn.execute(
                "TRUNCATE alerts, incidents, incident_events, commit_candidates,"
                " deploy_events, postmortems, runbooks, runbook_chunks, llm_budget CASCADE"
            )
            for t in ("checkpoints", "checkpoint_blobs", "checkpoint_writes"):
                await conn.execute(f"TRUNCATE {t}")
        # seed runbooks + plant the scenario deploy relative to "now"
        for path in sorted((ROOT / "simulator" / "runbooks").glob("*.md")):
            await ingest_runbook(deps.pool, get_embedder(deps.settings), path)
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
            yield c, deps
