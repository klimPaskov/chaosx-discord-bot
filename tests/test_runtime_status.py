import ast
from pathlib import Path
from types import SimpleNamespace

from chaosx_bot.runtime_status import (
    ProcessInfo,
    collect_process_tree,
    format_hermes_progress,
    format_process_panel,
    record_command_timing,
    timing_lines,
    timing_stats,
)


def _write_status(
    proc_root: Path,
    *,
    pid: int,
    ppid: int,
    name: str,
    state: str = "S (sleeping)",
    rss_kib: int = 1024,
) -> None:
    folder = proc_root / str(pid)
    folder.mkdir(parents=True)
    (folder / "status").write_text(
        f"Name:\t{name}\nState:\t{state}\nPPid:\t{ppid}\nVmRSS:\t{rss_kib} kB\n",
        encoding="utf-8",
    )


def test_collect_process_tree_includes_launcher_bot_and_descendants_only(tmp_path: Path):
    _write_status(tmp_path, pid=100, ppid=1, name="uv")
    _write_status(tmp_path, pid=101, ppid=100, name="python3", state="R (running)")
    _write_status(tmp_path, pid=102, ppid=101, name="hermes")
    _write_status(tmp_path, pid=103, ppid=102, name="node", rss_kib=4096)
    _write_status(tmp_path, pid=200, ppid=1, name="unrelated")

    snapshot = collect_process_tree(root_pid=101, proc_root=tmp_path)

    assert snapshot.launcher == ProcessInfo(100, 1, "uv", "S (sleeping)", 1024)
    assert snapshot.bot == ProcessInfo(101, 100, "python3", "R (running)", 1024)
    assert [(depth, item.pid, item.name) for depth, item in snapshot.descendants] == [
        (1, 102, "hermes"),
        (2, 103, "node"),
    ]
    assert "unrelated" not in repr(snapshot)


def test_process_panel_shows_safe_process_and_active_model_details():
    snapshot = SimpleNamespace(
        launcher=ProcessInfo(100, 1, "uv", "S (sleeping)", 1024),
        bot=ProcessInfo(101, 100, "python3", "R (running)", 2048),
        descendants=((1, ProcessInfo(102, 101, "hermes", "S (sleeping)", 4096)),),
    )
    activity = SimpleNamespace(
        label="admin event-idea",
        pid=102,
        stage="reasoning/tools",
        model="gpt-5.6-luna",
        provider="openai-codex",
        reasoning_effort="xhigh",
        elapsed_seconds=17,
        actor_id=123,
        prompt_hash="abcdef1234567890",
    )

    panel = format_process_panel(snapshot, [activity])

    assert "## ChaosX processes" in panel
    assert "Bot: `python3` PID `101`" in panel
    assert "Launcher: `uv` PID `100`" in panel
    assert "admin event-idea" in panel
    assert "gpt-5.6-luna" in panel
    assert "xhigh" in panel
    assert "17s" in panel
    assert "abcdef123456" in panel
    assert "prompt=" not in panel


def test_progress_card_reports_execution_phase_without_prompt_content():
    activity = SimpleNamespace(
        label="admin ask",
        pid=777,
        stage="reasoning/tools",
        model="gpt-5.6-luna",
        provider="openai-codex",
        reasoning_effort="xhigh",
        elapsed_seconds=9,
        actor_id=123,
        prompt_hash="1234567890abcdef",
    )

    card = format_hermes_progress("admin ask", activity)

    assert "## ChaosX live progress" in card
    assert "`/admin ask`" in card
    assert "reasoning/tools" in card
    assert "PID `777`" in card
    assert "Reasoning effort: `xhigh`" in card
    assert "9s" in card
    assert "chain-of-thought" in card
    assert "owner request" not in card.casefold()


def test_admin_processes_command_and_owner_progress_wiring_exist():
    bot_path = Path(__file__).resolve().parents[1] / "src" / "chaosx_bot" / "bot.py"
    source = bot_path.read_text(encoding="utf-8")
    tree = ast.parse(source)
    functions = {
        node.name: node
        for node in ast.walk(tree)
        if isinstance(node, ast.AsyncFunctionDef)
        and node.name in {"admin_processes", "run_hermes_command"}
    }

    assert set(functions) == {"admin_processes", "run_hermes_command"}
    process_source = ast.get_source_segment(source, functions["admin_processes"]) or ""
    command_source = ast.get_source_segment(source, functions["run_hermes_command"]) or ""
    assert "owner_gate" in process_source
    assert "collect_process_tree" in process_source
    assert "active_hermes_runs" in process_source
    assert "progress_callback" in command_source
    assert "_owner_progress_loop" in command_source


def test_latency_panel_and_persisted_timings_are_wired():
    bot_path = Path(__file__).resolve().parents[1] / "src" / "chaosx_bot" / "bot.py"
    source = bot_path.read_text(encoding="utf-8")
    assert "timing_lines" in source
    assert "format_process_panel(snapshot, activities)" in source
    assert "db_path=bot.settings.db_path" in source


def test_timings_persist_across_calls(tmp_path: Path):
    db = tmp_path / "t.db"
    record_command_timing("ask", 1.5, path="ask-api", db_path=db)
    record_command_timing("ask", 2.5, path="ask-api", db_path=db)
    stats = timing_stats(db_path=db, name="ask")
    assert stats["count"] == 2
    assert stats["p50"] == 2.0
    assert stats["max"] == 2.5


def test_stats_are_filtered_by_name_and_ordered(tmp_path: Path):
    db = tmp_path / "t.db"
    for seconds in (0.2, 0.4, 0.6, 0.8, 1.0, 1.2, 1.4, 1.6, 1.8, 2.0):
        record_command_timing("event", seconds, path="ask-api", db_path=db)
    record_command_timing("ask", 9.0, path="ask-api", db_path=db)
    event = timing_stats(db_path=db, name="event")
    assert event["count"] == 10
    assert event["max"] == 2.0
    assert 1.5 < event["p95"] <= 2.0
    assert timing_stats(db_path=db, name="ask")["max"] == 9.0


def test_missing_db_is_not_an_error(tmp_path: Path):
    assert timing_stats(db_path=tmp_path / "nope.db")["count"] == 0


def test_timing_lines_render_percentiles(tmp_path: Path):
    db = tmp_path / "t.db"
    record_command_timing("ask", 3.0, path="ask-api", db_path=db)
    text = "\n".join(timing_lines(db_path=db, limit=5))
    assert "ask" in text
    assert "p50" in text


def test_record_never_raises_on_a_bad_path(tmp_path: Path):
    # instrumentation must never break a command
    record_command_timing("ask", 1.0, path="ask-api", db_path=tmp_path / "missing-dir" / "x.db")


def test_answer_events_are_recorded_and_summarised(tmp_path):
    from chaosx_bot.runtime_status import answer_quality_lines, record_answer_event

    db = tmp_path / "events.db"
    record_answer_event(db_path=db, path="ask-api", seconds=4.2, denial=True, cache_hit=False, unverified=1)
    record_answer_event(db_path=db, path="ask-api", seconds=3.1, cache_hit=True)
    lines = answer_quality_lines(db_path=db, days=7)
    assert lines and "answers 7d: 2" in lines[0]
    assert "denial 1" in lines[0] and "cache 50%" in lines[0]


def test_answer_event_without_db_is_a_no_op():
    from chaosx_bot.runtime_status import answer_quality_lines, record_answer_event

    record_answer_event(db_path=None)
    assert answer_quality_lines(db_path=None) == []
