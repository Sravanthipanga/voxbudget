"""Timeout handling and circuit breakers — the real-time resilience kit.

Rules this module enforces:

1. **Every stage runs under a deadline.** A stage call is wrapped in
   ``asyncio.wait_for`` with the stage's timeout (budget x factor). A hung
   provider can never consume the whole turn.
2. **Cancellation propagates.** When a stage times out, its coroutine is
   cancelled and downstream stages for that turn are skipped — there is no
   point asking the LLM for a reply to a transcript that never arrived.
3. **Circuit breakers fail fast.** After N consecutive failures a provider's
   breaker opens; calls are rejected immediately (0 ms) instead of each one
   burning its full timeout. After a cool-down, one half-open probe decides
   whether the breaker closes again.
4. **Retries are for idempotent, off-critical-path work only.** On the
   critical path a retry costs a second full stage latency, which almost
   always blows the budget — so the pipeline degrades instead of retrying.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from enum import StrEnum
from typing import TypeVar

T = TypeVar("T")


class StageTimeoutError(TimeoutError):
    def __init__(self, stage: str, timeout_ms: float):
        super().__init__(f"stage '{stage}' exceeded its {timeout_ms:.0f} ms timeout")
        self.stage = stage
        self.timeout_ms = timeout_ms


class CircuitOpenError(RuntimeError):
    def __init__(self, name: str):
        super().__init__(f"circuit breaker open for '{name}' — failing fast")
        self.name = name


class BreakerState(StrEnum):
    CLOSED = "closed"
    OPEN = "open"
    HALF_OPEN = "half_open"


@dataclass
class CircuitBreaker:
    """Classic closed -> open -> half-open breaker, per provider."""

    name: str
    failure_threshold: int = 3
    reset_timeout_s: float = 30.0
    state: BreakerState = BreakerState.CLOSED
    consecutive_failures: int = 0
    _opened_at: float = field(default=0.0, repr=False)

    def allow(self, now: float | None = None) -> bool:
        now = time.monotonic() if now is None else now
        if self.state is BreakerState.OPEN:
            if now - self._opened_at >= self.reset_timeout_s:
                self.state = BreakerState.HALF_OPEN
                return True  # one probe call
            return False
        return True

    def on_success(self) -> None:
        self.consecutive_failures = 0
        self.state = BreakerState.CLOSED

    def on_failure(self, now: float | None = None) -> None:
        now = time.monotonic() if now is None else now
        self.consecutive_failures += 1
        if self.state is BreakerState.HALF_OPEN or (
            self.consecutive_failures >= self.failure_threshold
        ):
            self.state = BreakerState.OPEN
            self._opened_at = now


async def run_with_timeout(
    coro_factory: Callable[[], Awaitable[T]],
    *,
    stage: str,
    timeout_ms: float,
    breaker: CircuitBreaker | None = None,
) -> T:
    """Run one stage under its deadline and (optionally) its circuit breaker.

    Raises StageTimeoutError on deadline, CircuitOpenError when the breaker
    is open. The underlying coroutine is cancelled on timeout — cancellation
    propagates, it is never swallowed.
    """
    if breaker is not None and not breaker.allow():
        raise CircuitOpenError(breaker.name)
    try:
        result = await asyncio.wait_for(coro_factory(), timeout=timeout_ms / 1000.0)
    except TimeoutError as exc:
        if breaker is not None:
            breaker.on_failure()
        raise StageTimeoutError(stage, timeout_ms) from exc
    except asyncio.CancelledError:
        if breaker is not None:
            breaker.on_failure()
        raise
    except Exception:
        if breaker is not None:
            breaker.on_failure()
        raise
    if breaker is not None:
        breaker.on_success()
    return result
