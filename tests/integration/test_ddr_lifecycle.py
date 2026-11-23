"""D-20: DDR PDF -> document version -> linked record lifecycle.

A DDR-classified PDF imported through the batch importer produces a document
version; a DDR report can then be registered against that exact version, with the
version as evidence and the well as subject. The lifecycle is idempotent and the
report stays CANDIDATE until a human approves it.
"""

from __future__ import annotations

import json
from pathlib import Path

from tests.integration.test_ddr_batch_import import DDR_TEXT, _pdf_bytes

from drilling_intelligence.core.enums import ConfirmationStatus, RecordState
from drilling_intelligence.documents.registry import DocumentRegistry
from drilling_intelligence.documents.repository import DocumentRepository
from drilling_intelligence.extraction.registry import build_default_router
from drilling_intelligence.operations.ddr_batch import MANIFEST_FORMAT, import_ddr_batch
from drilling_intelligence.operations.repository import OperationsRepository
from drilling_intelligence.wells.repository import WellRepository


def _batch_in(workspace, tmp_path: Path) -> tuple[str, str]:
    """Import one synthetic DDR PDF; return (document_id, document_version_id)."""
    base = tmp_path / "corpus"
    base.mkdir()
    (base / "syn.pdf").write_bytes(_pdf_bytes(DDR_TEXT))
    manifest = base / "manifest.json"
    manifest.write_text(json.dumps({"format": MANIFEST_FORMAT, "files": [{"path": "syn.pdf"}]}))
    with workspace.database.unit_of_work() as session:
        registry = DocumentRegistry(
            DocumentRepository(session),
            router=build_default_router(workspace.settings),
            settings=workspace.settings,
        )
        summary = import_ddr_batch(
            registry,
            manifest_path=manifest,
            base_dir=base,
            workspace_root=workspace.root,
            workspace_id=_ws_id(workspace),
        )
    assert summary.imported == 1 and summary.failed == 0
    entry = next(e for e in summary.entries if e.status == "IMPORTED")
    return entry.document_id, entry.document_version_id


def _ws_id(workspace) -> str:
    from drilling_intelligence.cli.app import _workspace_row_id

    return _workspace_row_id(workspace)


def test_ddr_pdf_to_linked_record_lifecycle(workspace, tmp_path):
    with workspace.database.unit_of_work() as session:
        well = WellRepository(session).create_well(name="LIFE-01")
        well_id = well.id
    document_id, version_id = _batch_in(workspace, tmp_path)

    with workspace.database.unit_of_work() as session:
        repo = OperationsRepository(session)
        report = repo.register_report(
            well_id=well_id,
            document_id=document_id,
            document_version_id=version_id,
            report_number="SYN-1",
            report_date_text="2026-11-23",
            status=ConfirmationStatus.CANDIDATE,
            record_state=RecordState.ACTUAL,
            origin="DERIVED",
            created_by="lifecycle-test",
            provenance=[{"event": "linked", "document_version_id": version_id}],
        )
        # Idempotent: same version + well returns the same row.
        again = repo.register_report(
            well_id=well_id,
            document_version_id=version_id,
            origin="DERIVED",
            created_by="lifecycle-test",
        )
        assert again.id == report.id

    with workspace.database.read_only() as session:
        fetched = OperationsRepository(session).get_report(report.id)
        assert fetched.document_version_id == version_id
        assert fetched.document_id == document_id
        assert fetched.status == "CANDIDATE"
        assert fetched.well_id == well_id


def test_link_refuses_unknown_version(workspace):
    with workspace.database.unit_of_work() as session:
        well = WellRepository(session).create_well(name="LIFE-02")
        repo = OperationsRepository(session)
        import pytest

        with pytest.raises(Exception, match="no document version"):
            repo.register_report(
                well_id=well.id,
                document_version_id="ver-does-not-exist",
                origin="DERIVED",
            )
