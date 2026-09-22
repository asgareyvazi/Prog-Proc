"""V3 mud promotion: source replacement is explicit and human confirmation wins."""

from __future__ import annotations

from openpyxl import load_workbook
from sqlalchemy import select
from tests.fixtures.fieldops import fetch, ingest, promote

from drilling_intelligence.database.models import MudMeasurement, MudReport
from drilling_intelligence.ingestion.pipeline import IngestionPipeline
from drilling_intelligence.operations.service import OperationalService


def test_changed_mud_source_preserves_confirmed_history(workspace) -> None:
    """A new source version creates current rows without rewriting a confirmed old value."""
    ingest(workspace)
    first = promote(workspace)
    assert first["counts"]["mud_report"]["created"] == 1

    with workspace.database.read_only() as session:
        old_report = session.scalar(select(MudReport).where(MudReport.is_current.is_(True)))
        assert old_report is not None
        old_measurement = session.scalar(
            select(MudMeasurement).where(
                MudMeasurement.mud_report_id == old_report.id,
                MudMeasurement.property_name == "mud_weight",
                MudMeasurement.sample_key == "SUMMARY",
            )
        )
        assert old_measurement is not None
        old_measurement_id = str(old_measurement.id)
        old_version_id = str(old_measurement.document_version_id)
        old_value = old_measurement.value

    with workspace.database.session() as session:
        outcome = OperationalService.for_workspace(workspace).set_status(
            "mud_measurement",
            old_measurement_id,
            "CONFIRMED",
            by="mud.engineer",
            reason="checked against the signed mud report",
            session=session,
        )
        assert outcome["status"] == "CONFIRMED"
        session.commit()

    source = workspace.root / "corpus" / "mud_report_well-a3.xlsx"
    workbook = load_workbook(source)
    workbook["Summary"]["B9"] = 10.3
    workbook.save(source)

    result = IngestionPipeline(
        settings=workspace.settings,
        workspace_root=workspace.root,
        database=workspace.database,
    ).run(root=workspace.root / "corpus", force=True)
    assert result.ok, result.error
    assert result.counts["MODIFIED"] == 1
    second = promote(workspace)
    assert second["counts"]["mud_report"]["created"] == 1
    assert second["counts"]["mud_measurement"]["created"] >= 1

    measurements = fetch(workspace, MudMeasurement)
    old = next(row for row in measurements if row.id == old_measurement_id)
    assert old.document_version_id == old_version_id
    assert old.value == old_value
    assert old.status == "CONFIRMED"
    assert old.is_current is False

    current = [
        row for row in measurements if row.property_name == "mud_weight" and row.is_current is True
    ]
    assert len(current) == 1
    assert current[0].value == 10.3
    assert current[0].status == "CANDIDATE"
    assert current[0].document_version_id != old_version_id

    reports = fetch(workspace, MudReport)
    assert sum(bool(row.is_current) for row in reports) == 1
    assert sum(row.document_version_id == old_version_id for row in reports) == 1
