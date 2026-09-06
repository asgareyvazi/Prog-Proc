"""Promotion: the generated corpus becomes operations, events, NPT and problems - and nothing else.

Every assertion here is about the promise the operational layer makes.  The rows come out of what the
files actually say, so the same folder produces the same history twice; a value nobody wrote down
stays missing; and a row belonging to a well this workspace has never heard of is *reported*, not
filed somewhere plausible.  The corpus is the real generated one - an NPT export a clerk would
recognise, and a daily report whose time-breakdown table adds up - because a fixture built for the
feature would only prove the feature works on a corpus it invented.
"""

from __future__ import annotations

import pytest
from sqlalchemy import func, select
from tests.fixtures.fieldops import (
    DDR_ACTIVITIES,
    DDR_NPT_LINES,
    DDR_TOTAL,
    STATED,
    ZERO_HOURS,
    fetch,
    field_id,
    ingest,
    promote,
    well_id_for,
)

from drilling_intelligence.core.enums import (
    CauseStatus,
    ConfirmationStatus,
    KnowledgeOrigin,
    RecordState,
)
from drilling_intelligence.database.models import (
    DdrReport,
    Document,
    KnowledgeRelation,
    NptRecord,
    ProblemOccurrence,
    Well,
    WellEvent,
    WellOperation,
)
from drilling_intelligence.operations.promote import VersionPromoter
from drilling_intelligence.operations.repository import OperationsRepository
from drilling_intelligence.operations.service import OperationalService


def test_promotion_writes_the_rows_the_sheet_states(workspace) -> None:
    ingest(workspace)
    summary = promote(workspace)
    counts = summary["counts"]
    # Two report-shaped documents in the corpus: the NPT export and the daily report.
    assert counts["report"]["created"] == 2, summary
    assert counts["event"]["created"] == len(STATED), summary
    assert counts["problem"]["created"] == len(STATED), summary
    # Each NPT line carries the activity it happened during, and the daily report's time breakdown
    # contributes one operation per activity row.
    assert counts["operation"]["created"] == len(STATED) + DDR_ACTIVITIES, summary
    # The three stated lines, plus the two the daily report itself coded NPT.
    assert counts["npt"]["created"] == len(STATED) + len(DDR_NPT_LINES), summary
    assert summary["totals"]["conflict"] == 0, summary
    # A line stating no NPT is seen and refused, and the refusal is reported rather than silent.
    assert summary["skipped"].get("ZERO_NPT") == 1, summary["skipped"]
    # So is the daily report's 18.5 h total: its own NPT lines add up to exactly that.
    assert summary["skipped"].get("TOTAL_ALREADY_COUNTED") == 1, summary["skipped"]
    assert summary["versions"] == 2, summary


def test_each_row_says_which_well_and_which_file_it_came_from(workspace) -> None:
    ingest(workspace)
    promote(workspace)
    rows = fetch(workspace, NptRecord)
    hours: dict[str, list[float]] = {}
    names = {well.id: well.name for well in fetch(workspace, Well)}
    for row in rows:
        assert row.origin == KnowledgeOrigin.DERIVED.value
        assert row.created_by == "promoter"
        # Promotion never vouches for a row: "CANDIDATE" is what "the file said so" means.
        assert row.status == ConfirmationStatus.CANDIDATE.value
        assert row.document_version_id, f"{row.id} has no version to follow back to"
        assert row.provenance, f"NPT row {row.id} was written without provenance"
        assert row.category, "the open vocabulary should have filed a category, even a fallback one"
        hours.setdefault(names[row.well_id], []).append(row.duration_hours)
    # A shared sheet is split by the well each row names, not by the well the folder was filed under.
    assert sorted(hours["A-3"]) == sorted([6.5, 12.0, *DDR_NPT_LINES]), hours
    assert sorted(hours["B-11"]) == [22.25], hours


def test_a_row_from_another_well_is_not_charged_to_this_reports_well(workspace) -> None:
    """The B-11 line has no report, because A-3's report does not describe B-11's day.

    This is the quiet corruption a shared sheet invites: attaching the row to the report the file was
    filed under makes "everything this report produced" return another well's hours, and every total
    built on that query is wrong in two wells at once.
    """
    ingest(workspace)
    promote(workspace)
    b11 = fetch(workspace, NptRecord, well_id=well_id_for(workspace, "B-11"))
    a3 = fetch(workspace, NptRecord, well_id=well_id_for(workspace, "A-3"))
    assert [row.report_id for row in b11] == [None], b11
    assert all(row.report_id for row in a3), a3
    assert all(row.document_version_id for row in b11), "the trace back to the file must survive"


def test_the_zero_hour_line_produces_no_row(workspace) -> None:
    ingest(workspace)
    promote(workspace)
    assert ZERO_HOURS not in {row.duration_hours for row in fetch(workspace, NptRecord)}
    assert not [
        event for event in fetch(workspace, WellEvent) if "circulating" in event.description.lower()
    ]


def test_promotion_is_idempotent(workspace) -> None:
    ingest(workspace)
    first = promote(workspace)
    second = promote(workspace)
    assert first["totals"]["created"] > 0, first
    assert second["totals"]["created"] == 0, second
    assert second["totals"]["conflict"] == 0, [
        e for e in second["skipped_details"] if e["reason"] == "SOURCE_CHANGED"
    ]
    # Every row the first pass created is a row the second pass recognised, and no row is added.
    assert first["totals"]["created"] == second["totals"]["unchanged"], (first, second)
    with workspace.database.read_only() as session:
        stored = int(session.scalar(select(func.count()).select_from(NptRecord)) or 0)
    assert stored == first["counts"]["npt"]["created"], stored


def test_a_row_naming_an_unknown_well_is_refused_not_moved(workspace) -> None:
    """Only A-3 exists, so the B-11 line is reported and left out - never charged to A-3."""
    ingest(workspace, wells=("A-3",))
    summary = promote(workspace)
    assert summary["skipped"].get("WELL_NOT_FOUND") == 1, summary["skipped"]
    detail = next(
        entry for entry in summary["skipped_details"] if entry["reason"] == "WELL_NOT_FOUND"
    )
    assert "B-11" in detail["detail"], detail
    assert len(fetch(workspace, WellEvent)) == len(STATED) - 1, (
        "one row less should have been promoted"
    )
    assert fetch(workspace, Well, name="B-11") == [], "a well nobody registered must not be minted"


def test_cause_is_stated_and_root_cause_stays_unknown(workspace) -> None:
    ingest(workspace)
    promote(workspace)
    problems = fetch(workspace, ProblemOccurrence)
    assert problems
    for row in problems:
        assert row.immediate_cause, "the report's own reason text should have been kept"
        assert row.immediate_cause_status == CauseStatus.KNOWN.value
        # A reason code is not a diagnosis, so promotion never claims a root cause.
        assert not row.root_cause
        assert row.root_cause_status == CauseStatus.UNKNOWN.value
        assert row.code in {"NPT-STUCK", "NPT-EQUIP"}, row.code
        assert row.problem_type in {"stuck_pipe", "equipment_failure"}, row.problem_type
    assert all(event.category == "npt" for event in fetch(workspace, WellEvent))
    operations = fetch(workspace, WellOperation)
    assert all(op.record_state == RecordState.ACTUAL.value for op in operations)
    assert all(op.ended_at is None for op in operations), (
        "neither file gave an end time, so an operation's end would be invented"
    )


def test_the_daily_report_total_is_not_promoted_beside_its_own_lines(workspace) -> None:
    """18.5 h is the sum of the report's two NPT lines; promoting both would count it twice."""
    ingest(workspace)
    promote(workspace)
    with workspace.database.read_only() as session:
        document = session.scalar(
            select(Document).where(Document.filename == "daily_drilling_report_well-a3.docx")
        )
        rows = list(session.scalars(select(NptRecord).where(NptRecord.document_id == document.id)))
    assert sorted(row.duration_hours for row in rows) == sorted(DDR_NPT_LINES), rows
    assert sum(row.duration_hours or 0.0 for row in rows) == pytest.approx(DDR_TOTAL)
    for row in rows:
        assert row.duration_basis == "STATED", "the file stated these; nobody measured them here"
        assert row.subcategory == "NPT", "the code the report used, verbatim"
        # An unclassified reason is filed under the fallback rather than invented, and it keeps the
        # report's own wording so a person can classify it later without opening the file.
        assert row.category == "other"
        assert row.event_id is None, "the report named no event to point at"
        assert row.operation_id, "the same line named the activity it happened during"


def test_summary_counts_what_is_missing_as_well_as_what_is_there(workspace) -> None:
    ingest(workspace)
    promote(workspace)
    with workspace.database.session() as session:
        summary = OperationsRepository(session).record_summary(field_id=field_id(workspace))
    assert summary["npt"]["rows"] == len(STATED) + len(DDR_NPT_LINES), summary
    assert summary["npt"]["unknown_duration"] == 0, summary
    # The daily report's lines have no date column at all, so exactly those rows are undated.
    assert summary["npt"]["undated"] == len(DDR_NPT_LINES), summary
    assert summary["npt"]["total_hours"] == pytest.approx(
        sum(item[2] for item in STATED) + sum(DDR_NPT_LINES)
    ), summary
    assert summary["npt"]["promoted"] == len(STATED) + len(DDR_NPT_LINES), summary
    # Field scope reaches every well's rows through the well, and the grouping is the real one.
    assert summary["problems"] == len(STATED), summary
    stuck = next(row for row in summary["npt_by_category"] if row["group"] == "stuck_pipe")
    assert stuck["records"] == len(STATED) - 1 and stuck["hours"] == pytest.approx(28.75), stuck


def test_identity_keys_are_content_addresses(workspace) -> None:
    ingest(workspace)
    promote(workspace)
    first = [row.identity_key for row in fetch(workspace, NptRecord)]
    assert all(key and key.startswith("promote:") for key in first), first
    assert len(set(first)) == len(first), "two different source rows produced one identity"
    promote(workspace)
    assert sorted(first) == sorted(row.identity_key for row in fetch(workspace, NptRecord)), (
        "a second promotion minted new ids instead of recognising the rows it had written"
    )


def test_a_conflicting_edit_is_reported_not_overwritten(workspace) -> None:
    """Somebody corrected the row, the artefact disagrees, and the row wins - loudly.

    Re-running promotion is routine (a folder re-ingested, a workspace repaired), and silently
    rewriting a row on the strength of a file would erase a decision the platform has no record of
    how to make.
    """
    ingest(workspace)
    promote(workspace)
    with workspace.database.session() as session:
        row = session.scalar(
            select(NptRecord).where(NptRecord.duration_hours == 22.25).order_by(NptRecord.id)
        )
        assert row is not None
        row.description = "corrected by the drilling engineer after the morning meeting"
        session.commit()
    summary = promote(workspace)
    assert summary["totals"]["conflict"] >= 1, summary
    assert any(entry["reason"] == "SOURCE_CHANGED" for entry in summary["skipped_details"]), summary
    stored = next(row for row in fetch(workspace, NptRecord) if row.duration_hours == 22.25)
    assert stored.description.startswith("corrected by"), "the promoter overwrote a person's edit"


def test_promoting_a_document_that_is_not_a_report_changes_nothing(workspace) -> None:
    ingest(workspace)
    with workspace.database.read_only() as session:
        document = session.scalar(
            select(Document).where(Document.filename == "well_a3_program_rev12.pdf")
        )
        assert document is not None
        document_id, version_id = str(document.id), str(document.current_version_id)
    outcome = OperationalService.for_workspace(workspace).promote(
        document_id=document_id, version_id=version_id
    )
    assert outcome.report_id == "", "a drilling program is not a report of a day's work"
    assert any(entry["reason"] == "NOT_A_REPORT" for entry in outcome.skipped), outcome.skipped
    assert outcome.counts.get("npt", {}).get("created", 0) == 0, outcome.to_dict()


def test_report_rows_keep_the_day_the_file_gave_them(workspace) -> None:
    ingest(workspace)
    promote(workspace)
    reports = fetch(workspace, DdrReport)
    assert len(reports) == 2, reports
    for row in reports:
        # A day is kept either as a date or as the wording that could not be read as one - never as a
        # guess.  Falling back on "today" would make every undated report look like this morning's.
        if row.report_date is None:
            assert row.report_date_text, "an undated report keeps what the file actually wrote"
        else:
            assert row.report_date.year == 2025
            assert row.report_date_text
        assert row.record_state == RecordState.ACTUAL.value
        assert row.status == ConfirmationStatus.CANDIDATE.value
        assert row.origin == KnowledgeOrigin.DERIVED.value
        # And the row says where the day came from: a field, the registry's document date, or neither.
        assert row.attributes.get("report_date_source") in (
            "field",
            "document_date",
            "none",
        ), row.attributes
    assert [row for row in reports if row.report_date is not None], (
        "the NPT sheet dates its own rows in ISO, so its report is dated"
    )


def test_promotion_links_the_report_to_what_came_out_of_it(workspace) -> None:
    """The graph, not only the foreign keys: every promoted row is reachable from its report."""
    ingest(workspace)
    promote(workspace)
    with workspace.database.read_only() as session:
        counts = dict(
            session.execute(
                select(KnowledgeRelation.relation, func.count()).group_by(
                    KnowledgeRelation.relation
                )
            ).all()
        )
    assert counts.get("REPORT_CONTAINS_EVENT", 0) >= len(STATED) - 1, counts
    assert counts.get("REPORT_CONTAINS_OPERATION", 0) >= len(STATED) + DDR_ACTIVITIES - 1, counts
    assert counts.get("OPERATION_HAS_EVENT", 0) >= len(STATED), counts
    assert counts.get("EVENT_CAUSES_NPT", 0) >= len(STATED), counts
    assert counts.get("EVENT_HAS_PROBLEM", 0) >= len(STATED), counts


def test_delete_promoted_leaves_a_manual_row_alone(workspace) -> None:
    """Re-promoting a version removes what that version derived, and nothing a person wrote."""
    ingest(workspace)
    promote(workspace)
    with workspace.database.session() as session:
        repository = OperationsRepository(session)
        manual = repository.record_npt(
            well_id=well_id_for(workspace, "A-3"),
            category="NPT-STUCK",
            description="entered by the toolpusher from the paper log",
            duration_hours=3.0,
        )
        manual_id = manual.id
        session.commit()
    promote(workspace)
    assert any(row.id == manual_id for row in fetch(workspace, NptRecord)), (
        "a hand-entered row vanished during a re-promotion"
    )


def test_promoter_reports_a_version_with_nothing_to_promote(workspace) -> None:
    ingest(workspace)
    with workspace.database.read_only() as session:
        document = session.scalar(
            select(Document).where(Document.filename == "mud_report_well-a3.xlsx")
        )
        assert document is not None
        document_id, version_id = str(document.id), str(document.current_version_id)
    with workspace.database.session() as session:
        outcome = VersionPromoter(session).promote(document_id=document_id, version_id=version_id)
        session.rollback()
    # A mud report is neither a day's report nor an NPT sheet: nothing is written, and the reason is.
    assert outcome.counts == {} or set(outcome.counts) <= {"removed"}, outcome.counts
    assert any(entry["reason"] == "NOT_A_REPORT" for entry in outcome.skipped), outcome.skipped
    assert all(row.document_id != document_id for row in fetch(workspace, NptRecord))


def test_a_field_scope_promotes_the_field_it_names(workspace) -> None:
    """``--field`` once selected documents by comparing document ids against well ids.

    The scoped sweep matched no version at all, so it returned ``created 0`` and exited successfully - the
    worst shape a bug in a batch tool can take, because nothing was written and nothing was reported, and
    the next person's spreadsheet was quietly 59 hours short.  So the assertion counts versions and rows,
    not just the absence of an exception; and the empty-scope half proves a scope that matches nothing
    neither invents rows nor deletes the ones it never looked at.
    """
    ingest(workspace)
    service = OperationalService.for_workspace(workspace)
    with workspace.database.session() as session:
        outcome = service.promote_workspace(session=session, field_id=field_id(workspace))
        session.commit()
    assert outcome["versions"] == 2, outcome
    assert outcome["totals"]["created"] == 22, outcome["counts"]
    assert outcome["totals"]["conflict"] == 0, outcome["counts"]
    assert len(fetch(workspace, NptRecord)) == len(STATED) + len(DDR_NPT_LINES)

    with workspace.database.session() as session:
        empty = OperationalService.for_workspace(workspace).promote_workspace(
            session=session, field_id="0" * 32
        )
        session.rollback()
    assert empty["versions"] == 0 and empty["totals"]["created"] == 0, empty
    assert len(fetch(workspace, NptRecord)) == len(STATED) + len(DDR_NPT_LINES), (
        "a scope that matched nothing deleted rows it never touched"
    )


def test_a_stated_duration_keeps_its_unit_and_an_unreadable_one_stays_unknown() -> None:
    """``90 min`` is a duration the report stated, not a cell nobody could read.

    The row stores hours, so a sheet that says minutes is converted - through ``core.units``, which is the
    unit authority, rather than by a division written next to the parser.  What must not happen is either of
    the two failure modes: reading "90 min" as 90 hours, or reading it as nothing and reporting a field
    with one less lost hour than it has.  Anything genuinely not a duration returns ``None`` so it is counted
    as an unknown duration, and a zero is never the answer to "the cell said something odd".
    """
    from drilling_intelligence.operations.promote import _hours

    assert _hours(6.5) == 6.5
    assert _hours("6,5") == pytest.approx(6.5), "the decimal-comma convention, still"
    assert _hours("6.5 h") == 6.5 and _hours("6.5 hrs") == 6.5
    assert _hours("90 min") == pytest.approx(1.5)
    assert _hours("90 minutes") == pytest.approx(1.5)
    assert _hours("1.5 days") == pytest.approx(36.0)
    assert _hours("45") == 45.0, "a bare number is hours, because that is what the column means"
    assert _hours("soon") is None
    assert _hours("") is None and _hours(None) is None
    assert _hours("N/A") is None, "a placeholder is not zero lost time"
    assert _hours(-3) == -3.0, "the parser reports; the database's CHECK is what refuses a negative"
