from __future__ import annotations

import csv
import hashlib
import os
import re
import sqlite3
import subprocess
from dataclasses import dataclass
from pathlib import Path
from time import time

TEXT_EXTENSIONS = {
    ".md", ".txt", ".csv", ".yml", ".yaml", ".json", ".toml", ".mod",
    ".gui", ".gfx", ".asset", ".lua",
}
TEXT_ROOTS = {
    ".agents", ".codex", "common", "docs", "events", "history", "interface",
    "localisation", "music", "sound", "paradox_wiki",
}
SKIP_DIRS = {".git", ".venv", "__pycache__", ".pytest_cache", "tmp"}
MAX_FILE_BYTES = 750_000

SCHEMA = """
CREATE TABLE IF NOT EXISTS index_meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS source_docs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    path TEXT NOT NULL UNIQUE,
    source_class TEXT NOT NULL,
    file_type TEXT NOT NULL,
    commit_sha TEXT NOT NULL,
    mtime REAL NOT NULL,
    content_hash TEXT NOT NULL,
    indexed_at REAL NOT NULL,
    content TEXT NOT NULL
);

CREATE VIRTUAL TABLE IF NOT EXISTS source_docs_fts USING fts5(
    path,
    source_class,
    content,
    content='source_docs',
    content_rowid='id'
);

CREATE TRIGGER IF NOT EXISTS source_docs_ai AFTER INSERT ON source_docs BEGIN
    INSERT INTO source_docs_fts(rowid, path, source_class, content) VALUES (new.id, new.path, new.source_class, new.content);
END;
CREATE TRIGGER IF NOT EXISTS source_docs_ad AFTER DELETE ON source_docs BEGIN
    INSERT INTO source_docs_fts(source_docs_fts, rowid, path, source_class, content) VALUES('delete', old.id, old.path, old.source_class, old.content);
END;
CREATE TRIGGER IF NOT EXISTS source_docs_au AFTER UPDATE ON source_docs BEGIN
    INSERT INTO source_docs_fts(source_docs_fts, rowid, path, source_class, content) VALUES('delete', old.id, old.path, old.source_class, old.content);
    INSERT INTO source_docs_fts(rowid, path, source_class, content) VALUES (new.id, new.path, new.source_class, new.content);
END;

CREATE TABLE IF NOT EXISTS catalog_events (
    row_key TEXT PRIMARY KEY,
    event_id TEXT,
    name TEXT NOT NULL,
    details TEXT NOT NULL,
    evo_i TEXT,
    evo_ii TEXT,
    evo_iii TEXT,
    evo_iv TEXT,
    evo_v TEXT,
    world_end TEXT,
    type TEXT,
    cluster_id TEXT,
    member_severity TEXT,
    status TEXT,
    indexed_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS catalog_scenarios (
    row_key TEXT PRIMARY KEY,
    scenario_id TEXT,
    name TEXT NOT NULL,
    details TEXT NOT NULL,
    evo_i TEXT,
    evo_ii TEXT,
    evo_iii TEXT,
    evo_iv TEXT,
    evo_v TEXT,
    world_end TEXT,
    type TEXT,
    cluster_id TEXT,
    member_severity TEXT,
    status TEXT,
    indexed_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS catalog_clusters (
    row_key TEXT PRIMARY KEY,
    cluster_id TEXT,
    name TEXT NOT NULL,
    details TEXT,
    members TEXT,
    type TEXT,
    chaos_level TEXT,
    status TEXT,
    indexed_at REAL NOT NULL
);
"""


@dataclass(frozen=True)
class IndexStats:
    docs: int
    events: int
    scenarios: int
    clusters: int
    commit_sha: str


def repo_commit(repo: Path) -> str:
    try:
        return subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=repo, text=True).strip()
    except Exception:
        return "unknown"


def source_class_for(path: str) -> str:
    if path.startswith("docs/specs/"):
        return "accepted_source_specification"
    if path.startswith("docs/plans/"):
        return "working_plan_or_addendum"
    if path.startswith("localisation/"):
        return "player_facing_localisation"
    if path.startswith("docs/spreadsheets/"):
        return "catalog"
    if path.startswith("docs/events/") or path.startswith("docs/systems/") or path.startswith("CHAOS_REDUX_MECHANICS"):
        return "canonical_documentation"
    if path.startswith("docs/assets/") or path.startswith("docs/super_events/"):
        return "asset_or_super_event_documentation"
    if path.startswith(".agents/") or path == "AGENTS.md":
        return "agent_skill_or_contract"
    if path.startswith(("common/", "events/", "history/", "interface/", "gfx/")):
        return "current_implementation_evidence"
    return "repository_document"


def is_indexable(repo: Path, path: Path) -> bool:
    rel = path.relative_to(repo)
    parts = set(rel.parts)
    if parts & SKIP_DIRS:
        return False
    if path.is_dir():
        return False
    if path.stat().st_size > MAX_FILE_BYTES:
        return False
    if len(rel.parts) == 1:
        return path.suffix.lower() in TEXT_EXTENSIONS or path.name in {"AGENTS.md", "README.md", "CHAOS_REDUX_MECHANICS.md", "CONTRIBUTING.md"}
    if rel.parts[0] not in TEXT_ROOTS:
        return False
    return path.suffix.lower() in TEXT_EXTENSIONS


def iter_indexable_files(repo: Path):
    for root, dirs, files in os.walk(repo):
        root_path = Path(root)
        dirs[:] = [d for d in dirs if d not in SKIP_DIRS]
        for name in files:
            path = root_path / name
            try:
                if is_indexable(repo, path):
                    yield path
            except OSError:
                continue


def read_text(path: Path) -> str | None:
    try:
        return path.read_text(encoding="utf-8-sig", errors="replace")
    except Exception:
        return None


def hash_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8", errors="replace")).hexdigest()


def connect(db_path: Path) -> sqlite3.Connection:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.executescript(SCHEMA)
    return conn


def rebuild_index(repo: Path, db_path: Path) -> IndexStats:
    repo = repo.resolve()
    commit = repo_commit(repo)
    indexed_at = time()
    conn = connect(db_path)
    with conn:
        conn.execute("DELETE FROM source_docs")
        docs = 0
        for path in iter_indexable_files(repo):
            text = read_text(path)
            if not text:
                continue
            rel = path.relative_to(repo).as_posix()
            conn.execute(
                """
                INSERT INTO source_docs(path, source_class, file_type, commit_sha, mtime, content_hash, indexed_at, content)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (rel, source_class_for(rel), path.suffix.lower().lstrip("."), commit, path.stat().st_mtime, hash_text(text), indexed_at, text),
            )
            docs += 1
        events = _load_events(conn, repo, indexed_at)
        scenarios = _load_scenarios(conn, repo, indexed_at)
        clusters = _load_clusters(conn, repo, indexed_at)
        conn.execute("INSERT OR REPLACE INTO index_meta(key, value) VALUES ('commit_sha', ?)", (commit,))
        conn.execute("INSERT OR REPLACE INTO index_meta(key, value) VALUES ('indexed_at', ?)", (str(indexed_at),))
        conn.execute("INSERT OR REPLACE INTO index_meta(key, value) VALUES ('doc_count', ?)", (str(docs),))
    conn.close()
    return IndexStats(docs=docs, events=events, scenarios=scenarios, clusters=clusters, commit_sha=commit)


def _load_events(conn: sqlite3.Connection, repo: Path, indexed_at: float) -> int:
    path = repo / "docs/spreadsheets/chaos_redux_events_catalog.csv"
    conn.execute("DROP TABLE IF EXISTS catalog_events")
    conn.execute("""
    CREATE TABLE catalog_events (
        row_key TEXT PRIMARY KEY,
        event_id TEXT,
        name TEXT NOT NULL,
        details TEXT NOT NULL,
        evo_i TEXT,
        evo_ii TEXT,
        evo_iii TEXT,
        evo_iv TEXT,
        evo_v TEXT,
        world_end TEXT,
        type TEXT,
        cluster_id TEXT,
        member_severity TEXT,
        status TEXT,
        indexed_at REAL NOT NULL
    )
    """)
    if not path.exists():
        return 0
    count = 0
    with path.open(newline="", encoding="utf-8-sig") as fp:
        reader = csv.DictReader(fp)
        for row_number, row in enumerate(reader, 1):
            event_id = (row.get("ID") or "").strip()
            name = (row.get("Event Name") or "").strip()
            if not event_id and not name:
                continue
            row_key = event_id if event_id else f"unassigned:{row_number}:{name}"
            conn.execute(
                """
                INSERT OR REPLACE INTO catalog_events(row_key, event_id, name, details, evo_i, evo_ii, evo_iii, evo_iv, evo_v, world_end, type, cluster_id, member_severity, status, indexed_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    row_key, event_id, name, row.get("Details") or "", row.get("Evo I") or "", row.get("Evo II") or "",
                    row.get("Evo III") or "", row.get("Evo IV") or "", row.get("Evo V") or "",
                    row.get("World-End Scenario") or "", row.get("Type") or "", row.get("Cluster ID") or "",
                    row.get("Member Severity") or "", row.get("Status") or "", indexed_at,
                ),
            )
            count += 1
    return count


def _load_scenarios(conn: sqlite3.Connection, repo: Path, indexed_at: float) -> int:
    """Load triggerable/manual scenarios, not the event catalog copy.

    `chaos_redux_scenarios_catalog.csv` currently mirrors event rows, while the
    player-facing `/scenario` command is meant to answer SCN-* triggerable
    scenario IDs from the scenario system docs.
    """
    path = repo / "docs/systems/triggerable_scenarios.md"
    conn.execute("DROP TABLE IF EXISTS catalog_scenarios")
    conn.execute("""
    CREATE TABLE catalog_scenarios (
        row_key TEXT PRIMARY KEY,
        scenario_id TEXT,
        name TEXT NOT NULL,
        details TEXT NOT NULL,
        evo_i TEXT,
        evo_ii TEXT,
        evo_iii TEXT,
        evo_iv TEXT,
        evo_v TEXT,
        world_end TEXT,
        type TEXT,
        cluster_id TEXT,
        member_severity TEXT,
        status TEXT,
        indexed_at REAL NOT NULL
    )
    """)
    text = read_text(path)
    if not text:
        return 0
    pattern = re.compile(r"^###\s+SCN-(\d{3}):\s+(.+?)\s*$", re.MULTILINE)
    matches = list(pattern.finditer(text))
    count = 0
    for idx, match in enumerate(matches):
        start = match.end()
        end = matches[idx + 1].start() if idx + 1 < len(matches) else len(text)
        scenario_id = str(int(match.group(1)))
        name = match.group(2).strip()
        details = text[start:end].strip()
        status = "Reserved" if "reserved" in details.casefold() or "placeholder" in details.casefold() else "Fully Functional"
        conn.execute(
            """
            INSERT OR REPLACE INTO catalog_scenarios(row_key, scenario_id, name, details, evo_i, evo_ii, evo_iii, evo_iv, evo_v, world_end, type, cluster_id, member_severity, status, indexed_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                scenario_id, scenario_id, name, details, "", "", "", "", "", "",
                "Triggerable Scenario", "", "", status, indexed_at,
            ),
        )
        count += 1
    return count


def _load_clusters(conn: sqlite3.Connection, repo: Path, indexed_at: float) -> int:
    path = repo / "docs/spreadsheets/chaos_redux_clusters_catalog.csv"
    conn.execute("DROP TABLE IF EXISTS catalog_clusters")
    conn.execute("""
    CREATE TABLE catalog_clusters (
        row_key TEXT PRIMARY KEY,
        cluster_id TEXT,
        name TEXT NOT NULL,
        details TEXT,
        members TEXT,
        type TEXT,
        chaos_level TEXT,
        status TEXT,
        indexed_at REAL NOT NULL
    )
    """)
    if not path.exists():
        return 0
    count = 0
    with path.open(newline="", encoding="utf-8-sig") as fp:
        reader = csv.DictReader(fp)
        for row_number, row in enumerate(reader, 1):
            cluster_id = (row.get("Cluster ID") or "").strip()
            name = (row.get("Cluster Name") or "").strip()
            if not cluster_id and not name:
                continue
            row_key = cluster_id if cluster_id else f"planned:{row_number}:{name}"
            conn.execute(
                """
                INSERT OR REPLACE INTO catalog_clusters(row_key, cluster_id, name, details, members, type, chaos_level, status, indexed_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (row_key, cluster_id, name, row.get("Details") or "", row.get("Members (ID)") or "", row.get("Type") or "", row.get("Chaos level") or "", row.get("Status") or "", indexed_at),
            )
            count += 1
    return count
