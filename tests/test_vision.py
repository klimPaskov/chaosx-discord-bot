import asyncio
from types import SimpleNamespace

import pytest

from chaosx_bot import bot as bot_module
from chaosx_bot.ask_api import _build_user_content
from chaosx_bot.bot import _public_model_completion, attachment_context_for
from chaosx_bot.hermes_bridge import PUBLIC_ASK_BOUNDARY, build_public_prompt


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


def test_build_user_content_orders_text_first():
    out = _build_user_content("t", ["u1", "u2"])
    assert [p["type"] for p in out] == ["text", "image_url", "image_url"]


# --- attachment_context_for: images ---


def test_attachment_context_image_png_data_uri():
    class A:
        filename = "shot.png"
        content_type = "image/png"
        size = 5

        async def read(self):
            return b"AAAAA"

    images, text = asyncio.run(attachment_context_for(SimpleNamespace(attachments=[A()], content="")))
    assert len(images) == 1
    assert images[0].startswith("data:image/png;base64,")
    assert text == ""


def test_attachment_context_filters_non_image_non_text():
    class A:
        filename = "bundle.zip"
        content_type = "application/zip"
        size = 10

        async def read(self):
            return b"data"

    images, text = asyncio.run(attachment_context_for(SimpleNamespace(attachments=[A()], content="")))
    assert images == []
    assert text == ""


# --- attachment_context_for: text files ---


def test_attachment_context_text_file_is_read():
    class A:
        filename = "stack.log"
        content_type = "text/plain"
        size = 100

        async def read(self):
            return b"Error at line 12\nIndexError"

    images, text = asyncio.run(attachment_context_for(SimpleNamespace(attachments=[A()], content="")))
    assert images == []
    assert "Attached file contents:" in text
    assert "`stack.log`:" in text
    assert "Error at line 12" in text


def test_attachment_context_text_file_without_content_type_via_extension():
    class A:
        filename = "notes.md"
        content_type = None
        size = 8

        async def read(self):
            return b"# title"

    images, text = asyncio.run(attachment_context_for(SimpleNamespace(attachments=[A()], content="")))
    assert text != ""
    assert "# title" in text


def test_attachment_context_binary_text_is_skipped():
    class A:
        filename = "weird.txt"
        content_type = "text/plain"
        size = 100

        async def read(self):
            return b"abc\x00def"

    images, text = asyncio.run(attachment_context_for(SimpleNamespace(attachments=[A()], content="")))
    assert images == []
    assert text == ""


def test_attachment_context_image_count_cap():
    class A:
        filename = "a.png"
        content_type = "image/png"
        size = 2

        async def read(self):
            return b"aa"

    images, _ = asyncio.run(attachment_context_for(SimpleNamespace(attachments=[A(), A(), A(), A()], content="")))
    assert len(images) == 3


# --- attachment_context_for: links ---


def test_attachment_context_fetches_link(monkeypatch):
    async def fake_fetch(url, **kwargs):
        return f"content of {url}"

    monkeypatch.setattr(bot_module, "_fetch_url_text", fake_fetch)
    msg = SimpleNamespace(attachments=[], content="see https://example.com/docs for details")
    images, text = asyncio.run(attachment_context_for(msg))
    assert images == []
    assert "Linked page contents:" in text
    assert "<https://example.com/docs>" in text
    assert "content of https://example.com/docs" in text


def test_attachment_context_no_urls_no_network(monkeypatch):
    calls = []

    async def fake_fetch(url, **kwargs):
        calls.append(url)
        return "x"

    monkeypatch.setattr(bot_module, "_fetch_url_text", fake_fetch)
    msg = SimpleNamespace(attachments=[], content="no links here")
    _, text = asyncio.run(attachment_context_for(msg))
    assert text == ""
    assert calls == []


def test_attachment_context_mixed_image_and_text():
    class Img:
        filename = "shot.png"
        content_type = "image/png"
        size = 5

        async def read(self):
            return b"AAAAA"

    class Log:
        filename = "crash.log"
        content_type = "text/plain"
        size = 30

        async def read(self):
            return b"traceback"

    images, text = asyncio.run(attachment_context_for(SimpleNamespace(attachments=[Img(), Log()], content="")))
    assert len(images) == 1
    assert "crash.log" in text


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
        bot=object(),
        system=PUBLIC_ASK_BOUNDARY,
        prompt=prompt,
        model="deepseek-v4-flash-vision-exp",
        reasoning_effort="low",
        timeout_seconds=60,
        activity_label="t",
        images=["data:image/png;base64,YWJj"],
    )
    assert result.ok
    assert captured["images"] == ["data:image/png;base64,YWJj"]


@pytest.mark.asyncio
async def test_public_model_completion_defaults_empty(monkeypatch):
    captured = {}

    async def fake_stream(**kwargs):
        captured.update(kwargs)
        yield "", "ok"

    monkeypatch.setattr(bot_module, "direct_chat_completion_stream", fake_stream)
    prompt = build_public_prompt(user_request="hi", guild_name="G", channel_name="C", reference_context="ctx")
    result = await _public_model_completion(
        bot=object(),
        system=PUBLIC_ASK_BOUNDARY,
        prompt=prompt,
        model="deepseek-v4-flash-vision-exp",
        reasoning_effort="low",
        timeout_seconds=60,
        activity_label="t",
    )
    assert result.ok
    assert captured["images"] == []


@pytest.mark.asyncio
async def test_public_model_completion_appends_attachment_text(monkeypatch):
    captured = {}

    async def fake_stream(**kwargs):
        captured.update(kwargs)
        yield "", "ok"

    monkeypatch.setattr(bot_module, "direct_chat_completion_stream", fake_stream)
    prompt = build_public_prompt(user_request="what log says?", guild_name="G", channel_name="C", reference_context="ctx")
    result = await _public_model_completion(
        bot=object(),
        system=PUBLIC_ASK_BOUNDARY,
        prompt=prompt,
        model="deepseek-v4-flash-vision-exp",
        reasoning_effort="low",
        timeout_seconds=60,
        activity_label="t",
        attachment_text="Attached file contents:\n`crash.log`:\nError at line 12",
    )
    assert result.ok
    assert "Attached file contents:" in captured["user"]
    assert "Error at line 12" in captured["user"]
