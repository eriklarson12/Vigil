"""FastAPI app factory. Lifespan wires the Deps container: pool + migrations,
service catalog, LLM/embedder clients, Slack sender, and the graph Runner."""

import contextlib

import structlog
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from vigil.api.dashboard import router as dashboard_router
from vigil.api.internal import router as internal_router
from vigil.config import get_settings
from vigil.db.pool import apply_migrations, create_pool, open_pool_with_retry
from vigil.graph.deps import Deps
from vigil.graph.runner import Runner
from vigil.impact.catalog import ServiceCatalog
from vigil.ingest.webhook import router as webhook_router
from vigil.llm.client import get_llm_client
from vigil.logging_utils import configure_logging
from vigil.rag.embed import get_embedder
from vigil.slack.interactions import router as slack_router
from vigil.slack.sender import SlackSender

configure_logging()
log = structlog.get_logger()


@contextlib.asynccontextmanager
async def lifespan(app: FastAPI):
    settings = get_settings()
    pool = create_pool(settings.database_url)
    await open_pool_with_retry(pool)
    await apply_migrations(pool)

    deps = Deps(
        settings=settings,
        pool=pool,
        catalog=ServiceCatalog.load(settings.services_file),
        llm=get_llm_client(settings, pool),
        embedder=get_embedder(settings),
        slack=None,  # type: ignore[arg-type]
    )
    deps.slack = SlackSender(settings, pool)
    runner = Runner(deps)
    deps.runner = runner
    await runner.setup()
    app.state.deps = deps
    log.info("vigil_started", slack_mode=settings.slack_mode, github_mode=settings.github_mode)
    try:
        yield
    finally:
        await runner.shutdown()
        await pool.close()


def create_app() -> FastAPI:
    app = FastAPI(title="Vigil", version="0.1.0", lifespan=lifespan)
    settings = get_settings()
    app.add_middleware(
        CORSMiddleware,
        allow_origins=[settings.dashboard_url, "http://localhost:5173"],
        allow_methods=["GET", "POST"],
        allow_headers=["*"],
    )
    app.include_router(webhook_router)
    app.include_router(slack_router)
    app.include_router(internal_router)
    app.include_router(dashboard_router)
    return app


app = create_app()
