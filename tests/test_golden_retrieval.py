"""Retrieval must reach the right document for known questions. No model call, no network, ~1 s."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from chaosx_bot.config import load_settings
from chaosx_bot.knowledge import Knowledge

GOLDEN = json.loads((Path(__file__).parent / "golden_questions.json").read_text(encoding="utf-8"))


def _knowledge() -> Knowledge:
    settings = load_settings()
    try:
        from chaosx_bot.db import connect

        conn = connect(settings.db_path)
        try:
            count = conn.execute("SELECT COUNT(*) FROM source_docs").fetchone()[0]
        finally:
            conn.close()
    except Exception:
        pytest.skip("no index database on this host")
    if not count:
        pytest.skip("index is empty on this host")
    return Knowledge(
        settings.chaos_redux_repo,
        settings.db_path,
        settings.obsidian_vault_path,
        settings.qoder_repowiki_path,
        catalog_repo=settings.focus_tree_repo or settings.chaos_redux_repo,
        autorebuild=False,
    )


@pytest.mark.parametrize("case", GOLDEN, ids=[c["q"][:32] for c in GOLDEN])
def test_retrieval_reaches_the_expected_document(case):
    knowledge = _knowledge()
    rows = knowledge.public_ask_rows(case["q"], limit=8)
    blob = " ".join(str(row[0]) for row in rows).lower()
    assert case["expect_path"].lower() in blob, f"{case['q']!r} -> {blob[:300]}"
