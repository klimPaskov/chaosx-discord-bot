from __future__ import annotations

import base64
import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest

from chaosx_bot.bot import ChaosXBot, register_commands
from chaosx_bot.config import Settings
from chaosx_bot.focus_trees import (
    FocusTreeCatalog,
    FocusTreeMcpClient,
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
    assert record.focus_count == 2
    assert catalog.for_event("007") == [record]
    assert catalog.search("event 7") == [record]
    assert catalog.search("ABC") == [record]
    assert catalog.search("alpha republic") == [record]
    assert catalog.search("story focus") == [record]
    assert catalog.for_event(2) == []


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

    assert not temporary_root.exists()


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
            payload(b"5678", offset=4, complete=True, continuation=None),
        ]
    )
    data = await read_resource_bytes(cast(Any, session), uri1, max_bytes=16, expected_mime="image/png")
    assert data == b"12345678"
    assert session.uris == [uri1, uri2]


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
