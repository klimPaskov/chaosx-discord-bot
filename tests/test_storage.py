import json

from chaosx_bot.storage import Store


async def test_list_playtest_reports_returns_observations(tmp_path):
    store = Store(tmp_path / "chaosx-test.db")
    await store.init()
    await store.create_playtest(
        playtest_id="pt-1",
        actor_id=123,
        guild_id=456,
        channel_id=789,
        target="event id 7",
        start_time="",
        duration_minutes=0,
        voice="",
        build="",
    )
    report = {"event_id": "7", "observation": "Fury snowballed too quickly.", "reporter_id": 123, "created_at": "2026-07-13T12:00:00+00:00"}
    await store.add_playtest_report(playtest_id="pt-1", report=report)

    rows = await store.list_playtest_reports(limit=5)

    assert len(rows) == 1
    playtest_id, _created_at, target, status, report_json = rows[0]
    assert playtest_id == "pt-1"
    assert target == "event id 7"
    assert status == "reported"
    assert json.loads(report_json)["observation"] == "Fury snowballed too quickly."


async def test_automation_list_includes_descriptions(tmp_path):
    store = Store(tmp_path / "chaosx-test.db")
    await store.init()

    rows = await store.list_automations()

    by_name = {name: (enabled, destination, description) for name, enabled, destination, description in rows}
    assert "weekly_content_dump" in by_name
    assert by_name["weekly_content_dump"][0] == 1
    assert "fresh visuals" in by_name["weekly_content_dump"][2]
    for deleted_name in {
        "agent_draft_pr_mode",
        "ci_failure_first_recovery",
        "stale_blocker_reminder",
        "trusted_role_direct_issue_creation",
        "weekly_project_digest",
        "selected_channel_content_watcher",
        "pull_request_ready_summary",
    }:
        assert deleted_name not in by_name


async def test_admin_ask_memory_is_scoped_pruned_and_clearable(tmp_path):
    store = Store(tmp_path / "chaosx-test.db")
    await store.init()
    for idx in range(4):
        await store.record_admin_ask_turn(
            actor_id=123,
            guild_id=456,
            channel_id=789,
            prompt_hash=f"hash-{idx}",
            status="ok",
            request=f"request {idx}",
            output_excerpt=f"output {idx}",
            keep_last=3,
        )
    await store.record_admin_ask_turn(
        actor_id=123,
        guild_id=456,
        channel_id=999,
        prompt_hash="other-channel",
        status="ok",
        request="other request",
        output_excerpt="other output",
    )

    rows = await store.list_admin_ask_memory(actor_id=123, guild_id=456, channel_id=789, limit=10)

    assert [row[3] for row in rows] == ["request 1", "request 2", "request 3"]
    assert await store.list_admin_ask_memory(actor_id=123, guild_id=456, channel_id=999, limit=10)
    deleted = await store.clear_admin_ask_memory(actor_id=123, guild_id=456, channel_id=789)
    assert deleted == 3
    assert await store.list_admin_ask_memory(actor_id=123, guild_id=456, channel_id=789, limit=10) == []
    assert await store.list_admin_ask_memory(actor_id=123, guild_id=456, channel_id=999, limit=10)


async def test_message_ask_memory_is_channel_scoped_pruned_and_chainable(tmp_path):
    store = Store(tmp_path / "chaosx-test.db")
    await store.init()
    for idx in range(4):
        await store.record_message_ask_turn(
            mode="public",
            actor_id=100 + idx,
            guild_id=456,
            channel_id=789,
            source_message_id=1000 + idx,
            bot_message_id=2000 + idx,
            parent_bot_message_id=2000 + idx - 1 if idx else None,
            prompt_hash=f"public-hash-{idx}",
            status="ok",
            request=f"public request {idx}",
            output_excerpt=f"public output {idx}",
            keep_last=3,
        )
    await store.record_message_ask_turn(
        mode="admin",
        actor_id=999,
        guild_id=456,
        channel_id=999,
        source_message_id=9000,
        bot_message_id=9999,
        parent_bot_message_id=None,
        prompt_hash="other-channel",
        status="ok",
        request="other public request",
        output_excerpt="other public output",
        keep_last=3,
    )

    rows = await store.list_recent_message_ask_memory(guild_id=456, channel_id=789, limit=10)
    chain = await store.list_message_ask_chain(bot_message_id=2003, guild_id=456, channel_id=789, limit=10)

    assert [row[5] for row in rows] == ["public request 1", "public request 2", "public request 3"]
    assert [row[6] for row in rows] == ["public output 1", "public output 2", "public output 3"]
    assert [row[5] for row in chain] == ["public request 1", "public request 2", "public request 3"]
    assert await store.get_message_ask_turn(bot_message_id=2003, guild_id=456, channel_id=789)
    assert await store.list_recent_message_ask_memory(guild_id=456, channel_id=999, limit=10)
    assert await store.list_recent_message_ask_memory(guild_id=111, channel_id=789, limit=10) == []
    assert await store.list_message_ask_chain(bot_message_id=2003, guild_id=111, channel_id=789, limit=10) == []
