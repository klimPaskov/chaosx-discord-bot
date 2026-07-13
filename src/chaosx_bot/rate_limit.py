from __future__ import annotations

from dataclasses import dataclass
from time import monotonic


@dataclass
class RateLimitResult:
    allowed: bool
    retry_after_seconds: int = 0


class FixedWindowRateLimiter:
    """Small in-memory per-process rate limiter for Discord command abuse control."""

    def __init__(self) -> None:
        self._windows: dict[tuple[str, int], tuple[float, int]] = {}

    def check(self, *, bucket: str, user_id: int, limit: int, window_seconds: int) -> RateLimitResult:
        now = monotonic()
        key = (bucket, int(user_id))
        window_start, count = self._windows.get(key, (now, 0))
        elapsed = now - window_start
        if elapsed >= window_seconds:
            self._windows[key] = (now, 1)
            return RateLimitResult(True)
        if count >= limit:
            return RateLimitResult(False, max(1, int(window_seconds - elapsed)))
        self._windows[key] = (window_start, count + 1)
        return RateLimitResult(True)
