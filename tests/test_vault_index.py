from pathlib import Path

from chaosx_bot.vault_index import refresh_vault_indexes


def test_refresh_vault_indexes_updates_root_events_suggestions_and_log(tmp_path: Path):
    (tmp_path / "Events/Event Specs").mkdir(parents=True)
    (tmp_path / "Planning/Community Suggestions").mkdir(parents=True)
    (tmp_path / "Events/Event Specs/007 - Fury.md").write_text("# 007 - Fury\n\nFury details.\n", encoding="utf-8")
    suggestion = tmp_path / "Planning/Community Suggestions/Chaos Newspaper - abc123.md"
    suggestion.write_text("---\ntitle: \"Chaos Newspaper\"\n---\n\n# Chaos Newspaper\n", encoding="utf-8")
    (tmp_path / "index.md").write_text(
        "# Chaos Redux Wiki Index\n\n> Content catalog for the standalone Chaos Redux vault.\n> Last updated: 2000-01-01 | Total pages: 1\n\n## Events\n\n- stale\n\n## Planning\n\n- stale\n",
        encoding="utf-8",
    )
    (tmp_path / "Events/Events Index.md").write_text("# Events Index\n", encoding="utf-8")
    (tmp_path / "Planning/Community Suggestions/Community Suggestions Index.md").write_text("# Community Suggestions\n", encoding="utf-8")
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
