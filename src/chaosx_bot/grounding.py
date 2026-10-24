"""Answers may name files; those names must exist in the index.

Naming a file is the strongest claim a Chaos Redux answer makes and the easiest one to fake. Only backticked
strings that look like repo paths count as citations -- bare prose mentions ("the event file") are not, because
there is nothing to check against.
"""

from __future__ import annotations

import re

_PATH_RE = re.compile(r"`([A-Za-z0-9_][A-Za-z0-9_./-]*\.(?:txt|md|yml|yaml|json|csv|mod))`")

# "I have nothing on that" / "the knowledge base has no mention". First person or the corpus as subject.
_REFUSAL_RE = re.compile(
    r"(?:knowledge base|context|documents?|corpus)[^.\n]{0,40}"
    r"(?:has|have|contains?)\s+(?:no|nothing)"
    r"|i\s+(?:do not|don't|did not|didn't)\s+have\s+(?:any\s+)?(?:solid\s+)?(?:details|information|anything)"
    r"|nothing\s+(?:in|on)\s+(?:the\s+)?(?:knowledge base|context|documents?|files?)",
    re.IGNORECASE,
)


def citations(answer: str) -> list[str]:
    """Backticked file paths named by the answer, in order, deduplicated."""
    seen: list[str] = []
    for match in _PATH_RE.finditer(answer or ""):
        path = match.group(1).lstrip("./")
        if path not in seen:
            seen.append(path)
    return seen


def unverified_citations(answer: str, known_paths: set[str]) -> list[str]:
    """Citations whose path is not in the index. Substring match: answers often give a suffix of the path."""
    known = [p.lstrip("./") for p in known_paths]
    missing: list[str] = []
    for cited in citations(answer):
        if not any(k == cited or k.endswith("/" + cited) for k in known):
            missing.append(cited)
    return missing


def is_ungrounded_refusal(answer: str, context: str, *, min_context_chars: int = 600) -> bool:
    """True when the answer refuses while retrieval clearly supplied material on the subject.

    A thin context (under ``min_context_chars``) means the refusal may be honest, so it is left alone.
    """
    if len(context or "") < min_context_chars:
        return False
    return bool(_REFUSAL_RE.search(answer or ""))
