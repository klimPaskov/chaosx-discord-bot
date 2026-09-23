"""The public digest never carries the online count (Hoops: it goes stale in minutes)."""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from chaosx_bot.routine_posts import server_facts_line, strip_online_count  # noqa: E402


def test_server_facts_line_can_omit_online():
    server = {"members": 69, "online": 16, "members_source": "discord", "qa_saved": 8}
    assert server_facts_line(server) == "8 questions asked in the server, 69 members in the server (16 online right now)"
    assert server_facts_line(server, include_online=False) == "8 questions asked in the server, 69 members in the server"
    # the member count is never dropped, only the volatile online figure
    assert "69 members" in server_facts_line(server, include_online=False)


def test_strip_online_count_removes_every_shape():
    cases = {
        "We are 69 members, 16 online right now.": "We are 69 members",
        "69 members (16 online) and 8 questions came in.": "69 members and 8 questions came in.",
        "The server sits at 69 members - 16 online right now - quiet week otherwise":
            "The server sits at 69 members - quiet week otherwise",
        "69 members, 16 online.": "69 members",
    }
    for text, expected in cases.items():
        assert strip_online_count(text) == expected


def test_strip_online_count_leaves_clean_text_alone():
    for text in ("69 members and 8 questions came in.", "No online figure here at all.", ""):
        assert strip_online_count(text) == text
