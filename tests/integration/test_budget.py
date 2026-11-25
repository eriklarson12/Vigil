"""The daily LLM budget counts the day Google counts (ADR-007).

Google resets free-tier requests-per-day at midnight Pacific. A counter keyed on the
database's date reopens hours early, and every call in that gap is a 429 on the primary
model. The date assertions here can only fail while the UTC and Pacific dates differ,
which is 16:00 or 17:00 to midnight Pacific.

Needs Postgres:  docker compose up -d db && uv run pytest -m integration
"""

from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from vigil.llm.budget import BudgetExhausted, consume_call

pytestmark = pytest.mark.integration


def _pacific_today():
    return datetime.now(ZoneInfo("America/Los_Angeles")).date()


async def test_calls_are_counted_on_the_pacific_day(client):
    _, deps = client
    assert await consume_call(deps.pool, daily_limit=5) == 1
    assert await consume_call(deps.pool, daily_limit=5) == 2
    async with deps.pool.connection() as conn:
        cur = await conn.execute("SELECT day, calls_used FROM llm_budget")
        rows = await cur.fetchall()
    assert rows == [(_pacific_today(), 2)]


async def test_the_call_past_the_limit_is_refused(client):
    _, deps = client
    await consume_call(deps.pool, daily_limit=2)
    await consume_call(deps.pool, daily_limit=2)
    with pytest.raises(BudgetExhausted):
        await consume_call(deps.pool, daily_limit=2)


async def test_stats_reads_the_same_day_the_budget_writes(client):
    c, deps = client
    for _ in range(3):
        await consume_call(deps.pool, daily_limit=20)
    stats = (await c.get("/api/stats")).json()
    assert stats["llm"]["today_used"] == 3
