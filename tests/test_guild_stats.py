"""Server size must come from Discord, never from the bot's own users table."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import httpx
import pytest

from chaosx_bot.guild_stats import (
    GuildCounts,
    GuildCountsCache,
    auth_header,
    fetch_guild_counts,
    parse_guild_counts,
)
from chaosx_bot.routine_posts import server_facts_line


def test_parse_uses_discord_counts():
    counts = parse_guild_counts(
        {"approximate_member_count": 69, "approximate_presence_count": 12}, guild_id=1395
    )
    assert counts.members == 69 and counts.online == 12
    assert counts.source == "discord" and counts.available
    assert counts.as_signals() == {"members": 69, "online": 12, "members_source": "discord"}


@pytest.mark.parametrize(
    "payload",
    [None, {}, {"approximate_member_count": 0}, {"approximate_member_count": None}, {"error": "x"}],
)
def test_unusable_payloads_are_reported_as_unavailable(payload):
    """0 is not a member count: Discord omits the fields when it cannot answer."""
    counts = parse_guild_counts(payload, guild_id=1395)
    assert counts.members is None and counts.source == "unavailable"
    assert not counts.available


def test_auth_header_uses_bot_scheme():
    """Bearer <token> is answered with 401 for bot tokens; Discord wants Bot <token>."""
    assert auth_header("abc") == "Bot abc"
    assert auth_header("Bot abc") == "Bot abc"


@pytest.mark.asyncio
async def test_fetch_guild_counts_reads_the_api_and_survives_errors():
    calls: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(200, json={"approximate_member_count": 69, "approximate_presence_count": 12})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        counts = await fetch_guild_counts(1395, token="tok", client=client)
    assert counts.members == 69
    assert "with_counts=true" in str(calls[0].url)
    assert calls[0].headers["authorization"] == "Bot tok"


@pytest.mark.asyncio
async def test_fetch_guild_counts_never_raises_on_bad_response():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(403, json={"message": "Missing Access"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        counts = await fetch_guild_counts(1395, token="tok", client=client)
    assert counts.members is None and not counts.available


@pytest.mark.asyncio
async def test_cache_reuses_a_fresh_value_and_refreshes_when_stale(monkeypatch):
    hits: list[int] = []

    async def fake_fetch(guild_id, *, token, client=None):
        hits.append(1)
        return parse_guild_counts({"approximate_member_count": 69, "approximate_presence_count": 12}, guild_id=guild_id)

    monkeypatch.setattr("chaosx_bot.guild_stats.fetch_guild_counts", fake_fetch)
    cache = GuildCountsCache(ttl_seconds=600)
    moment = datetime.now(timezone.utc)

    first = await cache.get(1395, token="tok", now=moment)
    assert first.members == 69 and len(hits) == 1
    # inside the TTL window the cached value is reused without another call
    cached = await cache.get(1395, token="tok", now=moment + timedelta(seconds=30))
    assert cached.members == 69 and len(hits) == 1
    # past the TTL it refreshes
    refreshed = await cache.get(1395, token="tok", now=moment + timedelta(hours=5))
    assert refreshed.members == 69 and len(hits) == 2


def test_server_facts_line_labels_members_as_discord_and_omits_unknown():
    line = server_facts_line(
        {"qa_saved": 8, "members": 69, "online": 12, "members_source": "discord"}
    )
    assert "8 questions asked in the server" in line
    assert "69 members in the server (12 online right now)" in line

    unknown = server_facts_line({"qa_saved": 8, "members": 36, "members_source": "unavailable"})
    assert "36" not in unknown
    assert "members" not in unknown
