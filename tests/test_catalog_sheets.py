"""Catalog workbook sheet resolution tests.

Regression: the Chaos Redux catalog workbook gained a "Cluster Memberships" sheet
between Clusters and Scenarios (2026-09-19). The loader resolved worksheets by
positional file index, so Scenarios was read from the memberships sheet, parsed
to 0 rows, and the bot's knowledge layer treated "0 scenarios" as a rebuild
trigger — rebuilding the whole index on every request.
"""

import zipfile
from pathlib import Path

import pytest

from chaosx_bot.indexer import CatalogReadError, _catalog_rows, _xlsx_sheet_member

NS_XLSX = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
NS_REL = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"


def _col(index: int) -> str:
    letters = ""
    while index:
        index, rem = divmod(index - 1, 26)
        letters = chr(65 + rem) + letters
    return letters


def _sheet_xml(rows: list[list[str]]) -> str:
    body = []
    for r, row in enumerate(rows, start=1):
        cells = "".join(
            f'<c r="{_col(i)}{r}" t="inlineStr"><is><t>{value}</t></is></c>'
            for i, value in enumerate(row, start=1)
        )
        body.append(f'<row r="{r}">{cells}</row>')
    return (
        '<?xml version="1.0"?><worksheet xmlns="' + NS_XLSX + '">'
        f'<sheetData>{"".join(body)}</sheetData></worksheet>'
    )


def _workbook_xml(names: list[str]) -> str:
    sheets = "".join(
        f'<sheet name="{name}" sheetId="{i}" r:id="rId{i}"/>' for i, name in enumerate(names, start=1)
    )
    return (
        '<?xml version="1.0"?><workbook xmlns="' + NS_XLSX + f'" xmlns:r="{NS_REL}">'
        f"<sheets>{sheets}</sheets></workbook>"
    )


def _rels_xml(names: list[str], absolute: bool = False) -> str:
    rels = "".join(
        '<Relationship Id="rId%d" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" '
        'Target="%s"/>' % (i, (f"/xl/worksheets/sheet{i}.xml" if absolute else f"worksheets/sheet{i}.xml"))
        for i, _ in enumerate(names, start=1)
    )
    return (
        '<?xml version="1.0"?><Relationships '
        'xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
        f"{rels}</Relationships>"
    )


def build_workbook(path: Path, sheets: list[tuple[str, list[list[str]]]], *, absolute: bool = False) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    names = [name for name, _ in sheets]
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr("xl/workbook.xml", _workbook_xml(names))
        zf.writestr("xl/_rels/workbook.xml.rels", _rels_xml(names, absolute=absolute))
        for index, (_, rows) in enumerate(sheets, start=1):
            zf.writestr(f"xl/worksheets/sheet{index}.xml", _sheet_xml(rows))
    return path


EVENTS = [
    ["ID", "Event Name", "Details", "World-End Scenario"],
    ["1", "Communist Insurgency", "Org networks harden.", "Yes"],
]
CLUSTERS = [["Cluster ID", "Cluster Name", "Details"], ["C1", "Red Wave", "Cluster details"]]
MEMBERSHIPS = [["Cluster ID", "Event ID", "Notes"], ["C1", "1", "membership row"]]
SCENARIOS = [
    ["Scenario ID", "Scenario Name", "Details", "Type Options", "Intensity Scaling", "Status"],
    ["SCN-001", "Zombie Apocalypse", "Outbreak spreads.", "Diverse, Random", "Low to Maximum", "Needs Testing"],
    ["SCN-002", "Solar Flare", "Grid collapse.", "Standard", "High", "Planned"],
]


def _workbook_with_memberships_sheet(tmp_path: Path, *, absolute: bool = False) -> Path:
    """5-sheet workbook: Scenarios sits at sheet4, memberships took sheet3."""
    return build_workbook(
        tmp_path / "docs/spreadsheets/chaos_redux_events_catalog.xlsx",
        [
            ("Events", EVENTS),
            ("Clusters", CLUSTERS),
            ("Cluster Memberships", MEMBERSHIPS),
            ("Scenarios", SCENARIOS),
            ("Legend", [["Key", "Meaning"], ["C1", "red"]]),
        ],
        absolute=absolute,
    )


@pytest.mark.parametrize("absolute", [False, True])
def test_scenarios_resolve_by_name_when_a_sheet_is_inserted_before_them(tmp_path: Path, absolute: bool):
    repo = _workbook_with_memberships_sheet(tmp_path, absolute=absolute)
    rows = _catalog_rows(
        repo.parents[2],
        csv_name="chaos_redux_scenarios_catalog.csv",
        sheet_index=3,
        sheet_name="Scenarios",
    )
    assert [row["Scenario ID"] for row in rows] == ["SCN-001", "SCN-002"]
    assert rows[0]["Scenario Name"] == "Zombie Apocalypse"
    assert "membership row" not in str(rows)


def test_events_and_clusters_still_resolve_by_name(tmp_path: Path):
    repo_root = _workbook_with_memberships_sheet(tmp_path).parents[2]
    events = _catalog_rows(repo_root, csv_name="chaos_redux_events_catalog.csv", sheet_index=1, sheet_name="Events")
    clusters = _catalog_rows(repo_root, csv_name="chaos_redux_clusters_catalog.csv", sheet_index=2, sheet_name="Clusters")
    assert [row["ID"] for row in events] == ["1"]
    assert [row["Cluster ID"] for row in clusters] == ["C1"]


def test_positional_fallback_used_when_sheet_name_is_unexpected(tmp_path: Path):
    path = build_workbook(
        tmp_path / "docs/spreadsheets/chaos_redux_events_catalog.xlsx",
        [("Events", EVENTS), ("Clusters", CLUSTERS), ("Szenarien", SCENARIOS)],
    )
    with zipfile.ZipFile(path) as zf:
        assert _xlsx_sheet_member(zf, sheet_name="Scenarios", sheet_index=3) == "xl/worksheets/sheet3.xml"


def test_missing_sheet_is_a_catalog_read_error(tmp_path: Path):
    path = build_workbook(
        tmp_path / "docs/spreadsheets/chaos_redux_events_catalog.xlsx",
        [("Events", EVENTS)],  # no Scenarios sheet and no sheet3 member
    )
    with pytest.raises(CatalogReadError):
        _catalog_rows(path.parents[2], csv_name="chaos_redux_scenarios_catalog.csv", sheet_index=3, sheet_name="Scenarios")


def test_csv_fallback_used_when_workbook_is_absent(tmp_path: Path):
    repo_root = tmp_path
    sheets = repo_root / "docs/spreadsheets"
    sheets.mkdir(parents=True)
    (sheets / "chaos_redux_scenarios_catalog.csv").write_text(
        "Scenario ID,Scenario Name,Details\nSCN-009,CSV Fallback,From the export\n", encoding="utf-8"
    )
    rows = _catalog_rows(repo_root, csv_name="chaos_redux_scenarios_catalog.csv", sheet_index=3, sheet_name="Scenarios")
    assert [row["Scenario Name"] for row in rows] == ["CSV Fallback"]
