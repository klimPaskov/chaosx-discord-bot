from __future__ import annotations

import asyncio
import io
import os
import re
import shlex
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path
from typing import Any, AsyncIterator, Iterable, Sequence

import cairosvg
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from .config import Settings
from .focus_trees import _structured_content, read_resource_bytes

EVENTS_ROOT = Path("events")
SCRIPTED_GUI_ROOT = Path("common/scripted_guis")
EVENT_SOURCE_RE = re.compile(r"^(\d{3})(?:[_-]|$)")
EVENT_ID_RE = re.compile(r"(?m)^\s*id\s*=\s*(?:\"([^\"]+)\"|([A-Za-z0-9_.:-]+))")
ASSIGNMENT_RE = re.compile(r"([A-Za-z0-9_.:-]+)\s*=\s*\{")
WINDOW_NAME_RE = re.compile(r"\bwindow_name\s*=\s*(?:\"([^\"]+)\"|([A-Za-z0-9_.:-]+))")
CONTEXT_TYPE_RE = re.compile(r"\bcontext_type\s*=\s*(?:\"([^\"]+)\"|([A-Za-z0-9_.:-]+))")


class EventVisualError(RuntimeError):
    """A public event-chain or scripted-GUI render could not be produced."""


@dataclass(frozen=True)
class EventChainRecord:
    relative_path: str
    label: str
    event_id: int | None
    event_keys: tuple[str, ...]
    source_mtime_ns: int
    source_size: int

    @property
    def primary_event_key(self) -> str:
        preferred = next((key for key in self.event_keys if key.endswith(".1")), None)
        return preferred or self.event_keys[0]

    @property
    def filename(self) -> str:
        suffix = str(self.event_id) if self.event_id is not None else _safe_slug(self.label)
        return f"event_chain_{suffix}.png"

    @property
    def searchable_text(self) -> str:
        return " ".join((self.label, self.relative_path, *self.event_keys)).casefold()


@dataclass(frozen=True)
class ScriptedGuiRecord:
    relative_path: str
    label: str
    gui_id: str
    window_name: str
    context_type: str
    event_id: int | None
    source_mtime_ns: int
    source_size: int

    @property
    def filename(self) -> str:
        return f"scripted_gui_{_safe_slug(self.window_name)}.png"

    @property
    def searchable_text(self) -> str:
        return " ".join(
            (self.label, self.relative_path, self.gui_id, self.window_name, self.context_type)
        ).casefold()


@dataclass(frozen=True)
class EventChainGraph:
    record: EventChainRecord
    png: bytes


@dataclass(frozen=True)
class ScriptedGuiPreview:
    record: ScriptedGuiRecord
    png: bytes


@dataclass(frozen=True)
class RelatedEventVisuals:
    chain: EventChainGraph | None
    guis: tuple[ScriptedGuiPreview, ...]
    chain_failed: bool = False
    failed_guis: int = 0


class EventChainCatalog:
    def __init__(self, repo: Path) -> None:
        self.repo = repo

    def discover(self) -> list[EventChainRecord]:
        root = self.repo / EVENTS_ROOT
        records: list[EventChainRecord] = []
        if not root.is_dir():
            return records
        for source in sorted(root.rglob("*.txt")):
            try:
                text = source.read_text(encoding="utf-8-sig", errors="replace")
                stat = source.stat()
            except OSError:
                continue
            event_keys = _event_definition_ids(text)
            if not event_keys:
                continue
            relative = source.relative_to(self.repo).as_posix()
            event_id = _source_event_id(source.name)
            records.append(
                EventChainRecord(
                    relative_path=relative,
                    label=_source_label(source.stem),
                    event_id=event_id,
                    event_keys=event_keys,
                    source_mtime_ns=stat.st_mtime_ns,
                    source_size=stat.st_size,
                )
            )
        return records

    def for_event(self, event_id: int) -> EventChainRecord | None:
        return next((record for record in self.discover() if record.event_id == event_id), None)

    def find(self, query: str) -> EventChainRecord | None:
        records = self.discover()
        normalized = " ".join(query.casefold().strip().split())
        if not normalized:
            return None
        numeric = _query_event_id(normalized)
        if numeric is not None:
            match = next((record for record in records if record.event_id == numeric), None)
            if match:
                return match
        exact = next(
            (
                record
                for record in records
                if normalized == record.primary_event_key.casefold()
                or any(normalized == key.casefold() for key in record.event_keys)
            ),
            None,
        )
        if exact:
            return exact
        tokens = normalized.split()
        matches = [record for record in records if all(token in record.searchable_text for token in tokens)]
        return matches[0] if matches else None


class ScriptedGuiCatalog:
    def __init__(self, repo: Path) -> None:
        self.repo = repo

    def discover(self) -> list[ScriptedGuiRecord]:
        root = self.repo / SCRIPTED_GUI_ROOT
        records: list[ScriptedGuiRecord] = []
        if not root.is_dir():
            return records
        for source in sorted(root.rglob("*.txt")):
            try:
                text = source.read_text(encoding="utf-8-sig", errors="replace")
                stat = source.stat()
            except OSError:
                continue
            relative = source.relative_to(self.repo).as_posix()
            event_id = _source_event_id(source.name)
            label = _source_label(source.stem)
            for outer in _named_blocks(text, "scripted_gui"):
                for gui_id, body in _top_level_blocks(outer):
                    window_name = _assignment_value(WINDOW_NAME_RE, body)
                    if not window_name:
                        continue
                    records.append(
                        ScriptedGuiRecord(
                            relative_path=relative,
                            label=label,
                            gui_id=gui_id,
                            window_name=window_name,
                            context_type=_assignment_value(CONTEXT_TYPE_RE, body),
                            event_id=event_id,
                            source_mtime_ns=stat.st_mtime_ns,
                            source_size=stat.st_size,
                        )
                    )
        unique: dict[tuple[str, str], ScriptedGuiRecord] = {}
        for record in records:
            unique.setdefault((record.gui_id, record.window_name), record)
        return sorted(unique.values(), key=lambda record: (record.relative_path, record.gui_id))

    def for_event(self, event_id: int) -> list[ScriptedGuiRecord]:
        return [record for record in self.discover() if record.event_id == event_id]

    def search(self, query: str) -> list[ScriptedGuiRecord]:
        records = self.discover()
        normalized = " ".join(query.casefold().strip().split())
        if not normalized:
            return []
        numeric = _query_event_id(normalized)
        if numeric is not None:
            matches = [record for record in records if record.event_id == numeric]
            if matches:
                return matches
        exact = [
            record
            for record in records
            if normalized in {record.gui_id.casefold(), record.window_name.casefold()}
        ]
        if exact:
            return exact
        tokens = normalized.split()
        return [record for record in records if all(token in record.searchable_text for token in tokens)]


class EventVisualMcpClient:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self._event_cache: dict[tuple[Any, ...], bytes] = {}
        self._gui_cache: dict[tuple[Any, ...], bytes] = {}

    async def render_event_chain(self, record: EventChainRecord) -> EventChainGraph:
        key = self._event_key(record)
        cached = self._event_cache.get(key)
        if cached is not None:
            return EventChainGraph(record, cached)
        try:
            async with self._session() as (session, workspace_id):
                png = await self._render_event_chain(session, workspace_id, record)
        except EventVisualError:
            raise
        except Exception as exc:
            raise EventVisualError("Event-chain graph is unavailable") from exc
        self._event_cache[key] = png
        return EventChainGraph(record, png)

    async def render_scripted_guis(self, records: Sequence[ScriptedGuiRecord]) -> tuple[tuple[ScriptedGuiPreview, ...], int]:
        selected = list(records[: self.settings.scripted_gui_max_previews])
        if not selected:
            return (), 0
        try:
            async with self._session() as (session, workspace_id):
                return await self._render_gui_records(session, workspace_id, selected)
        except EventVisualError:
            raise
        except Exception as exc:
            raise EventVisualError("Scripted-GUI previews are unavailable") from exc

    async def render_related(
        self,
        chain: EventChainRecord | None,
        guis: Sequence[ScriptedGuiRecord],
    ) -> RelatedEventVisuals:
        selected_guis = list(guis[: self.settings.scripted_gui_max_previews])
        if chain is None and not selected_guis:
            return RelatedEventVisuals(None, ())
        try:
            async with self._session() as (session, workspace_id):
                graph: EventChainGraph | None = None
                chain_failed = False
                if chain is not None:
                    try:
                        key = self._event_key(chain)
                        png = self._event_cache.get(key)
                        if png is None:
                            png = await self._render_event_chain(session, workspace_id, chain)
                            self._event_cache[key] = png
                        graph = EventChainGraph(chain, png)
                    except Exception:
                        chain_failed = True
                previews, failed = await self._render_gui_records(session, workspace_id, selected_guis)
                return RelatedEventVisuals(graph, previews, chain_failed, failed)
        except EventVisualError:
            raise
        except Exception as exc:
            raise EventVisualError("Related event visuals are unavailable") from exc

    @asynccontextmanager
    async def _session(self) -> AsyncIterator[tuple[ClientSession, str]]:
        args = shlex.split(self.settings.focus_mcp_args)
        config_path = self.settings.focus_mcp_config_path.expanduser()
        if config_path and "--config" not in args:
            args.extend(("--config", str(config_path)))
        params = StdioServerParameters(command=self.settings.focus_mcp_command, args=args)
        timeout = self.settings.focus_mcp_timeout_seconds
        total_timeout = min(timeout * 2, 900)
        async with asyncio.timeout(total_timeout):
            with open(os.devnull, "w", encoding="utf-8") as errlog:
                async with stdio_client(params, errlog=errlog) as (read, write):
                    async with ClientSession(
                        read,
                        write,
                        read_timeout_seconds=timedelta(seconds=timeout),
                    ) as session:
                        await session.initialize()
                        yield session, await self._workspace_id(session)

    async def _workspace_id(self, session: ClientSession) -> str:
        configured = self.settings.focus_mcp_workspace_id.strip()
        if configured:
            return configured
        payload = _structured_content(await session.call_tool("hoi4.mods", {}))
        expected = self.settings.focus_mcp_workspace_name.casefold().strip()
        workspaces = (payload.get("data") or {}).get("mods") or payload.get("workspaces") or []
        for workspace in workspaces:
            if not isinstance(workspace, dict):
                continue
            workspace_id = str(workspace.get("id") or "")
            name = str(workspace.get("name") or "").casefold()
            if workspace_id and (name == expected or workspace_id.casefold() == expected):
                return workspace_id
        raise EventVisualError("Chaos Redux MCP workspace was not found")

    async def _render_event_chain(
        self,
        session: ClientSession,
        workspace_id: str,
        record: EventChainRecord,
    ) -> bytes:
        payload = _structured_content(
            await session.call_tool(
                "hoi4.event_render",
                {
                    "workspaceId": workspace_id,
                    "view": "neighborhood",
                    "selector": {
                        "kind": "manifest",
                        "manifest": {"eventIds": list(record.event_keys)},
                    },
                    "direction": "both",
                    "maxDepth": self.settings.event_chain_max_depth,
                    "maxNodes": self.settings.event_chain_max_nodes,
                    "expandHelpers": False,
                    "includeHtml": False,
                    "refresh": False,
                },
            )
        )
        artifact = _artifact(payload, "image/png")
        return await read_resource_bytes(
            session,
            str(artifact["uri"]),
            max_bytes=self.settings.focus_tree_max_attachment_bytes,
            expected_mime="image/png",
        )

    async def _render_gui_records(
        self,
        session: ClientSession,
        workspace_id: str,
        records: Sequence[ScriptedGuiRecord],
    ) -> tuple[tuple[ScriptedGuiPreview, ...], int]:
        previews: list[ScriptedGuiPreview] = []
        failed = 0
        for record in records:
            key = self._gui_key(record)
            png = self._gui_cache.get(key)
            if png is None:
                try:
                    png = await self._render_gui(session, workspace_id, record)
                    self._gui_cache[key] = png
                except Exception:
                    failed += 1
                    continue
            previews.append(ScriptedGuiPreview(record, png))
        return tuple(previews), failed

    async def _render_gui(
        self,
        session: ClientSession,
        workspace_id: str,
        record: ScriptedGuiRecord,
    ) -> bytes:
        width = self.settings.scripted_gui_preview_width
        height = self.settings.scripted_gui_preview_height
        payload = _structured_content(
            await session.call_tool(
                "hoi4.gui_render",
                {
                    "workspaceId": workspace_id,
                    "windowName": record.window_name,
                    "scenario": {
                        "id": "discord-preview",
                        "resolution": {"width": width, "height": height},
                        "state": "normal",
                    },
                    "states": ["normal"],
                    "resolutions": [{"width": width, "height": height, "uiScale": 1.0}],
                },
            )
        )
        artifact = _artifact(payload, "image/svg+xml")
        svg = await read_resource_bytes(
            session,
            str(artifact["uri"]),
            max_bytes=self.settings.focus_tree_max_attachment_bytes,
            expected_mime="image/svg+xml",
        )
        output = io.BytesIO()
        cairosvg.svg2png(bytestring=svg, write_to=output, output_width=width, output_height=height)
        png = output.getvalue()
        if not png.startswith(b"\x89PNG\r\n\x1a\n"):
            raise EventVisualError("MCP scripted-GUI preview did not convert to PNG")
        if len(png) > self.settings.focus_tree_max_attachment_bytes:
            raise EventVisualError("Scripted-GUI preview exceeds the Discord upload limit")
        return png

    def _event_key(self, record: EventChainRecord) -> tuple[Any, ...]:
        return (
            record.relative_path,
            record.primary_event_key,
            record.source_mtime_ns,
            record.source_size,
            self.settings.event_chain_max_depth,
            self.settings.event_chain_max_nodes,
        )

    def _gui_key(self, record: ScriptedGuiRecord) -> tuple[Any, ...]:
        return (
            record.relative_path,
            record.gui_id,
            record.window_name,
            record.source_mtime_ns,
            record.source_size,
            self.settings.scripted_gui_preview_width,
            self.settings.scripted_gui_preview_height,
        )


def _artifact(payload: dict[str, Any], mime_type: str) -> dict[str, Any]:
    for artifact in payload.get("artifacts") or []:
        if isinstance(artifact, dict) and artifact.get("mimeType") == mime_type and artifact.get("uri"):
            return artifact
    raise EventVisualError(f"MCP render did not return {mime_type}")


def _event_definition_ids(text: str) -> tuple[str, ...]:
    ids: list[str] = []
    event_block_names = {
        "country_event",
        "news_event",
        "state_event",
        "unit_leader_event",
        "operative_leader_event",
    }
    for block_name, body in _top_level_blocks(text):
        if block_name not in event_block_names:
            continue
        event_id = _assignment_value(EVENT_ID_RE, body)
        if event_id and event_id not in ids:
            ids.append(event_id)
    return tuple(ids)


def _source_event_id(filename: str) -> int | None:
    match = EVENT_SOURCE_RE.match(filename)
    return int(match.group(1)) if match else None


def _query_event_id(query: str) -> int | None:
    match = re.fullmatch(r"(?:event\s*)?0*(\d{1,3})", query)
    return int(match.group(1)) if match else None


def _source_label(stem: str) -> str:
    return re.sub(r"^\d{3}[_-]?", "", stem).replace("_scripted_guis", "").replace("_scripted_gui", "").replace("_", " ").strip().title()


def _safe_slug(value: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "_", value.casefold()).strip("_")
    return slug[:80] or "preview"


def _assignment_value(pattern: re.Pattern[str], text: str) -> str:
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


def _top_level_blocks(text: str) -> Iterable[tuple[str, str]]:
    position = 0
    while match := ASSIGNMENT_RE.search(text, position):
        prefix = text[position : match.start()]
        if _brace_delta(prefix) != 0:
            position = match.end()
            continue
        opening = text.find("{", match.start())
        closing = _matching_brace(text, opening)
        if closing < 0:
            return
        yield match.group(1), text[opening + 1 : closing]
        position = closing + 1


def _brace_delta(text: str) -> int:
    depth = 0
    quoted = False
    escaped = False
    for char in text:
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
    return depth


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
