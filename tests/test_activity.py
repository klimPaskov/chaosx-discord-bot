"""Chaos tiers and the XP rollup rules."""

from __future__ import annotations

from datetime import datetime, timezone

from chaosx_bot.activity import (
    DEFAULT_ELIGIBLE_TIER,
    TIERS,
    banter_eligible,
    channel_weight,
    day_xp,
    meets_tier,
    message_xp,
    parse_day,
    rollup_days,
    standings_line,
    tier_for_xp,
    tier_progress,
    tier_rollup,
)

BOT_SPAM = 1396551514469699765
OFF_TOPIC = 1525485352742031400
ISSUES = 1490285093955309850
GENERAL = 1395459672055480344


def test_tiers_are_the_mods_chaos_meter():
    assert [name for name, _ in TIERS] == [
        "Calm World",
        "Gathering Storm",
        "Rising Chaos",
        "Chaos Tier",
        "Total Chaos",
        "World Collapse",
    ]
    assert [threshold for _, threshold in TIERS] == [0, 200, 400, 600, 800, 1000]
    assert tier_for_xp(0) == "Calm World"
    assert tier_for_xp(199.9) == "Calm World"
    assert tier_for_xp(200) == "Gathering Storm"
    assert tier_for_xp(400) == "Rising Chaos"
    assert tier_for_xp(600) == "Chaos Tier"
    assert tier_for_xp(800) == "Total Chaos"
    assert tier_for_xp(1000) == "World Collapse"
    assert tier_for_xp(99999) == "World Collapse"
    assert tier_for_xp(-5) == "Calm World"


def test_tier_progress_measures_the_way_to_the_next_tier():
    progress = tier_progress(520)
    assert progress.tier == "Rising Chaos"
    assert progress.next_tier == "Chaos Tier"
    assert progress.xp_into_tier == 120
    assert progress.xp_to_next == 200
    assert progress.label == "Rising Chaos - 120/200 to Chaos Tier"
    # the top tier has nowhere left to climb
    top = tier_progress(1500)
    assert top.next_tier is None and top.label == "World Collapse"


def test_eligibility_floor_is_the_high_tier_hoops_asked_for():
    assert DEFAULT_ELIGIBLE_TIER == "Rising Chaos"
    assert meets_tier(400) and not meets_tier(399)
    assert not meets_tier(0, "Chaos Tier")
    assert meets_tier(600, "Chaos Tier")


def test_message_xp_rules():
    # excluded channels are worth nothing
    assert message_xp("a normal chat message", channel_id=BOT_SPAM, index_in_day=0) == 0.0
    assert message_xp("a normal chat message", channel_id=OFF_TOPIC, index_in_day=0) == 0.0
    assert channel_weight(BOT_SPAM) == 0.0
    # short messages count half
    assert message_xp("ok", channel_id=GENERAL, index_in_day=0) == 0.5
    assert message_xp("this is long enough", channel_id=GENERAL, index_in_day=0) == 1.0
    # helping channels are worth more than chatting
    assert message_xp("here is how you fix it", channel_id=ISSUES, index_in_day=0) == 1.25
    # diminishing returns after the tenth message of a day
    assert message_xp("this is long enough", channel_id=GENERAL, index_in_day=9) == 1.0
    assert message_xp("this is long enough", channel_id=GENERAL, index_in_day=10) == 0.2


def test_day_xp_applies_diminishing_returns_and_the_burst_guard():
    messages = [("a normal chat message", GENERAL)] * 30
    count, xp = day_xp(messages)
    assert count == 30
    # first 10 full, next 10 at 0.2, then nothing from the burst guard
    assert xp == 10 * 1.0 + 10 * 0.2


def test_rollup_groups_per_member_per_day_and_keeps_order():
    rows = [
        (7, "2026-09-22T20:51:00+00:00", GENERAL, "a normal chat message"),
        (7, "2026-09-22T21:02:00+00:00", GENERAL, "another normal message"),
        (7, "2026-09-22T22:00:00+00:00", BOT_SPAM, "spam does not count here"),
        (8, "2026-09-22T10:00:00+00:00", ISSUES, "here is how you fix it"),
        (7, "2026-09-23T09:00:00+00:00", GENERAL, "new day full value"),
    ]
    grouped = {(user, day): (count, xp) for user, day, count, xp in rollup_days(rows)}
    assert grouped[(7, "2026-09-22")] == (3, 2.0)
    assert grouped[(8, "2026-09-22")] == (1, 1.25)
    assert grouped[(7, "2026-09-23")] == (1, 1.0)


def test_parse_day_normalises_timestamps():
    assert parse_day("2026-09-22T23:30:00+03:00") == "2026-09-22"
    assert parse_day("") == ""


def test_banter_eligibility_requires_every_clause():
    good = dict(xp=650, seen_in_channel_days_ago=2)
    assert banter_eligible(**good) is True
    assert banter_eligible(**{**good, "xp": 399}) is False          # below the tier floor
    assert banter_eligible(**{**good, "seen_in_channel_days_ago": 40}) is False
    assert banter_eligible(**{**good, "seen_in_channel_days_ago": None}) is False
    assert banter_eligible(**{**good, "excluded": True}) is False   # Holly / Hoops
    assert banter_eligible(**{**good, "opted_out": True}) is False
    assert banter_eligible(**{**good, "is_bot": True}) is False


def test_standings_and_rollup_formatting():
    progress = tier_progress(650)
    line = standings_line(1, "Hoops McCann", progress, messages=277, days=28)
    assert line == "1. Hoops McCann - Chaos Tier - 50/200 to Total Chaos (650 chaos, 277 messages/28 days)"
    assert tier_rollup([(5, 650.4), (6, 0)]) == [(5, 650.4, "Chaos Tier"), (6, 0.0, "Calm World")]


def test_select_banter_candidates_ranks_and_filters():
    from datetime import datetime, timedelta, timezone

    from chaosx_bot.activity import select_banter_candidates

    now = datetime(2026, 9, 23, 12, 0, tzinfo=timezone.utc)
    recent = (now - timedelta(days=2)).isoformat()
    stale = (now - timedelta(days=40)).isoformat()
    tiers = {1: 650.0, 2: 700.0, 3: 120.0, 4: 900.0, 5: 500.0}
    seen = {1: recent, 2: recent, 3: recent, 4: recent, 5: stale}
    picked = select_banter_candidates(
        tiers=tiers, seen=seen, opted_out={1}, excluded={2}, bots=set(), now=now
    )
    # 1 opted out, 2 excluded (Holly/Hoops), 3 below the tier floor, 5 inactive in the channel
    assert picked == [4]
    assert select_banter_candidates(tiers={}, seen={}, opted_out=set(), excluded=set(), bots=set(), now=now) == []
    assert select_banter_candidates(
        tiers={9: 4000.0}, seen={9: recent}, opted_out=set(), excluded=set(), bots={9}, now=now
    ) == []
