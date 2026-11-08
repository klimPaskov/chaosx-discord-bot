from __future__ import annotations

import asyncio
import base64
import io
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, cast

import pytest
from PIL import Image, ImageDraw

from chaosx_bot.config import Settings
from chaosx_bot.event_visuals import (
    EventChainCatalog,
    EventChainRecord,
    EmptyGuiPreviewError,
    EventVisualMcpClient,
    ScriptedGuiCatalog,
    ScriptedGuiRecord,
    UiWindowCatalog,
    UiWindowRecord,
    _crop_and_scale_gui_preview,
    _remove_offline_banner,
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
                    "name": "event-neighborhood.json" if self.mime_type == "application/json" else "render.svg",
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


def test_death_package_keeps_all_seven_top_level_event_definitions(tmp_path: Path) -> None:
    root = tmp_path / "events"
    root.mkdir()
    event_ids = (
        "chaosx.nr10.1",
        "chaosx.nr10.2",
        "chaosx.nr10.3",
        "chaosx.nr10.10",
        "chaosx.nr10.20",
        "chaosx.nr10.21",
        "chaosx.nr10.24",
    )
    blocks = "\n".join(
        f"country_event = {{ id = {event_id} is_triggered_only = yes }}"
        for event_id in event_ids
    )
    (root / "010_death.txt").write_text(
        f"add_namespace = chaosx.nr10\n{blocks}\n",
        encoding="utf-8",
    )

    record = EventChainCatalog(tmp_path).for_event(10)

    assert record is not None
    assert record.event_keys == event_ids
    assert record.primary_event_key == "chaosx.nr10.1"


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


def test_event_cache_key_changes_with_output_dpi() -> None:
    record = EventChainRecord(
        "events/007_fury.txt",
        "Fury",
        7,
        ("chaosx.nr7.1",),
        1,
        2,
    )
    standard = EventVisualMcpClient(
        Settings(discord_token="dummy", event_chain_graphviz_dpi=144)
    )
    readable = EventVisualMcpClient(
        Settings(discord_token="dummy", event_chain_graphviz_dpi=192)
    )

    assert standard._event_key(record) != readable._event_key(record)


@pytest.mark.asyncio
async def test_event_chain_render_calls_mcp_and_builds_compact_png() -> None:
    graph = b'''{
      "nodes": [
        {"id":"event:chaosx.nr7.1","kind":"event","eventId":"chaosx.nr7.1","metadata":{}},
        {"id":"option:one","kind":"option"},
        {"id":"event:chaosx.nr7.2","kind":"event","eventId":"chaosx.nr7.2","metadata":{}}
      ],
      "edges": [
        {"from":"event:chaosx.nr7.1","to":"option:one"},
        {"from":"option:one","to":"event:chaosx.nr7.2"}
      ]
    }'''
    session = FakeSession(mime_type="application/json", data=graph)
    settings = Settings(discord_token="dummy", event_chain_max_depth=2, event_chain_max_nodes=40)
    client = EventVisualMcpClient(settings)
    record = EventChainRecord(
        "events/007_fury.txt",
        "Fury",
        7,
        ("chaosx.nr7.1", "chaosx.nr7.2"),
        1,
        2,
    )

    rendered = await client._render_event_chain(cast(Any, session), "workspace", record)

    assert rendered.startswith(b"\x89PNG\r\n\x1a\n")
    assert session.calls[0][0] == "hoi4.event_render"
    arguments = session.calls[0][1]
    assert arguments["selector"] == {
        "kind": "manifest",
        "manifest": {"eventIds": ["chaosx.nr7.1", "chaosx.nr7.2"]},
    }
    assert arguments["maxDepth"] == 2
    assert arguments["maxNodes"] == 40


@pytest.mark.asyncio
async def test_scripted_gui_render_reads_svg_and_converts_to_png() -> None:
    svg = b'''<svg xmlns="http://www.w3.org/2000/svg" width="320" height="200">
      <rect width="320" height="200" fill="#111923"/>
      <rect x="20" y="50" width="220" height="100" fill="#345678" stroke="#d9a928" stroke-width="4"/>
    </svg>'''
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
        "state": "active",
        "scriptedGui": {"fury_gui": True},
        "visibility": {"fury_window": True, "fury_gui": True},
    }
    assert arguments["states"] == ["active"]


@pytest.mark.asyncio
async def test_scripted_gui_cache_uses_mcp_dependency_revision(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path))
    settings = Settings(discord_token="dummy")
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
    current_revision = ["a" * 64]
    renders = 0

    @asynccontextmanager
    async def fake_session():
        yield cast(Any, object()), "workspace"

    async def revisions(*_args: object) -> dict[tuple[str, str], str]:
        return {(record.window_name, record.gui_id): current_revision[0]}

    async def render(*_args: object) -> bytes:
        nonlocal renders
        renders += 1
        return b"\x89PNG\r\n\x1a\n" + bytes([renders])

    monkeypatch.setattr(client, "_session", fake_session)
    monkeypatch.setattr(client, "_gui_revisions", revisions)
    monkeypatch.setattr(client, "_render_gui", render)

    first, _ = await client.render_scripted_guis((record,))
    unchanged, _ = await client.render_scripted_guis((record,))
    assert first[0].png == unchanged[0].png
    assert renders == 1

    current_revision[0] = "b" * 64
    changed, _ = await client.render_scripted_guis((record,))
    assert changed[0].png != first[0].png
    assert renders == 2

    fresh_client = EventVisualMcpClient(settings)
    monkeypatch.setattr(fresh_client, "_session", fake_session)
    monkeypatch.setattr(fresh_client, "_gui_revisions", revisions)

    async def unexpected_render(*_args: object) -> bytes:
        raise AssertionError("fresh client should reuse the revision-keyed disk cache")

    monkeypatch.setattr(fresh_client, "_render_gui", unexpected_render)
    disk_cached, _ = await fresh_client.render_scripted_guis((record,))
    assert disk_cached[0].png == changed[0].png


@pytest.mark.asyncio
async def test_related_visuals_render_chain_and_useful_gui_concurrently(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path))
    client = EventVisualMcpClient(Settings(discord_token="dummy"))
    chain = EventChainRecord(
        "events/001_test.txt", "Test", 1, ("chaosx.nr1.1",), 101, 202
    )
    mapicon = ScriptedGuiRecord(
        "common/scripted_guis/001_test.txt",
        "Map icon",
        "test_mapicon_gui",
        "test_mapicon_window",
        "player_context",
        1,
        101,
        202,
    )
    dashboard = ScriptedGuiRecord(
        "common/scripted_guis/001_test.txt",
        "Dashboard",
        "test_dashboard_gui",
        "test_dashboard_window",
        "player_context",
        1,
        101,
        202,
    )
    started: set[str] = set()
    release = asyncio.Event()

    async def rendezvous(name: str) -> bytes:
        started.add(name)
        if len(started) == 2:
            release.set()
        await asyncio.wait_for(release.wait(), timeout=1)
        return b"\x89PNG\r\n\x1a\nrendered"

    async def render_chain(*_args: object) -> bytes:
        return await rendezvous("chain")

    async def render_gui(*args: object) -> bytes:
        record = cast(ScriptedGuiRecord, args[-1])
        return await rendezvous(record.gui_id)

    @asynccontextmanager
    async def fake_session():
        yield cast(Any, object()), "workspace"

    monkeypatch.setattr(client, "_render_event_chain", render_chain)
    monkeypatch.setattr(client, "_render_gui", render_gui)
    monkeypatch.setattr(client, "_session", fake_session)

    async def revisions(*_args: object) -> dict[tuple[str, str], str]:
        return {(dashboard.window_name, dashboard.gui_id): "a" * 64}

    monkeypatch.setattr(client, "_gui_revisions", revisions)

    result = await client.render_related(chain, (mapicon, dashboard))

    assert started == {"chain", "test_dashboard_gui"}
    assert result.chain is not None
    assert [preview.record for preview in result.guis] == [dashboard]
    assert not result.chain_failed
    assert result.failed_guis == 0


def test_empty_scripted_gui_preview_is_suppressed() -> None:
    image = Image.new("RGB", (320, 200), "#111923")
    ImageDraw.Draw(image).rectangle((8, 5, 220, 24), outline="#d9a928", width=2)
    output = io.BytesIO()
    image.save(output, format="PNG")

    with pytest.raises(EmptyGuiPreviewError):
        _crop_and_scale_gui_preview(output.getvalue(), 320, 200)




def test_gui_preview_upscale_keeps_hard_edges() -> None:
    """Upscaled previews must keep crisp 1px edges (NEAREST), not LANCZOS-smear them.

    Regression: _crop_and_scale_gui_preview used LANCZOS for 2-4x enlargements,
    which blurred bitmap-font scripted-GUI previews.
    """
    image = Image.new("RGB", (320, 200), "#ffffff")
    ImageDraw.Draw(image).rectangle((60, 60, 100, 100), fill="#000000")
    output = io.BytesIO()
    image.save(output, format="PNG")

    preview = _crop_and_scale_gui_preview(output.getvalue(), 320, 200)
    img = Image.open(io.BytesIO(preview)).convert("L")
    assert img.size[0] > 320 // 2  # content was enlarged to fill the max preview size

    # Scan the middle row across the black square's left edge: NEAREST yields only
    # hard 0/255 pixels, LANCZOS would introduce intermediate grays.
    row = [img.getpixel((x, img.size[1] // 2)) for x in range(img.size[0])]
    intermediates = sum(1 for v in row if v not in (0, 255))
    assert intermediates == 0

def test_offline_disclaimer_is_removed_from_gui_svg() -> None:
    svg = b'''<svg xmlns="http://www.w3.org/2000/svg" width="320" height="200">
      <rect width="320" height="200" fill="#111923"/>
      <g><rect x="8" y="8" width="215" height="20" fill="#05080c"/><text>OFFLINE APPROXIMATION - NOT HOI4</text></g>
    </svg>'''

    cleaned = _remove_offline_banner(svg)

    assert b"OFFLINE APPROXIMATION" not in cleaned
    assert b"#111923" in cleaned


GUI_FILE = """
guiTypes = {
	containerWindowType = {
		name = "chaos_meter_popup_window"
		size = { width = 527 height = 534 }
		containerWindowType = {
			name = "chaos_meter_status_content"
		}
	}
	instantTextBoxType = { name = "stray_text" }
	windowType = { name = "old_style_window" }
}
"""


def test_ui_window_catalog_lists_top_level_windows_and_resolves_queries(tmp_path: Path) -> None:
    interface = tmp_path / "interface"
    interface.mkdir()
    (interface / "chaosx_chaos_meter_popup.gui").write_text(GUI_FILE, encoding="utf-8")
    (interface / "007_fury_leader_overlay.gui").write_text(
        'guiTypes = { containerWindowType = { name = "fury_overlay" } }', encoding="utf-8"
    )

    catalog = UiWindowCatalog(tmp_path)

    names = [record.window_name for record in catalog.discover()]
    assert names == ["fury_overlay", "chaos_meter_popup_window", "old_style_window"]
    assert [record.window_name for record in catalog.search("chaos_meter_popup_window")] == ["chaos_meter_popup_window"]
    # A file-name query matches every window the file defines.
    assert [record.window_name for record in catalog.search("meter popup")] == [
        "chaos_meter_popup_window",
        "old_style_window",
    ]
    assert [record.window_name for record in catalog.search("7")] == ["fury_overlay"]
    assert catalog.search("nothing like this") == []


@pytest.mark.asyncio
async def test_ui_window_render_has_no_scripted_gui_flag() -> None:
    svg = b'''<svg xmlns="http://www.w3.org/2000/svg" width="320" height="200">
      <rect width="320" height="200" fill="#111923"/>
      <rect x="20" y="50" width="220" height="100" fill="#345678" stroke="#d9a928" stroke-width="4"/>
    </svg>'''
    session = FakeSession(mime_type="image/svg+xml", data=svg, text=True)
    settings = Settings(discord_token="dummy", scripted_gui_preview_width=320, scripted_gui_preview_height=200)
    client = EventVisualMcpClient(settings)

    png = await client._render_window(cast(Any, session), "chaos_redux", "chaos_meter_popup_window", "")

    assert png.startswith(b"\x89PNG\r\n\x1a\n")
    name, arguments = session.calls[0]
    assert name == "hoi4.gui_render"
    assert arguments["workspaceId"] == "chaos_redux"
    assert arguments["windowName"] == "chaos_meter_popup_window"
    scenario = cast(dict[str, Any], arguments["scenario"])
    assert scenario["scriptedGui"] == {}
    assert scenario["visibility"] == {"chaos_meter_popup_window": True}


@pytest.mark.asyncio
async def test_ui_window_previews_are_cached_and_failures_counted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path))
    client = EventVisualMcpClient(Settings(discord_token="dummy", scripted_gui_max_previews=3))
    good = UiWindowRecord("interface/a.gui", "good_window", "A", None, 1, 2)
    empty = UiWindowRecord("interface/b.gui", "empty_window", "B", None, 1, 2)
    bad = UiWindowRecord("interface/c.gui", "bad_window", "C", None, 1, 2)
    renders: list[str] = []

    @asynccontextmanager
    async def fake_session():
        yield cast(Any, object()), "workspace"

    async def render(_session: object, _workspace: str, window_name: str, gui_id: str) -> bytes:
        assert gui_id == ""
        renders.append(window_name)
        if window_name == "empty_window":
            raise EmptyGuiPreviewError("blank")
        if window_name == "bad_window":
            raise RuntimeError("boom")
        return b"\x89PNG\r\n\x1a\nui"

    monkeypatch.setattr(client, "_session", fake_session)
    monkeypatch.setattr(client, "_render_window", render)

    previews, failed = await client.render_ui_windows((good, empty, bad))
    again, _ = await client.render_ui_windows((good,))

    assert [preview.record.window_name for preview in previews] == ["good_window"]
    assert failed == 1
    assert [preview.png for preview in again] == [b"\x89PNG\r\n\x1a\nui"]
    assert renders.count("good_window") == 1


@pytest.mark.asyncio
async def test_gui_revisions_fall_back_to_file_stats_without_the_plugin() -> None:
    """A host without the private ChaosX plugin still previews scripted GUIs."""

    class NoPluginSession:
        async def call_tool(self, name: str, _arguments: dict[str, object]) -> object:
            raise RuntimeError(f"unknown tool {name}")

    client = EventVisualMcpClient(Settings(discord_token="dummy"))
    record = ScriptedGuiRecord("common/scripted_guis/a.txt", "A", "a_gui", "a_window", "ctx", 1, 10, 20)
    changed = ScriptedGuiRecord("common/scripted_guis/a.txt", "A", "a_gui", "a_window", "ctx", 1, 11, 20)

    first = await client._gui_revisions(cast(Any, NoPluginSession()), "workspace", [record])
    second = await client._gui_revisions(cast(Any, NoPluginSession()), "workspace", [changed])

    key = ("a_window", "a_gui")
    assert set(first) == {key}
    assert len(first[key]) == 64 and first[key] != second[key]


class _TechSession:
    """Answers hoi4.tech_render for the folder IDs in ``known`` and fails for any other."""

    def __init__(self, known: set[str]) -> None:
        self.known = known
        self.calls: list[dict[str, object]] = []
        self.png = b"\x89PNG\r\n\x1a\ntech"

    async def call_tool(self, name: str, arguments: dict[str, object]) -> object:
        assert name == "hoi4.tech_render"
        self.calls.append(arguments)
        ok = arguments.get("folderId") in self.known
        structured: dict[str, object] = (
            {
                "status": "ok",
                "artifacts": [
                    {"mimeType": "image/png", "uri": "hoi4-agent://workspace/test/artifact/tech", "size": len(self.png)}
                ],
            }
            if ok
            else {"status": "error", "code": "TECH_RENDER_FOLDER_EMPTY"}
        )

        class Result:
            def model_dump(self, **_: object) -> dict[str, object]:
                return {"isError": not ok, "structuredContent": structured}

        return Result()

    async def read_resource(self, _: object) -> FakeResourceResult:
        return FakeResourceResult(mime_type="image/png", data=self.png)


def _tech_client(session: _TechSession, monkeypatch: pytest.MonkeyPatch) -> EventVisualMcpClient:
    client = EventVisualMcpClient(Settings(discord_token="dummy"))

    @asynccontextmanager
    async def fake_session():
        yield cast(Any, session), "chaos_redux"

    monkeypatch.setattr(client, "_session", fake_session)
    return client


@pytest.mark.asyncio
async def test_tech_folder_view_retries_with_the_folder_suffix(monkeypatch: pytest.MonkeyPatch) -> None:
    session = _TechSession({"biowarfare_folder"})
    client = _tech_client(session, monkeypatch)

    png = await client.render_tech_view(view="folder", folder_id="biowarfare")

    assert png == session.png
    assert [call["folderId"] for call in session.calls] == ["biowarfare", "biowarfare_folder"]


@pytest.mark.asyncio
async def test_tech_folder_view_uses_an_exact_id_without_retrying(monkeypatch: pytest.MonkeyPatch) -> None:
    session = _TechSession({"naval_folder"})
    client = _tech_client(session, monkeypatch)

    await client.render_tech_view(view="folder", folder_id="naval_folder")

    assert [call["folderId"] for call in session.calls] == ["naval_folder"]


@pytest.mark.asyncio
async def test_tech_view_failure_is_a_visual_error(monkeypatch: pytest.MonkeyPatch) -> None:
    from chaosx_bot.event_visuals import EventVisualError

    client = _tech_client(_TechSession(set()), monkeypatch)

    with pytest.raises(EventVisualError):
        await client.render_tech_view(view="folder", folder_id="nothing")
