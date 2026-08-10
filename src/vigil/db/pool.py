"""Async connection pool + plain-SQL migration runner.

Neon note: the pool retries opening connections (compute auto-wakes on
connection but the first attempt can hit a cold start).
"""

import asyncio
import pathlib

import structlog
from psycopg_pool import AsyncConnectionPool

log = structlog.get_logger()

MIGRATIONS_DIR = pathlib.Path(__file__).parent / "migrations"


def create_pool(dsn: str) -> AsyncConnectionPool:
    return AsyncConnectionPool(dsn, min_size=1, max_size=5, open=False, kwargs={"autocommit": False})


async def open_pool_with_retry(pool: AsyncConnectionPool, attempts: int = 3, delay: float = 0.5) -> None:
    last: Exception | None = None
    for i in range(attempts):
        try:
            await pool.open(wait=True, timeout=15)
            return
        except Exception as exc:  # noqa: BLE001 - Neon cold start / transient
            last = exc
            log.warning("db_open_retry", attempt=i + 1, error=str(exc))
            await asyncio.sleep(delay * (i + 1))
    raise last  # type: ignore[misc]


async def apply_migrations(pool: AsyncConnectionPool) -> list[str]:
    """Apply pending migrations in filename order; each file runs in one transaction."""
    applied_now: list[str] = []
    async with pool.connection() as conn:
        await conn.execute(
            "CREATE TABLE IF NOT EXISTS schema_migrations ("
            " filename text PRIMARY KEY, applied_at timestamptz NOT NULL DEFAULT now())"
        )
        cur = await conn.execute("SELECT filename FROM schema_migrations")
        already = {row[0] for row in await cur.fetchall()}
    for path in sorted(MIGRATIONS_DIR.glob("*.sql")):
        if path.name in already:
            continue
        async with pool.connection() as conn:  # one tx per migration
            await conn.execute(path.read_text(encoding="utf-8"))
            await conn.execute("INSERT INTO schema_migrations (filename) VALUES (%s)", (path.name,))
        applied_now.append(path.name)
        log.info("migration_applied", file=path.name)
    return applied_now
