from __future__ import annotations

import asyncio
import base64
import io
import json
import os
import re
import shlex
import subprocess
import tempfile
from contextlib import asynccontextmanager, contextmanager
from dataclasses import dataclass
from datetime import timedelta
from functools import lru_cache
from pathlib import Path
from typing import Any, AsyncIterator, Iterable, Iterator, Sequence

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from PIL import Image
from pydantic import AnyUrl

from .config import Settings
from .visual_cache import VisualArtifactCache, mcp_launcher_fingerprint

FOCUS_ROOT = Path("common/national_focus")
COUNTRY_TAGS_ROOT = Path("common/country_tags")
FOCUS_RASTER_TOOL = "hoi4.focus_raster"
COUNTRY_ASSET_TOOL = "chaosx.focus_country_assets"
COUNTRY_ASSET_DISPLAY_WIDTH = {"flag": 656, "leader": 624}
EVENT_PREFIX_RE = re.compile(r"^(\d{3})(?:_|$)")
ASSIGNMENT_VALUE_RE = r'(?:"([^"\n]+)"|([A-Za-z0-9_.:\-]+))'
TREE_ID_RE = re.compile(rf"(?m)^\s*id\s*=\s*{ASSIGNMENT_VALUE_RE}")
FOCUS_RE = re.compile(r"(?m)^\s*focus\s*=\s*\{")
SHARED_FOCUS_RE = re.compile(r"(?m)^\s*shared_focus\s*=\s*[A-Za-z0-9_.:\-]+")
COUNTRY_TAG_RE = re.compile(r"\b(?:tag|original_tag)\s*=\s*([A-Z][A-Z0-9_]{1,7})\b")
COUNTRY_TAG_DEF_RE = re.compile(r'^\s*([A-Z][A-Z0-9_]{1,7})\s*=\s*"countries/([^"\n]+)\.txt"', re.MULTILINE)
PACKAGE_COUNTRY_TAG_RE = re.compile(
    r"(?m)(?:\boriginal_tag|\btag)\s*=\s*([A-Z][A-Z0-9]{2})\b|^\s*([A-Z][A-Z0-9]{2})\s*=\s*\{"
)


class FocusTreeError(RuntimeError):
    """Internal focus-tree failure whose details must not be posted publicly."""


@lru_cache(maxsize=16)
def _validate_mcp_node_runtime(command: str) -> str:
    """Fail clearly when a local HOI4 Agent Tools server uses an obsolete Node runtime."""

    expanded = str(Path(command).expanduser()) if "/" in command else command
    if Path(expanded).name.casefold() not in {"node", "node.exe"}:
        return expanded
    try:
        completed = subprocess.run(
            [expanded, "--version"],
            capture_output=True,
            text=True,
            check=False,
            timeout=10,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise FocusTreeError("The configured Node.js runtime could not be started") from exc
    match = re.fullmatch(r"v?(\d+)(?:\.\d+){0,2}", completed.stdout.strip())
    if completed.returncode != 0 or match is None:
        raise FocusTreeError("The configured Node.js runtime version could not be determined")
    if int(match.group(1)) < 22:
        raise FocusTreeError(
            "HOI4 Agent Tools 2.x requires Node.js 22 or newer; configure an absolute compatible Node path"
        )
    return expanded


@contextmanager
def isolated_mcp_server_parameters(settings: Settings) -> Iterator[StdioServerParameters]:
    """Run read-only render sessions with disposable MCP artifacts and server state."""

    args = shlex.split(settings.focus_mcp_args)
    command = _validate_mcp_node_runtime(settings.focus_mcp_command)
    workspace_cwd = (settings.focus_tree_repo or settings.chaos_redux_repo).expanduser()
    cwd = str(workspace_cwd) if workspace_cwd.is_dir() else None
    configured_path = settings.focus_mcp_config_path.expanduser()
    server_env = {**os.environ, "HOI4_AGENT_TOOLS_CHAOSX": "1"}
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
        yield StdioServerParameters(command=command, args=args, cwd=cwd, env=server_env)
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
        yield StdioServerParameters(command=command, args=args, cwd=cwd, env=server_env)


class SharedMcpSession:
    """One lazily initialized MCP process shared by all ChaosX visual renderers."""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self._start_lock = asyncio.Lock()
        self._usage_lock = asyncio.Lock()
        self._worker_task: asyncio.Task[None] | None = None
        self._ready = asyncio.Event()
        self._shutdown = asyncio.Event()
        self._session: ClientSession | None = None
        self._start_error: BaseException | None = None

    async def start(self) -> None:
        if self._session is not None:
            return
        async with self._start_lock:
            if self._session is not None:
                return
            if self._worker_task is None or self._worker_task.done():
                self._ready = asyncio.Event()
                self._shutdown = asyncio.Event()
                self._start_error = None
                self._worker_task = asyncio.create_task(
                    self._run_worker(), name="chaosx-shared-mcp-session"
                )
            await self._ready.wait()
            if self._start_error is not None:
                raise FocusTreeError("HOI4 Agent Tools MCP session failed to start") from self._start_error
            if self._session is None:
                raise FocusTreeError("HOI4 Agent Tools MCP session is unavailable")

    async def _run_worker(self) -> None:
        try:
            with isolated_mcp_server_parameters(self.settings) as params:
                with open(os.devnull, "w", encoding="utf-8") as errlog:
                    async with stdio_client(params, errlog=errlog) as (read, write):
                        timeout = self.settings.focus_mcp_timeout_seconds
                        async with ClientSession(
                            read,
                            write,
                            read_timeout_seconds=timedelta(seconds=timeout),
                        ) as session:
                            await session.initialize()
                            self._session = session
                            self._ready.set()
                            await self._shutdown.wait()
        except BaseException as exc:
            if not self._ready.is_set():
                self._start_error = exc
                self._ready.set()
            elif not isinstance(exc, asyncio.CancelledError):
                self._start_error = exc
        finally:
            self._session = None
            if not self._ready.is_set():
                self._ready.set()

    @asynccontextmanager
    async def session(self) -> AsyncIterator[ClientSession]:
        await self.start()
        async with self._usage_lock:
            if self._session is None:
                raise FocusTreeError("HOI4 Agent Tools MCP session is unavailable")
            yield self._session

    async def close(self) -> None:
        async with self._start_lock:
            async with self._usage_lock:
                task = self._worker_task
                self._shutdown.set()
                if task is not None:
                    try:
                        await task
                    except asyncio.CancelledError:
                        pass
                self._worker_task = None
                self._session = None
                self._start_error = None


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
    package_country_tags: tuple[str, ...] = ()

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
    def asset_country_tags(self) -> tuple[str, ...]:
        if self.country_tags:
            return self.country_tags[:4]
        if self.package_country_tags:
            return self.package_country_tags[:4]
        prefix = re.match(r"^([A-Z0-9]{3})(?:_|$)", self.tree_id)
        return (prefix.group(1),) if prefix else ()

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
class FocusCountryAsset:
    tag: str
    kind: str
    filename: str
    png: bytes


@dataclass(frozen=True)
class FocusTreeGraph:
    record: FocusTreeRecord
    png: bytes
    country_assets: tuple[FocusCountryAsset, ...] = ()


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
        package_tags: dict[int, tuple[str, ...]] = {}
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
            if event_id is not None and event_id not in package_tags:
                package_tags[event_id] = self._package_country_tags(
                    event_id, set(country_names)
                )
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
                        package_country_tags=(
                            package_tags.get(event_id, ()) if event_id is not None else ()
                        ),
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

    def _package_country_tags(
        self, event_id: int, known_tags: set[str]
    ) -> tuple[str, ...]:
        prefix = f"{event_id:03d}_"
        tags: set[str] = set()
        for relative_root in ("events", "common"):
            root = self.repo / relative_root
            if not root.is_dir():
                continue
            for path in sorted(root.rglob(f"{prefix}*.txt")):
                try:
                    raw = _without_comments(
                        path.read_text(encoding="utf-8-sig", errors="replace")
                    )
                except OSError:
                    continue
                for assignment_tag, scope_tag in PACKAGE_COUNTRY_TAG_RE.findall(raw):
                    tag = assignment_tag or scope_tag
                    if tag in known_tags:
                        tags.add(tag)
        return tuple(sorted(tags))


class FocusTreeMcpClient:
    def __init__(
        self, settings: Settings, session_pool: SharedMcpSession | None = None
    ) -> None:
        self.settings = settings
        self.session_pool = session_pool
        self._cache: dict[tuple[Any, ...], bytes] = {}
        self._disk_cache = VisualArtifactCache(
            "focus-trees", max_item_bytes=settings.focus_tree_max_attachment_bytes
        )
        self._launcher_fingerprint = mcp_launcher_fingerprint(
            settings.focus_mcp_command, settings.focus_mcp_args
        )

    async def render(self, records: Sequence[FocusTreeRecord]) -> FocusTreeRenderBatch:
        selected = list(records[: self.settings.focus_tree_max_graphs])
        if not selected:
            return FocusTreeRenderBatch((), 0, 0)
        graphs: list[FocusTreeGraph] = []
        missing: list[FocusTreeRecord] = []
        cached_pngs: dict[FocusTreeRecord, bytes] = {}
        for record in selected:
            key = self._cache_key(record)
            cached = self._cache.get(key)
            if cached is None:
                cached = self._disk_cache.get(key)
                if cached is not None:
                    self._cache[key] = cached
            if cached is None:
                missing.append(record)
                continue
            if record.asset_country_tags:
                cached_pngs[record] = cached
                missing.append(record)
            else:
                graphs.append(FocusTreeGraph(record, cached))
        failed = 0
        if missing:
            try:
                rendered, failed = await self._render_uncached(missing, cached_pngs)
            except Exception as exc:
                raise FocusTreeError("HOI4 Agent Tools MCP rendering failed") from exc
            for graph in rendered:
                key = self._cache_key(graph.record)
                self._cache[key] = graph.png
                self._disk_cache.put(key, graph.png)
                graphs.append(graph)
        graph_order = {record: index for index, record in enumerate(selected)}
        graphs.sort(key=lambda graph: graph_order[graph.record])
        return FocusTreeRenderBatch(tuple(graphs), len(selected), failed)

    async def _render_uncached(
        self,
        records: Sequence[FocusTreeRecord],
        cached_pngs: dict[FocusTreeRecord, bytes] | None = None,
    ) -> tuple[list[FocusTreeGraph], int]:
        timeout = self.settings.focus_mcp_timeout_seconds
        if self.session_pool is not None:
            async with asyncio.timeout(timeout):
                async with self.session_pool.session() as session:
                    workspace_id = await self._workspace_id(session)
                    return await self._render_records(
                        session, workspace_id, records, cached_pngs or {}
                    )
        async with asyncio.timeout(timeout):
            with isolated_mcp_server_parameters(self.settings) as params:
                with open(os.devnull, "w", encoding="utf-8") as errlog:
                    async with stdio_client(params, errlog=errlog) as (read, write):
                        async with ClientSession(
                            read,
                            write,
                            read_timeout_seconds=timedelta(seconds=timeout),
                        ) as session:
                            await session.initialize()
                            workspace_id = await self._workspace_id(session)
                            return await self._render_records(
                                session, workspace_id, records, cached_pngs or {}
                            )

    async def _render_records(
        self,
        session: ClientSession,
        workspace_id: str,
        records: Sequence[FocusTreeRecord],
        cached_pngs: dict[FocusTreeRecord, bytes] | None = None,
    ) -> tuple[list[FocusTreeGraph], int]:
        rendered: list[FocusTreeGraph] = []
        failed = 0
        cached_pngs = cached_pngs or {}
        for record in records:
            try:
                png = cached_pngs.get(record)
                if png is None:
                    png = await self._render_one(session, workspace_id, record)
            except Exception:
                failed += 1
                continue
            try:
                country_assets = await self._render_country_assets(
                    session, workspace_id, record
                )
            except Exception:
                country_assets = ()
            rendered.append(FocusTreeGraph(record, png, country_assets))
        return rendered, failed

    async def _workspace_id(self, session: ClientSession) -> str:
        configured = self.settings.focus_mcp_workspace_id.strip()
        if configured:
            return configured
        tools = await session.list_tools()
        if not any(tool.name == "hoi4.mods" for tool in tools.tools):
            return "current"
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
            FOCUS_RASTER_TOOL,
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
            raise FocusTreeError("MCP focus raster returned no PNG artifact")
        declared_size = int(png_artifact.get("size") or 0)
        if declared_size > self.settings.focus_tree_max_attachment_bytes:
            raise FocusTreeError("MCP focus graph is too large for Discord")
        return await read_resource_bytes(
            session,
            str(png_artifact["uri"]),
            max_bytes=self.settings.focus_tree_max_attachment_bytes,
            expected_mime="image/png",
        )

    async def _render_country_assets(
        self,
        session: ClientSession,
        workspace_id: str,
        record: FocusTreeRecord,
    ) -> tuple[FocusCountryAsset, ...]:
        tags = record.asset_country_tags
        if not tags:
            return ()
        result = await session.call_tool(
            COUNTRY_ASSET_TOOL,
            {
                "workspaceId": workspace_id,
                "countryTags": list(tags),
                **({"eventId": record.event_id} if record.event_id is not None else {}),
                "treeId": record.tree_id,
            },
        )
        structured = _structured_content(result)
        artifacts = {
            str(artifact.get("name") or ""): artifact
            for artifact in structured.get("artifacts") or []
            if isinstance(artifact, dict) and artifact.get("mimeType") == "image/png"
        }
        countries = (structured.get("data") or {}).get("countries") or []
        rendered: list[FocusCountryAsset] = []
        for country in countries:
            if not isinstance(country, dict):
                continue
            tag = str(country.get("tag") or "")
            if tag not in tags:
                continue
            for kind, field in (
                ("flag", "flagArtifactName"),
                ("leader", "leaderPortraitArtifactName"),
            ):
                artifact_name = str(country.get(field) or "")
                artifact = artifacts.get(artifact_name)
                if not artifact or not artifact.get("uri"):
                    continue
                declared_size = int(artifact.get("size") or 0)
                if declared_size > self.settings.focus_tree_max_attachment_bytes:
                    continue
                png = await read_resource_bytes(
                    session,
                    str(artifact["uri"]),
                    max_bytes=self.settings.focus_tree_max_attachment_bytes,
                    expected_mime="image/png",
                )
                png = _scale_country_asset_for_discord(png, kind)
                if len(png) > self.settings.focus_tree_max_attachment_bytes:
                    continue
                rendered.append(
                    FocusCountryAsset(
                        tag=tag,
                        kind=kind,
                        filename=f"{tag.lower()}-{kind}.png",
                        png=png,
                    )
                )
        return tuple(rendered)

    def _cache_key(self, record: FocusTreeRecord) -> tuple[Any, ...]:
        return (
            record.relative_path,
            record.tree_id,
            record.source_mtime_ns,
            record.source_size,
            self.settings.focus_tree_review_scale,
            FOCUS_RASTER_TOOL,
            self._launcher_fingerprint,
        )


def _scale_country_asset_for_discord(png: bytes, kind: str) -> bytes:
    """Upscale source-native country art so Discord does not present it as a thumbnail."""

    target_width = COUNTRY_ASSET_DISPLAY_WIDTH.get(kind)
    if target_width is None:
        return png
    try:
        with Image.open(io.BytesIO(png)) as source:
            image = source.convert("RGBA")
    except Exception:
        return png
    if image.width >= target_width:
        return png
    target_height = max(1, round(image.height * target_width / image.width))
    scaled = image.resize((target_width, target_height), Image.Resampling.LANCZOS)
    output = io.BytesIO()
    scaled.save(output, format="PNG", optimize=True)
    return output.getvalue()


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
        range_meta = next(
            (value for key, value in meta.items() if key.endswith("artifact-byte-range")),
            {},
        )
        if not range_meta:
            return bytes(chunks + chunk)
        returned = range_meta.get("returnedRange") or {}
        offset = int(returned.get("offset") or 0)
        returned_length = int(returned.get("length") or len(chunk))
        end_exclusive = int(returned.get("endExclusive") or offset + returned_length)
        if offset != len(chunks):
            raise FocusTreeError("Out-of-order MCP artifact chunk")
        if returned_length != len(chunk) or end_exclusive != offset + returned_length:
            raise FocusTreeError("Malformed MCP artifact byte range")
        total_size = int(range_meta.get("totalSize") or 0)
        if total_size and total_size > max_bytes:
            raise FocusTreeError("MCP artifact exceeds the Discord upload limit")
        chunks.extend(chunk)
        if len(chunks) > max_bytes:
            raise FocusTreeError("MCP artifact exceeds the Discord upload limit")
        continuation = range_meta.get("continuationUri")
        complete = bool(range_meta.get("complete"))
        if complete:
            if continuation or offset != 0 or (total_size and len(chunks) != total_size):
                raise FocusTreeError("Malformed MCP artifact completion metadata")
            return bytes(chunks)
        if continuation:
            if total_size and end_exclusive >= total_size:
                raise FocusTreeError("Malformed MCP artifact continuation metadata")
            next_uri = str(continuation)
            continue
        if total_size and end_exclusive == total_size and len(chunks) == total_size:
            return bytes(chunks)
        raise FocusTreeError("Incomplete MCP artifact resource")
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
