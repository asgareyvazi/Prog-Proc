"""The mud unit matrix, exercised through real ingestion and promotion.

The generated corpus reports mud in imperial units only - ``ppg``, ``cP``, ``pct``, ``lb/100ft2``,
``mg/l``, ``bbl``, ``ft``.  A metric report is not an exotic input: a North Sea or Middle East
operator files the same daily test sheet with ``sg``, ``mm``, ``Pa`` and ``m3`` in the headers.  Until
this suite existed nothing in the repository read such a header, so the behaviour on one was unknown
rather than decided.

What this suite pins down is the *policy*, not a list of units:

*   A unit the closed vocabulary knows is stored **verbatim**.  Never converted: a value filed in
    metres is stored as metres, and ``normalized_value``/``normalized_unit`` stay NULL because
    normalising one would be an engineering decision the source did not make.
*   A unit the vocabulary does **not** know is not guessed and not invented.  The measurement keeps
    its value, its ``unit`` is empty, ``quality`` is ``UNVERIFIED``, and - the part that makes the
    refusal safe rather than lossy - ``source_label`` still holds the source's own header text, so a
    reader can see the operator wrote ``sg`` even though the system declines to claim it understands
    it.
*   An annotation in parentheses is never mistaken for a unit.
*   An unknown unit degrades one row's confidence; it never fails the document.

Adding a unit to the vocabulary is a one-line change.  This suite says what happens the day before
somebody makes it, and what must still be true the day after.

Each report states every property exactly once.  Stating one property twice with two different units
is the *conflict* case and belongs to ``test_mud_duplicate_properties_v3.py``, not here; mixing the
two would make a unit assertion pass or fail for the wrong reason.
"""

from __future__ import annotations

from openpyxl import Workbook
from tests.fixtures.fieldops import fetch, ingest_v4, promote_file, reingest

from drilling_intelligence.database.models import MudMeasurement, MudReport

FILE = "mud_report_well-a3.xlsx"

HEAD = (
    ("Well", "A-3"),
    ("Field", "North Cormorant"),
    ("Report date", "2025-06-14"),
    ("Revision", "3"),
)

#: Every unit the closed vocabulary is expected to know, one property each.
IMPERIAL: tuple[tuple[str, float, str], ...] = (
    ("Depth MD (ft)", 3086.0, "ft"),
    ("TVD (m)", 940.5, "m"),
    ("Mud weight (ppg)", 10.2, "ppg"),
    ("Plastic viscosity (cP)", 18.0, "cP"),
    ("Yield point (lb/100ft2)", 12.0, "lb/100ft2"),
    ("Chloride (mg/l)", 18500.0, "mg/l"),
    ("Total mud volume (bbl)", 1450.0, "bbl"),
    ("Pore pressure gradient (psi/ft)", 0.465, "psi/ft"),
)

#: ``h``/``hr``/``min`` are units a *duration* column would carry.  A mud report's ``Time`` column is
#: the sample clock (``06:00``), stored as text and never unit-parsed, so those tokens are exercised
#: against the shared helper in ``tests/unit/test_tableshape.py`` rather than against this contract.


#: The same properties filed in units the vocabulary does not hold.  An empty unit is a deliberate
#: refusal, not a parsing failure - and the value must survive it unchanged.
METRIC: tuple[tuple[str, float, str], ...] = (
    ("Depth MD (m)", 940.5, ""),
    ("TVD (m)", 907.0, ""),
    ("Mud weight (sg)", 1.22, ""),
    ("Plastic viscosity (Pa s)", 0.018, ""),
    ("Yield point (Pa)", 5.75, ""),
    ("Chloride (mg/L)", 18500.0, ""),
    ("Total mud volume (m3)", 230.5, ""),
    ("Pore pressure gradient (kPa/m)", 10.5, ""),
)

#: The mud contract requires *both* a summary block and a repeated daily test table; a workbook with
#: only one of them is refused with ``MISSING_PROVENANCE``.  Every case below therefore carries a
#: daily sheet, and the three header variants are what differ:
DAILY_IMPERIAL = ("Slip", "Time", "MW in (ppg)", "MW out (ppg)", "Visc (cP)", "Sand (pct)", "Notes")
DAILY_METRIC = ("Slip", "Time", "MW in (sg)", "MW out (sg)", "Visc (Pa s)", "Sand (pct)", "Notes")
#: One recognised unit and one unrecognised unit on the same sheet, side by side.
DAILY_MIXED = ("Slip", "Time", "MW in (ppg)", "MW out (sg)", "Visc (cP)", "Sand (pct)", "Notes")
DAILY_ROWS = (
    ("1st", "06:00", 10.15, 1.218, 18.0, 0.1, "Normal drilling"),
    ("2nd", "12:00", 10.2, 1.224, 19.0, 0.2, "Pumped pill"),
)


def _write_mud(workspace, matrix, *, headers=DAILY_IMPERIAL) -> None:
    path = workspace.root / "corpus" / FILE
    workbook = Workbook()
    summary = workbook.active
    summary.title = "Summary"
    summary["A1"] = "ACME DRILLING - WELL A-3 MUD REPORT"
    summary.merge_cells("A1:D1")
    row = 3
    for label, value in HEAD:
        summary.cell(row=row, column=1, value=label)
        summary.cell(row=row, column=2, value=value)
        row += 1
    for label, value, _unit in matrix:
        summary.cell(row=row, column=1, value=label)
        summary.cell(row=row, column=2, value=value)
        summary.cell(row=row, column=4, value="")
        row += 1
    sheet = workbook.create_sheet("Daily Tests")
    sheet.append(list(headers))
    for reading in DAILY_ROWS:
        sheet.append(list(reading))
    path.parent.mkdir(parents=True, exist_ok=True)
    workbook.save(path)


def _by_sample(workspace) -> dict[tuple[str, str], MudMeasurement]:
    """Keyed by ``(property, sample label)`` so an assertion can name the row it means.

    ``sample_key`` is a stable *identity* component (``<table key>:row:<n>``) rather than the label
    the source printed, so the label is what a reader - and therefore a test - should address a
    reading by.
    """
    return {
        (row.property_name, row.sample_label or ""): row for row in fetch(workspace, MudMeasurement)
    }


def _run(workspace, matrix, *, headers=DAILY_IMPERIAL) -> None:
    ingest_v4(workspace)
    _write_mud(workspace, matrix, headers=headers)
    reingest(workspace)
    promote_file(workspace, FILE)


# ---------------------------------------------------------------- recognised units
def test_every_known_unit_is_stored_verbatim(workspace) -> None:
    _run(workspace, IMPERIAL)

    rows = _by_sample(workspace)
    stored = {
        name: (row.value, row.unit, row.quality)
        for (name, sample), row in rows.items()
        if sample == "SUMMARY"
    }
    for label, value, unit in IMPERIAL:
        matches = [entry for entry in stored.values() if entry[0] == value]
        assert matches, f"{label} produced no stored measurement"
        assert any(entry[1] == unit for entry in matches), (
            f"{label} stored units {[entry[1] for entry in matches]}, expected {unit!r}"
        )
        assert all(entry[2] == "VALID" for entry in matches), (label, matches)


def test_no_value_is_converted_into_another_unit(workspace) -> None:
    """A depth filed in metres is stored as 940.5 m - never as 3085.6 ft."""
    _run(workspace, IMPERIAL)

    tvd = [row for row in fetch(workspace, MudMeasurement) if row.value == 940.5]
    assert tvd, "the metric TVD was not stored at all"
    for row in tvd:
        assert row.unit == "m", (row.property_name, row.unit)
    for row in fetch(workspace, MudMeasurement):
        assert row.normalized_value is None, row.property_name
        assert row.normalized_unit is None, row.property_name


# ---------------------------------------------------------------- refused units
def test_an_unknown_unit_is_refused_and_the_source_text_survives(workspace) -> None:
    """The refusal must be lossless, or it is not a refusal - it is a silent drop."""
    _run(workspace, METRIC, headers=DAILY_METRIC)

    rows = [row for row in fetch(workspace, MudMeasurement) if row.sample_key == "SUMMARY"]
    assert rows, "the metric report promoted no measurements at all"
    refused = [row for row in rows if not row.unit]
    assert refused, "a unit outside the vocabulary should leave the row without one"

    stored_units = {row.unit for row in rows}
    for bogus in ("sg", "m3", "Pa", "kPa/m", "Pa s"):
        assert bogus not in stored_units, f"{bogus!r} was accepted as a mud unit"

    for row in refused:
        assert row.quality == "UNVERIFIED", (row.property_name, row.quality)
        # The source's own header text is still on the row, so a reader can see what was said.
        assert "(" in (row.source_label or ""), row.source_label
        assert row.value is not None, row.property_name


def test_a_refused_unit_does_not_lose_the_value(workspace) -> None:
    _run(workspace, METRIC, headers=DAILY_METRIC)

    values = {row.value for row in fetch(workspace, MudMeasurement)}
    for _label, value, _unit in METRIC:
        assert value in values, f"{value} was dropped rather than stored without a unit"


def test_an_unknown_unit_does_not_fail_the_document(workspace) -> None:
    """One row's lost confidence is not a reason to reject an otherwise sound report."""
    ingest_v4(workspace)
    _write_mud(workspace, METRIC, headers=DAILY_METRIC)
    reingest(workspace)
    result = promote_file(workspace, FILE)

    assert result.outcome == "PROMOTED", result.to_dict()
    assert result.counts["mud_report"]["created"] == 1
    assert result.counts["mud_measurement"]["created"] > 0
    reports = fetch(workspace, MudReport)
    assert len(reports) == 1 and reports[0].is_current


# ---------------------------------------------------------------- annotations
def test_an_annotation_is_never_a_unit(workspace) -> None:
    """``Remarks (optional)`` is prose in brackets, not a measurement unit."""
    _run(workspace, (*IMPERIAL, ("Remarks (optional)", 0.0, "")))

    units = {row.unit for row in fetch(workspace, MudMeasurement)}
    for annotation in ("optional", "as reported by driller", "see attached"):
        assert annotation not in units, annotation


# ---------------------------------------------------------------- the daily sheet
def test_the_daily_sheet_keeps_recognised_and_unknown_units_apart(workspace) -> None:
    """Two columns, one sheet: the known one stays VALID, the unknown one stays UNVERIFIED."""
    _run(workspace, IMPERIAL, headers=DAILY_MIXED)

    rows = _by_sample(workspace)
    recognised = rows[("mud_weight_in", "1st")]
    refused = rows[("mud_weight_out", "1st")]
    assert (recognised.value, recognised.unit, recognised.quality) == (10.15, "ppg", "VALID")
    assert (refused.value, refused.unit, refused.quality) == (1.218, "", "UNVERIFIED")
    assert "MW out (sg)" in refused.source_label, refused.source_label
    # The refusal did not drop the reading: two samples of four daily properties.
    daily = [row for row in rows.values() if row.sample_label in {"1st", "2nd"}]
    assert len(daily) == 8, sorted((row.property_name, row.sample_label) for row in daily)


def test_source_value_text_survives_whatever_happened_to_the_unit(workspace) -> None:
    """``source_value_text`` is the last line of defence: the cell as the operator typed it."""
    _run(workspace, METRIC, headers=DAILY_METRIC)

    for row in fetch(workspace, MudMeasurement):
        assert row.source_value_text, f"{row.property_name} lost its source text"


# ---------------------------------------------------------------- report metadata
def test_report_metadata_is_unit_parsed_but_never_becomes_a_measurement(workspace) -> None:
    """``hole_size_in`` describes the report, so it is read into the parent - not measured.

    It still has to be *parsed* with the same unit rule, because it is what matches a hole section:
    a hole size read as ``in`` can match a durable section, one read as ``mm`` cannot be claimed to.
    """
    from drilling_intelligence.operations.mud import summary_entries

    payload = {
        "tables": [
            {
                "table_id": "t1",
                "sheet": "Summary",
                "anchor": "A1",
                "page": 1,
                "provenance": {"table_id": "t1"},
                "rows": [
                    ["ACME MUD REPORT", "", "", ""],
                    ["Hole size (in)", "8.5", "", ""],
                    ["Hole size (mm)", "215.9", "", ""],
                    ["Time (h)", "12", "", ""],
                ],
            }
        ]
    }
    entries = {entry.source_label: entry for entry in summary_entries(payload)}
    assert entries["Hole size (in)"].source_unit == "in"
    assert entries["Hole size (mm)"].source_unit == "", "mm was guessed at"
    assert entries["Hole size (mm)"].source_value == "215.9", "the metric value was lost"
    # ``Time`` is not a mud property at all, so it is not silently read as one.
    assert "Time (h)" not in entries, list(entries)

    _run(workspace, IMPERIAL)
    properties = {row.property_name for row in fetch(workspace, MudMeasurement)}
    from drilling_intelligence.operations.promote import MUD_SUMMARY_METADATA

    assert not properties & set(MUD_SUMMARY_METADATA), properties
