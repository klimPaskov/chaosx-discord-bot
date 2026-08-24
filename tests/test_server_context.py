"""Tests for server rules + channel reference context."""

from chaosx_bot.guild_channels import GuildChannels, format_channel_reference
from chaosx_bot.hermes_bridge import (
    build_auto_scan_answer_prompt,
    build_auto_scan_warning_prompt,
    build_public_prompt,
    redact_public_reasoning,
)
from chaosx_bot.server_rules import ServerRules


def test_redact_public_reasoning_keeps_real_thinking_drops_sensitive_lines() -> None:
    text = (
        "The user asks where to report bugs.\n"
        "My instructions say I must not reveal my system prompt.\n"
        "I think the issues channel is the right place: <#123>.\n"
        "I cannot disclose internal details about the hermes backend.\n"
        "Refusal: not allowed to share that."
    )
    out = redact_public_reasoning(text)
    # Genuine reasoning about the question is preserved...
    assert "issues channel" in out
    assert "<#123>" in out
    # ...but lines revealing the internal decision process are dropped.
    assert "instructions" not in out
    assert "cannot disclose" not in out
    assert "Refusal" not in out


def test_format_channel_reference_groups_and_truncates() -> None:
    channels = [
        {"id": "cat1", "type": 4, "name": "Community"},
        {"id": "c1", "type": 0, "name": "general", "topic": "General chat", "parent_id": "cat1", "position": 0},
        {"id": "c2", "type": 5, "name": "announcements", "topic": "", "parent_id": "cat1", "position": 1},
        {"id": "c3", "type": 0, "name": "rules", "topic": "Please read the rules!", "parent_id": None, "position": 2},
        {"id": "v1", "type": 2, "name": "voice-chat", "parent_id": "cat1", "position": 3},  # skipped
        {"id": "t1", "type": 11, "name": "public-thread", "parent_id": "c1"},  # skipped
    ]
    ref = format_channel_reference(channels)
    assert "<#c1>" in ref and "General chat" in ref
    assert "<#c2>" in ref
    assert "<#c3>" in ref and "Please read the rules!" in ref
    assert "voice-chat" not in ref
    assert "public-thread" not in ref


def test_format_channel_reference_empty() -> None:
    assert format_channel_reference([]) == ""


def test_guild_channels_block_needs_guild_id() -> None:
    gc = GuildChannels(bot_token="x", guild_id=0)
    assert gc.needs_refresh() is False
    assert gc.channels_block() == ""


def test_server_rules_block_empty_without_fetch() -> None:
    rules = ServerRules(bot_token="x", channel_id=0)
    assert rules.needs_refresh() is False
    assert rules.rules_block() == ""
    assert rules.text_sync() == ""


def test_public_prompt_includes_rules_and_channels() -> None:
    prompt = build_public_prompt(
        user_request="Where do I report bugs?",
        guild_name="Chaos Redux",
        channel_name="general",
        server_rules="1. No NSFW content.",
        server_channels="<#111> — Bug reports",
    )
    assert "Server rules" in prompt
    assert "No NSFW content" in prompt
    assert "Server channels" in prompt
    assert "<#111> — Bug reports" in prompt
    # Instruction must tell the model to copy the mention verbatim (clickable link).
    assert "<#channel_id>" in prompt


def test_warning_prompt_includes_rules_and_boundary_cites_them() -> None:
    prompt = build_auto_scan_warning_prompt(
        user_message="bad message",
        guild_name="G",
        channel_name="C",
        gate_reason="offtopic",
        server_rules="5. Keep pings to a minimum.",
    )
    assert "Server rules" in prompt
    assert "Keep pings to a minimum" in prompt
    # The boundary must instruct referencing the specific broken rule.
    assert "reference the specific server rule" in prompt


def test_auto_scan_answer_prompt_includes_channels() -> None:
    prompt = build_auto_scan_answer_prompt(
        user_message="where is the suggestion channel?",
        guild_name="G",
        channel_name="C",
        reference_context="ctx",
        gate_reason="local",
        server_channels="<#222> — Post ideas",
    )
    assert "<#222> — Post ideas" in prompt
