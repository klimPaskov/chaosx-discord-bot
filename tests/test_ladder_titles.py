"""Ladder titles: only above Chaos Tier, and more of them the higher you climb (Hoops 2026-09-24)."""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import aiosqlite  # noqa: E402

from chaosx_bot.activity import (  # noqa: E402
    TIERS,
    TITLE_MIN_TIER,
    cumulative_perks,
    has_ladder_titles,
    has_perk,
    title_slots_for,
)
from chaosx_bot.storage import Store  # noqa: E402

TIER_NAMES = [name for name, _threshold in TIERS]


def test_no_titles_at_or_below_chaos_tier():
    for name in TIER_NAMES:
        if name == TITLE_MIN_TIER:
            break
        assert title_slots_for(name) == 0, name
        assert has_ladder_titles(name) is False, name
    assert title_slots_for("Chaos Tier") == 0


def test_more_titles_the_higher_the_tier():
    slots = [title_slots_for(name) for name in TIER_NAMES]
    assert slots == sorted(slots), slots  # never fewer titles as you climb
    assert title_slots_for("Total Chaos") == 2
    assert title_slots_for("World Collapse") > title_slots_for("Total Chaos")
    assert has_ladder_titles("World Collapse") is True


def test_unknown_tier_holds_no_titles():
    assert title_slots_for("Eternal Chaos") == 0
    assert title_slots_for("") == 0


def test_titles_are_advertised_only_from_the_threshold_up():
    assert has_perk("Chaos Tier", "ladder_titles") is False
    assert has_perk("Total Chaos", "ladder_titles") is True
    assert has_perk("World Collapse", "ladder_titles") is True
    assert any("title" in perk for perk in cumulative_perks("Total Chaos"))
    assert not any("title" in perk for perk in cumulative_perks("Chaos Tier"))


async def _legacy_store(path: Path) -> Store:
    """A database from before titles became a ladder: one title per member, keyed by user_id."""
    db = await aiosqlite.connect(path)
    await db.execute(
        "CREATE TABLE member_titles (user_id INTEGER PRIMARY KEY, title TEXT NOT NULL, "
        "blurb TEXT NOT NULL DEFAULT '', source TEXT NOT NULL DEFAULT 'model', updated_at TEXT NOT NULL)"
    )
    await db.execute(
        "INSERT INTO member_titles(user_id, title, blurb, source, updated_at) "
        "VALUES (7, 'Duke of Legacy', 'old row', 'model', '2026-01-01T00:00:00+00:00')"
    )
    await db.commit()
    await db.close()
    store = Store(path)
    await store.init()
    return store


def test_single_title_databases_migrate_into_slot_zero(tmp_path):
    async def scenario():
        path = tmp_path / "legacy.db"
        store = await _legacy_store(path)
        assert await store.member_title_slots(7) == ["Duke of Legacy"]
        assert await store.member_title(7) == ("Duke of Legacy", "old row")
        async with aiosqlite.connect(path) as db:
            cur = await db.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table' AND name = 'member_titles'"
            )
            assert await cur.fetchone() is None  # the old table is gone, not left as a second truth
    asyncio.run(scenario())


def test_member_slots_round_trip(tmp_path):
    async def scenario():
        store = Store(tmp_path / "slots.db")
        await store.init()
        assert await store.member_title_slots(42) == []
        await store.set_member_title_slot(user_id=42, slot=0, title="Warden of Quiet Threads")
        await store.set_member_title_slot(user_id=42, slot=1, title="Baron of the Second Wave")
        assert await store.member_title_slots(42) == ["Warden of Quiet Threads", "Baron of the Second Wave"]
        # the leaderboard line keeps using the first title only
        assert await store.member_titles() == {42: "Warden of Quiet Threads"}
        assert await store.all_member_title_slots() == {
            42: ["Warden of Quiet Threads", "Baron of the Second Wave"]
        }
        # rewriting a slot replaces it instead of appending a duplicate
        await store.set_member_title_slot(user_id=42, slot=1, title="Baron of the Third Wave")
        assert (await store.member_title_slots(42))[1] == "Baron of the Third Wave"
        assert await store.clear_member_title_slots(42) == 2
        assert await store.member_title_slots(42) == []
        assert await store.member_titles() == {}
    asyncio.run(scenario())


class _StubBot:
    """Just the surface `_ensure_member_titles` touches - no discord client, no model calls."""

    def __init__(self, store: Store, *, owner_id: int = 111, owner_title: str = "The Host",
                 xp: float = 0.0, tier: str | None = None):
        self.store = store
        self.settings = type("S", (), {"owner_id": owner_id, "owner_title": owner_title})()
        self._xp = xp
        self._tier = tier
        self.generated: list[dict] = []

    def _never_mention_ids(self) -> set[int]:
        return set()

    async def _title_facts_inputs(self, user_id: int):
        return (self._xp, self._tier, [], 12, 4)

    async def _generate_member_title(self, **kwargs):
        self.generated.append(kwargs)
        return f"Title {len(self.generated)} for {kwargs['tier']}"


def _call(stub, user_id: int = 500, name: str = "Member"):
    from chaosx_bot.bot import ChaosXBot

    return ChaosXBot._ensure_member_titles(stub, user_id=user_id, name=name)


def test_member_below_the_threshold_holds_no_titles(tmp_path):
    async def scenario():
        store = Store(tmp_path / "gate.db")
        await store.init()
        # a stored title from the old rules must be removed, not shown
        await store.set_member_title_slot(user_id=500, slot=0, title="Keeper of Calm World")
        stub = _StubBot(store, xp=650.0, tier="Chaos Tier")
        assert await _call(stub) == []
        assert await store.member_title_slots(500) == []
        assert stub.generated == []  # nothing is written for a member below the threshold
    asyncio.run(scenario())


def test_titles_scale_past_the_threshold_and_stay_distinct(tmp_path):
    async def scenario():
        for tier, expected in (("Total Chaos", 2), ("World Collapse", 4)):
            store = Store(tmp_path / f"{expected}.db")
            await store.init()
            stub = _StubBot(store, xp=1000.0, tier=tier)
            titles = await _call(stub)
            assert len(titles) == expected, (tier, titles)
            assert await store.member_title_slots(500) == titles
            # each later title is told what the member already holds, so they cannot repeat
            assert stub.generated[0]["already"] == []
            assert stub.generated[1]["already"] == titles[:1]
            # a second call is a no-op: nothing new is generated
            assert await _call(stub) == titles
            assert len(stub.generated) == expected
    asyncio.run(scenario())


def test_owner_keeps_configured_title_and_is_not_on_the_ladder(tmp_path):
    async def scenario():
        store = Store(tmp_path / "owner.db")
        await store.init()
        stub = _StubBot(store, xp=1500.0, tier="World Collapse")
        assert await _call(stub, user_id=111) == ["The Host"]
        assert stub.generated == []
        assert await store.member_title_slots(111) == ["The Host"]
    asyncio.run(scenario())
