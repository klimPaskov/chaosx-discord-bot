"""Server intel: digest facts from the bot's own tables + archive Q&A."""

from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

from chaosx_bot.server_intel import (
    IntelFacts,
    archive_fallback,
    build_archive_prompt,
    build_intel_prompt,
    collect_intel,
    extract_keywords,
    intel_fallback,
    jump_link,
    load_display_names,
    search_archive,
    summarize_topics,
)

UTC = timezone.utc
SCHEMA = """
CREATE TABLE message_ask_memory (
    id INTEGER PRIMARY KEY, created_at TEXT, mode TEXT, actor_id INTEGER, guild_id INTEGER,
    channel_id INTEGER, source_message_id INTEGER, bot_message_id INTEGER,
    parent_bot_message_id INTEGER, prompt_hash TEXT, status TEXT, request TEXT, output_excerpt TEXT
);
CREATE TABLE auto_scan_events (
    id INTEGER PRIMARY KEY, created_at TEXT, action TEXT, reason TEXT, confidence INTEGER,
    actor_id INTEGER, guild_id INTEGER, channel_id INTEGER, source_message_id INTEGER,
    bot_message_id INTEGER, content_excerpt TEXT, response_excerpt TEXT
);
CREATE TABLE message_archive (
    id INTEGER PRIMARY KEY, message_id INTEGER, channel_id INTEGER, author_id INTEGER,
    author_name TEXT, content TEXT, created_at TEXT, visibility TEXT
);
CREATE TABLE users (
    user_id INTEGER PRIMARY KEY, display_name TEXT, first_seen_at TEXT, last_seen_at TEXT,
    is_bot INTEGER, updated_at TEXT
);
CREATE TABLE audit_log (
    id INTEGER PRIMARY KEY, created_at TEXT, actor_id INTEGER, guild_id INTEGER,
    channel_id INTEGER, command TEXT, summary TEXT
);
CREATE TABLE hermes_runs (
    id INTEGER PRIMARY KEY, created_at TEXT, actor_id INTEGER, guild_id INTEGER,
    channel_id INTEGER, prompt_hash TEXT, status TEXT, output_excerpt TEXT
);
CREATE TABLE conversation_summaries (
    channel_id INTEGER, scope TEXT, summary TEXT, last_message_id INTEGER, updated_at TEXT
);
"""


def _now(offset_days: float = 0) -> str:
    return (datetime.now(UTC) - timedelta(days=offset_days)).isoformat()


def _db(tmp_path, *, recent: bool = True) -> Path:
    path = tmp_path / "chaosx-test.db"
    db = sqlite3.connect(path)
    db.executescript(SCHEMA)
    created = _now(1) if recent else _now(30)
    db.execute(
        "INSERT INTO message_ask_memory(created_at, mode, actor_id, channel_id, request, output_excerpt) "
        "VALUES (?, 'public', 7, 100, 'How does the Zombie Outbreak event work?', 'It starts when...')",
        (created,),
    )
    db.execute(
        "INSERT INTO message_ask_memory(created_at, mode, actor_id, channel_id, request, output_excerpt) "
        "VALUES (?, 'public', 8, 100, 'how much does a flight to tokyo cost', 'I can only answer Chaos Redux questions.')",
        (created,),
    )
    db.execute(
        "INSERT INTO auto_scan_events(created_at, action, reason, confidence, actor_id, channel_id, content_excerpt) "
        "VALUES (?, 'shadow', 'no specific domain term', 20, 9, 101, 'anyone play valorant?')",
        (created,),
    )
    db.execute(
        "INSERT INTO auto_scan_events(created_at, action, reason, confidence, actor_id, channel_id, content_excerpt, response_excerpt) "
        "VALUES (?, 'soft_warning', 'spam-ish', 60, 9, 101, 'lol lol lol', 'Take it easy.')",
        (created,),
    )
    db.execute(
        "INSERT INTO auto_scan_events(created_at, action, reason, confidence, actor_id, channel_id) "
        "VALUES (?, 'answer', 'grounded', 90, 7, 100)",
        (created,),
    )
    db.execute(
        "INSERT INTO auto_scan_events(created_at, action, reason, confidence, actor_id, channel_id) "
        "VALUES (?, 'banter', 'greeting', 50, 7, 100)",
        (created,),
    )
    db.execute(
        "INSERT INTO message_archive(message_id, channel_id, author_id, author_name, content, created_at, visibility) "
        "VALUES (555, 100, 7, 'Hoops McCann', 'we decided the zombie FSM uses two phases', ?, 'public')",
        (created,),
    )
    db.execute(
        "INSERT INTO message_archive(message_id, channel_id, author_id, author_name, content, created_at, visibility) "
        "VALUES (556, 101, 8, 'Tester', 'zombie balance feels off in the outbreak event', ?, 'public')",
        (_now(2 if recent else 40),),
    )
    db.execute(
        "INSERT INTO users(user_id, display_name, first_seen_at, last_seen_at) VALUES (7, 'Hoops McCann', ?, ?)",
        (created, created),
    )
    db.execute(
        "INSERT INTO audit_log(created_at, actor_id, guild_id, channel_id, command, summary) "
        "VALUES (?, 7, 99, 100, 'admin announce', 'posted release note')",
        (created,),
    )
    db.execute(
        "INSERT INTO hermes_runs(created_at, actor_id, guild_id, channel_id, prompt_hash, status) "
        "VALUES (?, 7, 99, 100, 'abc', 'failed')",
        (created,),
    )
    db.execute(
        "INSERT INTO conversation_summaries(channel_id, scope, summary, last_message_id, updated_at) "
        "VALUES (100, 'public', 'talked about zombies', 555, ?)",
        (created,),
    )
    db.commit()
    db.close()
    return path


def test_collect_intel_counts_window(tmp_path):
    facts = collect_intel(_db(tmp_path), window_days=7)
    assert len(facts.asks) == 2
    assert len(facts.refused) == 1
    assert facts.refused[0]["request"].startswith("how much does a flight")
    assert len(facts.shadow) == 1
    assert len(facts.warnings) == 1
    assert facts.answers == 1
    assert facts.banter == 1
    assert facts.archived_messages == 2
    assert dict(facts.top_channels)[100] == 1
    assert dict(facts.top_channels)[101] == 1
    assert facts.new_members == 1
    assert facts.known_members == 1  # people the bot has seen; server size comes from Discord
    assert facts.member_count == 0  # no Discord counts injected in a pure collect_intel run
    assert facts.failed_runs == 1
    assert len(facts.admin_actions) == 1
    assert facts.summaries and facts.summaries[0]["channel_id"] == 100


def test_collect_intel_respects_window(tmp_path):
    facts = collect_intel(_db(tmp_path, recent=False), window_days=7)
    assert facts.asks == []
    assert facts.archived_messages == 0
    assert facts.missing  # empty window is reported, never silently blank


def test_collect_intel_tolerates_missing_tables(tmp_path):
    path = tmp_path / "empty.db"
    sqlite3.connect(path).close()
    facts = collect_intel(path)
    assert facts.asks == []
    assert facts.missing


def test_intel_prompt_and_fallback_use_real_requests(tmp_path):
    facts = collect_intel(_db(tmp_path), window_days=7)
    names = {7: "Hoops McCann", 8: "Tester"}
    channels = {100: "general", 101: "vibe-coding"}
    prompt = build_intel_prompt(facts=facts, member_names=names, channel_names=channels)
    assert "Zombie Outbreak" in prompt and "#general" in prompt and "Hoops McCann" in prompt
    assert "no pings" in prompt.lower()
    fallback = intel_fallback(facts=facts, member_names=names, channel_names=channels)
    assert "Server intel" in fallback
    assert "@everyone" not in fallback and "@here" not in fallback
    assert "2 messages" in fallback or "1 messages" in fallback


def test_keywords_drop_stopwords_and_short_words():
    keywords = extract_keywords("what did we decide about the zombie FSM in the outbreak event?")
    assert "zombie" in keywords and "fsm" in keywords and "outbreak" in keywords
    assert "the" not in keywords and "did" not in keywords and "we" not in keywords
    assert extract_keywords("hi??") == []


def test_search_archive_ranks_best_match_and_reports_jump_links(tmp_path):
    path = _db(tmp_path)
    hits = search_archive(path, question="what did we decide about the zombie fsm?")
    assert hits, "expected archive matches"
    assert hits[0]["message_id"] == 555  # contains zombie AND fsm
    assert hits[0]["score"] >= 3  # "decide" (matches "decided"), "zombie" and "fsm"
    channels = {100: "general", 101: "vibe-coding"}
    prompt = build_archive_prompt(
        question="what did we decide about the zombie fsm?", hits=hits, channel_names=channels, guild_id=99
    )
    assert "https://discord.com/channels/99/100/555" in prompt
    assert "Never invent a decision" in prompt
    fallback = archive_fallback(
        question="what did we decide about the zombie fsm?", hits=hits, channel_names=channels, guild_id=99
    )
    assert "Hoops McCann" in fallback and "general" in fallback


def test_search_archive_no_hits_and_no_keywords(tmp_path):
    path = _db(tmp_path)
    assert search_archive(path, question="hi there") == []
    assert archive_fallback(question="what about the navy rework?", hits=[], channel_names={}, guild_id=99)
    assert "No archived messages match" in archive_fallback(
        question="what about the navy rework?", hits=[], channel_names={}, guild_id=99
    )


def test_search_archive_can_exclude_private(tmp_path):
    path = _db(tmp_path)
    db = sqlite3.connect(path)
    db.execute(
        "INSERT INTO message_archive(message_id, channel_id, author_id, author_name, content, created_at, visibility) "
        "VALUES (557, 102, 7, 'Hoops McCann', 'private: zombie fsm note', ?, 'private')",
        (_now(0.5),),
    )
    db.commit()
    db.close()
    public_hits = search_archive(path, question="zombie fsm", include_private=False)
    assert all(hit["visibility"] == "public" for hit in public_hits)
    all_hits = search_archive(path, question="zombie fsm", include_private=True)
    assert any(hit["message_id"] == 557 for hit in all_hits)


def test_summarize_topics_and_jump_link():
    topics = summarize_topics(["zombie outbreak bug", "zombie balance", "playtest schedule"])
    assert topics[0][0] == "zombie" and topics[0][1] == 2
    assert jump_link(99, 100, 555).endswith("/99/100/555")
    assert jump_link(None, 100, 555) == ""


def test_intel_facts_default_window_label():
    facts = IntelFacts(window_days=7)
    assert facts.window_days == 7


def test_load_display_names_reads_users_table(tmp_path):
    names = load_display_names(_db(tmp_path))
    assert names == {7: "Hoops McCann"}


def test_search_archive_needs_more_than_one_keyword_on_multiword_questions(tmp_path):
    path = _db(tmp_path)
    hits = search_archive(path, question="what did we decide about the zombie fsm?")
    # the 2-of-3 keyword message (zombie + fsm) comes first, the single-word one is dropped
    assert [hit["message_id"] for hit in hits][0] == 555
    assert 556 not in [hit["message_id"] for hit in hits]
    assert min(hit["score"] for hit in hits) >= 2


def test_search_archive_falls_back_to_single_matches_when_nothing_clears_the_bar(tmp_path):
    path = _db(tmp_path)
    hits = search_archive(path, question="what did we decide about the navy rework plans?")
    # no message matches two of these words, so the weak matches are still returned
    assert hits, "expected the weak-match fallback to return something"
    assert all(hit["score"] >= 1 for hit in hits)
