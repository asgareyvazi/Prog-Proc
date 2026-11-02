"""Intake registration uses the document registry and remains separate from promotion."""

from __future__ import annotations

import pytest
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from drilling_intelligence.core.errors import ValidationError
from drilling_intelligence.database.models import (
    AuditEvent,
    DdrReport,
    Document,
    DocumentVersion,
    IntakeQueueItem,
    IntakeRegistrationRecord,
    NptRecord,
    WellOperation,
)
from drilling_intelligence.review.intake import IntakeQueueService


def test_reviewed_intake_registers_measured_version_without_promotion(
    db, workspace, committed_well_id, tmp_path
) -> None:
    source = tmp_path / "handover.txt"
    source.write_text(
        "Daily drilling report\nOperator wording stays source text.\n", encoding="utf-8"
    )
    service = IntakeQueueService(db, workspace_root=workspace.root, settings=workspace.settings)
    queued = service.enqueue([source], actor="intake-operator")[0]
    preview = service.preview(queued.id)
    assert preview.item.sha256 == queued.sha256
    assert preview.classification

    reviewed = service.decide(
        queued.id,
        decision="REVIEWED",
        actor="reviewer-1",
        reason="Source and ownership inspected",
    )
    assert reviewed.status == "REVIEWED"
    registered = service.register(
        queued.id,
        actor="registrar-2",
        reason="Register reviewed handover into authoritative registry",
        well_id=committed_well_id,
    )
    assert registered.item.status == "REGISTERED"
    assert registered.item.registered_by == "registrar-2"
    assert registered.item.registered_well_id == committed_well_id
    assert registered.item.document_id
    assert registered.item.document_version_id

    again = service.register(
        queued.id,
        actor="registrar-3",
        reason="Idempotent retry",
        well_id=committed_well_id,
    )
    assert again.already_registered is True
    assert again.item.document_id == registered.item.document_id
    assert again.item.document_version_id == registered.item.document_version_id

    with db.read_only() as session:
        document = session.get(Document, registered.item.document_id)
        version = session.get(DocumentVersion, registered.item.document_version_id)
        queue_row = session.get(IntakeQueueItem, queued.id)
        registration_record = session.get(IntakeRegistrationRecord, queued.id)
        audit = list(
            session.scalars(
                select(AuditEvent)
                .where(
                    AuditEvent.subject_type == "intake_queue_item",
                    AuditEvent.subject_id == queued.id,
                )
                .order_by(AuditEvent.at)
            )
        )
        assert session.scalars(select(DdrReport)).all() == []
        assert session.scalars(select(WellOperation)).all() == []
        assert session.scalars(select(NptRecord)).all() == []
    assert document is not None and document.well_id == committed_well_id
    assert version is not None and version.sha256 == queued.sha256
    assert queue_row is not None and queue_row.status == "REVIEWED"
    assert registration_record is not None
    assert registration_record.document_id == registered.item.document_id
    assert registration_record.document_version_id == registered.item.document_version_id
    assert [row.action for row in audit] == [
        "intake.queued",
        "intake.reviewed",
        "intake.registered",
    ]
    assert audit[-1].detail["promotion_performed"] is False

    # The registration FK must preserve its queue provenance, not cascade it away.
    with pytest.raises(IntegrityError), db.unit_of_work() as session:
        persisted_item = session.get(IntakeQueueItem, queued.id)
        assert persisted_item is not None
        session.delete(persisted_item)
        session.flush()
    with db.read_only() as session:
        assert session.get(IntakeQueueItem, queued.id) is not None
        assert session.get(IntakeRegistrationRecord, queued.id) is not None


def test_stale_or_unreviewed_intake_cannot_register(
    db, workspace, committed_well_id, tmp_path
) -> None:
    source = tmp_path / "changed.txt"
    source.write_text("Original bytes", encoding="utf-8")
    service = IntakeQueueService(db, workspace_root=workspace.root, settings=workspace.settings)
    queued = service.enqueue([source], actor="operator")[0]
    with pytest.raises(ValidationError, match="only a REVIEWED"):
        service.register(
            queued.id, actor="registrar", reason="not yet reviewed", well_id=committed_well_id
        )

    service.decide(queued.id, decision="REVIEWED", actor="reviewer", reason="review completed")
    source.write_text("Changed after the human reviewed the first digest", encoding="utf-8")
    with pytest.raises(ValidationError, match="source changed after intake"):
        service.register(
            queued.id,
            actor="registrar",
            reason="stale file must fail closed",
            well_id=committed_well_id,
        )

    with db.read_only() as session:
        row = session.get(IntakeQueueItem, queued.id)
        assert row is not None and row.status == "REVIEWED"
        assert session.scalars(select(Document)).all() == []
