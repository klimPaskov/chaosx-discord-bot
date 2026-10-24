"""A plain project question without a mention must reach an answer, plurals included.

Hoops, 2026-10-24: "Okay, what are the mcps used for?" got no reply — the term list holds "mcp" and the
matcher required an exact space-delimited hit, so "mcps" carried no domain signal. The same pass made the
gate prove relevance (a project term of the message has to appear in what retrieval returned) and stopped
bare weak words ("state", "balance", "map") from answering ordinary chat.
"""

import sqlite3

from chaosx_bot.auto_scan import (
    classify_auto_answer,
    classify_message,
    gating_domain_terms,
    _contains_normalized_phrase,
    normalize_scan_text,
)
from chaosx_bot.config import load_settings


class FakeKnowledge:
    def public_ask_context(self, query: str) -> str:
        return "HOI4 Agent Tools and HOI4 Live Tools are the two MCP servers."


class IndexedKnowledge:
    """Retrieval that really does return mod documents, the way the live index does for common words."""

    def __init__(self, db_path) -> None:
        self.db_path = db_path  # indexer.connect() expects a Path

    def ensure_index(self) -> None:
        return None

    def public_ask_context(self, query: str) -> str:
        return (
            "1. State history files define population; the state id is referenced by events. "
            "Balance is tuned in the ai_strategy files. The map is split into provinces. "
            "Tiers are recalculated nightly; chaos meter stages are documented here."
        )


def _db(tmp_path):
    db = tmp_path / "chaosx-gate-test.db"
    conn = sqlite3.connect(db)
    conn.execute("CREATE TABLE catalog_events (row_key TEXT, event_id TEXT, name TEXT)")
    conn.execute("CREATE TABLE catalog_scenarios (row_key TEXT, scenario_id TEXT, name TEXT)")
    conn.execute("CREATE TABLE catalog_clusters (row_key TEXT, cluster_id TEXT, name TEXT)")
    conn.commit()
    conn.close()
    return db


def _classify(content: str, knowledge=None, tmp_path=None):
    settings = load_settings()
    return classify_auto_answer(content, knowledge=knowledge or FakeKnowledge(), settings=settings)


def test_plural_term_matches_its_singular():
    text = normalize_scan_text("what are the mcps used for?")
    assert _contains_normalized_phrase(text, "mcp")
    assert _contains_normalized_phrase(normalize_scan_text("any mods coming?"), "mod")
    assert _contains_normalized_phrase(normalize_scan_text("show me events"), "event")
    assert _contains_normalized_phrase(normalize_scan_text("how do focuses work?"), "focus")
    assert _contains_normalized_phrase(normalize_scan_text("is modding hard?"), "mod")


def test_es_suffix_does_not_leak_generic_words():
    assert not _contains_normalized_phrase(normalize_scan_text("what modes are there?"), "mod")


def test_plain_question_about_the_mcps_is_answered():
    decision = _classify("Okay, what are the mcps used for?")
    assert decision.acted, decision.reason
    assert decision.source == "project_context"


def test_chaos_meter_question_is_answered(tmp_path):
    decision = _classify("what modes does the chaos meter have?", knowledge=IndexedKnowledge(_db(tmp_path)))
    assert decision.acted, decision.reason


def test_tier_question_is_answered(tmp_path):
    decision = _classify("how is the tier calculated?", knowledge=IndexedKnowledge(_db(tmp_path)))
    assert decision.acted, decision.reason


def test_weak_terms_alone_do_not_trigger_even_when_retrieval_matches(tmp_path):
    # The index really does return "state" and "balance" documents for anything containing those words, so a
    # bare weak term must not be enough on its own.
    knowledge = IndexedKnowledge(_db(tmp_path))
    for message in ("what state are you in", "how do I balance my budget", "is the map accurate on google maps?"):
        assert not _classify(message, knowledge=knowledge).acted, message
        assert gating_domain_terms(message) == [], message


def test_casual_question_without_domain_signal_stays_quiet():
    decision = classify_message("what time is it for you?", knowledge=FakeKnowledge(), settings=load_settings())
    assert not decision.acted


def test_other_games_are_not_answered_uninvited(tmp_path):
    knowledge = IndexedKnowledge(_db(tmp_path))
    for message in ("minecraft mods are fun", "hearts of iron 3 was better?", "eu4 mana system is weird"):
        assert not _classify(message, knowledge=knowledge).acted, message
