"""The web-evidence gate: search when outside facts are wanted, and never attach an unasked-for picture.

Reported by Hoops 2026-10-12: the bot answered a conversational question from the mod's own files and attached
`chaosx-source-table.png`, a data table scraped off a search result page. The question matched no intent keyword,
so it fell through to the "the local knowledge base looks thin, go look outside" fallback, and the evidence image
came along with the search.

Both halves are pinned here:
  * a conversational/opinion ask does not trigger a fallback search,
  * the evidence image is only attached when the member asked for outside information.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from chaosx_bot.bot import (  # noqa: E402
    WEB_EVIDENCE_MIN_LOCAL_CHARS,
    needs_web_evidence,
    wants_web_evidence_image,
)

RICH = "x" * (WEB_EVIDENCE_MIN_LOCAL_CHARS + 500)
THIN = "x" * 100

# The exact ask that produced the stray image, plus other conversational shapes that must not search.
CASUAL = [
    "Have you tried to vibe code making hoi4 ai much stronger?",
    "what do you think about the new focus tree",
    "do you like the direction we're going",
    "thanks, that's cool",
    "lol nice",
]

# Explicit outside-world intent always searches, and therefore may carry the image.
WEB_INTENT = [
    "what is the latest hoi4 patch?",
    "show me the steam workshop page for this mod",
    "when was the last release?",
    "search the web for chaos redux",
]


def check(label: str, condition: bool, detail: str = "") -> bool:
    print(f"  {'ok  ' if condition else 'FAIL'} {label}{(' — ' + detail) if detail else ''}")
    return condition


def main() -> int:
    ok = True
    print("conversational asks: no search, no image")
    for ask in CASUAL:
        search = needs_web_evidence(ask, reference_context=THIN)
        ok &= check(f"no fallback search: {ask[:44]!r}", search is False)
        ok &= check("no image", wants_web_evidence_image(ask) is False)

    print("explicit outside-world intent: search, image allowed")
    for ask in WEB_INTENT:
        ok &= check(f"searches: {ask[:44]!r}", needs_web_evidence(ask, reference_context=RICH) is True)
        ok &= check("image allowed", wants_web_evidence_image(ask) is True)

    print("ordinary knowledge asks")
    ok &= check(
        "rich local context: no search",
        needs_web_evidence("tell me about the supply system", reference_context=RICH) is False,
    )
    ok &= check(
        "thin local context: still searches (fallback kept)",
        needs_web_evidence("tell me about the supply system", reference_context=THIN) is True,
    )
    ok &= check(
        "but the fallback attaches no picture",
        wants_web_evidence_image("tell me about the supply system") is False,
    )
    ok &= check(
        "catalog lookups never search",
        needs_web_evidence("event 2", reference_context=THIN) is False,
    )
    ok &= check(
        "a catalog-shaped knowledge ask does not search either",
        needs_web_evidence("how does the chaos meter work", reference_context=THIN) is False,
    )

    print()
    if ok:
        print("RESULT: no fallback search for conversation, and no unasked-for evidence image")
    else:
        print("RESULT: FAILED — the gate lets unasked-for web material through")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
