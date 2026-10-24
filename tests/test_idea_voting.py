"""Community voting on filed event ideas."""
from __future__ import annotations

import asyncio
from pathlib import Path

from chaosx_bot.bot import IdeaStatusView, _with_idea_vote_line
from chaosx_bot.storage import Store


def run(coro):
    return asyncio.run(coro)


async def _store(tmp_path: Path) -> Store:
    store = Store(tmp_path / "votes.db")
    await store.init()  # applies SCHEMA, which now includes the idea_votes table
    return store


def test_vote_toggle_counts_and_is_per_member(tmp_path: Path) -> None:
    async def go() -> None:
        store = await _store(tmp_path)
        idea = await store.create_idea_submission(
            user_id=1, title="pirate nation", raw_idea="idea", draft="draft", priority=False
        )

        voted, count = await store.toggle_idea_vote(submission_id=idea, user_id=42)
        assert (voted, count) == (True, 1)

        voted, count = await store.toggle_idea_vote(submission_id=idea, user_id=99)
        assert (voted, count) == (True, 2), "a second member's vote must add, not replace"

        voted, count = await store.toggle_idea_vote(submission_id=idea, user_id=42)
        assert (voted, count) == (False, 1), "pressing again removes that member's vote only"

        voted, count = await store.toggle_idea_vote(submission_id=idea, user_id=42)
        assert (voted, count) == (True, 2)

    run(go())


def test_vote_counts_batch(tmp_path: Path) -> None:
    async def go() -> None:
        store = await _store(tmp_path)
        first = await store.create_idea_submission(user_id=1, title="a", raw_idea="a", draft="a", priority=False)
        second = await store.create_idea_submission(user_id=1, title="b", raw_idea="b", draft="b", priority=False)
        for voter in (11, 12, 13):
            await store.toggle_idea_vote(submission_id=first, user_id=voter)
        await store.toggle_idea_vote(submission_id=second, user_id=11)

        counts = await store.idea_vote_counts([first, second, 999])
        assert counts == {first: 3, second: 1}, counts
        assert await store.idea_vote_counts([]) == {}

    run(go())


def test_idea_view_offers_a_public_vote_button() -> None:
    view = IdeaStatusView(7)
    ids = {getattr(child, "custom_id", "") for child in view.children}
    assert "chaosx:ideavote:7" in ids
    assert any(getattr(child, "custom_id", "").startswith("chaosx:idea:7:") for child in view.children)


def test_vote_line_is_replaced_not_duplicated() -> None:
    body = "Filed\n🗳️ **Votes:** 0\n-# Submission `#7`"
    once = _with_idea_vote_line(body, 3)
    assert once.count("**Votes:**") == 1
    assert "🗳️ **Votes:** 3" in once
    twice = _with_idea_vote_line(once, 4)
    assert twice.count("**Votes:**") == 1 and "4" in twice

    added = _with_idea_vote_line("body without a votes line", 2)
    assert added.endswith("🗳️ **Votes:** 2")
