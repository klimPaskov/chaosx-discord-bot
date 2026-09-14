"""Auto-scan answer gate: one generic domain word must not ground an answer.

Hoops' ollama/DeepSeek-pricing message (a chat between two people, no bot
mention) was auto-answered at confidence 100 because the word "access" counts
as a mod-domain signal and the project lookup falls back to an OR query, so
unrelated snippets looked like grounding.
"""

import sqlite3
from types import SimpleNamespace
from typing import Any, cast

from chaosx_bot.auto_scan import (
    classify_auto_answer,
    matched_domain_terms,
    specific_domain_terms,
)
from chaosx_bot.config import Settings

CHAT_MESSAGE = (
    "hmm, ollama sub gives you deepseek access? Is the pricing good? "
    "I feel like using deepseek official api will be cheaper than third party provider?"
)

CONTEXT = "1. … New Ore — whoever controls the state gains access to it"


def _settings() -> Settings:
    return Settings(_env_file=None, discord_token="dummy")


def _knowledge(tmp_path, context: str = CONTEXT):
    db = tmp_path / "chaosx-gate-test.db"
    conn = sqlite3.connect(db)
    conn.execute("CREATE TABLE catalog_events (row_key TEXT, event_id TEXT, name TEXT)")
    conn.execute("CREATE TABLE catalog_scenarios (row_key TEXT, scenario_id TEXT, name TEXT)")
    conn.execute("CREATE TABLE catalog_clusters (row_key TEXT, cluster_id TEXT, name TEXT)")
    conn.commit()
    conn.close()
    return cast(
        Any,
        SimpleNamespace(
            public_ask_context=lambda question: context,
            ensure_index=lambda: None,
            db_path=db,
        ),
    )


def test_generic_domain_word_alone_does_not_auto_answer(tmp_path):
    assert matched_domain_terms(CHAT_MESSAGE) == ["access"]
    assert specific_domain_terms(CHAT_MESSAGE) == []
    decision = classify_auto_answer(CHAT_MESSAGE, knowledge=_knowledge(tmp_path), settings=_settings())
    assert decision.action == "none"


def test_chat_questions_with_only_generic_words_stay_silent(tmp_path):
    knowledge = _knowledge(tmp_path)
    settings = _settings()
    for message in (
        "is the server down?",
        "any bugs in the community?",
        "can I get access?",
        "any suggestions for the server?",
    ):
        assert classify_auto_answer(message, knowledge=knowledge, settings=settings).action != "answer", message


def test_specific_domain_questions_still_auto_answer(tmp_path):
    knowledge = _knowledge(tmp_path)
    settings = _settings()
    for message in (
        "How does the Zombie Outbreak event work?",
        "when is the next playtest?",
        "is the mod dead?",
        "what is this cluster about?",
        "does hoi4 support this mechanic?",
    ):
        decision = classify_auto_answer(message, knowledge=knowledge, settings=settings)
        assert decision.action == "answer", message
        assert decision.reason == "grounded Chaos Redux question"


def test_exact_and_catalog_paths_are_unaffected(tmp_path):
    settings = _settings()
    catalog = cast(Any, SimpleNamespace(event=lambda event_id: "## Event 47: Nuclear Mystery"))
    assert classify_auto_answer("What is event 47, the mysterious nuke?", knowledge=catalog, settings=settings).action == "answer"
    help_answer = classify_auto_answer("What can ChaosX do?", knowledge=cast(Any, SimpleNamespace()), settings=settings)
    assert help_answer.action == "answer"
    assert "/ask" in help_answer.reference_context
    issue_answer = classify_auto_answer("How do I report a bug?", knowledge=cast(Any, SimpleNamespace()), settings=settings)
    assert issue_answer.action == "answer"
    assert "/issue" in issue_answer.reference_context
