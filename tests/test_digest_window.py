"""The digest covers the last complete ISO week and thanks its top contributors."""
from __future__ import annotations

import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from chaosx_bot.routine_posts import (  # noqa: E402
    digest_window_note,
    iso_week_key,
    iso_week_start,
    last_complete_week,
    thanks_facts_line,
    with_window_note,
)

POST = """🌟 **Weekly Chaos Redux digest — 0.1** 🌟

### 🛠️ This week in the mod
- 🎨 Icons landed.
"""


def test_window_is_the_previous_iso_week():
    # Wednesday 23 September 2026 -> the week Mon 14 to Sun 20 September
    now = datetime(2026, 9, 23, 13, 0, tzinfo=timezone.utc)
    start, end = last_complete_week(now)
    assert start == datetime(2026, 9, 14, tzinfo=timezone.utc)
    assert end == datetime(2026, 9, 21, tzinfo=timezone.utc)
    assert iso_week_key(start) == "2026-W38"
    assert iso_week_key(now) == "2026-W39"


def test_window_on_the_scheduled_monday_is_the_week_that_just_ended():
    monday_noon = datetime(2026, 9, 21, 12, 0, tzinfo=timezone.utc)
    start, end = last_complete_week(monday_noon)
    assert (start, end) == (
        datetime(2026, 9, 14, tzinfo=timezone.utc),
        datetime(2026, 9, 21, tzinfo=timezone.utc),
    )


def test_iso_week_start_is_monday_midnight():
    assert iso_week_start(datetime(2026, 9, 20, 23, 59, tzinfo=timezone.utc)) == datetime(
        2026, 9, 14, tzinfo=timezone.utc
    )
    assert iso_week_start(datetime(2026, 9, 21, 0, 1, tzinfo=timezone.utc)) == datetime(
        2026, 9, 21, tzinfo=timezone.utc
    )


def test_window_note_names_the_week_and_spans_months():
    note = digest_window_note(
        window_start=datetime(2026, 9, 14, tzinfo=timezone.utc),
        window_end=datetime(2026, 9, 21, tzinfo=timezone.utc),
    )
    assert note == "-# Covering Mon 14 – Sun 20 September 2026"
    across = digest_window_note(
        window_start=datetime(2026, 9, 28, tzinfo=timezone.utc),
        window_end=datetime(2026, 10, 5, tzinfo=timezone.utc),
    )
    assert across == "-# Covering Mon 28 September – Sun 4 October 2026"


def test_note_sits_under_the_title_and_is_idempotent():
    start = datetime(2026, 9, 14, tzinfo=timezone.utc)
    end = datetime(2026, 9, 21, tzinfo=timezone.utc)
    text = with_window_note(POST, window_start=start, window_end=end)
    lines = text.splitlines()
    assert lines[0].startswith("🌟 **Weekly Chaos Redux digest")
    assert lines[1] == ""
    assert lines[2] == "-# Covering Mon 14 – Sun 20 September 2026"
    assert with_window_note(text, window_start=start, window_end=end) == text


def test_thanks_line_names_members_and_their_chaos():
    line = thanks_facts_line([{"name": "Hoops McCann", "xp": 690}, {"name": "Holly", "xp": 520}])
    assert line == "Hoops McCann (690 chaos), Holly (520 chaos)"
    assert thanks_facts_line([]) == "no member activity to thank this week"
    assert thanks_facts_line([{"name": "", "xp": 10}]) == "no member activity to thank this week"
