"""Token bucket rate limiter for Hardcover API (60 req/min)."""

import time


class RateLimiter:
    """Token bucket: burst up to `capacity`, then refill at `refill_rate` tokens/sec."""

    def __init__(self, capacity: int = 30, refill_rate: float = 1.0) -> None:
        self._capacity = capacity
        self._refill_rate = refill_rate
        self._tokens = float(capacity)
        self._last_refill = time.monotonic()

    def acquire(self) -> None:
        """Block until a token is available, then consume it."""
        self._refill()
        while self._tokens < 1.0:
            deficit = 1.0 - self._tokens
            time.sleep(deficit / self._refill_rate)
            self._refill()
        self._tokens -= 1.0

    def _refill(self) -> None:
        now = time.monotonic()
        elapsed = now - self._last_refill
        self._tokens = min(self._capacity, self._tokens + elapsed * self._refill_rate)
        self._last_refill = now
