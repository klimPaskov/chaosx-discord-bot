import os
from pathlib import Path
from shutil import copy2
import sqlite3
import time

import pytest

from chaosx_bot.indexer import (
    CatalogReadError,
    _catalog_rows,
    catalog_fingerprint,
    index_commit,
    is_qoder_indexable,
    is_vault_indexable,
    rebuild_index,
)
from chaosx_bot.knowledge import Knowledge, _fts_and_query, _fts_query


def _first_existing(*candidates: str) -> Path | None:
    """First candidate path that exists, so the test works on the PC and on the bot host alike.

    These were hardcoded to one machine (/home/klim/... and the Windows vault mount), which meant the test quietly
    exercised less on the host instead of failing.
    """
    for candidate in candidates:
        path = Path(candidate).expanduser()
        if path.exists():
            return path
    return None


REPO = _first_existing("/home/klim/projects/chaos_redux", "/home/ubuntu/chaosx/mod/current", "~/chaosx/mod/current")
VAULT = _first_existing(
    "/mnt/c/Users/klimp/Documents/Chaos Redux Vault",
    "/home/ubuntu/chaosx/chaos-redux-vault",
    "~/chaosx/chaos-redux-vault",
)

# A host may serve an index built where the authoritative corpus and catalog live (the PC's working tree), in which
# case rebuilding here produces a different revision — different doc counts, different event numbering — and the
# corpus tests would be asserting against the wrong tree. That host sets CHAOSX_INDEX_AUTOREBUILD=0 in the bot's
# .env, so ask the settings loader rather than os.environ (a .env file is not exported to the test process).
def _autorebuild_enabled() -> bool:
    try:
        from chaosx_bot.config import load_settings

        return bool(load_settings().index_autorebuild)
    except Exception:
        return True  # cannot tell: assume this host owns the corpus and run the real assertions


AUTHORITATIVE_CORPUS = _autorebuild_enabled()
SKIP_NO_CORPUS = pytest.mark.skipif(
    not AUTHORITATIVE_CORPUS, reason="serves an index built elsewhere; the corpus here is not authoritative"
)
SKIP_NO_CORPUS


def test_knowledge_uses_and_refreshes_from_live_catalog_repo(tmp_path: Path):
    repo = tmp_path / "knowledge"
    (repo / "docs").mkdir(parents=True)
    (repo / "docs/readme.md").write_text("Chaos Redux knowledge.", encoding="utf-8")
    old_catalog = tmp_path / "old-catalog"
    live_catalog = tmp_path / "live-catalog"
    header = (
        "ID,Event Name,Details,Evo I,Evo II,Evo III,Evo IV,Evo V,"
        "World-End Scenario,Type,Cluster ID,Member Severity,Status\n"
    )
    rows = {
        old_catalog: "18,Resources found,Old catalog,Stage 1,,,,,,Minor,,,Draft\n",
        live_catalog: (
            "18,A Rich Find,Live catalog,Stage 1,Stage 2,Stage 3,Stage 4,,"
            ",Major,,,Playable\n"
        ),
    }
    for root, row in rows.items():
        spreadsheets = root / "docs/spreadsheets"
        spreadsheets.mkdir(parents=True)
        (spreadsheets / "chaos_redux_events_catalog.csv").write_text(
            header + row,
            encoding="utf-8",
        )

    db = tmp_path / "live-catalog.db"
    rebuild_index(repo, db, catalog_repo=old_catalog)
    stale = Knowledge(repo, db, catalog_repo=old_catalog).event("18")
    assert "Resources found" in stale

    refreshed = Knowledge(repo, db, catalog_repo=live_catalog)
    refreshed.event("18")  # non-blocking: spawns the background refresh
    # Wait on the refresh itself instead of polling the database. Polling produced a flake that passed alone
    # and failed under a full-file run; the thread handle makes the wait exact and surfaces a real error.
    thread = refreshed._refresh_thread
    assert thread is not None, "no background refresh was started"
    thread.join(timeout=180)
    assert not thread.is_alive(), "background refresh did not finish within 180s"
    assert refreshed._refresh_error is None, f"refresh failed: {refreshed._refresh_error!r}"
    out = refreshed.event("18")
    assert "A Rich Find" in out
    assert "Evolution stages: `4`" in out
    assert "Status: `Playable`" in out
    conn = sqlite3.connect(db)
    try:
        meta = dict(conn.execute("SELECT key, value FROM index_meta"))
    finally:
        conn.close()
    assert meta["catalog_fingerprint"] == catalog_fingerprint(live_catalog)


def test_catalog_workbook_takes_precedence_over_stale_csv(tmp_path: Path):
    source = Path('/home/klim/projects/chaos_redux/docs/spreadsheets/chaos_redux_events_catalog.xlsx')
    if not source.exists():
        return
    catalog_root = tmp_path / 'docs/spreadsheets'
    catalog_root.mkdir(parents=True)
    copy2(source, catalog_root / source.name)
    (catalog_root / 'chaos_redux_events_catalog.csv').write_text(
        'ID,Event Name,Details\n2,Stale CSV Name,Old details\n',
        encoding='utf-8',
    )

    rows = _catalog_rows(
        tmp_path,
        csv_name='chaos_redux_events_catalog.csv',
        sheet_index=1,
    )
    event_two = next(row for row in rows if row.get('ID') == '2')
    assert event_two['Event Name'] == 'Zombie Outbreak'


def test_unreadable_workbook_never_silently_uses_stale_csv(tmp_path: Path):
    catalog_root = tmp_path / 'docs/spreadsheets'
    catalog_root.mkdir(parents=True)
    (catalog_root / 'chaos_redux_events_catalog.xlsx').write_bytes(b'incomplete-save')
    (catalog_root / 'chaos_redux_events_catalog.csv').write_text(
        'ID,Event Name,Details\n2,Stale CSV Name,Old details\n',
        encoding='utf-8',
    )

    with pytest.raises(CatalogReadError):
        _catalog_rows(
            tmp_path,
            csv_name='chaos_redux_events_catalog.csv',
            sheet_index=1,
        )


def test_vault_index_whitelist_and_secret_exclusions(tmp_path: Path):
    vault = tmp_path / 'vault'
    allowed = vault / 'Events/Event Specs/001 - Test.md'
    allowed.parent.mkdir(parents=True)
    allowed.write_text('public event spec', encoding='utf-8')
    secret = vault / 'important tokens.md'
    secret.write_text('token=never-index', encoding='utf-8')
    raw = vault / 'raw/repo-docs/raw.md'
    raw.parent.mkdir(parents=True)
    raw.write_text('raw ingest detail', encoding='utf-8')
    personalish = vault / 'Daily/private.md'
    personalish.parent.mkdir(parents=True)
    personalish.write_text('not chaos public ask material', encoding='utf-8')

    assert is_vault_indexable(vault, allowed)
    assert not is_vault_indexable(vault, secret)
    assert not is_vault_indexable(vault, raw)
    assert not is_vault_indexable(vault, personalish)


def test_qoder_repowiki_knowledge_root(tmp_path: Path):
    repo = tmp_path / 'repo'
    (repo / 'docs').mkdir(parents=True)
    (repo / 'events').mkdir(parents=True)
    (repo / 'README.md').write_text('# Fake repo\n', encoding='utf-8')
    qoder = tmp_path / 'master__en-US'
    (qoder / 'repo-knowledge').mkdir(parents=True)
    (qoder / 'codebase').mkdir(parents=True)
    (qoder / 'repo-knowledge' / 'build_system.md').write_text(
        '# Build System\nGrumbleflarn pipeline notes for the mod build.\n',
        encoding='utf-8',
    )
    (qoder / 'codebase' / 'index.md').write_text(
        '# Codebase Index\nGrumbleflarn modules and entry points.\n',
        encoding='utf-8',
    )
    (qoder / '.export-hash').write_text('abc123', encoding='utf-8')
    (qoder / 'cache').mkdir(parents=True)
    (qoder / 'cache' / 'noise.json').write_text('{"ignored": true}', encoding='utf-8')

    assert is_qoder_indexable(qoder, qoder / 'repo-knowledge' / 'build_system.md')
    assert not is_qoder_indexable(qoder, qoder / '.export-hash')
    assert not is_qoder_indexable(qoder, qoder / 'cache' / 'noise.json')

    db = tmp_path / 'qoder-test.db'
    stats = rebuild_index(repo, db, qoder_path=qoder)
    conn = sqlite3.connect(db)
    try:
        rows = conn.execute(
            "SELECT path, source_class FROM source_docs WHERE path LIKE 'qoder/%' ORDER BY path"
        ).fetchall()
        meta = dict(conn.execute("SELECT key, value FROM index_meta"))
        assert meta['qoder_doc_count'] == '2'
        assert ('qoder/repo-knowledge/build_system.md', 'qoder_repo_knowledge') in rows
        assert ('qoder/codebase/index.md', 'qoder_codebase') in rows
        assert conn.execute("SELECT COUNT(*) FROM source_docs WHERE path LIKE '%export-hash%'").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM source_docs WHERE path LIKE '%cache%'").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM source_docs").fetchone()[0] == stats.docs
        assert 'qoder:' in conn.execute("SELECT value FROM index_meta WHERE key = 'commit_sha'").fetchone()[0]
    finally:
        conn.close()

    knowledge = Knowledge(repo, db, qoder_path=qoder)
    search = knowledge.search('Grumbleflarn')
    assert 'qoder/repo-knowledge/build_system.md' in search
    ask_context = knowledge.public_ask_context('Grumbleflarn')
    assert 'Grumbleflarn' in ask_context


def test_qoder_fingerprint_tracks_export_hash(tmp_path: Path):
    qoder = tmp_path / 'master__en-US'
    (qoder / 'repo-knowledge').mkdir(parents=True)
    (qoder / 'repo-knowledge' / 'build_system.md').write_text('# Build\nnotes\n', encoding='utf-8')
    (qoder / '.export-hash').write_text('hash-v1', encoding='utf-8')
    repo = tmp_path / 'repo'
    repo.mkdir()

    fingerprint_v1 = index_commit(repo, qoder_path=qoder)
    assert fingerprint_v1.endswith('qoder:hash-v1')
    (qoder / '.export-hash').write_text('hash-v2', encoding='utf-8')
    fingerprint_v2 = index_commit(repo, qoder_path=qoder)
    assert fingerprint_v2.endswith('qoder:hash-v2')
    assert fingerprint_v1 != fingerprint_v2


def test_fts_query_builders_never_produce_column_filter_syntax():
    # Hoops report 2026-08-26: /ask failed with "no such column: code" when a
    # user query contained a hyphen-joined token. FTS5 misparses a bare
    # `x-code` / `event-5` MATCH term as a column filter. The builders must
    # split hyphens and quote every term so MATCH only sees literal terms.
    assert _fts_and_query("event-5") == '"event" AND "5"'
    assert _fts_and_query("mod-code") == '"mod" AND "code"'
    assert _fts_and_query("what is the code for event 5") == '"what" AND "is" AND "the" AND "code" AND "for" AND "event" AND "5"'
    assert _fts_query("event-5") == '"event" OR "5"'
    assert _fts_query("") == '""'
    assert _fts_and_query("") == '""'
    # Plain queries unchanged in shape (quoted terms, no hyphens needed).
    assert _fts_and_query("zombie outbreak") == '"zombie" AND "outbreak"'
    assert _fts_query("zombie outbreak") == '"zombie" OR "outbreak"'
