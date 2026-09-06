import asyncio

from chaosx_bot.bot import _ThinkingFeed


class _FakeBot:
    settings = None


class _FakeMsg:
    id = 0

    def __init__(self, dm: "_FakeDm") -> None:
        self._dm = dm

    async def edit(self, content="", **_: object) -> None:
        self._dm.edits.append(content)


class _FakeDm:
    def __init__(self) -> None:
        self.sent: list[str] = []
        self.edits: list[str] = []
        self._n = 0

    async def send(self, content: str) -> _FakeMsg:
        self.sent.append(content)
        self._n += 1
        msg = _FakeMsg(self)
        msg.id = self._n
        return msg


class _FakeUser:
    def __init__(self, dm: _FakeDm) -> None:
        self._dm = dm

    async def create_dm(self) -> _FakeDm:
        return self._dm


def test_thinking_feed_paginates_long_reasoning_without_cutoff() -> None:
    dm = _FakeDm()
    feed = _ThinkingFeed(_FakeBot(), label="ask", interaction=None, raw=True, dm_user=_FakeUser(dm))
    assert asyncio.run(feed.start()) is True
    feed._last_edit = 0.0  # bypass the edit throttle so the test is fast

    # Reasoning that comfortably exceeds MAX_CHARS (1900).
    long = ("lorem ipsum dolor sit amet " * 120).strip()
    assert len(long) > 2000

    asyncio.run(feed.emit(long, ""))
    # A continuation message must have been opened — reasoning is never cut off.
    assert len(dm.sent) >= 2, "long reasoning must open a continuation message"

    # Reconstruct everything rendered and confirm no meaningful reasoning loss.
    rendered = "".join(e.replace("🧠 **ChaosX is thinking:**\n", "") for e in dm.edits)
    assert len(rendered) >= len(long) - 40, "reasoning must not be truncated"


def test_thinking_feed_short_reasoning_stays_single_message() -> None:
    dm = _FakeDm()
    feed = _ThinkingFeed(_FakeBot(), label="ask", interaction=None, raw=True, dm_user=_FakeUser(dm))
    assert asyncio.run(feed.start()) is True
    feed._last_edit = 0.0
    asyncio.run(feed.emit("short reasoning", ""))
    assert len(dm.sent) == 1, "short reasoning should stay in one message"
