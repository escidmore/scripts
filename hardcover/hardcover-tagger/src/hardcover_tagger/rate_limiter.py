"""Paced rate limiter for Hardcover API requests."""

import time


class RateLimiter:
    """Pace requests evenly to avoid strict rolling-window rate limits."""

    def __init__(self, requests_per_minute: int = 50) -> None:
        if requests_per_minute <= 0:
            msg = "requests_per_minute must be positive"
            raise ValueError(msg)
        self._interval = 60.0 / requests_per_minute
        self._next_request = time.monotonic()

    def acquire(self) -> None:
        """Block until the next request slot is available."""
        now = time.monotonic()
        if now < self._next_request:
            time.sleep(self._next_request - now)
            now = time.monotonic()

        self._next_request = max(now, self._next_request) + self._interval
