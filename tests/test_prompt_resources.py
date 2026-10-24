from chaosx_bot.hermes_bridge import build_auto_scan_answer_prompt, build_public_prompt
from chaosx_bot.resources import OFFICIAL_RESOURCES


def test_public_prompt_carries_every_official_link():
    prompt = build_public_prompt(
        user_request="where can I watch your tutorials and support the mod?",
        guild_name="Chaos Redux",
        channel_name="general",
        reference_context="",
    )
    for resource in OFFICIAL_RESOURCES:
        assert resource.url in prompt
    assert "only URLs you may give" in prompt


def test_auto_scan_answer_prompt_carries_every_official_link():
    prompt = build_auto_scan_answer_prompt(
        user_message="where do I donate for the mod?",
        guild_name="Chaos Redux",
        channel_name="general",
        reference_context="",
        gate_reason="test",
    )
    for resource in OFFICIAL_RESOURCES:
        assert resource.url in prompt
