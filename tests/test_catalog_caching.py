from __future__ import annotations

import time
from pathlib import Path

from chaosx_bot.event_visuals import EventChainCatalog, ScriptedGuiCatalog
from chaosx_bot.focus_trees import FocusTreeCatalog
from chaosx_bot.knowledge import Knowledge


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _write_gui(root: Path, name: str, window: str, gui_id: str = "test_gui") -> None:
    _write(
        root / name,
        f"""
        scripted_gui = {{
          {gui_id} = {{
            context_type = player_context
            window_name = {window}
          }}
        }}
        """,
    )


def test_scripted_gui_catalog_memoizes_and_refreshes_dynamically(
    tmp_path: Path,
) -> None:
    root = tmp_path / "common" / "scripted_guis"
    root.mkdir(parents=True)
    _write_gui(root, "001_a.txt", "window_a")

    catalog = ScriptedGuiCatalog(tmp_path)
    first = catalog.discover()
    assert [record.window_name for record in first] == ["window_a"]

    # Repeated lookups hit the memoized snapshot without re-scanning.
    assert catalog.discover() is first

    # New files appear automatically once the snapshot is invalidated, so
    # catalog entries stay dynamic without restarts or manual cache clears.
    _write_gui(root, "001_b.txt", "window_b")
    catalog._cache.invalidate()
    second = catalog.discover()
    assert {record.window_name for record in second} == {"window_a", "window_b"}


def test_event_chain_catalog_memoizes_discovery(tmp_path: Path) -> None:
    events = tmp_path / "events"
    events.mkdir(parents=True)
    _write(events / "001_test.txt", "country_event = { id = test.1 }")

    catalog = EventChainCatalog(tmp_path)
    first = catalog.discover()
    assert len(first) == 1
    assert catalog.discover() is first


def test_focus_catalog_single_pass_package_tags(tmp_path: Path) -> None:
    repo = tmp_path / "mod"
    _write(
        repo / "common/national_focus/010_death.txt",
        """
        focus_tree = {
            id = death_focus_tree
            country = { factor = 0 modifier = { add = 10 is_death_country = yes } }
            focus = { id = DEATH_START }
        }
        """,
    )
    _write(repo / "common/country_tags/010_death.txt", 'DTH = "countries/Death.txt"\n')
    _write(
        repo / "events/010_death_events.txt",
        "country_event = { id = death.1\n    DTH = { }\n}\n",
    )

    record = FocusTreeCatalog(repo).for_event(10)[0]

    assert record.country_tags == ()
    assert record.package_country_tags == ("DTH",)
    assert record.asset_country_tags == ("DTH",)


def test_knowledge_freshness_check_does_not_block_commands(tmp_path: Path) -> None:
    events = tmp_path / "events"
    events.mkdir(parents=True)
    _write(events / "001_test.txt", "country_event = { id = test.1 }")

    knowledge = Knowledge(tmp_path, tmp_path / "index.db")
    knowledge.ensure_index()  # cold build completes before any lookup
    knowledge.ensure_index()  # throttled — instant

    # The next due check spawns a background refresh instead of blocking the
    # command on full-repo scans.
    object.__setattr__(knowledge, "_last_freshness_check", 0.0)
    started = time.monotonic()
    knowledge.ensure_index()
    elapsed = time.monotonic() - started
    assert elapsed < 2.0
