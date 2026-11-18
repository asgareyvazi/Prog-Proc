"""Local-only operator intake, review preview, and explicit registry registration.

The queue stores references and a measured SHA-256, never document bytes. Preview is
transient. Registration is a separate, actor/reason-attributed action that rechecks the
same bytes and delegates to the existing :class:`DocumentRegistry`; it never promotes a
domain record.
"""

from __future__ import annotations

import mimetypes
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from sqlalchemy import select

from ..classification.rules import DeterministicClassifier
from ..core.enums import FileChangeKind
from ..core.errors import ValidationError
from ..core.hashing import sha256_file
from ..core.ids import new_id
from ..database.engine import sqlite_file_path
from ..database.models import (
    AuditEvent,
    Document,
    DocumentVersion,
    IntakeQueueItem,
    IntakeRegistrationRecord,
)
from ..database.session import Database
from ..documents.registry import DocumentRegistry
from ..documents.repository import DocumentRepository
from ..extraction.interfaces import ExtractionContext
from ..extraction.registry import build_default_router, default_extractors
from ..extraction.router import DocumentRouter
from ..operations.contracts import promotion_contract
from ..wells.repository import WellRepository

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
    document_id: str = ""
    document_version_id: str = ""
    registered_well_id: str = ""
    registered_by: str = ""
    registered_at: str = ""
    registration_reason: str = ""

    @classmethod
    def from_row(
        cls, row: IntakeQueueItem, registration: IntakeRegistrationRecord | None = None
    ) -> IntakeItem:
        return cls(
            id=row.id,
            source_path=row.source_path,
            filename=row.filename,
            extension=row.extension,
            size_bytes=row.size_bytes,
            sha256=row.sha256,
            status="REGISTERED" if registration is not None else row.status,
            enqueued_by=row.enqueued_by,
            enqueued_at=row.enqueued_at.isoformat(),
            reviewed_by=row.reviewed_by or "",
            reviewed_at=row.reviewed_at.isoformat() if row.reviewed_at else "",
            review_reason=row.review_reason or "",
            document_id=registration.document_id if registration else "",
            document_version_id=registration.document_version_id if registration else "",
            registered_well_id=registration.registered_well_id or "" if registration else "",
            registered_by=registration.registered_by if registration else "",
            registered_at=registration.registered_at.isoformat() if registration else "",
            registration_reason=registration.registration_reason if registration else "",
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


@dataclass(frozen=True)
class IntakeRegistration:
    """The registry result linked back to the reviewed queue item."""

    item: IntakeItem
    classification: str
    change: str
    already_registered: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "item": self.item.__dict__.copy(),
            "document_id": self.item.document_id,
            "document_version_id": self.item.document_version_id,
            "well_id": self.item.registered_well_id,
            "classification": self.classification,
            "change": self.change,
            "already_registered": self.already_registered,
            "status": self.item.status,
        }


class IntakeQueueService:
    """Thread-safe service; each public operation owns its SQLAlchemy unit of work."""

    def __init__(
        self,
        database: Database,
        *,
        workspace_root: str | Path | None = None,
        settings: Any = None,
    ) -> None:
        self._database = database
        self._workspace_root = (
            Path(workspace_root).expanduser().resolve() if workspace_root is not None else None
        )
        self._settings = settings

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
            statement = (
                select(IntakeQueueItem, IntakeRegistrationRecord)
                .outerjoin(
                    IntakeRegistrationRecord,
                    IntakeRegistrationRecord.intake_item_id == IntakeQueueItem.id,
                )
                .order_by(IntakeQueueItem.enqueued_at, IntakeQueueItem.id)
            )
            if status:
                normalized = status.strip().upper()
                if normalized == "REGISTERED":
                    statement = statement.where(
                        IntakeRegistrationRecord.intake_item_id.is_not(None)
                    )
                elif normalized == "REVIEWED":
                    statement = statement.where(
                        IntakeQueueItem.status == "REVIEWED",
                        IntakeRegistrationRecord.intake_item_id.is_(None),
                    )
                else:
                    statement = statement.where(IntakeQueueItem.status == normalized)
            return tuple(
                IntakeItem.from_row(row, registration)
                for row, registration in session.execute(statement)
            )

    def preview(self, item_id: str) -> IntakePreview:
        with self._database.read_only() as session:
            row = session.get(IntakeQueueItem, item_id)
            if row is None:
                raise ValidationError(f"intake item not found: {item_id}")
            registration = session.get(IntakeRegistrationRecord, row.id)
            item = IntakeItem.from_row(row, registration)
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

    def register(
        self,
        item_id: str,
        *,
        actor: str,
        reason: str,
        well_id: str = "",
    ) -> IntakeRegistration:
        """Register reviewed bytes via the normal document registry, without promoting them.

        The item's SHA is verified before and after ``DocumentRegistry.register`` as well as by
        that registry's own measured read. The queue link, registry rows and audit record commit
        together; any stale bytes or registration error roll the whole action back.
        """
        actor = _required(actor, "actor", 80)
        reason = _required(reason, "reason", 2000)
        if self._workspace_root is None or self._settings is None:
            raise ValidationError(
                "intake registration requires the open workspace root and settings"
            )
        with self._database.unit_of_work() as session:
            row = session.get(IntakeQueueItem, item_id)
            if row is None:
                raise ValidationError(f"intake item not found: {item_id}")
            existing_registration = session.get(IntakeRegistrationRecord, row.id)
            if existing_registration is not None:
                document = session.get(Document, existing_registration.document_id)
                version = session.get(DocumentVersion, existing_registration.document_version_id)
                if document is None or version is None:
                    raise ValidationError(
                        "registered intake item points to missing registry evidence"
                    )
                if well_id and str(existing_registration.registered_well_id or "") != well_id:
                    raise ValidationError(
                        "intake item was already registered to a different well; create a new "
                        "explicit registry action rather than changing its scope"
                    )
                return IntakeRegistration(
                    item=IntakeItem.from_row(row, existing_registration),
                    classification=str(document.classification),
                    change="UNCHANGED",
                    already_registered=True,
                )
            if row.status != "REVIEWED":
                raise ValidationError(
                    f"only a REVIEWED intake item can be registered (found {row.status})"
                )
            source = _validate_source(row.source_path)
            before = source.stat()
            if before.st_size != row.size_bytes or sha256_file(source) != row.sha256:
                raise ValidationError(
                    "source changed after intake; enqueue and review the new bytes before registration"
                )

            wells = WellRepository(session)
            workspace_id = wells.resolve_workspace_id(
                self._workspace_root,
                name=str(getattr(getattr(self._settings, "workspace", None), "name", "") or ""),
                data_dir=str(
                    getattr(getattr(self._settings, "workspace", None), "data_dir", "") or ""
                ),
                database_path=sqlite_file_path(self._database.url),
            )
            project_id = ""
            selected_well = None
            if well_id:
                selected_well = wells.get_well(well_id)
                if selected_well is None:
                    raise ValidationError(f"no well {well_id!r} in this workspace")
                project_id = str(selected_well.project_id or "")

            registry = DocumentRegistry(
                DocumentRepository(session),
                router=build_default_router(self._settings),
                settings=self._settings,
            )
            registration = registry.register(
                path=source,
                workspace_root=self._workspace_root,
                workspace_id=workspace_id,
                well_id=str(selected_well.id) if selected_well is not None else None,
                project_id=project_id or None,
                precomputed_sha=row.sha256,
                change=FileChangeKind.NEW,
            )
            if not registration.ok:
                raise ValidationError(
                    f"document registration failed: {registration.error}",
                    error_code=registration.error_code,
                )
            after = source.stat()
            if (
                (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns)
                or registration.sha256 != row.sha256
                or sha256_file(source) != row.sha256
            ):
                raise ValidationError(
                    "source changed during registry registration; all writes were rolled back"
                )
            registration_record = IntakeRegistrationRecord(
                id=row.id,
                intake_item_id=row.id,
                document_id=registration.document_id,
                document_version_id=registration.version_id,
                registered_well_id=str(selected_well.id) if selected_well is not None else None,
                registered_by=actor,
                registered_at=datetime.now(UTC),
                registration_reason=reason,
            )
            session.add(registration_record)
            session.flush()
            session.add(
                AuditEvent(
                    id=new_id("audit"),
                    actor=actor,
                    action="intake.registered",
                    subject_type="intake_queue_item",
                    subject_id=row.id,
                    detail={
                        "sha256": row.sha256,
                        "document_id": registration.document_id,
                        "document_version_id": registration.version_id,
                        "well_id": registration_record.registered_well_id,
                        "classification": registration.classification,
                        "reason": reason,
                        "promotion_performed": False,
                    },
                )
            )
            return IntakeRegistration(
                item=IntakeItem.from_row(row, registration_record),
                classification=registration.classification,
                change=registration.change.value,
            )


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


__all__ = ["IntakeItem", "IntakePreview", "IntakeQueueService", "IntakeRegistration"]
