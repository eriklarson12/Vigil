"""Sliding-window rate limit for the public write endpoints (spec §14, roadmap R12, ADR-014).

In-memory is acceptable only because the Container App runs --max-replicas 1 and Redis is
off the table. A second replica would need the Postgres counter pattern `llm_budget` uses.
A scale-to-zero restart resets every window, which is fine for abuse protection.
"""

import math
import time
from collections import deque
from collections.abc import Callable

from fastapi import HTTPException, Request


class SlidingWindowLimiter:
    def __init__(self, limit: int, window: float = 60.0, clock: Callable[[], float] = time.monotonic):
        self._limit = limit
        self._window = window
        self._clock = clock
        self._hits: dict[str, deque[float]] = {}

    def hit(self, key: str) -> float | None:
        """Record a hit. Returns None if allowed, else seconds until the next one would be."""
        now = self._clock()
        hits = self._hits.setdefault(key, deque())
        while hits and hits[0] <= now - self._window:
            hits.popleft()
        if len(hits) >= self._limit:
            # A rejected hit is not recorded, so hammering at the limit cannot extend the lockout.
            return hits[0] + self._window - now
        hits.append(now)
        return None


def enforce_rate_limit(request: Request, key: str) -> None:
    """Call only AFTER auth: unauthenticated floods would otherwise mint unbounded keys."""
    retry_after = request.app.state.limiter.hit(key)
    if retry_after is not None:
        raise HTTPException(
            status_code=429,
            detail="rate limited",
            headers={"Retry-After": str(max(1, math.ceil(retry_after)))},
        )
