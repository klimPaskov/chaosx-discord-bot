from pathlib import Path
import sqlite3

from chaosx_bot.indexer import is_vault_indexable, rebuild_index
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
    assert event_lines[2] == '- Evolution stages: `3`'
    assert event_lines[3] == '- Has world-end scenario: `Yes`'
    assert event_lines[4].startswith('- Status:')
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
    search = knowledge.search('Zombie Outbreak')
    assert 'Evidence:' not in search
    assert 'docs/specs/' not in search
    owner_event = knowledge.event('2', show_evidence=True)
    assert 'Private source detail' in owner_event
    scenario = knowledge.scenario('5')
    assert 'SCN-005: The World in Fury' in scenario
    assert 'Soviet Union Collapse' not in scenario
    assert 'Event 2:' not in scenario
    assert 'docs/spreadsheets' not in scenario
    owner_scenario = knowledge.scenario('5', show_evidence=True)
    assert 'triggerable_scenarios' in owner_scenario
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
    conn = sqlite3.connect(db)
    try:
        after = float(dict(conn.execute("SELECT key, value FROM index_meta"))["indexed_at"])
    finally:
        conn.close()
    assert after > before


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
