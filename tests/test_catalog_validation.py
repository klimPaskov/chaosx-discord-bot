from chaosx_bot.catalog_validation import (
    format_workbook_validation,
    validate_catalog_rows,
)


def _event(**overrides: str) -> dict[str, str]:
    row = {
        "ID": "1",
        "Event Name": "Zombie Outbreak",
        "Details": "An outbreak begins.",
        "Evo I": "Local spread",
        "Evo II": "",
        "Evo III": "",
        "Evo IV": "",
        "Evo V": "",
        "World-End Scenario": "",
        "Type": "World Event",
        "Cluster ID": "1",
        "Member Severity": "High",
        "Status": "Needs Testing",
    }
    row.update(overrides)
    return row


def _cluster(**overrides: str) -> dict[str, str]:
    row = {
        "Cluster ID": "1",
        "Cluster Name": "Outbreaks",
        "Details": "Disease events.",
        "Members (ID)": "1",
        "Type": "Evolution",
        "Chaos level": "Medium",
        "Status": "Needs Testing",
    }
    row.update(overrides)
    return row


def _scenario(**overrides: str) -> dict[str, str]:
    row = {
        "Scenario ID": "SCN-001",
        "Scenario Name": "Total Infection",
        "Details": "The infection consumes the world.",
        "Type Options": "Fast; Slow",
        "Intensity Scaling": "Regional to global",
        "Status": "Needs Testing",
    }
    row.update(overrides)
    return row


def test_valid_catalog_has_no_findings() -> None:
    report = validate_catalog_rows([_event()], [_cluster()], [_scenario()])

    assert report.valid
    assert report.findings == ()
    assert "**PASS**" in format_workbook_validation(report)


def test_validation_finds_duplicate_ids_cross_reference_errors_and_evolution_gaps() -> None:
    report = validate_catalog_rows(
        [
            _event(**{"Cluster ID": "9", "Evo I": "", "Evo II": "Regional spread"}),
            _event(**{"Event Name": "Second outbreak"}),
        ],
        [_cluster(**{"Members (ID)": "99"})],
        [_scenario(**{"Scenario ID": "SCN-1", "Scenario Name": ""})],
    )

    codes = {finding.code for finding in report.findings}
    assert not report.valid
    assert {
        "duplicate_event_id",
        "evolution_gap",
        "unknown_cluster_reference",
        "unknown_event_member",
        "invalid_scenario_id",
        "missing_scenario_name",
    } <= codes
    rendered = format_workbook_validation(report)
    assert "**FAIL**" in rendered
    assert "duplicate_event_id" in rendered


def test_validation_allows_named_planned_cluster_without_an_id() -> None:
    planned = _cluster(**{"Cluster ID": "", "Cluster Name": "Planned cluster", "Members (ID)": ""})

    report = validate_catalog_rows([_event(**{"Cluster ID": ""})], [planned], [_scenario()])

    assert "invalid_cluster_id" not in {finding.code for finding in report.findings}
    assert report.valid
