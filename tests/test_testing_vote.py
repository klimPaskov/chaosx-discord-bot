"""The testing ballot: weighted votes, one per member, any candidate that needs testing."""
from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from chaosx_bot.activity import TIERS, VOTING_WEIGHT, cumulative_perks, voting_weight  # noqa: E402
from chaosx_bot.bot import ChaosXBot  # noqa: E402
from chaosx_bot.config import Settings  # noqa: E402
from chaosx_bot.storage import Store  # noqa: E402


def test_voting_weight_scales_with_tier():
    assert voting_weight("Calm World") == 0
    assert voting_weight("Gathering Storm") == 0
    assert voting_weight("Rising Chaos") == 1
    assert voting_weight("Chaos Tier") == 2
    assert voting_weight("Total Chaos") == 2
    assert voting_weight("World Collapse") == 2
    assert voting_weight("not a tier") == 0
    assert set(VOTING_WEIGHT) == {name for name, _threshold in TIERS}


def test_perk_is_advertised_from_rising_chaos_up():
    rising = cumulative_perks("Rising Chaos")
    chaos = cumulative_perks("Chaos Tier")
    assert sum("vote on what gets tested" in perk for perk in rising) == 1
    assert sum("vote on what gets tested" in perk for perk in chaos) == 1
    perk = next(p for p in chaos if "vote on what gets" in p)
    # the weight is advertised as a benefit, never as an exact multiplier (Hoops 2026-09-24)
    assert "extra weight" in perk and "double" not in perk
    assert not any("vote on what gets tested" in perk for perk in cumulative_perks("Gathering Storm"))


@pytest.mark.asyncio
async def test_vote_round_trip_and_weighted_tally(tmp_path):
    store = Store(tmp_path / "poll.db")
    await store.init()
    await store.set_testing_vote(1, "event:6", "Convoy", 2)
    await store.set_testing_vote(2, "event:6", "Convoy", 0)
    await store.set_testing_vote(3, "scenario:2", "Zombies", 1)
    tally = await store.testing_vote_tally()
    assert tally[0] == ("event:6", "Convoy", 2, 2)
    assert tally[1] == ("scenario:2", "Zombies", 1, 1)

    # one vote per member: changing your mind replaces the old vote instead of piling up
    await store.set_testing_vote(1, "scenario:2", "Zombies", 2)
    tally = await store.testing_vote_tally()
    assert tally[0] == ("scenario:2", "Zombies", 2, 3)
    assert tally[1] == ("event:6", "Convoy", 1, 0)
    assert await store.member_testing_vote(1) == ("scenario:2", "Zombies", 2)

    await store.clear_testing_vote(1)
    assert await store.member_testing_vote(1) is None


@pytest.mark.asyncio
async def test_nominations_round_trip(tmp_path):
    store = Store(tmp_path / "noms.db")
    await store.init()
    await store.add_testing_nomination(key="nomination:convoy-payouts", label="Convoy payouts", user_id=7)
    await store.add_testing_nomination(key="nomination:borders", label="Border gore", user_id=7)
    assert [label for _key, label, _detail in await store.testing_nominations()] == [
        "Nomination: Convoy payouts",
        "Nomination: Border gore",
    ]
    assert await store.count_testing_nominations(user_id=7) == 2
    assert await store.count_testing_nominations(user_id=8) == 0
    await store.deactivate_testing_nomination(key="nomination:borders")
    assert [label for _key, label, _detail in await store.testing_nominations()] == ["Nomination: Convoy payouts"]


class _PollStore(Store):
    """Store with a known tier, so the panel text renders without a live database."""

    async def member_tier(self, user_id):  # type: ignore[override]
        return (700.0, "Chaos Tier") if int(user_id) == 2000 else (10.0, "Calm World")


class _Knowledge:
    def testing_candidates(self):
        return {
            "event": [
                ("event:1", "Event 001: Communist Insurgency", "Minor Fire-Once - needs testing"),
                ("event:2", "Event 002: Zombie Outbreak", "Major - needs testing"),
            ],
            "scenario": [("scenario:1", "SCN-001: Zombie Apocalypse", "scenario needs testing")],
            "cluster": [],
        }


@pytest.mark.asyncio
async def test_panel_text_lists_families_and_never_prints_weights(tmp_path):
    store = _PollStore(tmp_path / "panel.db")
    await store.init()
    await store.set_testing_vote(2000, "event:2", "Event 002: Zombie Outbreak", 2)
    await store.add_testing_nomination(key="nomination:convoys", label="Convoy payouts", user_id=5)
    # a real bot instance with a stubbed catalog and database, so the real methods run
    bot = ChaosXBot(Settings(discord_token="dummy"))
    bot.store = store
    bot.knowledge = _Knowledge()

    text = await bot._testing_vote_panel_text(2000)
    assert "What should we test next?" in text
    assert "Events" in text and "Scenarios" in text and "Nominated" in text
    assert "Event 002: Zombie Outbreak" in text and "1 vote" in text  # IDs are visible (Hoops)
    assert "extra weight" in text
    # the exact weight never appears in member-facing text
    assert "weight 2" not in text and "counts 2" not in text and "2 weighted" not in text

    calm = await bot._testing_vote_panel_text(3000)
    assert "You have not voted yet." in calm
    assert "weight 0" not in calm
