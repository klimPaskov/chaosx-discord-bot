"""Storage roundtrip for the activity rollup and chaos tiers."""

from __future__ import annotations

import aiosqlite
import pytest

from chaosx_bot.activity import rollup_days, tier_for_xp
from chaosx_bot.storage import Store

GENERAL = 1395459672055480344
ISSUES = 1490285093955309850


async def _seed(db_path) -> None:
    async with aiosqlite.connect(db_path) as db:
        await db.execute(
            "CREATE TABLE IF NOT EXISTS message_archive (id INTEGER PRIMARY KEY AUTOINCREMENT, "
            "message_id INTEGER, channel_id INTEGER, author_id INTEGER, author_name TEXT, content TEXT, "
            "created_at TEXT, visibility TEXT)"
        )
        await db.execute(
            "CREATE TABLE IF NOT EXISTS users (user_id INTEGER PRIMARY KEY, display_name TEXT, "
            "first_seen_at TEXT, last_seen_at TEXT, is_bot INTEGER DEFAULT 0, updated_at TEXT)"
        )
        await db.executemany(
            "INSERT INTO message_archive(message_id, channel_id, author_id, author_name, content, created_at, visibility) "
            "VALUES(?, ?, ?, ?, ?, ?, 'public')",
            [
                (1, GENERAL, 7, "Hoops McCann", "a normal chat message", "2026-09-22T20:00:00+00:00"),
                (2, GENERAL, 7, "Hoops McCann", "another normal message", "2026-09-22T20:05:00+00:00"),
                (3, ISSUES, 8, "Siegfried", "here is how you fix that bug", "2026-09-22T11:00:00+00:00"),
            ],
        )
        await db.executemany(
            "INSERT INTO users(user_id, display_name, first_seen_at, last_seen_at, is_bot, updated_at) "
            "VALUES(?, ?, '2026-01-01T00:00:00+00:00', '2026-09-22T20:00:00+00:00', 0, '2026-09-22T20:00:00+00:00')",
            [(7, "Hoops McCann"), (8, "Siegfried")],
        )
        await db.commit()


@pytest.mark.asyncio
async def test_activity_tables_roundtrip(tmp_path):
    store = Store(tmp_path / "chaosx.db")
    await store.init()
    await _seed(store.db_path)

    assert await store.activity_cursor() == 0
    rows = await store.archive_rows_after(0)
    assert [row[0] for row in rows] == [1, 2, 3]  # id, author_id, created_at, channel_id, content

    rolled = rollup_days([(row[1], row[2], row[3], row[4]) for row in rows])
    assert await store.upsert_activity_days(rolled) == 2
    # re-running the same day must replace, not add
    await store.upsert_activity_days(rolled)
    assert await store.recompute_member_tiers() == 2

    totals = await store.member_tier(7)
    assert totals is not None
    assert totals[1] == tier_for_xp(totals[0])

    top = await store.top_members(limit=5)
    by_user = {row[0]: row for row in top}
    assert [row[0] for row in top] == [7, 8]  # two chat messages (2.0) beat one help post (1.25)
    assert by_user[7][1] == "Hoops McCann" and by_user[8][1] == "Siegfried"
    assert by_user[8][2] == pytest.approx(1.25)  # help-channel weight, per message
    assert by_user[8][3] == 1 and by_user[8][4] == 1  # messages, active days
    # a windowed leaderboard only counts days inside the window
    assert [row[0] for row in await store.top_members(limit=5, since_day="2026-09-23")] == []

    await store.set_activity_cursor(3)
    assert await store.activity_cursor() == 3
    assert await store.archive_rows_after(3) == []

    seen = await store.last_seen_in_channel(GENERAL)
    assert 7 in seen and 8 not in seen


@pytest.mark.asyncio
async def test_member_preferences_roundtrip(tmp_path):
    store = Store(tmp_path / "chaosx.db")
    await store.init()
    assert await store.member_prefs(11) == {"banter_optout": False, "leaderboard_optout": False}
    await store.set_member_pref(11, "banter_optout", True)
    assert (await store.member_prefs(11))["banter_optout"] is True
    assert await store.opted_out_members() == {11}
    await store.set_member_pref(11, "banter_optout", False)
    assert await store.opted_out_members() == set()
    with pytest.raises(ValueError):
        await store.set_member_pref(11, "nonsense", True)


@pytest.mark.asyncio
async def test_bot_authors_are_kept_out_of_the_rollup(tmp_path):
    """ChaosX must not rank on its own leaderboard."""
    store = Store(tmp_path / "chaosx.db")
    await store.init()
    await _seed(store.db_path)
    async with aiosqlite.connect(store.db_path) as db:
        await db.execute(
            "INSERT INTO message_archive(message_id, channel_id, author_id, author_name, content, created_at, visibility) "
            "VALUES(9, ?, 1526134739122262077, 'ChaosX', 'a long bot answer that would otherwise count', "
            "'2026-09-22T20:10:00+00:00', 'public')",
            (GENERAL,),
        )
        await db.commit()

    rows = await store.archive_rows_after(0, ignore_ids={1526134739122262077})
    assert [row[1] for row in rows] == [7, 7, 8]
    rolled = rollup_days([(row[1], row[2], row[3], row[4]) for row in rows])
    await store.upsert_activity_days(rolled)
    await store.recompute_member_tiers()
    assert [row[0] for row in await store.top_members(limit=5)] == [7, 8]
    # a day re-roll for an ignored author contributes nothing
    assert await store.archive_day_rows(1526134739122262077, "2026-09-22", ignore_ids={1526134739122262077}) == []
