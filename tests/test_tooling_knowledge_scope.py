"""Modding-tooling topics are answerable, and described as real capabilities."""

from chaosx_bot.auto_scan import specific_domain_terms
from chaosx_bot.capabilities import probe_capabilities


def test_tooling_topics_count_as_domain_signal():
    for text in (
        "which comfyui workflow should I use for hoi4 portraits?",
        "what is the hoi4 agent tools mcp?",
        "how do I use codex for modding?",
    ):
        assert specific_domain_terms(text), text


def test_capability_summary_mentions_tooling_and_links():
    summary = probe_capabilities(members_intent=True)["summary"]
    assert "modding tooling" in summary
    assert "official links" in summary
