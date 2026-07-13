from chaosx_bot.auth import deny_reason, is_allowed_guild, is_owner, public_deny_reason
from chaosx_bot.config import Settings
from chaosx_bot.hermes_bridge import build_owner_prompt, prompt_hash
from chaosx_bot.rate_limit import FixedWindowRateLimiter


def test_owner_only_gate():
    assert is_owner(123, 123)
    assert not is_owner(456, 123)
    assert "restricted" in deny_reason(456, 123, 1, None)


def test_guild_lock():
    assert is_allowed_guild(1, None)
    assert is_allowed_guild(1, 1)
    assert not is_allowed_guild(2, 1)
    assert "different guild" in deny_reason(123, 123, 2, 1)
    assert public_deny_reason(1, 1) is None
    assert "different guild" in public_deny_reason(2, 1)


def test_prompt_boundary_contains_untrusted_content_warning():
    prompt = build_owner_prompt(owner_request="summarize #issues", guild_name="Chaos Redux", channel_name="bot-spam")
    assert "untrusted data" in prompt
    assert "Do not reveal secrets" in prompt
    assert "summarize #issues" in prompt


def test_prompt_hash_is_stable():
    assert prompt_hash("abc") == prompt_hash("abc")
    assert prompt_hash("abc") != prompt_hash("abcd")


def test_blank_optional_guild_ids_are_allowed():
    settings = Settings(
        _env_file=None,
        allowed_guild_id="",
        command_guild_id="",
        discord_token="dummy",
    )
    assert settings.allowed_guild_id is None
    assert settings.command_guild_id is None


def test_ask_model_defaults_to_luna_medium():
    settings = Settings(_env_file=None, discord_token="dummy")
    assert settings.ask_model == "luna-medium"
    assert settings.ask_provider == "nous"
    assert settings.operator_model == "luna-xhigh"
    assert settings.operator_provider == "nous"


def test_fixed_window_rate_limiter_blocks_after_limit():
    limiter = FixedWindowRateLimiter()
    assert limiter.check(bucket="ask", user_id=1, limit=2, window_seconds=3600).allowed
    assert limiter.check(bucket="ask", user_id=1, limit=2, window_seconds=3600).allowed
    blocked = limiter.check(bucket="ask", user_id=1, limit=2, window_seconds=3600)
    assert not blocked.allowed
    assert blocked.retry_after_seconds > 0
    assert limiter.check(bucket="ask", user_id=2, limit=2, window_seconds=3600).allowed
