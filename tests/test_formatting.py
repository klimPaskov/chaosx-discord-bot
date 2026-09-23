"""Messages carry structure: headings, sub-headings, bullets, small print (Hoops 2026-09-23)."""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from chaosx_bot.formatting import (  # noqa: E402
    STRUCTURE_RULE,
    block,
    bullets,
    heading,
    kv,
    numbered,
    section,
    small,
    table,
)
from chaosx_bot.hermes_bridge import (  # noqa: E402
    AUTO_SCAN_BANTER_BOUNDARY,
    AUTO_SCAN_WARNING_BOUNDARY,
    PUBLIC_ASK_BOUNDARY,
    SYSTEM_BOUNDARY,
)


def test_headings_and_sections():
    assert heading("Chaos tiers", "🌪️") == "## 🌪️ Chaos tiers"
    assert heading("Chaos tiers") == "## Chaos tiers"
    assert section("The ladder", "🪜") == "### 🪜 The ladder"
    assert section("Perks") == "### Perks"


def test_lists_labels_and_small_print():
    assert bullets(["a", "b"]) == ["• a", "• b"]
    assert bullets(["a", "  "]) == ["• a"]
    assert numbered(["step one", "step two"]) == ["1. step one", "2. step two"]
    assert kv("Chaos", "690") == "**Chaos:** 690"
    assert kv("Chaos", "690", "💠") == "💠 **Chaos:** 690"
    assert small("footnote") == "-# footnote"
    assert table([("A", "1"), ("B", "2")]) == ["**A:** 1", "**B:** 2"]


def test_block_joins_parts_and_drops_empties():
    text = block(heading("Title", "🌀"), None, section("Sub"), bullets(["one", "two"]), "")
    assert text == "## 🌀 Title\n\n### Sub\n\n• one\n• two"
    assert block() == ""
    assert "\n\n\n" not in text


def test_answer_boundaries_carry_the_structure_rule():
    # every answer path that writes prose must be told to structure it
    for boundary in (SYSTEM_BOUNDARY, PUBLIC_ASK_BOUNDARY):
        assert STRUCTURE_RULE in boundary
    # banter and rule warnings stay one-liners: no headings there
    for boundary in (AUTO_SCAN_BANTER_BOUNDARY, AUTO_SCAN_WARNING_BOUNDARY):
        assert STRUCTURE_RULE not in boundary
