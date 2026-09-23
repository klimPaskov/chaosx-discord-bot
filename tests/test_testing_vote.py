"""The testing poll: weighted votes, one per member, slots that survive a restart."""
from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from chaosx_bot.activity import TIERS, VOTING_WEIGHT, cumulative_perks, voting_weight  # noqa: E402
from chaosx_bot.bot import ChaosXBot, TestingVoteOptionsView  # noqa: E402
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
    # the upgrade is described once, not repeated as a second "weight" line
    assert sum("vote on what gets tested" in perk for perk in chaos) == 1
    assert "double from Chaos Tier" in chaos[chaos.index(next(p for p in chaos if "vote on what gets" in p))]
    assert not any("vote on what gets tested" in perk for perk in cumulative_perks("Gathering Storm"))


@pytest.mark.asyncio
async def test_vote_round_trip_and_weighted_tally(tmp_path):
    store = Store(tmp_path / "poll.db")
    await store.init()
    await store.set_testing_poll_options([("006", "Event 006: Convoy"), ("012", "Event 012: Zombies")])
    options = await store.testing_poll_options()
    assert options[1] == ("006", "Event 006: Convoy")
    assert options[2] == ("012", "Event 012: Zombies")

    await store.set_testing_vote(1, "006", "Event 006: Convoy", 2)
    await store.set_testing_vote(2, "006", "Event 006: Convoy", 0)
    await store.set_testing_vote(3, "012", "Event 012: Zombies", 1)
    tally = await store.testing_vote_tally()
    assert tally[0] == ("006", "Event 006: Convoy", 2, 2)
    assert tally[1] == ("012", "Event 012: Zombies", 1, 1)

    # one vote per member: changing your mind replaces the old vote instead of piling up
    await store.set_testing_vote(1, "012", "Event 012: Zombies", 2)
    tally = await store.testing_vote_tally()
    assert tally[0] == ("012", "Event 012: Zombies", 2, 3)
    assert tally[1] == ("006", "Event 006: Convoy", 1, 0)
    assert await store.member_testing_vote(1) == ("012", "Event 012: Zombies", 2)


class _PollStore(Store):
    """Store with two known members, so the panel text can be rendered without a live database."""

    TIERS = {2000: "Chaos Tier", 3000: "Calm World"}

    async def member_tier(self, user_id):  # type: ignore[override]
        tier = self.TIERS.get(int(user_id))
        return (700.0, tier) if tier else None


@pytest.mark.asyncio
async def test_panel_text_explains_weight(tmp_path):
    store = _PollStore(tmp_path / "panel.db")
    await store.init()
    await store.set_testing_poll_options([("006", "Event 006: Convoy")])
    await store.set_testing_vote(2000, "006", "Event 006: Convoy", 2)
    bot = SimpleNamespace(store=store)
    bot._testing_vote_panel_text = lambda uid, just_voted="": ChaosXBot._testing_vote_panel_text(
        bot, uid, just_voted=just_voted
    )

    text = await bot._testing_vote_panel_text(2000)
    assert "What should we test next?" in text
    assert "counts **2**" in text and "Chaos Tier" in text
    assert "Event 006: Convoy" in text and "1 voter(s), 2 weighted" in text

    quiet = await bot._testing_vote_panel_text(3000, just_voted="Event 006: Convoy")
    assert "recorded but does not count" in quiet
    assert "Rising Chaos and above carry weight" in quiet

    view = TestingVoteOptionsView(SimpleNamespace(store=store), labels=["Event 006: Convoy"])
    assert [item.custom_id for item in view.children] == [f"chaosx_vote_slot{n}" for n in range(1, 6)]
