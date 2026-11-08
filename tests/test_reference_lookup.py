from __future__ import annotations

from contextlib import asynccontextmanager
from typing import Any, cast

import pytest

from chaosx_bot.config import Settings
from chaosx_bot.reference_lookup import (
    ReferenceHit,
    ReferenceLookupError,
    ReferenceMcpClient,
    clean_excerpt,
    clean_query,
    format_reference_answer,
)


class _Result:
    def __init__(self, structured: dict[str, Any], *, is_error: bool = False) -> None:
        self.structured = structured
        self.is_error = is_error

    def model_dump(self, **_: object) -> dict[str, Any]:
        return {"isError": self.is_error, "structuredContent": self.structured}


class _Session:
    def __init__(self, results: list[_Result]) -> None:
        self.results = results
        self.calls: list[tuple[str, dict[str, Any]]] = []

    async def call_tool(self, name: str, arguments: dict[str, Any]) -> _Result:
        self.calls.append((name, arguments))
        return self.results.pop(0)


def _client(session: _Session, monkeypatch: pytest.MonkeyPatch) -> ReferenceMcpClient:
    client = ReferenceMcpClient(Settings(discord_token="dummy"))

    @asynccontextmanager
    async def fake_session(_settings: object, _pool: object, **_kw: object):
        yield cast(Any, session), "chaos_redux"

    monkeypatch.setattr("chaosx_bot.reference_lookup.mcp_session", fake_session)
    return client


def _hit(**overrides: Any) -> ReferenceHit:
    values: dict[str, Any] = {
        "id": "abc",
        "revision": "rev1",
        "source": "wiki",
        "heading": "Scripted GUI",
        "title": "decision modding",
        "path": "decision_modding.md",
        "start_line": 286,
        "match_line": 288,
        "excerpt": "A decision category can have a scripted GUI.",
    }
    values.update(overrides)
    return ReferenceHit(**values)


def _search_payload(*results: dict[str, Any]) -> _Result:
    return _Result({"status": "ok", "data": {"results": list(results)}})


def _raw(source: str = "wiki", **overrides: Any) -> dict[str, Any]:
    raw: dict[str, Any] = {
        "id": "abc",
        "revision": "rev1",
        "source": source,
        "heading": "Scripted GUI",
        "title": "decision modding",
        "path": "decision_modding.md",
        "startLine": 286,
        "matchLine": 288,
        "excerpt": "### Scripted GUI <a id=\"Scripted_GUI\"></a> A decision category.",
    }
    raw.update(overrides)
    return raw


@pytest.mark.asyncio
async def test_search_asks_for_public_sources_only_and_parses_hits(monkeypatch: pytest.MonkeyPatch) -> None:
    session = _Session([_search_payload(_raw("wiki"), _raw("script_doc", id="leak"), _raw("game_doc", id="g1"))])
    client = _client(session, monkeypatch)

    hits = await client.search("  scripted   gui  ", limit=4)

    name, arguments = session.calls[0]
    assert name == "hoi4.reference_search"
    assert arguments == {
        "workspaceId": "chaos_redux",
        "query": "scripted gui",
        "sources": ["game_doc", "wiki"],
        "limit": 4,
    }
    # script_doc can name the host's mod paths, so a hit from it is never shown.
    assert [hit.id for hit in hits] == ["abc", "g1"]
    assert hits[0].match_line == 288
    assert hits[1].source_label == "game docs"


@pytest.mark.asyncio
async def test_search_is_cached_per_query(monkeypatch: pytest.MonkeyPatch) -> None:
    session = _Session([_search_payload(_raw())])
    client = _client(session, monkeypatch)

    first = await client.search("Scripted GUI")
    second = await client.search("scripted gui")

    assert first == second
    assert len(session.calls) == 1


@pytest.mark.asyncio
async def test_search_failure_is_a_lookup_error(monkeypatch: pytest.MonkeyPatch) -> None:
    session = _Session([_Result({"status": "error"}, is_error=True)])
    client = _client(session, monkeypatch)

    with pytest.raises(ReferenceLookupError):
        await client.search("anything")


@pytest.mark.asyncio
async def test_empty_query_makes_no_call(monkeypatch: pytest.MonkeyPatch) -> None:
    session = _Session([])
    client = _client(session, monkeypatch)

    assert await client.search("   ") == ()
    assert session.calls == []


@pytest.mark.asyncio
async def test_read_starts_at_the_matching_line(monkeypatch: pytest.MonkeyPatch) -> None:
    session = _Session([_Result({"status": "ok", "data": {"text": "line one\nline two"}})])
    client = _client(session, monkeypatch)

    text = await client.read(_hit(), max_lines=12)

    assert text == "line one\nline two"
    name, arguments = session.calls[0]
    assert name == "hoi4.reference_read"
    assert arguments == {
        "workspaceId": "chaos_redux",
        "id": "abc",
        "revision": "rev1",
        "source": "wiki",
        "startLine": 288,
        "maxLines": 12,
    }


def test_query_and_excerpt_cleaning() -> None:
    assert clean_query("  a\n\tb  " + "x" * 300) == ("a b " + "x" * 300)[:120]
    assert clean_excerpt('### Heading <a id="x"></a> body   text', 100) == "### Heading body text"
    assert clean_excerpt("word " * 100, 20).endswith("…")
    assert len(clean_excerpt("word " * 100, 20)) <= 20


def test_answer_without_hits_says_so() -> None:
    text = format_reference_answer("zzz`", [])
    assert text.startswith("No HOI4 documentation matched `zzz'`")


def test_answer_quotes_the_section_and_lists_other_hits_within_discord_limits() -> None:
    hits = [_hit(), _hit(id="b", heading="scripted_gui", source="game_doc", excerpt="| `scripted_gui` | 19 | |")]
    text = format_reference_answer("scripted gui", hits, "```\ninjected\n```\n" + "body " * 600)

    assert text.startswith("## HOI4 documentation — `scripted gui`")
    assert "**Scripted GUI** · Paradox wiki: decision modding" in text
    assert "**Also relevant**" in text and "game docs" in text
    assert len(text) <= 1900
    # A section cannot break out of the code block that quotes it.
    assert text.count("```") == 2


def test_answer_falls_back_to_the_search_excerpt() -> None:
    text = format_reference_answer("scripted gui", [_hit(excerpt='<a id="x"></a>A decision category.')])
    assert "A decision category." in text and "<a id" not in text


@pytest.mark.asyncio
async def test_reference_lookup_does_not_queue_behind_a_render() -> None:
    from chaosx_bot.focus_trees import SharedMcpSession

    pool = SharedMcpSession(Settings(discord_token="dummy"))
    marker = object()
    pool._session = cast(Any, marker)  # a started session, as after the first render

    async with pool._usage_lock:  # a render is holding the session
        async with pool.shared_session() as session:
            assert session is marker
