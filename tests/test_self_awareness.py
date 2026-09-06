import tempfile
from pathlib import Path

from chaosx_bot import bot as bot_module
from chaosx_bot.auto_scan import looks_like_cost_question
from chaosx_bot.config import Settings
from chaosx_bot.cost import CostTracker
from chaosx_bot.hermes_bridge import build_public_prompt


def test_looks_like_cost_question_detection():
    assert looks_like_cost_question("how much does answering cost?")
    assert looks_like_cost_question("how much does each api call cost?")
    assert looks_like_cost_question("how many tokens per answer?")
    assert looks_like_cost_question("how expensive is a reply?")
    assert not looks_like_cost_question("what model are you running on?")
    assert not looks_like_cost_question("tell me about the evolution stages")


def test_cost_tracker_records_and_summarizes():
    tf = tempfile.mktemp(suffix=".json")
    tracker = CostTracker(tf)
    tracker.record(
        model="deepseek-v4-flash-vision-exp",
        usage={
            "prompt_tokens": 21,
            "completion_tokens": 64,
            "completion_tokens_details": {"reasoning_tokens": 64},
        },
    )
    summary = tracker.summary()
    assert "Last call" in summary
    assert "1 call(s)" in summary
    assert "$" in summary
    Path(tf).unlink(missing_ok=True)


def test_cost_lookup_block_only_injected_on_cost_question():
    settings = Settings(cost_usage_path=Path(tempfile.mktemp(suffix=".json")))
    with_cost = bot_module._cost_lookup_block(settings=settings, text="how much does it cost?")
    assert "Self-awareness" in with_cost
    assert bot_module._cost_lookup_block(settings=settings, text="what model are you?") == ""


def test_public_prompt_injects_cost_context():
    prompt = build_public_prompt(
        user_request="how much does it cost?",
        guild_name="Guild",
        channel_name="Channel",
        cost_context="COSTBLOCK123",
    )
    assert "COSTBLOCK123" in prompt
