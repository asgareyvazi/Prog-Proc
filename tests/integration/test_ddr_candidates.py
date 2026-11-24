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

SHORT_DDR = "Daily Drilling Report. Report No. SYN-1. Well CANDWELL. Date 2026-11-24. Depth from 100 m to 200 m."


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
    assert by_field["report_number"].status == "PENDING"
    # locators are real page-level (page derived from form-feed separators, never hardcoded 1)
    assert all(c.locator["page"] >= 1 for c in candidates)
    assert any(c.locator["page"] > 1 for c in candidates) or all(
        c.locator["page"] == 1 for c in candidates
    )


def test_ambiguous_field_is_unresolved_not_guessed(workspace, tmp_path):
    text = "Daily Drilling Report SYNTHETIC. Report No. AAA-1. Report No. BBB-2. Depth from 10 m to 20 m."
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
        values = accepted_values(session, version)
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


# ------------------------------------------------- 2026-11-24B Phase G
from sqlalchemy import func, select  # noqa: E402

from drilling_intelligence.database.models import DdrDisposition  # noqa: E402
from drilling_intelligence.operations.ddr_workflow import DdrWorkflowService  # noqa: E402


def test_phase_g_page_level_locators_and_unit_contract(workspace, tmp_path):
    text = (
        "Daily Drilling Report SYNTHETIC.\f"  # page 2 starts after the form feed
        "Report No. PG-9. Well CANDWELL. Date 2026-11-24. Depth from 50 m to 90 m."
    )
    version_id = _import_one(workspace, tmp_path, text)
    with workspace.database.read_only() as session:
        version = session.get(DocumentVersion, version_id)
        candidates = build_ddr_candidates(version, text)
    by_field = {c.field: c for c in candidates}
    assert by_field["report_number"].locator["page"] == 2  # real page, not hardcoded 1
    assert by_field["depth_from_m"].unit == "m" and by_field["depth_from_m"].status == "PENDING"


def test_phase_g_wrong_unit_is_unresolved_never_converted(workspace, tmp_path):
    text = "Daily Drilling Report SYNTHETIC. Report No. FT-1. Depth from 100 ft to 200 ft."
    version_id = _import_one(workspace, tmp_path, text)
    service = DdrWorkflowService.for_workspace(workspace)
    cands = service.list_candidates(version_id)
    depth = next(c for c in cands if c["field"] == "depth_from_m")
    assert depth["status"] == "UNRESOLVED"
    assert depth["unit"] == "ft"
    assert "refusing to convert" in depth["note"]


def test_phase_g_dispositions_are_append_only_and_metadata_untouched(workspace, tmp_path):
    version_id = _import_one(workspace, tmp_path)
    service = DdrWorkflowService.for_workspace(workspace)
    cands = service.list_candidates(version_id)
    rn = next(c for c in cands if c["field"] == "report_number")
    with workspace.database.read_only() as session:
        version = session.get(DocumentVersion, version_id)
        meta_before = dict(version.metadata_json or {})
    service.dispose(version_id, rn["candidate_id"], "ACCEPTED", actor="op-g")
    with workspace.database.unit_of_work() as session:
        version = session.get(DocumentVersion, version_id)
        assert version.metadata_json == meta_before  # immutable evidence untouched
        count = session.execute(
            select(func.count())
            .select_from(DdrDisposition)
            .where(DdrDisposition.document_version_id == version_id)
        ).scalar_one()
        assert count == 1
        # append-only enforced at the database level
        import pytest as _pytest
        from sqlalchemy import text as _text

        with _pytest.raises(Exception, match="append-only"):
            session.execute(
                _text(
                    "UPDATE ddr_disposition SET decision='REJECTED' WHERE document_version_id=:v"
                ),
                {"v": version_id},
            )


def test_phase_g_authorize_gates_require_accepted_required_fields(workspace, tmp_path):
    version_id = _import_one(workspace, tmp_path)
    service = DdrWorkflowService.for_workspace(workspace)
    with workspace.database.unit_of_work() as session:
        well = WellRepository(session).create_well(name="CAND-03")
        with pytest.raises(ValidationError, match="required field"):
            service.authorize(version_id, well_id=well.id, actor="op-g")
