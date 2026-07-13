from chaosx_bot.auth import deny_reason, is_allowed_guild, is_owner
from chaosx_bot.config import Settings
from chaosx_bot.hermes_bridge import build_owner_prompt, prompt_hash


def test_owner_only_gate():
    assert is_owner(123, 123)
    assert not is_owner(456, 123)
    assert "owner-only" in deny_reason(456, 123, 1, None)


def test_guild_lock():
    assert is_allowed_guild(1, None)
    assert is_allowed_guild(1, 1)
    assert not is_allowed_guild(2, 1)
    assert "different guild" in deny_reason(123, 123, 2, 1)


def test_prompt_boundary_contains_untrusted_content_warning():
    prompt = build_owner_prompt(owner_request="summarize #issues", guild_name="Chaos Redux", channel_name="bot-spam")
    assert "untrusted data" in prompt
    assert "Do not reveal secrets" in prompt
    assert "summarize #issues" in prompt


def test_prompt_hash_is_stable():
    assert prompt_hash("abc") == prompt_hash("abc")
    assert prompt_hash("abc") != prompt_hash("abcd")


def test_blank_optional_guild_ids_are_allowed():
    settings = Settings(CHAOSX_DISCORD_TOKEN="dummy", CHAOSX_ALLOWED_GUILD_ID="", CHAOSX_COMMAND_GUILD_ID="")
    assert settings.allowed_guild_id is None
    assert settings.command_guild_id is None
