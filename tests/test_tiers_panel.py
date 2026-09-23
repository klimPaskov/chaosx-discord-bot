"""Chaos-tier leaderboard surfaces: public panel, self view, and leaderboard opt-out."""

from __future__ import annotations

from types import SimpleNamespace

import aiosqlite
import pytest

from chaosx_bot.activity import tier_for_xp
from chaosx_bot.bot import ChaosXBot
from chaosx_bot.storage import Store

GENERAL = 1395459672055480344


class _FakeStore:
    """Only the calls the panel makes, with the opt-out set recorded so we can assert on it."""

    def __init__(self, rows, *, opted_out=None, tier=None, rank=None, week_rank=None, bonus=0.0):
        self.rows = rows
        self.opted_out = set(opted_out or set())
        self.tier = tier
        self.bonus = bonus
        self.rank = rank
        self.week_rank = week_rank
        self.calls: list[dict] = []
        self.prefs = {"banter_optout": False, "leaderboard_optout": False}

    async def activity_xp_by_member(self):
        return {int(row[0]): float(row[2]) for row in self.rows}

    async def top_members(self, *, limit, since_day=None, exclude_ids=None):
        self.calls.append({"limit": limit, "since_day": since_day, "exclude_ids": set(exclude_ids or set())})
        return [row for row in self.rows if int(row[0]) not in self.opted_out][:limit]

    async def opted_out_members(self, field="leaderboard_optout"):
        return set(self.opted_out)

    async def member_tier(self, user_id):
        return self.tier

    async def member_rank(self, user_id, *, since_day=None):
        return self.week_rank if since_day else self.rank

    async def bonus_xp_total(self, user_id):
        return float(self.bonus or 0.0)

    async def set_member_pref(self, user_id, field, value):
        self.prefs[field] = value


def _bot(store):
    """A real instance so unbound methods can call each other, without running __init__ (no gateway)."""
    bot = ChaosXBot.__new__(ChaosXBot)
    bot.store = store
    return bot


@pytest.mark.asyncio
async def test_panel_text_lists_the_tiers_and_hides_opted_out_members():
    rows = [
        (1, "Hoops McCann", 690.0, 1881, 108),
        (2, "Holly", 520.0, 948, 72),
        (3, "Cristi756", 91.0, 613, 12),
    ]
    store = _FakeStore(rows, opted_out={2})
    text = await ChaosXBot._tier_panel_text(_bot(store), "all")
    assert "🌪️ Chaos tiers" in text
    assert "Calm World (0)" in text and "World Collapse (1000)" in text
    assert "Hoops McCann" in text and "Cristi756" in text
    assert "Holly" not in text  # opted out of the leaderboard
    assert store.calls[0]["exclude_ids"] == {2}
    assert "Chaos Tier" in text and "Calm World" in text
    # every row carries its tier emoji, and the ladder itself is emoji-labelled
    assert "🔥" in text and "🌿" in text and "🌑" in text  # no skulls anywhere (Hoops)
    assert "💀" not in text and "☠️" not in text
    assert "contributions earn far more" in text
    assert "### 🪜 The ladder" in text and "### 🏆 All time" in text  # structure, not flat prose


@pytest.mark.asyncio
async def test_week_scope_uses_the_last_seven_days():
    store = _FakeStore([(1, "Hoops McCann", 29.0, 43, 5)])
    text = await ChaosXBot._tier_panel_text(_bot(store), "week")
    assert "This week" in text
    assert store.calls[0]["since_day"] is not None
    assert "`/tiers scope:all`" in text  # the footer tells you how to switch back
    assert "Hide me / show me" in text


@pytest.mark.asyncio
async def test_empty_leaderboard_says_so_instead_of_printing_nothing():
    store = _FakeStore([])
    text = await ChaosXBot._tier_panel_text(_bot(store), "all")
    assert "no activity recorded yet" in text


@pytest.mark.asyncio
async def test_self_text_reports_tier_rank_and_visibility():
    store = _FakeStore([], opted_out={7}, tier=(690.0, tier_for_xp(690.0)), rank=1, week_rank=2)
    text = await ChaosXBot._tier_self_text(_bot(store), 7)
    assert "Chaos Tier - 90/200 to Total Chaos" in text
    assert "#1 all time" in text and "#2 this week" in text
    assert "hidden from the leaderboard" in text

    store2 = _FakeStore([], tier=None, rank=None, week_rank=None, bonus=0.0)
    plain = await ChaosXBot._tier_self_text(_bot(store2), 8)
    assert "Calm World" in plain
    assert "not ranked yet" in plain and "no activity recorded this week" in plain
    assert "shown on the leaderboard" in plain
    # the self view explains the chat cap, the contribution reward and the perks of this tier
    assert "Chat is capped at" in plain and "from contributions" in plain
    # Calm World perks: the colour plus the written congratulation on every climb
    assert "Calm World colour" in plain and "congratulation" in plain


@pytest.mark.asyncio
async def test_storage_rank_and_opt_out_exclusion(tmp_path):
    store = Store(tmp_path / "chaosx.db")
    await store.init()
    await store.upsert_activity_days(
        [(1, "2026-09-22", 10, 690.0), (2, "2026-09-22", 10, 520.0), (3, "2026-09-22", 5, 91.0)]
    )
    await store.recompute_member_tiers()
    assert await store.member_rank(1) == 1
    await store.upsert_activity_days(
        [(1, "2026-09-22", 10, 690.0), (2, "2026-09-22", 10, 520.0), (3, "2026-09-22", 5, 91.0)]
    )
    assert [row[0] for row in await store.top_members(limit=5)] == [1, 2, 3]
    assert [row[1] for row in await store.top_members(limit=5)] == ["", "", ""]
    assert await store.member_rank(2) == 2
    assert await store.member_rank(99) is None
    # an opt-out removes the member from rankings and their rank
    assert [row[0] for row in await store.top_members(limit=5, exclude_ids={2})] == [1, 3]
    assert await store.member_rank(2, since_day="2026-09-01") == 2
    assert await store.member_rank(4) is None
    async with aiosqlite.connect(store.db_path) as db:
        cur = await db.execute("SELECT xp, tier FROM member_tiers WHERE user_id = 1")
        row = await cur.fetchone()
    assert row[1] == tier_for_xp(float(row[0]))

@pytest.mark.asyncio
async def test_standings_pluralise_a_single_day():
    from chaosx_bot.bot import _tier_standings_lines

    lines = _tier_standings_lines([(1, "groovy", 11.0, 16, 1), (2, "Cristi756", 91.0, 613, 12)])
    assert "16 messages/1 day)" in lines[0]
    assert "613 messages/12 days)" in lines[1]


class _Followup:
    """Records what would be sent, including the view attached to each chunk."""

    def __init__(self):
        self.sent: list[dict] = []

    async def send(self, content, **kwargs):
        self.sent.append({"content": content, **kwargs})


class _Response:
    async def defer(self, **_kwargs):
        return None


class _AuditStore:
    async def audit(self, **_kwargs):
        return None


class _RateLimiter:
    def check(self, **_kwargs):
        return SimpleNamespace(allowed=True, retry_after_seconds=0)


@pytest.mark.asyncio
async def test_command_attaches_the_view_to_the_first_chunk_only():
    from chaosx_bot.bot import TierPanelView, send_scripted_response
    from chaosx_bot.config import Settings

    followup = _Followup()
    bot = SimpleNamespace(
        settings=Settings(discord_token="dummy", allowed_guild_id=2, owner_id=99),
        rate_limiter=_RateLimiter(),
        store=_AuditStore(),
    )
    interaction = SimpleNamespace(
        user=SimpleNamespace(id=1),
        guild_id=2,
        channel_id=3,
        response=_Response(),
        followup=followup,
    )
    view = TierPanelView(SimpleNamespace(), scope="all")
    await send_scripted_response(
        bot,
        interaction,
        command_name="chaosx tiers",
        summary="all",
        render=lambda: "Chaos tiers panel line\n" * 300,  # over 1900 chars, so two chunks
        view=view,
    )
    assert len(followup.sent) >= 2  # long output is sent as several messages
    assert followup.sent[0]["view"] is view
    assert all(call["view"] is None for call in followup.sent[1:])
    assert all(call["allowed_mentions"] is not None for call in followup.sent)


@pytest.mark.asyncio
async def test_sync_callable_returning_a_coroutine_is_awaited():
    """Regression: `render=lambda: bot._tier_panel_text(x)` must not raise.

    The lambda is not a coroutine function, so it ran in a thread and returned a coroutine object;
    `len()` on it raised "object of type 'coroutine' has no len()" and /tiers failed for every user
    (2026-09-23).
    """
    from chaosx_bot.bot import send_scripted_response
    from chaosx_bot.config import Settings

    followup = _Followup()
    bot = SimpleNamespace(
        settings=Settings(discord_token="dummy", allowed_guild_id=2, owner_id=99),
        rate_limiter=_RateLimiter(),
        store=_AuditStore(),
    )
    interaction = SimpleNamespace(
        user=SimpleNamespace(id=1),
        guild_id=2,
        channel_id=3,
        response=_Response(),
        followup=followup,
    )

    async def render_panel() -> str:
        return "## Chaos tiers\npanel body"

    await send_scripted_response(
        bot,
        interaction,
        command_name="chaosx tiers",
        summary="all",
        render=lambda: render_panel(),  # sync callable, async result
    )
    assert followup.sent[0]["content"].startswith("## Chaos tiers")
    assert "scripted command failed" not in followup.sent[0]["content"]


@pytest.mark.asyncio
async def test_self_text_lists_perks_and_splits_chat_from_contributions():
    store = _FakeStore([], tier=(690.0, tier_for_xp(690.0)), rank=1, week_rank=1, bonus=190.0)
    text = await ChaosXBot._tier_self_text(_bot(store), 7)
    assert "500 from chat" in text and "190 from contributions" in text
    assert "priority" in text.lower()  # Rising Chaos and up get the idea priority perk
    assert "🎁" in text
