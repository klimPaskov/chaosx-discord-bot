"""The digest states the dates it covers (Hoops: "what week? What dates does it span to?")."""
from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from chaosx_bot.routine_posts import digest_window_note, with_window_note  # noqa: E402

POST = """🌟 **Weekly Chaos Redux digest — 0.1** 🌟

### 🛠️ This week in the mod
- 🎨 Icons landed.
"""


def test_window_note_names_the_range():
    now = datetime(2026, 9, 23, 13, 0, tzinfo=timezone.utc)
    note = digest_window_note(window_days=7, now=now)
    assert note == "-# Covering 16–23 September 2026 (the last 7 days)"


def test_window_note_handles_a_month_boundary():
    now = datetime(2026, 10, 3, 12, 0, tzinfo=timezone.utc)
    note = digest_window_note(window_days=7, now=now)
    assert note == "-# Covering 26 September – 3 October 2026 (the last 7 days)"


def test_note_is_inserted_under_the_title_and_is_idempotent():
    now = datetime(2026, 9, 23, 13, 0, tzinfo=timezone.utc)
    text = with_window_note(POST, window_days=7, now=now)
    lines = text.splitlines()
    assert lines[0].startswith("🌟 **Weekly Chaos Redux digest")
    assert lines[1] == ""
    assert lines[2] == "-# Covering 16–23 September 2026 (the last 7 days)"
    assert "### 🛠️ This week in the mod" in text
    assert with_window_note(text, window_days=7, now=now) == text
