from chaosx_bot.bot import PUBLIC_ASK_REDIRECT, public_ask_rejection_reason


IN_DOMAIN = [
    "how do I set up the AI agentic modding workflow for hoi4?",
    "which comfyui workflow should I use for portraits?",
    "what is the hoi4 agent tools mcp?",
    "how do I install the mod and become a tester?",
    "where can I watch your tutorials?",
    "do you have a donation page?",
]


def test_modding_tooling_questions_are_in_domain():
    for question in IN_DOMAIN:
        assert public_ask_rejection_reason(question) is None, question


def test_general_chat_is_still_refused():
    for question in ("give me a pasta recipe", "write me a poem", "capital of france?"):
        assert public_ask_rejection_reason(question) == PUBLIC_ASK_REDIRECT


def test_redirect_wording_mentions_modding_and_links():
    assert "modding" in PUBLIC_ASK_REDIRECT
    assert "links" in PUBLIC_ASK_REDIRECT
