"""Retry with exponential backoff + a simple circuit breaker.

Every broker/network call in fx_commodities goes through these. The breaker
exists so a dead terminal doesn't turn the trader into a tight error loop:
after `failure_threshold` consecutive failures the circuit opens and callers
get CircuitOpenError immediately until `cooldown_s` elapses (then one probe
call is allowed through — half-open).
"""

from __future__ import annotations

import functools
import logging
import random
import threading
import time
from typing import Callable, TypeVar

logger = logging.getLogger(__name__)

T = TypeVar("T")


class CircuitOpenError(RuntimeError):
    """Raised when the circuit breaker is open — do not start new work."""


class CircuitBreaker:
    def __init__(self, name: str, failure_threshold: int = 5, cooldown_s: float = 30.0):
        self.name = name
        self.failure_threshold = failure_threshold
        self.cooldown_s = cooldown_s
        self._failures = 0
        self._opened_at: float | None = None
        self._lock = threading.Lock()

    @property
    def is_open(self) -> bool:
        with self._lock:
            if self._opened_at is None:
                return False
            if time.monotonic() - self._opened_at >= self.cooldown_s:
                return False  # half-open: allow a probe
            return True

    def before_call(self) -> None:
        if self.is_open:
            raise CircuitOpenError(f"circuit '{self.name}' is open")

    def record_success(self) -> None:
        with self._lock:
            self._failures = 0
            self._opened_at = None

    def record_failure(self) -> None:
        with self._lock:
            self._failures += 1
            if self._failures >= self.failure_threshold and self._opened_at is None:
                self._opened_at = time.monotonic()
                logger.error(
                    "circuit '%s' OPEN after %d consecutive failures",
                    self.name, self._failures,
                )


def with_retry(
    attempts: int = 3,
    backoff: tuple[float, ...] = (1.0, 2.0, 4.0),
    jitter: bool = True,
    retry_on: tuple[type[BaseException], ...] = (Exception,),
    breaker: CircuitBreaker | None = None,
    sleep: Callable[[float], None] = time.sleep,
) -> Callable[[Callable[..., T]], Callable[..., T]]:
    """Decorator. CircuitOpenError is never retried; it propagates immediately."""

    def decorator(fn: Callable[..., T]) -> Callable[..., T]:
        @functools.wraps(fn)
        def wrapper(*args, **kwargs) -> T:
            last_exc: BaseException | None = None
            for attempt in range(attempts):
                if breaker is not None:
                    breaker.before_call()
                try:
                    result = fn(*args, **kwargs)
                except CircuitOpenError:
                    raise
                except retry_on as exc:
                    last_exc = exc
                    if breaker is not None:
                        breaker.record_failure()
                    if attempt < attempts - 1:
                        delay = backoff[min(attempt, len(backoff) - 1)]
                        if jitter:
                            delay *= 1.0 + random.uniform(-0.2, 0.2)
                        logger.warning(
                            "%s failed (attempt %d/%d): %s — retrying in %.1fs",
                            fn.__qualname__, attempt + 1, attempts, exc, delay,
                        )
                        sleep(delay)
                else:
                    if breaker is not None:
                        breaker.record_success()
                    return result
            assert last_exc is not None
            raise last_exc

        return wrapper

    return decorator
