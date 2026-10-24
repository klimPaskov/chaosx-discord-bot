"""Discord renders model markdown literally when it is malformed; repair it before sending."""

from chaosx_bot.bot import _chunk
from chaosx_bot.formatting import repair_discord_markdown


def test_deep_headings_become_three_hashes():
    assert repair_discord_markdown("#### Step one") == "### Step one"


def test_heading_glued_to_a_paragraph_gets_a_blank_line():
    repaired = repair_discord_markdown("Short version here.\n## Install\n- one")
    assert repaired == "Short version here.\n\n## Install\n- one"


def test_bracket_citation_is_not_left_looking_like_a_broken_link():
    repaired = repair_discord_markdown("Run the installer [HOI4 Live Tools readme].")
    assert "[HOI4 Live Tools readme]" not in repaired
    assert "`HOI4 Live Tools readme`" in repaired


def test_underscore_identifier_is_backticked():
    repaired = repair_discord_markdown("Use hoi4_portrait_source.json for photos.")
    assert "`hoi4_portrait_source.json`" in repaired


def test_real_markdown_link_survives():
    text = "See [the guide](https://example.com) for details."
    assert repair_discord_markdown(text) == text


def test_code_spans_and_fences_are_untouched():
    text = "Run `hoi4_portrait_source.json` then:\n```\nhoi4-live-tools\\app\\x.py\n```\n"
    repaired = repair_discord_markdown(text)
    assert "`hoi4_portrait_source.json`" in repaired
    assert repaired.count("`hoi4_portrait_source.json`") == 1
    assert "```\nhoi4-live-tools\\app\\x.py\n```" in repaired


def test_unclosed_bold_is_closed_on_its_own_line():
    repaired = repair_discord_markdown("- **Download the zip\n- next line")
    assert repaired.count("**") % 2 == 0


def test_three_blank_lines_collapse():
    assert repair_discord_markdown("a\n\n\n\nb") == "a\n\nb"


def test_chunk_reopens_a_fence_it_had_to_close():
    body = "\n".join(f"line {i}" for i in range(400))
    text = f"## Steps\n```txt\n{body}\n```\n"
    parts = _chunk(text, limit=600)
    assert len(parts) > 1
    for part in parts:
        assert part.count("```") % 2 == 0, part[-80:]
    assert parts[1].lstrip().startswith("```txt")
