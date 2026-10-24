"""Catch answers that claim a capability the bot actually has, and give the model one chance to correct itself.

Availability is not the same as a tool succeeding. A failed download, a blocked page or a timeout must be
reported as *that failure*, never generalised into "I can't access X" (Hoops 2026-10-18).
"""

from __future__ import annotations

import re

# Only first-person capability claims. "I can't find that event in the catalog" is a legitimate answer about
# content and must not be flagged.
DENIAL_RE = re.compile(
    r"i (?:(?:can(?:'|\u2019|no)t|am unable to|'m unable to)\s+"
    r"(?:access|fetch|see|check|read|open|view|pull|download|verify|browse|search)|"
    r"(?:don(?:'|\u2019)?t|do not)\s+have access to|"
    r"lack access to)\b|"
    r"\bno access to\b",
    re.IGNORECASE,
)


def is_denial(text: str) -> bool:
    """True when the answer claims the bot cannot access something, rather than saying content was absent."""
    return bool(DENIAL_RE.search(text or ""))


def corrective_prompt(capability: str) -> str:
    """The single retry instruction: name the capability, ask again, forbid generalising a tool failure."""
    return (
        f"Your previous answer claimed you cannot access something, but you can: {capability}. "
        "Answer the question again using the tools available to you. If a specific tool failed, name that tool in "
        "one line instead of claiming you lack the ability."
    )
