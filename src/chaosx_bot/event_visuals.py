from __future__ import annotations

import asyncio
import io
import json
import os
import re
import subprocess
from xml.etree import ElementTree
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path
from typing import Any, AsyncIterator, Iterable, Sequence

import cairosvg
from mcp import ClientSession
from mcp.client.stdio import stdio_client
from PIL import Image, ImageChops

from .config import Settings
from .focus_trees import (
    SharedMcpSession,
    _structured_content,
    isolated_mcp_server_parameters,
    read_resource_bytes,
)
from .visual_cache import VisualArtifactCache, mcp_launcher_fingerprint

EVENTS_ROOT = Path("events")
SCRIPTED_GUI_ROOT = Path("common/scripted_guis")
EVENT_SOURCE_RE = re.compile(r"^(\d{3})(?:[_-]|$)")
EVENT_ID_RE = re.compile(r"(?m)^\s*id\s*=\s*(?:\"([^\"]+)\"|([A-Za-z0-9_.:-]+))")
ASSIGNMENT_RE = re.compile(r"([A-Za-z0-9_.:-]+)\s*=\s*\{")
WINDOW_NAME_RE = re.compile(r"\bwindow_name\s*=\s*(?:\"([^\"]+)\"|([A-Za-z0-9_.:-]+))")
CONTEXT_TYPE_RE = re.compile(r"\bcontext_type\s*=\s*(?:\"([^\"]+)\"|([A-Za-z0-9_.:-]+))")


class EventVisualError(RuntimeError):
    """A public event-chain or scripted-GUI render could not be produced."""


class EmptyGuiPreviewError(EventVisualError):
    """Raised when the offline renderer produced no meaningful GUI content."""


def _is_low_value_auto_gui(record: ScriptedGuiRecord) -> bool:
    identity = f"{record.gui_id} {record.window_name}".casefold()
    return any(token in identity for token in ("mapicon", "map_icon", "map icon"))


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
    def __init__(
        self, settings: Settings, session_pool: SharedMcpSession | None = None
    ) -> None:
        self.settings = settings
        self.session_pool = session_pool
        self._event_cache: dict[tuple[Any, ...], bytes] = {}
        self._gui_cache: dict[tuple[Any, ...], bytes] = {}
        self._event_disk_cache = VisualArtifactCache(
            "event-chains", max_item_bytes=settings.focus_tree_max_attachment_bytes
        )
        self._gui_disk_cache = VisualArtifactCache(
            "scripted-guis", max_item_bytes=settings.focus_tree_max_attachment_bytes
        )
        self._launcher_fingerprint = mcp_launcher_fingerprint(
            settings.focus_mcp_command, settings.focus_mcp_args
        )

    def _cached_event(self, key: tuple[Any, ...]) -> bytes | None:
        cached = self._event_cache.get(key)
        if cached is None:
            cached = self._event_disk_cache.get(key)
            if cached is not None:
                self._event_cache[key] = cached
        return cached

    def _store_event(self, key: tuple[Any, ...], png: bytes) -> None:
        self._event_cache[key] = png
        self._event_disk_cache.put(key, png)

    def _cached_gui(self, key: tuple[Any, ...]) -> bytes | None:
        cached = self._gui_cache.get(key)
        if cached is None:
            cached = self._gui_disk_cache.get(key)
            if cached is not None:
                self._gui_cache[key] = cached
        return cached

    def _store_gui(self, key: tuple[Any, ...], png: bytes) -> None:
        self._gui_cache[key] = png
        self._gui_disk_cache.put(key, png)

    async def render_event_chain(self, record: EventChainRecord) -> EventChainGraph:
        key = self._event_key(record)
        cached = self._cached_event(key)
        if cached is not None:
            return EventChainGraph(record, cached)
        try:
            async with self._session() as (session, workspace_id):
                png = await self._render_event_chain(session, workspace_id, record)
        except EventVisualError:
            raise
        except Exception as exc:
            raise EventVisualError("Event-chain graph is unavailable") from exc
        self._store_event(key, png)
        return EventChainGraph(record, png)

    async def render_scripted_guis(self, records: Sequence[ScriptedGuiRecord]) -> tuple[tuple[ScriptedGuiPreview, ...], int]:
        selected = list(records[: self.settings.scripted_gui_max_previews])
        if not selected:
            return (), 0
        cached = [self._cached_gui(self._gui_key(record)) for record in selected]
        if all(png is not None for png in cached):
            return (
                tuple(
                    ScriptedGuiPreview(record, png)
                    for record, png in zip(selected, cached, strict=True)
                    if png is not None
                ),
                0,
            )
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
        useful_guis = [record for record in guis if not _is_low_value_auto_gui(record)]
        candidates = useful_guis or list(guis)
        selected_guis = list(candidates[: self.settings.scripted_gui_max_previews])
        if chain is None and not selected_guis:
            return RelatedEventVisuals(None, ())

        graph: EventChainGraph | None = None
        needs_chain = False
        if chain is not None:
            event_key = self._event_key(chain)
            cached = self._cached_event(event_key)
            if cached is None:
                needs_chain = True
            else:
                graph = EventChainGraph(chain, cached)
        guis_cached = all(
            self._cached_gui(self._gui_key(record)) is not None for record in selected_guis
        )
        if not needs_chain and guis_cached:
            previews = tuple(
                ScriptedGuiPreview(record, self._cached_gui(self._gui_key(record)) or b"")
                for record in selected_guis
            )
            return RelatedEventVisuals(graph, previews)

        try:
            async with self._session() as (session, workspace_id):
                async def render_chain() -> tuple[EventChainGraph | None, bool]:
                    if chain is None or not needs_chain:
                        return graph, False
                    try:
                        png = await self._render_event_chain(session, workspace_id, chain)
                    except Exception:
                        return None, True
                    self._store_event(self._event_key(chain), png)
                    return EventChainGraph(chain, png), False

                (rendered_graph, chain_failed), (previews, failed) = await asyncio.gather(
                    render_chain(),
                    self._render_gui_records(session, workspace_id, selected_guis),
                )
                return RelatedEventVisuals(rendered_graph, previews, chain_failed, failed)
        except EventVisualError:
            raise
        except Exception as exc:
            raise EventVisualError("Related event visuals are unavailable") from exc

    @asynccontextmanager
    async def _session(self) -> AsyncIterator[tuple[ClientSession, str]]:
        timeout = self.settings.focus_mcp_timeout_seconds
        total_timeout = min(timeout * 2, 900)
        async with asyncio.timeout(total_timeout):
            if self.session_pool is not None:
                async with self.session_pool.session() as session:
                    yield session, await self._workspace_id(session)
                return
            with isolated_mcp_server_parameters(self.settings) as params:
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
        tools = await session.list_tools()
        if not any(tool.name == "hoi4.mods" for tool in tools.tools):
            return "current"
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
        render_nodes = min(240, max(self.settings.event_chain_max_nodes, len(record.event_keys) * 3))
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
                    "maxNodes": render_nodes,
                    "expandHelpers": False,
                    "includeHtml": False,
                    "refresh": False,
                },
            )
        )
        artifact = next(
            (
                item
                for item in payload.get("artifacts") or []
                if isinstance(item, dict)
                and item.get("mimeType") == "application/json"
                and item.get("uri")
                and not str(item.get("name") or "").endswith("-manifest.json")
            ),
            None,
        )
        if artifact is None:
            raise EventVisualError("MCP event render did not return authoritative graph data")
        raw = await read_resource_bytes(
            session,
            str(artifact["uri"]),
            max_bytes=self.settings.focus_tree_max_attachment_bytes,
            expected_mime="application/json",
        )
        try:
            graph = json.loads(raw)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise EventVisualError("MCP event graph data is invalid") from exc
        if not isinstance(graph, dict):
            raise EventVisualError("MCP event graph data is invalid")
        return await asyncio.to_thread(
            _render_compact_event_chain,
            graph,
            record,
            self.settings.event_chain_graphviz_command,
            self.settings.event_chain_graphviz_dpi,
            self.settings.focus_tree_max_attachment_bytes,
        )

    async def _render_gui_records(
        self,
        session: ClientSession,
        workspace_id: str,
        records: Sequence[ScriptedGuiRecord],
    ) -> tuple[tuple[ScriptedGuiPreview, ...], int]:
        async def render_one(
            record: ScriptedGuiRecord,
        ) -> tuple[ScriptedGuiPreview | None, bool]:
            key = self._gui_key(record)
            png = self._cached_gui(key)
            if png is None:
                try:
                    png = await self._render_gui(session, workspace_id, record)
                except EmptyGuiPreviewError:
                    return None, False
                except Exception:
                    return None, True
                self._store_gui(key, png)
            return ScriptedGuiPreview(record, png), False

        results = await asyncio.gather(*(render_one(record) for record in records))
        previews = tuple(preview for preview, _failed in results if preview is not None)
        failed = sum(1 for _preview, did_fail in results if did_fail)
        return previews, failed

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
                        "state": "active",
                        "scriptedGui": {record.gui_id: True},
                        "visibility": {record.window_name: True, record.gui_id: True},
                    },
                    "states": ["active"],
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
        cairosvg.svg2png(
            bytestring=_remove_offline_banner(svg),
            write_to=output,
            output_width=width,
            output_height=height,
        )
        png = _crop_and_scale_gui_preview(output.getvalue(), width, height)
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
            self.settings.event_chain_graphviz_dpi,
            self._launcher_fingerprint,
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
            self._launcher_fingerprint,
        )


def _render_compact_event_chain(
    graph: dict[str, Any],
    record: EventChainRecord,
    graphviz_command: str,
    graphviz_dpi: int,
    max_bytes: int,
) -> bytes:
    raw_nodes = graph.get("nodes") or []
    raw_edges = graph.get("edges") or []
    nodes = {
        str(node.get("id")): node
        for node in raw_nodes
        if isinstance(node, dict) and isinstance(node.get("id"), str)
    }
    wanted = {f"event:{event_key}" for event_key in record.event_keys}
    event_nodes = {node_id: nodes[node_id] for node_id in wanted if node_id in nodes}
    if not event_nodes:
        raise EventVisualError("MCP event graph contains no package events")

    adjacency: dict[str, set[str]] = {}
    for edge in raw_edges:
        if not isinstance(edge, dict):
            continue
        source = str(edge.get("from") or "")
        target = str(edge.get("to") or "")
        if source in nodes and target in nodes:
            adjacency.setdefault(source, set()).add(target)

    projected: set[tuple[str, str]] = set()
    for source in event_nodes:
        pending = [(target, 0) for target in adjacency.get(source, ())]
        visited: set[str] = set()
        while pending:
            target, depth = pending.pop()
            if target in visited or depth > 5:
                continue
            visited.add(target)
            if target in event_nodes:
                if target != source:
                    projected.add((source, target))
                continue
            if str(nodes.get(target, {}).get("kind") or "") == "event":
                continue
            pending.extend((next_target, depth + 1) for next_target in adjacency.get(target, ()))

    title = f"{record.label} — event flow"
    subtitle = f"{len(event_nodes)} events • {len(projected)} internal links • options/helpers collapsed"
    degree = max((sum(1 for source, _ in projected if source == node_id) for node_id in event_nodes), default=0)
    use_force_layout = len(event_nodes) > 20 or degree > 8
    layout_attributes = (
        'layout=sfdp, overlap=prism, splines=true, sep="+18", K=1.1, repulsiveforce=1.8, '
        if use_force_layout
        else 'rankdir=LR, splines=ortho, concentrate=true, nodesep="0.32", ranksep="0.62", '
    )
    dot: list[str] = [
        "digraph event_chain {",
        f'graph [bgcolor="#111923", {layout_attributes}pad="0.28", outputorder=edgesfirst, '
        'fontname="DejaVu Sans", fontcolor="#f4f6f8", fontsize=22, labelloc=t, '
        f'label={json.dumps(title + chr(10) + subtitle, ensure_ascii=False)}];',
        'node [shape=box, style="rounded,filled", fillcolor="#1d2a38", color="#d9a928", '
        'fontcolor="#f4f6f8", penwidth=1.6, fontname="DejaVu Sans", fontsize=13, margin="0.14,0.09"];',
        'edge [color="#7890a6", penwidth=1.4, arrowsize=0.72];',
    ]
    primary = f"event:{record.primary_event_key}"
    for node_id, node in sorted(event_nodes.items(), key=lambda item: _event_sort_key(item[0])):
        event_key = str(node.get("eventId") or node_id.removeprefix("event:"))
        label = event_key.removeprefix("chaosx.")
        metadata_value = node.get("metadata")
        metadata: dict[str, Any] = metadata_value if isinstance(metadata_value, dict) else {}
        attributes = [f"label={json.dumps(label, ensure_ascii=False)}"]
        if node_id == primary:
            attributes.extend(('fillcolor="#6b481d"', 'penwidth=2.4'))
        elif metadata.get("hidden") is True:
            attributes.extend(('fillcolor="#29313b"', 'style="rounded,filled,dashed"'))
        dot.append(f"{json.dumps(node_id)} [{', '.join(attributes)}];")
    for source, target in sorted(projected):
        dot.append(f"{json.dumps(source)} -> {json.dumps(target)};")
    dot.append("}")
    try:
        command = [graphviz_command]
        if use_force_layout:
            command.append("-Ksfdp")
        command.extend(("-Tpng", f"-Gdpi={graphviz_dpi}", "-Gsize=18,12"))
        completed = subprocess.run(
            command,
            input="\n".join(dot).encode("utf-8"),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
            timeout=30,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise EventVisualError("Compact event-chain rendering failed") from exc
    png = completed.stdout
    if completed.returncode != 0 or not png.startswith(b"\x89PNG\r\n\x1a\n"):
        raise EventVisualError("Compact event-chain rendering failed")
    if len(png) > max_bytes:
        raise EventVisualError("Compact event-chain image exceeds the Discord upload limit")
    return png


def _event_sort_key(node_id: str) -> tuple[Any, ...]:
    parts = re.split(r"(\d+)", node_id.casefold())
    return tuple(int(part) if part.isdigit() else part for part in parts)


def _crop_and_scale_gui_preview(png: bytes, max_width: int, max_height: int) -> bytes:
    try:
        image = Image.open(io.BytesIO(png)).convert("RGBA")
    except Exception as exc:
        raise EventVisualError("MCP scripted-GUI preview PNG is invalid") from exc
    background = Image.new("RGBA", image.size, image.getpixel((image.width - 1, image.height - 1)))
    difference = ImageChops.difference(image, background).convert("L")
    mask = difference.point([0 if value <= 8 else 255 for value in range(256)])
    content_top = min(32, image.height)
    content_histogram = mask.crop((0, content_top, image.width, image.height)).histogram()
    visible_pixels = content_histogram[255] if len(content_histogram) > 255 else 0
    if visible_pixels < 500:
        raise EmptyGuiPreviewError("Scripted-GUI preview contains no useful visible interface")
    bounds = mask.getbbox()
    if bounds is None:
        raise EmptyGuiPreviewError("Scripted-GUI preview is blank")
    left, top, right, bottom = bounds
    padding = 14
    crop_box = (
        max(0, left - padding),
        max(0, top - padding),
        min(image.width, right + padding),
        min(image.height, bottom + padding),
    )
    cropped = image.crop(crop_box)
    scale = min(max_width / cropped.width, max_height / cropped.height, 4.0)
    if scale != 1.0:
        target = (max(1, round(cropped.width * scale)), max(1, round(cropped.height * scale)))
        cropped = cropped.resize(target, Image.Resampling.LANCZOS)
    output = io.BytesIO()
    cropped.save(output, format="PNG", optimize=True)
    return output.getvalue()


def _remove_offline_banner(svg: bytes) -> bytes:
    """Remove HOI4 Agent Tools' in-image disclaimer; Discord captions retain the preview label."""

    try:
        root = ElementTree.fromstring(svg)
    except ElementTree.ParseError as exc:
        raise EventVisualError("MCP scripted-GUI SVG is invalid") from exc
    for child in reversed(root):
        if child.tag.rsplit("}", 1)[-1] != "g":
            continue
        rectangle = next(
            (
                item
                for item in child
                if item.tag.rsplit("}", 1)[-1] == "rect"
                and item.attrib.get("width") == "215"
                and item.attrib.get("height") == "20"
                and item.attrib.get("fill") == "#05080c"
            ),
            None,
        )
        if rectangle is not None:
            root.remove(child)
            break
    return ElementTree.tostring(root, encoding="utf-8")


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
