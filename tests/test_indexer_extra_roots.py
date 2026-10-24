"""Extra knowledge roots (agentic modding toolchains) index as public tooling docs."""

import sqlite3
from pathlib import Path

from chaosx_bot.indexer import (
    is_tooling_indexable,
    is_vault_indexable,
    parse_extra_roots,
    rebuild_index,
)


def _write(path: Path, text: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def test_parse_extra_roots_skips_missing_paths(tmp_path):
    root = tmp_path / "tools"
    root.mkdir()
    spec = f"tools={root}:ghost={tmp_path / 'nope'}"
    assert parse_extra_roots(spec) == [("tools", root)]


def test_parse_extra_roots_normalizes_names(tmp_path):
    root = tmp_path / "tools"
    root.mkdir()
    assert parse_extra_roots(f"HOI4 Live Tools = {root}") == [("hoi4-live-tools", root)]


def test_tooling_walker_skips_node_modules_and_binaries(tmp_path):
    root = tmp_path / "repo"
    keep = _write(root / "docs" / "focus.md", "focus rendering notes")
    _write(root / "node_modules" / "x" / "README.md", "junk")
    _write(root / "logo.png", "not text")
    assert is_tooling_indexable(root, keep) is True
    assert is_tooling_indexable(root, root / "node_modules" / "x" / "README.md") is False
    assert is_tooling_indexable(root, root / "logo.png") is False


def test_rebuild_indexes_extra_roots_under_tooling_prefix(tmp_path):
    repo = tmp_path / "mod"
    _write(repo / "README.md", "chaos redux mod readme")
    tools = tmp_path / "tooling"
    _write(tools / "docs" / "gui.md", "scripted gui rendering with the mcp")
    db = tmp_path / "test.db"

    stats = rebuild_index(repo, db, None, catalog_repo=None, extra_roots=[("hoi4-agent-tools", tools)])

    conn = sqlite3.connect(db)
    rows = dict(
        conn.execute("select path, source_class from source_docs where path like 'tooling/%'").fetchall()
    )
    conn.close()
    assert rows.get("tooling/hoi4-agent-tools/docs/gui.md") == "tooling_doc"
    assert stats.docs >= 2


def test_knowledge_passes_extra_roots_and_ranks_them(tmp_path):
    from chaosx_bot.knowledge import Knowledge, _authority

    repo = tmp_path / "mod"
    _write(repo / "README.md", "chaos redux")
    tools = tmp_path / "tools"
    _write(
        tools / "docs" / "comfyui.md",
        "comfyui portrait workflow for hoi4: batch folders, flux lora, dds export",
    )
    db = tmp_path / "k.db"

    knowledge = Knowledge(repo, db, None, None, catalog_repo=None, extra_roots=[("comfy", tools)])
    knowledge._ensure_index_sync()

    conn = sqlite3.connect(db)
    count = conn.execute(
        "select count(*) from source_docs where path like 'tooling/%' and source_class='tooling_doc'"
    ).fetchone()[0]
    conn.close()
    assert count == 1
    assert _authority("tooling/comfy/docs/comfyui.md") < 4
    context = knowledge.public_ask_context("comfyui portrait workflow")
    assert "comfyui" in context.casefold()


def test_tooling_vault_folder_is_indexable(tmp_path):
    vault = tmp_path / "vault"
    page = vault / "Tooling" / "HOI4 Agent Tools MCP.md"
    page.parent.mkdir(parents=True)
    page.write_text("mcp tooling notes", encoding="utf-8")
    assert is_vault_indexable(vault, page) is True


def test_settings_expose_extra_knowledge_roots():
    from chaosx_bot.config import Settings

    settings = Settings(_env_file=None, extra_knowledge_roots="a=/tmp/a")
    assert settings.extra_knowledge_roots == "a=/tmp/a"
