from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

from .indexer import _catalog_rows

EVENT_HEADERS = {
    "ID",
    "Event Name",
    "Details",
    "Evo I",
    "Evo II",
    "Evo III",
    "Evo IV",
    "Evo V",
    "World-End Scenario",
    "Type",
    "Cluster ID",
    "Member Severity",
    "Status",
}
CLUSTER_HEADERS = {
    "Cluster ID",
    "Cluster Name",
    "Details",
    "Members (ID)",
    "Type",
    "Chaos level",
    "Status",
}
SCENARIO_HEADERS = {
    "Scenario ID",
    "Scenario Name",
    "Details",
    "Type Options",
    "Intensity Scaling",
    "Status",
}
EVOLUTION_COLUMNS = ("Evo I", "Evo II", "Evo III", "Evo IV", "Evo V")
SCENARIO_ID_RE = re.compile(r"SCN-(\d{3})", re.IGNORECASE)
INTEGER_ID_RE = re.compile(r"0*(\d+)")


@dataclass(frozen=True)
class CatalogFinding:
    severity: str
    sheet: str
    row: int | None
    code: str
    message: str


@dataclass(frozen=True)
class WorkbookValidationReport:
    source: str
    event_rows: int
    cluster_rows: int
    scenario_rows: int
    findings: tuple[CatalogFinding, ...]

    @property
    def errors(self) -> tuple[CatalogFinding, ...]:
        return tuple(item for item in self.findings if item.severity == "error")

    @property
    def warnings(self) -> tuple[CatalogFinding, ...]:
        return tuple(item for item in self.findings if item.severity == "warning")

    @property
    def valid(self) -> bool:
        return not self.errors


def validate_workbook(repo: Path) -> WorkbookValidationReport:
    workbook = repo / "docs/spreadsheets/chaos_redux_events_catalog.xlsx"
    if not workbook.exists():
        return WorkbookValidationReport(
            source=str(workbook),
            event_rows=0,
            cluster_rows=0,
            scenario_rows=0,
            findings=(
                CatalogFinding(
                    "error",
                    "Workbook",
                    None,
                    "missing_workbook",
                    "The authoritative catalog workbook does not exist.",
                ),
            ),
        )
    events = _catalog_rows(
        repo,
        csv_name="chaos_redux_events_catalog.csv",
        sheet_index=1,
    )
    clusters = _catalog_rows(
        repo,
        csv_name="chaos_redux_clusters_catalog.csv",
        sheet_index=2,
    )
    scenarios = _catalog_rows(
        repo,
        csv_name="chaos_redux_scenarios_catalog.csv",
        sheet_index=3,
    )
    return validate_catalog_rows(
        events,
        clusters,
        scenarios,
        source=str(workbook),
    )


def validate_catalog_rows(
    events: list[dict[str, str]],
    clusters: list[dict[str, str]],
    scenarios: list[dict[str, str]],
    *,
    source: str = "chaos_redux_events_catalog.xlsx",
) -> WorkbookValidationReport:
    findings: list[CatalogFinding] = []
    _validate_headers(events, EVENT_HEADERS, "Events", findings)
    _validate_headers(clusters, CLUSTER_HEADERS, "Clusters", findings)
    _validate_headers(scenarios, SCENARIO_HEADERS, "Scenarios", findings)

    event_ids: dict[str, tuple[int, dict[str, str]]] = {}
    event_names: dict[str, tuple[int, str]] = {}
    event_cluster_ids: dict[str, set[str]] = {}
    for row_number, row in enumerate(events, 2):
        raw_id = _value(row, "ID")
        name = _value(row, "Event Name")
        if not raw_id and not name:
            continue
        event_id = _integer_id(raw_id)
        if raw_id and event_id is None:
            findings.append(_finding("error", "Events", row_number, "invalid_event_id", f"Event ID `{raw_id}` is not numeric."))
        elif event_id is not None:
            _record_unique_id(event_ids, event_id, row_number, row, "Events", "event", findings)
            if not name:
                findings.append(_finding("error", "Events", row_number, "missing_event_name", f"Event {event_id} has no name."))
            if not _value(row, "Details"):
                findings.append(_finding("warning", "Events", row_number, "missing_event_details", f"Event {event_id} has no details."))
            if not _value(row, "Status"):
                findings.append(_finding("warning", "Events", row_number, "missing_event_status", f"Event {event_id} has no status."))
        if name:
            normalized_name = _normalize_name(name)
            previous = event_names.get(normalized_name)
            if previous and previous[1] != (event_id or ""):
                findings.append(
                    _finding(
                        "warning",
                        "Events",
                        row_number,
                        "duplicate_event_name",
                        f"Event name `{name}` also appears on row {previous[0]}.",
                    )
                )
            else:
                event_names[normalized_name] = (row_number, event_id or "")
        _validate_evolution_gaps(row, row_number, event_id or name, findings)
        event_cluster_references, malformed = _parse_id_list(_value(row, "Cluster ID"))
        if malformed:
            findings.append(
                _finding(
                    "error",
                    "Events",
                    row_number,
                    "invalid_cluster_reference",
                    f"Cluster reference contains invalid text: `{malformed}`.",
                )
            )
        if event_id is not None:
            event_cluster_ids.setdefault(event_id, set(event_cluster_references))

    cluster_ids: dict[str, tuple[int, dict[str, str]]] = {}
    cluster_members: dict[str, set[str]] = {}
    for row_number, row in enumerate(clusters, 2):
        raw_id = _value(row, "Cluster ID")
        name = _value(row, "Cluster Name")
        if not raw_id and not name:
            continue
        cluster_id = _integer_id(raw_id)
        if raw_id and cluster_id is None:
            findings.append(_finding("error", "Clusters", row_number, "invalid_cluster_id", f"Cluster ID `{raw_id}` is not numeric."))
        elif cluster_id is not None:
            _record_unique_id(cluster_ids, cluster_id, row_number, row, "Clusters", "cluster", findings)
            if not name:
                findings.append(_finding("error", "Clusters", row_number, "missing_cluster_name", f"Cluster {cluster_id} has no name."))
        members, malformed = _parse_id_list(_value(row, "Members (ID)"))
        if malformed:
            findings.append(
                _finding(
                    "error",
                    "Clusters",
                    row_number,
                    "invalid_cluster_members",
                    f"Member list contains invalid text: `{malformed}`.",
                )
            )
        if cluster_id is not None:
            cluster_members[cluster_id] = set(members)

    scenario_ids: dict[str, tuple[int, dict[str, str]]] = {}
    for row_number, row in enumerate(scenarios, 2):
        raw_id = _value(row, "Scenario ID")
        name = _value(row, "Scenario Name")
        if not raw_id and not name:
            continue
        match = SCENARIO_ID_RE.fullmatch(raw_id)
        if not match:
            findings.append(
                _finding(
                    "error",
                    "Scenarios",
                    row_number,
                    "invalid_scenario_id",
                    f"Scenario ID `{raw_id or '(blank)'}` must use `SCN-###`.",
                )
            )
        else:
            scenario_id = str(int(match.group(1)))
            _record_unique_id(scenario_ids, scenario_id, row_number, row, "Scenarios", "scenario", findings)
        if not name:
            findings.append(_finding("error", "Scenarios", row_number, "missing_scenario_name", f"Scenario `{raw_id or row_number}` has no name."))
        if not _value(row, "Details"):
            findings.append(_finding("warning", "Scenarios", row_number, "missing_scenario_details", f"Scenario `{raw_id or name}` has no details."))
        if not _value(row, "Status"):
            findings.append(_finding("warning", "Scenarios", row_number, "missing_scenario_status", f"Scenario `{raw_id or name}` has no status."))

    known_events = set(event_ids)
    known_clusters = set(cluster_ids)
    for event_id, references in event_cluster_ids.items():
        for cluster_id in references:
            if cluster_id not in known_clusters:
                row_number = event_ids[event_id][0]
                findings.append(
                    _finding(
                        "error",
                        "Events",
                        row_number,
                        "unknown_cluster_reference",
                        f"Event {event_id} references missing cluster {cluster_id}.",
                    )
                )
    for cluster_id, members in cluster_members.items():
        for event_id in members:
            cluster_row = cluster_ids[cluster_id][0]
            if event_id not in known_events:
                findings.append(
                    _finding(
                        "error",
                        "Clusters",
                        cluster_row,
                        "unknown_event_member",
                        f"Cluster {cluster_id} lists missing event {event_id}.",
                    )
                )
                continue
            references = event_cluster_ids.get(event_id, set())
            if cluster_id not in references:
                findings.append(
                    _finding(
                        "warning",
                        "Clusters",
                        cluster_row,
                        "one_sided_cluster_membership",
                        f"Cluster {cluster_id} lists event {event_id}, but that event does not reference cluster {cluster_id}.",
                    )
                )

    findings.sort(key=lambda item: (item.severity != "error", item.sheet, item.row or 0, item.code))
    return WorkbookValidationReport(
        source=source,
        event_rows=len(events),
        cluster_rows=len(clusters),
        scenario_rows=len(scenarios),
        findings=tuple(findings),
    )


def format_workbook_validation(report: WorkbookValidationReport, *, limit_per_severity: int = 20) -> str:
    result = "PASS" if report.valid and not report.warnings else "PASS WITH WARNINGS" if report.valid else "FAIL"
    lines = [
        "## Chaos Redux workbook validation",
        f"- Result: **{result}**",
        f"- Rows: `{report.event_rows}` events · `{report.cluster_rows}` clusters · `{report.scenario_rows}` scenarios",
        f"- Findings: `{len(report.errors)}` error(s) · `{len(report.warnings)}` warning(s)",
    ]
    if report.errors:
        lines.extend(["", "### Errors"])
        lines.extend(_format_findings(report.errors, limit_per_severity))
    if report.warnings:
        lines.extend(["", "### Warnings"])
        lines.extend(_format_findings(report.warnings, limit_per_severity))
    if not report.findings:
        lines.extend(["", "No structural or cross-reference problems were found."])
    return "\n".join(lines)


def _validate_headers(
    rows: list[dict[str, str]],
    required: set[str],
    sheet: str,
    findings: list[CatalogFinding],
) -> None:
    if not rows:
        findings.append(_finding("error", sheet, None, "empty_sheet", "The sheet has no data rows."))
        return
    missing = sorted(required - set(rows[0]))
    if missing:
        findings.append(
            _finding(
                "error",
                sheet,
                1,
                "missing_columns",
                f"Missing required column(s): {', '.join(missing)}.",
            )
        )


def _record_unique_id(
    seen: dict[str, tuple[int, dict[str, str]]],
    entity_id: str,
    row_number: int,
    row: dict[str, str],
    sheet: str,
    kind: str,
    findings: list[CatalogFinding],
) -> None:
    previous = seen.get(entity_id)
    if previous:
        findings.append(
            _finding(
                "error",
                sheet,
                row_number,
                f"duplicate_{kind}_id",
                f"{kind.title()} ID {entity_id} is duplicated from row {previous[0]}.",
            )
        )
    else:
        seen[entity_id] = (row_number, row)


def _validate_evolution_gaps(
    row: dict[str, str],
    row_number: int,
    label: str,
    findings: list[CatalogFinding],
) -> None:
    populated = [bool(_value(row, column)) for column in EVOLUTION_COLUMNS]
    if not any(populated):
        return
    highest = max(index for index, value in enumerate(populated) if value)
    missing = [EVOLUTION_COLUMNS[index] for index in range(highest + 1) if not populated[index]]
    if missing:
        findings.append(
            _finding(
                "warning",
                "Events",
                row_number,
                "evolution_gap",
                f"Event `{label}` has later evolution stages but is missing {', '.join(missing)}.",
            )
        )


def _parse_id_list(value: str) -> tuple[list[str], str]:
    if not value:
        return [], ""
    tokens = [token.strip() for token in re.split(r"[,;]", value) if token.strip()]
    parsed: list[str] = []
    malformed: list[str] = []
    for token in tokens:
        entity_id = _integer_id(token)
        if entity_id is None:
            malformed.append(token)
        else:
            parsed.append(entity_id)
    return parsed, ", ".join(malformed)


def _integer_id(value: str) -> str | None:
    match = INTEGER_ID_RE.fullmatch((value or "").strip())
    return str(int(match.group(1))) if match else None


def _normalize_name(value: str) -> str:
    return re.sub(r"\s+", " ", value.casefold()).strip()


def _value(row: dict[str, str], key: str) -> str:
    return (row.get(key) or "").strip()


def _finding(
    severity: str,
    sheet: str,
    row: int | None,
    code: str,
    message: str,
) -> CatalogFinding:
    return CatalogFinding(severity, sheet, row, code, message)


def _format_findings(findings: tuple[CatalogFinding, ...], limit: int) -> list[str]:
    lines = []
    for finding in findings[:limit]:
        location = finding.sheet + (f" row {finding.row}" if finding.row else "")
        lines.append(f"- **{location}** · `{finding.code}` — {finding.message}")
    remaining = len(findings) - limit
    if remaining > 0:
        lines.append(f"- …and `{remaining}` more finding(s).")
    return lines
