"""Members' vocabulary and document authority: the two things that decide what retrieval sees first."""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from chaosx_bot.knowledge import _authority, _query_terms, _row_sort_key, expand_terms


def test_super_event_is_one_concept():
    terms = expand_terms(_query_terms("explain the super event text"))
    assert "superevent" in terms


def test_focus_tree_maps_to_focus():
    terms = expand_terms(_query_terms("how do focus trees interact with chaos"))
    assert "focus_tree" in terms


def test_expansion_never_invents_terms_out_of_thin_air():
    # "there" is a stopword, so the only term left is "hello" and it has no synonyms
    assert expand_terms(_query_terms("hello there")) == ["hello"]


def test_expansion_preserves_the_question_order():
    terms = expand_terms(_query_terms("super event text"))
    assert terms.index("super") < terms.index("event") < terms.index("text")


def test_expansion_does_not_duplicate():
    terms = expand_terms(_query_terms("super superevent event"))
    assert len(terms) == len(set(terms))


def test_authority_ranks_the_vault_above_raw_ingest():
    assert _authority("vault/Events/x.md") < _authority("docs/Events/x.md") < _authority("raw/repo-docs/x.txt")
    assert _authority("unknown/place.md") == 4
    assert _authority("README.md") == 0  # root docs are curated entry points


def test_sort_key_puts_authority_first_then_relevance():
    rows = [
        ("raw/repo-docs/x.txt", "raw", "snippet", -9.0),
        ("docs/Events/x.md", "docs", "snippet", -3.0),
        ("vault/Events/x.md", "vault", "snippet", -1.0),
    ]
    ordered = sorted(rows, key=_row_sort_key)
    assert [r[0] for r in ordered] == [
        "vault/Events/x.md",
        "docs/Events/x.md",
        "raw/repo-docs/x.txt",
    ]
