import hashlib
import hmac
from pathlib import Path

import pytest

from chaosx_bot.storage import Store
from chaosx_bot.webhook_server import summarize_github_event, verify_github_signature


def test_github_signature_validation():
    secret = 'secret'
    body = b'{"ok":true}'
    sig = 'sha256=' + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    assert verify_github_signature(secret, body, sig)
    assert not verify_github_signature(secret, body, 'sha256=bad')
    assert not verify_github_signature('', body, sig)


def test_pr_summary_key_is_stable():
    action, card_key, summary, body = summarize_github_event('pull_request', {
        'action': 'ready_for_review',
        'repository': {'full_name': 'klimPaskov/Chaos-Redux'},
        'pull_request': {'number': 12, 'title': 'Add zombies', 'html_url': 'https://example/pr/12', 'draft': False, 'merged': False, 'state': 'open'},
    })
    assert action == 'ready_for_review'
    assert card_key == 'github:pr:klimPaskov/Chaos-Redux:12'
    assert 'Add zombies' in summary
    assert 'https://example/pr/12' in body


@pytest.mark.asyncio
async def test_store_dedupes_github_deliveries(tmp_path: Path):
    store = Store(tmp_path / 'test.db')
    await store.init()
    assert await store.record_github_delivery(delivery_id='d1', event='push', action=None, status='received', summary='one')
    assert not await store.record_github_delivery(delivery_id='d1', event='push', action=None, status='received', summary='dup')
