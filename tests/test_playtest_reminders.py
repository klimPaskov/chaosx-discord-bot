"""Playtest reminder/result-request automation: timing parsing, windows and idempotency."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from chaosx_bot.playtest_reminders import (
    format_reminder,
    format_result_request,
    parse_schedule_json,
    parse_start_time,
    reminder_due,
    result_request_due,
    rows_to_signals,
    strip_schedule_json,
)
from chaosx_bot.storage import Store

UTC = timezone.utc


def test_parse_schedule_json_reads_trailing_block():
    text = (
        "1. Playtest draft — id abc\n"
        "2. Parsed plan — Friday\n"
        '{"start_iso": "2026-09-25T17:00:00+00:00", "duration_minutes": 90, "voice": "Voice 2", "build": "0.1"}'
    )
    timing = parse_schedule_json(text)
    assert timing.parsed is True
    assert timing.start == datetime(2026, 9, 25, 17, 0, tzinfo=UTC)
    assert timing.duration_minutes == 90
    assert timing.voice == "Voice 2"
    assert timing.build == "0.1"


def test_parse_schedule_json_fenced_and_naive():
    text = '```json\n{"start": "2026-09-25T17:00:00", "duration_minutes": "75"}\n```'
    timing = parse_schedule_json(text)
    assert timing.parsed is True
    assert timing.start is not None and timing.start.tzinfo is not None
    assert timing.duration_minutes == 75


def test_parse_schedule_json_without_timing_is_not_parsed():
    assert parse_schedule_json("just a draft, no timing").parsed is False
    assert parse_schedule_json('{"start_iso": "not a date"}').parsed is False
    assert parse_schedule_json('{"start_iso": ""}').parsed is False


def test_strip_schedule_json_removes_block_only():
    body = "1. Playtest draft\n2. Parsed plan\n"
    assert strip_schedule_json(body + '{"start_iso": "2026-09-25T17:00:00+00:00"}').strip() == body.strip()
    assert strip_schedule_json(body) == body.rstrip()
    assert strip_schedule_json(body + "```json\n{\"duration_minutes\": 30}\n```").strip() == body.strip()


def test_parse_start_time_ignores_placeholders():
    assert parse_start_time("AI draft") is None
    assert parse_start_time("") is None
    assert parse_start_time("draft") is None
    parsed = parse_start_time("2026-09-25T17:00:00+00:00")
    assert parsed == datetime(2026, 9, 25, 17, 0, tzinfo=UTC)
    assert parse_start_time("garbage") is None


def test_reminder_due_window():
    start = datetime(2026, 9, 25, 17, 0, tzinfo=UTC)
    assert reminder_due(start=start, now=start - timedelta(minutes=29), lead_minutes=30) is True
    assert reminder_due(start=start, now=start - timedelta(minutes=45), lead_minutes=30) is False
    assert reminder_due(start=start, now=start, lead_minutes=30) is False  # already started
    assert reminder_due(start=None, now=start, lead_minutes=30) is False


def test_result_request_due_window():
    start = datetime(2026, 9, 25, 17, 0, tzinfo=UTC)
    end = start + timedelta(minutes=90)
    assert result_request_due(start=start, duration_minutes=90, now=end + timedelta(minutes=15)) is True
    assert result_request_due(start=start, duration_minutes=90, now=end + timedelta(minutes=5)) is False
    assert result_request_due(start=start, duration_minutes=90, now=end - timedelta(minutes=5)) is False
    assert result_request_due(start=start, duration_minutes=90, now=end + timedelta(hours=20)) is False
    assert result_request_due(start=None, duration_minutes=90, now=end) is False


def test_formatters_have_no_pings_and_show_local_time():
    start = datetime(2026, 9, 25, 17, 0, tzinfo=UTC)
    reminder = format_reminder(
        target="Test Fury", start=start, duration_minutes=90, voice="Voice 2", build="0.1"
    )
    assert "Test Fury" in reminder and "Voice 2" in reminder
    assert "20:00" in reminder  # UTC+3 local time
    assert "@everyone" not in reminder and "@here" not in reminder
    results = format_result_request(target="Test Fury", start=start, duration_minutes=90)
    assert "Test Fury" in results and "2026-09-25 17:00 UTC" in results


def test_rows_to_signals_marks_placeholder_rows_unparsed():
    signals = rows_to_signals(
        [
            {"playtest_id": "a", "target": "t", "start_time": "2026-09-25T17:00:00+00:00", "duration_minutes": 60, "status": "draft"},
            {"playtest_id": "b", "target": "t", "start_time": "AI draft", "duration_minutes": 0, "status": "draft"},
        ]
    )
    assert signals[0]["start"] is not None
    assert signals[0]["duration_minutes"] == 60
    assert signals[1]["start"] is None


@pytest.mark.asyncio
async def test_store_playtest_timing_and_marks_round_trip(tmp_path):
    store = Store(tmp_path / "chaosx.db")
    await store.init()
    await store.create_playtest(
        playtest_id="pt-1",
        actor_id=1,
        guild_id=99,
        channel_id=5,
        target="Test Fury",
        start_time="AI draft",
        duration_minutes=0,
        voice="AI draft",
        build="",
    )
    assert await store.list_scheduled_playtests(guild_id=99) == []

    start_iso = "2026-09-25T17:00:00+00:00"
    await store.update_playtest_schedule(
        playtest_id="pt-1", start_time=start_iso, duration_minutes=90, voice="Voice 2", build="0.1"
    )
    rows = await store.list_scheduled_playtests(guild_id=99)
    assert len(rows) == 1
    assert rows[0]["start_time"] == start_iso
    assert rows[0]["duration_minutes"] == 90
    assert rows[0]["voice"] == "Voice 2"

    assert await store.playtest_automation_marks(kind="reminder") == set()
    await store.mark_playtest_automation(playtest_id="pt-1", kind="reminder")
    await store.mark_playtest_automation(playtest_id="pt-1", kind="reminder")
    assert await store.playtest_automation_marks(kind="reminder") == {"pt-1"}
    assert await store.playtest_automation_marks(kind="results") == set()
