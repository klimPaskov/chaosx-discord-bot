"""A source table is only attached when it is about the question (Hoops, 2026-11-05)."""

from __future__ import annotations

from chaosx_bot.web_sources import EvidenceImage, evidence_matches_question


def _image(title: str = "", url: str = "") -> EvidenceImage:
    return EvidenceImage(
        png=b"\x89PNG",
        filename="chaosx-source-table.png",
        label=f"Source table — {(url.split('/')[2] if '//' in url else 'source')}",
        source_title=title,
        source_url=url,
    )


def test_table_about_the_question_is_kept():
    image = _image("France - Wikipedia", "https://en.wikipedia.org/wiki/France")
    assert evidence_matches_question(image, "what is the current population of France?")


def test_unrelated_table_is_dropped():
    image = _image("Swedish grammar - Wikipedia", "https://en.wikipedia.org/wiki/Swedish_grammar")
    assert not evidence_matches_question(image, "kill all clankers")
    assert not evidence_matches_question(image, "how does Zombie Outbreak work?")


def test_no_question_keywords_means_no_attachment():
    image = _image("France - Wikipedia", "https://en.wikipedia.org/wiki/France")
    assert not evidence_matches_question(image, "hi")
    assert not evidence_matches_question(image, "")
    assert not evidence_matches_question(None, "population of France")


def test_the_host_alone_can_match():
    image = _image("", "https://www.hoi4.org/tech-tree")
    assert evidence_matches_question(image, "hoi4 tech tree values")
