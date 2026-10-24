"""A heading's emoji is never repeated by the bullet underneath it (Hoops 2026-09-23)."""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from chaosx_bot.formatting import fix_duplicate_emoji  # noqa: E402
from chaosx_bot.routine_posts import sanitize_post  # noqa: E402

POST = """🌟 **Weekly Chaos Redux digest — 0.1** 🌟

### 🛠️ This week in the mod
- 🎨 Icons and flags led the week.
- 🧊 The zombie model landed.

### 💬 From the community
A quiet week.

### 🔎 What's next
🔎 Testing focus: the zombie outbreak.
"""


def test_duplicate_heading_emoji_is_dropped():
    fixed = fix_duplicate_emoji(POST)
    assert "### 🔎 What's next\nTesting focus: the zombie outbreak." in fixed
    assert "\n🔎 Testing focus" not in fixed
    # the sections whose bullets use their own emoji are untouched
    assert "- 🎨 Icons and flags led the week." in fixed
    assert "- 🧊 The zombie model landed." in fixed
    assert "### 🛠️ This week in the mod" in fixed


def test_sanitize_post_applies_the_guard():
    cleaned = sanitize_post(POST)
    assert "\n🔎 Testing focus" not in cleaned
    assert "Testing focus: the zombie outbreak." in cleaned


def test_other_emoji_under_a_heading_are_kept():
    text = "### 🔊 Sound\n- ⚙️ Scripting work landed too.\n- 🔊 New ambience."
    fixed = fix_duplicate_emoji(text)
    assert fixed == "### 🔊 Sound\n- ⚙️ Scripting work landed too.\n- New ambience."


def test_headings_without_bullets_are_untouched():
    text = "## Title\n### Sub\nPlain paragraph with 🔎 inside."
    assert fix_duplicate_emoji(text) == text
