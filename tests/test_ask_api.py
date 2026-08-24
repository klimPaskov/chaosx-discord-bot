"""Tests for the direct-ask fast path and internal-infrastructure redaction."""

import asyncio
from pathlib import Path

import pytest

from chaosx_bot.ask_api import DirectAskError, direct_chat_completion, resolve_api_key
from chaosx_bot.bot import sanitize_public_ask_output
from chaosx_bot.hermes_bridge import (
    PUBLIC_ASK_BOUNDARY,
    _INTERNAL_INFRASTRUCTURE_REDACTIONS,
    build_public_prompt,
    redact_internal_infrastructure,
)


class _FeedSpy:
    """Records owner-feed calls without touching Discord."""

    def __init__(self) -> None:
        self.started = False
        self.reasoning = ""
        self.content = ""
        self.finished_with = ""

    async def start(self) -> bool:
        self.started = True
        return True

    async def emit(self, reasoning_delta: str, content_delta: str) -> None:
        self.reasoning += reasoning_delta or ""
        self.content += content_delta or ""

    async def finish(self, final_answer: str = "") -> None:
        self.finished_with = final_answer


@pytest.mark.parametrize(
    ("source", "expected"),
    [
        # The observed leak: operator answer mentioning storage internals.
        (
            "Pulled the full record from Discord API + bot DB. Here's the grounded picture:",
            "Pulled the full record from my records. Here's the grounded picture:",
        ),
        ("I checked the bot DB and found 3 entries.", "I checked my records and found 3 entries."),
        ("The data lives in chaosx.db.", "The data lives in my records."),
        ("Querying sqlite directly...", "Querying storage directly..."),
        ("Pulled via the Discord API last night.", "Pulled via message history last night."),
        ("The conversation memory holds that fact.", "The prior context holds that fact."),
        ("Hermes ran the task.", "my backend ran the task."),
        ("Snippet with no internal terms stays as-is.", "Snippet with no internal terms stays as-is."),
        ("Hermes and Qoder internals.", "my backend and notes internals."),
        # Case-insensitive phrase collapse.
        ("DISCORD API + BOT DB report", "my records report"),
    ],
)
def test_redact_internal_infrastructure(source: str, expected: str) -> None:
    assert redact_internal_infrastructure(source) == expected


def test_redaction_applies_through_public_sanitizer() -> None:
    output = sanitize_public_ask_output(
        "ChaosX answer: I pulled that from the bot DB; here is the event info."
    )
    assert "bot DB" not in output
    assert "my records" in output


def test_redaction_leaves_normal_answers_untouched() -> None:
    answer = "The Black Plague event is state-based with a biowarfare integration."
    assert sanitize_public_ask_output(answer) == answer


def test_redaction_list_is_ordered_specific_first() -> None:
    # The compound "discord api + bot db" pattern must beat the generic ones.
    sources = [pattern.pattern for pattern, _ in _INTERNAL_INFRASTRUCTURE_REDACTIONS]
    assert sources.index("discord api\\s*\\+\\s*bot db") < sources.index("\\bbot(?:'s)?\\s+db\\b")
    assert sources.index("\\bthe\\s+bot(?:'s)?\\s+db\\b") < sources.index("\\bbot(?:'s)?\\s+db\\b")


def test_resolve_api_key_env_override(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DEEPSEEK_API_KEY", "env-key-123")
    assert resolve_api_key() == "env-key-123"


def test_resolve_api_key_from_env_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    env_file = tmp_path / ".env"
    env_file.write_text("CHAOSX_DISCORD_TOKEN=x\nDEEPSEEK_API_KEY=file-key-456\n", encoding="utf-8")
    monkeypatch.setattr("chaosx_bot.ask_api._KEY_CANDIDATES", (env_file,))
    assert resolve_api_key() == "file-key-456"


def test_resolve_api_key_missing(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    empty = tmp_path / ".env"
    empty.write_text("NO_KEY_HERE=1\n", encoding="utf-8")
    monkeypatch.setattr("chaosx_bot.ask_api._KEY_CANDIDATES", (empty,))
    assert resolve_api_key() == ""


@pytest.mark.asyncio
async def test_direct_chat_completion_missing_key(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    empty = tmp_path / ".env"
    empty.write_text("", encoding="utf-8")
    monkeypatch.setattr("chaosx_bot.ask_api._KEY_CANDIDATES", (empty,))
    with pytest.raises(DirectAskError):
        await direct_chat_completion(system="s", user="u")


def test_public_prompt_split_round_trip() -> None:
    prompt = build_public_prompt(
        user_request="What is the Fury event?",
        guild_name="Chaos Redux",
        channel_name="general",
        reference_context="Fury is a major event.",
    )
    assert prompt.startswith(PUBLIC_ASK_BOUNDARY)
    user_part = prompt[len(PUBLIC_ASK_BOUNDARY):].strip()
    assert user_part.startswith("Discord context:")
    assert "What is the Fury event?" in user_part


@pytest.mark.asyncio
async def test_public_model_completion_uses_direct_path_first(monkeypatch: pytest.MonkeyPatch) -> None:
    from chaosx_bot import bot as bot_module

    captured: dict = {}

    async def fake_stream(**kwargs):
        captured.update(kwargs)
        yield "thinking about it", ""
        yield "", "Fast answer."

    monkeypatch.setattr(bot_module, "direct_chat_completion_stream", fake_stream)
    from chaosx_bot.bot import _public_model_completion

    prompt = build_public_prompt(
        user_request="Hi",
        guild_name="G",
        channel_name="C",
        reference_context="ctx",
    )
    result = await _public_model_completion(
        bot=object(),
        system=PUBLIC_ASK_BOUNDARY,
        prompt=prompt,
        model="deepseek-v4-flash",
        reasoning_effort="low",
        timeout_seconds=60,
        activity_label="mention ask",
    )
    assert result.ok
    assert result.stdout == "Fast answer."
    assert captured["system"] == PUBLIC_ASK_BOUNDARY
    assert captured["user"].startswith("Discord context:")


@pytest.mark.asyncio
async def test_public_model_completion_streams_owner_feed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from chaosx_bot import bot as bot_module

    async def fake_stream(**kwargs):
        yield "step one", ""
        yield "step two", ""
        yield "", "The answer."

    monkeypatch.setattr(bot_module, "direct_chat_completion_stream", fake_stream)
    from chaosx_bot.bot import _public_model_completion

    feed = _FeedSpy()

    prompt = build_public_prompt(
        user_request="Hi",
        guild_name="G",
        channel_name="C",
        reference_context="ctx",
    )
    result = await _public_model_completion(
        bot=object(),
        system=PUBLIC_ASK_BOUNDARY,
        prompt=prompt,
        model="deepseek-v4-flash",
        reasoning_effort="low",
        timeout_seconds=60,
        activity_label="mention ask",
        actor_id=999,
        feed=feed,
    )
    assert result.ok
    assert result.stdout == "The answer."
    assert feed.reasoning == "step onestep two"
    assert feed.finished_with == "The answer."
