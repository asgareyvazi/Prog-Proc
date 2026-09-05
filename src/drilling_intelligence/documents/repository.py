"""Persistence for the document registry (sections 12, 13, 14, 16).

Repositories are the only place that touches SQLAlchemy sessions.  They contain
no engineering logic, no extraction logic and no formatting - just queries,
uniqueness handling and integrity checks.  Services compose them.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..core.enums import DocumentClassification, FileChangeKind, ProcessingStatus
from ..core.hashing import filename_identity
from ..core.ids import new_id
from ..database.models import AuditEvent, Document, DocumentVersion, Extraction, Source

# --------------------------------------------------------------------------- identity
#: Fields that belong to the *document slot*, not to one version.  A change here
#: is a metadata edit; a change to ``sha256`` is a new version.
DOCUMENT_LEVEL_FIELDS = {
    "well_id",
    "project_id",
    "title",
    "classification",
    "classification_confidence",
    "document_date",
    "status",
    "source_authority",
    "wellbore",
    "interval_from",
    "interval_to",
    "tags",
    "notes",
    "revision",
    "revision_key",
}


def identity_for(workspace_root: Any, path: Any) -> str:
    """Registry identity = path inside the workspace (case/separator normalised)."""
    return filename_identity(path, workspace_root)


class DocumentRepository:
    """CRUD for documents, versions and extractions."""

    def __init__(self, session: Session) -> None:
        self.session = session

    # -- lookup -------------------------------------------------------------
    def get(self, document_id: str) -> Document | None:
        return self.session.get(Document, document_id)

    def by_identity(self, workspace_id: str | None, identity_path: str) -> Document | None:
        stmt = select(Document).where(Document.identity_path == identity_path)
        if workspace_id:
            stmt = stmt.where(Document.workspace_id == workspace_id)
        return self.session.execute(stmt).scalar_one_or_none()

    def version(self, version_id: str) -> DocumentVersion | None:
        return self.session.get(DocumentVersion, version_id)

    def version_by_sha(self, sha256: str) -> DocumentVersion | None:
        """Any version with identical bytes, anywhere in the registry (duplicate check).

        Newest first, preferring a current version, so the duplicate pointer is
        useful rather than arbitrary.
        """
        return self.session.execute(
            select(DocumentVersion)
            .where(DocumentVersion.sha256 == sha256)
            .order_by(DocumentVersion.is_current.desc(), DocumentVersion.created_at.desc())
            .limit(1)
        ).scalar_one_or_none()

    def versions_for(self, document_id: str) -> list[DocumentVersion]:
        return list(
            self.session.execute(
                select(DocumentVersion).where(DocumentVersion.document_id == document_id).order_by(DocumentVersion.version_number)
            ).scalars()
        )

    def list_documents(
        self,
        *,
        workspace_id: str | None = None,
        well_id: str | None = None,
        classification: str | None = None,
        processing_status: str | None = None,
        search_text: str | None = None,
        limit: int = 500,
        offset: int = 0,
    ) -> list[Document]:
        stmt = select(Document)
        if workspace_id:
            stmt = stmt.where(Document.workspace_id == workspace_id)
        if well_id:
            stmt = stmt.where(Document.well_id == well_id)
        if classification:
            stmt = stmt.where(Document.classification == classification)
        if processing_status:
            stmt = stmt.where(Document.processing_status == processing_status)
        if search_text:
            stmt = stmt.where(Document.filename.ilike(f"%{search_text}%"))
        stmt = stmt.order_by(Document.filename.asc(), Document.identity_path.asc()).limit(limit).offset(offset)
        return list(self.session.execute(stmt).scalars())

    def counts(self, *, workspace_id: str | None = None) -> dict[str, Any]:
        def tally(model: Any, column: Any) -> dict[str, int]:
            stmt = select(column, func.count()).group_by(column)
            if workspace_id and hasattr(model, "workspace_id"):
                stmt = stmt.where(model.workspace_id == workspace_id)
            return {str(key or ""): int(count) for key, count in self.session.execute(stmt).all()}

        return {
            "documents": int(self.session.execute(select(func.count()).select_from(Document)).scalar_one() or 0),
            "versions": int(self.session.execute(select(func.count()).select_from(DocumentVersion)).scalar_one() or 0),
            "extractions": int(self.session.execute(select(func.count()).select_from(Extraction)).scalar_one() or 0),
            "by_classification": tally(Document, Document.classification),
            "by_processing_status": tally(Document, Document.processing_status),
        }

    # -- registration -------------------------------------------------------
    def create_document(
        self,
        *,
        workspace_id: str | None,
        identity_path: str,
        filename: str,
        extension: str,
        mime_type: str,
        size_bytes: int,
        sha256: str,
        file_created_at: datetime | None = None,
        file_modified_at: datetime | None = None,
        well_id: str | None = None,
        project_id: str | None = None,
        classification: DocumentClassification | str = DocumentClassification.OTHER,
        status: str = "UNKNOWN",
        source_authority: str | None = None,
        revision: str | None = None,
        revision_key: int = 0,
        document_date: datetime | None = None,
        notes: str | None = None,
    ) -> Document:
        document = Document(
            id=new_id("doc"),
            workspace_id=workspace_id,
            project_id=project_id,
            well_id=well_id,
            identity_path=identity_path,
            filename=filename,
            extension=(extension or "").lower(),
            mime_type=mime_type or "",
            size_bytes=size_bytes,
            sha256=sha256,
            file_created_at=file_created_at,
            file_modified_at=file_modified_at,
            imported_at=datetime.now(UTC),
            classification=str(getattr(classification, "value", classification)),
            status=str(getattr(status, "value", status)),
            source_authority=source_authority,
            revision=revision,
            revision_key=revision_key,
            document_date=document_date,
            processing_status=ProcessingStatus.REGISTERED.value,
            notes=notes,
            change_count=0,
        )
        self.session.add(document)
        self.session.flush()
        return document

    def create_version(
        self,
        document: Document,
        *,
        sha256: str,
        source_path: str,
        size_bytes: int,
        parser: str,
        parser_version: str,
        extraction_version: str,
        origin: FileChangeKind,
        mime_type: str = "",
        file_modified_at: datetime | None = None,
        revision: str | None = None,
        revision_key: int = 0,
        status: str | None = None,
        supersedes_version_id: str | None = None,
        duplicate_of_version_id: str | None = None,
        metadata_json: dict[str, Any] | None = None,
        page_count: int | None = None,
        sheet_count: int | None = None,
        word_count: int | None = None,
    ) -> DocumentVersion:
        existing = self.versions_for(document.id)
        version = DocumentVersion(
            id=new_id("ver"),
            document_id=document.id,
            version_number=len(existing) + 1,
            revision=revision,
            revision_key=revision_key,
            status=status or document.status,
            source_path=source_path,
            sha256=sha256,
            size_bytes=size_bytes,
            file_modified_at=file_modified_at,
            mime_type=mime_type,
            parser=parser,
            parser_version=parser_version,
            extraction_version=extraction_version,
            origin=str(getattr(origin, "value", origin)),
            supersedes_version_id=supersedes_version_id,
            duplicate_of_version_id=duplicate_of_version_id,
            metadata_json=metadata_json or {},
            page_count=page_count,
            sheet_count=sheet_count,
            word_count=word_count,
            is_current=True,
        )
        self.session.add(version)
        # The pointer to the new row is a foreign key, so the version has to exist in
        # the transaction first: writing it before the flush makes SQLite fail the
        # constraint mid-transaction and the whole ingestion run rolls back.
        self.session.flush()
        for previous in existing:
            previous.is_current = False
            if previous.id != version.id and previous.superseded_by_version_id is None:
                previous.superseded_by_version_id = version.id
        document.current_version_id = version.id
        document.change_count = len(existing) + 1
        self.session.flush()
        return version

    def touch_document_from_version(self, document: Document, version: DocumentVersion) -> None:
        """Keep the registry row consistent with its current version.

        Deliberately a separate call: the registry pointer and the version
        snapshot are written in one unit of work by the service, and this makes
        the relationship explicit rather than a side effect.
        """
        document.sha256 = version.sha256
        document.size_bytes = version.size_bytes
        document.file_modified_at = version.file_modified_at
        if version.revision:
            document.revision = version.revision
            document.revision_key = version.revision_key

    def update_document_metadata(self, document: Document, values: dict[str, Any]) -> dict[str, Any]:
        """Update only document-level fields; unknown keys are reported, not applied."""
        applied: dict[str, Any] = {}
        for key, raw_value in values.items():
            if key not in DOCUMENT_LEVEL_FIELDS:
                continue
            value = str(getattr(raw_value, "value", raw_value)) if key == "classification" else raw_value
            setattr(document, key, value)
            applied[key] = value
        self.session.flush()
        return applied

    def set_document_status(self, document_id: str, status: ProcessingStatus) -> bool:
        """Set the pipeline state by id (used after indexing, where no instance is loaded)."""
        document = self.get(document_id)
        if document is None:
            return False
        document.processing_status = str(getattr(status, "value", status))
        self.session.flush()
        return True

    def set_processing(self, document: Document, status: ProcessingStatus, error: str | None = None) -> None:
        document.processing_status = str(getattr(status, "value", status))
        document.processing_error = error

    # -- extraction storage -------------------------------------------------
    def find_cached_extraction(self, *, content_sha256: str, extractor: str, extractor_version: str, config_hash: str) -> Extraction | None:
        """Extraction cache lookup: identical bytes + identical extractor = reuse."""
        stmt = (
            select(Extraction)
            .where(
                Extraction.content_sha256 == content_sha256,
                Extraction.extractor == extractor,
                Extraction.extractor_version == extractor_version,
                Extraction.config_hash == (config_hash or ""),
            )
            .order_by(Extraction.created_at.desc())
            .limit(1)
        )
        return self.session.execute(stmt).scalar_one_or_none()

    def save_extraction(
        self,
        *,
        document: Document,
        version: DocumentVersion,
        extractor: str,
        extractor_version: str,
        content_sha256: str,
        config_hash: str,
        document_json: dict[str, Any] | None,
        text: str,
        stats: dict[str, Any] | None,
        router_decision: dict[str, Any] | None,
        status: str = "OK",
        error: str | None = None,
        duration_ms: float | None = None,
    ) -> Extraction:
        extraction = Extraction(
            id=new_id("ext"),
            document_id=document.id,
            document_version_id=version.id,
            content_sha256=content_sha256,
            extractor=extractor,
            extractor_version=extractor_version,
            config_hash=config_hash or "",
            router_decision=router_decision or {},
            status=status,
            error=error,
            duration_ms=duration_ms,
            stats=stats or {},
            document_json=document_json,
            text_blob=text,
        )
        self.session.add(extraction)
        version.parser = extractor
        version.parser_version = extractor_version
        version.extraction_version = extractor_version
        version.extracted_at = datetime.now(UTC)
        self.session.flush()
        return extraction

    def latest_extraction(self, document_id: str) -> Extraction | None:
        return self.session.execute(
            select(Extraction).where(Extraction.document_id == document_id).order_by(Extraction.created_at.desc()).limit(1)
        ).scalar_one_or_none()

    def extraction_for_version(self, version_id: str) -> Extraction | None:
        return self.session.execute(select(Extraction).where(Extraction.document_version_id == version_id).limit(1)).scalar_one_or_none()

    # -- sources ------------------------------------------------------------
    def get_or_create_source(
        self,
        *,
        kind: str,
        reference: str,
        label: str,
        authority_tier: str = "general_knowledge",
        document_id: str | None = None,
        document_version_id: str | None = None,
        revision: str | None = None,
        verified: bool = False,
        notes: str | None = None,
    ) -> Source:
        source = self.session.execute(select(Source).where(Source.kind == kind, Source.reference == reference)).scalar_one_or_none()
        if source is not None:
            changed = False
            if authority_tier and source.authority_tier != authority_tier:
                source.authority_tier = authority_tier
                changed = True
            if document_version_id and source.document_version_id != document_version_id:
                source.document_version_id = document_version_id
                changed = True
            if revision and source.revision != revision:
                source.revision = revision
                changed = True
            if changed:
                self.session.flush()
            return source
        source = Source(
            id=new_id("src"),
            kind=kind,
            reference=reference,
            label=label,
            authority_tier=authority_tier,
            document_id=document_id,
            document_version_id=document_version_id,
            revision=revision,
            verified=verified,
            notes=notes,
        )
        self.session.add(source)
        self.session.flush()
        return source

    # -- audit --------------------------------------------------------------
    def audit(self, *, action: str, subject_type: str, subject_id: str, detail: dict[str, Any] | None = None, actor: str = "system") -> AuditEvent:
        event = AuditEvent(
            id=new_id("aud"),
            actor=actor,
            action=action,
            subject_type=subject_type,
            subject_id=subject_id,
            detail=detail or {},
        )
        self.session.add(event)
        return event

    def audit_trail(self, subject_type: str, subject_id: str, limit: int = 50) -> list[AuditEvent]:
        return list(
            self.session.execute(
                select(AuditEvent)
                .where(AuditEvent.subject_type == subject_type, AuditEvent.subject_id == subject_id)
                .order_by(AuditEvent.at.desc())
                .limit(limit)
            ).scalars()
        )


__all__ = ["DOCUMENT_LEVEL_FIELDS", "DocumentRepository", "identity_for"]
