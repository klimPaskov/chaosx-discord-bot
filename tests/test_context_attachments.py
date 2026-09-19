"""Context attachments: material pulled from an earlier message.

Hoops (2026-09-19): "not only video, but any attachment. It should be able to
get the attachment if a user for example points to it and says like to get it
from an earlier message for example".

Depth rules under test:
- the replied-to message: always considered;
- the message immediately above: always considered;
- further back (2+): only when the text points at an attachment.
"""
from __future__ import annotations

import asyncio
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "src"))

from chaosx_bot import bot as botmod  # noqa: E402
from chaosx_bot.config import Settings  # noqa: E402

CLIP = Path("/tmp/video_smoke/clip.mp4")


class FakeAttachment:
    def __init__(self, filename: str, content_type: str | None, data: bytes):
        self.filename = filename
        self.content_type = content_type
        self.size = len(data)
        self.url = "https://cdn.discordapp.com/attachments/1/2/" + filename
        self._data = data

    async def read(self) -> bytes:
        return self._data


class FakeChannel:
    def __init__(self, history: list, fetch: dict[int, object] | None = None):
        self._history = history
        self._fetch = fetch or {}

    def history(self, *, limit: int, before=None):
        async def gen():
            for message in self._history[:limit]:
                yield message

        return gen()

    async def fetch_message(self, message_id: int):
        if message_id in self._fetch:
            return self._fetch[message_id]
        raise RuntimeError("unknown message")


class FakeReference:
    def __init__(self, resolved=None, message_id: int | None = None):
        self.resolved = resolved
        self.message_id = message_id


class FakeMessage:
    def __init__(
        self,
        attachments: list | None = None,
        content: str = "",
        *,
        history: list | None = None,
        reference=None,
        fetch: dict | None = None,
        age_minutes: int = 0,
        message_id: int = 900,
    ):
        self.attachments = attachments or []
        self.content = content
        self.id = message_id
        self.reference = reference
        self.created_at = datetime.now(timezone.utc) - timedelta(minutes=age_minutes)
        self.channel = FakeChannel(history or [], fetch)


def _settings(**overrides) -> Settings:
    return Settings(_env_file=None, discord_token="dummy").model_copy(update=overrides)


def _png_attachment(name: str = "shot.png") -> FakeAttachment:
    import base64
    import struct
    import zlib

    def chunk(tag: bytes, payload: bytes) -> bytes:
        return (
            struct.pack(">I", len(payload))
            + tag
            + payload
            + struct.pack(">I", zlib.crc32(tag + payload) & 0xFFFFFFFF)
        )

    raw = b"\x00" + b"\xff\x00\x00" * 8
    png = (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", struct.pack(">IIBBBBB", 8, 1, 8, 2, 0, 0, 0))
        + chunk(b"IDAT", zlib.compress(raw))
        + chunk(b"IEND", b"")
    )
    return FakeAttachment(name, "image/png", png)


def test_previous_message_attachment_is_picked_up() -> None:
    earlier = FakeMessage([_png_attachment()], "here is the screenshot", message_id=1)
    ask = FakeMessage([], "ChaosX what does this show?", history=[earlier])
    images: list[str] = []
    text = asyncio.run(
        botmod.context_attachment_text(ask, settings=_settings(), images=images, existing_text="")
    )
    assert len(images) == 1
    assert "From an earlier message (1 message above)" in text
    assert "the user did not attach anything to the current message" in text


def test_replied_to_message_attachment_is_picked_up_from_far_back() -> None:
    target = FakeMessage([_png_attachment("old.png")], "the original post", message_id=77)
    ask = FakeMessage([], "what is this?", reference=FakeReference(resolved=target))
    images: list[str] = []
    text = asyncio.run(
        botmod.context_attachment_text(ask, settings=_settings(), images=images, existing_text="")
    )
    assert len(images) == 1
    assert "the message this replies to" in text


def test_reply_target_fetched_when_not_cached() -> None:
    target = FakeMessage([_png_attachment()], "", message_id=55)
    ask = FakeMessage(
        [], "explain this", reference=FakeReference(resolved=None, message_id=55), fetch={55: target}
    )
    images: list[str] = []
    text = asyncio.run(
        botmod.context_attachment_text(ask, settings=_settings(), images=images, existing_text="")
    )
    assert len(images) == 1
    assert "the message this replies to" in text


def test_deep_history_requires_a_pointer() -> None:
    """The attachment 3 messages back must not be dragged in without pointing language."""
    older = [
        FakeMessage([], "unrelated chatter", message_id=13),
        FakeMessage([], "more chatter", message_id=12),
        FakeMessage([_png_attachment()], "random old screenshot", message_id=11),
    ]
    ask = FakeMessage([], "what is the chaos meter cap?", history=older)
    images: list[str] = []
    text = asyncio.run(
        botmod.context_attachment_text(ask, settings=_settings(), images=images, existing_text="")
    )
    assert images == []
    assert text == ""


def test_deep_history_with_pointer_is_picked_up() -> None:
    older = [
        FakeMessage([], "chatter", message_id=13),
        FakeMessage([], "more chatter", message_id=12),
        FakeMessage([_png_attachment()], "screenshot", message_id=11),
    ]
    ask = FakeMessage([], "ChaosX get the attachment from the earlier message above", history=older)
    images: list[str] = []
    text = asyncio.run(
        botmod.context_attachment_text(ask, settings=_settings(), images=images, existing_text="")
    )
    assert len(images) == 1
    assert "3 messages above" in text
    assert text.index("3 messages above") < text.index("Attached") if "Attached" in text else True


def test_own_attachment_suppresses_context_lookup() -> None:
    earlier = FakeMessage([_png_attachment()], "old", message_id=1)
    ask = FakeMessage([_png_attachment("mine.png")], "what is this?", history=[earlier])
    images: list[str] = []
    text = asyncio.run(
        botmod.context_attachment_text(ask, settings=_settings(), images=images, existing_text="")
    )
    assert text == ""
    assert images == []


def test_context_video_from_earlier_message_is_transcribed() -> None:
    if not CLIP.is_file():
        pytest.skip("no video fixture available")
    earlier = FakeMessage([FakeAttachment("clip.mp4", "video/mp4", CLIP.read_bytes())], "clip", message_id=2)
    ask = FakeMessage([], "ChaosX what is the video about?", history=[earlier])
    images: list[str] = []
    text = asyncio.run(
        botmod.context_attachment_text(ask, settings=_settings(), images=images, existing_text="")
    )
    assert "## Attached video: clip.mp4" in text
    assert "Speech transcript" in text
    assert text.index("From an earlier message") < text.index("## Attached video: clip.mp4")
    assert len(images) > 0


def test_disabled_setting_is_a_no_op() -> None:
    earlier = FakeMessage([_png_attachment()], "old", message_id=1)
    ask = FakeMessage([], "what is this?", history=[earlier])
    images: list[str] = []
    text = asyncio.run(
        botmod.context_attachment_text(
            ask, settings=_settings(context_attachment_enabled=False), images=images, existing_text="KEEP"
        )
    )
    assert text == "KEEP"
    assert images == []


def test_unavailable_history_is_survivable() -> None:
    ask = FakeMessage([], "nothing here")
    ask.channel = FakeChannel([])
    images: list[str] = []
    text = asyncio.run(
        botmod.context_attachment_text(ask, settings=_settings(), images=images, existing_text="")
    )
    assert text == ""
    assert images == []


def test_text_file_from_earlier_message_is_included() -> None:
    earlier = FakeMessage(
        [FakeAttachment("notes.txt", "text/plain", b"chaos meter caps at 100\n")], "", message_id=3
    )
    ask = FakeMessage([], "ChaosX what does the file above say about the chaos meter?", history=[earlier])
    images: list[str] = []
    text = asyncio.run(
        botmod.context_attachment_text(ask, settings=_settings(), images=images, existing_text="")
    )
    assert "chaos meter caps at 100" in text
    assert "1 message above" in text
