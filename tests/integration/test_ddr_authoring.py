"""Transactional tests for manual DDR drafts, submission, review, and revisions."""

from __future__ import annotations

import pytest
from sqlalchemy import select

from drilling_intelligence.core.errors import ValidationError
from drilling_intelligence.database.models import AuditEvent, DdrActivity, DdrReport, WellOperation
from drilling_intelligence.operations.ddr import DdrAuthoringService
from drilling_intelligence.review import ReviewActionRequest, ReviewActionService


def _activities() -> list[dict[str, str]]:
    return [
        {
            "operation_type": "drilling",
            "label": "Drilled 12 1/4 in. section",
            "description": "Rotary drilling continued; wording entered by the operator.",
            "period_text": "06:00-18:00",
        },
        {
            "operation_type": "tripping",
            "label": "POOH for bit inspection",
            "description": "No duration or depth is inferred.",
            "period_text": "18:00-20:00",
        },
    ]


def test_ddr_draft_submit_review_and_revision_are_explicit(
    db, workspace, committed_well_id
) -> None:
    service = DdrAuthoringService(db)
    draft = service.create_draft(
        well_id=committed_well_id,
        report_date="2026-10-31",
        report_number="DDR-31",
        shift="Day",
        summary="Operator-entered daily summary.",
        activities=_activities(),
        actor="author-1",
        reason="Create shift report",
    )
    assert draft["status"] == "CANDIDATE"
    assert draft["authoring_state"] == "DRAFT"
    assert draft["report_date_text"] == "2026-10-31"
    assert len(draft["activities"]) == 2
    assert all(not item["operation_id"] for item in draft["activities"])

    action_service = ReviewActionService.for_workspace(workspace)
    assert (
        action_service.available_actions(
            {
                "record_type": "ddr_report",
                "status": "CANDIDATE",
                "data": {"authoring_state": "DRAFT"},
            }
        )
        == ()
    )
    with db.read_only() as session:
        assert session.scalar(select(DdrReport).where(DdrReport.id == draft["report_id"]))
        assert session.scalars(select(WellOperation)).all() == []

    with pytest.raises(ValidationError, match="changed since it was loaded"):
        service.save_draft(
            draft["report_id"],
            expected_updated_at="stale",
            report_date="2026-10-31",
            summary="stale edit",
            actor="author-1",
            reason="stale edit attempt",
        )

    saved = service.save_draft(
        draft["report_id"],
        expected_updated_at=draft["updated_at"],
        report_date="2026-10-31",
        report_number="DDR-31",
        shift="Day",
        summary="Operator-entered daily summary, corrected before submission.",
        activities=_activities(),
        actor="author-1",
        reason="Save revised draft content",
    )
    assert saved["summary"].endswith("corrected before submission.")
    assert len(saved["activities"]) == 2

    submitted = service.submit(
        draft["report_id"],
        expected_updated_at=saved["updated_at"],
        actor="author-1",
        reason="Submit for independent review",
    )
    assert submitted["authoring_state"] == "SUBMITTED"
    assert submitted["status"] == "CANDIDATE"
    with db.read_only() as session:
        operations = list(
            session.scalars(
                select(WellOperation)
                .where(WellOperation.report_id == draft["report_id"])
                .order_by(WellOperation.id)
            )
        )
        activities = list(
            session.scalars(
                select(DdrActivity)
                .where(DdrActivity.ddr_report_id == draft["report_id"])
                .order_by(DdrActivity.sequence)
            )
        )
        audit = list(
            session.scalars(
                select(AuditEvent).where(
                    AuditEvent.subject_type == "ddr_report",
                    AuditEvent.subject_id == draft["report_id"],
                )
            )
        )
    assert len(operations) == len(activities) == 2
    assert {row.status for row in operations} == {"CANDIDATE"}
    assert {row.origin for row in operations} == {"MANUAL"}
    assert all(row.is_current for row in operations)
    assert {item.operation_id for item in activities} == {row.id for row in operations}
    assert {row.action for row in audit} == {
        "ddr.draft_created",
        "ddr.draft_saved",
        "ddr.submitted",
    }
    assert all(row.detail["content_sha256"] for row in audit if "content_sha256" in row.detail)

    actions = action_service.available_actions(
        {
            "record_type": "ddr_report",
            "status": "CANDIDATE",
            "current": True,
            "data": {"authoring_state": "SUBMITTED"},
        }
    )
    assert {item.action_id for item in actions} == {"confirm", "reject"}
    confirmed = action_service.execute(
        ReviewActionRequest(
            record_type="ddr_report",
            record_id=draft["report_id"],
            action="confirm",
            actor="reviewer-2",
            expected_status="CANDIDATE",
            expected_current=True,
            expected_updated_at=submitted["updated_at"],
            expected_scope={"well_id": committed_well_id},
        )
    )
    assert confirmed.status == "CONFIRMED"

    reviewed = service.get(draft["report_id"])
    revision = service.create_revision(
        reviewed["report_id"],
        expected_updated_at=reviewed["updated_at"],
        actor="author-1",
        reason="Corrected shift narrative after review",
    )
    assert revision["revision_group_id"] == draft["revision_group_id"]
    assert revision["revision_number"] == 2
    assert revision["supersedes_report_id"] == draft["report_id"]
    assert revision["authoring_state"] == "DRAFT"
    assert revision["status"] == "CANDIDATE"
    assert revision["is_current"] is True
    assert len(revision["activities"]) == 2
    with db.read_only() as session:
        previous = session.get(DdrReport, draft["report_id"])
        old_operations = list(
            session.scalars(
                select(WellOperation).where(WellOperation.report_id == draft["report_id"])
            )
        )
    assert previous is not None and previous.status == "CONFIRMED" and not previous.is_current
    assert old_operations and all(not row.is_current for row in old_operations)
    historical = service.get(draft["report_id"])
    with pytest.raises(ValidationError, match="historical DDR revision is read-only"):
        service.save_draft(
            draft["report_id"],
            expected_updated_at=historical["updated_at"],
            report_date="2026-10-31",
            actor="author-1",
            reason="late edit",
        )
    from drilling_intelligence.database.migrations import upgrade

    with pytest.raises(RuntimeError, match="cannot downgrade 0026 while authored DDR revisions"):
        upgrade(db.engine, "0025", allow_downgrade=True)


def test_ddr_rejects_unknown_operation_type_and_ambiguous_date(db, committed_well_id) -> None:
    service = DdrAuthoringService(db)
    with pytest.raises(ValidationError, match="registered operation vocabulary"):
        service.create_draft(
            well_id=committed_well_id,
            report_date="2026-10-31",
            activities=[{"operation_type": "guess", "label": "Unclassified work"}],
            actor="author",
            reason="test refusal",
        )
    with pytest.raises(ValidationError, match="YYYY-MM-DD"):
        service.create_draft(
            well_id=committed_well_id,
            report_date="October 31, 2026",
            summary="A report with no canonical day is refused.",
            actor="author",
            reason="test refusal",
        )
    with db.read_only() as session:
        assert session.scalars(select(DdrReport)).all() == []
