"""Edges of the features added today: idea voting, voice speaker attribution, the votes line."""
from __future__ import annotations

import asyncio
import sqlite3
from pathlib import Path

from chaosx_bot.bot import IdeaStatusView, _voice_speaker_line, _with_idea_vote_line
from chaosx_bot.storage import Store


def run(coro):
    return asyncio.run(coro)


async def _store(tmp_path: Path, name: str = "edges.db") -> Store:
    store = Store(tmp_path / name)
    await store.init()
    return store


class _Member:
    def __init__(self, name: str) -> None:
        self.display_name = name


class _Guild:
    def __init__(self, member):
        self._member = member

    def get_member(self, _user_id):
        return self._member


class _Bot:
    def __init__(self, member):
        self._member = member

    def get_guild(self, _guild_id):
        return _Guild(self._member)


def test_vote_on_a_missing_idea_is_refused(tmp_path: Path) -> None:
    async def go() -> None:
        store = await _store(tmp_path)
        assert await store.toggle_idea_vote(submission_id=9999, user_id=5) is None
        assert await store.idea_vote_counts([9999]) == {}

    run(go())


def test_database_from_before_voting_migrates(tmp_path: Path) -> None:
    """A DB created before idea_votes existed must gain the table on startup, keeping votes working."""
    db = tmp_path / "old.db"
    connection = sqlite3.connect(db)
    # the real pre-voting schema (init() also creates indexes, so the columns must exist)
    connection.execute(
        "CREATE TABLE idea_submissions ("
        "id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER NOT NULL, title TEXT NOT NULL, "
        "raw_idea TEXT NOT NULL, draft TEXT, vault_path TEXT, forum_channel_id INTEGER, "
        "forum_message_id INTEGER, status TEXT NOT NULL DEFAULT 'filed', priority INTEGER NOT NULL DEFAULT 0, "
        "reviewer_note TEXT, promoted_path TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL)"
    )
    connection.execute(
        "INSERT INTO idea_submissions (id, user_id, title, raw_idea, status, priority, created_at, updated_at) "
        "VALUES (1, 42, 'older idea', 'raw', 'filed', 0, '2026-09-20T10:00:00+00:00', '2026-09-20T10:00:00+00:00')"
    )
    connection.commit()
    connection.close()

    async def go() -> None:
        store = Store(db)
        await store.init()
        assert await store.toggle_idea_vote(submission_id=1, user_id=7) == (True, 1)
        assert await store.idea_vote_counts([1]) == {1: 1}

    run(go())


def test_repeated_toggles_stay_consistent(tmp_path: Path) -> None:
    async def go() -> None:
        store = await _store(tmp_path)
        idea = await store.create_idea_submission(
            user_id=1, title="toggle", raw_idea="i", draft="d", priority=False
        )
        states = []
        for _ in range(12):
            voted, count = await store.toggle_idea_vote(submission_id=idea, user_id=3)
            states.append((voted, count))
        assert states[0] == (True, 1)
        assert states[-1] == (False, 0)
        assert [count for _, count in states] == [1, 0] * 6

    run(go())


def test_speaker_name_cannot_smuggle_a_mention() -> None:
    line = _voice_speaker_line(_Bot(_Member("@everyone")), 1, 2)
    assert "@everyone" not in line
    assert "everyone" in line  # the plain name is still used
    long_name = _voice_speaker_line(_Bot(_Member("N" * 200)), 1, 2)
    assert len(long_name) < 400


def test_vote_line_never_breaks_the_message_limit() -> None:
    near_limit = "x" * 1990
    assert _with_idea_vote_line(near_limit, 4) == near_limit, "must not trim a member's idea text"
    assert len(_with_idea_vote_line("x" * 100, 4)) < 2000
    assert "🗳️ **Votes:** 4" in _with_idea_vote_line("Filed", 4)
    assert _with_idea_vote_line("", 0) == "🗳️ **Votes:** 0"
    replaced = _with_idea_vote_line("Filed\n🗳️ **Votes:** 1\ntail", 9)
    assert replaced.count("**Votes:**") == 1 and "9" in replaced and "tail" in replaced


def test_view_has_exactly_one_vote_button() -> None:
    view = IdeaStatusView(5)
    ids = [getattr(child, "custom_id", "") for child in view.children]
    assert ids.count("chaosx:ideavote:5") == 1
    assert len(view.children) <= 25, "Discord caps a view at 25 components"
    assert "chaosx:ideavote:6" not in ids, "buttons must not leak across ideas"
