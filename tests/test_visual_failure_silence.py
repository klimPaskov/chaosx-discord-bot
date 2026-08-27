import threading
from types import SimpleNamespace
from typing import Any, cast

import pytest

from chaosx_bot.bot import (
    send_focus_tree_graphs,
    send_related_event_visuals,
    send_scripted_response,
    send_visuals_with_working_status,
)
from chaosx_bot.config import Settings
from chaosx_bot.event_visuals import EventVisualError
from chaosx_bot.focus_trees import FocusTreeError


class _Followup:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []

    async def send(self, content: str, **kwargs: Any) -> SimpleNamespace:
        self.calls.append((content, kwargs))
        return SimpleNamespace(id=100 + len(self.calls))


class _Store:
    def __init__(self) -> None:
        self.audits: list[dict[str, Any]] = []

    async def audit(self, **kwargs: Any) -> None:
        self.audits.append(kwargs)


class _Status:
    def __init__(self) -> None:
        self.edits: list[str] = []
        self.deleted = False

    async def edit(self, *, content: str, **kwargs: Any) -> None:
        self.edits.append(content)

    async def delete(self, **kwargs: Any) -> None:
        self.deleted = True


class _WorkingFollowup:
    def __init__(self, status: _Status) -> None:
        self.status = status
        self.calls: list[tuple[str, dict[str, Any]]] = []

    async def send(self, content: str, **kwargs: Any) -> _Status:
        self.calls.append((content, kwargs))
        return self.status


@pytest.mark.asyncio
async def test_focus_tree_mcp_failure_is_audited_but_not_posted():
    class Renderer:
        async def render(self, _records: list[Any]) -> None:
            raise FocusTreeError("renderer unavailable")

    followup = _Followup()
    store = _Store()
    bot = SimpleNamespace(
        settings=Settings(discord_token="dummy", focus_tree_graphs_enabled=True),
        focus_tree_mcp=Renderer(),
        store=store,
    )
    interaction = SimpleNamespace(
        user=SimpleNamespace(id=1), guild_id=2, channel_id=3, followup=followup
    )

    await send_focus_tree_graphs(
        cast(Any, bot), cast(Any, interaction), cast(Any, [object()])
    )

    assert followup.calls == []
    assert store.audits[0]["command"] == "focus tree render error"


@pytest.mark.asyncio
async def test_related_visual_mcp_failure_is_audited_but_not_posted():
    class Renderer:
        async def render_related(self, _chain: Any, _guis: list[Any]) -> None:
            raise EventVisualError("renderer unavailable")

    followup = _Followup()
    store = _Store()
    bot = SimpleNamespace(
        settings=Settings(
            discord_token="dummy",
            event_chain_graphs_enabled=True,
            scripted_gui_previews_enabled=True,
        ),
        event_chain_catalog=SimpleNamespace(for_event=lambda _event_id: object()),
        scripted_gui_catalog=SimpleNamespace(for_event=lambda _event_id: [object()]),
        event_visual_mcp=Renderer(),
        store=store,
    )
    interaction = SimpleNamespace(
        user=SimpleNamespace(id=1), guild_id=2, channel_id=3, followup=followup
    )

    await send_related_event_visuals(cast(Any, bot), cast(Any, interaction), 18)

    assert followup.calls == []
    assert store.audits[0]["command"] == "related event visuals error"


@pytest.mark.asyncio
async def test_scripted_render_runs_off_gateway_loop_and_attachment_error_is_silent():
    gateway_thread = threading.get_ident()
    render_threads: list[int] = []
    followup = _Followup()
    store = _Store()

    class Response:
        async def defer(self, **_kwargs: Any) -> None:
            return None

        def is_done(self) -> bool:
            return False

        async def send_message(self, *_args: Any, **_kwargs: Any) -> None:
            return None

    def render() -> str:
        render_threads.append(threading.get_ident())
        return "Current catalog output"

    async def after_send() -> None:
        raise EventVisualError("renderer unavailable")

    settings = Settings(discord_token="dummy", allowed_guild_id=2, owner_id=99)
    bot = SimpleNamespace(
        settings=settings,
        rate_limiter=SimpleNamespace(
            check=lambda **_kwargs: SimpleNamespace(allowed=True, retry_after_seconds=0)
        ),
        store=store,
    )
    interaction = SimpleNamespace(
        user=SimpleNamespace(id=1),
        guild_id=2,
        channel_id=3,
        response=Response(),
        followup=followup,
    )

    await send_scripted_response(
        cast(Any, bot),
        cast(Any, interaction),
        command_name="chaosx event",
        summary="18",
        render=render,
        after_send=after_send,
    )

    assert render_threads and render_threads[0] != gateway_thread
    assert [content for content, _kwargs in followup.calls] == ["Current catalog output"]
    assert store.audits[-1]["command"] == "chaosx event attachment error"


@pytest.mark.asyncio
async def test_scripted_response_awaits_async_render() -> None:
    """Async renders must be awaited, not run through to_thread (f929baa6 regression:
    /event returned an unawaited coroutine -> TypeError -> interaction never responded)."""
    followup = _Followup()
    store = _Store()

    class Response:
        async def defer(self, **_kwargs: Any) -> None:
            return None

    async def render() -> str:
        return "Async event content"

    bot = SimpleNamespace(
        settings=Settings(discord_token="dummy", allowed_guild_id=2, owner_id=99),
        rate_limiter=SimpleNamespace(
            check=lambda **_kwargs: SimpleNamespace(allowed=True, retry_after_seconds=0)
        ),
        store=store,
    )
    interaction = SimpleNamespace(
        user=SimpleNamespace(id=1),
        guild_id=2,
        channel_id=3,
        response=Response(),
        followup=followup,
    )

    await send_scripted_response(
        cast(Any, bot),
        cast(Any, interaction),
        command_name="chaosx event",
        summary="1",
        render=render,
    )

    assert [content for content, _kwargs in followup.calls] == ["Async event content"]
    assert store.audits[0]["command"] == "chaosx event"


@pytest.mark.asyncio
async def test_visual_working_status_shows_updates_and_cleans_up():
    status = _Status()
    followup = _WorkingFollowup(status)
    store = _Store()

    class FocusRenderer:
        async def render(self, _records: list[Any]) -> SimpleNamespace:
            return SimpleNamespace(
                graphs=[],
                attempted=0,
                failed=0,
            )

    class RelatedRenderer:
        async def render_related(self, _chain: Any, _guis: list[Any]) -> SimpleNamespace:
            return SimpleNamespace(chain=None, guis=[], chain_failed=False, failed_guis=0)

    bot = SimpleNamespace(
        settings=Settings(
            discord_token="dummy",
            focus_tree_graphs_enabled=True,
            event_chain_graphs_enabled=True,
            scripted_gui_previews_enabled=True,
        ),
        focus_tree_mcp=FocusRenderer(),
        event_visual_mcp=RelatedRenderer(),
        event_chain_catalog=SimpleNamespace(for_event=lambda _event_id: None),
        scripted_gui_catalog=SimpleNamespace(for_event=lambda _event_id: []),
        store=store,
    )
    interaction = SimpleNamespace(
        user=SimpleNamespace(id=1), guild_id=2, channel_id=3, followup=followup
    )

    await send_visuals_with_working_status(
        cast(Any, bot),
        cast(Any, interaction),
        focus_records=[object()],
        chain=object(),
        guis=[object()],
        event_id=18,
    )

    assert followup.calls[0][0] == "⏳ Still working — retrieving visual previews…"
    assert followup.calls[0][1].get("ephemeral") is True
    assert status.edits == [
        "⏳ Retrieving focus tree previews…",
        "⏳ Retrieving event chain & scripted GUIs…",
    ]
    assert status.deleted is True


@pytest.mark.asyncio
async def test_visual_working_status_stays_silent_when_nothing_to_render():
    followup = _WorkingFollowup(_Status())
    store = _Store()
    bot = SimpleNamespace(
        settings=Settings(
            discord_token="dummy",
            focus_tree_graphs_enabled=True,
            event_chain_graphs_enabled=True,
            scripted_gui_previews_enabled=True,
        ),
        store=store,
    )
    interaction = SimpleNamespace(
        user=SimpleNamespace(id=1), guild_id=2, channel_id=3, followup=followup
    )

    await send_visuals_with_working_status(
        cast(Any, bot),
        cast(Any, interaction),
        focus_records=[],
        chain=None,
        guis=[],
        event_id=18,
    )

    assert followup.calls == []