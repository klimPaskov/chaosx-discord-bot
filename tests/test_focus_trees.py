from __future__ import annotations

import asyncio
import base64
import json
from contextlib import asynccontextmanager, contextmanager
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest

from chaosx_bot.bot import ChaosXBot, register_commands, send_focus_tree_graphs
from chaosx_bot.config import Settings
from chaosx_bot.focus_trees import (
    FocusTreeCatalog,
    FocusCountryAsset,
    FocusTreeError,
    FocusTreeGraph,
    FocusTreeMcpClient,
    FocusTreeRecord,
    FocusTreeRenderBatch,
    SharedMcpSession,
    _validate_mcp_node_runtime,
    isolated_mcp_server_parameters,
    read_resource_bytes,
)


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def test_catalog_discovers_event_country_and_tree_queries(tmp_path: Path) -> None:
    _write(
        tmp_path / "common/country_tags/test.txt",
        'ABC = "countries/Alpha Republic.txt"\n',
    )
    _write(
        tmp_path / "common/national_focus/007_alpha.txt",
        """
        focus_tree = {
          id = ABC_story_focus_tree
          country = { modifier = { add = 10 original_tag = ABC } }
          focus = { id = ABC_start x = 0 y = 0 }
          focus = { id = ABC_next x = 0 y = 1 prerequisite = { focus = ABC_start } }
        }
        """,
    )
    _write(
        tmp_path / "common/national_focus/002_blank.txt",
        "focus_tree = { id = EMPTY_focus country = { original_tag = ZZZ } }",
    )

    catalog = FocusTreeCatalog(tmp_path)
    records = catalog.discover()

    assert len(records) == 1
    record = records[0]
    assert record.event_id == 7
    assert record.tree_id == "ABC_story_focus_tree"
    assert record.country_tags == ("ABC",)
    assert record.country_names == ("Alpha Republic",)
    assert record.asset_country_tags == ("ABC",)
    assert record.focus_count == 2
    assert catalog.for_event("007") == [record]
    assert catalog.search("event 7") == [record]
    assert catalog.search("ABC") == [record]
    assert catalog.search("alpha republic") == [record]
    assert catalog.search("story focus") == [record]
    assert catalog.for_event(2) == []


def test_catalog_infers_mod_defined_country_for_scripted_selector(tmp_path: Path) -> None:
    repo = tmp_path / "mod"
    _write(
        repo / "common/national_focus/010_death.txt",
        """
        focus_tree = {
            id = death_focus_tree
            country = { factor = 0 modifier = { add = 10 is_death_country = yes } }
            focus = { id = DEATH_START }
        }
        """,
    )
    _write(
        repo / "common/country_tags/010_death.txt",
        'DTH = "countries/Death.txt"\n',
    )
    _write(
        repo / "common/scripted_effects/010_death_effects.txt",
        """
        death_create = {
            DTH = { set_country_flag = death_country }
            GER = { }
        }
        """,
    )

    record = FocusTreeCatalog(repo).for_event(10)[0]

    assert record.country_tags == ()
    assert record.country_names == ()
    assert record.package_country_tags == ("DTH",)
    assert record.asset_country_tags == ("DTH",)


def test_catalog_discovers_dynamic_country_selector(tmp_path: Path) -> None:
    _write(
        tmp_path / "common/national_focus/010_dynamic.txt",
        """
        focus_tree = {
          id = plague_focus_tree
          country = { factor = 0 modifier = { add = 10 is_plague_country = yes } }
          focus = { id = plague_start x = 0 y = 0 }
        }
        """,
    )
    record = FocusTreeCatalog(tmp_path).search("plague country")[0]
    assert record.selector_hints == ("is_plague_country", "yes")


def test_focus_tree_command_is_registered() -> None:
    bot = ChaosXBot(Settings(discord_token="dummy", command_guild_id=None, allowed_guild_id=None))
    register_commands(bot)
    assert {command.name for command in bot.tree.get_commands()} >= {"event", "focus-tree", "event-chain", "scripted-gui"}
    admin = next(command for command in bot.tree.get_commands() if command.name == "admin")
    assert "restart" in {command.name for command in getattr(admin, "commands", [])}


def test_mcp_render_config_uses_disposable_storage(tmp_path: Path) -> None:
    config_path = tmp_path / "config.json"
    config_path.write_text(
        '{"version":1,"modRoots":["/mods"],"serverStateRoot":"/old/state","workspaceStorageRoot":"/old/workspaces"}',
        encoding="utf-8",
    )
    settings = Settings(
        discord_token="dummy",
        chaos_redux_repo=tmp_path,
        focus_tree_repo=tmp_path,
        focus_mcp_args=f"server --config {config_path}",
        focus_mcp_config_path=config_path,
    )

    with isolated_mcp_server_parameters(settings) as parameters:
        isolated_path = Path(parameters.args[parameters.args.index("--config") + 1])
        isolated = json.loads(isolated_path.read_text(encoding="utf-8"))
        temporary_root = isolated_path.parent
        assert isolated["modRoots"] == ["/mods"]
        assert Path(isolated["serverStateRoot"]).is_relative_to(temporary_root)
        assert Path(isolated["workspaceStorageRoot"]).is_relative_to(temporary_root)
        assert parameters.cwd == str(tmp_path)
        assert parameters.env is not None
        assert parameters.env["HOI4_AGENT_TOOLS_CHAOSX"] == "1"

    assert not temporary_root.exists()


def test_local_mcp_rejects_obsolete_node_runtime(monkeypatch: pytest.MonkeyPatch) -> None:
    _validate_mcp_node_runtime.cache_clear()
    monkeypatch.setattr(
        "chaosx_bot.focus_trees.subprocess.run",
        lambda *_args, **_kwargs: SimpleNamespace(returncode=0, stdout="v18.19.1\n"),
    )

    with pytest.raises(FocusTreeError, match="Node.js 22 or newer"):
        _validate_mcp_node_runtime("node")

    _validate_mcp_node_runtime.cache_clear()


def test_local_mcp_accepts_compatible_absolute_node_runtime(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _validate_mcp_node_runtime.cache_clear()
    monkeypatch.setattr(
        "chaosx_bot.focus_trees.subprocess.run",
        lambda *_args, **_kwargs: SimpleNamespace(returncode=0, stdout="v22.22.3\n"),
    )

    command = "/home/test/.nvm/versions/node/v22.22.3/bin/node"
    assert _validate_mcp_node_runtime(command) == command

    _validate_mcp_node_runtime.cache_clear()


@pytest.mark.asyncio
async def test_shared_mcp_session_opens_and_closes_stdio_in_worker_task(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    lifecycle_tasks: list[asyncio.Task[object] | None] = []

    @contextmanager
    def fake_parameters(_settings: Settings):
        yield object()

    @asynccontextmanager
    async def fake_stdio(_params: object, **_kwargs: object):
        lifecycle_tasks.append(asyncio.current_task())
        try:
            yield object(), object()
        finally:
            lifecycle_tasks.append(asyncio.current_task())

    class FakeClientSession:
        def __init__(self, *_args: object, **_kwargs: object) -> None:
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args: object) -> None:
            return None

        async def initialize(self) -> None:
            return None

    monkeypatch.setattr(
        "chaosx_bot.focus_trees.isolated_mcp_server_parameters", fake_parameters
    )
    monkeypatch.setattr("chaosx_bot.focus_trees.stdio_client", fake_stdio)
    monkeypatch.setattr("chaosx_bot.focus_trees.ClientSession", FakeClientSession)
    pool = SharedMcpSession(Settings(discord_token="dummy"))

    await pool.start()
    async with pool.session() as session:
        assert isinstance(session, FakeClientSession)
    await pool.close()

    assert len(lifecycle_tasks) == 2
    assert lifecycle_tasks[0] is lifecycle_tasks[1]
    assert lifecycle_tasks[0] is not asyncio.current_task()


class _DumpResult:
    def __init__(self, payload: dict) -> None:
        self.payload = payload

    def model_dump(self, **_kwargs):
        return self.payload


class _ResourceSession:
    def __init__(self, payloads: list[dict]) -> None:
        self.payloads = payloads
        self.uris: list[str] = []

    async def read_resource(self, uri):
        self.uris.append(str(uri))
        return _DumpResult(self.payloads.pop(0))


@pytest.mark.asyncio
async def test_read_resource_bytes_joins_verified_mcp_chunks() -> None:
    uri1 = "hoi4-agent://workspace/test/artifact/first"
    uri2 = "hoi4-agent://workspace/test/artifact/second"

    def payload(data: bytes, *, offset: int, complete: bool, continuation: str | None) -> dict:
        return {
            "contents": [
                {
                    "uri": uri1,
                    "mimeType": "image/png",
                    "blob": base64.b64encode(data).decode(),
                    "_meta": {
                        "io.github.klimpaskov/hoi4-agent-tools.artifact-byte-range": {
                            "version": 1,
                            "unit": "byte",
                            "totalSize": 8,
                            "returnedRange": {"offset": offset, "length": len(data), "endExclusive": offset + len(data)},
                            "complete": complete,
                            "continuationUri": continuation,
                        }
                    },
                }
            ]
        }

    session = _ResourceSession(
        [
            payload(b"1234", offset=0, complete=False, continuation=uri2),
            payload(b"5678", offset=4, complete=False, continuation=None),
        ]
    )
    data = await read_resource_bytes(cast(Any, session), uri1, max_bytes=16, expected_mime="image/png")
    assert data == b"12345678"
    assert session.uris == [uri1, uri2]

    incomplete = _ResourceSession(
        [payload(b"1234", offset=0, complete=False, continuation=None)]
    )
    with pytest.raises(FocusTreeError, match="Incomplete MCP artifact resource"):
        await read_resource_bytes(
            cast(Any, incomplete), uri1, max_bytes=16, expected_mime="image/png"
        )


@pytest.mark.asyncio
async def test_focus_png_uses_dedicated_raster_tool() -> None:
    png = b"\x89PNG\r\n\x1a\nraster"
    uri = "hoi4-agent://workspace/test/artifact/focus.png"

    class RasterSession:
        def __init__(self) -> None:
            self.calls: list[tuple[str, dict[str, object]]] = []

        async def call_tool(self, name: str, arguments: dict[str, object]):
            self.calls.append((name, arguments))
            return _DumpResult(
                {
                    "isError": False,
                    "structuredContent": {
                        "status": "ok",
                        "artifacts": [
                            {"mimeType": "image/png", "uri": uri, "size": len(png)}
                        ],
                    },
                }
            )

        async def read_resource(self, requested_uri):
            assert str(requested_uri) == uri
            return _DumpResult(
                {
                    "contents": [
                        {
                            "uri": uri,
                            "mimeType": "image/png",
                            "blob": base64.b64encode(png).decode(),
                        }
                    ]
                }
            )

    session = RasterSession()
    client = FocusTreeMcpClient(
        Settings(discord_token="dummy", focus_tree_review_scale=0.5)
    )
    record = FocusTreeRecord(
        "test_tree",
        "common/national_focus/test.txt",
        3,
        ("TST",),
        ("Test",),
        (),
        12,
        123,
        456,
    )

    rendered = await client._render_one(cast(Any, session), "current", record)

    assert rendered == png
    assert session.calls == [
        (
            "hoi4.focus_raster",
            {
                "workspaceId": "current",
                "relativePath": "common/national_focus/test.txt",
                "treeId": "test_tree",
                "reviewScale": 0.5,
            },
        )
    ]


@pytest.mark.asyncio
async def test_focus_country_assets_use_private_chaosx_tool() -> None:
    flag = b"\x89PNG\r\n\x1a\nflag"
    leader = b"\x89PNG\r\n\x1a\nleader"
    flag_uri = "hoi4-agent://workspace/test/artifact/flag.png"
    leader_uri = "hoi4-agent://workspace/test/artifact/leader.png"

    class AssetSession:
        def __init__(self) -> None:
            self.calls: list[tuple[str, dict[str, object]]] = []

        async def call_tool(self, name: str, arguments: dict[str, object]):
            self.calls.append((name, arguments))
            return _DumpResult(
                {
                    "isError": False,
                    "structuredContent": {
                        "status": "ok",
                        "artifacts": [
                            {
                                "name": "chaosx-TST-flag.png",
                                "mimeType": "image/png",
                                "uri": flag_uri,
                                "size": len(flag),
                            },
                            {
                                "name": "chaosx-TST-leader.png",
                                "mimeType": "image/png",
                                "uri": leader_uri,
                                "size": len(leader),
                            },
                        ],
                        "data": {
                            "countries": [
                                {
                                    "tag": "TST",
                                    "flagArtifactName": "chaosx-TST-flag.png",
                                    "leaderPortraitArtifactName": "chaosx-TST-leader.png",
                                }
                            ]
                        },
                    },
                }
            )

        async def read_resource(self, requested_uri):
            uri = str(requested_uri)
            data = {flag_uri: flag, leader_uri: leader}[uri]
            return _DumpResult(
                {
                    "contents": [
                        {
                            "uri": uri,
                            "mimeType": "image/png",
                            "blob": base64.b64encode(data).decode(),
                        }
                    ]
                }
            )

    session = AssetSession()
    client = FocusTreeMcpClient(Settings(discord_token="dummy"))
    record = FocusTreeRecord(
        "TST_focus",
        "common/national_focus/003_test.txt",
        3,
        ("TST",),
        ("Test",),
        (),
        12,
        123,
        456,
    )

    rendered = await client._render_country_assets(cast(Any, session), "current", record)

    assert [(item.tag, item.kind, item.filename, item.png) for item in rendered] == [
        ("TST", "flag", "tst-flag.png", flag),
        ("TST", "leader", "tst-leader.png", leader),
    ]
    assert session.calls == [
        (
            "chaosx.focus_country_assets",
            {
                "workspaceId": "current",
                "countryTags": ["TST"],
                "eventId": 3,
                "treeId": "TST_focus",
            },
        )
    ]


@pytest.mark.asyncio
async def test_focus_graph_sends_country_assets_and_tree_in_one_message() -> None:
    record = FocusTreeRecord(
        "TST_focus",
        "common/national_focus/003_test.txt",
        3,
        ("TST",),
        ("Test",),
        (),
        12,
        123,
        456,
    )
    graph = FocusTreeGraph(
        record,
        b"tree",
        (
            FocusCountryAsset("TST", "flag", "tst-flag.png", b"flag"),
            FocusCountryAsset("TST", "leader", "tst-leader.png", b"leader"),
        ),
    )

    class Renderer:
        async def render(self, records):
            assert records == [record]
            return FocusTreeRenderBatch((graph,), 1, 0)

    class Followup:
        def __init__(self) -> None:
            self.calls: list[tuple[str, dict[str, object]]] = []

        async def send(self, content: str, **kwargs: object) -> None:
            self.calls.append((content, kwargs))

    followup = Followup()
    bot = SimpleNamespace(
        settings=Settings(discord_token="dummy", focus_tree_graphs_enabled=True),
        focus_tree_mcp=Renderer(),
    )
    interaction = SimpleNamespace(
        user=SimpleNamespace(id=1),
        guild_id=2,
        channel_id=3,
        followup=followup,
    )

    await send_focus_tree_graphs(cast(Any, bot), cast(Any, interaction), [record])

    assert len(followup.calls) == 1
    content, kwargs = followup.calls[0]
    assert content == "### Baseline focus tree, portrait, and flag"
    uploads = cast(list[Any], kwargs["files"])
    assert [upload.filename for upload in uploads] == [
        "TST_focus.png",
        "tst-leader.png",
        "tst-flag.png",
    ]


@pytest.mark.asyncio
async def test_focus_render_refreshes_country_assets_while_reusing_focus_png() -> None:
    client = FocusTreeMcpClient(Settings(discord_token="dummy"))
    record = FocusTreeRecord(
        "TST_focus",
        "common/national_focus/003_test.txt",
        3,
        ("TST",),
        ("Test",),
        (),
        12,
        123,
        456,
    )
    client._cache[client._cache_key(record)] = b"cached-tree"
    calls: list[dict[FocusTreeRecord, bytes]] = []

    async def refresh(records, cached_pngs=None):
        assert records == [record]
        assert cached_pngs == {record: b"cached-tree"}
        calls.append(cast(dict[FocusTreeRecord, bytes], cached_pngs))
        version = len(calls)
        return [
            FocusTreeGraph(
                record,
                b"cached-tree",
                (
                    FocusCountryAsset(
                        "TST", "flag", "tst-flag.png", f"flag-{version}".encode()
                    ),
                ),
            )
        ], 0

    client._render_uncached = refresh

    first = await client.render([record])
    second = await client.render([record])

    assert first.graphs[0].country_assets[0].png == b"flag-1"
    assert second.graphs[0].country_assets[0].png == b"flag-2"
    assert len(calls) == 2


@pytest.mark.asyncio
async def test_mcp_workspace_is_discovered_by_exact_name() -> None:
    result = _DumpResult(
        {
            "isError": False,
            "structuredContent": {
                "status": "ok",
                "data": {
                    "mods": [
                        {"id": "mod_other", "name": "other"},
                        {"id": "mod_chaos", "name": "chaos_redux"},
                    ]
                },
            },
        }
    )
    session = SimpleNamespace(call_tool=lambda *_args, **_kwargs: None)

    async def call_tool(*_args, **_kwargs):
        return result

    async def list_tools():
        return SimpleNamespace(tools=[SimpleNamespace(name="hoi4.mods")])

    session.call_tool = call_tool
    session.list_tools = list_tools
    client = FocusTreeMcpClient(Settings(discord_token="dummy"))
    assert await client._workspace_id(cast(Any, session)) == "mod_chaos"


@pytest.mark.asyncio
async def test_mcp_workspace_uses_current_for_inventory_free_server() -> None:
    async def list_tools():
        return SimpleNamespace(tools=[SimpleNamespace(name="hoi4.gui_render")])

    session = SimpleNamespace(list_tools=list_tools)
    client = FocusTreeMcpClient(Settings(discord_token="dummy"))
    assert await client._workspace_id(cast(Any, session)) == "current"
