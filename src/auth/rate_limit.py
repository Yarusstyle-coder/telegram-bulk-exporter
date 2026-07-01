"""In-memory sliding-window rate limiter with lockout penalty.

Designed for localhost single-user use. No Redis, no shared state.
"""

from __future__ import annotations

import threading
import time
from collections import deque


class RateLimiter:
    """Sliding-window counter with a hard lockout when the window is exceeded.

    .attempt(key) → True if the call is allowed, False if rate-limited.

    Once a key exceeds `max_attempts` inside `window_seconds`, it is locked for
    `penalty_seconds` regardless of the sliding window. This prevents trivial
    wait-and-retry abuse.
    """

    def __init__(
        self,
        max_attempts: int = 5,
        window_seconds: float = 300.0,
        penalty_seconds: float = 300.0,
    ) -> None:
        if max_attempts <= 0:
            raise ValueError("max_attempts must be positive")
        self.max_attempts = max_attempts
        self.window_seconds = window_seconds
        self.penalty_seconds = penalty_seconds
        self._events: dict[str, deque[float]] = {}
        self._locked_until: dict[str, float] = {}
        self._lock = threading.Lock()

    def attempt(self, key: str) -> bool:
        """Record an attempt. Return True if allowed, False if rate-limited."""
        now = time.monotonic()
        with self._lock:
            until = self._locked_until.get(key, 0.0)
            if until > now:
                return False
            if until and until <= now:
                self._locked_until.pop(key, None)

            dq = self._events.setdefault(key, deque())
            threshold = now - self.window_seconds
            while dq and dq[0] < threshold:
                dq.popleft()

            if len(dq) >= self.max_attempts:
                self._locked_until[key] = now + self.penalty_seconds
                return False

            dq.append(now)
            return True

    def reset(self, key: str) -> None:
        """Clear all state for `key` (e.g. after a successful login)."""
        with self._lock:
            self._events.pop(key, None)
            self._locked_until.pop(key, None)

    def is_locked(self, key: str) -> bool:
        with self._lock:
            return self._locked_until.get(key, 0.0) > time.monotonic()
