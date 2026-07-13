from pathlib import Path

from chaosx_bot.indexer import rebuild_index
from chaosx_bot.knowledge import Knowledge


def test_rebuild_index_and_event_lookup(tmp_path: Path):
    repo = Path('/home/klim/projects/chaos_redux')
    if not repo.exists():
        return
    db = tmp_path / 'chaosx-test.db'
    stats = rebuild_index(repo, db)
    assert stats.docs > 100
    assert stats.events >= 180
    assert stats.clusters >= 14
    knowledge = Knowledge(repo, db)
    event = knowledge.event('2')
    assert 'Zombie Outbreak' in event
    assert 'Evidence:' not in event
    assert 'docs/spreadsheets' not in event
    search = knowledge.search('Zombie Outbreak')
    assert 'Evidence:' not in search
    assert 'docs/specs/' not in search
    owner_event = knowledge.event('2', show_evidence=True)
    assert 'Private source detail' in owner_event
    owner_search = knowledge.search('Zombie Outbreak', limit=2, show_evidence=True)
    assert 'Evidence:' in owner_search
    search = knowledge.search('Zombie Outbreak', limit=2)
    assert 'Search results' in search
    status = knowledge.status()
    assert 'Indexed commit' not in status
    assert 'source docs' not in status
