"""Hidden budget for casual replies.

Real project questions are never limited — someone asking how testing works should get an answer the tenth time as
well as the first. What gets limited is the chatter: banter, one-liners, "bro do your job", name-addressed jokes.
That is a fixed number per rolling hour, per member, with **no visible counter and no refusal message**: once the
budget is spent the bot simply stops answering casual messages until the window slides. Designated banter
channels (bot-spam) are exempt, because that is what they are for.
"""

from __future__ import annotations

import time
from collections import deque

WINDOW_SECONDS = 3600
MAX_TRACKED_USERS = 5000

_windows: dict[int, deque[float]] = {}


def _prune(bucket: deque[float], now: float) -> None:
    while bucket and now - bucket[0] > WINDOW_SECONDS:
        bucket.popleft()


def _bucket(user_id: int, now: float) -> deque[float]:
    bucket = _windows.get(user_id)
    if bucket is None:
        if len(_windows) >= MAX_TRACKED_USERS:
            # Drop the emptiest window rather than growing without bound.
            oldest = min(_windows.items(), key=lambda item: item[1][-1] if item[1] else 0.0)
            _windows.pop(oldest[0], None)
        bucket = deque()
        _windows[user_id] = bucket
    _prune(bucket, now)
    return bucket


def remaining(user_id: int, limit: int, *, now: float | None = None) -> int:
    """How many casual replies this member has left in the current hour."""
    if limit <= 0:
        return 10_000
    moment = time.time() if now is None else now
    return max(limit - len(_bucket(user_id, moment)), 0)


def allow(user_id: int, limit: int, *, exempt: bool = False, now: float | None = None) -> bool:
    """Whether a casual reply may be sent. Exempt channels and owner/unlimited never count."""
    if exempt or limit <= 0:
        return True
    moment = time.time() if now is None else now
    _prune(_bucket(user_id, moment), moment)
    return len(_bucket(user_id, moment)) < limit


def note(user_id: int, *, now: float | None = None) -> None:
    """Record that a casual reply was actually sent."""
    moment = time.time() if now is None else now
    _bucket(user_id, moment).append(moment)


def reset(user_id: int | None = None) -> None:
    """Clear one member's window, or all of them (used by tests)."""
    if user_id is None:
        _windows.clear()
    else:
        _windows.pop(user_id, None)
