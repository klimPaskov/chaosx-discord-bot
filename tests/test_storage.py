import json

from chaosx_bot.storage import Store, normalize_question_key


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
    assert by_name["question_answer_tracking"][0] == 1
    assert "/admin qna" in by_name["question_answer_tracking"][2]
    assert by_name["auto_question_answering"][0] == 1
    assert "Zero-token" in by_name["auto_question_answering"][2]
    assert by_name["auto_soft_rule_warnings"][0] == 1
    assert "soft warnings" in by_name["auto_soft_rule_warnings"][2]
    assert by_name["auto_bot_topic_banter"][0] == 1
    assert "deterministic banter" in by_name["auto_bot_topic_banter"][2]
    assert await store.automation_enabled("question_answer_tracking")
    assert await store.automation_enabled("auto_question_answering")
    assert await store.automation_enabled("auto_soft_rule_warnings")
    assert await store.automation_enabled("auto_bot_topic_banter")
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


async def test_auto_scan_event_log_is_scoped_and_listable(tmp_path):
    store = Store(tmp_path / "chaosx-test.db")
    await store.init()
    await store.record_auto_scan_event(
        action="soft_warning",
        reason="mass ping usage",
        confidence=100,
        actor_id=123,
        guild_id=456,
        channel_id=789,
        source_message_id=1000,
        bot_message_id=2000,
        content_excerpt="@everyone hello",
        response_excerpt="soft warning",
    )
    await store.record_auto_scan_event(
        action="answer",
        reason="explicit event id 2",
        confidence=100,
        actor_id=124,
        guild_id=456,
        channel_id=789,
        source_message_id=1001,
        bot_message_id=2001,
        content_excerpt="What is event 2?",
        response_excerpt="Zombie Outbreak",
    )
    await store.record_auto_scan_event(
        action="answer",
        reason="other guild",
        confidence=100,
        actor_id=125,
        guild_id=999,
        channel_id=789,
        source_message_id=1002,
        bot_message_id=2002,
        content_excerpt="What is event 2?",
        response_excerpt="Other guild",
    )
    await store.record_auto_scan_event(
        action="shadow",
        reason="shadow auto-answer: explicit event id 2",
        confidence=100,
        actor_id=126,
        guild_id=456,
        channel_id=789,
        source_message_id=1003,
        bot_message_id=None,
        content_excerpt="What is event 2?",
        response_excerpt="Zombie Outbreak",
    )
    await store.record_auto_scan_event(
        action="banter",
        reason="bot-topic insult/roast",
        confidence=100,
        actor_id=127,
        guild_id=456,
        channel_id=789,
        source_message_id=1004,
        bot_message_id=2004,
        content_excerpt="this chaos bot is so stupid",
        response_excerpt="Who are you calling stupid?",
    )

    rows = await store.list_auto_scan_events(guild_id=456, limit=10)
    warnings = await store.list_auto_scan_events(guild_id=456, limit=10, action="soft_warning")

    assert [row[2] for row in rows] == ["banter", "shadow", "answer", "soft_warning"]
    assert len(warnings) == 1
    assert warnings[0][3] == "mass ping usage"
    assert warnings[0][10] == "@everyone hello"


async def test_question_answer_log_lists_searches_and_counts_popular_questions(tmp_path):
    store = Store(tmp_path / "chaosx-test.db")
    await store.init()
    assert normalize_question_key("<@123> How does Zombie Outbreak work?!") == "how does zombie outbreak work"

    await store.record_question_answer(
        mode="slash",
        actor_id=100,
        guild_id=456,
        channel_id=789,
        source_message_id=None,
        bot_message_id=2000,
        parent_bot_message_id=None,
        question="How does Zombie Outbreak work?",
        answer="Zombie Outbreak starts a spreading crisis.",
        prompt_hash="hash-1",
    )
    await store.record_question_answer(
        mode="mention ask",
        actor_id=101,
        guild_id=456,
        channel_id=789,
        source_message_id=1001,
        bot_message_id=2001,
        parent_bot_message_id=None,
        question="how does zombie outbreak work",
        answer="Zombie Outbreak spreads through infected states.",
        prompt_hash="hash-2",
    )
    await store.record_question_answer(
        mode="slash",
        actor_id=102,
        guild_id=456,
        channel_id=999,
        source_message_id=None,
        bot_message_id=2002,
        parent_bot_message_id=None,
        question="What is Fury?",
        answer="Fury is an event concept when documented.",
        prompt_hash="hash-3",
    )
    await store.record_question_answer(
        mode="slash",
        actor_id=103,
        guild_id=999,
        channel_id=789,
        source_message_id=None,
        bot_message_id=2003,
        parent_bot_message_id=None,
        question="How does Zombie Outbreak work?",
        answer="Other guild answer.",
        prompt_hash="hash-4",
    )

    rows = await store.list_question_answers(guild_id=456, limit=10)
    search_rows = await store.list_question_answers(guild_id=456, limit=10, query="Fury")
    popular = await store.list_popular_question_answers(guild_id=456, limit=10)

    assert [row[6] for row in rows] == ["What is Fury?", "how does zombie outbreak work", "How does Zombie Outbreak work?"]
    assert len(search_rows) == 1
    assert search_rows[0][6] == "What is Fury?"
    assert popular[0][1] == 2
    assert popular[0][3] == "how does zombie outbreak work"
    assert "infected states" in popular[0][4]
