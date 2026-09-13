"""The bot must name the real release model, not the API slug."""

from chaosx_bot.hermes_bridge import build_public_prompt
from chaosx_bot.models import display_model_name


def test_current_id_maps_to_release_name():
    assert display_model_name("deepseek-flash") == "DeepSeek V4.1 Flash"


def test_retired_flash_ids_route_to_v41_flash():
    for model_id in (
        "deepseek-v4-flash",
        "deepseek-v4-flash-vision-exp",
        "deepseek-chat",
        "deepseek-reasoner",
    ):
        assert display_model_name(model_id) == "DeepSeek V4.1 Flash"


def test_unknown_or_empty_ids_pass_through():
    assert display_model_name("deepseek-v4-pro") == "DeepSeek V4 Pro"
    assert display_model_name("some-future-model") == "some-future-model"
    assert display_model_name("  deepseek-flash  ") == "DeepSeek V4.1 Flash"
    assert display_model_name(None) == ""


def test_prompt_states_release_name_not_slug():
    prompt = build_public_prompt(
        user_request="what model are you running?",
        guild_name="Chaos Redux",
        channel_name="general",
        reference_context="ctx",
        model_name="deepseek-flash",
    )
    assert "you are running on the DeepSeek V4.1 Flash model" in prompt
    assert "deepseek-flash" not in prompt
