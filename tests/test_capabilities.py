"""The bot must know what it can do, so it never claims a capability it actually has."""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from chaosx_bot.capabilities import probe_capabilities


def test_probe_reports_installed_binary(monkeypatch):
    monkeypatch.setattr(
        "shutil.which", lambda name: "/usr/bin/yt-dlp" if name == "yt-dlp" else None
    )
    monkeypatch.setattr("importlib.util.find_spec", lambda name: None)
    caps = probe_capabilities(members_intent=True)
    assert "yt-dlp" in caps["available"]
    assert "Download" in caps["summary"]


def test_probe_reports_missing_video_support(monkeypatch):
    monkeypatch.setattr("shutil.which", lambda name: None)
    monkeypatch.setattr("importlib.util.find_spec", lambda name: None)
    caps = probe_capabilities(members_intent=True)
    assert "yt-dlp" not in caps["available"]
    assert "Download" not in caps["summary"]


def test_summary_never_promises_absent_capabilities(monkeypatch):
    monkeypatch.setattr("shutil.which", lambda name: None)
    monkeypatch.setattr("importlib.util.find_spec", lambda name: None)
    caps = probe_capabilities(members_intent=False)
    assert "member" not in caps["summary"].lower()
    assert "Never state that you lack a capability listed here." in caps["summary"]


def test_members_intent_is_reported(monkeypatch):
    monkeypatch.setattr("shutil.which", lambda name: "/usr/bin/yt-dlp")
    monkeypatch.setattr("importlib.util.find_spec", lambda name: object())
    assert probe_capabilities(members_intent=True)["checks"]["member directory"] is True
    assert probe_capabilities(members_intent=False)["checks"]["member directory"] is False


def test_boundaries_carry_the_capability_rule():
    from chaosx_bot import hermes_bridge

    assert "CAPABILITIES:" in hermes_bridge.SYSTEM_BOUNDARY
    assert "CAPABILITIES:" in hermes_bridge.PUBLIC_ASK_BOUNDARY
    assert "Never claim you cannot access something" in hermes_bridge.PUBLIC_ASK_BOUNDARY


def test_members_intent_and_probe_are_wired():
    from pathlib import Path

    source = (Path(__file__).resolve().parents[1] / "src" / "chaosx_bot" / "bot.py").read_text(
        encoding="utf-8"
    )
    assert "intents.members = settings.members_intent_enabled" in source
    assert "set_capability_summary(" in source
    assert "probe_capabilities(members_intent=" in source


def test_members_intent_has_a_portal_safe_fallback():
    from pathlib import Path

    root = Path(__file__).resolve().parents[1] / "src" / "chaosx_bot"
    bot_src = (root / "bot.py").read_text(encoding="utf-8")
    main_src = (root / "main.py").read_text(encoding="utf-8")
    assert "intents.members = settings.members_intent_enabled" in bot_src
    assert "discord.PrivilegedIntentsRequired" in main_src
    assert "members_intent_enabled" in main_src
