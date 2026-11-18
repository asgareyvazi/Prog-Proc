"""Controlled export (JSON/HTML/DOCX/PDF) and structure-only templates for authoring."""

from __future__ import annotations

import json

import pytest

from drilling_intelligence.core.errors import ValidationError
from drilling_intelligence.engineering.authoring import EngineeringAuthoringService
from drilling_intelligence.engineering.export import export_record
from drilling_intelligence.engineering.repository import EngineeringRepository
from drilling_intelligence.engineering.templates import (
    PROCEDURE_TEMPLATES,
    PROGRAM_TEMPLATES,
    create_from_template,
    list_templates,
)
from drilling_intelligence.wells.repository import WellRepository


@pytest.fixture
def hierarchy(session):
    repository = WellRepository(session)
    project = repository.get_or_create_project("Export Fixture Block")
    field = repository.get_or_create_field("Export Fixture Field", project=project)
    well = repository.create_well("EXPORT-01", project_id=project.id, field_id=field.id)
    return {"project": project, "field": field, "well": well}


def _stamp(row) -> str:
    return row.updated_at.isoformat()


def _make_program(repo, service, hierarchy) -> object:
    program = repo.create_program(
        code="EXP-DRP",
        title="Export fixture program",
        summary="Fixture summary.",
        author="author-a",
        created_by="author-a",
        well_id=hierarchy["well"].id,
    )
    service.add_program_section(
        program.id,
        expected_updated_at=_stamp(program),
        section_key="design_basis",
        title="Design basis",
        content="Fixture design basis text.",
        completion_state="SOURCE_PROVIDED",
        source_reference="FIXTURE-SRC-1",
    )
    return repo.get_program(program.id)


def test_export_json_html_docx_pdf_are_no_clobber_and_draft_watermarked(
    session, hierarchy, tmp_path
) -> None:
    repo = EngineeringRepository(session)
    service = EngineeringAuthoringService(session)
    program = _make_program(repo, service, hierarchy)

    results = {}
    for fmt in ("json", "html", "docx", "pdf"):
        out = tmp_path / f"program.{fmt}"
        results[fmt] = export_record(service, "program", program.id, fmt=fmt, out_path=out)
        assert out.exists() and out.stat().st_size > 0
        assert results[fmt]["watermarked_draft"] is True
        assert results[fmt]["status"] == "DRAFT"

    payload = json.loads((tmp_path / "program.json").read_text())
    assert payload["record_type"] == "program"
    assert payload["id"] == program.id
    assert payload["sections"][0]["section_key"] == "design_basis"
    assert "DRAFT - NOT APPROVED" in payload["export_notice"]
    assert "not authenticated identities" in payload["attribution_notice"]

    html_text = (tmp_path / "program.html").read_text()
    assert "DRAFT - NOT APPROVED" in html_text
    assert "Export fixture program" in html_text

    # No-clobber: exporting again to the same path must refuse.
    with pytest.raises(ValidationError, match="no-clobber"):
        export_record(
            service, "program", program.id, fmt="json", out_path=tmp_path / "program.json"
        )

    # Unsupported format refused.
    with pytest.raises(ValidationError, match="unsupported export format"):
        export_record(service, "program", program.id, fmt="exe", out_path=tmp_path / "x.exe")


def test_export_procedure_includes_link_status(session, hierarchy, tmp_path) -> None:
    repo = EngineeringRepository(session)
    service = EngineeringAuthoringService(session)
    procedure = repo.create_procedure(
        code="EXP-PROC",
        title="Export fixture procedure",
        created_by="author-a",
        well_id=hierarchy["well"].id,
    )
    service.add_procedure_step(
        procedure.id,
        expected_updated_at=_stamp(procedure),
        title="Notify",
        instructions="Fixture instruction text.",
        responsible_role="driller",
        acceptance_criteria="Fixture acceptance text.",
    )
    result = export_record(
        service, "procedure", procedure.id, fmt="json", out_path=tmp_path / "proc.json"
    )
    payload = json.loads((tmp_path / "proc.json").read_text())
    assert payload["record_type"] == "procedure"
    assert payload["steps"][0]["title"] == "Notify"
    assert payload["program_link"]["link_status"] == "NONE"
    assert result["watermarked_draft"] is True


def test_program_template_creates_structure_only_draft(session, hierarchy) -> None:
    repo = EngineeringRepository(session)
    service = EngineeringAuthoringService(session)
    record = create_from_template(
        service,
        "program",
        "standard_drilling_program",
        content={
            "title": "Templated program",
            "author": "author-a",
            "well_id": hierarchy["well"].id,
            "code": "TPL-DRP",
        },
    )
    assert str(record.status) == "DRAFT"
    assert record.origin == "TEMPLATE"
    sections = repo.list_program_sections(record.id)
    expected = PROGRAM_TEMPLATES["standard_drilling_program"]["sections"]
    assert [s.title for s in sections] == [title for _key, title in expected]
    for section in sections:
        assert section.completion_state == "OWNER_DECISION_REQUIRED"
        assert section.content == ""
    # Structural validation must NOT report ready: no real content was supplied.
    assert service.validate_program(record.id)["ready_for_review"] is False


def test_procedure_template_creates_placeholder_steps_only(session, hierarchy) -> None:
    repo = EngineeringRepository(session)
    service = EngineeringAuthoringService(session)
    record = create_from_template(
        service,
        "procedure",
        "standard_operational_procedure",
        content={
            "title": "Templated procedure",
            "created_by": "author-a",
            "well_id": hierarchy["well"].id,
        },
    )
    steps = repo.list_procedure_steps(record.id)
    assert len(steps) == len(PROCEDURE_TEMPLATES["standard_operational_procedure"]["steps"])
    for step in steps:
        assert "PLACEHOLDER" in step.instructions
        assert step.responsible_role in (None, "")
        assert step.acceptance_criteria in (None, "")
    assert service.validate_procedure(record.id)["ready_for_review"] is False


def test_unknown_template_is_refused(session) -> None:
    service = EngineeringAuthoringService(session)
    with pytest.raises(ValidationError, match="unknown program template"):
        create_from_template(
            service,
            "program",
            "no_such_template",
            content={"title": "x", "author": "a"},
        )


def test_list_templates_covers_both_kinds() -> None:
    programs = list_templates("programs")
    procedures = list_templates("procedures")
    assert "standard_drilling_program" in programs
    assert "standard_operational_procedure" in procedures
    with pytest.raises(ValidationError, match="must be 'program' or 'procedure'"):
        list_templates("widgets")
