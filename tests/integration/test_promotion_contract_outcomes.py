"""Runtime proof that the static contract registry is visible in batch outcomes."""

from __future__ import annotations

from sqlalchemy import select
from tests.fixtures.fieldops import fetch, ingest

from drilling_intelligence.database.models import Document, NptRecord
from drilling_intelligence.operations.service import OperationalService


def test_forensic_batch_reports_evidence_only_files_without_writing_them(workspace) -> None:
    ingest(workspace)
    service = OperationalService.for_workspace(workspace)
    with workspace.database.session() as session:
        summary = service.promote_workspace(session=session, include_unsupported=True)
        session.commit()

    # The generated corpus has six current extracted versions: four certified/promotable domain
    # sources and two evidence-only/degraded sources.  The latter are visited only because this is
    # the explicit forensic mode.
    assert summary["versions"] == 6, summary
    assert summary["outcomes"]["unsupported"] >= 2, summary
    assert summary["eligibility"]["ineligible"] >= 2, summary
    assert summary["skipped"].get("UNSUPPORTED_CLASSIFICATION", 0) >= 2, summary

    unsupported_ids: list[str] = []
    with workspace.database.read_only() as session:
        for filename in (
            "lesson_learned_ll-2025-014.txt",
            "scanned_well_b11_report.pdf",
        ):
            document = session.scalar(select(Document).where(Document.filename == filename))
            assert document is not None
            unsupported_ids.append(str(document.id))
    assert not [row for row in fetch(workspace, NptRecord) if row.document_id in unsupported_ids]
