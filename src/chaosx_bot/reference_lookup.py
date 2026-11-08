from __future__ import annotations

import re
from collections import OrderedDict
from dataclasses import dataclass
from typing import Any, Sequence

from .config import Settings
from .focus_trees import SharedMcpSession, _structured_content, mcp_session
from .visual_cache import mcp_launcher_fingerprint

# Public documentation only: the installed game's own docs and the offline Paradox wiki snapshot.
# script_doc is a dump of the host's own generated docs, which can name mod paths.
PUBLIC_REFERENCE_SOURCES = ("game_doc", "wiki")
SOURCE_LABELS = {"game_doc": "game docs", "wiki": "Paradox wiki"}
MAX_QUERY_CHARS = 120
CACHE_ENTRIES = 128
_WHITESPACE_RE = re.compile(r"\s+")
_MARKUP_RE = re.compile(r"<a id=\"[^\"]*\"></a>|<[^>]{1,80}>")


class ReferenceLookupError(RuntimeError):
    """The documentation lookup could not run; details must not be posted publicly."""


@dataclass(frozen=True)
class ReferenceHit:
    id: str
    revision: str
    source: str
    heading: str
    title: str
    path: str
    start_line: int
    match_line: int
    excerpt: str

    @property
    def source_label(self) -> str:
        return SOURCE_LABELS.get(self.source, self.source)


def clean_query(query: str) -> str:
    return _WHITESPACE_RE.sub(" ", query or "").strip()[:MAX_QUERY_CHARS]


def clean_excerpt(text: str, limit: int) -> str:
    """Flatten whitespace and drop HTML anchors so a hit reads as one short paragraph."""

    flat = _WHITESPACE_RE.sub(" ", _MARKUP_RE.sub("", text or "")).strip()
    if len(flat) <= limit:
        return flat
    return flat[: max(1, limit - 1)].rstrip() + "…"


class ReferenceMcpClient:
    """Documentation search through HOI4 Agent Tools instead of scanning files in the bot."""

    def __init__(self, settings: Settings, session_pool: SharedMcpSession | None = None) -> None:
        self.settings = settings
        self.session_pool = session_pool
        self._search_cache: OrderedDict[tuple[Any, ...], tuple[ReferenceHit, ...]] = OrderedDict()
        self._read_cache: OrderedDict[tuple[Any, ...], str] = OrderedDict()
        self._launcher_fingerprint = mcp_launcher_fingerprint(
            settings.focus_mcp_command, settings.focus_mcp_args
        )

    async def search(self, query: str, *, limit: int = 4) -> tuple[ReferenceHit, ...]:
        query = clean_query(query)
        if not query:
            return ()
        limit = max(1, min(limit, 12))
        key = (query.casefold(), limit, self._launcher_fingerprint)
        cached = self._search_cache.get(key)
        if cached is not None:
            self._search_cache.move_to_end(key)
            return cached
        try:
            async with mcp_session(self.settings, self.session_pool, exclusive=False) as (session, workspace_id):
                payload = _structured_content(
                    await session.call_tool(
                        "hoi4.reference_search",
                        {
                            "workspaceId": workspace_id,
                            "query": query,
                            "sources": list(PUBLIC_REFERENCE_SOURCES),
                            "limit": limit,
                        },
                    )
                )
        except ReferenceLookupError:
            raise
        except Exception as exc:  # an anyio ExceptionGroup from the MCP must not escape as a crash
            raise ReferenceLookupError("Documentation search is unavailable") from exc
        hits = tuple(_parse_hits((payload.get("data") or {}).get("results")))
        self._remember(self._search_cache, key, hits)
        return hits

    async def read(self, hit: ReferenceHit, *, max_lines: int = 24) -> str:
        """The section a search hit points at, starting at the line that matched."""

        key = (hit.id, hit.revision, hit.match_line, max_lines, self._launcher_fingerprint)
        cached = self._read_cache.get(key)
        if cached is not None:
            self._read_cache.move_to_end(key)
            return cached
        try:
            async with mcp_session(self.settings, self.session_pool, exclusive=False) as (session, workspace_id):
                payload = _structured_content(
                    await session.call_tool(
                        "hoi4.reference_read",
                        {
                            "workspaceId": workspace_id,
                            "id": hit.id,
                            "revision": hit.revision,
                            "source": hit.source,
                            "startLine": max(1, hit.match_line or hit.start_line or 1),
                            "maxLines": max(1, min(max_lines, 80)),
                        },
                    )
                )
        except ReferenceLookupError:
            raise
        except Exception as exc:
            raise ReferenceLookupError("Documentation read is unavailable") from exc
        text = _section_text(payload.get("data") or {})
        self._remember(self._read_cache, key, text)
        return text

    @staticmethod
    def _remember(cache: OrderedDict[Any, Any], key: Any, value: Any) -> None:
        cache[key] = value
        cache.move_to_end(key)
        while len(cache) > CACHE_ENTRIES:
            cache.popitem(last=False)


def _parse_hits(results: Any) -> list[ReferenceHit]:
    hits: list[ReferenceHit] = []
    for item in results if isinstance(results, list) else []:
        if not isinstance(item, dict):
            continue
        identifier = str(item.get("id") or "")
        revision = str(item.get("revision") or "")
        source = str(item.get("source") or "")
        if not identifier or not revision or source not in PUBLIC_REFERENCE_SOURCES:
            continue
        start = _as_int(item.get("startLine"))
        hits.append(
            ReferenceHit(
                id=identifier,
                revision=revision,
                source=source,
                heading=clean_excerpt(str(item.get("heading") or ""), 90),
                title=clean_excerpt(str(item.get("title") or ""), 90),
                path=str(item.get("path") or ""),
                start_line=start,
                match_line=_as_int(item.get("matchLine")) or start,
                excerpt=str(item.get("excerpt") or ""),
            )
        )
    return hits


def _section_text(data: dict[str, Any]) -> str:
    for field in ("text", "content", "excerpt", "markdown"):
        value = data.get(field)
        if isinstance(value, str) and value.strip():
            return value
    lines = data.get("lines")
    if isinstance(lines, list):
        parts: list[str] = []
        for line in lines:
            if isinstance(line, str):
                parts.append(line)
            elif isinstance(line, dict) and isinstance(line.get("text"), str):
                parts.append(line["text"])
        return "\n".join(parts)
    return ""


def _as_int(value: Any) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def format_reference_answer(
    query: str, hits: Sequence[ReferenceHit], top_section: str = "", *, limit: int = 1900
) -> str:
    """A Discord message: the best section first, then the other hits as one-line pointers."""

    safe_query = clean_excerpt(query.replace("`", "'"), MAX_QUERY_CHARS)
    if not hits:
        return f"No HOI4 documentation matched `{safe_query}`. Try the script token or a plainer phrase."
    first = hits[0]
    lines = [f"## HOI4 documentation — `{safe_query}`"]
    heading = first.heading or first.title or "Result"
    lines.append(f"**{heading}** · {first.source_label}: {first.title}")
    body = top_section.strip() or first.excerpt
    quoted = clean_excerpt(body, 900) if not top_section.strip() else _quote_block(body, 900)
    lines.append(quoted)
    others = hits[1:]
    if others:
        lines.append("**Also relevant**")
        for hit in others:
            lines.append(
                f"- **{hit.heading or hit.title}** · {hit.source_label}: {hit.title} — "
                f"{clean_excerpt(hit.excerpt, 140)}"
            )
    lines.append("-# Offline copy of the game docs and the Paradox wiki, searched by the mod tools.")
    text = "\n".join(lines)
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def _quote_block(text: str, limit: int) -> str:
    cleaned = _MARKUP_RE.sub("", text).strip().replace("```", "'''")
    if len(cleaned) > limit:
        cleaned = cleaned[: limit - 1].rstrip() + "…"
    return f"```\n{cleaned}\n```"
