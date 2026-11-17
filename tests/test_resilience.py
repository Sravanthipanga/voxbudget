import asyncio

import pytest

from voxbudget.resilience import (
    BreakerState, CircuitBreaker, CircuitOpenError, StageTimeoutError,
    run_with_timeout,
)


async def _ok():
    return 42


async def _slow():
    await asyncio.sleep(5)
    return 42


async def _boom():
    raise ValueError("provider down")


def test_timeout_raises_stage_timeout():
    with pytest.raises(StageTimeoutError):
        asyncio.run(run_with_timeout(_slow, stage="stt", timeout_ms=30))


def test_success_within_deadline():
    assert asyncio.run(run_with_timeout(_ok, stage="stt", timeout_ms=500)) == 42


def test_breaker_opens_after_threshold():
    b = CircuitBreaker("x", failure_threshold=3)
    for _ in range(3):
        with pytest.raises(ValueError):
            asyncio.run(run_with_timeout(_boom, stage="s", timeout_ms=500, breaker=b))
    assert b.state is BreakerState.OPEN


def test_open_breaker_fails_fast():
    b = CircuitBreaker("x", failure_threshold=1)
    with pytest.raises(ValueError):
        asyncio.run(run_with_timeout(_boom, stage="s", timeout_ms=500, breaker=b))
    with pytest.raises(CircuitOpenError):
        asyncio.run(run_with_timeout(_ok, stage="s", timeout_ms=500, breaker=b))


def test_breaker_half_open_after_reset():
    b = CircuitBreaker("x", failure_threshold=1, reset_timeout_s=10.0)
    b.on_failure(now=100.0)
    assert b.state is BreakerState.OPEN
    assert b.allow(now=105.0) is False
    assert b.allow(now=111.0) is True
    assert b.state is BreakerState.HALF_OPEN


def test_success_resets_breaker():
    b = CircuitBreaker("x", failure_threshold=3)
    b.on_failure()
    assert b.consecutive_failures == 1
    asyncio.run(run_with_timeout(_ok, stage="s", timeout_ms=500, breaker=b))
    assert b.consecutive_failures == 0
    assert b.state is BreakerState.CLOSED


def test_timeout_counts_as_breaker_failure():
    b = CircuitBreaker("x", failure_threshold=5)
    with pytest.raises(StageTimeoutError):
        asyncio.run(run_with_timeout(_slow, stage="stt", timeout_ms=20, breaker=b))
    assert b.consecutive_failures == 1
