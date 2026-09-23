"""Dynamic XP rules, member titles and the emoji-quiet tier panel (Hoops 2026-09-23)."""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from chaosx_bot.activity import (  # noqa: E402
    BONUS_FULL_PER_MONTH,
    BONUS_XP,
    CHAT_DAILY_XP_CAP,
    CHAT_DAILY_XP_CAP_MAX,
    LADDER_QUOTE_LINE,
    TIERS,
    TIER_EMOJI,
    chat_daily_cap,
    contribution_xp,
    day_xp,
    has_perk,
    rollup_days,
)
from chaosx_bot.titles import MAX_TITLE_WORDS, clean_title, fallback_title, title_facts_line  # noqa: E402


def test_chat_cap_is_dynamic():
    assert chat_daily_cap(0) == CHAT_DAILY_XP_CAP
    assert chat_daily_cap(3) > chat_daily_cap(0)
    assert chat_daily_cap(99) == CHAT_DAILY_XP_CAP_MAX
    assert chat_daily_cap(-5) == CHAT_DAILY_XP_CAP


def test_contribution_value_drops_after_the_full_allowance():
    base = BONUS_XP["event_idea"]
    assert contribution_xp(base, this_month=0) == base
    assert contribution_xp(base, this_month=BONUS_FULL_PER_MONTH - 1) == base
    assert contribution_xp(base, this_month=BONUS_FULL_PER_MONTH) == base / 2


def test_day_xp_honours_a_custom_ceiling():
    messages = [(f"message number {index} here", 1) for index in range(40)]
    _, base = day_xp(messages)
    _, raised = day_xp(messages, daily_cap=CHAT_DAILY_XP_CAP_MAX + 5)
    assert base <= CHAT_DAILY_XP_CAP
    assert raised > base


def test_rollup_uses_the_per_member_ceiling():
    rows = [(1, "2026-09-23T10:00:00+00:00", 1, "a real sentence here") for _ in range(30)]
    rows += [(2, "2026-09-23T11:00:00+00:00", 1, "another real sentence") for _ in range(30)]
    plain = dict(((uid, xp) for uid, _day, _count, xp in rollup_days(rows)))
    boosted = dict(((uid, xp) for uid, _day, _count, xp in rollup_days(rows, cap_by_user={2: 50.0})))
    assert boosted[2] > plain[2]
    assert boosted[1] == plain[1]


def test_top_tier_is_open_ended():
    # the ladder stops at World Collapse: chaos keeps counting, no further tier appears
    assert TIERS[-1][0] == "World Collapse"
    assert TIERS[-1][1] == 1000


def test_world_collapse_emoji_is_colourful_and_no_skulls_anywhere():
    assert TIER_EMOJI["World Collapse"] == "🎆"
    for emoji in TIER_EMOJI.values():
        assert emoji not in {"💀", "☠️"}


def test_ladder_quote():
    assert LADDER_QUOTE_LINE.startswith("-# ")
    assert "Chaos is a ladder" in LADDER_QUOTE_LINE


def test_credit_shoutout_is_a_chaos_tier_perk():
    assert has_perk("Chaos Tier", "credit_shoutout")
    assert has_perk("World Collapse", "credit_shoutout")
    assert not has_perk("Rising Chaos", "credit_shoutout")


def test_clean_title_strips_noise():
    assert clean_title('"Duke of the Diminishing Returns"') == "Duke of the Diminishing Returns"
    assert clean_title("**Keeper of Six Messages a Day**") == "Keeper of Six Messages a Day"
    assert clean_title("Baron of the Late-Night Questions.\nsecond line") == "Baron of the Late-Night Questions"
    assert clean_title("Warden of <@123>") == ""  # never a ping
    assert clean_title("   ") == ""


def test_clean_title_caps_length():
    long_style = " ".join(["Grand"] * (MAX_TITLE_WORDS + 8))
    assert len(clean_title(long_style).split()) <= MAX_TITLE_WORDS


def test_fallback_title_is_deterministic_and_pompous():
    assert fallback_title(tier="Calm World", messages=0, contributions=0, active_days=0)
    assert fallback_title(tier="Chaos Tier", messages=0, contributions=0, active_days=0) == "Keeper of the Chaos Tier"
    assert fallback_title(tier="Rising Chaos", messages=500, contributions=0, active_days=5) == (
        "Warden of the Endless Conversation"
    )


def test_title_facts_stay_public_and_bounded():
    facts = title_facts_line(
        name="tester", tier="Rising Chaos", chaos=500, messages=120, active_days=9,
        contributions=["event idea", "playtest report"], style="x" * 900,
    )
    assert "tester" in facts and "Rising Chaos" in facts
    assert len(facts) < 700
