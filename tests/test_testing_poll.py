"""The ballot covers every candidate, pages long families, and can hold free-text nominations."""
from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from chaosx_bot.testing_poll import (  # noqa: E402
    FAMILIES,
    MAX_NOMINATIONS_PER_MEMBER,
    MAX_SELECT_OPTIONS,
    candidate_detail,
    candidate_key,
    candidate_kind,
    candidate_label,
    clamp_label,
    family_label,
    is_safe_nomination,
    nomination_slug,
    split_pages,
)


def test_candidate_labels_lead_with_the_catalog_id():
    """Hoops (2026-09-24): "The IDs should be visible for what you want to set for testing." """
    assert candidate_label("event", 3, "The Holy Realm") == "Event 003: The Holy Realm"
    assert candidate_label("event", "39", "Murder Mystery") == "Event 039: Murder Mystery"
    assert candidate_label("scenario", 1, "Zombie Apocalypse") == "SCN-001: Zombie Apocalypse"
    assert candidate_label("scenario", "SCN-007", "Named") == "SCN-007: Named"
    assert candidate_label("cluster", 11, "Various Anomalies") == "Cluster 11: Various Anomalies"
    assert candidate_label("nomination", None, "Convoy payouts") == "Nomination: Convoy payouts"
    # a label with no name still identifies the candidate
    assert candidate_label("event", 12, "") == "Event 012"


def test_candidate_detail_says_what_it_is():
    assert candidate_detail("event", "Minor Fire-Once · cluster 16") == "Minor Fire-Once · cluster 16 - needs testing"
    assert candidate_detail("scenario") == "scenario needs testing"
    assert candidate_detail("nomination") == "nominated by a member"


def test_candidate_keys_are_family_scoped():
    assert candidate_key("event", "7") == "event:7"
    assert candidate_key("scenario", 2) == "scenario:2"
    assert candidate_kind("cluster:11") == "cluster"
    assert candidate_kind("nomination:convoy-payouts") == "nomination"


def test_every_family_has_a_label_and_is_known():
    assert set(FAMILIES) == {"event", "scenario", "cluster", "nomination"}
    for kind in FAMILIES:
        assert family_label(kind)


def test_long_families_are_paged_not_truncated():
    # 29 events marked Needs Testing on the live catalog: all of them must be reachable
    events = [(candidate_key("event", str(index)), f"Event {index}") for index in range(1, 30)]
    pages = split_pages(events)
    assert len(pages) == 2
    assert len(pages[0]) == MAX_SELECT_OPTIONS == 25
    assert len(pages[1]) == 4
    assert [item for page in pages for item in page] == events  # nothing dropped, nothing duplicated
    assert split_pages([]) == [[]]


def test_nomination_slug_is_stable_and_readable():
    assert nomination_slug("Convoy payouts!") == "convoy-payouts"
    assert nomination_slug("  Border   gore ") == "border-gore"
    assert nomination_slug("") == "nomination"


def test_labels_are_trimmed_on_a_word_boundary():
    long = "word " * 40
    trimmed = clamp_label(long, limit=20)
    assert len(trimmed) <= 20 and not trimmed.endswith(" ")


def test_nominations_cannot_smuggle_a_ping():
    assert is_safe_nomination("the convoy system") is True
    assert is_safe_nomination("@everyone look") is False
    assert is_safe_nomination("ping <@123456789>") is False
    assert is_safe_nomination("role <@&999>") is False
    assert MAX_NOMINATIONS_PER_MEMBER >= 1


def test_catalog_candidates_cover_all_three_families(tmp_path):
    """`testing_candidates` reads the catalogs live: everything marked Needs Testing is on the ballot."""
    from chaosx_bot.knowledge import Knowledge

    db_path = tmp_path / "catalog.db"
    conn = sqlite3.connect(db_path)
    conn.executescript(
        """
        CREATE TABLE catalog_events (event_id TEXT, name TEXT, status TEXT, type TEXT, cluster_id TEXT);
        CREATE TABLE catalog_scenarios (scenario_id TEXT, name TEXT, status TEXT);
        CREATE TABLE catalog_clusters (cluster_id TEXT, name TEXT, status TEXT);
        """
    )
    rows = {
        "catalog_events": [("1", "Communist Insurgency", "Needs Testing"), ("2", "Zombie Outbreak", "Implemented")],
        # type/cluster are supplied by the executemany below (the event table carries them)
        "catalog_scenarios": [("1", "Zombie Apocalypse", "Needs Testing")],
        "catalog_clusters": [("11", "Various Anomalies", "Needs Testing"), ("12", "Pacts", "Planned")],
    }
    conn.executemany(
        "INSERT INTO catalog_events VALUES (?, ?, ?, 'Minor Fire-Once', '16')",
        [row for row in rows["catalog_events"]],
    )
    for table in ("catalog_scenarios", "catalog_clusters"):
        conn.executemany(f"INSERT INTO {table} VALUES (?, ?, ?)", rows[table])
    conn.commit()
    conn.close()

    # Knowledge is a frozen dataclass; bypass __init__ but keep the query path real
    knowledge = Knowledge.__new__(Knowledge)
    object.__setattr__(knowledge, "db_path", db_path)  # indexer's connect() expects a Path
    object.__setattr__(knowledge, "ensure_index", lambda: None)

    grouped = knowledge.testing_candidates()
    # every entry carries the ID in the label a member actually reads
    assert grouped["event"] == [("event:1", "Event 001: Communist Insurgency", "Minor Fire-Once · cluster 16 - needs testing")]
    assert grouped["scenario"] == [("scenario:1", "SCN-001: Zombie Apocalypse", "scenario needs testing")]
    assert grouped["cluster"] == [("cluster:11", "Cluster 11: Various Anomalies", "cluster needs testing")]
    # nothing that is not marked for testing leaks onto the ballot
    flat = [key for items in grouped.values() for key, _label, _detail in items]
    assert "event:2" not in flat and "cluster:12" not in flat


def test_panel_counts_every_candidate(tmp_path):
    """The panel's family buttons report the true candidate counts, not a fixed five."""
    from chaosx_bot.bot import TestingPanelView, ChaosXBot
    from chaosx_bot.config import Settings

    bot = ChaosXBot(Settings(discord_token="dummy"))
    view = TestingPanelView(bot, counts={"event": 29, "scenario": 15, "cluster": 11, "nomination": 2})
    labels = [child.label for child in view.children]
    assert any("(29)" in label for label in labels)
    assert any("(15)" in label for label in labels)
    custom_ids = [child.custom_id for child in view.children]
    # exactly one nomination button ("Nominated" is a family button and contains the same word)
    assert sum(custom_id == "chaosx_test_nominate" for custom_id in custom_ids) == 1
    # persistent ids, so a panel posted weeks ago still routes
    assert "chaosx_test_family_event" in custom_ids
    assert "chaosx_test_nominate" in custom_ids
    assert "chaosx_test_clear" in custom_ids


def test_select_view_pages_candidates(tmp_path):
    from chaosx_bot.bot import TestingCandidateSelectView, ChaosXBot
    from chaosx_bot.config import Settings

    bot = ChaosXBot(Settings(discord_token="dummy"))
    candidates = [
        (candidate_key("event", str(index)), f"Event {index:03d}: Name {index}", "Minor Fire-Once - needs testing")
        for index in range(1, 30)
    ]
    view = TestingCandidateSelectView(bot, kind="event", candidates=candidates)
    select = next(child for child in view.children if child.custom_id.startswith("chaosx_test_pick"))
    assert len(select.options) == MAX_SELECT_OPTIONS
    # the ID is on the option a member picks, and the detail is the option's second line
    assert select.options[0].label == "Event 001: Name 1"
    assert select.options[0].description == "Minor Fire-Once - needs testing"
    assert len(view.pages) == 2 and "page 1/2" in view.page_note
    second = TestingCandidateSelectView(bot, kind="event", candidates=candidates, page=1)
    assert len(next(c for c in second.children if c.custom_id.startswith("chaosx_test_pick")).options) == 4
