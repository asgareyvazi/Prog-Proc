"""Intake-to-human-disposition E2E: real files, real SQLite, no provider or domain writer."""

from __future__ import annotations

from pathlib import Path

import pytest
from sqlalchemy import func, inspect, select

from drilling_intelligence.core.errors import ValidationError
from drilling_intelligence.database.models import AuditEvent, Document, IntakeQueueItem
from drilling_intelligence.review.intake import IntakeQueueService
from drilling_intelligence.wells.workspace import Workspace


@pytest.fixture
def workspace(tmp_path: Path) -> Workspace:
    from drilling_intelligence.cli.app import main

    root = tmp_path / "workspace"
    assert main(["workspace", "create", str(root)]) == 0
    workspace = Workspace.open(root)
    _ = workspace.database
    yield workspace
    workspace.close()


def test_intake_preview_and_review_are_audited_without_registering_domain_rows(
    workspace: Workspace, tmp_path: Path
) -> None:
    source = tmp_path / "daily_drilling_report.txt"
    source.write_text(
        "Daily Drilling Report\nDate: 2026-10-31\nOperations: rotary drilling\n", encoding="utf-8"
    )
    service = IntakeQueueService(workspace.database)
    queued = service.enqueue([source], actor="operator-1")
    assert len(queued) == 1
    item = queued[0]
    assert item.status == "QUEUED"
    assert len(item.sha256) == 64
    assert item.filename == source.name
    assert service.enqueue([source], actor="operator-2")[0].id == item.id
    assert len(service.list_items(status="QUEUED")) == 1

    preview = service.preview(item.id)
    assert preview.item.sha256 == item.sha256
    assert preview.extractor
    assert preview.classification
    assert preview.contract_level
    assert preview.classification_evidence
    assert preview.text_excerpt.startswith("Daily Drilling Report")

    with workspace.database.read_only() as session:
        assert session.scalar(select(func.count()).select_from(Document)) == 0
        events = session.scalars(select(AuditEvent).where(AuditEvent.subject_id == item.id)).all()
        assert [event.action for event in events] == ["intake.queued"]

    decided = service.decide(
        item.id, decision="REVIEWED", actor="reviewer-2", reason="source checked"
    )
    assert decided.status == "REVIEWED"
    assert decided.reviewed_by == "reviewer-2"
    with pytest.raises(ValidationError, match="already REVIEWED"):
        service.decide(item.id, decision="REJECTED", actor="reviewer-2", reason="retry")
    with workspace.database.read_only() as session:
        assert session.scalar(select(func.count()).select_from(Document)) == 0
        events = session.scalars(select(AuditEvent).where(AuditEvent.subject_id == item.id)).all()
        assert [event.action for event in events] == ["intake.queued", "intake.reviewed"]


def test_intake_refuses_unsupported_and_changed_sources(
    workspace: Workspace, tmp_path: Path
) -> None:
    unsupported = tmp_path / "payload.exe"
    unsupported.write_bytes(b"not a supported document")
    service = IntakeQueueService(workspace.database)
    with pytest.raises(ValidationError, match="unsupported intake extension"):
        service.enqueue([unsupported], actor="operator")

    source = tmp_path / "notes.txt"
    source.write_text("initial bytes", encoding="utf-8")
    item = service.enqueue([source], actor="operator")[0]
    source.write_text("changed bytes", encoding="utf-8")
    with pytest.raises(ValidationError, match="source changed"):
        service.preview(item.id)
    with pytest.raises(ValidationError, match="source changed"):
        service.decide(
            item.id,
            decision="REVIEWED",
            actor="reviewer",
            reason="must not accept stale source",
        )
    with workspace.database.read_only() as session:
        row = session.get(IntakeQueueItem, item.id)
        assert row is not None and row.status == "QUEUED"


def test_intake_requires_governed_disposition(workspace: Workspace, tmp_path: Path) -> None:
    source = tmp_path / "shift.csv"
    source.write_text("time,operation\n08:00,drilling\n", encoding="utf-8")
    service = IntakeQueueService(workspace.database)
    item = service.enqueue([source], actor="operator")[0]
    for actor, reason in (("", "reason"), ("reviewer", "")):
        with pytest.raises(ValidationError):
            service.decide(item.id, decision="REVIEWED", actor=actor, reason=reason)
    with pytest.raises(ValidationError, match="must be REVIEWED or REJECTED"):
        service.decide(item.id, decision="APPROVED", actor="reviewer", reason="not a domain action")
    assert service.list_items(status="QUEUED")[0].status == "QUEUED"


def test_intake_migration_downgrade_and_upgrade_is_deterministic(workspace: Workspace) -> None:
    from drilling_intelligence.database.migrations import upgrade

    engine = workspace.database.engine
    down = upgrade(engine, "0023", allow_downgrade=True)
    assert down.mode == "downgraded" and down.current == "0023"
    assert "intake_queue_item" not in inspect(engine).get_table_names()
    up = upgrade(engine)
    assert up.current == "0026"
    assert "intake_queue_item" in inspect(engine).get_table_names()


def test_intake_local_preview_supports_real_pdf_workbook_docx_csv_and_text(
    workspace: Workspace, corpus_dir: Path
) -> None:
    service = IntakeQueueService(workspace.database)
    paths = [
        corpus_dir / "well_a3_program_rev12.pdf",
        corpus_dir / "mud_report_well-a3.xlsx",
        corpus_dir / "daily_drilling_report_well-a3.docx",
        corpus_dir / "npt_summary_2025-06.csv",
        corpus_dir / "lesson_learned_ll-2025-014.txt",
    ]
    items = service.enqueue(paths, actor="operator")
    assert len(items) == 5
    previews = [service.preview(item.id) for item in items]
    assert [preview.item.extension for preview in previews] == [
        ".pdf",
        ".xlsx",
        ".docx",
        ".csv",
        ".txt",
    ]
    assert all(preview.extractor and preview.contract_level for preview in previews)
    with workspace.database.read_only() as session:
        assert session.scalar(select(func.count()).select_from(Document)) == 0
