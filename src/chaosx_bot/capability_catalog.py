"""What the project's tool repositories can do, read from the repositories themselves.

Hoops (2026-10-31): the bot told members that Agentic HOI4 Modding has no 3D model workflow. It does — the
`hoi4-3d-model-pipeline` skill plus `.tools/3d_pipeline` — but that skill lives in a dot-folder deep in the repo,
the curated notes never mentioned it, and BM25 ranked plan documents above it. The model then filled the gap with
a confident "no".

The fix is not a hard-coded sentence. This module walks every tooling root the bot indexes and builds a small
catalog of capabilities from authoritative files: agent skills (`*/skills/<name>/SKILL.md` frontmatter),
specialist subagents, tool packages (`.tools/<name>/README.md`), README feature sections, top-level docs pages
and workflow files. Each entry carries a status that is derived from evidence, not guessed:

- ``documented workflow`` — a skill/agent/doc describes how to do it;
- ``workflow + implementation`` — the skill points at scripts or a tool package that exist in the repo;
- ``implementation`` — a tool package or workflow file without a skill on top;
- ``experimental`` — the source itself says preview/experimental/beta/not done yet;
- ``contributor skill`` — a skill for developing the tool itself, not for modders.

`Knowledge.public_ask_context` prepends the matching entries to every public answer's material, so the answer
model sees "this exists, with this status" whenever a question touches it. The catalog is rebuilt whenever a
root's revision changes, so a newly added skill is known on the next question after the clone updates.
"""

from __future__ import annotations

import re
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

# Display names for the known roots; anything else gets a title-cased name.
REPO_NAMES = {
    "agentic-modding": "Agentic HOI4 Modding",
    "hoi4-agent-tools": "HOI4 Agent Tools",
    "live-tools": "HOI4 Live Tools",
    "mod-setup": "HOI4 Mod Setup",
    "comfyui-portraits": "ComfyUI HOI4 portraits",
}

# Words a member uses to name a repository.
REPO_ALIASES = {
    "agentic-modding": ("agentic", "agent kit", "the kit", "modding kit", "skills"),
    "hoi4-agent-tools": ("agent tools", "agent-tools", "mcp server", "mcp"),
    "live-tools": ("live tools", "live-tools", "live testing", "playtest"),
    "mod-setup": ("mod setup", "setup app", "installer", "mod-setup"),
    "comfyui-portraits": ("comfyui", "portrait", "portraits", "lora"),
}

SKIP_DIRS = {".git", "node_modules", "dist", "build", ".venv", "venv", "__pycache__", "target", "third_party"}
_SKILL_DIR_PREFERENCE = (".claude", ".agents", ".codex", ".cursor", ".qoder", ".opencode", "skills")
_EXPERIMENTAL_RE = re.compile(
    r"\b(?:experimental|beta|prototype|work in progress|wip|not done yet|unstable|early access|in preview|"
    r"preview (?:build|release|runtime|version|feature))\b",
    re.IGNORECASE,
)
# In a heading, "preview" names the status ("Native Rust preview"); in prose it is usually the verb.
_EXPERIMENTAL_HEADING_RE = re.compile(r"\bpreview\b", re.IGNORECASE)
_SKIP_SECTIONS = re.compile(
    r"^(?:license|licen[cs]e and .*|contributing|acknowledg\w*|development|faq|privacy|security|"
    r"changelog|releasing|table of contents|contents|credits|support|code of conduct|"
    r"updating .*|skill size|screenshots.*|learn more|documentation)$",
    re.IGNORECASE,
)
_SKIP_DOC_NAMES = re.compile(
    r"(?:audit|release|adr|handoff|plan|roadmap|changelog|development|contributing|security|fixture|"
    r"baseline|evaluation|comparison|validation|^\d\d_)",
    re.IGNORECASE,
)
_GENERIC_TOKENS = {
    "hoi4", "hearts", "iron", "mod", "mods", "modding", "modder", "agent", "agents", "agentic", "kit", "tool",
    "tools", "workflow", "workflows", "skill", "skills", "ai", "use", "using", "make", "making", "create",
    "creating", "can", "could", "does", "do", "is", "are", "have", "has", "support", "supports", "help", "project",
    "repo", "repository", "the", "a", "an", "and", "or", "for", "with", "of", "to", "in", "on", "it", "its",
    "this", "that", "what", "how", "which", "any", "there", "you", "your", "my", "me", "i", "be", "get", "work",
    "works", "feature", "features", "custom", "new", "own", "exist", "exists", "existing", "game", "chaos",
    "redux", "chaosx", "about", "from", "into", "also", "not", "no", "yes", "if", "when", "where", "who", "why",
    "will", "would", "should", "able", "possible", "something", "thing", "things", "way", "ways", "set", "up",
}
_SYNONYMS = {
    "3d": ("3d", "model", "mesh", "blender", "rig"),
    "model": ("model", "3d", "mesh"),
    "mesh": ("mesh", "3d", "model"),
    "portrait": ("portrait", "leader", "comfyui", "lora"),
    "picture": ("image", "art"),
    "image": ("image", "art"),
    "map": ("map", "province", "state", "terrain", "worldgen"),
    "province": ("province", "map", "state"),
    "test": ("test", "playtest", "live"),
    "testing": ("test", "playtest", "live"),
    "playtest": ("playtest", "live", "test"),
    "focus": ("focus", "tree"),
    "tree": ("tree", "focus"),
    "gui": ("gui", "interface", "window", "scripted"),
    "music": ("music", "audio", "sound"),
    "sound": ("sound", "audio"),
    "animation": ("animation", "animate", "frame", "anim"),
    "animated": ("animation", "animate", "frame", "anim"),
    "frame": ("animation", "frame", "anim"),
    "frames": ("animation", "frame", "anim"),
    "sprite": ("animation", "frame"),
    "animate": ("animation", "animate", "anim"),
    "icon": ("icon", "art", "image"),
    "unit": ("unit", "division", "units"),
    "event": ("event", "events"),
    "decision": ("decision", "mission"),
    "tech": ("technology", "tech", "doctrine"),
    "technology": ("technology", "tech", "doctrine"),
    "install": ("install", "setup", "installer"),
}


@dataclass(frozen=True)
class Capability:
    root: str
    repo: str
    kind: str
    name: str
    summary: str
    status: str
    path: str

    def tokens(self) -> set[str]:
        return _tokens(f"{self.name} {self.kind}")

    def summary_tokens(self) -> set[str]:
        return _tokens(self.summary)


def _stem(word: str) -> str:
    word = word.casefold()
    for suffix in ("ing", "es", "s"):
        if len(word) > len(suffix) + 2 and word.endswith(suffix) and not word.endswith("ss"):
            return word[: -len(suffix)]
    return word


def _tokens(text: str) -> set[str]:
    words = re.findall(r"[a-z0-9]+", (text or "").casefold().replace("3-d", "3d"))
    return {_stem(w) for w in words if w not in _GENERIC_TOKENS and len(w) >= 2}


def _frontmatter(text: str) -> dict[str, str]:
    match = re.match(r"^---\s*\n(.*?)\n---\s*\n", text or "", re.DOTALL)
    if not match:
        return {}
    data: dict[str, str] = {}
    current = ""
    for line in match.group(1).splitlines():
        kv = re.match(r"^([A-Za-z_][\w-]*):\s*(.*)$", line)
        if kv:
            current = kv.group(1).lower()
            data[current] = kv.group(2).strip().strip("\"'")
        elif current and line.startswith((" ", "\t")):
            data[current] = (data[current] + " " + line.strip()).strip()
    for key, value in list(data.items()):
        if value in {">", "|", ">-", "|-"}:
            data[key] = ""
    return data


def _body(text: str) -> str:
    return re.sub(r"^---\s*\n.*?\n---\s*\n", "", text or "", count=1, flags=re.DOTALL)


def _first_paragraph(text: str, limit: int = 260) -> str:
    for block in re.split(r"\n\s*\n", _body(text)):
        block = block.strip()
        if not block or block.startswith(("#", "```", "|", "<", "![", "[!", ">")) or set(block) <= {"-", "=", "*"}:
            continue
        clean = re.sub(r"\[([^\]]+)\]\([^)]+\)", r"\1", block)
        clean = re.sub(r"[`*_]", "", " ".join(clean.split()))
        return _shorten(clean, limit)
    return ""


def _shorten(text: str, limit: int) -> str:
    text = " ".join((text or "").split())
    if len(text) <= limit:
        return text
    cut = text[:limit].rsplit(" ", 1)[0].rstrip(",;:")
    return cut + "…"


def _read(path: Path, limit: int = 200_000) -> str:
    try:
        return path.read_text(encoding="utf-8", errors="replace")[:limit]
    except OSError:
        return ""


def _walk(root: Path, pattern: str) -> Iterable[Path]:
    for path in root.glob(pattern):
        if not (set(path.relative_to(root).parts) & SKIP_DIRS):
            yield path


def _referenced_paths_exist(root: Path, texts: Iterable[str]) -> bool:
    """True when the skill text points at a script or tool package that is really in the repo."""
    for text in texts:
        for ref in re.findall(r"(?:\.tools|scripts|tools)/[\w./-]+", text or ""):
            ref = ref.rstrip("./")
            if ref and (root / ref).exists():
                return True
    return False


def _status_for(text: str, *, implemented: bool, base: str) -> str:
    if _EXPERIMENTAL_RE.search(text[:1500] if text else ""):
        return "experimental"
    if base == "documented workflow" and implemented:
        return "workflow + implementation"
    return base


def _skill_entries(name: str, root: Path, repo: str) -> list[Capability]:
    by_name: dict[str, tuple[int, Path]] = {}
    for path in _walk(root, "**/skills/*/SKILL.md"):
        rel = path.relative_to(root)
        top = rel.parts[0]
        rank = _SKILL_DIR_PREFERENCE.index(top) if top in _SKILL_DIR_PREFERENCE else len(_SKILL_DIR_PREFERENCE)
        skill = path.parent.name
        if skill not in by_name or rank < by_name[skill][0]:
            by_name[skill] = (rank, path)
    slug = root.name.casefold()
    entries: list[Capability] = []
    for skill, (_rank, path) in sorted(by_name.items()):
        text = _read(path)
        meta = _frontmatter(text)
        title = meta.get("name") or skill
        summary = meta.get("description") or _first_paragraph(text)
        refs = [text] + [_read(p, 40_000) for p in sorted(path.parent.glob("references/*.md"))[:8]]
        implemented = _referenced_paths_exist(root, refs)
        contributor = title.casefold().startswith(slug + "-") or (
            slug in {"hoi4-mod-setup", "hoi4-agent-tools", "hoi4-live-tools"} and ".agents" in path.parts
        )
        base = "contributor skill" if contributor else "documented workflow"
        entries.append(
            Capability(
                root=name, repo=repo, kind="agent skill", name=title, summary=_shorten(summary, 300),
                status=_status_for(summary, implemented=implemented, base=base), path=str(path.relative_to(root)),
            )
        )
    return entries


def _agent_entries(name: str, root: Path, repo: str) -> list[Capability]:
    seen: set[str] = set()
    entries: list[Capability] = []
    for path in sorted(_walk(root, ".claude/agents/*.md")) + sorted(_walk(root, ".agents/agents/*.md")):
        text = _read(path, 20_000)
        meta = _frontmatter(text)
        title = meta.get("name") or path.stem
        if title in seen:
            continue
        seen.add(title)
        summary = meta.get("description") or _first_paragraph(text)
        entries.append(
            Capability(
                root=name, repo=repo, kind="specialist subagent", name=title, summary=_shorten(summary, 240),
                status=_status_for(summary, implemented=False, base="documented workflow"),
                path=str(path.relative_to(root)),
            )
        )
    return entries


def _tool_package_entries(name: str, root: Path, repo: str) -> list[Capability]:
    entries: list[Capability] = []
    for path in sorted(_walk(root, ".tools/*/README.md")):
        text = _read(path, 30_000)
        heading = re.search(r"^#\s+(.+)$", text, re.MULTILINE)
        title = heading.group(1).strip() if heading else path.parent.name
        summary = _first_paragraph(text)
        entries.append(
            Capability(
                root=name, repo=repo, kind=f"tool package ({path.parent.name})", name=title, summary=summary,
                status=_status_for(summary, implemented=True, base="implementation"), path=str(path.relative_to(root)),
            )
        )
    return entries


def _readme_entries(name: str, root: Path, repo: str) -> list[Capability]:
    readme = root / "README.md"
    text = _read(readme)
    if not text:
        return []
    entries = [
        Capability(
            root=name, repo=repo, kind="repository", name=repo, summary=_first_paragraph(text, 320),
            status="implementation", path="README.md",
        )
    ]
    sections = re.split(r"^##\s+", text, flags=re.MULTILINE)[1:]
    for section in sections:
        heading, _, body = section.partition("\n")
        heading = heading.strip()
        if not heading or _SKIP_SECTIONS.match(heading):
            continue
        summary = _first_paragraph(body)
        if not summary:
            continue
        experimental = _EXPERIMENTAL_RE.search(heading + " " + summary) or _EXPERIMENTAL_HEADING_RE.search(heading)
        status = "experimental" if experimental else "documented feature"
        entries.append(
            Capability(root=name, repo=repo, kind="README section", name=heading, summary=summary, status=status,
                       path="README.md")
        )
    return entries


def _doc_entries(name: str, root: Path, repo: str) -> list[Capability]:
    entries: list[Capability] = []
    for path in sorted(_walk(root, "docs/*.md")):
        if _SKIP_DOC_NAMES.search(path.stem) or path.stem.casefold() == "readme":
            continue
        text = _read(path, 40_000)
        heading = re.search(r"^#\s+(.+)$", text, re.MULTILINE)
        title = heading.group(1).strip() if heading else path.stem.replace("_", " ").replace("-", " ")
        summary = _first_paragraph(text)
        if not summary:
            continue
        status = "experimental" if _EXPERIMENTAL_RE.search(title + " " + summary) else "documented feature"
        entries.append(
            Capability(root=name, repo=repo, kind="guide", name=title, summary=summary, status=status,
                       path=str(path.relative_to(root)))
        )
    return entries


def _workflow_file_entries(name: str, root: Path, repo: str) -> list[Capability]:
    entries: list[Capability] = []
    for path in sorted(_walk(root, "workflows/*.json")):
        title = path.stem.replace("_", " ")
        entries.append(
            Capability(root=name, repo=repo, kind="ComfyUI workflow", name=title,
                       summary=f"Ready-to-load ComfyUI workflow file `{path.name}`.", status="implementation",
                       path=str(path.relative_to(root)))
        )
    return entries


def build_catalog(roots: Iterable[tuple[str, Path]]) -> list[Capability]:
    catalog: list[Capability] = []
    for name, root in roots:
        root = Path(root)
        if not root.is_dir():
            continue
        repo = REPO_NAMES.get(name, name.replace("-", " ").title())
        for builder in (_readme_entries, _skill_entries, _agent_entries, _tool_package_entries, _doc_entries,
                        _workflow_file_entries):
            try:
                catalog.extend(builder(name, root, repo))
            except Exception as exc:  # one unreadable file must not hide a whole repository
                print(f"[capabilities] {name}: {builder.__name__} failed: {type(exc).__name__}: {exc}", flush=True)
    return catalog


def _expand(tokens: set[str]) -> set[str]:
    out = set(tokens)
    for token in tokens:
        out.update(_stem(word) for word in _SYNONYMS.get(token, ()))
    return out


def repos_named_in(query: str) -> set[str]:
    lowered = f" {(query or '').casefold()} "
    return {root for root, aliases in REPO_ALIASES.items() if any(f"{alias}" in lowered for alias in aliases)}


def match(catalog: list[Capability], query: str, limit: int = 6) -> list[Capability]:
    """Entries relevant to a question, best first. Empty when nothing specific matches."""
    raw = _tokens(query)
    if not raw and not repos_named_in(query):
        return []
    wanted = _expand(raw)
    named = repos_named_in(query)
    scored: list[tuple[float, int, Capability]] = []
    for index, cap in enumerate(catalog):
        name_hits = len(raw & cap.tokens()) * 3 + len((wanted - raw) & cap.tokens()) * 1.5
        summary_hits = len(raw & cap.summary_tokens()) + len((wanted - raw) & cap.summary_tokens()) * 0.5
        score = name_hits + summary_hits
        if cap.root in named:
            score += 1.0
            if cap.kind == "repository":
                score += 2.0
        if cap.status == "contributor skill":
            score -= 2.0
        if cap.kind == "ComfyUI workflow" and not (raw & cap.tokens() - {"image", "text", "source", "batch"}):
            continue
        if cap.kind == "agent skill":
            score += 0.5
        if score >= 3.0:
            scored.append((score, -index, cap))
    scored.sort(key=lambda item: (item[0], item[1]), reverse=True)
    picked: list[Capability] = []
    seen: set[tuple[str, str]] = set()
    for _score, _i, cap in scored:
        # A skill and the subagent that runs it share a name; both are worth showing.
        key = (cap.root, cap.name.casefold(), cap.kind)
        if key in seen:
            continue
        seen.add(key)
        picked.append(cap)
        if len(picked) >= limit:
            break
    return picked


def render(entries: list[Capability], *, freshness: dict[str, str] | None = None) -> str:
    if not entries:
        return ""
    lines = [
        "Capabilities found in the project repositories (read from the current repositories; authoritative for "
        "what exists - status says how far it goes):"
    ]
    for cap in entries:
        when = (freshness or {}).get(cap.root)
        stamp = f", repo updated {when}" if when else ""
        lines.append(f"- {cap.repo} · {cap.kind} `{cap.name}` · {cap.status}{stamp}: {cap.summary}")
    return "\n".join(lines)


def root_revision(root: Path) -> str:
    """Cheap revision id for cache keys: the checked-out commit, or a tree mtime when it is not a git checkout."""
    try:
        out = subprocess.run(
            ["git", "-C", str(root), "log", "-1", "--format=%H %cs"], capture_output=True, text=True, timeout=10
        )
        if out.returncode == 0 and out.stdout.strip():
            return out.stdout.strip()
    except (OSError, subprocess.SubprocessError):
        pass
    try:
        return f"mtime:{max((p.stat().st_mtime for p in root.glob('**/SKILL.md')), default=0):.0f}"
    except OSError:
        return ""
