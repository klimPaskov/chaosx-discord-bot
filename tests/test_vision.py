import asyncio
import io
import struct
from types import SimpleNamespace

import pytest

from chaosx_bot import bot as bot_module
from chaosx_bot.ask_api import _build_user_content
from chaosx_bot.bot import _public_model_completion, attachment_context_for
from chaosx_bot.hermes_bridge import PUBLIC_ASK_BOUNDARY, build_public_prompt


def _mk_png(width: int = 8, height: int = 8, rgb=(255, 0, 0)) -> bytes:
    from PIL import Image
    image = Image.new("RGB", (width, height), rgb)
    buf = io.BytesIO()
    image.save(buf, format="PNG")
    return buf.getvalue()


def _mk_dds(width: int = 4, height: int = 4, rgba=(255, 0, 0, 255)) -> bytes:
    """Build a minimal uncompressed 32-bit BGRA DDS file for ffmpeg to decode."""
    r, g, b, a = rgba
    header = b"DDS "
    header += struct.pack("<IIIIII", 124, 0x00001007, height, width, 0, 0)  # size, flags, h, w, pitch, depth
    header += struct.pack("<I", 0)  # mipmap count
    header += b"\x00" * 44  # reserved1[11]
    header += struct.pack("<IIIIIIII", 32, 0x41, 0, 32, 0x00FF0000, 0x0000FF00, 0x000000FF, 0xFF000000)
    header += struct.pack("<IIIII", 0x00001000, 0, 0, 0, 0)  # caps, caps2, caps3, caps4, reserved2
    pixels = bytearray()
    for _ in range(height * width):
        value = (a << 24) | (r << 16) | (g << 8) | b
        pixels += struct.pack("<I", value)
    return header + bytes(pixels)


class Attr:
    """Minimal Discord-attachment stand-in."""

    def __init__(self, filename, content_type, size, payload):
        self.filename = filename
        self.content_type = content_type
        self.size = size
        self._payload = payload

    async def read(self):
        return self._payload


def _msg(attachments=(), content=""):
    return SimpleNamespace(attachments=list(attachments), content=content)


# --- _build_user_content (vision content parts) ---


def test_build_user_content_no_images_is_plain_text():
    assert _build_user_content("hi") == "hi"
    assert _build_user_content("hi", []) == "hi"
    assert _build_user_content("hi", None) == "hi"


def test_build_user_content_with_images_builds_content_parts():
    out = _build_user_content("what is this?", ["data:image/png;base64,AAA"])
    assert out[0] == {"type": "text", "text": "what is this?"}
    assert out[1] == {"type": "image_url", "image_url": {"url": "data:image/png;base64,AAA"}}
    assert len(out) == 2


# --- attachment_context_for: images across formats ---


def test_attachment_context_png_image_data_uri():
    png = _mk_png()
    images, text = asyncio.run(attachment_context_for(_msg([Attr("shot.png", "image/png", len(png), png)])))
    assert len(images) == 1
    assert images[0].startswith("data:image/png;base64,")
    assert text == ""


def test_attachment_context_dds_converted_to_png():
    # DDS is not a browser image and Pillow can't read it; ffmpeg must convert.
    dds = _mk_dds(4, 4, (0, 255, 0, 255))
    images, text = asyncio.run(attachment_context_for(_msg([Attr("tex.dds", None, len(dds), dds)])))
    assert len(images) == 1
    assert images[0].startswith("data:image/png;base64,")
    assert text == ""


def test_attachment_context_image_count_cap():
    pngs = [_mk_png(rgb=(i, 0, 0)) for i in range(4)]
    images, _ = asyncio.run(attachment_context_for(_msg([Attr(f"a{i}.png", "image/png", len(pngs[i]), pngs[i]) for i in range(4)])))
    assert len(images) == 3


# --- attachment_context_for: text files (broad decode) ---


def test_attachment_context_text_file_is_read():
    img, text = asyncio.run(attachment_context_for(_msg([Attr("crash.log", "text/plain", 100, b"Error at line 12\nIndexError")])))
    assert img == []
    assert "Attached file contents:" in text
    assert "`crash.log`:" in text
    assert "Error at line 12" in text


def test_attachment_context_text_without_content_type_via_extension():
    img, text = asyncio.run(attachment_context_for(_msg([Attr("notes.md", None, 8, b"# title")])))
    assert text != ""
    assert "# title" in text


def test_attachment_context_unusual_extension_still_read_as_text():
    # All-printable content with an unknown extension decodes as text.
    img, text = asyncio.run(attachment_context_for(_msg([Attr("weird.datafile", "application/octet-stream", 12, b"hello world")])))
    assert "hello world" in text


def test_attachment_context_binary_is_not_dropped_silently():
    # Binary content (null + control bytes) is neither image nor text; it is
    # acknowledged as an unreadable attachment so nothing is dropped silently.
    payload = b"\x50\x4b\x03\x04\x00\x00\x00\x00"  # PK zip-ish header
    img, text = asyncio.run(attachment_context_for(_msg([Attr("bundle.zip", "application/zip", len(payload), payload)])))
    assert img == []
    assert "Unreadable attachments" in text
    assert "`bundle.zip`" in text


# --- attachment_context_for: links ---


def test_attachment_context_fetches_link(monkeypatch):
    async def fake_fetch(url, **kwargs):
        return f"content of {url}"

    monkeypatch.setattr(bot_module, "_fetch_url_text", fake_fetch)
    img, text = asyncio.run(attachment_context_for(_msg(content="see https://example.com/docs for details")))
    assert "Linked page contents:" in text
    assert "<https://example.com/docs>" in text
    assert "content of https://example.com/docs" in text


def test_attachment_context_no_urls_no_network(monkeypatch):
    calls = []

    async def fake_fetch(url, **kwargs):
        calls.append(url)
        return "x"

    monkeypatch.setattr(bot_module, "_fetch_url_text", fake_fetch)
    _, text = asyncio.run(attachment_context_for(_msg(content="no links here")))
    assert text == ""
    assert calls == []


def test_attachment_context_mixed_image_and_text_and_binary():
    png = _mk_png()
    img, text = asyncio.run(attachment_context_for(
        _msg([Attr("shot.png", "image/png", len(png), png),
              Attr("crash.log", "text/plain", 30, b"traceback"),
              Attr("bundle.bin", "application/octet-stream", 8, b"\x00\x01\x02\x03")])
    ))
    assert len(img) == 1
    assert "crash.log" in text
    assert "bundle.bin" in text


# --- _public_model_completion forwards images + attachment_text ---


@pytest.mark.asyncio
async def test_public_model_completion_forwards_images(monkeypatch):
    captured = {}

    async def fake_stream(**kwargs):
        captured.update(kwargs)
        yield "", "ok"

    monkeypatch.setattr(bot_module, "direct_chat_completion_stream", fake_stream)
    prompt = build_public_prompt(user_request="hi", guild_name="G", channel_name="C", reference_context="ctx")
    result = await _public_model_completion(
        bot=object(), system=PUBLIC_ASK_BOUNDARY, prompt=prompt, model="deepseek-v4-flash-vision-exp",
        reasoning_effort="low", timeout_seconds=60, activity_label="t",
        images=["data:image/png;base64,YWJj"],
    )
    assert result.ok
    assert captured["images"] == ["data:image/png;base64,YWJj"]


@pytest.mark.asyncio
async def test_public_model_completion_appends_attachment_text(monkeypatch):
    captured = {}

    async def fake_stream(**kwargs):
        captured.update(kwargs)
        yield "", "ok"

    monkeypatch.setattr(bot_module, "direct_chat_completion_stream", fake_stream)
    prompt = build_public_prompt(user_request="what log says?", guild_name="G", channel_name="C", reference_context="ctx")
    result = await _public_model_completion(
        bot=object(), system=PUBLIC_ASK_BOUNDARY, prompt=prompt, model="deepseek-v4-flash-vision-exp",
        reasoning_effort="low", timeout_seconds=60, activity_label="t",
        attachment_text="Attached file contents:\n`crash.log`:\nError at line 12",
    )
    assert result.ok
    assert "Attached file contents:" in captured["user"]
    assert "Error at line 12" in captured["user"]


@pytest.mark.asyncio
async def test_public_model_completion_defaults_empty(monkeypatch):
    captured = {}

    async def fake_stream(**kwargs):
        captured.update(kwargs)
        yield "", "ok"

    monkeypatch.setattr(bot_module, "direct_chat_completion_stream", fake_stream)
    prompt = build_public_prompt(user_request="hi", guild_name="G", channel_name="C", reference_context="ctx")
    result = await _public_model_completion(
        bot=object(), system=PUBLIC_ASK_BOUNDARY, prompt=prompt, model="deepseek-v4-flash-vision-exp",
        reasoning_effort="low", timeout_seconds=60, activity_label="t",
    )
    assert result.ok
    assert captured["images"] == []


@pytest.mark.asyncio
async def test_public_model_completion_fallback_keeps_image(monkeypatch):
    """When the direct path fails, the tool-enabled Hermes fallback must be
    pointed at the attached image so it doesn't say 'no image'."""
    from chaosx_bot.bot import HermesResult
    from chaosx_bot.ask_api import DirectAskError

    captured = {}

    async def failing_stream(**kw):
        raise DirectAskError("direct stream returned an empty answer")
        yield  # pragma: no cover - makes this an async generator that raises on iterate

    monkeypatch.setattr(bot_module, "direct_chat_completion_stream", failing_stream)
    monkeypatch.setattr(bot_module, "_persist_image_uris", lambda images, prefix="chaosx_att": ["/tmp/chaosx_att_test.png"])

    async def a_fake_run_hermes(**kw):
        captured.update(kw)
        return HermesResult(prompt_hash="h", returncode=0, stdout="ok", stderr="")

    monkeypatch.setattr(bot_module, "run_hermes", a_fake_run_hermes)

    settings = SimpleNamespace(
        hermes_bin="/usr/bin/hermes",
        hermes_profile="chaos_redux",
        chaos_redux_repo="/repo",
        ask_provider="deepseek",
    )
    bot = SimpleNamespace(settings=settings)
    prompt = build_public_prompt(user_request="which user has this avatar?", guild_name="G", channel_name="C", reference_context="")
    result = await _public_model_completion(
        bot=bot, system=PUBLIC_ASK_BOUNDARY, prompt=prompt, model="deepseek-v4-flash-vision-exp",
        reasoning_effort="high", timeout_seconds=60, activity_label="t",
        images=["data:image/png;base64,YWJj"],
    )
    assert result.ok
    assert "/tmp/chaosx_att_test.png" in captured["prompt"]
    assert "vision capability" in captured["prompt"]


def test_persist_image_uris_writes_files():
    import base64
    import os
    import io as _io
    from PIL import Image
    from chaosx_bot.bot import _persist_image_uris

    buf = _io.BytesIO()
    Image.new("RGB", (8, 8), (255, 0, 0)).save(buf, "PNG")
    uri = "data:image/png;base64," + base64.b64encode(buf.getvalue()).decode()
    paths = _persist_image_uris([uri])
    assert len(paths) == 1
    assert os.path.exists(paths[0]) and os.path.splitext(paths[0])[1] == ".png"
    image = Image.open(paths[0])
    image.load()
    assert image.convert("RGB").getpixel((0, 0)) == (255, 0, 0)
    for path in paths:
        os.remove(path)
    assert not os.path.exists(paths[0])
