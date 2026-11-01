"""Local-only operator intake queue and non-writing document review preview.

The queue records a path, measured SHA-256 and explicit human disposition. Preview
uses only the installed deterministic extractors/classifier and static promotion
contracts; it neither persists extracted content nor invokes a provider or writer.
"""

from __future__ import annotations

import mimetypes
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from sqlalchemy import select

from ..classification.rules import DeterministicClassifier
from ..core.errors import ValidationError
from ..core.hashing import sha256_file
from ..core.ids import new_id
from ..database.models import AuditEvent, IntakeQueueItem
from ..database.session import Database
from ..extraction.interfaces import ExtractionContext
from ..extraction.registry import default_extractors
from ..extraction.router import DocumentRouter
from ..operations.contracts import promotion_contract

_SUPPORTED = frozenset({".pdf", ".xlsx", ".xlsm", ".docx", ".csv", ".txt"})
_MAX_BYTES = 1_073_741_824


@dataclass(frozen=True)
class IntakeItem:
    id: str
    source_path: str
    filename: str
    extension: str
    size_bytes: int
    sha256: str
    status: str
    enqueued_by: str
    enqueued_at: str
    reviewed_by: str = ""
    reviewed_at: str = ""
    review_reason: str = ""

    @classmethod
    def from_row(cls, row: IntakeQueueItem) -> IntakeItem:
        return cls(
            id=row.id,
            source_path=row.source_path,
            filename=row.filename,
            extension=row.extension,
            size_bytes=row.size_bytes,
            sha256=row.sha256,
            status=row.status,
            enqueued_by=row.enqueued_by,
            enqueued_at=row.enqueued_at.isoformat(),
            reviewed_by=row.reviewed_by or "",
            reviewed_at=row.reviewed_at.isoformat() if row.reviewed_at else "",
            review_reason=row.review_reason or "",
        )


@dataclass(frozen=True)
class IntakePreview:
    item: IntakeItem
    extractor: str
    classification: str
    confidence: float
    contract_level: str
    contract_notes: str
    text_excerpt: str
    paragraph_count: int
    table_count: int
    classification_evidence: tuple[dict[str, Any], ...]
    provenance_preview: tuple[dict[str, Any], ...]
    warnings: tuple[str, ...]


class IntakeQueueService:
    """Thread-safe service; each public operation owns its own SQLAlchemy unit of work."""

    def __init__(self, database: Database) -> None:
        self._database = database

    def enqueue(self, paths: list[str | Path], *, actor: str) -> tuple[IntakeItem, ...]:
        actor = _required(actor, "actor", 80)
        accepted: list[IntakeItem] = []
        with self._database.unit_of_work() as session:
            for raw_path in paths:
                source = _validate_source(raw_path)
                before = source.stat()
                digest = sha256_file(source)
                after = source.stat()
                if (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns):
                    raise ValidationError(f"source changed while hashing: {source}")
                duplicate = session.scalar(
                    select(IntakeQueueItem).where(IntakeQueueItem.sha256 == digest)
                )
                if duplicate is not None:
                    accepted.append(IntakeItem.from_row(duplicate))
                    continue
                row = IntakeQueueItem(
                    id=new_id("intake"),
                    source_path=str(source),
                    filename=source.name,
                    extension=source.suffix.lower(),
                    size_bytes=after.st_size,
                    sha256=digest,
                    status="QUEUED",
                    enqueued_by=actor,
                    enqueued_at=datetime.now(UTC),
                )
                session.add(row)
                session.flush()
                session.add(
                    AuditEvent(
                        id=new_id("audit"),
                        actor=actor,
                        action="intake.queued",
                        subject_type="intake_queue_item",
                        subject_id=row.id,
                        detail={
                            "sha256": digest,
                            "filename": row.filename,
                            "size_bytes": row.size_bytes,
                        },
                    )
                )
                accepted.append(IntakeItem.from_row(row))
        return tuple(accepted)

    def list_items(self, *, status: str = "") -> tuple[IntakeItem, ...]:
        with self._database.read_only() as session:
            statement = select(IntakeQueueItem).order_by(
                IntakeQueueItem.enqueued_at, IntakeQueueItem.id
            )
            if status:
                statement = statement.where(IntakeQueueItem.status == status.upper())
            return tuple(IntakeItem.from_row(row) for row in session.scalars(statement))

    def preview(self, item_id: str) -> IntakePreview:
        with self._database.read_only() as session:
            row = session.get(IntakeQueueItem, item_id)
            if row is None:
                raise ValidationError(f"intake item not found: {item_id}")
            item = IntakeItem.from_row(row)
        source = _validate_source(item.source_path)
        if sha256_file(source) != item.sha256:
            raise ValidationError(
                "source changed after intake; enqueue the new version before preview"
            )
        context = ExtractionContext(
            path=source,
            filename=item.filename,
            sha256=item.sha256,
            extension=item.extension,
            size_bytes=item.size_bytes,
            mime_type=mimetypes.guess_type(item.filename)[0] or "application/octet-stream",
        )
        router = DocumentRouter(default_extractors(), settings=None)
        document, choice, _extractor = router.extract(context)
        classification = DeterministicClassifier().classify(
            filename=item.filename,
            text=document.text,
            extension=item.extension,
            document=document,
        )
        contract = promotion_contract(classification.classification)
        if contract is None:
            raise ValidationError(
                "no static promotion contract exists for the previewed classification"
            )
        return IntakePreview(
            item=item,
            extractor=choice.extractor,
            classification=classification.classification.value,
            confidence=round(classification.confidence, 4),
            contract_level=contract.level.value,
            contract_notes=contract.notes,
            text_excerpt=(document.text or "")[:1200],
            paragraph_count=len(document.paragraphs),
            table_count=len(document.tables),
            classification_evidence=tuple(
                evidence.to_dict() for evidence in classification.evidence[:20]
            ),
            provenance_preview=tuple(item.to_dict() for item in document.provenance[:20]),
            warnings=tuple(document.diagnostics[:20]),
        )

    def decide(self, item_id: str, *, decision: str, actor: str, reason: str) -> IntakeItem:
        actor = _required(actor, "actor", 80)
        reason = _required(reason, "reason", 2000)
        target = decision.strip().upper()
        if target not in {"REVIEWED", "REJECTED"}:
            raise ValidationError("intake decision must be REVIEWED or REJECTED")
        with self._database.unit_of_work() as session:
            row = session.get(IntakeQueueItem, item_id)
            if row is None:
                raise ValidationError(f"intake item not found: {item_id}")
            if row.status != "QUEUED":
                raise ValidationError(
                    f"intake item is already {row.status}; disposition is immutable"
                )
            source = _validate_source(row.source_path)
            if sha256_file(source) != row.sha256:
                raise ValidationError("source changed after intake; refusing a stale disposition")
            row.status = target
            row.reviewed_by = actor
            row.reviewed_at = datetime.now(UTC)
            row.review_reason = reason
            session.flush()
            session.add(
                AuditEvent(
                    id=new_id("audit"),
                    actor=actor,
                    action=f"intake.{target.lower()}",
                    subject_type="intake_queue_item",
                    subject_id=row.id,
                    detail={"sha256": row.sha256, "reason": reason},
                )
            )
            return IntakeItem.from_row(row)


def _required(value: str, name: str, limit: int) -> str:
    normalized = str(value or "").strip()
    if not normalized or len(normalized) > limit:
        raise ValidationError(f"{name} is required and must not exceed {limit} characters")
    return normalized


def _validate_source(raw_path: str | Path) -> Path:
    source = Path(raw_path).expanduser().resolve(strict=True)
    if not source.is_file():
        raise ValidationError(f"intake source is not a regular file: {source}")
    if source.suffix.lower() not in _SUPPORTED:
        raise ValidationError(f"unsupported intake extension: {source.suffix or '(none)'}")
    size = source.stat().st_size
    if size <= 0 or size > _MAX_BYTES:
        raise ValidationError(f"intake file size must be between 1 and {_MAX_BYTES} bytes")
    return source


__all__ = ["IntakeItem", "IntakePreview", "IntakeQueueService"]
