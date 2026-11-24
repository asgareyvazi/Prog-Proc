"""Phase F / D-20 reopened: source-to-DDR candidate mapping and disposition workflow."""

from __future__ import annotations

import json

import pytest
from tests.integration.test_ddr_batch_import import _pdf_bytes

from drilling_intelligence.cli.app import _workspace_row_id
from drilling_intelligence.core.errors import ValidationError
from drilling_intelligence.database.models import DocumentVersion
from drilling_intelligence.documents.registry import DocumentRegistry
from drilling_intelligence.documents.repository import DocumentRepository
from drilling_intelligence.extraction.registry import build_default_router
from drilling_intelligence.operations.ddr_batch import (
    MANIFEST_FORMAT,
    import_ddr_batch,
    review_queue,
)
from drilling_intelligence.operations.ddr_candidates import (
    accepted_values,
    authorize_report,
    build_ddr_candidates,
    record_disposition,
)
from drilling_intelligence.wells.repository import WellRepository

SHORT_DDR = "Daily Drilling Report. Report No. SYN-1. Well W-1. From 100 To 200."


def _import_one(workspace, tmp_path, text=SHORT_DDR):
    base = tmp_path / "corpus"
    base.mkdir(exist_ok=True)
    (base / "syn.pdf").write_bytes(_pdf_bytes(text))
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
            workspace_id=_workspace_row_id(workspace),
        )
    assert summary.imported == 1
    return summary.entries[0].document_version_id


def _text_for(workspace, version_id):
    from sqlalchemy import select

    from drilling_intelligence.database.models import Extraction

    with workspace.database.read_only() as session:
        rows = (
            session.execute(select(Extraction).where(Extraction.document_version_id == version_id))
            .scalars()
            .all()
        )
    return " ".join(r.text_blob or "" for r in rows)


def test_candidates_extract_fields_with_locators(workspace, tmp_path):
    version_id = _import_one(workspace, tmp_path)
    with workspace.database.read_only() as session:
        version = session.get(DocumentVersion, version_id)
        candidates = build_ddr_candidates(version, _text_for(workspace, version_id))
    by_field = {c.field: c for c in candidates}
    assert by_field["report_number"].extracted_value == "SYN-1"
    assert by_field["report_number"].locator["line"] >= 1
    assert by_field["depth_from_m"].extracted_value == "100"
    assert by_field["depth_to_m"].extracted_value == "200"
    assert all(c.status == "PENDING" for c in candidates)


def test_ambiguous_field_is_unresolved_not_guessed(workspace, tmp_path):
    text = "Daily Drilling Report SYNTHETIC. Report No. AAA-1. Report No. BBB-2. Depth from 10 to 20 m."
    version_id = _import_one(workspace, tmp_path, text)
    with workspace.database.read_only() as session:
        version = session.get(DocumentVersion, version_id)
        candidates = build_ddr_candidates(version, _text_for(workspace, version_id))
    rn = next(c for c in candidates if c.field == "report_number")
    assert rn.status == "UNRESOLVED" and rn.confidence == 0.0


def test_disposition_and_authorize_keep_candidate_until_review(workspace, tmp_path):
    version_id = _import_one(workspace, tmp_path)
    with workspace.database.unit_of_work() as session:
        well = WellRepository(session).create_well(name="CAND-01")
        version = session.get(DocumentVersion, version_id)
        candidates = build_ddr_candidates(version, _text_for(workspace, version_id))
        by_field = {c.field: c for c in candidates}
        record_disposition(session, version, by_field["report_number"], "ACCEPTED", actor="op-1")
        record_disposition(
            session, version, by_field["depth_from_m"], "EDITED", actor="op-1", edited_value="101"
        )
        record_disposition(session, version, by_field["well"], "REJECTED", actor="op-1")
        values = accepted_values(version)
        assert values["report_number"] == "SYN-1"
        assert values["depth_from_m"] == "101"  # edited value preserved separately
        assert "well" not in values  # rejected
        report = authorize_report(session, version_id=version_id, well_id=well.id, actor="op-1")
        assert report.status == "CANDIDATE"
        assert report.report_number == "SYN-1"
        assert report.document_version_id == version_id
        # the exact-version review queue sees the recorded disposition (not pending)
        registry = DocumentRegistry(
            DocumentRepository(session),
            router=build_default_router(workspace.settings),
            settings=workspace.settings,
        )
        queue = review_queue(registry, workspace_id=_workspace_row_id(workspace))
        entry = next(q for q in queue if q["document_version_id"] == version_id)
        assert entry["pending_review"] is False
        assert entry["version_disposition"] in ("ACCEPTED", "EDITED", "REJECTED")


def test_unknown_version_refused(workspace, tmp_path):
    with workspace.database.unit_of_work() as session:
        well = WellRepository(session).create_well(name="CAND-02")
        with pytest.raises(ValidationError, match="unknown document version"):
            authorize_report(session, version_id="ver-nope", well_id=well.id, actor="op-1")


def test_invalid_decision_refused(workspace, tmp_path):
    version_id = _import_one(workspace, tmp_path)
    with workspace.database.unit_of_work() as session:
        version = session.get(DocumentVersion, version_id)
        cand = build_ddr_candidates(version, _text_for(workspace, version_id))[0]
        with pytest.raises(ValidationError, match="decision"):
            record_disposition(session, version, cand, "MAYBE", actor="op-1")
        with pytest.raises(ValidationError, match="edited value"):
            record_disposition(session, version, cand, "EDITED", actor="op-1", edited_value="  ")
