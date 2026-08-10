"""Postgres-backed daily generation-call budget (ADR-007).

The increment is atomic; being a call or two over at the boundary is fine —
the point is stopping runaway spend during alert storms, not exact accounting.
"""

from psycopg_pool import AsyncConnectionPool


class BudgetExhausted(Exception):
    pass


async def consume_call(pool: AsyncConnectionPool, daily_limit: int) -> int:
    async with pool.connection() as conn:
        cur = await conn.execute(
            """
            INSERT INTO llm_budget (day, calls_used) VALUES (CURRENT_DATE, 1)
            ON CONFLICT (day) DO UPDATE SET calls_used = llm_budget.calls_used + 1
            RETURNING calls_used
            """
        )
        (calls_used,) = await cur.fetchone()
    if calls_used > daily_limit:
        raise BudgetExhausted(f"LLM daily budget exhausted: {calls_used}/{daily_limit}")
    return calls_used
