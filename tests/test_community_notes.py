from chaosx_bot.community_notes import (
    format_event_idea_post_body,
    format_event_idea_post_title,
    should_write_approved_note,
    write_event_idea_note,
    write_suggestion_note,
)
from chaosx_bot.config import Settings


def test_community_note_settings_defaults():
    settings = Settings(_env_file=None, discord_token="dummy")
    assert str(settings.obsidian_vault_path).endswith("Chaos Redux Vault")
    assert settings.community_notes_enabled is True
    assert settings.community_event_specs_folder == "Events/Event Specs"
    assert settings.community_suggestions_folder == "Planning/Community Suggestions"
    assert settings.community_event_ideas_channel_id == 1395464994639839356


def test_event_idea_channel_post_format_is_safe_and_readable():
    draft = "# Comet Capital Swap\n\nA token=abc123 comet causes @everyone to swap capitals."
    title = format_event_idea_post_title(
        raw_idea="A strange comet causes countries to swap capitals.",
        draft=draft,
    )
    body = format_event_idea_post_body(
        raw_idea="A strange comet causes countries to swap capitals and pings @here.",
        draft=draft,
        actor_id=123,
    )
    assert title == "Comet Capital Swap"
    assert "**New approved Chaos Redux event idea**" in body
    assert "user:123" in body
    assert "> A strange comet" in body
    assert "＠everyone" in body
    assert "＠here" in body
    assert "abc123" not in body
    assert "[REDACTED]" in body
    assert "vault" not in body.casefold()


def test_write_event_idea_note_uses_spec_structure(tmp_path):
    result = write_event_idea_note(
        vault_path=tmp_path,
        event_specs_folder="Events/Event Specs",
        raw_idea="A strange comet causes countries to swap capitals.",
        draft="# Comet Capital Swap\n\nCountries randomly swap capitals and suffer temporary chaos.",
        actor_id=123,
        guild_id=456,
        channel_id=789,
        event_type="Minor Fire-Once",
        cluster="Space weirdness",
        evo_i="More countries are affected.",
        world_end="If every capital changes at once, world order collapses.",
    )
    assert result is not None
    assert result.path.exists()
    text = result.path.read_text(encoding="utf-8")
    assert "## Catalog entry" in text
    assert "- Event ID: `TBD`" in text
    assert "## Details" in text
    assert "## Evolutions" in text
    assert "## World-end scenario" in text
    assert "Reporter Discord ID: `123`" in text


def test_write_suggestion_note_uses_planning_frontmatter(tmp_path):
    result = write_suggestion_note(
        vault_path=tmp_path,
        suggestions_folder="Planning/Community Suggestions",
        raw_suggestion="Add a chaos newspaper mechanic.",
        draft="# Chaos Newspaper\n\nA recurring newspaper could report escalating event combinations.",
        actor_id=123,
        guild_id=456,
        channel_id=789,
    )
    assert result is not None
    assert result.path.exists()
    text = result.path.read_text(encoding="utf-8")
    assert text.startswith("---\n")
    assert "type: community-suggestion" in text
    assert "# Chaos Newspaper" in text
    assert "## Original suggestion" in text
    assert "## Cleaned suggestion" in text


def test_rejected_notes_are_not_written(tmp_path):
    assert not should_write_approved_note("short", "")
    result = write_suggestion_note(
        vault_path=tmp_path,
        suggestions_folder="Planning/Community Suggestions",
        raw_suggestion="spam spam spam",
        draft="Off-topic / not related to Chaos Redux.",
        actor_id=123,
        guild_id=None,
        channel_id=None,
    )
    assert result is None
