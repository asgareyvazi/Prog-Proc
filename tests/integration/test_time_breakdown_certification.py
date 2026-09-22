"""Standalone certification for the explicit TIME_BREAKDOWN promotion contract.

The V3 corpus exercises a daily report that contains a breakdown table, but that is not enough to prove
that the taxonomy's standalone TIME_BREAKDOWN classification enters the same narrow writer.  This suite
uses a real generated CSV, the real text/CSV extractor, the real classifier, the real migration-backed
workspace and the existing operational repositories.
"""

from __future__ import annotations

from sqlalchemy import select
from tests.fixtures.fieldops import ingest, promote, well_id_for
from tests.fixtures.generate import build_time_breakdown_csv

from drilling_intelligence.core.enums import ConfirmationStatus, KnowledgeOrigin, RecordState
from drilling_intelligence.database.models import DdrReport, Document, NptRecord, WellOperation
from drilling_intelligence.ingestion.pipeline import IngestionPipeline
from drilling_intelligence.operations.contracts import CoverageLevel, promotion_contract
from drilling_intelligence.operations.promote import find_breakdown_tables, find_npt_tables


def _ingest_standalone_breakdown(workspace):
    root = ingest(workspace)
    build_time_breakdown_csv(root / "time_breakdown_well-a3.csv")
    result = IngestionPipeline(
        settings=workspace.settings,
        workspace_root=workspace.root,
        database=workspace.database,
    ).run(
        root=root,
        well_id=well_id_for(workspace, "A-3"),
    )
    assert result.ok, result
    assert result.failures == 0, result.failures_report()
    return root


def _time_breakdown_version(workspace) -> tuple[Document, str]:
    with workspace.database.read_only() as session:
        document = session.scalar(
            select(Document).where(Document.filename == "time_breakdown_well-a3.csv")
        )
        assert document is not None
        assert document.current_version_id
        return document, str(document.current_version_id)


def test_time_breakdown_is_classified_and_promoted_from_its_real_csv(workspace) -> None:
    _ingest_standalone_breakdown(workspace)

    document, version_id = _time_breakdown_version(workspace)
    assert document.classification == "TIME_BREAKDOWN"
    contract = promotion_contract(document.classification)
    assert contract is not None
    assert contract.level == CoverageLevel.END_TO_END_CERTIFIED
    assert contract.handler == "report"

    first = promote(workspace)
    assert first["totals"]["created"] > 0, first
    with workspace.database.read_only() as session:
        report = session.scalar(
            select(DdrReport).where(DdrReport.document_version_id == version_id)
        )
        operations = list(
            session.scalars(
                select(WellOperation)
                .where(WellOperation.document_version_id == version_id)
                .order_by(WellOperation.label)
            )
        )
        npt_rows = list(
            session.scalars(
                select(NptRecord)
                .where(NptRecord.document_version_id == version_id)
                .order_by(NptRecord.duration_hours)
            )
        )

    assert report is not None
    # Five source activities become operations; the source's total row is not a sixth activity.
    assert [row.label for row in operations] == [
        "Circulating",
        "Drilling",
        "NPT - equipment",
        "NPT - stuck bit",
        "Tripping",
    ]
    assert all(row.record_state == RecordState.ACTUAL.value for row in operations)
    assert all(row.status == ConfirmationStatus.CANDIDATE.value for row in operations)
    assert all(row.origin == KnowledgeOrigin.DERIVED.value for row in operations)
    assert all(row.document_version_id == version_id for row in operations)
    assert {row.attributes["source_duration"]["text"] for row in operations} == {
        "8.25",
        "14.00",
        "1.50",
        "6.50",
        "12.00",
    }
    # The CSV extractor gives the activity rows exact line locators; promotion narrows table
    # provenance to those rows instead of storing only the table's full line range.
    assert {row.provenance[0]["locator"]["line_start"] for row in operations} == {2, 3, 4, 5, 6}
    assert all(row.started_at is not None for row in operations)
    assert {row.started_at.date().isoformat() for row in operations} == {"2025-06-15"}

    # Only the source's explicit NPT codes create NPT facts.  Productive/trip/circulating hours do not
    # become NPT merely because the table has a duration column.
    assert [row.duration_hours for row in npt_rows] == [6.5, 12.0]
    assert [row.duration_text for row in npt_rows] == ["6.50", "12.00"]
    assert all(row.event_id is None for row in npt_rows)


def test_time_breakdown_promotion_is_idempotent(workspace) -> None:
    _ingest_standalone_breakdown(workspace)
    first = promote(workspace)
    second = promote(workspace)

    assert first["totals"]["created"] > 0, first
    assert second["totals"]["created"] == 0, second
    assert second["totals"]["conflict"] == 0, second
    with workspace.database.read_only() as session:
        document, version_id = _time_breakdown_version(workspace)
        assert document.current_version_id == version_id
        assert session.scalar(
            select(WellOperation.identity_key)
            .where(WellOperation.document_version_id == version_id)
            .limit(1)
        )
        assert (
            len(
                list(
                    session.scalars(
                        select(WellOperation).where(WellOperation.document_version_id == version_id)
                    )
                )
            )
            == 5
        )


def test_time_breakdown_shape_is_not_an_npt_fallback_or_a_filename_rule() -> None:
    source_shaped = {
        "tables": [
            {
                "table_id": "activity-hours",
                "has_header": True,
                "rows": [
                    ["Date", "Activity", "Hours", "Code"],
                    ["2025-06-15", "Drilling", "8.25", "DRILL"],
                ],
            }
        ]
    }
    npt_shaped = {
        "tables": [
            {
                "table_id": "npt-hours",
                "has_header": True,
                "rows": [
                    ["Date", "Activity", "NPT Hours", "Code"],
                    ["2025-06-15", "Drilling", "8.25", "NPT"],
                ],
            }
        ]
    }
    without_activity = {
        "tables": [
            {
                "table_id": "hours-only",
                "has_header": True,
                "rows": [["Date", "Hours"], ["2025-06-15", "8.25"]],
            }
        ]
    }

    assert len(find_breakdown_tables(source_shaped)) == 1
    assert not find_npt_tables(source_shaped)
    # An explicit NPT duration header is claimed by the NPT contract, never promoted twice as a
    # generic time breakdown merely because it also has an Activity column.
    assert find_npt_tables(npt_shaped)
    assert not find_breakdown_tables(npt_shaped)
    assert not find_breakdown_tables(without_activity)
