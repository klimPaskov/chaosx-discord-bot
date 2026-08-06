import threading
from types import SimpleNamespace
from typing import Any, cast

import pytest

from chaosx_bot.bot import (
    send_focus_tree_graphs,
    send_related_event_visuals,
    send_scripted_response,
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