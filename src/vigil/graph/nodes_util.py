"""The degradation contract (spec §13.2): every node retries its own transient
failures, then degrades into `errors[node_name]` instead of raising. Retry
policies are therefore implemented HERE, in-node, rather than via LangGraph's
RetryPolicy — a node that raised past retries would fail the whole run, which
is exactly what the contract forbids."""

import asyncio
from collections.abc import Awaitable, Callable
from typing import Any

import structlog

log = structlog.get_logger()

NodeFn = Callable[[dict[str, Any]], Awaitable[dict[str, Any]]]


def degrading(name: str, retries: int = 0, backoff: float = 0.0) -> Callable[[NodeFn], NodeFn]:
    def decorate(fn: NodeFn) -> NodeFn:
        async def wrapper(state: dict[str, Any]) -> dict[str, Any]:
            for attempt in range(retries + 1):
                try:
                    return await fn(state)
                except Exception as exc:  # noqa: BLE001 - contract: degrade, don't crash
                    detail = f"{type(exc).__name__}: {exc}"
                    if attempt < retries:
                        log.warning("node_retry", node=name, attempt=attempt + 1, error=detail)
                        await asyncio.sleep(backoff)
                        continue
                    log.error("node_degraded", node=name, error=detail)
                    return {"errors": {name: detail[:300]}}
            return {"errors": {name: "unreachable"}}

        wrapper.__name__ = name
        return wrapper

    return decorate
