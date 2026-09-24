"""Regression tests: a command must always answer, even when a side effect fails.

Hoops, 2026-09-24: "none of the commands still work for me, i always get this error: The application
did not respond." Two causes, both covered here:

1. `view=None` was passed to `followup.send` on `/help` -> discord.py raises TypeError -> no reply.
2. `store.audit()` was awaited BEFORE the reply, so a transient `database is locked` on the 520MB
   archive aborted the command before anything was sent.
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import discord  # noqa: E402

from chaosx_bot.bot import _view_kwargs, safe_audit, send_scripted_response  # noqa: E402


class FakeResponse:
    def __init__(self) -> None:
        self.deferred = False
        self.messages: list[tuple[str, dict]] = []

    async def defer(self, **kwargs) -> None:
        self.deferred = True

    async def send_message(self, content: str, **kwargs) -> None:
        self.messages.append((content, kwargs))

    def is_done(self) -> bool:
        return self.deferred


class FakeInteraction:
    def __init__(self) -> None:
        self.user = SimpleNamespace(id=4242)
        self.guild_id = 1
        self.channel_id = 2
        self.response = FakeResponse()

        class Followup:
            def __init__(self, outer: "FakeInteraction") -> None:
                self.outer = outer

            async def send(self, content: str, **kwargs) -> None:
                self.outer.response.messages.append((content, kwargs))

        self.followup = Followup(self)


class FakeStore:
    def __init__(self, *, fail: bool = False) -> None:
        self.fail = fail
        self.rows: list[str] = []

    async def audit(self, **kwargs) -> None:
        if self.fail:
            raise RuntimeError("database is locked")
        self.rows.append(str(kwargs.get("command")))


def make_bot(*, audit_fails: bool = False):
    store = FakeStore(fail=audit_fails)
    limiter = SimpleNamespace(check=lambda **kwargs: SimpleNamespace(allowed=True, retry_after_seconds=0))
    settings = SimpleNamespace(
        public_scripted_limit_per_hour=100,
        allowed_guild_id=1,
        owner_id=999,
        # every setting the public gate / response path reads, so the fake cannot pass by accident
        discord_token="",
        hermes_profile="test",
    )
    return SimpleNamespace(store=store, rate_limiter=limiter, settings=settings), store


def test_view_kwargs_never_passes_none():
    # `view=None` raises inside discord.py: "expected view parameter to be of type View or LayoutView".
    assert _view_kwargs(None) == {}
    view = discord.ui.View()
    assert _view_kwargs(view) == {"view": view}


def test_scripted_command_replies_even_when_the_audit_fails():
    bot, _store = make_bot(audit_fails=True)
    interaction = FakeInteraction()
    asyncio.run(
        send_scripted_response(
            bot,
            interaction,
            command_name="chaosx help",
            summary="probe",
            render=lambda: "### Chaos tiers\n- real output",
            view=None,
        )
    )
    sent = [content for content, _kwargs in interaction.response.messages]
    assert sent, "the command sent nothing when the audit write failed"
    assert "real output" in sent[0]
    # and no view kwarg was forced into the send
    assert "view" not in interaction.response.messages[0][1]


def test_auditing_still_happens_when_it_works():
    bot, store = make_bot()
    interaction = FakeInteraction()
    asyncio.run(
        send_scripted_response(
            bot,
            interaction,
            command_name="chaosx tiers",
            summary="all",
            render=lambda: "panel",
        )
    )
    assert store.rows == ["chaosx tiers"]


def test_safe_audit_swallows_store_failures():
    bot, _store = make_bot(audit_fails=True)
    # must not raise
    asyncio.run(safe_audit(bot, interaction=FakeInteraction(), command="probe", summary="s"))


def test_store_audit_never_raises_on_a_broken_database(tmp_path):
    from chaosx_bot.storage import Store

    store = Store(tmp_path / "missing-dir" / "nested" / "chaosx.db")
    # no init(): the parent directory does not exist, so the write cannot succeed
    asyncio.run(
        store.audit(actor_id=1, guild_id=1, channel_id=1, command="probe", summary="x")
    )
