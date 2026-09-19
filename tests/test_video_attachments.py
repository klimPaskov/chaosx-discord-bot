"""Regression tests for video attachments and video links in the ask paths.

These cover the failure Hoops hit on 2026-09-19: he asked ChaosX about a video
and got "no video reached me", with nothing in the bot's records to explain it.
The harness feeds realistic Discord attachment objects (bytes, filename, size,
content_type) through the real ``attachment_context_for``.
"""
from __future__ import annotations

import asyncio
import sys
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


class FakeMessage:
    def __init__(self, attachments: list, content: str = ""):
        self.attachments = attachments
        self.content = content
        self.id = 424242


def _settings(**overrides) -> Settings:
    base = Settings(_env_file=None, discord_token="dummy")
    return base.model_copy(update=overrides)


def _clip_bytes() -> bytes:
    if not CLIP.is_file():
        pytest.skip("no video fixture available")
    return CLIP.read_bytes()


def test_video_attachment_produces_frames_and_transcript_block() -> None:
    settings = _settings()
    data = _clip_bytes()
    message = FakeMessage([FakeAttachment("clip.mp4", "video/mp4", data)], "what is this?")
    images, text = asyncio.run(botmod.attachment_context_for(message, settings=settings))
    assert len(images) == settings.video_frame_count
    assert "## Attached video: clip.mp4" in text
    assert "Speech transcript" in text


def test_video_attachment_without_content_type_still_detected() -> None:
    """Discord omits content_type for some uploads; magic bytes must carry it."""
    settings = _settings()
    message = FakeMessage([FakeAttachment("clip.mp4", None, _clip_bytes())])
    images, text = asyncio.run(botmod.attachment_context_for(message, settings=settings))
    assert len(images) == settings.video_frame_count
    assert "## Attached video" in text


def test_oversize_video_is_acknowledged_not_ignored() -> None:
    settings = _settings(video_max_bytes=1024)
    message = FakeMessage([FakeAttachment("big.mp4", "video/mp4", b"\x00" * 4096)])
    images, text = asyncio.run(botmod.attachment_context_for(message, settings=settings))
    assert images == []
    assert "too large to analyse" in text


def test_no_attachment_yields_empty_context() -> None:
    """The state that produced 'no video reached me': nothing attached at all."""
    settings = _settings()
    message = FakeMessage([], "what is the video about?")
    images, text = asyncio.run(botmod.attachment_context_for(message, settings=settings))
    assert images == []
    assert text == ""


def test_unfetchable_video_link_is_reported_explicitly(monkeypatch: pytest.MonkeyPatch) -> None:
    settings = _settings()

    async def _no_download(url: str, *, max_bytes: int):
        return None

    monkeypatch.setattr(botmod, "download_video", _no_download)
    monkeypatch.setattr(botmod, "ytdlp_available", lambda: False)
    images: list[str] = []
    block = asyncio.run(
        botmod._video_link_block("https://www.youtube.com/watch?v=abc123", settings, images)
    )
    assert block is not None
    assert "could not download" in block
    assert "never describe or summarise video content you did not receive" in block
    assert images == []


def test_video_lookalike_filename_is_not_treated_as_video() -> None:
    """A text file named .mp4 must fall through to the text/decoder path."""
    settings = _settings()
    message = FakeMessage([FakeAttachment("notes.mp4", "text/plain", b"just some notes\n")])
    images, text = asyncio.run(botmod.attachment_context_for(message, settings=settings))
    assert images == []
    assert "## Attached video" not in text
