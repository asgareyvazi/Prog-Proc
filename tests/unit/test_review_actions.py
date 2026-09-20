"""Governed review actions stay immutable, stale-safe and delegated to domain owners."""

from __future__ import annotations

import pytest

from drilling_intelligence.database.serialize import record_to_dict
from drilling_intelligence.engineering.repository import EngineeringRepository
from drilling_intelligence.review import (
    ReviewActionRequest,
    ReviewActionService,
    ReviewRecord,
)


def test_capabilities_are_derived_and_do_not_offer_historical_or_unbacked_approval(
    workspace,
) -> None:
    service = ReviewActionService.for_workspace(workspace)
    backed_lesson = ReviewRecord(
        record_type="lesson_learned",
        record_id="les-1",
        status="DRAFT",
        current=True,
        provenance=({"locator": "lesson.xlsx!B4"},),
    )
    actions = service.available_actions(backed_lesson)
    assert {action.action_id for action in actions} == {
        "submit_for_review",
        "approve",
        "reject",
    }
    approval = next(action for action in actions if action.action_id == "approve")
    assert approval.requires_actor is True
    assert approval.requires_evidence is True
    assert approval.requires_independent_actor is True

    historical = ReviewRecord(
        record_type="lesson_learned",
        record_id="les-old",
        status="APPROVED",
        current=False,
        provenance=backed_lesson.provenance,
    )
    assert service.available_actions(historical) == ()

    unbacked = ReviewRecord(
        record_type="lesson_learned",
        record_id="les-2",
        status="DRAFT",
        current=True,
    )
    assert "approve" not in {action.action_id for action in service.available_actions(unbacked)}


def test_action_request_freezes_scope_and_requires_displayed_status() -> None:
    request = ReviewActionRequest(
        record_type="procedure",
        record_id="proc-1",
        action="submit-for-review",
        actor="reviewer",
        expected_status="draft",
        expected_scope={"well_id": "well-1"},
    )
    assert request.record_type == "procedure_record"
    assert request.action == "submit_for_review"
    assert request.expected_status == "DRAFT"
    with pytest.raises(TypeError):
        request.expected_scope["well_id"] = "well-2"  # type: ignore[index]
    with pytest.raises(ValueError, match="expected_status"):
        ReviewActionRequest(
            record_type="procedure_record",
            record_id="proc-1",
            action="approve",
            actor="reviewer",
            expected_status="",
        )


def test_action_rechecks_status_and_routes_to_the_existing_procedure_repository(workspace) -> None:
    with workspace.database.unit_of_work() as session:
        procedure = EngineeringRepository(session).create_procedure(
            code="ACT-001", title="Action boundary procedure", created_by="author"
        )
        procedure_id = str(procedure.id)
        before = record_to_dict(procedure)

    service = ReviewActionService.for_workspace(workspace)
    submitted = service.execute(
        ReviewActionRequest(
            record_type="procedure_record",
            record_id=procedure_id,
            action="submit_for_review",
            actor="reviewer",
            expected_status=before["status"],
            expected_revision=before["revision"],
            expected_current=before["is_current"],
            expected_updated_at=before["updated_at"],
        )
    )
    assert submitted.status == "IN_REVIEW"
    assert submitted.changed is True

    with workspace.database.read_only() as session:
        current = record_to_dict(EngineeringRepository(session).get_procedure(procedure_id))

    approved = service.execute(
        ReviewActionRequest(
            record_type="procedure_record",
            record_id=procedure_id,
            action="approve",
            actor="approver",
            reason="reviewed against the cited procedure",
            expected_status=current["status"],
            expected_revision=current["revision"],
            expected_current=current["is_current"],
            expected_updated_at=current["updated_at"],
        )
    )
    assert approved.status == "APPROVED"

    with pytest.raises(Exception, match="stale"):
        service.execute(
            ReviewActionRequest(
                record_type="procedure_record",
                record_id=procedure_id,
                action="approve",
                actor="second-reviewer",
                expected_status="IN_REVIEW",
                expected_revision=current["revision"],
                expected_current=current["is_current"],
                expected_updated_at=current["updated_at"],
            )
        )

    with workspace.database.read_only() as session:
        row = EngineeringRepository(session).get_procedure(procedure_id)
        assert row.approved_by == "approver"
        assert row.approved_at is not None


def test_domain_approval_retries_preserve_attribution_and_time(workspace) -> None:
    from drilling_intelligence.engineering.repository import EngineeringRepository

    with workspace.database.unit_of_work() as session:
        repository = EngineeringRepository(session)
        procedure = repository.create_procedure(title="Retry-safe procedure")
        repository.approve_procedure(procedure.id, by="first-reviewer", note="accepted")
        procedure_by = procedure.approved_by
        procedure_at = procedure.approved_at
        repository.approve_procedure(procedure.id, by="second-reviewer", note="retry")
        assert procedure.approved_by == procedure_by
        assert procedure.approved_at == procedure_at

        program = repository.create_program(title="Retry-safe program")
        repository.approve_program(program.id, by="first-reviewer", note="accepted")
        program_by = program.approver
        program_at = program.approved_at
        repository.approve_program(program.id, by="second-reviewer", note="retry")
        assert program.approver == program_by
        assert program.approved_at == program_at
