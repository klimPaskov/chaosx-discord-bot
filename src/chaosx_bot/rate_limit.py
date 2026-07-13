from __future__ import annotations

from dataclasses import dataclass
from time import monotonic


@dataclass
class RateLimitResult:
    allowed: bool
    retry_after_seconds: int = 0
    remaining: int = 0
    reset_after_seconds: int = 0


class FixedWindowRateLimiter:
    """Small in-memory per-process rate limiter for Discord command abuse control."""

    def __init__(self) -> None:
        self._windows: dict[tuple[str, int], tuple[float, int]] = {}

    def check(self, *, bucket: str, user_id: int, limit: int, window_seconds: int) -> RateLimitResult:
        now = monotonic()
        key = (bucket, int(user_id))
        window_start, count = self._windows.get(key, (now, 0))
        elapsed = now - window_start
        reset_after = max(1, int(window_seconds - elapsed))
        if elapsed >= window_seconds:
            self._windows[key] = (now, 1)
            return RateLimitResult(True, remaining=max(0, limit - 1), reset_after_seconds=window_seconds)
        if count >= limit:
            return RateLimitResult(False, reset_after, remaining=0, reset_after_seconds=reset_after)
        self._windows[key] = (window_start, count + 1)
        return RateLimitResult(True, remaining=max(0, limit - count - 1), reset_after_seconds=reset_after)
