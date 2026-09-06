import asyncio
from types import SimpleNamespace

from chaosx_bot.ask_api import _build_user_content
from chaosx_bot.bot import extract_message_images


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


def test_extract_message_images_filters_non_image():
    class A:
        filename = "notes.txt"
        content_type = "text/plain"
        size = 10

        async def read(self):
            return b"data"

    msg = SimpleNamespace(attachments=[A()])
    assert asyncio.run(extract_message_images(msg)) == []


def test_extract_message_images_png_data_uri():
    class A:
        filename = "shot.png"
        content_type = "image/png"
        size = 5

        async def read(self):
            return b"AAAAA"

    msg = SimpleNamespace(attachments=[A()])
    uris = asyncio.run(extract_message_images(msg))
    assert len(uris) == 1
    assert uris[0].startswith("data:image/png;base64,")


def test_extract_message_images_extension_fallback_when_no_content_type():
    class A:
        filename = "img.webp"
        content_type = None
        size = 3

        async def read(self):
            return b"abc"

    msg = SimpleNamespace(attachments=[A()])
    assert asyncio.run(extract_message_images(msg)) == ["data:image/webp;base64,YWJj"]


def test_extract_message_images_oversized_is_skipped():
    class A:
        filename = "big.png"
        content_type = "image/png"
        size = 9_000_000

        async def read(self):
            return b"x"

    msg = SimpleNamespace(attachments=[A()])
    assert asyncio.run(extract_message_images(msg)) == []


def test_extract_message_images_caps_count():
    class A:
        filename = "a.png"
        content_type = "image/png"
        size = 2

        async def read(self):
            return b"aa"

    msg = SimpleNamespace(attachments=[A(), A(), A(), A(), A()])
    assert len(asyncio.run(extract_message_images(msg))) == 3


import pytest

from chaosx_bot import bot as bot_module
from chaosx_bot.bot import _public_model_completion
from chaosx_bot.hermes_bridge import PUBLIC_ASK_BOUNDARY, build_public_prompt


@pytest.mark.asyncio
async def test_public_model_completion_forwards_images_to_stream(monkeypatch):
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
async def test_public_model_completion_default_images_empty_list(monkeypatch):
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
