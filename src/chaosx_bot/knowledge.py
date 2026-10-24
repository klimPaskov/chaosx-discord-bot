from __future__ import annotations

import logging
import re
import time
import sqlite3
import subprocess
import threading
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from .formatting import block, bullets, heading, section, small
from .vault_index import refresh_vault_indexes, vault_indexes_are_stale

LOGGER = logging.getLogger("chaosx.knowledge")
from .indexer import (
    CatalogReadError,
    INDEX_SCHEMA_VERSION,
    VAULT_ALLOWED_ROOTS,
    catalog_fingerprint,
    connect,
    index_commit,
    iter_indexable_files,
    iter_qoder_indexable_files,
    iter_vault_indexable_files,
    rebuild_index,
    _latest_tooling_mtime,
)

MAX_EXCERPT_CHARS = 1400
PRIVATE_SOURCE_CLASSES = {"accepted_source_specification", "planning_document"}
PUBLIC_ASK_EXCLUDED_SOURCE_CLASSES = {"agent_skill_or_contract"}
MAX_CONTEXT_CHARS = 6000
# Last-resort fallback for public asks: guaranteed-hit digest terms so the
# answering model always gets real docs/notes/code ground truth from the index
# instead of answering from memory.
PUBLIC_ASK_DIGEST_QUERY = '"chaos redux" OR chaos_redux OR hoi4 OR event OR scenario OR mod OR mechanic OR focus'
# Freshness checks must never block a command: the check itself (git commit,
# workbook hash) runs at most this often, and the expensive full-repo mtime
# walk happens on a background thread (see FULL_FRESHNESS_CHECK_SECONDS).
INDEX_FRESHNESS_CHECK_SECONDS = 60.0
# How often the background freshness worker performs the full indexable-file
# walk. Between full walks it relies on the much cheaper commit/workbook
# fingerprint, so uncommitted working-tree edits appear within this window
# while commands never pay for the walk.
FULL_FRESHNESS_CHECK_SECONDS = 300.0


def _fuzzy_name_row(conn, table: str, value: str):
    """Find the best catalog row by word-overlap on the name column (index 2).

    Handles multi-word name queries that fail the substring LIKE (e.g.
    "civil war namibia" -> "Namibian Civil War"). Matches when at least half
    the query words (min 2) appear in a row's name; returns the best-scoring
    row or None.
    """
    words = [w for w in re.split(r"\W+", (value or "").casefold()) if len(w) > 1]
    if len(words) < 2:
        return None
    rows = conn.execute(f"SELECT * FROM {table}").fetchall()
    best = None
    best_score = 0
    for row in rows:
        name = str(row[2] or "").casefold()
        score = sum(1 for word in words if word in name)
        if score > best_score:
            best, best_score = row, score
    threshold = max(2, (len(words) + 1) // 2)
    return best if best_score >= threshold else None


@dataclass(frozen=True)
class Knowledge:
    repo: Path
    db_path: Path
    vault_path: Path | None = None
    qoder_path: Path | None = None
    catalog_repo: Path | None = None
    # False on a host that serves an index built somewhere else (the PC's working tree, where the authoritative
    # corpus and catalog live). Staleness then means "the index came from a different revision", and rebuilding it
    # locally would index this host's copy instead and silently drop documents that only exist in the other tree —
    # which is how the interim host lost its zombie-outbreak knowledge. An empty index is still built, so the bot
    # can never run with no knowledge at all.
    autorebuild: bool = True
    extra_roots: list[tuple[str, Path]] | None = None
    _last_freshness_check: float = field(default=0.0, init=False, repr=False, compare=False)
    _last_full_freshness: float = field(default=0.0, init=False, repr=False, compare=False)
    _refresh_in_flight: bool = field(default=False, init=False, repr=False, compare=False)
    _refresh_thread: Any = field(default=None, init=False, repr=False, compare=False)
    _refresh_error: BaseException | None = field(default=None, init=False, repr=False, compare=False)

    def ensure_index(self) -> None:
        now = datetime.now(tz=timezone.utc).timestamp()
        if now - self._last_freshness_check < INDEX_FRESHNESS_CHECK_SECONDS:
            return
        object.__setattr__(self, "_last_freshness_check", now)
        conn = connect(self.db_path)
        try:
            count = conn.execute("SELECT COUNT(*) FROM source_docs").fetchone()[0]
        finally:
            conn.close()
        if count == 0:
            # Nothing to serve yet: the first build must complete before any
            # lookup can return content.
            self._ensure_index_sync()
            return
        if self._refresh_in_flight:
            return
        object.__setattr__(self, "_refresh_in_flight", True)
        thread = threading.Thread(
            target=self._refresh_worker, name="chaosx-index-refresh", daemon=True
        )
        # The handle and the error are kept so a caller (a test, or a future health check) can wait for the
        # rebuild and see why it failed instead of polling the database and guessing.
        object.__setattr__(self, "_refresh_thread", thread)
        thread.start()

    def _refresh_worker(self) -> None:
        try:
            self._ensure_index_sync()
        except BaseException as exc:  # recorded, then re-raised so the thread reports it too
            object.__setattr__(self, "_refresh_error", exc)
            raise
        finally:
            object.__setattr__(self, "_refresh_in_flight", False)

    def _refresh_vault_indexes_if_stale(self) -> bool:
        """Regenerate the vault's index files when a deletion left stale listings behind.

        Hoops (2026-09-24): "the bot shouldn't use deleted information ever". Note-level rows are pruned
        by the rebuild (it deletes and re-reads), but the index files are only regenerated on a capture,
        so a human deleting notes in Obsidian used to leave listings the bot then quoted as fact.
        """
        if not self.vault_path or not Path(self.vault_path).exists():
            return False
        try:
            if not vault_indexes_are_stale(vault_path=Path(self.vault_path)):
                return False
            refresh_vault_indexes(
                vault_path=Path(self.vault_path),
                reason="Regenerated automatically: an index listed notes that no longer exist.",
            )
        except Exception as exc:  # never block a lookup on vault housekeeping
            LOGGER.warning("vault index refresh skipped: %s", exc)
            return False
        return True

    def _ensure_index_sync(self) -> None:
        now = datetime.now(tz=timezone.utc).timestamp()
        vault_indexes_refreshed = self._refresh_vault_indexes_if_stale()
        conn = connect(self.db_path)
        try:
            meta = dict(conn.execute("SELECT key, value FROM index_meta").fetchall())
            count = conn.execute("SELECT COUNT(*) FROM source_docs").fetchone()[0]
            scenarios = conn.execute("SELECT COUNT(*) FROM catalog_scenarios").fetchone()[0]
        finally:
            conn.close()
        indexed_at = _float_meta(meta.get("indexed_at"))
        current_commit = index_commit(self.repo, self.vault_path, self.qoder_path, self.extra_roots)
        catalog_root = self.catalog_repo or self.repo
        current_catalog_fingerprint = catalog_fingerprint(catalog_root)
        latest_source_mtime = 0.0
        if now - self._last_full_freshness >= FULL_FRESHNESS_CHECK_SECONDS:
            latest_source_mtime = max(
                _latest_indexable_mtime(self.repo),
                _latest_vault_indexable_mtime(self.vault_path),
                _latest_qoder_indexable_mtime(self.qoder_path),
                _latest_tooling_mtime(self.extra_roots or []),
                _latest_catalog_mtime(catalog_root),
            )
            object.__setattr__(self, "_last_full_freshness", now)
        needs_rebuild = (
            count == 0
            or scenarios == 0
            or meta.get("schema_version") != INDEX_SCHEMA_VERSION
            or meta.get("commit_sha") != current_commit
            or meta.get("catalog_fingerprint") != current_catalog_fingerprint
            or (latest_source_mtime > indexed_at)
            or vault_indexes_refreshed
        )
        if needs_rebuild and not self.autorebuild and count > 0 and scenarios > 0:
            # Serving an index built elsewhere: leave it alone rather than replacing it with this host's revision.
            return
        if needs_rebuild:
            try:
                rebuild_index(
                    self.repo,
                    self.db_path,
                    self.vault_path,
                    catalog_repo=self.catalog_repo,
                    qoder_path=self.qoder_path,
                    extra_roots=self.extra_roots,
                )
            except CatalogReadError:
                if count == 0 or scenarios == 0:
                    raise
                # Excel can briefly replace the workbook while saving. Keep the
                # last complete index and retry naturally on the next command.
                return
        object.__setattr__(self, "_last_freshness_check", now)

    def status(self) -> str:
        self.ensure_index()
        conn = connect(self.db_path)
        try:
            docs = conn.execute("SELECT COUNT(*) FROM source_docs").fetchone()[0]
            events = conn.execute("SELECT COUNT(*) FROM catalog_events").fetchone()[0]
            repeatable_events = conn.execute("SELECT COUNT(*) FROM catalog_events WHERE type LIKE '%Repeatable%'").fetchone()[0]
            fire_once_events = conn.execute("SELECT COUNT(*) FROM catalog_events WHERE type LIKE '%Fire-Once%'").fetchone()[0]
            major_events = conn.execute("SELECT COUNT(*) FROM catalog_events WHERE type LIKE '%Major%'").fetchone()[0]
            scenarios = conn.execute("SELECT COUNT(*) FROM catalog_scenarios").fetchone()[0]
            clusters = conn.execute("SELECT COUNT(*) FROM catalog_clusters").fetchone()[0]
            status_rows = conn.execute("SELECT COALESCE(NULLIF(status, ''), 'Unmarked') AS status, COUNT(*) FROM catalog_events GROUP BY 1 ORDER BY COUNT(*) DESC, status LIMIT 6").fetchall()
        finally:
            conn.close()
        status_text = ", ".join(f"{name}: {count}" for name, count in status_rows) or "none"
        return (
            "## Chaos Redux catalog status\n"
            f"- Events: `{events}` total\n"
            f"- Repeatable events: `{repeatable_events}`\n"
            f"- Fire-once events: `{fire_once_events}`\n"
            f"- Major events: `{major_events}`\n"
            f"- Triggerable scenarios: `{scenarios}`\n"
            f"- Clusters: `{clusters}`\n"
            f"- Indexed docs/files: `{docs}`\n"
            f"- Event status breakdown: {status_text}\n"
        )

    def testing_queue(self) -> str:
        """Every event marked `Needs Testing`, one compact line each.

        Hoops (2026-09-24): "you should be able to select any event that is currently marked as needing
        testing" - the list used to stop at the first few. Rows stay compact on purpose so the whole
        queue fits in one message as the catalog grows; the ballot below the message holds the voting.
        """
        self.ensure_index()
        conn = connect(self.db_path)
        try:
            rows = conn.execute(
                """
                SELECT event_id, name, type, cluster_id
                FROM catalog_events
                WHERE status LIKE '%Needs Testing%'
                ORDER BY CAST(event_id AS INTEGER)
                """
            ).fetchall()
            others = {
                family: conn.execute(
                    f"SELECT COUNT(*) FROM catalog_{family}s WHERE status LIKE '%Needs Testing%'"
                ).fetchone()[0]
                for family in ("scenario", "cluster")
            }
        finally:
            conn.close()
        if not rows:
            return block(
                heading("Testing queue", "🕹️"),
                small("No events are currently marked `Needs Testing` in the catalog."),
            )
        lines: list[str] = []
        used = 0
        for event_id, name, type_, cluster_id in rows:
            line = f"`Event {int(event_id):03d}` **{name}**"
            if type_:
                line += f" - {type_}"
            if used + len(line) > 1700 and lines:
                break
            lines.append(line)
            used += len(line) + 1
        hidden = len(rows) - len(lines)
        return block(
            heading(f"Testing queue - {len(rows)} event(s)", "🕹️"),
            bullets(lines),
            small(f"and {hidden} more - the ballot below lists every one.") if hidden else None,
            small(
                f"Also on the ballot: {others['scenario']} scenario(s), {others['cluster']} cluster(s), "
                "plus anything a member nominates - testing is not only about events."
            ),
            small(
                "Pick any event, play it, then use `/playtest report` with the event ID and what happened. "
                "Vote on what gets tested next with the button below."
            ),
        )

    def testing_candidate_count(self) -> int:
        """How many candidates are on the ballot across every family."""
        try:
            return sum(len(items) for items in self.testing_candidates().values())
        except Exception:
            return 0

    def testing_candidates(self) -> dict[str, list[tuple[str, str, str]]]:
        """Every object currently marked `Needs Testing`, grouped by family.

        Hoops (2026-09-24): the poll used to show only the first five events. This returns the whole
        ballot - all events, scenarios and clusters whose catalog status says Needs Testing - and the
        caller pages it because Discord allows 25 options per select.

        Each entry is ``(vote key, label carrying the catalog ID, detail line)``: the ID has to be on
        screen because a tester quotes it in `/playtest report`.
        """
        from .testing_poll import candidate_detail, candidate_key, candidate_label

        self.ensure_index()
        grouped: dict[str, list[tuple[str, str, str]]] = {"event": [], "scenario": [], "cluster": []}
        queries = {
            "event": ("catalog_events", "event_id", "name"),
            "scenario": ("catalog_scenarios", "scenario_id", "name"),
            "cluster": ("catalog_clusters", "cluster_id", "name"),
        }
        conn = connect(self.db_path)
        try:
            for kind, (table, id_column, name_column) in queries.items():
                extra_columns = ", type, cluster_id" if kind == "event" else ""
                try:
                    rows = conn.execute(
                        f"SELECT {id_column}, {name_column}{extra_columns} FROM {table} "
                        f"WHERE status LIKE '%Needs Testing%' "
                        f"ORDER BY CAST({id_column} AS INTEGER), {name_column}"
                    ).fetchall()
                except sqlite3.Error:
                    continue
                for row in rows:
                    ident, name = row[0], row[1]
                    detail = ""
                    if kind == "event":
                        type_ = str(row[2] or "").strip() if len(row) > 2 else ""
                        cluster_id = str(row[3] or "").strip() if len(row) > 3 else ""
                        detail = " · ".join(
                            part for part in (type_, f"cluster {cluster_id}" if cluster_id else "") if part
                        )
                    grouped[kind].append(
                        (
                            candidate_key(kind, str(ident)),
                            candidate_label(kind, ident, name),
                            candidate_detail(kind, detail),
                        )
                    )
        finally:
            conn.close()
        return grouped

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

    def _excerpt_for_terms(
        self, path: str, terms: list[str], window: int = 900, anchors: list[str] | None = None
    ) -> str:
        """A passage from `path` centred on the most specific query term the document contains.

        FTS snippets window around the *first* match, which for a README is the badge header — the line
        that answers "how do I install this" sits 15 lines below it and never reaches the model. Anchoring
        on the longest term the document actually contains puts the answer in the window instead.
        """
        conn = connect(self.db_path)
        try:
            row = conn.execute("SELECT content FROM source_docs WHERE path = ?", (path,)).fetchone()
        finally:
            conn.close()
        if not row:
            return ""
        content = str(row[0] or "")
        if not content:
            return ""
        lowered = content.lower()
        anchor = 0
        # A caller-supplied anchor (the project term the question carried) beats the longest query word:
        # "how do I debug a crash?" centred on "debug" and never showed the crash passage at all.
        ordered = list(anchors or []) + sorted(terms, key=len, reverse=True)
        for term in ordered:
            if len(term) < 4:
                continue
            index = lowered.find(term)
            if index >= 0:
                anchor = index
                break
        start = max(0, anchor - 220)
        passage = content[start : start + window]
        passage = re.sub(r"[ \t]+", " ", passage).strip()
        return f" … {passage}" if start else passage

    def public_ask_context(
        self,
        query: str,
        limit: int = 6,
        include_sources: bool = False,
        anchors: list[str] | None = None,
    ) -> str:
        """Return internal retrieval snippets for public /ask.

        Broad ask may use specs and planning docs for accuracy, but user-facing
        answers should not expose source paths/classes/commits by default. When
        the user explicitly asks for files/sources, include repo-relative paths.
        Retrieval never returns empty on a healthy index: a precise AND query
        falls back to the looser OR query, then to a fixed project digest, so
        the answering model always has docs/notes/code ground truth to use.
        """
        self.ensure_index()
        rows = self._public_ask_ranked_rows(query, limit)
        terms = expand_terms(_query_terms(query))
        snippets: list[str] = []
        total = 0
        seen_snips: set[str] = set()
        for i, (path, source_class, snip, _rank) in enumerate(rows, 1):
            clean = self._excerpt_for_terms(path, terms, anchors=anchors) or _clean_snippet(snip)
            if not clean:
                continue
            # Duplicate chunks (same doc indexed under several paths) add length, not information.
            fingerprint = clean[:200]
            if fingerprint in seen_snips:
                continue
            seen_snips.add(fingerprint)
            item = f"{i}. {clean}"
            if include_sources:
                item = f"{item}\n   Source: {path} ({source_class})"
            if total + len(item) > MAX_CONTEXT_CHARS:
                break
            snippets.append(item)
            total += len(item)
        return "\n".join(snippets)

    def _public_ask_ranked_rows(self, query: str, limit: int) -> list[tuple]:
        """Best available retrieval for a natural-language question, never empty.

        Order: the query as phrases longest-first, then all meaningful terms, then any single
        meaningful term, then files NAMED after the subject (a file's head is rarely its answer, so
        this only fills gaps), then a fixed digest so the answerer always has ground truth. Results
        are spread across source folders: keyword-stuffed headers otherwise crowd out short, on-topic
        documents.
        """
        base_terms = _query_terms(query)
        terms = expand_terms(base_terms)
        rows: list[tuple] = []
        seen: set[str] = set()
        per_source: dict[str, int] = {}

        def _source_of(doc_path: str) -> str:
            parts = doc_path.split("/")
            if parts[0] in ("vault", "docs", "tooling") and len(parts) > 1:
                return "/".join(parts[:2])
            return parts[0]

        def take(candidates, cap: int = 0, per_source_cap: int = 2) -> None:
            # Authority is a within-stage tiebreak: a curated vault copy outranks a raw ingest of the same
            # text, but a later stage can never jump ahead of an earlier one (phrase beats digest).
            ordered = sorted(candidates, key=_row_sort_key)
            for row in ordered:
                if len(rows) >= limit:
                    return
                if cap and len(rows) >= cap:
                    return
                if row[0] in seen:
                    continue
                source = _source_of(row[0])
                if per_source_cap and per_source.get(source, 0) >= per_source_cap:
                    continue
                seen.add(row[0])
                rows.append(row)
                per_source[source] = per_source.get(source, 0) + 1

        # 1. Phrases, longest first: "zombie outbreak" before its individual words. FTS snippets
        #    window around the matched terms, which is what the answering model actually needs.
        if len(base_terms) >= 2:
            for size in range(min(4, len(base_terms)), 1, -1):
                for start in range(0, len(base_terms) - size + 1):
                    phrase = '"' + " ".join(base_terms[start : start + size]) + '"'
                    take(self._public_ask_fts_rows(phrase, limit))
                if len(rows) >= limit:
                    break
        # 2. All terms, then any term.
        if terms:
            take(self._public_ask_fts_rows(" AND ".join(f'"{t}"' for t in terms[:8]), limit))
            # Deeper pool for the loosest query so the per-source cap has a spread to choose from.
            take(self._public_ask_fts_rows(" OR ".join(f'"{t}"' for t in terms[:10]), limit * 4))
        # 3. Filename matches, but only to fill gaps: a file's head is usually not its answer, so
        #    this must never displace a matched-region snippet above.
        if len(rows) < limit:
            take(self._public_ask_path_rows(terms, max(2, limit // 2)))
        # 4. Guaranteed baseline.
        take(self._public_ask_fts_rows(PUBLIC_ASK_DIGEST_QUERY, limit), per_source_cap=1)
        if not rows:  # never hand the answerer an empty context
            take(self._public_ask_fts_rows(PUBLIC_ASK_DIGEST_QUERY, limit), per_source_cap=0)
        return rows

    def commit_sha(self) -> str:
        """The revision the shipped index was built from. Used to stamp cached answers."""
        cached = getattr(self, "_commit_sha_cache", None)
        if cached is not None:
            return cached
        conn = connect(self.db_path)
        try:
            row = conn.execute("SELECT value FROM index_meta WHERE key = 'commit_sha'").fetchone()
        finally:
            conn.close()
        value = str(row[0]) if row else ""
        self._commit_sha_cache = value
        return value

    def known_paths(self, *, ttl_seconds: float = 600.0) -> set[str]:
        """Every path in the index, for citation checking.

        Cached, because the index only changes on a rebuild and this is called once per answer.
        """
        now = time.time()
        cached = getattr(self, "_known_paths_cache", None)
        if cached and now - cached[0] < ttl_seconds:
            return cached[1]
        conn = connect(self.db_path)
        try:
            paths = {row[0] for row in conn.execute("SELECT path FROM source_docs")}
        finally:
            conn.close()
        self._known_paths_cache = (now, paths)
        return paths

    def public_ask_rows(self, query: str, limit: int = 8) -> list[tuple]:
        """The rows a public answer would be built from. Used by tests and answer telemetry."""
        return self._public_ask_ranked_rows(query, limit)

    def _public_ask_path_rows(self, terms: list[str], limit: int) -> list[tuple]:
        """Documents whose path/filename contains the query terms, best overlap first."""
        if not terms:
            return []
        conn = connect(self.db_path)
        try:
            vault_allow = " OR ".join(
                f"d.path LIKE 'vault/{root}/%'" for root in sorted(VAULT_ALLOWED_ROOTS)
            )
            score = " + ".join(
                f"(CASE WHEN d.path LIKE ? THEN {_term_weight(t)} ELSE 0 END)" for t in terms
            )
            likes = " OR ".join("d.path LIKE ?" for _ in terms)
            window_term = max(terms, key=len).lower()
            params: list = [window_term]
            params += [f"%{t}%" for t in terms]      # score CASE expressions
            params += [f"%{t}%" for t in terms]      # WHERE ... LIKE
            params += sorted(PUBLIC_ASK_EXCLUDED_SOURCE_CLASSES)
            params.append(max(4, limit * 2))
            return conn.execute(
                f"""
                SELECT d.path, d.source_class,
                       substr(
                           replace(replace(d.content, char(10), ' '), char(13), ' '),
                           max(1, instr(lower(d.content), ?) - 300),
                           700
                       ) AS snip,
                       -({score}) AS rank
                FROM source_docs d
                WHERE ({likes})
                  AND d.source_class NOT IN ({", ".join("?" for _ in PUBLIC_ASK_EXCLUDED_SOURCE_CLASSES)})
                  AND NOT (
                      d.path LIKE 'vault/%'
                      AND NOT ({vault_allow})
                  )
                ORDER BY rank
                LIMIT ?
                """,
                params,
            ).fetchall()
        finally:
            conn.close()

    def _public_ask_fts_rows(self, safe_query: str, limit: int) -> list[tuple]:
        conn = connect(self.db_path)
        try:
            vault_allow = " OR ".join(
                f"d.path LIKE 'vault/{root}/%'" for root in sorted(VAULT_ALLOWED_ROOTS)
            )
            return conn.execute(
                f"""
                SELECT d.path, d.source_class,
                       snippet(source_docs_fts, 2, '', '', ' … ', 90) AS snip,
                       bm25(source_docs_fts) AS rank
                FROM source_docs_fts
                JOIN source_docs d ON d.id = source_docs_fts.rowid
                WHERE source_docs_fts MATCH ?
                  AND d.source_class NOT IN ({", ".join("?" for _ in PUBLIC_ASK_EXCLUDED_SOURCE_CLASSES)})
                  AND NOT (
                      d.path LIKE 'vault/%'
                      AND NOT ({vault_allow})
                  )
                ORDER BY bm25(source_docs_fts) - CASE
                            -- curated vault knowledge answers questions; plans/handoffs and test
                            -- baselines merely repeat project words and used to win on that alone
                            WHEN d.path LIKE 'vault/%' THEN 3.0
                            WHEN d.path LIKE 'qoder/%' THEN 1.5
                            ELSE 0.0
                         END
                LIMIT ?
                """,
                (safe_query, *sorted(PUBLIC_ASK_EXCLUDED_SOURCE_CLASSES), limit),
            ).fetchall()
        finally:
            conn.close()

    def resolve_event_id(self, event: str) -> int | None:
        """Resolve a public event ID/name query to its numeric catalog ID."""
        self.ensure_index()
        requested_event_id = _explicit_numeric_lookup_id(event, "event")
        row = self._find_event_by_id(requested_event_id) if requested_event_id else self._find_event(event)
        if not row or row[1] is None:
            return None
        try:
            return int(row[1])
        except (TypeError, ValueError):
            return None

    def event(self, event: str, view: str = "overview", show_evidence: bool = False) -> str:
        self.ensure_index()
        requested_event_id = _explicit_numeric_lookup_id(event, "event")
        row = self._find_event_by_id(requested_event_id) if requested_event_id else self._find_event(event)
        if not row:
            if requested_event_id:
                return f"No event for id `{requested_event_id}` was found."
            return self.search(event, scope="all", limit=5, show_evidence=show_evidence) + "\n\nNo exact event match; showing search results instead."
        keys = ["row_key", "event_id", "name", "details", "evo_i", "evo_ii", "evo_iii", "evo_iv", "evo_v", "world_end", "type", "cluster_id", "member_severity", "chaos_level", "status", "indexed_at"]
        data = dict(zip(keys, row))
        event_label = f"Event {data['event_id']}: {data['name']}" if data["event_id"] else f"Unassigned event idea: {data['name']}"
        evos = [("Evo I", data["evo_i"]), ("Evo II", data["evo_ii"]), ("Evo III", data["evo_iii"]), ("Evo IV", data["evo_iv"]), ("Evo V", data["evo_v"])]
        evolution_stage_count = sum(1 for _label, text in evos if text)
        has_world_end_scenario = bool((data["world_end"] or "").strip())
        chaos_level = (data["chaos_level"] or "").strip()
        lines = [
            f"## {event_label}",
            f"- Type: `{data['type'] or 'unknown'}`",
            f"- Chaos level: `{chaos_level or 'unknown'}`",
            f"- Evolution stages: `{evolution_stage_count}`",
            f"- World-end scenario(s): `{'Yes' if has_world_end_scenario else 'No'}`",
            f"- Status: `{data['status'] or 'unknown'}`",
            f"- Cluster: `{data['cluster_id'] or 'none'}`",
            f"- Member severity: `{data['member_severity'] or 'none'}`",
            "",
            data["details"][:MAX_EXCERPT_CHARS] or "No details available.",
        ]
        shown_evos = [f"- **{label}:** {text[:400]}" for label, text in evos if text]
        if shown_evos and view in {"overview", "design", "history"}:
            lines += ["", "### Evolution stages", *shown_evos]
        if data["world_end"]:
            lines += ["", "### World-end scenario", data["world_end"][:700]]
        if show_evidence:
            paths = self._entity_paths(data["event_id"], data["name"])
            if paths:
                lines += ["", "### Private source paths", *[f"- `{p}` — {sc}" for p, sc in paths[:12]]]
            lines += ["", self._footer("catalog workbook", "docs/spreadsheets/chaos_redux_events_catalog.xlsx")]
        return "\n".join(lines)

    def scenario(self, scenario: str, show_evidence: bool = False) -> str:
        self.ensure_index()
        requested_scenario_id = _explicit_numeric_lookup_id(scenario, "scenario", "scn")
        row = self._find_scenario_by_id(requested_scenario_id) if requested_scenario_id else self._find_scenario(scenario)
        if not row:
            if requested_scenario_id:
                return f"No scenario for id `{requested_scenario_id}` was found."
            return self.search(scenario, scope="all", limit=5, show_evidence=show_evidence) + "\n\nNo exact scenario match; showing public search results instead."
        keys = [
            "row_key",
            "scenario_id",
            "name",
            "details",
            "type_options",
            "intensity_scaling",
            "status",
            "indexed_at",
        ]
        data = dict(zip(keys, row))
        scenario_label = f"SCN-{int(data['scenario_id']):03d}: {data['name']}" if data["scenario_id"] else f"Unassigned scenario idea: {data['name']}"
        lines = [
            f"## {scenario_label}",
            f"- Type options: {data['type_options'] or 'Not specified'}",
            f"- Intensity scaling: {data['intensity_scaling'] or 'Not specified'}",
            f"- Status: `{data['status'] or 'unknown'}`",
            "",
            data["details"][:MAX_EXCERPT_CHARS] or "No details available.",
        ]
        if show_evidence:
            lines += ["", self._footer("scenario catalog", "docs/spreadsheets/chaos_redux_events_catalog.xlsx")]
        return "\n".join(lines)

    def cluster(self, cluster: str, show_evidence: bool = False) -> str:
        self.ensure_index()
        requested_cluster_id = _explicit_numeric_lookup_id(cluster, "cluster")
        conn = connect(self.db_path)
        try:
            if requested_cluster_id:
                row = conn.execute("SELECT * FROM catalog_clusters WHERE cluster_id = ?", (requested_cluster_id,)).fetchone()
            else:
                row = conn.execute("SELECT * FROM catalog_clusters WHERE lower(name) LIKE ? ORDER BY cluster_id LIMIT 1", (f"%{cluster.lower()}%",)).fetchone()
        finally:
            conn.close()
        if not row:
            if requested_cluster_id:
                return f"No cluster for id `{requested_cluster_id}` was found."
            return f"No registered cluster match for `{cluster}`. Planned clusters without IDs remain unassigned."
        row_key, cluster_id, name, details, members, type_, chaos_level, status, indexed_at = row
        label = f"Cluster {cluster_id}: {name}" if cluster_id else f"Planned cluster idea: {name}"
        member_lines = self._cluster_member_lines(members)
        member_text = "\n".join(member_lines) if member_lines else (members or "none")
        text = (
            f"## {label}\n"
            f"- Type: `{type_ or 'unknown'}`\n"
            f"- Chaos level: `{chaos_level or 'unknown'}`\n"
            f"- Members:\n{member_text}\n"
            f"- Status: `{status or 'unknown'}`\n\n"
            f"{details or 'No details available.'}"
        )
        if show_evidence:
            text += f"\n\n{self._footer('catalog workbook', 'docs/spreadsheets/chaos_redux_events_catalog.xlsx')}"
        return text

    def _cluster_member_lines(self, members: str) -> list[str]:
        ids = re.findall(r"\d+", members or "")
        if not ids:
            return []
        conn = connect(self.db_path)
        try:
            lines: list[str] = []
            for event_id in ids[:20]:
                row = conn.execute("SELECT event_id, name FROM catalog_events WHERE event_id = ?", (str(int(event_id)),)).fetchone()
                if row:
                    lines.append(f"  - `{int(row[0]):03d}` {row[1]}")
                else:
                    lines.append(f"  - `{int(event_id):03d}`")
            return lines
        finally:
            conn.close()

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
            "Community commands: `/ask`, `/event`, `/scenario`, `/cluster`, `/status`, `/testing`, `/suggestion`, `/event-idea`, `/issue`, `/playtest report`.\n"
            "Commands that use AI say so in `/help`; lookup commands are usually faster for exact event, scenario, cluster, and testing info."
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
            row = conn.execute("SELECT * FROM catalog_events WHERE lower(name) LIKE ? ORDER BY CAST(event_id AS INTEGER) LIMIT 1", (f"%{value.lower()}%",)).fetchone()
            if row:
                return row
            return _fuzzy_name_row(conn, "catalog_events", value)
        finally:
            conn.close()

    def _find_event_by_id(self, event_id: str):
        conn = connect(self.db_path)
        try:
            return conn.execute("SELECT * FROM catalog_events WHERE event_id = ?", (event_id,)).fetchone()
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
            row = conn.execute("SELECT * FROM catalog_scenarios WHERE lower(name) LIKE ? ORDER BY CAST(scenario_id AS INTEGER) LIMIT 1", (f"%{value.lower()}%",)).fetchone()
            if row:
                return row
            return _fuzzy_name_row(conn, "catalog_scenarios", value)
        finally:
            conn.close()

    def _find_scenario_by_id(self, scenario_id: str):
        conn = connect(self.db_path)
        try:
            return conn.execute("SELECT * FROM catalog_scenarios WHERE scenario_id = ?", (scenario_id,)).fetchone()
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


def _latest_catalog_mtime(repo: Path) -> float:
    catalog_root = repo / "docs/spreadsheets"
    candidates = [
        catalog_root / "chaos_redux_events_catalog.xlsx",
        catalog_root / "chaos_redux_events_catalog.csv",
        catalog_root / "chaos_redux_scenarios_catalog.csv",
        catalog_root / "chaos_redux_clusters_catalog.csv",
    ]
    mtimes: list[float] = []
    for path in candidates:
        try:
            mtimes.append(path.stat().st_mtime)
        except OSError:
            continue
    return max(mtimes, default=0.0)


def _latest_vault_indexable_mtime(vault_path: Path | None) -> float:
    if not vault_path or not vault_path.exists():
        return 0.0
    latest = 0.0
    for path in iter_vault_indexable_files(vault_path):
        try:
            latest = max(latest, path.stat().st_mtime)
        except OSError:
            continue
    return latest


def _latest_qoder_indexable_mtime(qoder_path: Path | None) -> float:
    if not qoder_path or not qoder_path.exists():
        return 0.0
    latest = 0.0
    for path in iter_qoder_indexable_files(qoder_path):
        try:
            latest = max(latest, path.stat().st_mtime)
        except OSError:
            continue
    return latest


def _extract_number(value: str) -> str | None:
    m = re.search(r"\b(?:event\s*)?(\d{1,3})\b", value, re.I)
    return m.group(1) if m else None


def _explicit_numeric_lookup_id(value: str, *prefixes: str) -> str | None:
    value = value.strip()
    if not value:
        return None
    if re.fullmatch(r"\d{1,3}", value):
        return str(int(value))
    prefix_pattern = "|".join(re.escape(prefix) for prefix in prefixes)
    match = re.fullmatch(rf"(?i)(?:{prefix_pattern})[\s#:_-]*(\d{{1,3}})", value)
    return str(int(match.group(1))) if match else None


# Words that carry no retrieval signal. Leaving them in let a question like "what is the zombie
# outbreak super event" rank documents that only share "what/is/the" above the document about zombies.
QUERY_STOPWORDS = frozenset(
    """a an the is are was were be been being do does did doing have has had having i me my we our you
    your he she it they them this that these those what which who whom whose when where why how can
    could should would will shall may might must of in on at to for with from by about into over
    after before between under above or and but if then than so not no yes there here please tell
    known does any some about me use uses using used make makes making get gets getting need needs
    want wants work works working show shows give gives find finds look looking""".split()
)


# Community vocabulary, not the mod's own spelling. Each key maps to extra literal terms FTS should try.
_SYNONYMS: dict[str, tuple[str, ...]] = {
    "superevent": ("super", "event"),
    "super": ("superevent",),
    "focus": ("focus_tree",),
    "focus_tree": ("focus",),
    "tree": ("focus_tree",),
    "ai": ("artificial", "intelligence", "strategy"),
    "country": ("tag",),
    "tag": ("country",),
    "decision": ("decisions",),
    "localisation": ("localization", "loc"),
    "localization": ("localisation", "loc"),
    "mio": ("military_industrial_organisation",),
    "modifier": ("modifiers",),
}


def expand_terms(terms: list[str]) -> list[str]:
    """Query terms plus the vocabulary members actually use, order preserved, no duplicates."""
    expanded: list[str] = []
    for term in terms:
        for candidate in (term, *_SYNONYMS.get(term, ())):
            if candidate not in expanded:
                expanded.append(candidate)
    return expanded


# Which copy of a document to trust when several say the same thing. The vault is hand-curated, docs/ is
# generated from the repo, qoder/ is an external summary, and raw/ is machine ingest of everything.
_AUTHORITY: dict[str, int] = {"vault": 0, "docs": 1, "tooling": 2, "qoder": 2, "raw": 3}


def _authority(path: str) -> int:
    text = str(path)
    if "/" not in text:
        # Repo-root docs (README, CONTRIBUTING) are curated entry points, not incidental files.
        return 0
    return _AUTHORITY.get(text.split("/", 1)[0], 4)


def _row_sort_key(row: tuple) -> tuple[int, float]:
    """Authority first, then relevance. bm25 is lower-is-better, so both parts sort ascending."""
    rank = float(row[3]) if len(row) > 3 and row[3] is not None else 0.0
    return (_authority(row[0]), rank)


def _query_terms(query: str) -> list[str]:
    """Meaningful query terms, in the order the question gave them."""
    tokens = [t.lower() for t in re.findall(r"[A-Za-z0-9_]+", query)]
    return [t for t in tokens if len(t) > 1 and t not in QUERY_STOPWORDS]


def _term_weight(term: str) -> int:
    """Longer terms are more distinctive, so they count for more in a filename match."""
    return 3 if len(term) >= 6 else 1


def _fts_query(query: str) -> str:
    # Same hyphen/quoting treatment as _fts_and_query; see its docstring.
    tokens = re.findall(r"[A-Za-z0-9_]+", query)
    return " OR ".join(f'"{t}"' for t in tokens[:8]) or '""'


def _fts_and_query(query: str) -> str:
    # Hyphens are separators to the FTS5 tokenizer, but a bare hyphen-joined
    # token in MATCH (e.g. `event-5`, `mod-code`) is misparsed as a column
    # filter ("no such column: 5"/"no such column: code"). Split on hyphens
    # and quote each term so MATCH always treats them as literal terms.
    tokens = re.findall(r"[A-Za-z0-9_]+", query)
    return " AND ".join(f'"{t}"' for t in tokens[:8]) or '""'


def _trim_words(text: str, limit: int) -> str:
    """Trim to a whole word at or before `limit` so bullets never end mid-word."""
    text = " ".join(str(text).split())
    if len(text) <= limit:
        return text
    cut = text[:limit].rsplit(" ", 1)[0].rstrip(" ,;:-")
    return f"{cut}…"


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
