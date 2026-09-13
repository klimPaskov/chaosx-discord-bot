from chaosx_bot.bot import reasoning_effort_for_path
from chaosx_bot.config import Settings


def _settings(**overrides):
    return Settings(_env_file=None, discord_token="dummy", **overrides)


def test_public_paths_use_light_reasoning():
    settings = _settings()
    # Community /ask (public slash ask).
    assert reasoning_effort_for_path(settings, owner_only=False, use_ask_model=True) == "low"
    # Scripted public commands (/suggestion, public /event-idea) pin no model.
    assert reasoning_effort_for_path(settings, owner_only=False) == "low"


def test_owner_public_slash_ask_stays_light():
    # The owner's own public /ask is still a public surface: light reasoning.
    settings = _settings()
    assert reasoning_effort_for_path(settings, owner_only=False, use_ask_model=True) == "low"


def test_admin_paths_use_high_reasoning():
    settings = _settings()
    # /admin ask routes through the public-ask branch with owner_only=True.
    assert reasoning_effort_for_path(settings, owner_only=True, use_ask_model=True) == "high"
    # Owner/admin mention+reply and operator-model commands.
    assert reasoning_effort_for_path(settings, owner_only=True, use_operator_model=True) == "high"
    # Owner/admin commands with no model pinned (/admin sync|reindex|jobs, /playtest cancel).
    assert reasoning_effort_for_path(settings, owner_only=True) == "high"


def test_effort_comes_from_settings_not_hardcoded():
    settings = _settings(ask_reasoning_effort="minimal", operator_reasoning_effort="xhigh")
    assert reasoning_effort_for_path(settings, owner_only=False, use_ask_model=True) == "minimal"
    assert reasoning_effort_for_path(settings, owner_only=True, use_operator_model=True) == "xhigh"
