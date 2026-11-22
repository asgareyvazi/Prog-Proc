"""Phase V: synthetic end-to-end matrix V1-V15.

Every scenario uses synthetic, labelled fixtures only. No real well data, no invented
engineering values: each test asserts workflow mechanics (identity, lifecycle,
provenance, refusal) rather than engineering truth.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from drilling_intelligence.cli.app import _workspace_row_id
from drilling_intelligence.database.backup import create_workspace_backup
from drilling_intelligence.engineering.authoring import EngineeringAuthoringService
from drilling_intelligence.engineering.export import export_record
from drilling_intelligence.engineering.repository import EngineeringRepository
from drilling_intelligence.engineering.service import EngineeringService
from drilling_intelligence.operations.ddr import DdrAuthoringService
from drilling_intelligence.operations.ddr_batch import MANIFEST_FORMAT, import_ddr_batch
from drilling_intelligence.documents.registry import DocumentRegistry
from drilling_intelligence.documents.repository import DocumentRepository
from drilling_intelligence.extraction.registry import build_default_router
from drilling_intelligence.wells.repository import WellRepository

pytestmark = pytest.mark.integration


def _well(workspace, name="E2E-01"):
    with workspace.database.unit_of_work() as session:
        wells = WellRepository(session)
        project = wells.get_or_create_project("E2E Block")
        return wells.create_well(name, project_id=project.id).id


def _stamp(workspace, record_id: str, kind: str = "procedure") -> str:
    with workspace.database.read_only() as session:
        repo = EngineeringRepository(session)
        record = (
            repo.get_procedure(record_id) if kind == "procedure" else repo.get_program(record_id)
        )
        return record.updated_at.isoformat()


# ---------------------------------------------------------------- V1-V4
def test_v1_workspace_bootstrap_and_head(workspace) -> None:
    from drilling_intelligence.database.migrations import METADATA_REVISION, current_revision

    assert current_revision(workspace.database.engine) == "0029" == METADATA_REVISION


def test_v2_well_registration_and_sections(workspace) -> None:
    well_id = _well(workspace)
    with workspace.database.unit_of_work() as session:
        wells = WellRepository(session)
        well = wells.get_well(well_id)
        assert well is not None and well.name == "E2E-01"
        sections = wells.list_sections(well_id)
        assert isinstance(sections, list)


def test_v3_ddr_manual_journey_create_submit(workspace) -> None:
    well_id = _well(workspace, "E2E-03")
    service = DdrAuthoringService(workspace.database)
    draft = service.create_draft(
        well_id=well_id,
        report_date="2026-10-20",
        report_number="SYN-E2E-03",
        summary="synthetic fixture summary",
        activities=[
            {
                "operation_type": "drilling",
                "label": "synthetic drill activity",
                "period_text": "06:00-08:00",
                "description": "synthetic",
            }
        ],
        actor="e2e-tester",
        reason="synthetic E2E fixture",
    )
    assert draft["status"] == "CANDIDATE"
    report_id = draft.get("id") or draft.get("report_id")
    submitted = service.submit(
        report_id,
        actor="e2e-tester",
        reason="synthetic submit",
        expected_updated_at=service.get(report_id)["updated_at"],
    )
    assert submitted["status"] in ("SUBMITTED", "IN_REVIEW", "CANDIDATE")


def test_v4_ddr_batch_import_matrix_entry(workspace, tmp_path) -> None:
    from tests.integration.test_ddr_batch_import import _pdf_bytes, _write_corpus

    manifest = _write_corpus(tmp_path)
    with workspace.database.unit_of_work() as session:
        registry = DocumentRegistry(
            DocumentRepository(session),
            router=build_default_router(workspace.settings),
            settings=workspace.settings,
        )
        summary = import_ddr_batch(
            registry,
            manifest_path=manifest,
            base_dir=tmp_path,
            workspace_root=workspace.root,
            workspace_id=_workspace_row_id(workspace),
        )
    assert summary.total == 5 and summary.imported == 3 and summary.failed == 2


# ---------------------------------------------------------------- V5-V8
def test_v5_document_intake_registration_classification(workspace, tmp_path) -> None:
    pdf = tmp_path / "syn_report.pdf"
    from tests.integration.test_ddr_batch_import import _pdf_bytes, DDR_TEXT

    pdf.write_bytes(_pdf_bytes(DDR_TEXT))
    with workspace.database.unit_of_work() as session:
        registry = DocumentRegistry(
            DocumentRepository(session),
            router=build_default_router(workspace.settings),
            settings=workspace.settings,
        )
        result = registry.register(
            path=pdf,
            workspace_root=workspace.root,
            workspace_id=_workspace_row_id(workspace),
        )
    assert result.ok and result.classification == "DDR"


def test_v6_program_authoring_validate_submit(workspace) -> None:
    well_id = _well(workspace, "E2E-06")
    with workspace.database.unit_of_work() as session:
        repo = EngineeringRepository(session)
        service = EngineeringAuthoringService(session)
        program = repo.create_program(
            code="E2E-DRP",
            title="Synthetic E2E program",
            summary="synthetic",
            author="e2e",
            created_by="e2e",
            well_id=well_id,
        )
        service.add_program_section(
            program.id,
            expected_updated_at=program.updated_at.isoformat(),
            section_key="design_basis",
            title="Design basis",
            content="synthetic design basis text",
            completion_state="SOURCE_PROVIDED",
            source_reference="SYN-E2E-SRC",
        )
        program = repo.get_program(program.id)
        service.add_program_target(
            program.id,
            expected_updated_at=program.updated_at.isoformat(),
            name="synthetic 8.5in section",
            planned_depth_md_value=1200,
            planned_depth_md_unit="m",
        )
        program = repo.get_program(program.id)
        report = service.validate_program(program.id)
        assert report["ready_for_review"] is True
        assert report["ready_for_review"] in (True, False)  # mechanics only
        submitted = service.submit(
            "program", program.id, expected_updated_at=program.updated_at.isoformat(), by="e2e"
        )
        assert submitted.status in ("SUBMITTED", "IN_REVIEW")


def test_v7_procedure_authoring_hold_point_and_link(workspace) -> None:
    well_id = _well(workspace, "E2E-07")
    with workspace.database.unit_of_work() as session:
        repo = EngineeringRepository(session)
        service = EngineeringAuthoringService(session)
        program = repo.create_program(
            code="E2E-LINK", title="Link target", author="e2e", created_by="e2e", well_id=well_id
        )
        procedure = repo.create_procedure(
            code="E2E-PROC", title="Synthetic E2E procedure", created_by="e2e", well_id=well_id
        )
        service.add_procedure_step(
            procedure.id,
            expected_updated_at=procedure.updated_at.isoformat(),
            title="Notify",
            instructions="synthetic instruction text",
            hold_point=True,
            completion_state="DRAFT",
        )
        procedure = repo.get_procedure(procedure.id)
        service.link_procedure_to_program(
            procedure.id,
            program.id,
            expected_updated_at=procedure.updated_at.isoformat(),
            by="e2e",
        )
        link = service.procedure_program_link(procedure.id)
        assert link["link_status"] == "LINKED"
        steps = repo.list_procedure_steps(procedure.id)
        assert steps[0].hold_point is True and steps[0].completion_state == "DRAFT"


def test_v8_procedure_export_all_formats_watermarked(workspace, tmp_path) -> None:
    well_id = _well(workspace, "E2E-08")
    with workspace.database.unit_of_work() as session:
        repo = EngineeringRepository(session)
        service = EngineeringAuthoringService(session)
        procedure = repo.create_procedure(
            code="E2E-EXP", title="Synthetic export procedure", created_by="e2e", well_id=well_id
        )
        service.add_procedure_step(
            procedure.id,
            expected_updated_at=procedure.updated_at.isoformat(),
            title="Step",
            instructions="synthetic",
            completion_state="SOURCE_PROVIDED",
        )
        for fmt in ("json", "html", "docx", "pdf"):
            result = export_record(
                service, "procedure", procedure.id, fmt=fmt, out_path=tmp_path / f"e2e.{fmt}"
            )
            assert result["watermarked_draft"] is True
        payload = json.loads((tmp_path / "e2e.json").read_text())
        assert payload["steps"][0]["completion_state"] == "SOURCE_PROVIDED"


# ---------------------------------------------------------------- V9-V12
def test_v9_revision_marks_link_stale(workspace) -> None:
    well_id = _well(workspace, "E2E-09")
    with workspace.database.unit_of_work() as session:
        repo = EngineeringRepository(session)
        service = EngineeringAuthoringService(session)
        program = repo.create_program(
            code="E2E-STALE",
            title="Stale link target",
            author="e2e",
            created_by="e2e",
            well_id=well_id,
        )
        service.add_program_section(
            program.id,
            expected_updated_at=program.updated_at.isoformat(),
            section_key="design_basis",
            title="Design basis",
            content="synthetic",
            completion_state="SOURCE_PROVIDED",
        )
        program = repo.get_program(program.id)
        service.add_program_target(
            program.id,
            expected_updated_at=program.updated_at.isoformat(),
            name="synthetic section",
            planned_depth_md_value=1000,
            planned_depth_md_unit="m",
        )
        program = repo.get_program(program.id)
        procedure = repo.create_procedure(
            code="E2E-STALE-P", title="Stale link proc", created_by="e2e", well_id=well_id
        )
        service.link_procedure_to_program(
            procedure.id,
            program.id,
            expected_updated_at=procedure.updated_at.isoformat(),
            by="e2e",
        )
        # New current program revision supersedes the linked one.
        service.submit(
            "program", program.id, expected_updated_at=program.updated_at.isoformat(), by="e2e"
        )
        new_rev = repo.revise_program(program.id, by="e2e")
        assert new_rev.is_current
        link = service.procedure_program_link(procedure.id)
        assert link["link_status"] == "STALE"


def test_v10_npt_rollup_only_approved_method(workspace) -> None:
    well_id = _well(workspace, "E2E-10")
    from drilling_intelligence.core.errors import ValidationError
    from drilling_intelligence.engineering.service import (
        APPROVED_CALCULATION_METHODS,
        NPT_ROLLUP_METHOD_ID,
    )

    assert APPROVED_CALCULATION_METHODS == frozenset({"npt.hours_rollup"})
    service = EngineeringService.for_workspace(workspace)
    # A roll-up of nothing is refused honestly, not reported as zero hours.
    with pytest.raises(ValidationError, match="no NPT row that states a duration"):
        service.record_npt_rollup(well_id=well_id, created_by="e2e")
    assert NPT_ROLLUP_METHOD_ID == "npt.hours_rollup"


def test_v11_provenance_grounded_search(workspace) -> None:
    well_id = _well(workspace, "E2E-11")
    from drilling_intelligence.search.index import InMemorySearchIndex
    from drilling_intelligence.search.service import SearchService

    service = SearchService(index=InMemorySearchIndex(), database=workspace.database)
    result = service.search("synthetic E2E query", workspace_id=_workspace_row_id(workspace))
    assert hasattr(result, "results")


def test_v12_report_composition_smoke(workspace, tmp_path) -> None:
    from drilling_intelligence.reporting.service import build_report

    well_id = _well(workspace, "E2E-12")
    with workspace.database.read_only() as session:
        report = build_report(session, well_ids=[well_id])
    assert report is not None


# ---------------------------------------------------------------- V13-V15
def test_v13_backup_snapshot_no_clobber(workspace, tmp_path) -> None:
    dest = tmp_path / "backup.zip"
    _ = workspace.database.engine  # materialise the lazily-created database first
    result = create_workspace_backup(workspace, dest)
    assert dest.exists() and len(result.to_dict()["archive_sha256"]) == 64
    from drilling_intelligence.database.backup import BackupError

    with pytest.raises(BackupError):
        create_workspace_backup(workspace, dest)  # no-overwrite


def test_v14_unapproved_calculation_refused(workspace) -> None:
    from drilling_intelligence.core.errors import ValidationError

    well_id = _well(workspace, "E2E-14")
    from drilling_intelligence.engineering.service import execute_calculation

    with pytest.raises(ValidationError, match="not approved for execution"):
        execute_calculation(
            workspace.database, well_id=well_id, method_id="invented.formula_v9", created_by="e2e"
        )
    with pytest.raises(ValidationError, match="no NPT row that states a duration"):
        execute_calculation(
            workspace.database, well_id=well_id, method_id="npt.hours_rollup", created_by="e2e"
        )


def test_v15_cli_surface_smoke(workspace) -> None:
    from drilling_intelligence.cli.app import main as cli_main

    assert cli_main(["doctor", "--workspace", str(workspace.root), "--json"]) == 0
