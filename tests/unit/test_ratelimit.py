"""Sliding-window math for the public-route rate limit (roadmap R12)."""

import pytest

from vigil.ratelimit import SlidingWindowLimiter


class _Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


@pytest.fixture()
def clock() -> _Clock:
    return _Clock()


def test_allows_up_to_the_limit_then_rejects(clock):
    limiter = SlidingWindowLimiter(60, clock=clock)
    assert all(limiter.hit("k") is None for _ in range(60))
    assert limiter.hit("k") == pytest.approx(60.0)


def test_retry_after_counts_down_to_the_oldest_hit_expiring(clock):
    limiter = SlidingWindowLimiter(2, clock=clock)
    limiter.hit("k")
    clock.now += 10
    limiter.hit("k")
    clock.now += 5
    assert limiter.hit("k") == pytest.approx(45.0)


def test_window_slides_one_slot_free_when_the_oldest_expires(clock):
    limiter = SlidingWindowLimiter(2, clock=clock)
    limiter.hit("k")
    clock.now += 10
    limiter.hit("k")
    clock.now += 50  # the first hit is exactly one window old
    assert limiter.hit("k") is None
    assert limiter.hit("k") is not None


def test_rejected_hits_do_not_extend_the_lockout(clock):
    limiter = SlidingWindowLimiter(1, clock=clock)
    limiter.hit("k")
    for _ in range(30):
        clock.now += 1
        assert limiter.hit("k") is not None
    clock.now += 30
    assert limiter.hit("k") is None


def test_keys_are_independent(clock):
    limiter = SlidingWindowLimiter(1, clock=clock)
    assert limiter.hit("webhook") is None
    assert limiter.hit("webhook") is not None
    assert limiter.hit("slack:T1") is None
