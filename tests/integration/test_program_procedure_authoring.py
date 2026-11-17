"""Public-domain authoring coverage for structured Program sections and Procedure steps."""

from __future__ import annotations

import pytest

from drilling_intelligence.core.errors import ValidationError
from drilling_intelligence.engineering.authoring import EngineeringAuthoringService
from drilling_intelligence.engineering.repository import EngineeringRepository
from drilling_intelligence.wells.repository import WellRepository


@pytest.fixture
def hierarchy(session):
    repository = WellRepository(session)
    project = repository.get_or_create_project("Authoring Fixture Block")
    field = repository.get_or_create_field("Authoring Fixture Field", project=project)
    well = repository.create_well("AUTHORING-01", project_id=project.id, field_id=field.id)
    return {"project": project, "field": field, "well_a": well}


def _stamp(row) -> str:
    return row.updated_at.isoformat()


def test_program_sections_targets_review_revision_and_compare(session, hierarchy) -> None:
    repo = EngineeringRepository(session)
    service = EngineeringAuthoringService(session)
    program = repo.create_program(
        code="W-17-DRP",
        title="Well W-17 drilling program",
        author="author-a",
        created_by="author-a",
        well_id=hierarchy["well_a"].id,
    )
    first = service.add_program_section(
        program.id,
        expected_updated_at=_stamp(program),
        section_key="design_basis",
        title="Design basis",
        content="Source-entered synthetic fixture basis.",
    )
    program = repo.get_program(program.id)
    second = service.add_program_section(
        program.id,
        expected_updated_at=_stamp(program),
        section_key="well_prognosis",
        title="Well prognosis",
        content="No geological values supplied in this fixture.",
    )
    program = repo.get_program(program.id)
    target = service.add_program_target(
        program.id,
        expected_updated_at=_stamp(program),
        name="12 1/4 in",
        hole_size_in=12.25,
        planned_depth_md_value=1800.0,
        planned_depth_md_unit="m",
    )
    assert target.planned_depth_md_value == 1800.0
    assert service.validate_program(program.id)["ready_for_review"] is True

    program = repo.get_program(program.id)
    reordered = service.reorder_program_sections(
        program.id, expected_updated_at=_stamp(program), ordered_ids=[second.id, first.id]
    )
    assert [row.id for row in reordered] == [second.id, first.id]
    program = repo.get_program(program.id)
    service.submit("program", program.id, expected_updated_at=_stamp(program), by="author-a")
    program = repo.get_program(program.id)
    with pytest.raises(ValidationError, match="cannot also be its reviewer"):
        service.record_review(
            "program",
            program.id,
            expected_updated_at=_stamp(program),
            by="author-a",
            note="same person",
        )
    program = repo.get_program(program.id)
    service.record_review(
        "program",
        program.id,
        expected_updated_at=_stamp(program),
        by="reviewer-b",
        note="Reviewed against supplied references; no calculations assessed.",
    )
    program = repo.get_program(program.id)
    service.decide(
        "program",
        program.id,
        expected_updated_at=_stamp(program),
        by="reviewer-b",
        approved=True,
        reason="Structural review complete.",
    )
    assert str(repo.get_program(program.id).status) == "APPROVED"

    program = repo.get_program(program.id)
    revised = repo.revise_program(program.id, by="author-c", changes={"author": "author-c"})
    assert str(revised.status) == "DRAFT"
    assert revised.approver is None and revised.approved_at is None
    assert [(s.sequence, s.section_key) for s in repo.list_program_sections(revised.id)] == [
        (1, "well_prognosis"),
        (2, "design_basis"),
    ]
    assert len(repo.list_targets(revised.id)) == 1
    comparison = service.compare_programs(program.id, revised.id)
    assert comparison["engineering_significance_assessed"] is False


def test_program_target_units_are_explicit_and_stale_writes_are_refused(session, hierarchy) -> None:
    repo = EngineeringRepository(session)
    service = EngineeringAuthoringService(session)
    program = repo.create_program(
        code="W-17-DRP2", title="Program", author="a", well_id=hierarchy["well_a"].id
    )
    stamp = _stamp(program)
    with pytest.raises(ValidationError, match="explicitly supplied"):
        service.add_program_target(
            program.id, expected_updated_at=stamp, planned_depth_md_value=2000.0, name="section"
        )
    service.add_program_section(
        program.id,
        expected_updated_at=stamp,
        section_key="basis",
        title="Basis",
        content="Explicit source fixture.",
    )
    with pytest.raises(ValidationError, match="stale"):
        service.add_program_section(
            program.id,
            expected_updated_at=stamp,
            section_key="stale",
            title="Stale",
            content="Must not be saved.",
        )


def test_procedure_structured_steps_edit_reorder_review_and_revision(session, hierarchy) -> None:
    repo = EngineeringRepository(session)
    service = EngineeringAuthoringService(session)
    procedure = repo.create_procedure(
        code="W-17-P-01",
        title="Synthetic trip procedure",
        procedure_type="tripping",
        description="Fixture-only example; not an operational instruction.",
        created_by="author-a",
        well_id=hierarchy["well_a"].id,
    )
    first = service.add_procedure_step(
        procedure.id,
        expected_updated_at=_stamp(procedure),
        title="Pre-job check",
        instructions="Confirm the approved checklist supplied by the operator.",
        responsible_role="Driller",
        acceptance_criteria="Checklist status recorded.",
        hold_point=True,
        safety_controls="Use the referenced approved control document.",
        source_reference="synthetic-fixture:procedure-step-1",
    )
    procedure = repo.get_procedure(procedure.id)
    second = service.add_procedure_step(
        procedure.id,
        expected_updated_at=_stamp(procedure),
        title="Record status",
        instructions="Record the current status from the source record.",
        responsible_role="Toolpusher",
        acceptance_criteria="Status and source recorded.",
    )
    procedure = repo.get_procedure(procedure.id)
    service.update_procedure_step(
        procedure.id,
        second.id,
        expected_updated_at=_stamp(procedure),
        changes={"evidence_required": "Source record reference"},
    )
    procedure = repo.get_procedure(procedure.id)
    rows = service.reorder_procedure_steps(
        procedure.id, expected_updated_at=_stamp(procedure), ordered_ids=[second.id, first.id]
    )
    assert [row.id for row in rows] == [second.id, first.id]
    assert service.validate_procedure(procedure.id)["ready_for_review"] is True

    procedure = repo.get_procedure(procedure.id)
    service.submit("procedure", procedure.id, expected_updated_at=_stamp(procedure), by="author-a")
    procedure = repo.get_procedure(procedure.id)
    service.record_review(
        "procedure",
        procedure.id,
        expected_updated_at=_stamp(procedure),
        by="reviewer-b",
        note="Structure checked; this is not an operational approval claim.",
    )
    procedure = repo.get_procedure(procedure.id)
    service.decide(
        "procedure",
        procedure.id,
        expected_updated_at=_stamp(procedure),
        by="reviewer-b",
        approved=True,
        reason="Reviewed fixture content.",
    )
    procedure = repo.get_procedure(procedure.id)
    revised = repo.revise_procedure(procedure.id, by="author-c")
    copied_steps = repo.list_procedure_steps(revised.id)
    assert [(row.sequence, row.title) for row in copied_steps] == [
        (1, "Record status"),
        (2, "Pre-job check"),
    ]
    assert all(row.id not in {first.id, second.id} for row in copied_steps)
    assert str(revised.status) == "DRAFT"
    assert revised.approved_by is None and revised.approved_at is None
    result = service.compare_procedures(procedure.id, revised.id)
    assert result["step_changes"] == []
    assert result["engineering_significance_assessed"] is False


def test_procedure_cannot_submit_incomplete_or_edit_after_submission(session, hierarchy) -> None:
    repo = EngineeringRepository(session)
    service = EngineeringAuthoringService(session)
    procedure = repo.create_procedure(
        title="Incomplete", description="", created_by="author-a", well_id=hierarchy["well_a"].id
    )
    checklist = service.validate_procedure(procedure.id)
    assert checklist["ready_for_review"] is False
    assert "purpose_and_scope" in checklist["missing"]
    with pytest.raises(ValidationError, match="incomplete"):
        service.submit(
            "procedure", procedure.id, expected_updated_at=_stamp(procedure), by="author-a"
        )


def test_authoring_rejects_invalid_updates_before_persistence(session, hierarchy) -> None:
    repo = EngineeringRepository(session)
    service = EngineeringAuthoringService(session)
    program = repo.create_program(
        code="W-17-VALIDATION",
        title="Validation fixture",
        author="a",
        well_id=hierarchy["well_a"].id,
    )
    target = service.add_program_target(
        program.id,
        expected_updated_at=_stamp(program),
        name="Synthetic target",
        planned_depth_md_value=1000.0,
        planned_depth_md_unit="m",
    )
    program = repo.get_program(program.id)
    with pytest.raises(ValidationError, match="unit"):
        service.update_program_target(
            program.id,
            target.id,
            expected_updated_at=_stamp(program),
            changes={"planned_depth_md_unit": ""},
        )
    with pytest.raises(ValidationError, match="explicitly supplied"):
        service.update_program_target(
            program.id,
            target.id,
            expected_updated_at=_stamp(program),
            changes={"planned_depth_md_value": 1200.0},
        )
    cleared = service.update_program_target(
        program.id,
        target.id,
        expected_updated_at=_stamp(program),
        changes={"planned_depth_md_value": None},
    )
    assert cleared.planned_depth_md_value is None
    assert cleared.planned_depth_md_unit == ""

    program = repo.get_program(program.id)
    with pytest.raises(ValidationError, match="completion_state"):
        service.add_program_section(
            program.id,
            expected_updated_at=_stamp(program),
            section_key="basis",
            title="Basis",
            completion_state="APPROVED",
        )
    section = service.add_program_section(
        program.id,
        expected_updated_at=_stamp(program),
        section_key="basis",
        title="Basis",
        content="Synthetic source fixture.",
    )
    program = repo.get_program(program.id)
    with pytest.raises(ValidationError, match="title"):
        service.update_program_section(
            program.id,
            section.id,
            expected_updated_at=_stamp(program),
            changes={"title": None},
        )

    procedure = repo.create_procedure(
        title="Validation procedure",
        description="Synthetic scope",
        created_by="author",
        well_id=hierarchy["well_a"].id,
    )
    step = service.add_procedure_step(
        procedure.id,
        expected_updated_at=_stamp(procedure),
        title="Step",
        instructions="Synthetic instructions.",
        parameters=[{"name": "source quantity", "value": None, "unit": "operator-defined"}],
    )
    procedure = repo.get_procedure(procedure.id)
    with pytest.raises(ValidationError, match="instructions"):
        service.update_procedure_step(
            procedure.id,
            step.id,
            expected_updated_at=_stamp(procedure),
            changes={"instructions": None},
        )
