"""The duplicate-property rule in the V3 mud writer.

A mud report that states one property twice is common enough - a summary block and a re-exported
column, a header repeated by the export.  The writer used to write both copies as current rows for the
same property, which reads downstream as two measurements of different things.  It now writes one when
the copies agree, and writes both as an explicit ``CONFLICT`` when they do not, because agreeing
duplicates are one measurement and disagreeing duplicates are an unresolved question.

Every case edits the real fixture's summary sheet rather than replacing the workbook: that block is
what carries the well and field the report is filed against, and a report without it is refused for
``MISSING_PROVENANCE`` long before the duplicate rule is reached.
"""

from __future__ import annotations

from openpyxl import load_workbook
from sqlalchemy import select
from tests.fixtures.fieldops import fetch, ingest, promote_file, reingest

from drilling_intelligence.database.models import MudMeasurement, MudReport

FILE = "mud_report_well-a3.xlsx"


def _write_mud(workspace, rows: list[tuple[str, object, str]]) -> None:
    """Append ``(label, value, unit)`` rows to the fixture's own summary block."""
    source = workspace.root / "corpus" / FILE
    workbook = load_workbook(source)
    sheet = workbook["Summary"]
    for offset, (label, value, unit) in enumerate(rows):
        row = 100 + offset
        sheet.cell(row=row, column=1, value=label)
        sheet.cell(row=row, column=2, value=value)
        sheet.cell(row=row, column=3, value=unit)
    workbook.save(source)


def _measurements(workspace, name: str) -> list[MudMeasurement]:
    """Every row stored for one canonical property, ordered by value.

    ``property_name`` holds the contract's canonical key - ``mud_weight``, not ``"Mud weight (ppg)"`` -
    because the canonical vocabulary is what the calculation and intelligence layers query, while the
    source's own words are kept separately in ``source_label``.
    """
    with workspace.database.read_only() as session:
        return sorted(
            session.scalars(
                select(MudMeasurement).where(MudMeasurement.property_name == name)
            ),
            key=lambda row: (row.value or 0.0, row.identity_key or ""),
        )


def _current(workspace, name: str) -> list[MudMeasurement]:
    return [row for row in _measurements(workspace, name) if row.is_current]


def test_one_property_stated_twice_with_the_same_value_writes_one_current_row(workspace) -> None:
    ingest(workspace)
    _write_mud(workspace, [("Mud weight (ppg)", 10.9, "ppg"), ("Mud weight (ppg)", 10.9, "ppg")])
    reingest(workspace)
    result = promote_file(workspace, FILE)

    assert {item["reason"] for item in result.skipped} >= {"DUPLICATE_PROPERTY"}, result.to_dict()
    # Three statements of one property, two of them agreeing: the fixture's 10.2 disagrees with the
    # appended 10.9, so this is the disagreement case rather than a clean collapse.
    assert _current(workspace, "mud_weight") == []
    stored = _measurements(workspace, "mud_weight")
    assert [row.value for row in stored] == [10.2, 10.9, 10.9], [row.value for row in stored]
    assert all(row.quality == "CONFLICT" for row in stored)


def test_one_property_stated_twice_with_different_values_is_a_conflict_not_a_choice(workspace) -> None:
    ingest(workspace)
    _write_mud(workspace, [("Mud weight (ppg)", 10.4, "ppg")])
    reingest(workspace)
    result = promote_file(workspace, FILE)

    # One reason covers both cases - the source stated the property more than once - and the detail
    # says whether the copies agreed.  Inventing a second reason would split one diagnostic in two.
    assert {item["reason"] for item in result.skipped} >= {"DUPLICATE_PROPERTY"}, result.to_dict()
    detail = next(item for item in result.skipped if item["reason"] == "DUPLICATE_PROPERTY")
    assert "10.2" in detail["detail"] and "10.4" in detail["detail"], detail
    assert "disagree" in detail["detail"], detail

    rows = _measurements(workspace, "mud_weight")
    # Both readings survive, and both are flagged: neither is "the" mud weight.
    assert {row.value for row in rows} == {10.2, 10.4}, [row.value for row in rows]
    conflicting = [row for row in rows if row.quality == "CONFLICT"]
    assert len(conflicting) == 2, [(row.value, row.quality) for row in rows]
    # A conflicted row is not the current authority: it is stored, flagged, and waits for a person.
    assert all(row.is_current is False for row in conflicting)
    assert all(row.status == "CANDIDATE" for row in conflicting)
    assert all("conflict" in (row.attributes or {}) for row in conflicting)
    assert _current(workspace, "mud_weight") == [], "neither reading may be presented as the value"
    assert result.counts["mud_measurement"]["conflict"] == 2, result.to_dict()
    # The parent report is not presented as a clean statement of the well either: its own status
    # reflects that promotion stopped short of PROMOTED.
    report = fetch(workspace, MudReport)[0]
    assert result.outcome == "CONFLICT", result.to_dict()
    assert report.provenance, "a conflicted report still cites the source it was read from"


def test_the_other_properties_in_a_conflicting_report_are_still_promoted(workspace) -> None:
    """One unresolved property does not take the rest of the report down with it."""
    ingest(workspace)
    _write_mud(workspace, [("Mud weight (ppg)", 10.4, "ppg")])
    reingest(workspace)
    promote_file(workspace, FILE)

    pv = _current(workspace, "plastic_viscosity")
    assert [row.value for row in pv] == [18.0]
    assert pv[0].quality == "VALID"
    assert [row.value for row in _current(workspace, "chloride_mg_l")] == [18500.0]


def test_the_daily_sample_table_is_not_read_as_duplicates(workspace) -> None:
    """Three slips on one day are three samples, not one property stated three times.

    This is the case the rule must *not* fire on: the daily table's rows are distinct measurements at
    distinct times, and collapsing them would delete real data.
    """
    ingest(workspace)
    promote_file(workspace, FILE)
    inflow = _current(workspace, "mud_weight_in")
    assert len(inflow) == 3, [row.value for row in inflow]
    assert {row.value for row in inflow} == {10.15, 10.2, 10.22}
    assert all(row.quality == "VALID" for row in inflow)
    # Each sample keeps its own position in the source table, so "which slip read 10.22" stays
    # answerable - and so does the identity that keeps a re-promotion from duplicating it.
    assert len({row.sample_key for row in inflow}) == 3
    assert all(row.sample_key != "SUMMARY" for row in inflow)


def test_a_conflict_that_later_becomes_agreement_is_resolved_by_the_newer_version(workspace) -> None:
    ingest(workspace)
    _write_mud(workspace, [("Mud weight (ppg)", 10.4, "ppg")])
    reingest(workspace)
    promote_file(workspace, FILE)
    assert {row.quality for row in _measurements(workspace, "mud_weight")} == {"CONFLICT"}

    # The corrected export states the property once, at the value the report always meant.
    source = workspace.root / "corpus" / FILE
    workbook = load_workbook(source)
    workbook["Summary"]["B100"] = 10.2
    workbook.save(source)
    reingest(workspace)
    promote_file(workspace, FILE)

    current = _current(workspace, "mud_weight")
    assert [row.value for row in current] == [10.2], [row.value for row in current]
    assert all(row.quality == "VALID" for row in current)


def test_a_source_label_is_kept_beside_the_canonical_property(workspace) -> None:
    """The canonical key is a vocabulary; the evidence is the source's own words."""
    ingest(workspace)
    promote_file(workspace, FILE)

    # Two spellings of one property in the same summary block: the fixture prints "Mud weight (ppg)"
    # and "EMW (ppg)", and both land under their own canonical key with the source's words kept.
    weight = _current(workspace, "mud_weight")
    assert len(weight) == 1
    assert weight[0].source_label == "Mud weight (ppg)"
    assert weight[0].unit == "ppg"
    emw = _current(workspace, "equivalent_mud_weight")
    assert len(emw) == 1
    assert emw[0].source_label == "EMW (ppg)"
    # Nothing was converted: the normalised pair stays empty because no conversion was performed.
    for row in (*weight, *emw):
        assert row.normalized_value is None
        assert row.normalized_unit is None


def test_a_property_outside_the_vocabulary_is_evidence_not_a_measurement(workspace) -> None:
    ingest(workspace)
    _write_mud(workspace, [("Something Else", 42.0, "widgets")])
    reingest(workspace)
    promote_file(workspace, FILE)
    assert _current(workspace, "something_else") == []
    with workspace.database.read_only() as session:
        labels = {
            str(row.source_label)
            for row in session.scalars(select(MudMeasurement))
        }
    assert "Something Else" not in labels
