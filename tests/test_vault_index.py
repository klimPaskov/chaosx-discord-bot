from pathlib import Path

from chaosx_bot.vault_index import (
    refresh_vault_indexes,
    vault_indexes_are_stale,
)


def _make_vault(root: Path) -> dict[str, Path]:
    (root / "Events/Event Specs").mkdir(parents=True)
    (root / "Planning/Community Suggestions").mkdir(parents=True)
    notes = {
        "kept": root / "Events/Event Specs/007 - Fury.md",
        "deleted": root / "Events/Event Specs/008 - Gone.md",
        "suggestion": root / "Planning/Community Suggestions/Chaos Newspaper - abc123.md",
    }
    notes["kept"].write_text("# 007 - Fury\n\nFury details.\n", encoding="utf-8")
    notes["deleted"].write_text("# 008 - Gone\n\nThis note will be deleted.\n", encoding="utf-8")
    notes["suggestion"].write_text('---\ntitle: "Chaos Newspaper"\n---\n\n# Chaos Newspaper\n', encoding="utf-8")
    (root / "index.md").write_text("# Index\n\nSome prose here.\n", encoding="utf-8")
    (root / "Events/Events Index.md").write_text("# Events Index\n", encoding="utf-8")
    (root / "Planning/Community Suggestions/Community Suggestions Index.md").write_text(
        "# Community Suggestions\n", encoding="utf-8"
    )
    (root / "log.md").write_text("# Log\n", encoding="utf-8")
    return notes


def test_refresh_vault_indexes_updates_root_events_suggestions_and_log(tmp_path: Path):
    (tmp_path / "Events/Event Specs").mkdir(parents=True)
    (tmp_path / "Planning/Community Suggestions").mkdir(parents=True)
    (tmp_path / "Events/Event Specs/007 - Fury.md").write_text("# 007 - Fury\n\nFury details.\n", encoding="utf-8")
    suggestion = tmp_path / "Planning/Community Suggestions/Chaos Newspaper - abc123.md"
    suggestion.write_text('---\ntitle: "Chaos Newspaper"\n---\n\n# Chaos Newspaper\n', encoding="utf-8")
    (tmp_path / "index.md").write_text(
        "# Chaos Redux Wiki Index\n\n> Content catalog for the standalone Chaos Redux vault.\n"
        "> Last updated: 2000-01-01 | Total pages: 1\n\n## Events\n\n- stale\n\n## Planning\n\n- stale\n",
        encoding="utf-8",
    )
    (tmp_path / "Events/Events Index.md").write_text("# Events Index\n", encoding="utf-8")
    (tmp_path / "Planning/Community Suggestions/Community Suggestions Index.md").write_text(
        "# Community Suggestions\n", encoding="utf-8"
    )
    (tmp_path / "log.md").write_text("# Log\n", encoding="utf-8")

    result = refresh_vault_indexes(vault_path=tmp_path, reason="test update", changed_path=suggestion)

    assert result.updated_paths
    index = (tmp_path / "index.md").read_text(encoding="utf-8")
    assert "Events/Event Specs/007 - Fury.md" in index
    assert "Planning/Community Suggestions/Chaos Newspaper - abc123.md" in index
    events_index = (tmp_path / "Events/Events Index.md").read_text(encoding="utf-8")
    assert "CHAOSX:EVENT_SPECS:START" in events_index
    assert "Events/Event Specs/007 - Fury.md" in events_index
    suggestions_index = (tmp_path / "Planning/Community Suggestions/Community Suggestions Index.md").read_text(encoding="utf-8")
    assert "CHAOSX:COMMUNITY_SUGGESTIONS:START" in suggestions_index
    assert "Chaos Newspaper" in suggestions_index
    log = (tmp_path / "log.md").read_text(encoding="utf-8")
    assert "ChaosX vault index refresh" in log
    assert "test update" in log


def test_deleted_notes_stop_appearing_in_the_indexes(tmp_path: Path):
    """Hoops (2026-09-24): "they were deleted intentionally. So the bot shouldn't use deleted information ever."

    Deletions happen in Obsidian and arrive through the vault sync; the indexes used to keep listing the
    removed notes, and the bot read those filenames as if the notes still existed.
    """
    notes = _make_vault(tmp_path)
    refresh_vault_indexes(vault_path=tmp_path, reason="initial capture")
    events_index = tmp_path / "Events/Events Index.md"
    assert "008 - Gone" in events_index.read_text(encoding="utf-8")
    assert not vault_indexes_are_stale(vault_path=tmp_path)

    notes["deleted"].unlink()
    notes["suggestion"].unlink()

    assert vault_indexes_are_stale(vault_path=tmp_path) is True
    result = refresh_vault_indexes(vault_path=tmp_path, reason="after local deletion")

    events_text = events_index.read_text(encoding="utf-8")
    index_text = (tmp_path / "index.md").read_text(encoding="utf-8")
    assert "008 - Gone" not in events_text
    assert "007 - Fury" in events_text          # the surviving note stays listed
    assert "Chaos Newspaper" not in index_text
    assert result.updated_paths                # the deletion was picked up
    log = (tmp_path / "log.md").read_text(encoding="utf-8")
    assert "ChaosX vault index refresh" in log
    # and the vault is clean afterwards, so the automatic refresh cannot loop
    assert vault_indexes_are_stale(vault_path=tmp_path) is False


def test_freshness_probe_and_log_report_pruned_hand_written_links(tmp_path: Path):
    """A stale line the generator does not own is pruned, reported, and never left behind."""
    _make_vault(tmp_path)
    refresh_vault_indexes(vault_path=tmp_path, reason="initial capture")
    index = tmp_path / "index.md"
    stale = "- [[Events/Event Specs/999 - Never Existed|Never Existed]] - `Events/Event Specs/999 - Never Existed.md`"
    index.write_text(
        index.read_text(encoding="utf-8").replace("## Events", f"{stale}\n\n## Events", 1),
        encoding="utf-8",
    )
    assert vault_indexes_are_stale(vault_path=tmp_path) is True
    result = refresh_vault_indexes(vault_path=tmp_path, reason="prune pass")
    assert any("999 - Never Existed" in line for line in result.pruned_links)
    assert "999 - Never Existed" not in index.read_text(encoding="utf-8")
    assert "stale link line" in (tmp_path / "log.md").read_text(encoding="utf-8")
    assert vault_indexes_are_stale(vault_path=tmp_path) is False


def test_pruning_keeps_prose_and_live_links(tmp_path: Path):
    _make_vault(tmp_path)
    refresh_vault_indexes(vault_path=tmp_path, reason="initial capture")
    index = tmp_path / "index.md"
    prose = "Prose mentioning `Events/Event Specs/007 - Fury.md` inline."
    stale = "- [[Events/Event Specs/999 - Never Existed|Never Existed]] - `Events/Event Specs/999 - Never Existed.md`"
    live = "- [[Events/Event Specs/007 - Fury|007 - Fury]] - `Events/Event Specs/007 - Fury.md`"
    # everything goes above the managed "## Events" section, i.e. outside the generator's own block
    index.write_text(
        index.read_text(encoding="utf-8").replace("## Events", f"{prose}\n{stale}\n{live}\n\n## Events", 1),
        encoding="utf-8",
    )
    refresh_vault_indexes(vault_path=tmp_path, reason="prune pass")
    text = index.read_text(encoding="utf-8")
    assert prose in text            # prose survives even though it names a real note
    assert live in text             # a link to an existing note survives
    assert stale not in text        # a pure link line to a missing note goes


def test_knowledge_repairs_stale_vault_indexes_automatically(tmp_path: Path):
    from chaosx_bot.knowledge import Knowledge

    notes = _make_vault(tmp_path)
    refresh_vault_indexes(vault_path=tmp_path, reason="initial capture")
    notes["deleted"].unlink()

    knowledge = Knowledge.__new__(Knowledge)
    object.__setattr__(knowledge, "vault_path", tmp_path)
    assert knowledge._refresh_vault_indexes_if_stale() is True
    assert "008 - Gone" not in (tmp_path / "Events/Events Index.md").read_text(encoding="utf-8")
    # second call is a no-op, so an index refresh can never spin
    assert knowledge._refresh_vault_indexes_if_stale() is False


def test_rebuild_prunes_index_rows_for_deleted_vault_notes(tmp_path: Path):
    """A deleted note must not survive as a searchable document."""
    import sqlite3

    from chaosx_bot.indexer import rebuild_index

    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "README.md").write_text("# Repo readme\n", encoding="utf-8")
    vault = tmp_path / "vault"
    (vault / "Events/Event Specs").mkdir(parents=True)
    note = vault / "Events/Event Specs/008 - Gone.md"
    note.write_text("# 008 - Gone\n\nUNIQUEMARKERZZTOP appears only here.\n", encoding="utf-8")
    db = tmp_path / "index.db"

    rebuild_index(repo, db, vault)
    conn = sqlite3.connect(db)
    rows = conn.execute("SELECT path FROM source_docs WHERE path LIKE 'vault/Events/Event Specs/%'").fetchall()
    marked = conn.execute("SELECT COUNT(*) FROM source_docs WHERE content LIKE '%UNIQUEMARKERZZTOP%'").fetchone()[0]
    conn.close()
    assert rows, "the note should be indexed while it exists"

    note.unlink()
    rebuild_index(repo, db, vault)
    conn = sqlite3.connect(db)
    remaining = conn.execute("SELECT path FROM source_docs WHERE path LIKE 'vault/Events/Event Specs/%'").fetchall()
    hits = conn.execute("SELECT COUNT(*) FROM source_docs WHERE content LIKE '%UNIQUEMARKERZZTOP%'").fetchone()[0]
    conn.close()
    assert remaining == []
    assert hits == 0 and marked == 1
