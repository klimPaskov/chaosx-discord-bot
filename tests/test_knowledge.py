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
from chaosx_bot.knowledge import Knowledge


def test_rebuild_index_and_event_lookup(tmp_path: Path):
    repo = Path('/home/klim/projects/chaos_redux')
    if not repo.exists():
        return
    vault = Path('/mnt/c/Users/klimp/Documents/Chaos Redux Vault')
    db = tmp_path / 'chaosx-test.db'
    stats = rebuild_index(repo, db, vault if vault.exists() else None)
    assert stats.docs > 100
    assert stats.events >= 180
    assert stats.scenarios >= 7
    assert stats.clusters >= 12
    knowledge = Knowledge(repo, db, vault if vault.exists() else None)
    event = knowledge.event('2')
    assert 'Zombie Outbreak' in event
    event_lines = event.splitlines()
    assert event_lines[1].startswith('- Type:')
    assert event_lines[2] == '- Chaos level: `unknown`'
    assert event_lines[3] == '- Evolution stages: `3`'
    assert event_lines[4] == '- World-end scenario(s): `Yes`'
    assert event_lines[5].startswith('- Status:')
    assert '### Evolution stages' in event
    assert '### World-end scenario' in event
    assert 'Evolution tracks' not in event
    assert 'World-end relationship' not in event
    assert 'Evidence:' not in event
    assert 'docs/spreadsheets' not in event
    assert knowledge.event('999') == 'No event for id `999` was found.'
    assert knowledge.event('event 999') == 'No event for id `999` was found.'
    scenario_miss = knowledge.scenario('10')
    assert scenario_miss == 'No scenario for id `10` was found.'
    assert 'Search results' not in scenario_miss
    assert knowledge.scenario('SCN-999') == 'No scenario for id `999` was found.'
    assert knowledge.cluster('999') == 'No cluster for id `999` was found.'
    assert knowledge.cluster('cluster 999') == 'No cluster for id `999` was found.'
    assert 'Fully Functional' in knowledge.event('4')
    clustered_event = knowledge.event('4')
    assert '- Chaos level: `unknown`' in clustered_event
    search = knowledge.search('Zombie Outbreak')
    assert 'Evidence:' not in search
    assert 'docs/specs/' not in search
    owner_event = knowledge.event('2', show_evidence=True)
    assert 'Private source detail' in owner_event
    scenario = knowledge.scenario('5')
    assert 'SCN-005: World in Fury' in scenario
    assert '- Type options: Pact creates' in scenario
    assert '- Intensity scaling: Low/Medium/High/Maximum' in scenario
    assert '- Status: `Needs Testing`' in scenario
    assert 'Soviet Union Collapse' not in scenario
    assert 'Event 2:' not in scenario
    assert 'docs/spreadsheets' not in scenario
    owner_scenario = knowledge.scenario('5', show_evidence=True)
    assert 'chaos_redux_events_catalog.xlsx' in owner_scenario
    owner_search = knowledge.search('Zombie Outbreak', limit=2, show_evidence=True)
    assert 'Evidence:' in owner_search
    search = knowledge.search('Zombie Outbreak', limit=2)
    assert 'Search results' in search
    cluster = knowledge.cluster('1')
    assert 'Cluster 1: Wars' in cluster
    assert '004` Random War' in cluster
    assert '007` Fury' in cluster
    ask_context = knowledge.public_ask_context('Zombie Outbreak')
    assert ask_context
    assert 'docs/' not in ask_context
    assert 'accepted_source_specification' not in ask_context
    if vault.exists():
        fury_context = knowledge.public_ask_context('Fury aggressor model')
        assert 'Fury' in fury_context or 'aggressor' in fury_context
        conn = sqlite3.connect(db)
        try:
            vault_docs = conn.execute("SELECT COUNT(*) FROM source_docs WHERE path LIKE 'vault/%'").fetchone()[0]
            hidden_docs = conn.execute("SELECT COUNT(*) FROM source_docs WHERE lower(path) LIKE '%important tokens%'").fetchone()[0]
        finally:
            conn.close()
        assert vault_docs > 0
        assert hidden_docs == 0
    ask_context_with_sources = knowledge.public_ask_context('Zombie Outbreak source path', include_sources=True)
    assert 'Source:' in ask_context_with_sources
    assert 'docs/' in ask_context_with_sources or 'events/' in ask_context_with_sources or 'common/' in ask_context_with_sources
    status = knowledge.status()
    assert 'Events:' in status
    assert 'Repeatable events:' in status
    assert 'Fire-once events:' in status
    assert 'Triggerable scenarios:' in status
    assert 'Known' not in status
    assert 'Indexed commit' not in status
    assert 'source docs' not in status
    testing = knowledge.testing_queue()
    assert '## Testing queue' in testing
    assert 'Use this before playtesting' in testing
    assert 'Event ' in testing


def test_public_ask_context_never_empty_on_healthy_index(tmp_path: Path):
    repo = Path('/home/klim/projects/chaos_redux')
    if not repo.exists():
        return
    db = tmp_path / 'chaosx-fallback.db'
    stats = rebuild_index(repo, db, None)
    assert stats.docs > 0
    knowledge = Knowledge(repo, db, None)
    # Positive: tokens with no index hits fall back to the project digest, so
    # the answering model always receives real docs/notes/code ground truth.
    fallback = knowledge.public_ask_context('zzzzqqqq xxxxxyyyy')
    assert fallback
    assert any(
        term in fallback.lower()
        for term in ('redux', 'event', 'scenario', 'mod', 'hoi4', 'mechanic', 'focus', 'chaos')
    )
    # Negative: a real query still returns its own relevant snippets.
    real = knowledge.public_ask_context('Zombie Outbreak')
    assert real
    assert 'zombie' in real.lower() or 'outbreak' in real.lower()


def test_knowledge_auto_refreshes_stale_index(tmp_path: Path):
    repo = Path('/home/klim/projects/chaos_redux')
    if not repo.exists():
        return
    vault = Path('/mnt/c/Users/klimp/Documents/Chaos Redux Vault')
    db = tmp_path / 'chaosx-stale-test.db'
    rebuild_index(repo, db, vault if vault.exists() else None)
    conn = sqlite3.connect(db)
    try:
        with conn:
            conn.execute("UPDATE index_meta SET value = '0' WHERE key = 'indexed_at'")
        before = float(dict(conn.execute("SELECT key, value FROM index_meta"))["indexed_at"])
    finally:
        conn.close()
    assert before == 0
    Knowledge(repo, db, vault if vault.exists() else None).status()
    # The rebuild runs on a background freshness worker; commands never block
    # on it, so wait for the refresh to land before asserting the new state.
    deadline = time.monotonic() + 180
    after = before
    while time.monotonic() < deadline:
        conn = sqlite3.connect(db)
        try:
            after = float(dict(conn.execute("SELECT key, value FROM index_meta"))["indexed_at"])
        finally:
            conn.close()
        if after > before:
            break
        time.sleep(0.5)
    assert after > before


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
    deadline = time.monotonic() + 60
    while time.monotonic() < deadline:
        conn = sqlite3.connect(db)
        try:
            meta = dict(conn.execute("SELECT key, value FROM index_meta"))
        finally:
            conn.close()
        if meta.get("catalog_fingerprint") == catalog_fingerprint(live_catalog):
            break
        time.sleep(0.2)
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
