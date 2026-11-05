"""Handle-level replay of the 2026-11-03 #bot-spam flood through handle_message_ask.

The gate tests prove the maths; this one proves the wiring. It feeds the recorded shape of that
evening through the real handler with a fake Discord client, so a future edit that drops a gate
from the banter branch fails here even though chat_gate stays green.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from typing import Any, cast

import pytest

from chaosx_bot.bot import handle_message_ask
from chaosx_bot.chat_gate import ChatGate
from chaosx_bot.config import Settings

GUILD = 1507371214317027490
BOT_SPAM = 1396551514469699765
BOT_ID = 999
SIEGFRIED = 1156181307504730152
SUSSY = 1125795621782159432


class _Store:
    def __init__(self) -> None:
        self.audits: list[dict[str, Any]] = []

    async def get_message_ask_turn(self, **_kwargs: Any) -> None:
        return None

    async def audit(self, **kwargs: Any) -> None:
        self.audits.append(kwargs)


class _FakeBot:
    def __init__(self) -> None:
        self.settings = Settings(
            discord_token="dummy",
            allowed_guild_id=GUILD,
            banter_channel_ids=str(BOT_SPAM),
            auto_scan_enabled=False,
            automation_reminder_channel_id=None,
        )
        self.user = SimpleNamespace(id=BOT_ID)
        self.store = _Store()
        self.chat = ChatGate()
        self.knowledge = SimpleNamespace()
        self._gate_log: dict[tuple, datetime] = {}
        self._last_reply_at: dict[int, datetime] = {}
        self._last_reply_author: dict[int, int] = {}


class _Author:
    def __init__(self, user_id: int) -> None:
        self.id = user_id
        self.bot = False
        self.display_name = "member"


class _Message:
    def __init__(self, text: str, *, author: int, when: datetime, mentions: list[Any] | None = None) -> None:
        self.id = int(when.timestamp() * 1000) % 10_000_000
        self.content = text
        self.author = _Author(author)
        self.channel = SimpleNamespace(id=BOT_SPAM)
        self.guild = SimpleNamespace(id=GUILD)
        self.mentions = mentions or []
        self.reference = None
        self.webhook_id = None
        self.created_at = when
        self.replies: list[str] = []
        self.attachments: list[Any] = []

    async def reply(self, text: str, **kwargs: Any) -> Any:
        self.replies.append(text)
        sent = SimpleNamespace(id=self.id + 1, content=text)
        return sent


@pytest.fixture()
def bot(monkeypatch: pytest.MonkeyPatch) -> _FakeBot:
    fake = _FakeBot()
    replies: list[str] = []

    async def fake_banter(_bot: Any, message: Any, request: str, _decision: Any) -> None:
        replies.append(request)
        # Mirror what the real banter path records, because the chain check reads it.
        _bot._last_reply_at[BOT_SPAM] = message.created_at
        _bot._last_reply_author[BOT_SPAM] = message.author.id

    monkeypatch.setattr("chaosx_bot.bot.run_mention_banter", fake_banter)
    fake.replies = replies  # type: ignore[attr-defined]
    return fake


NOW = datetime(2026, 11, 3, 21, 12, tzinfo=timezone.utc)


@pytest.mark.asyncio
async def test_unaddressed_flood_in_the_banter_channel_gets_exactly_one_reply(bot: _FakeBot) -> None:
    """169 copies of one line in #bot-spam: one answer, because the channel is for talking to the bot."""
    for index in range(169):
        message = _Message("Kill all clankers", author=SIEGFRIED, when=NOW + timedelta(seconds=index * 2))
        await handle_message_ask(cast(Any, bot), cast(Any, message))
    assert len(bot.replies) == 1, f"answered {len(bot.replies)} times"  # type: ignore[attr-defined]


@pytest.mark.asyncio
async def test_a_line_aimed_at_another_member_gets_no_reply(bot: _FakeBot) -> None:
    """Two humans talking in #bot-spam is not an invitation."""
    message = _Message(
        "<@1125795621782159432> hi", author=SIEGFRIED, when=NOW,
        mentions=[SimpleNamespace(id=SUSSY)],
    )
    await handle_message_ask(cast(Any, bot), cast(Any, message))
    assert message.replies == []
    assert bot.replies == []  # type: ignore[attr-defined]


@pytest.mark.asyncio
async def test_one_name_addressed_line_then_a_flood_gets_exactly_one_reply(bot: _FakeBot) -> None:
    """"chaosx lol" followed by twenty repeats: one answer, nineteen silences."""
    first = _Message("chaosx lol", author=SIEGFRIED, when=NOW)
    await handle_message_ask(cast(Any, bot), cast(Any, first))
    assert len(bot.replies) == 1  # type: ignore[attr-defined]

    for index in range(20):
        message = _Message("lol", author=SIEGFRIED, when=NOW + timedelta(seconds=3 + index))
        await handle_message_ask(cast(Any, bot), cast(Any, message))
        assert message.replies == []
    assert len(bot.replies) == 1, bot.replies  # type: ignore[attr-defined]


@pytest.mark.asyncio
async def test_a_real_question_is_never_silenced_by_the_banter_gates(bot: _FakeBot) -> None:
    """Questions take the ask path, so the reply gap and the repeat window do not apply."""
    question = _Message("how do i install chaos redux", author=SUSSY, when=NOW)
    # The ask path needs a model; what matters here is that it is not stopped by casual gates.
    allowed, _reason = bot.chat.casual_allowed(
        channel_id=BOT_SPAM, author_id=SUSSY, text=question.content, now=NOW
    )
    assert allowed
