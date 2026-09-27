"""Bit record promotion: one row per run, with nothing computed and no history overwritten.

A bit record is the clearest place where "update" would be a lie: bit 13 did not change bit 12, it
replaced it, and a database that stored one row per well would have thrown the earlier run away.  These
tests hold that boundary, plus the other ways the contract can go wrong - a footage figure nobody
stated, a BHA link that is really a guess, and a tally row belonging to a different well.
"""

from __future__ import annotations

from openpyxl import load_workbook
from tests.fixtures.fieldops import fetch, ingest_v4, promote, promote_file, reingest

from drilling_intelligence.core.enums import KnowledgeRelationType
from drilling_intelligence.database.integrity import check_domain_identities
from drilling_intelligence.database.models import BhaReport, BitRecord, KnowledgeRelation

FILE = "bit_tally_well-a3.xlsx"
BHA_FILE = "bha_tally_well-a3.xlsx"


def _runs(workspace, *, current_only: bool = True) -> dict[str, BitRecord]:
    """The current statement of each bit run.

    A re-ingested tally is a new document *version*, so the writer writes the runs again under the new
    version and stands the previous version's rows down rather than editing them.  Reading "the runs"
    without saying which version is how a test ends up asserting on a superseded row.
    """
    rows = fetch(workspace, BitRecord)
    if current_only:
        rows = [row for row in rows if row.is_current]
    return {row.bit_number: row for row in rows}


def test_a_tally_becomes_one_row_per_bit_run(workspace) -> None:
    ingest_v4(workspace)
    result = promote_file(workspace, FILE)
    assert result.outcome == "PROMOTED", result.to_dict()
    assert result.counts["bit_record"]["created"] == 2

    runs = _runs(workspace)
    assert set(runs) == {"12", "13"}
    first = runs["12"]
    assert first.run_number == "1"
    assert first.manufacturer == "Smith"
    assert first.bit_type == "PDC"
    assert first.iadc_code == "M1655SS"
    assert first.serial_number == "SN-4471"
    assert (first.size_value, first.size_unit, first.size_text) == (8.5, "in", "8.5")
    assert (first.depth_in_value, first.depth_in_unit) == (3500.0, "ft")
    assert (first.depth_out_value, first.depth_out_unit) == (9100.0, "ft")
    assert (first.footage_value, first.footage_unit) == (5600.0, "ft")
    assert first.rotating_hours == 96.0
    assert first.drilling_hours == 61.0
    assert first.pull_reason == "TD - casing point"
    assert first.dull_grade == "WT-1-NO-X-I-NO"
    assert first.nozzle_size_text == "3 x 16, 3 x 13"
    assert first.status == "CANDIDATE"
    assert first.origin == "DERIVED"
    assert first.provenance
    for row in runs.values():
        assert row.well_id == first.well_id
        assert row.document_version_id == first.document_version_id


def test_footage_is_what_the_source_stated_and_not_the_difference_of_the_depths(workspace) -> None:
    """5,600 ft is stated; 9,100 - 3,500 is also 5,600, and the row must not depend on that."""
    ingest_v4(workspace)
    source = workspace.root / "corpus" / FILE
    workbook = load_workbook(source)
    # Blank the footage for bit 12: a bit whose footage was not recorded has none, and the platform
    # does not reconstruct it from two depths.
    workbook["Bit Record"]["K2"] = None
    workbook.save(source)
    reingest(workspace)
    promote_file(workspace, FILE)

    run = _runs(workspace)["12"]
    assert run.footage_value is None
    assert run.footage_unit == ""
    assert run.depth_in_value == 3500.0 and run.depth_out_value == 9100.0


def test_a_replacement_bit_is_a_new_run_and_the_earlier_one_survives(workspace) -> None:
    ingest_v4(workspace)
    promote_file(workspace, FILE)
    assert set(_runs(workspace)) == {"12", "13"}

    source = workspace.root / "corpus" / FILE
    workbook = load_workbook(source)
    workbook["Bit Record"].append(
        [
            "14", "3", "A-3", "Halliburton", "PDC", "R1723RS", "SN-3320", 8.5, 10125.0, 10980.0,
            855.0, 19.0, 15.0, "Washout", "WO-3-LT-A-X-NO", "4 x 16", "15",
        ]
    )
    workbook.save(source)
    reingest(workspace)
    result = promote_file(workspace, FILE)

    # The re-ingested tally is a new document version, so all three runs are written under it and the
    # previous version's two rows are superseded - not deleted, and not edited in place.
    assert result.counts["bit_record"]["created"] == 3, result.to_dict()
    runs = _runs(workspace)
    assert set(runs) == {"12", "13", "14"}
    # All three are current statements of this version: a run is a fact about the past, and adding a
    # later run does not make the earlier ones historical.
    assert all(row.is_current for row in runs.values())
    assert all(row.status == "CANDIDATE" for row in runs.values())
    # Bit 12's own numbers are what the source always said: the replacement did not rewrite it.
    assert runs["12"].footage_value == 5600.0
    assert runs["12"].depth_out_value == 9100.0
    assert runs["13"].footage_value == 1025.0
    superseded = [row for row in fetch(workspace, BitRecord) if not row.is_current]
    assert {row.bit_number for row in superseded} == {"12", "13"}
    assert all(row.status == "SUPERSEDED" for row in superseded)


def test_a_bit_run_links_the_bha_the_source_named_and_no_other(workspace) -> None:
    ingest_v4(workspace)
    promote_file(workspace, BHA_FILE)  # promotes BHA 14
    promote_file(workspace, FILE)

    runs = _runs(workspace)
    bha = fetch(workspace, BhaReport)[0]
    assert bha.bha_number == "14"
    # Bit 13 names BHA 14, which exists; bit 12 names BHA 13, which was never promoted.
    assert runs["13"].bha_report_id == str(bha.id)
    assert runs["12"].bha_report_id is None
    assert runs["12"].attributes["source_bha_number"] == "13", (
        "an unmatched BHA number is kept, not discarded and not guessed"
    )

    with workspace.database.read_only() as session:
        from sqlalchemy import select

        edges = list(
            session.scalars(
                select(KnowledgeRelation).where(
                    KnowledgeRelation.relation == KnowledgeRelationType.BHA_HAS_BIT.value
                )
            )
        )
    assert len(edges) == 1
    assert edges[0].source_id == str(bha.id)
    assert edges[0].target_id == str(runs["13"].id)


def test_two_current_assemblies_sharing_a_number_leave_the_link_null(workspace) -> None:
    """An ambiguous link is reported; the bit is never attached to one of two candidates."""
    from openpyxl import Workbook

    # Written *before* the scan, so the pipeline links it to the well like any other source.  A file
    # dropped in afterwards is unlinked and would promote to MISSING_WELL, which is a different case.
    extra = workspace.root / "corpus" / "bha_tally_run14_second.xlsx"
    extra.parent.mkdir(parents=True, exist_ok=True)
    workbook = Workbook()
    summary = workbook.active
    summary.title = "Summary"
    for index, row in enumerate(
        [("Well", "A-3"), ("Field", "North Cormorant"), ("BHA No", "14"), ("Report date", "2025-06-20")],
        start=3,
    ):
        summary.cell(row=index, column=1, value=row[0])
        summary.cell(row=index, column=2, value=row[1])
    tally = workbook.create_sheet("BHA Tally")
    tally.append(["No", "Description", "OD (in)", "Length (ft)"])
    tally.append([1, "Drill collar 6-1/4", 6.25, 30.0])
    workbook.save(extra)

    ingest_v4(workspace)
    promote_file(workspace, BHA_FILE)
    second = promote_file(workspace, "bha_tally_run14_second.xlsx")
    assert second.outcome == "PROMOTED", second.to_dict()
    assemblies = fetch(workspace, BhaReport)
    assert len(assemblies) == 2
    assert {row.bha_number for row in assemblies} == {"14"}, "both sources number the assembly 14"
    assert all(row.is_current for row in assemblies)

    result = promote_file(workspace, FILE)
    assert {item["reason"] for item in result.skipped} >= {"AMBIGUOUS_BHA_LINK"}, result.to_dict()
    assert _runs(workspace)["13"].bha_report_id is None


def test_a_row_naming_another_well_is_skipped_and_the_rest_of_the_tally_is_promoted(workspace) -> None:
    ingest_v4(workspace)
    source = workspace.root / "corpus" / FILE
    workbook = load_workbook(source)
    # Row 1 is the sheet title, row 2 the header: bit 12 is row 3 and bit 13 is row 4.
    workbook["Bit Record"]["C4"] = "B-11"  # bit 13's well column
    workbook.save(source)
    reingest(workspace)

    result = promote_file(workspace, FILE)
    assert {item["reason"] for item in result.skipped} >= {"WELL_SCOPE_CONFLICT"}
    runs = _runs(workspace)
    assert set(runs) == {"12"}, "the row naming another well must not be written against this one"
    # And the surviving row is still this well's.
    with workspace.database.read_only() as session:
        from sqlalchemy import select

        from drilling_intelligence.database.models import Well

        assert session.scalar(select(Well.name).where(Well.id == runs["12"].well_id)) == "A-3"


def test_a_malformed_number_is_reported_and_stays_null(workspace) -> None:
    ingest_v4(workspace)
    source = workspace.root / "corpus" / FILE
    workbook = load_workbook(source)
    workbook["Bit Record"]["K4"] = "n/a"  # bit 13's footage (row 4; row 1 is the sheet title)
    workbook.save(source)
    reingest(workspace)

    result = promote_file(workspace, FILE)
    assert {item["reason"] for item in result.skipped} >= {"INVALID_FIELD"}
    run = _runs(workspace)["13"]
    assert run.footage_value is None
    # The rest of the row is unaffected: one unreadable cell does not discard the run.
    assert run.depth_in_value == 9100.0
    assert run.rotating_hours == 22.0


def test_missing_optional_columns_leave_explicit_gaps(workspace) -> None:
    from openpyxl import Workbook

    ingest_v4(workspace)
    source = workspace.root / "corpus" / FILE
    workbook = Workbook()
    sheet = workbook.active
    sheet.title = "Bit Record"
    sheet.append(["Bit No", "Size (in)", "Footage (ft)"])
    sheet.append(["7", 12.25, 1840.0])
    workbook.save(source)
    reingest(workspace)

    result = promote_file(workspace, FILE)
    assert result.counts["bit_record"]["created"] == 1, result.to_dict()
    run = fetch(workspace, BitRecord)[0]
    assert run.bit_number == "7"
    assert run.size_value == 12.25
    assert run.footage_value == 1840.0
    # Nothing was invented for the columns this source does not have.
    assert run.manufacturer == ""
    assert run.model == ""
    assert run.iadc_code == ""
    assert run.serial_number == ""
    assert run.depth_in_value is None
    assert run.depth_out_value is None
    assert run.rotating_hours is None
    assert run.drilling_hours is None
    assert run.pull_reason == ""
    assert run.dull_grade == ""
    assert run.nozzle_count is None
    assert run.run_number is None
    assert run.bha_report_id is None
    assert run.section_id is None


def test_a_table_with_a_bit_column_and_no_measurement_is_not_a_bit_record(workspace) -> None:
    from openpyxl import Workbook

    ingest_v4(workspace)
    source = workspace.root / "corpus" / FILE
    workbook = Workbook()
    sheet = workbook.active
    sheet.title = "Bit Record"
    sheet.append(["Bit No", "Make", "Model"])
    sheet.append(["7", "Smith", "S516"])
    workbook.save(source)
    reingest(workspace)

    result = promote_file(workspace, FILE)
    assert result.outcome == "UNSUPPORTED", result.to_dict()
    assert {item["reason"] for item in result.skipped} == {"NO_RECOGNISED_TABLE"}
    assert fetch(workspace, BitRecord) == []


def test_a_time_breakdown_with_a_bit_column_is_not_a_bit_record(workspace) -> None:
    """The same shape guard in the other direction: hours alone are not a bit run."""
    from openpyxl import Workbook

    ingest_v4(workspace)
    source = workspace.root / "corpus" / FILE
    workbook = Workbook()
    sheet = workbook.active
    sheet.title = "Bit Record"
    sheet.append(["Bit No", "Activity", "Hours"])
    sheet.append(["7", "Drilling", 8.0])
    workbook.save(source)
    reingest(workspace)

    result = promote_file(workspace, FILE)
    assert result.outcome == "UNSUPPORTED", result.to_dict()
    assert fetch(workspace, BitRecord) == []


def test_repromoting_the_same_tally_is_a_no_op(workspace) -> None:
    ingest_v4(workspace)
    first = promote_file(workspace, FILE)
    assert first.counts["bit_record"]["created"] == 2
    second = promote_file(workspace, FILE)
    assert second.outcome == "UNCHANGED", second.to_dict()
    assert second.counts["bit_record"]["unchanged"] == 2
    assert len(fetch(workspace, BitRecord)) == 2


def test_a_newer_source_version_stands_the_earlier_runs_down(workspace) -> None:
    ingest_v4(workspace)
    promote_file(workspace, FILE)
    before = {number: str(row.id) for number, row in _runs(workspace).items()}

    source = workspace.root / "corpus" / FILE
    workbook = load_workbook(source)
    # Row 1 is the sheet title and row 2 the header, so bit 12's footage is K3.
    workbook["Bit Record"]["K3"] = 5700.0
    workbook.save(source)
    reingest(workspace)
    promote_file(workspace, FILE)

    runs = _runs(workspace)
    assert set(runs) == {"12", "13"}
    assert runs["12"].footage_value == 5700.0
    # The restated footage is the current statement; the figure it replaced stays in the history
    # rather than being overwritten, so "what did the tally say before" remains answerable.
    superseded = [
        row
        for row in fetch(workspace, BitRecord)
        if not row.is_current and row.bit_number == "12"
    ]
    assert [row.footage_value for row in superseded] == [5600.0]
    assert {str(row.id) for row in runs.values()}.isdisjoint(before.values())


def test_a_promoted_tally_leaves_the_domain_invariants_clean(workspace) -> None:
    ingest_v4(workspace)
    promote_file(workspace, BHA_FILE)
    promote_file(workspace, FILE)
    with workspace.database.read_only() as session:
        problems = check_domain_identities(session)
    assert problems == [], [str(problem) for problem in problems]


def test_a_bit_narrative_produces_no_rows(workspace) -> None:
    from tests.fixtures.fieldops import register_wells, well_id_for
    from tests.fixtures.generate import build_v4_forensic_corpus

    from drilling_intelligence.ingestion.pipeline import IngestionPipeline

    register_wells(workspace)
    corpus = workspace.root / "corpus"
    build_v4_forensic_corpus(corpus)
    result = IngestionPipeline(
        settings=workspace.settings,
        workspace_root=workspace.root,
        database=workspace.database,
    ).run(root=corpus, well_id=well_id_for(workspace, "A-3"))
    assert result.ok, result.error
    outcome = promote_file(workspace, "bit_record_well-a3.txt")
    assert outcome.outcome == "UNSUPPORTED", outcome.to_dict()
    assert fetch(workspace, BitRecord) == []


def test_the_workspace_summary_reports_the_bit_domain(workspace) -> None:
    ingest_v4(workspace)
    promote_file(workspace, BHA_FILE)
    promote(workspace)
    from drilling_intelligence.operations.service import OperationalService

    summary = OperationalService.for_workspace(workspace).report()
    assert summary["bit_records"] == 2
    assert summary["bit"] == {"runs": 2, "linked_to_bha": 1}
    assert summary["bha_reports"] == 1
    assert summary["bha"] == {"reports": 1, "components": 6}
