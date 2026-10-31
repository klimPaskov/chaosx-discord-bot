"""Occasional spontaneous banter in #bot-spam (Hoops, 2026-10-31).

The bot may now and then open a conversation with a member who has been active recently. Everything about *when*
and *who* is deterministic — this module has no model in it — and the language model only writes the actual line,
from bounded public material.

Rules encoded here:

- the only channel it may post in is #bot-spam;
- it never posts while someone is mid-conversation in that channel, and never when the server is quiet;
- randomised interval (a base gap plus a stable jitter derived from the clock and the target), a daily cap, and
  quiet hours;
- the same member is not targeted twice inside a cooldown, nobody is targeted on the strength of one message, and
  the owner, bots and anyone who opted out are never candidates;
- the line never pings: a mention renders the name without notifying anyone.

The state is a plain dict the caller stores; the clock is passed in, so all of this is testable.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

# A member needs a little history before the bot talks to them, and the server needs to be alive for it to feel
# like a conversation rather than a broadcast.
MIN_CANDIDATE_MESSAGES = 3
MIN_SOURCE_MESSAGES = 6
RECENT_ACTIVITY_HOURS = 6.0
TARGET_WINDOW_HOURS = 48.0
# Never talk over a live conversation in #bot-spam.
BUSY_MINUTES = 12
BUSY_MIN_HUMANS = 2

_SERIOUS_RE = re.compile(
    r"\b(?:died|death|passed away|hospital|cancer|funeral|sick|illness|depressed|depression|suicide|"
    r"emergency|evicted|fired|divorce|breakup|grief|mourning|surgery|diagnosis)\b",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class Activity:
    """One member's recent activity, as counted from public channel history."""

    user_id: int
    display_name: str
    messages: int
    last_seen: datetime
    sample: tuple[str, ...] = ()


@dataclass(frozen=True)
class Decision:
    post: bool
    reason: str
    target: Activity | None = None
    next_allowed_at: datetime | None = None


def _stable_roll(*parts: object) -> float:
    digest = hashlib.sha256("|".join(str(p) for p in parts).encode()).hexdigest()
    return int(digest[:8], 16) / 0xFFFFFFFF


def in_quiet_hours(now: datetime, spec: str) -> bool:
    try:
        start_s, end_s = spec.split("-", 1)
        start, end = int(start_s) % 24, int(end_s) % 24
    except (AttributeError, ValueError):
        return False
    hour = now.hour
    if start == end:
        return False
    if start < end:
        return start <= hour < end
    return hour >= start or hour < end


def _as_utc(value: datetime) -> datetime:
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


def _recent_bot_spam_busy(bot_spam: list[tuple[int, datetime, str]], now: datetime) -> str:
    """A live conversation, or somebody telling the channel about something serious."""
    cutoff = now - timedelta(minutes=BUSY_MINUTES)
    recent = [(uid, when, text) for uid, when, text in bot_spam if _as_utc(when) >= cutoff]
    if len({uid for uid, _w, _t in recent}) >= BUSY_MIN_HUMANS:
        return "conversation in progress"
    if any(_SERIOUS_RE.search(text or "") for _uid, _when, text in recent):
        return "serious message just posted"
    return ""


def plan(
    *,
    now: datetime,
    history: list[tuple[int, datetime, str]],
    bot_spam: list[tuple[int, datetime, str]],
    state: dict,
    enabled: bool,
    quiet_hours: str,
    min_gap_minutes: int,
    max_per_day: int,
    target_cooldown_hours: int,
    excluded_ids: set[int],
    opted_out: set[int],
    names: dict[int, str],
    last_was_ignored: bool = False,
) -> Decision:
    """Decide whether to post now, and about whom. Deterministic: same inputs, same answer."""
    now = _as_utc(now)
    if not enabled:
        return Decision(False, "disabled")
    if in_quiet_hours(now, quiet_hours):
        return Decision(False, "quiet hours")
    if last_was_ignored:
        return Decision(False, "last post got no reply", next_allowed_at=now + timedelta(hours=24))

    last_post = state.get("last_post_at")
    if last_post:
        last = _as_utc(datetime.fromisoformat(last_post))
        gap = timedelta(minutes=min_gap_minutes)
        # Randomised cadence: the base gap plus up to 40% jitter, stable per day so a restart does not reschedule
        # the whole day randomly.
        gap = gap * (1.0 + 0.4 * _stable_roll("gap", now.date(), last.date()))
        if now - last < gap:
            return Decision(False, "gap not elapsed", next_allowed_at=last + gap)

    today = state.get("post_days", {}).get(now.date().isoformat(), []) if isinstance(state.get("post_days"), dict) else []
    if len(today) >= max_per_day:
        return Decision(False, "daily cap reached")

    busy = _recent_bot_spam_busy(bot_spam, now)
    if busy:
        return Decision(False, busy)

    cutoff = now - timedelta(hours=RECENT_ACTIVITY_HOURS)
    counts: dict[int, list[tuple[datetime, str]]] = {}
    for user_id, when, text in history:
        when = _as_utc(when)
        if when < cutoff or user_id in excluded_ids or user_id in opted_out:
            continue
        counts.setdefault(user_id, []).append((when, text or ""))
    if sum(len(v) for v in counts.values()) < MIN_SOURCE_MESSAGES:
        return Decision(False, "not enough recent activity")

    targeted = state.get("targets", {}) if isinstance(state.get("targets"), dict) else {}
    cooldown = timedelta(hours=target_cooldown_hours)
    eligible: list[Activity] = []
    for user_id, rows in counts.items():
        if len(rows) < MIN_CANDIDATE_MESSAGES:
            continue
        last_targeted = targeted.get(str(user_id))
        if last_targeted and now - _as_utc(datetime.fromisoformat(last_targeted)) < cooldown:
            continue
        rows.sort()
        eligible.append(
            Activity(
                user_id=user_id,
                display_name=names.get(user_id) or f"<@{user_id}>",
                messages=len(rows),
                last_seen=rows[-1][0],
                sample=tuple(text[:160] for _when, text in rows[-4:] if text),
            )
        )
    if not eligible:
        return Decision(False, "no eligible member")

    # Weight towards the most recently active, then let the hour decide among the plausible few so the same
    # person is not picked every time.
    eligible.sort(key=lambda a: (a.last_seen, a.messages), reverse=True)
    pool = eligible[:3]
    index = int(_stable_roll("pick", now.strftime("%Y-%m-%dT%H"), min(a.user_id for a in pool)) * len(pool)) % len(pool)
    target = pool[index]
    return Decision(True, "eligible", target=target)
