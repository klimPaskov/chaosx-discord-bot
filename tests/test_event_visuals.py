from __future__ import annotations

import base64
from pathlib import Path
from typing import Any, cast

import pytest

from chaosx_bot.config import Settings
from chaosx_bot.event_visuals import (
    EventChainCatalog,
    EventChainRecord,
    EventVisualMcpClient,
    ScriptedGuiCatalog,
    ScriptedGuiRecord,
)


class FakeToolResult:
    def __init__(self, artifacts: list[dict[str, object]]) -> None:
        self.artifacts = artifacts

    def model_dump(self, **_: object) -> dict[str, object]:
        return {
            "isError": False,
            "structuredContent": {
                "status": "ok",
                "code": "RENDERED",
                "artifacts": self.artifacts,
            },
        }


class FakeResourceResult:
    def __init__(self, *, mime_type: str, data: bytes, text: bool = False) -> None:
        self.mime_type = mime_type
        self.data = data
        self.text = text

    def model_dump(self, **_: object) -> dict[str, object]:
        content: dict[str, object] = {
            "uri": "hoi4-agent://test",
            "mimeType": self.mime_type,
            "_meta": {
                "io.github.test.artifact-byte-range": {
                    "returnedRange": {"offset": 0},
                    "totalSize": len(self.data),
                    "complete": True,
                    "continuationUri": None,
                }
            },
        }
        if self.text:
            content["text"] = self.data.decode("utf-8")
        else:
            content["blob"] = base64.b64encode(self.data).decode("ascii")
        return {"contents": [content]}


class FakeSession:
    def __init__(self, *, mime_type: str, data: bytes, text: bool = False) -> None:
        self.mime_type = mime_type
        self.data = data
        self.text = text
        self.calls: list[tuple[str, dict[str, object]]] = []

    async def call_tool(self, name: str, arguments: dict[str, object]) -> FakeToolResult:
        self.calls.append((name, arguments))
        return FakeToolResult(
            [
                {
                    "mimeType": self.mime_type,
                    "uri": "hoi4-agent://workspace/test/artifact/render",
                    "size": len(self.data),
                }
            ]
        )

    async def read_resource(self, _: object) -> FakeResourceResult:
        return FakeResourceResult(mime_type=self.mime_type, data=self.data, text=self.text)


def test_event_chain_catalog_discovers_and_resolves_event(tmp_path: Path) -> None:
    root = tmp_path / "events"
    root.mkdir()
    (root / "007_fury.txt").write_text(
        """
        add_namespace = chaosx.nr7
        country_event = { id = chaosx.nr7.1 country_event = { id = chaosx.nr7.2 } }
        news_event = { id = chaosx.news.7007 }
        """,
        encoding="utf-8",
    )

    catalog = EventChainCatalog(tmp_path)
    records = catalog.discover()

    assert len(records) == 1
    assert records[0].event_id == 7
    assert records[0].primary_event_key == "chaosx.nr7.1"
    assert records[0].event_keys == ("chaosx.nr7.1", "chaosx.news.7007")
    assert catalog.find("7") == records[0]
    assert catalog.find("Fury") == records[0]
    assert catalog.find("chaosx.news.7007") == records[0]


def test_scripted_gui_catalog_discovers_windows_and_event_matches(tmp_path: Path) -> None:
    root = tmp_path / "common" / "scripted_guis"
    root.mkdir(parents=True)
    (root / "007_fury_scripted_guis.txt").write_text(
        """
        scripted_gui = {
          fury_overlay_gui = {
            context_type = diplomacy_target_context
            window_name = "fury_overlay_window"
            visible = { always = yes }
          }
          fury_panel_gui = {
            context_type = player_context
            window_name = fury_panel_window
          }
        }
        """,
        encoding="utf-8",
    )

    catalog = ScriptedGuiCatalog(tmp_path)
    records = catalog.discover()

    assert [(record.gui_id, record.window_name) for record in records] == [
        ("fury_overlay_gui", "fury_overlay_window"),
        ("fury_panel_gui", "fury_panel_window"),
    ]
    assert catalog.for_event(7) == records
    assert catalog.search("fury overlay") == [records[0]]
    assert catalog.search("fury_panel_window") == [records[1]]


@pytest.mark.asyncio
async def test_event_chain_render_calls_mcp_and_reads_png() -> None:
    png = b"\x89PNG\r\n\x1a\nchain"
    session = FakeSession(mime_type="image/png", data=png)
    settings = Settings(discord_token="dummy", event_chain_max_depth=2, event_chain_max_nodes=40)
    client = EventVisualMcpClient(settings)
    record = EventChainRecord("events/007_fury.txt", "Fury", 7, ("chaosx.nr7.1",), 1, 2)

    rendered = await client._render_event_chain(cast(Any, session), "workspace", record)

    assert rendered == png
    assert session.calls[0][0] == "hoi4.event_render"
    arguments = session.calls[0][1]
    assert arguments["selector"] == {
        "kind": "manifest",
        "manifest": {"eventIds": ["chaosx.nr7.1"]},
    }
    assert arguments["maxDepth"] == 2
    assert arguments["maxNodes"] == 40


@pytest.mark.asyncio
async def test_scripted_gui_render_reads_svg_and_converts_to_png() -> None:
    svg = b'<svg xmlns="http://www.w3.org/2000/svg" width="320" height="200"><rect width="320" height="200" fill="#123456"/></svg>'
    session = FakeSession(mime_type="image/svg+xml", data=svg, text=True)
    settings = Settings(discord_token="dummy", scripted_gui_preview_width=320, scripted_gui_preview_height=200)
    client = EventVisualMcpClient(settings)
    record = ScriptedGuiRecord(
        "common/scripted_guis/007_fury.txt",
        "Fury",
        "fury_gui",
        "fury_window",
        "player_context",
        7,
        1,
        2,
    )

    png = await client._render_gui(cast(Any, session), "workspace", record)

    assert png.startswith(b"\x89PNG\r\n\x1a\n")
    assert session.calls[0][0] == "hoi4.gui_render"
    arguments = session.calls[0][1]
    assert arguments["windowName"] == "fury_window"
    assert arguments["scenario"] == {
        "id": "discord-preview",
        "resolution": {"width": 320, "height": 200},
        "state": "normal",
    }
