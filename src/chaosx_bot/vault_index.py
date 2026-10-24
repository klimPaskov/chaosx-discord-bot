from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path


@dataclass(frozen=True)
class VaultIndexRefreshResult:
    updated_paths: tuple[Path, ...]
    pruned_links: tuple[str, ...] = ()


SKIP_NOTE_NAMES = {"important tokens.md"}
SKIP_PATH_PARTS = {".git", ".obsidian", ".trash", "__pycache__"}


INDEX_FILE_NAMES = ("index.md", "Events Index.md", "Community Suggestions Index.md")
_PATH_REF_RE = re.compile(r"`([^`]+\.md)`")


def index_files(root: Path) -> list[Path]:
    """Every index file whose lines exist to point at real notes."""
    found: list[Path] = []
    for name in INDEX_FILE_NAMES:
        candidates = [root / name] if name == "index.md" else [p for p in root.rglob(name)]
        for path in candidates:
            if path.is_file() and not any(part in SKIP_PATH_PARTS for part in path.parts):
                found.append(path)
    return sorted(set(found))


def _removable_lines(root: Path, text: str) -> list[str]:
    """Index lines that only point at notes which no longer exist."""
    removable: list[str] = []
    for line in text.splitlines():
        refs = [ref.strip() for ref in _PATH_REF_RE.findall(line)]
        if not refs or not (line.lstrip().startswith(("-", "*", "|")) or "[[" in line):
            continue
        missing = [ref for ref in refs if not ref.startswith(("http://", "https://")) and not (root / ref).exists()]
        if missing and len(missing) == len(refs):
            removable.append(line.strip())
    return removable


def prune_dead_links(root: Path, updated: list[Path]) -> list[str]:
    """Drop index lines that only point at notes which no longer exist.

    Hoops (2026-09-24): "the bot shouldn't use deleted information ever". The index files are generated
    from disk, but a *deletion* used to leave the old listing in place (regeneration only ran on a
    capture), and the bot then read those filenames as if the notes still existed. Only whole link lines
    are removed, and only when every `.md` reference on them is missing, so prose survives untouched.
    """
    removed: list[str] = []
    for path in index_files(root):
        text = _read(path)
        if not text:
            continue
        dropped_here = set(_removable_lines(root, text))
        kept: list[str] = []
        for line in text.splitlines():
            if line.strip() in dropped_here:
                continue
            kept.append(line)
        if dropped_here:
            _write_if_changed(path, "\n".join(kept), updated)
            removed.extend(dropped_here)
    return removed


def vault_indexes_are_stale(
    *,
    vault_path: Path,
    event_specs_folder: str = "Events/Event Specs",
    suggestions_folder: str = "Planning/Community Suggestions",
) -> bool:
    """True when an index lists a note that is gone, or misses a note that exists."""
    root = vault_path.expanduser().resolve()
    if not root.exists():
        return False
    specs = root / event_specs_folder
    if specs.is_dir():
        on_disk = sorted(p.name for p in specs.glob("*.md"))
        # the events index sits beside the folder, not inside it (`Events/Events Index.md`)
        events_index = root / Path(event_specs_folder).parent / "Events Index.md"
        listed = sorted({ref.rsplit("/", 1)[-1] for ref in _PATH_REF_RE.findall(_read(events_index))})
        listed = [name for name in listed if name != "Events Index.md"]
        if listed != on_disk:
            return True
    for path in index_files(root):
        if _removable_lines(root, _read(path)):
            return True
    return False


def _today() -> str:
    return datetime.now(timezone.utc).date().isoformat()


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _read(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return ""


def _write_if_changed(path: Path, content: str, updated: list[Path]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    content = content.rstrip() + "\n"
    if _read(path) != content:
        path.write_text(content, encoding="utf-8")
        updated.append(path)


def _relative(root: Path, path: Path) -> str:
    return path.resolve().relative_to(root.resolve()).as_posix()


def _sort_key(path: Path) -> tuple[int, int, str]:
    match = re.match(r"^(\d+)", path.stem)
    if match:
        return (0, int(match.group(1)), path.stem.casefold())
    if path.stem.startswith("SCN-"):
        scenario = re.match(r"^SCN-(\d+)", path.stem, re.I)
        if scenario:
            return (1, int(scenario.group(1)), path.stem.casefold())
    return (2, 0, path.stem.casefold())


def _markdown_files(folder: Path, *, recursive: bool = False) -> list[Path]:
    if not folder.exists():
        return []
    iterator = folder.rglob("*.md") if recursive else folder.glob("*.md")
    return sorted(
        (
            p for p in iterator
            if p.is_file()
            and p.name not in SKIP_NOTE_NAMES
            and not (set(p.parts) & SKIP_PATH_PARTS)
        ),
        key=_sort_key,
    )


def _title_from_text(text: str, fallback: str) -> str:
    frontmatter = re.match(r"\A---\s*\n(.*?)\n---\s*\n", text, re.S)
    if frontmatter:
        title = re.search(r"(?im)^title:\s*[\"']?(.+?)[\"']?\s*$", frontmatter.group(1))
        if title:
            value = title.group(1).strip().strip('"\'')
            if value:
                return value
    heading = re.search(r"(?m)^#\s+(.+?)\s*$", text)
    if heading:
        value = heading.group(1).strip()
        if value:
            return value
    return fallback


def note_title(path: Path) -> str:
    return _title_from_text(_read(path)[:8000], path.stem)


def _wiki_link(root: Path, path: Path) -> str:
    target = Path(_relative(root, path)).with_suffix("").as_posix()
    title = note_title(path)
    return f"[[{target}|{title}]]"


def _index_line(root: Path, path: Path) -> str:
    return f"- {_wiki_link(root, path)} — `{_relative(root, path)}`"


def _replace_heading_section(text: str, heading: str, body: str) -> str:
    pattern = re.compile(rf"(^## {re.escape(heading)}\s*\n)(.*?)(?=^##\s+|\Z)", re.S | re.M)
    replacement = f"## {heading}\n\n{body.rstrip()}\n\n"
    if pattern.search(text):
        return pattern.sub(replacement, text, count=1).rstrip() + "\n"
    return text.rstrip() + "\n\n" + replacement


def _replace_auto_block(text: str, *, marker: str, heading: str, body: str) -> str:
    start = f"<!-- CHAOSX:{marker}:START -->"
    end = f"<!-- CHAOSX:{marker}:END -->"
    block = f"{start}\n{body.rstrip()}\n{end}"
    pattern = re.compile(rf"{re.escape(start)}\n.*?\n{re.escape(end)}", re.S)
    if pattern.search(text):
        return pattern.sub(block, text, count=1).rstrip() + "\n"
    return text.rstrip() + f"\n\n## {heading}\n\n{block}\n"


def _event_map_lines(root: Path, event_specs_folder: str) -> list[str]:
    lines = [
        "- [[Events/Event Catalog Index|Event Catalog Index]] — `Events/Event Catalog Index.md`",
        "- [[Events/Event Idea Registry|Event Idea Registry]] — `Events/Event Idea Registry.md`",
    ]
    lines.extend(_index_line(root, path) for path in _markdown_files(root / event_specs_folder))
    for rel in ("Events/Events Index.md", "Events/Events.md", "Events/Random Events Mod Ideas.md"):
        path = root / rel
        if path.exists():
            lines.append(_index_line(root, path))
    return lines


def _planning_map_lines(root: Path, suggestions_folder: str) -> list[str]:
    planning_root = root / "Planning"
    paths = [p for p in _markdown_files(planning_root, recursive=True) if p.name != ".DS_Store"]
    if not paths:
        return ["- No planning notes found."]
    return [_index_line(root, path) for path in sorted(paths, key=lambda p: _relative(root, p).casefold())]


def _refresh_root_index(root: Path, event_specs_folder: str, suggestions_folder: str, updated: list[Path]) -> None:
    path = root / "index.md"
    text = _read(path) or "# Chaos Redux Wiki Index\n\n> Content catalog for the standalone Chaos Redux vault.\n"
    total_pages = len(_markdown_files(root, recursive=True))
    updated_line = f"> Last updated: {_today()} | Total pages: {total_pages}"
    if re.search(r"(?m)^> Last updated: .*$", text):
        text = re.sub(r"(?m)^> Last updated: .*$", updated_line, text, count=1)
    else:
        text = text.replace("# Chaos Redux Wiki Index\n", f"# Chaos Redux Wiki Index\n\n{updated_line}\n", 1)
    text = _replace_heading_section(text, "Events", "\n".join(_event_map_lines(root, event_specs_folder)))
    text = _replace_heading_section(text, "Planning", "\n".join(_planning_map_lines(root, suggestions_folder)))
    _write_if_changed(path, text, updated)


def _refresh_events_index(root: Path, event_specs_folder: str, updated: list[Path]) -> None:
    path = root / "Events/Events Index.md"
    text = _read(path) or "---\ntitle: \"Events Index\"\ntype: index\n---\n\n# Events Index\n"
    lines = [_index_line(root, event_path) for event_path in _markdown_files(root / event_specs_folder)]
    if not lines:
        lines = ["- No event specs found."]
    text = _replace_auto_block(
        text,
        marker="EVENT_SPECS",
        heading="Event specs",
        body="\n".join(lines),
    )
    _write_if_changed(path, text, updated)


def _refresh_community_suggestions_index(root: Path, suggestions_folder: str, updated: list[Path]) -> None:
    folder = root / suggestions_folder
    path = folder / "Community Suggestions Index.md"
    text = _read(path) or f"""---
title: "Community Suggestions"
created: {_today()}
updated: {_today()}
type: index
tags: [chaos-redux, community, suggestions, planning]
sources: [discord]
confidence: medium
---

# Community Suggestions

Quiet holding folder for ChaosX-approved community suggestions that need human review before becoming implementation work or public commitments.
"""
    today = _today()
    if re.search(r"(?m)^updated:\s*.*$", text):
        text = re.sub(r"(?m)^updated:\s*.*$", f"updated: {today}", text, count=1)
    notes = [p for p in _markdown_files(folder) if p.name != "Community Suggestions Index.md"]
    lines = [_index_line(root, p) for p in notes] or ["- No community suggestions captured yet."]
    text = _replace_auto_block(
        text,
        marker="COMMUNITY_SUGGESTIONS",
        heading="Suggestion notes",
        body="\n".join(lines),
    )
    _write_if_changed(path, text, updated)


def _append_log(
    root: Path,
    *,
    reason: str,
    changed_path: Path | None,
    updated: list[Path],
    pruned: list[str] | None = None,
) -> None:
    path = root / "log.md"
    reason = re.sub(r"\s+", " ", reason.strip())[:240] or "Vault indexes refreshed."
    changed = f"`{_relative(root, changed_path)}`" if changed_path and changed_path.exists() else "not specified"
    entry = (
        f"\n\n## [{_today()}] update | ChaosX vault index refresh\n"
        f"- Refreshed vault index/reference notes.\n"
        f"- Reason: {reason}\n"
        f"- Changed note: {changed}\n"
    )
    if pruned:
        entry += f"- Removed {len(pruned)} stale link line(s) pointing at notes that no longer exist.\n"
        for line in pruned[:12]:
            cleaned = re.sub(r"\s+", " ", line)[:200]
            entry += f"  - {cleaned}\n"
    entry += f"- Timestamp: {_now_iso()}\n"
    text = _read(path).rstrip() + entry
    _write_if_changed(path, text, updated)


def refresh_vault_indexes(
    *,
    vault_path: Path,
    event_specs_folder: str = "Events/Event Specs",
    suggestions_folder: str = "Planning/Community Suggestions",
    reason: str = "Vault content updated.",
    changed_path: Path | None = None,
) -> VaultIndexRefreshResult:
    """Refresh Chaos Redux vault index/reference files after an automated note write."""
    root = vault_path.expanduser().resolve()
    if not root.exists():
        raise FileNotFoundError(f"vault path does not exist: {root}")
    updated: list[Path] = []
    _refresh_root_index(root, event_specs_folder, suggestions_folder, updated)
    _refresh_events_index(root, event_specs_folder, updated)
    _refresh_community_suggestions_index(root, suggestions_folder, updated)
    pruned = prune_dead_links(root, updated)
    _append_log(
        root,
        reason=reason,
        changed_path=changed_path,
        updated=updated,
        pruned=pruned,
    )
    return VaultIndexRefreshResult(updated_paths=tuple(updated), pruned_links=tuple(pruned))
