"""What should we test next: every candidate, not the first five.

Hoops, 2026-09-24: "the playtest what to test next only shows first 5 events, you should be able to
select any event that is currently marked as needing testing. (Or something else, you can test
something other than events as well)"

Design notes:

- **Candidates come from the catalogs, live.** Anything whose status says `Needs Testing` is on the
  ballot: events, scenarios and clusters. Nothing is hard-coded and nothing is capped, so adding an
  event to the workbook puts it in the poll on the next refresh.
- **Members can nominate anything else.** A nomination is free text, because "test something other than
  events" also means testing a mechanic, a system, a balance pass or a hunch that has no catalog row.
- **Discord limits a select menu to 25 options**, so long families are paged rather than truncated.
  Nothing is ever hidden from the ballot; it is just behind a page button.
- **Weights stay internal** (Hoops: exact values out of public text). Keys identify candidates; the
  stored vote carries the weight for the tally.
"""

from __future__ import annotations

MAX_SELECT_OPTIONS = 25  # Discord's hard limit for one string select

# Family keys, in the order the buttons appear. `nomination` is not catalog-backed.
FAMILIES: tuple[str, ...] = ("event", "scenario", "cluster", "nomination")

FAMILY_SINGULAR: dict[str, str] = {
    "event": "event",
    "scenario": "scenario",
    "cluster": "cluster",
    "nomination": "nomination",
}

FAMILY_PLURAL: dict[str, str] = {
    "event": "Events",
    "scenario": "Scenarios",
    "cluster": "Clusters",
    "nomination": "Nominated",
}

FAMILY_EMOJI: dict[str, str] = {
    "event": "📜",
    "scenario": "🎲",
    "cluster": "🧩",
    "nomination": "✍️",
}

MAX_NOMINATIONS_PER_MEMBER = 3
MAX_NOMINATION_CHARS = 90


def candidate_key(kind: str, ident: str) -> str:
    """Stable identity of a candidate: ``event:7``, ``scenario:2``, ``cluster:11``, ``nomination:slug``."""
    return f"{kind}:{str(ident).strip()}"


def candidate_kind(key: str) -> str:
    return str(key).split(":", 1)[0]


def nomination_slug(label: str) -> str:
    """A readable, stable-ish key fragment for a free-text nomination."""
    cleaned = "".join(ch.lower() if ch.isalnum() else "-" for ch in str(label).strip())
    while "--" in cleaned:
        cleaned = cleaned.replace("--", "-")
    return cleaned.strip("-")[:48] or "nomination"


def family_label(kind: str) -> str:
    return FAMILY_PLURAL.get(kind, kind.title())


def family_emoji(kind: str) -> str:
    return FAMILY_EMOJI.get(kind, "•")


def split_pages(items: list, size: int = MAX_SELECT_OPTIONS) -> list[list]:
    """Page a long candidate list instead of dropping the tail (Discord allows 25 options per select)."""
    if size <= 0:
        raise ValueError("size must be positive")
    return [items[index : index + size] for index in range(0, len(items), size)] or [[]]


FAMILY_DETAIL = {
    "event": "needs testing",
    "scenario": "scenario needs testing",
    "cluster": "cluster needs testing",
    "nomination": "nominated by a member",
}


def candidate_label(kind: str, ident: object = None, name: str = "") -> str:
    """Display label that leads with the catalog ID, matching the render conventions.

    Hoops (2026-09-24): "The IDs should be visible for what you want to set for testing. Right now there
    are only event names. Ids are also important" - a tester quotes the ID in `/playtest report`, so the
    ballot, the ballot text and the vote record all carry it. `Event 003`, `SCN-001`, `Cluster 11`.
    """
    clean_name = " ".join(str(name or "").split())
    raw = str(ident or "").strip()
    if kind == "event":
        prefix = f"Event {int(raw):03d}" if raw.isdigit() else (f"Event {raw}" if raw else "Event")
    elif kind == "scenario":
        if raw.isdigit():
            prefix = f"SCN-{int(raw):03d}"
        elif raw:
            prefix = raw if raw.upper().startswith("SCN") else f"SCN-{raw}"
        else:
            prefix = "Scenario"
    elif kind == "cluster":
        prefix = f"Cluster {raw}" if raw else "Cluster"
    else:
        prefix = "Nomination"
    return f"{prefix}: {clean_name}" if clean_name else prefix


def candidate_detail(kind: str, extra: str = "") -> str:
    """Second line of a select option: what the candidate is, beyond its name."""
    extra = " ".join(str(extra or "").split())
    base = FAMILY_DETAIL.get(kind, "needs testing")
    return f"{extra} - {base}" if extra else base


def clamp_label(text: str, limit: int = 100) -> str:
    """Discord select option labels are capped; trim on a word boundary so nothing reads as cut off."""
    text = " ".join(str(text or "").split())
    if len(text) <= limit:
        return text
    cut = text[:limit]
    if " " in cut:
        cut = cut[: cut.rfind(" ")]
    return cut.rstrip(" ,.;:-") or text[:limit]



# Words that must never reach a public label (same policy as every other public surface).
_FORBIDDEN_IN_LABEL = ("@everyone", "@here", "<@", "<@&")


def is_safe_nomination(label: str) -> bool:
    """Reject nominations that try to smuggle a ping or a mention into the ballot."""
    lowered = str(label or "").lower()
    return not any(token in lowered for token in _FORBIDDEN_IN_LABEL)
