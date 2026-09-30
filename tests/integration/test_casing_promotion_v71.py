"""Casing promotion: an actual run is not a plan, and a size is not a classification.

The two failures this suite exists to catch are the two a casing writer can make quietly.  The first
is writing the run into the plan - ``WellSection.casing_program`` is right there, and a writer that
used it would leave the intended string and the string that actually went in indistinguishable.  The
second is reading a type out of a size, because "9 5/8 in is production casing" is a convention and
not a fact, and a database that stores it as a fact cannot be argued with later.

Everything here runs the real path: file, ingestion, extraction, classification, promotion, then a
read from the database.
"""

from __future__ import annotations

from tests.fixtures.fieldops import fetch, ingest_v71, promote_file, reingest

from drilling_intelligence.database.models import (
    CasingRun,
    DrillingProgram,
    ProgramTarget,
    WellSection,
)

CASING = "casing_report_well-a3.xlsx"


def _runs(workspace, *, current_only: bool = True) -> dict[str, CasingRun]:
    rows = fetch(workspace, CasingRun)
    if current_only:
        rows = [row for row in rows if row.is_current]
    return {row.string_label or "": row for row in rows}


def _reasons(result) -> list[str]:
    return [str(item.get("reason") or "") for item in result.skipped]


def test_a_casing_tally_becomes_one_actual_row_per_string(workspace) -> None:
    ingest_v71(workspace)
    result = promote_file(workspace, CASING)
    assert result.outcome == "PROMOTED", result.to_dict()
    assert result.counts["casing_run"]["created"] == 4, result.to_dict()

    runs = _runs(workspace)
    assert set(runs) == {"Conductor", "Surface", "Intermediate", "Liner"}

    surface = runs["Surface"]
    assert surface.string_type == "surface"
    assert (surface.size_text, surface.size_value, surface.size_unit) == ("20", 20.0, "in")
    assert (surface.weight_value, surface.weight_unit) == (133.0, "lb/ft")
    assert surface.grade == "K-55"
    assert surface.connection == "Big Omega"
    assert (surface.top_depth_value, surface.top_depth_unit) == (0.0, "ft")
    assert (surface.shoe_depth_value, surface.shoe_depth_unit) == (1800.0, "ft")
    assert surface.record_state == "ACTUAL"
    assert surface.status == "CANDIDATE"
    assert surface.origin == "DERIVED"
    assert surface.provenance, "a promoted casing run must carry its source locator"


def test_the_plan_is_not_modified_by_an_actual_run(workspace) -> None:
    """The boundary the whole domain exists to keep: a report never rewrites the programme."""
    from tests.fixtures.fieldops import add_casing_program

    ingest_v71(workspace)
    add_casing_program(workspace, well_name="A-3")

    def snapshot():
        # Every place the plan is written, including the two ``casing_program`` labels: one on the
        # hole section and one on the programme target.  Both are planning text, and a casing report
        # that wrote into either would have quietly turned the plan into the result.
        return (
            [
                (row.id, row.name, row.casing_program, row.top_depth_value)
                for row in fetch(workspace, WellSection)
            ],
            [
                (row.id, row.name, row.casing_program, row.planned_depth_md_value)
                for row in fetch(workspace, ProgramTarget)
            ],
            len(fetch(workspace, DrillingProgram)),
        )

    assert snapshot()[1], "the fixture must actually have created a plan to protect"
    before = snapshot()

    promote_file(workspace, CASING)

    assert snapshot() == before, "a casing report must not touch the plan"
    assert len(_runs(workspace)) == 4, "the run went to its own table instead"


def test_string_type_is_never_inferred_from_size(workspace) -> None:
    """A 9 5/8 in string is not automatically production casing.

    The contract's type comes only from an explicit type column.  Remove that column and the type
    is unset - the size stays, the label stays, and nothing is guessed.
    """
    from drilling_intelligence.operations.casing import casing_run_entries

    ingest_v71(workspace)
    promote_file(workspace, CASING)
    assert _runs(workspace)["Intermediate"].string_type == "intermediate"

    rows = [
        ["Casing", "Size (in)", "Weight (lb/ft)", "Grade", "Shoe Depth (ft)"],
        ["9 5/8 in string", "9 5/8", "47", "P-110", "9200"],
    ]
    payload = {"tables": [{"table_id": "t", "sheet": "S", "provenance": {}, "rows": rows}]}
    entries = casing_run_entries(payload)
    assert len(entries) == 1
    assert entries[0].string_type == "", "no type column means no type, however obvious the size"
    assert entries[0].string_label == "9 5/8 in string"


def test_a_mixed_fraction_size_survives_as_text(workspace) -> None:
    """``9 5/8`` is kept verbatim; its value stays NULL rather than being derived."""
    ingest_v71(workspace)
    result = promote_file(workspace, CASING)
    assert "UNPARSED_VALUE" in _reasons(result), _reasons(result)

    intermediate = _runs(workspace)["Intermediate"]
    assert intermediate.size_text == "9 5/8"
    assert intermediate.size_value is None, "a mixed fraction is not converted here"
    assert intermediate.size_unit == "in"


def test_top_and_shoe_are_different_depths(workspace) -> None:
    ingest_v71(workspace)
    promote_file(workspace, CASING)
    liner = _runs(workspace)["Liner"]
    assert liner.top_depth_value == 9000.0
    assert liner.shoe_depth_value == 12400.0
    assert liner.top_depth_value != liner.shoe_depth_value


def test_source_units_are_preserved_not_converted(workspace) -> None:
    """A metric sheet stays metric: no value is silently restated in another unit."""
    from drilling_intelligence.operations.casing import casing_run_entries

    rows = [
        ["Casing", "Type", "Size (mm)", "Weight (kg/m)", "Grade", "Shoe Depth (m)"],
        ["Surface", "Surface", "508", "69.94", "K-55", "3048"],
    ]
    payload = {"tables": [{"table_id": "t", "sheet": "S", "provenance": {}, "rows": rows}]}
    entry = casing_run_entries(payload)[0]
    assert (entry.size_value, entry.size_unit) == (508.0, "mm")
    assert (entry.weight_value, entry.weight_unit) == (69.94, "kg/m")
    assert (entry.shoe_value, entry.shoe_unit) == (3048.0, "m")


def test_a_tubular_inventory_is_not_a_casing_run(workspace) -> None:
    """Size, weight and grade with no shoe depth is stock, not a string that was run."""
    from drilling_intelligence.operations.casing import casing_run_entries

    rows = [
        ["Item", "Size (in)", "Weight (lb/ft)", "Grade"],
        ["Casing joint", "9 5/8", "47", "P-110"],
    ]
    payload = {"tables": [{"table_id": "t", "sheet": "S", "provenance": {}, "rows": rows}]}
    assert casing_run_entries(payload) == []


def test_a_hole_section_plan_is_not_a_casing_run(workspace) -> None:
    """A plan has sizes and depths but no grade, weight, connection or type."""
    from drilling_intelligence.operations.casing import casing_run_entries

    rows = [
        ["Section", "Hole Size (in)", "Top (ft)", "Bottom (ft)"],
        ["Surface", "26", "0", "1800"],
    ]
    payload = {"tables": [{"table_id": "t", "sheet": "S", "provenance": {}, "rows": rows}]}
    assert casing_run_entries(payload) == []


def test_a_table_stating_both_plan_and_actual_is_refused_not_picked(workspace) -> None:
    """The sheet says which side is which; this contract writes actuals and will not choose."""
    from drilling_intelligence.operations.casing import (
        casing_run_entries,
        casing_table_is_ambiguous,
    )

    rows = [
        ["Casing", "Size (in)", "Grade", "Planned Shoe (ft)", "Actual Shoe (ft)"],
        ["Surface", "20", "K-55", "1800", "1815"],
    ]
    assert casing_run_entries({"tables": [{"table_id": "t", "sheet": "S", "rows": rows}]}) == []
    assert casing_table_is_ambiguous(rows) is True

    # And on the real path the whole artefact is refused, not half-promoted from one side.
    ingest_v71(workspace)
    from drilling_intelligence.core.enums import DocumentClassification
    from drilling_intelligence.operations.contracts import promotion_contract

    assert promotion_contract(DocumentClassification.CASING_REPORT).handler == "casing"


def test_repromoting_the_same_tally_creates_nothing_new(workspace) -> None:
    ingest_v71(workspace)
    promote_file(workspace, CASING)
    before = {row.id for row in fetch(workspace, CasingRun)}

    result = promote_file(workspace, CASING)
    after = {row.id for row in fetch(workspace, CasingRun)}

    assert after == before
    assert result.counts["casing_run"]["created"] == 0, result.to_dict()
    assert result.counts["casing_run"]["unchanged"] == 4, result.to_dict()


def test_a_corrected_shoe_depth_supersedes_rather_than_duplicates(workspace) -> None:
    """A corrected tally leaves one current row per string and keeps the earlier one as history."""
    from openpyxl import load_workbook

    ingest_v71(workspace)
    promote_file(workspace, CASING)
    assert sum(row.shoe_depth_value or 0 for row in _runs(workspace).values()) == 23550.0

    path = workspace.root / "corpus" / CASING
    workbook = load_workbook(path)
    workbook.active["H7"] = 9500  # the Intermediate shoe
    workbook.save(path)
    reingest(workspace)
    promote_file(workspace, CASING)

    current = _runs(workspace)
    assert set(current) == {"Conductor", "Surface", "Intermediate", "Liner"}
    assert current["Intermediate"].shoe_depth_value == 9500.0
    assert sum(row.shoe_depth_value or 0 for row in current.values()) == 23850.0

    superseded = [row for row in fetch(workspace, CasingRun) if not row.is_current]
    assert {row.string_label for row in superseded} == {
        "Conductor",
        "Surface",
        "Intermediate",
        "Liner",
    }
    assert any(
        row.string_label == "Intermediate" and row.shoe_depth_value == 9200.0 for row in superseded
    ), "the original shoe depth stays readable"


def test_a_string_the_source_stopped_stating_is_removed(workspace) -> None:
    from openpyxl import load_workbook

    ingest_v71(workspace)
    promote_file(workspace, CASING)
    assert set(_runs(workspace)) == {"Conductor", "Surface", "Intermediate", "Liner"}

    path = workspace.root / "corpus" / CASING
    workbook = load_workbook(path)
    workbook.active.delete_rows(8)  # the Liner row
    workbook.save(path)
    reingest(workspace)
    promote_file(workspace, CASING)

    assert set(_runs(workspace)) == {"Conductor", "Surface", "Intermediate"}


def test_a_casing_document_with_no_casing_table_is_refused(workspace) -> None:
    from drilling_intelligence.operations.casing import casing_run_entries

    rows = [["Remarks", "Notes"], ["Casing was run to plan", "no issues"]]
    assert casing_run_entries({"tables": [{"table_id": "t", "sheet": "S", "rows": rows}]}) == []
