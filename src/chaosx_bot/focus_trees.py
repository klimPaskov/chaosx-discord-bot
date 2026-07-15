from __future__ import annotations

import asyncio
import base64
import json
import os
import re
import shlex
import tempfile
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path
from typing import Any, Iterable, Iterator, Sequence

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from pydantic import AnyUrl

from .config import Settings

FOCUS_ROOT = Path("common/national_focus")
COUNTRY_TAGS_ROOT = Path("common/country_tags")
EVENT_PREFIX_RE = re.compile(r"^(\d{3})(?:_|$)")
ASSIGNMENT_VALUE_RE = r'(?:"([^"\n]+)"|([A-Za-z0-9_.:\-]+))'
TREE_ID_RE = re.compile(rf"(?m)^\s*id\s*=\s*{ASSIGNMENT_VALUE_RE}")
FOCUS_RE = re.compile(r"(?m)^\s*focus\s*=\s*\{")
SHARED_FOCUS_RE = re.compile(r"(?m)^\s*shared_focus\s*=\s*[A-Za-z0-9_.:\-]+")
COUNTRY_TAG_RE = re.compile(r"\b(?:tag|original_tag)\s*=\s*([A-Z][A-Z0-9_]{1,7})\b")
COUNTRY_TAG_DEF_RE = re.compile(r'^\s*([A-Z][A-Z0-9_]{1,7})\s*=\s*"countries/([^"\n]+)\.txt"', re.MULTILINE)


class FocusTreeError(RuntimeError):
    """Internal focus-tree failure whose details must not be posted publicly."""


@contextmanager
def isolated_mcp_server_parameters(settings: Settings) -> Iterator[StdioServerParameters]:
    """Run read-only render sessions with disposable MCP artifacts and server state."""

    args = shlex.split(settings.focus_mcp_args)
    configured_path = settings.focus_mcp_config_path.expanduser()
    config_argument_found = False
    for index, argument in enumerate(args):
        if argument == "--config":
            if index + 1 >= len(args):
                raise FocusTreeError("HOI4 Agent Tools MCP --config argument has no value")
            configured_path = Path(args[index + 1]).expanduser()
            config_argument_found = True
            break
        if argument.startswith("--config="):
            configured_path = Path(argument.split("=", 1)[1]).expanduser()
            config_argument_found = True
            break
    if not configured_path.is_file():
        if not config_argument_found:
            args.extend(("--config", str(configured_path)))
        yield StdioServerParameters(command=settings.focus_mcp_command, args=args)
        return
    try:
        config = json.loads(configured_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise FocusTreeError("HOI4 Agent Tools MCP config could not be isolated") from exc
    if not isinstance(config, dict):
        raise FocusTreeError("HOI4 Agent Tools MCP config is invalid")
    with tempfile.TemporaryDirectory(prefix="chaosx-hoi4-mcp-") as temporary:
        root = Path(temporary)
        isolated = {
            **config,
            "serverStateRoot": str(root / "state"),
            "workspaceStorageRoot": str(root / "workspaces"),
        }
        isolated_path = root / "config.json"
        isolated_path.write_text(json.dumps(isolated), encoding="utf-8")
        replaced = False
        for index, argument in enumerate(args):
            if argument == "--config":
                if index + 1 >= len(args):
                    raise FocusTreeError("HOI4 Agent Tools MCP --config argument has no value")
                args[index + 1] = str(isolated_path)
                replaced = True
                break
            if argument.startswith("--config="):
                args[index] = f"--config={isolated_path}"
                replaced = True
                break
        if not replaced:
            args.extend(("--config", str(isolated_path)))
        yield StdioServerParameters(command=settings.focus_mcp_command, args=args)


@dataclass(frozen=True)
class FocusTreeRecord:
    tree_id: str
    relative_path: str
    event_id: int | None
    country_tags: tuple[str, ...]
    country_names: tuple[str, ...]
    selector_hints: tuple[str, ...]
    focus_count: int
    source_mtime_ns: int
    source_size: int

    @property
    def label(self) -> str:
        tree_name = _humanize(self.tree_id.removesuffix("_focus_tree").removesuffix("_focus"))
        if self.country_names:
            return f"{', '.join(self.country_names[:3])} — {tree_name}"
        if self.country_tags:
            return f"{', '.join(self.country_tags[:3])} — {tree_name}"
        return tree_name

    @property
    def filename(self) -> str:
        safe = re.sub(r"[^A-Za-z0-9_.-]+", "-", self.tree_id).strip("-.") or "focus-tree"
        return f"{safe[:90]}.png"

    @property
    def searchable_text(self) -> str:
        return " ".join(
            (
                self.tree_id,
                Path(self.relative_path).stem,
                *self.country_tags,
                *self.country_names,
                *self.selector_hints,
            )
        ).casefold()


@dataclass(frozen=True)
class FocusTreeGraph:
    record: FocusTreeRecord
    png: bytes


@dataclass(frozen=True)
class FocusTreeRenderBatch:
    graphs: tuple[FocusTreeGraph, ...]
    attempted: int
    failed: int


class FocusTreeCatalog:
    def __init__(self, repo: Path) -> None:
        self.repo = repo

    def discover(self, *, include_empty: bool = False) -> list[FocusTreeRecord]:
        root = self.repo / FOCUS_ROOT
        if not root.is_dir():
            return []
        country_names = self._country_names()
        records: list[FocusTreeRecord] = []
        for path in sorted(root.rglob("*.txt")):
            try:
                raw = path.read_text(encoding="utf-8-sig", errors="replace")
                stat = path.stat()
            except OSError:
                continue
            relative_path = path.relative_to(self.repo).as_posix()
            event_match = EVENT_PREFIX_RE.match(path.stem)
            event_id = int(event_match.group(1)) if event_match else None
            for block in _named_blocks(_without_comments(raw), "focus_tree"):
                tree_id = _first_assignment_value(TREE_ID_RE, block)
                if not tree_id:
                    continue
                focus_count = len(FOCUS_RE.findall(block)) + len(SHARED_FOCUS_RE.findall(block))
                if not include_empty and focus_count == 0:
                    continue
                country_block = next(iter(_named_blocks(block, "country")), "")
                tags = tuple(sorted(set(COUNTRY_TAG_RE.findall(country_block))))
                names = tuple(country_names[tag] for tag in tags if tag in country_names)
                hints = tuple(sorted(_selector_hints(country_block)))
                records.append(
                    FocusTreeRecord(
                        tree_id=tree_id,
                        relative_path=relative_path,
                        event_id=event_id,
                        country_tags=tags,
                        country_names=names,
                        selector_hints=hints,
                        focus_count=focus_count,
                        source_mtime_ns=stat.st_mtime_ns,
                        source_size=stat.st_size,
                    )
                )
        return sorted(records, key=lambda item: (item.event_id is None, item.event_id or 0, item.relative_path, item.tree_id))

    def for_event(self, event_id: int | str) -> list[FocusTreeRecord]:
        try:
            number = int(str(event_id).strip())
        except ValueError:
            return []
        return [record for record in self.discover() if record.event_id == number]

    def search(self, query: str) -> list[FocusTreeRecord]:
        records = self.discover()
        normalized = " ".join(query.casefold().strip().split())
        event_match = re.fullmatch(r"(?:event\s*)?0*(\d{1,3})", normalized)
        if event_match:
            event_id = int(event_match.group(1))
            return [record for record in records if record.event_id == event_id]
        if not normalized:
            return []
        exact = [
            record
            for record in records
            if normalized
            in {
                record.tree_id.casefold(),
                Path(record.relative_path).stem.casefold(),
                *(tag.casefold() for tag in record.country_tags),
                *(name.casefold() for name in record.country_names),
            }
        ]
        if exact:
            return exact
        tokens = normalized.split()
        return [record for record in records if all(token in record.searchable_text for token in tokens)]

    def _country_names(self) -> dict[str, str]:
        root = self.repo / COUNTRY_TAGS_ROOT
        names: dict[str, str] = {}
        if not root.is_dir():
            return names
        for path in sorted(root.rglob("*.txt")):
            try:
                raw = _without_comments(path.read_text(encoding="utf-8-sig", errors="replace"))
            except OSError:
                continue
            for tag, source_name in COUNTRY_TAG_DEF_RE.findall(raw):
                names.setdefault(tag, _humanize(source_name))
        return names


class FocusTreeMcpClient:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self._cache: dict[tuple[Any, ...], bytes] = {}

    async def render(self, records: Sequence[FocusTreeRecord]) -> FocusTreeRenderBatch:
        selected = list(records[: self.settings.focus_tree_max_graphs])
        if not selected:
            return FocusTreeRenderBatch((), 0, 0)
        graphs: list[FocusTreeGraph] = []
        missing: list[FocusTreeRecord] = []
        for record in selected:
            cached = self._cache.get(self._cache_key(record))
            if cached is None:
                missing.append(record)
            else:
                graphs.append(FocusTreeGraph(record, cached))
        failed = 0
        if missing:
            try:
                rendered, failed = await self._render_uncached(missing)
            except Exception as exc:
                raise FocusTreeError("HOI4 Agent Tools MCP rendering failed") from exc
            for graph in rendered:
                self._cache[self._cache_key(graph.record)] = graph.png
                graphs.append(graph)
        graph_order = {record: index for index, record in enumerate(selected)}
        graphs.sort(key=lambda graph: graph_order[graph.record])
        return FocusTreeRenderBatch(tuple(graphs), len(selected), failed)

    async def _render_uncached(self, records: Sequence[FocusTreeRecord]) -> tuple[list[FocusTreeGraph], int]:
        rendered: list[FocusTreeGraph] = []
        failed = 0
        timeout = self.settings.focus_mcp_timeout_seconds
        async with asyncio.timeout(timeout):
            with isolated_mcp_server_parameters(self.settings) as params:
                with open(os.devnull, "w", encoding="utf-8") as errlog:
                    async with stdio_client(params, errlog=errlog) as (read, write):
                        async with ClientSession(read, write, read_timeout_seconds=timedelta(seconds=timeout)) as session:
                            await session.initialize()
                            workspace_id = await self._workspace_id(session)
                            for record in records:
                                try:
                                    png = await self._render_one(session, workspace_id, record)
                                except Exception:
                                    failed += 1
                                    continue
                                rendered.append(FocusTreeGraph(record, png))
        return rendered, failed

    async def _workspace_id(self, session: ClientSession) -> str:
        configured = self.settings.focus_mcp_workspace_id.strip()
        if configured:
            return configured
        result = await session.call_tool("hoi4.mods", {})
        structured = _structured_content(result)
        mods = (structured.get("data") or {}).get("mods") or []
        wanted = self.settings.focus_mcp_workspace_name.casefold().strip()
        for mod in mods:
            if isinstance(mod, dict) and str(mod.get("name") or "").casefold() == wanted:
                workspace_id = str(mod.get("id") or "")
                if workspace_id:
                    return workspace_id
        raise FocusTreeError("Chaos Redux MCP workspace was not found")

    async def _render_one(self, session: ClientSession, workspace_id: str, record: FocusTreeRecord) -> bytes:
        result = await session.call_tool(
            "hoi4.focus_render",
            {
                "workspaceId": workspace_id,
                "relativePath": record.relative_path,
                "treeId": record.tree_id,
                "reviewScale": self.settings.focus_tree_review_scale,
            },
        )
        structured = _structured_content(result)
        artifacts = structured.get("artifacts") or []
        png_artifact = next(
            (artifact for artifact in artifacts if isinstance(artifact, dict) and artifact.get("mimeType") == "image/png"),
            None,
        )
        if not png_artifact or not png_artifact.get("uri"):
            raise FocusTreeError("MCP focus render returned no PNG artifact")
        declared_size = int(png_artifact.get("size") or 0)
        if declared_size > self.settings.focus_tree_max_attachment_bytes:
            raise FocusTreeError("MCP focus graph is too large for Discord")
        return await read_resource_bytes(
            session,
            str(png_artifact["uri"]),
            max_bytes=self.settings.focus_tree_max_attachment_bytes,
            expected_mime="image/png",
        )

    def _cache_key(self, record: FocusTreeRecord) -> tuple[Any, ...]:
        return (
            record.relative_path,
            record.tree_id,
            record.source_mtime_ns,
            record.source_size,
            self.settings.focus_tree_review_scale,
        )


async def read_resource_bytes(session: ClientSession, uri: str, *, max_bytes: int, expected_mime: str) -> bytes:
    chunks = bytearray()
    seen: set[str] = set()
    next_uri: str | None = uri
    for _ in range(64):
        if not next_uri or next_uri in seen or not next_uri.startswith("hoi4-agent://"):
            raise FocusTreeError("Invalid MCP artifact continuation URI")
        seen.add(next_uri)
        result = await session.read_resource(AnyUrl(next_uri))
        payload = result.model_dump(by_alias=True)
        contents = payload.get("contents") or []
        if len(contents) != 1 or not isinstance(contents[0], dict):
            raise FocusTreeError("Unexpected MCP artifact resource response")
        content = contents[0]
        if str(content.get("mimeType") or "") != expected_mime:
            raise FocusTreeError("Unexpected MCP artifact MIME type")
        blob = content.get("blob")
        text = content.get("text")
        if isinstance(blob, str):
            try:
                chunk = base64.b64decode(blob, validate=True)
            except (ValueError, TypeError) as exc:
                raise FocusTreeError("Invalid base64 MCP artifact data") from exc
        elif isinstance(text, str):
            chunk = text.encode("utf-8")
        else:
            raise FocusTreeError("MCP artifact resource did not contain data")
        meta = content.get("_meta") or {}
        range_meta = next((value for key, value in meta.items() if key.endswith("artifact-byte-range")), {})
        returned = range_meta.get("returnedRange") or {}
        if returned and int(returned.get("offset") or 0) != len(chunks):
            raise FocusTreeError("Out-of-order MCP artifact chunk")
        total_size = int(range_meta.get("totalSize") or 0)
        if total_size and total_size > max_bytes:
            raise FocusTreeError("MCP artifact exceeds the Discord upload limit")
        chunks.extend(chunk)
        if len(chunks) > max_bytes:
            raise FocusTreeError("MCP artifact exceeds the Discord upload limit")
        continuation = range_meta.get("continuationUri")
        if range_meta.get("complete", continuation is None):
            if continuation:
                raise FocusTreeError("Malformed MCP artifact completion metadata")
            return bytes(chunks)
        if not continuation:
            raise FocusTreeError("Incomplete MCP artifact resource")
        next_uri = str(continuation)
    raise FocusTreeError("Too many MCP artifact chunks")


def _structured_content(result: Any) -> dict[str, Any]:
    payload = result.model_dump(by_alias=True)
    structured = payload.get("structuredContent")
    if payload.get("isError") or not isinstance(structured, dict) or structured.get("status") != "ok":
        raise FocusTreeError("MCP tool call failed")
    return structured


def _first_assignment_value(pattern: re.Pattern[str], text: str) -> str:
    match = pattern.search(text)
    if not match:
        return ""
    return next((value for value in match.groups() if value), "")


def _named_blocks(text: str, name: str) -> Iterable[str]:
    pattern = re.compile(rf"\b{re.escape(name)}\s*=\s*\{{")
    position = 0
    while match := pattern.search(text, position):
        opening = text.find("{", match.start())
        closing = _matching_brace(text, opening)
        if closing < 0:
            return
        yield text[opening + 1 : closing]
        position = closing + 1


def _matching_brace(text: str, opening: int) -> int:
    depth = 0
    quoted = False
    escaped = False
    for index in range(opening, len(text)):
        char = text[index]
        if quoted:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                quoted = False
            continue
        if char == '"':
            quoted = True
        elif char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                return index
    return -1


def _without_comments(text: str) -> str:
    cleaned: list[str] = []
    for line in text.splitlines():
        quoted = False
        escaped = False
        end = len(line)
        for index, char in enumerate(line):
            if quoted:
                if escaped:
                    escaped = False
                elif char == "\\":
                    escaped = True
                elif char == '"':
                    quoted = False
            elif char == '"':
                quoted = True
            elif char == "#":
                end = index
                break
        cleaned.append(line[:end])
    return "\n".join(cleaned)


def _selector_hints(country_block: str) -> set[str]:
    reserved = {"add", "and", "factor", "has_country_flag", "modifier", "not", "or", "original_tag", "tag"}
    words = set(re.findall(r"\b[a-z][a-z0-9_]{2,}\b", country_block.casefold()))
    return {word for word in words if word not in reserved and not word.isdigit()}


def _humanize(value: str) -> str:
    return " ".join(part for part in re.sub(r"[_\-]+", " ", value).split()).title()
