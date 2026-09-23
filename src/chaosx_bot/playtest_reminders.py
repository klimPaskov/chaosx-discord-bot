"""Playtest reminders and post-playtest result requests.

`playtest_reminders` and `post_playtest_result_request` have been automation presets since the
beginning, but nothing ever sent them: `/playtest schedule` is draft-only and stored the literal
placeholder ``start_time="AI draft"``. This module gives the preset real teeth:

* the schedule draft now also yields a machine-readable timing block, stored on the playtest row;
* a reminder is due inside a lead window before ``start_time`` and fires once;
* a result request is due shortly after ``start_time + duration`` and fires once;
* both are idempotent through the ``playtest_automation_marks`` table, so restarts cannot double-post.

Nothing here invents timing: if a playtest has no parsed ``start_time``, no reminder is due.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

from .routine_posts import sanitize_post

DEFAULT_LEAD_MINUTES = 30
DEFAULT_GRACE_MINUTES = 10
MAX_REMINDER_CHARS = 900


@dataclass(frozen=True)
class PlaytestTiming:
    start: datetime | None
    duration_minutes: int = 0
    voice: str = ""
    build: str = ""
    parsed: bool = False


def parse_schedule_json(text: str) -> PlaytestTiming:
    """Pull the machine-readable timing block out of a playtest scheduling draft."""
    cleaned = re.sub(r"^```(?:json)?\s*|\s*```$", "", (text or "").strip(), flags=re.IGNORECASE)
    payload = ""
    start_index = cleaned.rfind("{")
    if start_index != -1:
        end_index = cleaned.rfind("}")
        if end_index > start_index:
            payload = cleaned[start_index : end_index + 1]
    if not payload:
        return PlaytestTiming(start=None)
    try:
        data = json.loads(payload)
    except json.JSONDecodeError:
        return PlaytestTiming(start=None)
    if not isinstance(data, dict):
        return PlaytestTiming(start=None)
    raw_start = str(data.get("start_iso") or data.get("start") or "").strip()
    start: datetime | None = None
    if raw_start:
        try:
            start = datetime.fromisoformat(raw_start.replace("Z", "+00:00"))
        except ValueError:
            start = None
    if start is not None and start.tzinfo is None:
        start = start.replace(tzinfo=timezone.utc)
    duration = 0
    try:
        duration = int(data.get("duration_minutes") or 0)
    except (TypeError, ValueError):
        duration = 0
    return PlaytestTiming(
        start=start,
        duration_minutes=max(0, duration),
        voice=str(data.get("voice") or "")[:200],
        build=str(data.get("build") or "")[:200],
        parsed=start is not None,
    )


def strip_schedule_json(text: str) -> str:
    """Remove the trailing machine-readable timing block so the draft reads cleanly."""
    cleaned = (text or "").rstrip()
    fence = re.search(r"```(?:json)?\s*\{.*?\}\s*```\s*$", cleaned, flags=re.DOTALL | re.IGNORECASE)
    if fence:
        return cleaned[: fence.start()].rstrip()
    start = cleaned.rfind("{")
    if start != -1 and cleaned.rfind("}") > start:
        tail = cleaned[start:]
        if "start_iso" in tail or "duration_minutes" in tail:
            return cleaned[:start].rstrip()
    return cleaned


def parse_start_time(value: str | None) -> datetime | None:
    """Read a stored start_time; the placeholder drafts ('AI draft', '') are not timestamps."""
    text = (value or "").strip()
    if not text or text.lower() in {"ai draft", "draft", "tbd"}:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def reminder_due(
    *, start: datetime | None, now: datetime, lead_minutes: int = DEFAULT_LEAD_MINUTES
) -> bool:
    """True inside [start - lead, start) — the tick window before a playtest starts."""
    if start is None:
        return False
    return now >= start - timedelta(minutes=max(1, lead_minutes)) and now < start


def result_request_due(
    *,
    start: datetime | None,
    duration_minutes: int,
    now: datetime,
    grace_minutes: int = DEFAULT_GRACE_MINUTES,
) -> bool:
    """True from shortly after a playtest ends (bounded, so old drafts never fire)."""
    if start is None:
        return False
    end = start + timedelta(minutes=max(0, duration_minutes)) + timedelta(minutes=max(0, grace_minutes))
    return end <= now <= end + timedelta(hours=12)


def format_reminder(
    *,
    target: str,
    start: datetime,
    duration_minutes: int,
    voice: str = "",
    build: str = "",
    local_utc_offset_hours: int = 3,
    results_channel_mention: str = "",
) -> str:
    local = start.astimezone(timezone(timedelta(hours=local_utc_offset_hours)))
    minutes = int((start - datetime.now(timezone.utc)).total_seconds() // 60)
    lines = [
        f"**Playtest reminder — {sanitize_post(target or 'playtest', max_chars=200)}**",
        "",
        f"Starts in about {max(0, minutes)} minutes — {local.strftime('%a %d %b %H:%M')} your time "
        f"({start.strftime('%H:%M')} UTC).",
    ]
    if duration_minutes:
        lines.append(f"Planned length: {duration_minutes} minutes.")
    if voice:
        lines.append(f"Where: {voice}")
    if build:
        lines.append(f"Build: {build}")
    if results_channel_mention:
        lines.append(f"After the run, drop observations in {results_channel_mention}.")
    return sanitize_post("\n".join(lines), max_chars=MAX_REMINDER_CHARS)


def format_result_request(
    *,
    target: str,
    start: datetime,
    duration_minutes: int,
    results_channel_mention: str = "",
) -> str:
    when = start.strftime("%Y-%m-%d %H:%M UTC")
    lines = [
        f"**Playtest results — {sanitize_post(target or 'playtest', max_chars=200)}**",
        "",
        f"The window ({when}"
        + (f", {duration_minutes} min" if duration_minutes else "")
        + ") is over. How did it go?",
        "Reply here with crashes, balance problems, what worked, and what stayed untested"
        + (f" (or use {results_channel_mention})." if results_channel_mention else "."),
    ]
    return sanitize_post("\n".join(lines), max_chars=MAX_REMINDER_CHARS)


def mark_key(playtest_id: str, kind: str) -> str:
    return f"{kind}:{playtest_id}"


def timing_detail(timing: PlaytestTiming) -> str:
    return json.dumps(
        {
            "start_iso": timing.start.isoformat() if timing.start else "",
            "duration_minutes": timing.duration_minutes,
            "voice": timing.voice,
            "build": timing.build,
        },
        ensure_ascii=False,
    )[:1000]


def rows_to_signals(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Normalise stored playtest rows into what the reminder loop needs."""
    signals: list[dict[str, Any]] = []
    for row in rows:
        signals.append(
            {
                "playtest_id": str(row.get("playtest_id") or ""),
                "target": str(row.get("target") or ""),
                "status": str(row.get("status") or ""),
                "start": parse_start_time(str(row.get("start_time") or "")),
                "duration_minutes": int(row.get("duration_minutes") or 0),
                "voice": str(row.get("voice") or ""),
                "build": str(row.get("build") or ""),
            }
        )
    return signals
