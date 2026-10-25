"""Live server data for owner requests: directories, member ids, history scans."""

import asyncio
from datetime import datetime, timezone

from chaosx_bot import server_live


class FakeAuthor:
    def __init__(self, name, author_id):
        self.name = name
        self.display_name = name
        self.id = author_id


class FakeMessage:
    def __init__(self, content, author, when):
        self.content = content
        self.author = author
        self.created_at = when


class FakeTextChannel:
    def __init__(self, name, channel_id, messages=()):
        self.name = name
        self.id = channel_id
        self._messages = list(messages)

    def history(self, limit=100):
        channel = self

        class _Iter:
            def __aiter__(self):
                self._it = iter(channel._messages[:limit])
                return self

            async def __anext__(self):
                try:
                    return next(self._it)
                except StopIteration:
                    raise StopAsyncIteration

        return _Iter()


class FakeRole:
    def __init__(self, name, role_id, position=1, default=False):
        self.name = name
        self.id = role_id
        self.position = position
        self.members = []
        self._default = default

    def is_default(self):
        return self._default


class FakeGuild:
    def __init__(self, channels):
        self.name = "Chaos Redux"
        self.id = 1395459671598436533
        self.member_count = 120
        self.owner_id = 789502982122373150
        self.channels = channels
        self.text_channels = [channel for channel in channels if isinstance(channel, FakeTextChannel)]
        self.roles = [FakeRole("@everyone", 1, 0, default=True), FakeRole("ChaosX", 2, 10)]
        self.members = []
        self.me = type("Me", (), {"top_role": FakeRole("ChaosX", 2, 10), "guild_permissions": None})()


def test_channel_directory_carries_exact_ids():
    guild = FakeGuild([FakeTextChannel("💬┃jazz-and-conversation", 1395459672055480344)])
    directory = server_live.channel_directory(guild)
    assert "1395459672055480344" in directory and "jazz-and-conversation" in directory


def test_history_scan_names_the_authors_with_counts():
    when = datetime(2026, 10, 25, 12, 0, tzinfo=timezone.utc)
    channel = FakeTextChannel(
        "💬┃jazz-and-conversation",
        1395459672055480344,
        [
            FakeMessage("this raid thing is getting out of hand", FakeAuthor("gumo", 42), when),
            FakeMessage("I never blamed anyone for the raid", FakeAuthor("lust", 43), when),
            FakeMessage("nice weather today", FakeAuthor("gumo", 42), when),
        ],
    )
    scan = asyncio.run(server_live.scan_channels(FakeGuild([channel]), [channel], ["raid"]))
    assert "2 of 3" in scan
    assert "gumo id=42: 1 message(s)" in scan
    assert "lust id=43: 1 message(s)" in scan


def test_gather_always_reports_the_server_and_channels():
    guild = FakeGuild([FakeTextChannel("💬┃jazz-and-conversation", 999)])
    bot = type("Bot", (), {"settings": type("S", (), {"db_path": "/nonexistent.db"})()})()
    block = asyncio.run(server_live.gather(bot, guild, "what happened in jazz and conversation?"))
    assert "LIVE SERVER DATA" in block
    assert "1395459671598436533" in block  # the guild id
    assert "id=999" in block  # the channel id
