"""Repeated public questions are answered from a short-lived cache; anything live is never cached."""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from chaosx_bot.answer_cache import AnswerCache, is_live, normalize


def test_roundtrip_is_case_and_punctuation_insensitive(tmp_path):
    cache = AnswerCache(tmp_path / "a.db")
    cache.put("How does the Chaos Meter work?", "It rises with tension.")
    assert cache.get("how does the chaos meter work") == "It rises with tension."
    assert cache.get("how   does the CHAOS meter work???") == "It rises with tension."


def test_live_questions_are_never_cached(tmp_path):
    cache = AnswerCache(tmp_path / "a.db")
    assert is_live("what is the current status?")
    cache.put("what is the current status?", "5 members")
    assert cache.get("what is the current status?") is None


def test_ttl_expiry(tmp_path):
    cache = AnswerCache(tmp_path / "a.db")
    cache.ttl = 0
    cache.put("how does the chaos meter work", "It rises.")
    assert cache.get("how does the chaos meter work") is None


def test_empty_and_huge_answers_are_not_stored(tmp_path):
    cache = AnswerCache(tmp_path / "a.db")
    cache.put("how does the chaos meter work", "")
    cache.put("how does the chaos meter work", "x" * 5000)
    assert cache.get("how does the chaos meter work") is None


def test_normalize_collapses_whitespace_and_punctuation():
    assert normalize("  How   does the Chaos-Meter work?! ") == "how does the chaos meter work"


def test_unknown_question_misses(tmp_path):
    assert AnswerCache(tmp_path / "a.db").get("never asked before") is None


def test_cache_is_wired_into_the_ask_command():
    source = (Path(__file__).resolve().parents[1] / "src" / "chaosx_bot" / "bot.py").read_text(
        encoding="utf-8"
    )
    assert "bot.answer_cache.get(question, live_commit=commit)" in source
    assert "bot.answer_cache.put(question, output, commit=bot.knowledge.commit_sha())" in source


def test_near_duplicate_question_hits(tmp_path):
    cache = AnswerCache(tmp_path / "a.db")
    cache.put("how does the chaos meter work", "It rises with tension.", commit="rev1")
    assert cache.get("how does the chaos meter actually work", live_commit="rev1") == "It rises with tension."


def test_near_duplicate_requires_the_same_index_revision(tmp_path):
    cache = AnswerCache(tmp_path / "a.db")
    cache.put("how does the chaos meter work", "It rises with tension.", commit="rev1")
    assert cache.get("how does the chaos meter actually work", live_commit="rev2") is None


def test_a_different_question_is_not_a_near_duplicate(tmp_path):
    cache = AnswerCache(tmp_path / "a.db")
    cache.put("how does the chaos meter work", "It rises with tension.", commit="rev1")
    assert cache.get("what is the chaos level cap", live_commit="rev1") is None


def test_stats_count_hits_and_misses(tmp_path):
    cache = AnswerCache(tmp_path / "a.db")
    cache.put("how does the chaos meter work", "It rises.")
    cache.get("how does the chaos meter work")
    cache.get("something never asked")
    assert cache.hits == 1 and cache.misses == 1
    assert "1 hits / 1 misses" in cache.stats_line()


def test_disabled_cache_neither_stores_nor_serves(tmp_path):
    cache = AnswerCache(tmp_path / "a.db", enabled=False)
    cache.put("how does the chaos meter work", "It rises.")
    assert cache.get("how does the chaos meter work") is None
