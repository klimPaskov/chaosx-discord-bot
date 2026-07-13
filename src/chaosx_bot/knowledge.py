from __future__ import annotations

import re
import sqlite3
import subprocess
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from .indexer import connect, iter_indexable_files, rebuild_index, repo_commit

MAX_EXCERPT_CHARS = 1400
PRIVATE_SOURCE_CLASSES = {"accepted_source_specification", "planning_document"}
MAX_CONTEXT_CHARS = 2400
INDEX_FRESHNESS_CHECK_SECONDS = 0.0


@dataclass(frozen=True)
class Knowledge:
    repo: Path
    db_path: Path
    _last_freshness_check: float = field(default=0.0, init=False, repr=False, compare=False)

    def ensure_index(self) -> None:
        now = datetime.now(tz=timezone.utc).timestamp()
        if now - self._last_freshness_check < INDEX_FRESHNESS_CHECK_SECONDS:
            return
        conn = connect(self.db_path)
        try:
            meta = dict(conn.execute("SELECT key, value FROM index_meta").fetchall())
            count = conn.execute("SELECT COUNT(*) FROM source_docs").fetchone()[0]
            scenarios = conn.execute("SELECT COUNT(*) FROM catalog_scenarios").fetchone()[0]
        finally:
            conn.close()
        indexed_at = _float_meta(meta.get("indexed_at"))
        current_commit = repo_commit(self.repo)
        latest_source_mtime = _latest_indexable_mtime(self.repo)
        needs_rebuild = (
            count == 0
            or scenarios == 0
            or meta.get("commit_sha") != current_commit
            or latest_source_mtime > indexed_at
        )
        if needs_rebuild:
            rebuild_index(self.repo, self.db_path)
        object.__setattr__(self, "_last_freshness_check", now)

    def status(self) -> str:
        self.ensure_index()
        conn = connect(self.db_path)
        try:
            meta = dict(conn.execute("SELECT key, value FROM index_meta").fetchall())
            docs = conn.execute("SELECT COUNT(*) FROM source_docs").fetchone()[0]
            events = conn.execute("SELECT COUNT(*) FROM catalog_events").fetchone()[0]
            scenarios = conn.execute("SELECT COUNT(*) FROM catalog_scenarios").fetchone()[0]
            clusters = conn.execute("SELECT COUNT(*) FROM catalog_clusters").fetchone()[0]
        finally:
            conn.close()
        return (
            "## ChaosX status\n"
            f"- Known events: `{events}`\n"
            f"- Known scenarios: `{scenarios}`\n"
            f"- Known clusters: `{clusters}`\n"
            f"- Index health: `ready`\n"
            f"- Refresh mode: `auto when repo/catalog files change`\n"
        )

    def search(self, query: str, scope: str = "all", limit: int = 5, show_evidence: bool = False) -> str:
        self.ensure_index()
        safe_query = _fts_query(query)
        conn = connect(self.db_path)
        try:
            params: list[object] = [safe_query]
            where = "source_docs_fts MATCH ?"
            if not show_evidence:
                where += " AND d.source_class NOT IN ({})".format(",".join("?" for _ in PRIVATE_SOURCE_CLASSES))
                params.extend(sorted(PRIVATE_SOURCE_CLASSES))
            if scope != "all":
                where += " AND d.source_class LIKE ?"
                params.append(f"%{scope}%")
            rows = conn.execute(
                f"""
                SELECT d.path, d.source_class, d.commit_sha, d.indexed_at,
                       snippet(source_docs_fts, 2, '**', '**', ' … ', 18) AS snip,
                       bm25(source_docs_fts) AS rank
                FROM source_docs_fts
                JOIN source_docs d ON d.id = source_docs_fts.rowid
                WHERE {where}
                ORDER BY rank
                LIMIT ?
                """,
                (*params, limit),
            ).fetchall()
        finally:
            conn.close()
        if not rows:
            return f"No indexed results for `{query}`."
        lines = [f"## Search results for `{query}`"]
        for i, (path, source_class, commit, indexed_at, snip, _rank) in enumerate(rows, 1):
            item = f"{i}. `{path}`\n   {snip}"
            if show_evidence:
                item += f"\n   Evidence: `{source_class}` · commit `{commit[:12]}` · synced `{_fmt_ts(indexed_at)}`"
            lines.append(item)
        return "\n".join(lines)

    def public_ask_context(self, query: str, limit: int = 6, include_sources: bool = False) -> str:
        """Return internal retrieval snippets for public /ask.

        Broad ask may use specs and planning docs for accuracy, but user-facing
        answers should not expose source paths/classes/commits by default. When
        the user explicitly asks for files/sources, include repo-relative paths.
        """
        self.ensure_index()
        safe_query = _fts_query(query)
        conn = connect(self.db_path)
        try:
            rows = conn.execute(
                """
                SELECT d.path, d.source_class,
                       snippet(source_docs_fts, 2, '', '', ' … ', 35) AS snip,
                       bm25(source_docs_fts) AS rank
                FROM source_docs_fts
                JOIN source_docs d ON d.id = source_docs_fts.rowid
                WHERE source_docs_fts MATCH ?
                ORDER BY rank
                LIMIT ?
                """,
                (safe_query, limit),
            ).fetchall()
        finally:
            conn.close()
        snippets: list[str] = []
        total = 0
        for i, (path, source_class, snip, _rank) in enumerate(rows, 1):
            clean = _clean_snippet(snip)
            if not clean:
                continue
            item = f"{i}. {clean}"
            if include_sources:
                item = f"{item}\n   Source: {path} ({source_class})"
            if total + len(item) > MAX_CONTEXT_CHARS:
                break
            snippets.append(item)
            total += len(item)
        return "\n".join(snippets)

    def event(self, event: str, view: str = "overview", show_evidence: bool = False) -> str:
        self.ensure_index()
        row = self._find_event(event)
        if not row:
            return self.search(event, scope="all", limit=5, show_evidence=show_evidence) + "\n\nNo exact event match; showing search results instead."
        keys = ["row_key", "event_id", "name", "details", "evo_i", "evo_ii", "evo_iii", "evo_iv", "evo_v", "world_end", "type", "cluster_id", "member_severity", "status", "indexed_at"]
        data = dict(zip(keys, row))
        event_label = f"Event {data['event_id']}: {data['name']}" if data["event_id"] else f"Unassigned event idea: {data['name']}"
        lines = [
            f"## {event_label}",
            f"- Type: `{data['type'] or 'unknown'}`",
            f"- Status: `{data['status'] or 'unknown'}`",
            f"- Cluster: `{data['cluster_id'] or 'none'}`",
            f"- Member severity: `{data['member_severity'] or 'none'}`",
            "",
            data["details"][:MAX_EXCERPT_CHARS] or "No details available.",
        ]
        evos = [("Evo I", data["evo_i"]), ("Evo II", data["evo_ii"]), ("Evo III", data["evo_iii"]), ("Evo IV", data["evo_iv"]), ("Evo V", data["evo_v"])]
        shown_evos = [f"- **{label}:** {text[:400]}" for label, text in evos if text]
        if shown_evos and view in {"overview", "design", "history"}:
            lines += ["", "### Evolution tracks", *shown_evos]
        if data["world_end"]:
            lines += ["", "### World-end relationship", data["world_end"][:700]]
        if show_evidence:
            paths = self._entity_paths(data["event_id"], data["name"])
            if paths:
                lines += ["", "### Private source paths", *[f"- `{p}` — {sc}" for p, sc in paths[:12]]]
            lines += ["", self._footer("catalog", "docs/spreadsheets/chaos_redux_events_catalog.csv")]
        return "\n".join(lines)

    def scenario(self, scenario: str, view: str = "overview", show_evidence: bool = False) -> str:
        self.ensure_index()
        row = self._find_scenario(scenario)
        if not row:
            return self.search(scenario, scope="all", limit=5, show_evidence=show_evidence) + "\n\nNo exact scenario match; showing public search results instead."
        keys = ["row_key", "scenario_id", "name", "details", "evo_i", "evo_ii", "evo_iii", "evo_iv", "evo_v", "world_end", "type", "cluster_id", "member_severity", "status", "indexed_at"]
        data = dict(zip(keys, row))
        scenario_label = f"Scenario {data['scenario_id']}: {data['name']}" if data["scenario_id"] else f"Unassigned scenario idea: {data['name']}"
        lines = [
            f"## {scenario_label}",
            f"- Type: `{data['type'] or 'unknown'}`",
            f"- Status: `{data['status'] or 'unknown'}`",
            f"- Cluster: `{data['cluster_id'] or 'none'}`",
            f"- Member severity: `{data['member_severity'] or 'none'}`",
            "",
            data["details"][:MAX_EXCERPT_CHARS] or "No details available.",
        ]
        evos = [("Evo I", data["evo_i"]), ("Evo II", data["evo_ii"]), ("Evo III", data["evo_iii"]), ("Evo IV", data["evo_iv"]), ("Evo V", data["evo_v"])]
        shown_evos = [f"- **{label}:** {text[:400]}" for label, text in evos if text]
        if shown_evos and view in {"overview", "design", "history"}:
            lines += ["", "### Evolution tracks", *shown_evos]
        if data["world_end"]:
            lines += ["", "### World-end relationship", data["world_end"][:700]]
        if show_evidence:
            lines += ["", self._footer("catalog", "docs/spreadsheets/chaos_redux_scenarios_catalog.csv")]
        return "\n".join(lines)

    def cluster(self, cluster: str, show_evidence: bool = False) -> str:
        self.ensure_index()
        conn = connect(self.db_path)
        try:
            if cluster.strip().isdigit():
                row = conn.execute("SELECT * FROM catalog_clusters WHERE cluster_id = ?", (cluster.strip(),)).fetchone()
            else:
                row = conn.execute("SELECT * FROM catalog_clusters WHERE lower(name) LIKE ? ORDER BY cluster_id LIMIT 1", (f"%{cluster.lower()}%",)).fetchone()
        finally:
            conn.close()
        if not row:
            return f"No registered cluster match for `{cluster}`. Planned clusters without IDs remain unassigned."
        row_key, cluster_id, name, details, members, type_, chaos_level, status, indexed_at = row
        label = f"Cluster {cluster_id}: {name}" if cluster_id else f"Planned cluster idea: {name}"
        text = (
            f"## {label}\n"
            f"- Type: `{type_ or 'unknown'}`\n"
            f"- Chaos level: `{chaos_level or 'unknown'}`\n"
            f"- Members: `{members or 'none'}`\n"
            f"- Status: `{status or 'unknown'}`\n\n"
            f"{details or 'No details available.'}"
        )
        if show_evidence:
            text += f"\n\n{self._footer('catalog', 'docs/spreadsheets/chaos_redux_clusters_catalog.csv')}"
        return text

    def source(self, query: str, show_evidence: bool = False) -> str:
        self.ensure_index()
        paths = self._entity_paths(_extract_number(query) or query, query)
        if not paths:
            return self.search(query, limit=5, show_evidence=show_evidence)
        lines = [f"## Source map for `{query}`"]
        for path, source_class in paths[:20]:
            lines.append(f"- `{path}` — {source_class}")
        if show_evidence:
            lines.append("\nPrivate source precedence: accepted specs for intended design; implementation files for current behavior; localisation for player-facing text; catalogs for status/overview; plans as queued/dispositioned work.")
        return "\n".join(lines)

    def file_excerpt(self, rel_path: str, lines: str = "") -> str:
        path = (self.repo / rel_path).resolve()
        if not str(path).startswith(str(self.repo.resolve())):
            return "Blocked: path escapes repository root."
        if not path.exists() or not path.is_file():
            return f"File not found: `{rel_path}`"
        text = path.read_text(encoding="utf-8-sig", errors="replace").splitlines()
        start, end = _parse_lines(lines, len(text))
        excerpt = "\n".join(f"{i+1}|{_redact(line)}" for i, line in enumerate(text[start:end], start))
        if len(excerpt) > 1800:
            excerpt = excerpt[:1800] + "\n… truncated"
        return f"## `{rel_path}` lines {start+1}-{end}\n```text\n{excerpt}\n```"

    def help(self, topic: str = "all") -> str:
        return (
            "## ChaosX help\n"
            "Community commands: `/ask`, `/event`, `/scenario`, `/cluster`, `/mechanic`, `/search`, `/status`, `/testing`, `/work suggestion`, `/work event-idea`, `/playtest queue`.\n"
            "General questions are rate-limited; lookup commands are usually faster for event, scenario, mechanic, and testing info."
        )

    def _find_event(self, event: str):
        value = event.strip()
        conn = connect(self.db_path)
        try:
            number = _extract_number(value)
            if number:
                row = conn.execute("SELECT * FROM catalog_events WHERE event_id = ?", (str(int(number)),)).fetchone()
                if row:
                    return row
            return conn.execute("SELECT * FROM catalog_events WHERE lower(name) LIKE ? ORDER BY CAST(event_id AS INTEGER) LIMIT 1", (f"%{value.lower()}%",)).fetchone()
        finally:
            conn.close()

    def _find_scenario(self, scenario: str):
        value = scenario.strip()
        conn = connect(self.db_path)
        try:
            number = _extract_number(value)
            if number:
                row = conn.execute("SELECT * FROM catalog_scenarios WHERE scenario_id = ?", (str(int(number)),)).fetchone()
                if row:
                    return row
            return conn.execute("SELECT * FROM catalog_scenarios WHERE lower(name) LIKE ? ORDER BY CAST(scenario_id AS INTEGER) LIMIT 1", (f"%{value.lower()}%",)).fetchone()
        finally:
            conn.close()

    def _entity_paths(self, entity_id: str, name: str) -> list[tuple[str, str]]:
        terms = []
        if entity_id and str(entity_id).isdigit():
            n = int(entity_id)
            terms += [f"/{n:03d}_", f"/{n:03d}", f"{n:03d}_", f"Event {n}"]
        if name:
            slug = re.sub(r"[^a-z0-9]+", "_", name.lower()).strip("_")
            terms += [name, slug]
        conn = connect(self.db_path)
        try:
            found: list[tuple[str, str]] = []
            for term in terms:
                rows = conn.execute("SELECT path, source_class FROM source_docs WHERE lower(path) LIKE ? OR lower(content) LIKE ? LIMIT 8", (f"%{term.lower()}%", f"%{term.lower()}%"))
                for row in rows.fetchall():
                    if row not in found:
                        found.append(row)
            return found
        finally:
            conn.close()

    def _footer(self, source_class: str, path: str) -> str:
        conn = connect(self.db_path)
        try:
            meta = dict(conn.execute("SELECT key, value FROM index_meta").fetchall())
        finally:
            conn.close()
        return f"Private source detail: {source_class} · `{path}` · commit `{meta.get('commit_sha', 'unknown')[:12]}` · synced `{_fmt_ts(meta.get('indexed_at'))}` · confidence high"


def _git(repo: Path, args: list[str]) -> str:
    try:
        return subprocess.check_output(["git", *args], cwd=repo, text=True, stderr=subprocess.DEVNULL).strip()
    except Exception:
        return ""


def _fmt_ts(value) -> str:
    try:
        return datetime.fromtimestamp(float(value), tz=timezone.utc).isoformat(timespec="seconds")
    except Exception:
        return "unknown"


def _float_meta(value) -> float:
    try:
        return float(value)
    except Exception:
        return 0.0


def _latest_indexable_mtime(repo: Path) -> float:
    latest = 0.0
    for path in iter_indexable_files(repo):
        try:
            latest = max(latest, path.stat().st_mtime)
        except OSError:
            continue
    return latest


def _extract_number(value: str) -> str | None:
    m = re.search(r"\b(?:event\s*)?(\d{1,3})\b", value, re.I)
    return m.group(1) if m else None


def _fts_query(query: str) -> str:
    tokens = re.findall(r"[A-Za-z0-9_\-]+", query)
    return " OR ".join(tokens[:8]) or '""'


def _clean_snippet(value: str) -> str:
    value = re.sub(r"\s+", " ", value or "").strip()
    value = re.sub(r"(?i)docs/[A-Za-z0-9_./-]+", "", value)
    value = re.sub(r"(?i)[A-Za-z]:[/\\][^\s`]+", "", value)
    value = re.sub(r"`[^`]*(?:docs/|/mnt/|/home/)[^`]*`", "", value)
    return value[:500].strip(" -—:;,.`")


def _parse_lines(lines: str, total: int) -> tuple[int, int]:
    if not lines:
        return 0, min(total, 80)
    m = re.match(r"(\d+)(?:-(\d+))?$", lines.strip())
    if not m:
        return 0, min(total, 80)
    start = max(1, int(m.group(1)))
    end = int(m.group(2) or start + 79)
    end = min(total, end)
    return start - 1, max(start, end)


def _redact(line: str) -> str:
    line = re.sub(r"(?i)(token|secret|password|api[_-]?key)\s*=\s*[^\s]+", r"\1=<redacted>", line)
    line = re.sub(r"[A-Za-z0-9_-]{24}\.[A-Za-z0-9_-]{6}\.[A-Za-z0-9_-]{20,}", "<discord-token-redacted>", line)
    return line
