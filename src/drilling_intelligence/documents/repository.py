"""Persistence for the document registry (sections 12, 13, 14, 16).

Repositories are the only place that touches SQLAlchemy sessions.  They contain
no engineering logic, no extraction logic and no formatting - just queries,
uniqueness handling and integrity checks.  Services compose them.

Three concurrency/integrity rules are implemented here rather than in the callers,
because every caller must get them:

*   **version numbers are allocated under the unique constraint, not in Python.**
    ``max(version_number) + 1`` is only a *guess*; the ``uq_document_version_number``
    constraint is the arbiter.  A collision (another session that inserted a version
    for the same document since the read) rolls back to a savepoint and retries - so
    two ingestion runs can never both write "version 1" - and the numbers stay
    sequential (1, 2, 3 ...) with the gaps that a loser retries closing.
*   **the supersede/pointer write is ordered**, so the partial unique index on
    ``document_version.is_current`` and the deferred foreign key on
    ``document.current_version_id`` are satisfied by every statement, not just by the
    final state of the transaction.
*   **``get_or_create_source`` and the extraction cache upsert are race-safe**: the
    select-then-insert window is closed with a savepoint and a re-read on
    IntegrityError, so a concurrent writer produces a reuse, never a duplicate row
    and never a failed run.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from ..core.enums import DocumentClassification, FileChangeKind, ProcessingStatus
from ..core.errors import DrillingIntelligenceError
from ..core.filesystem import candidate_source_paths, first_existing
from ..core.hashing import filename_identity
from ..core.ids import new_id
from ..database.audit import AuditLog
from ..database.models import (
    AuditEvent,
    Document,
    DocumentVersion,
    Extraction,
    ExtractionCache,
    Source,
)

#: How many times a version-number collision is retried before giving up.  Concurrent
#: ingestion of the *same document* is rare; the loop is for correctness, not throughput.
MAX_VERSION_NUMBER_ATTEMPTS = 5

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
        #: The audit trail is append-only and this is the only handle on it that the
        #: repository offers: ``repository.audit(...)`` records, ``audit_trail(...)``
        #: reads.  There is deliberately no update or delete path - the ORM guards in
        #: :mod:`drilling_intelligence.database.audit` reject those even for a session
        #: that goes around this class.
        self.audit_log = AuditLog(session)

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
        fs_metadata_changed_at: datetime | None = None,
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
            fs_metadata_changed_at=fs_metadata_changed_at,
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
        source_relative_path: str | None = None,
    ) -> DocumentVersion:
        """Create the next immutable version of ``document`` and make it current.

        The version number is allocated *inside* a savepoint and re-derived when the
        unique constraint rejects it, so a concurrent writer can neither fail this call
        nor produce two rows numbered the same.  The sibling rows are re-read on each
        attempt for the same reason: a snapshot taken before the loop goes stale
        exactly when it matters.

        The writes are ordered deliberately: the previous current row must stop claiming
        to be current before the new row is inserted (the partial unique index is a
        per-statement check), while ``document.current_version_id`` can only be written
        after the version row exists (the deferred foreign key).  Every statement in
        between therefore leaves the database in a state the schema accepts, which is
        what makes the savepoint retry below safe.
        """
        last_error: Exception | None = None
        for _attempt in range(MAX_VERSION_NUMBER_ATTEMPTS):
            next_number = self.next_version_number(document.id)
            savepoint = self.session.begin_nested()
            try:
                siblings = self.versions_for(document.id)
                version = DocumentVersion(
                    id=new_id("ver"),
                    document_id=document.id,
                    version_number=next_number,
                    revision=revision,
                    revision_key=revision_key,
                    status=status or document.status,
                    source_path=source_path,
                    source_relative_path=source_relative_path,
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
                # Release the "current" slot *before* the new row claims it.  A unique
                # index - partial or not - is checked per statement, not deferred to
                # commit, and a flush always emits INSERTs before UPDATEs of the same
                # table, so writing the flag flips after the insert would collide with
                # the row it is meant to replace.  The separate flush is the fix, and it
                # is inside the savepoint so a retry undoes it too.
                for previous in siblings:
                    previous.is_current = False
                self.session.flush()

                if version.supersedes_version_id is None:
                    # The chain is the repository's business, not the caller's: whoever
                    # adds a version gets it linked to the version that was current
                    # before, or the supersede chain has a hole in it and "what did this
                    # revision replace?" stops having an answer.
                    predecessor = max(
                        (row for row in siblings if row.is_current or row.version_number < next_number),
                        key=lambda row: row.version_number,
                        default=None,
                    )
                    if predecessor is not None:
                        version.supersedes_version_id = predecessor.id
                self.session.add(version)
                self.session.flush()
                for previous in siblings:
                    if previous.superseded_by_version_id is None:
                        previous.superseded_by_version_id = version.id
                document.current_version_id = version.id
                document.change_count = next_number
                self.session.flush()
            except IntegrityError as exc:
                # Roll back only this attempt: the version row, the pointer and the flag
                # flips are discarded together, so the retry starts from clean state and
                # the surrounding transaction (the rest of the ingestion run) stays alive.
                savepoint.rollback()
                last_error = exc
                continue
            savepoint.commit()
            return version
        raise DrillingIntelligenceError(
            f"could not allocate a version number for document {document.id} after {MAX_VERSION_NUMBER_ATTEMPTS} attempts",
            document_id=document.id,
            last_error=str(last_error),
        )

    def next_version_number(self, document_id: str) -> int:
        """``max`` rather than ``count``: a document must never reuse a version number."""
        current = self.session.execute(
            select(func.max(DocumentVersion.version_number)).where(DocumentVersion.document_id == document_id)
        ).scalar_one_or_none()
        return int(current or 0) + 1

    def resolve_source_path(
        self,
        version: DocumentVersion,
        document: Document | None = None,
        *,
        workspace_root: Path | str | None = None,
    ) -> Path | None:
        """Where this version's file is *now*, or ``None`` if it cannot be found.

        The absolute path recorded at scan time is tried first, then the durable
        workspace-relative path against the workspace that is open right now - which is
        what makes provenance survive a moved or renamed workspace folder.
        """
        candidates = candidate_source_paths(
            recorded_path=version.source_path or "",
            workspace_root=workspace_root,
            relative_path=version.source_relative_path or (document.identity_path if document is not None else ""),
            filename=document.filename if document is not None else "",
        )
        return first_existing(candidates)

    def current_version(self, document: Document) -> DocumentVersion | None:
        """The version the registry points at, via the pointer (not "the newest row").

        Reading the pointer rather than the newest row matters: a repair, an approval
        rollback or an import can legitimately make those differ, and the registry must
        then report what it actually points at instead of hiding the disagreement.
        """
        if document.current_version_id:
            pointed = self.version(document.current_version_id)
            if pointed is not None:
                return pointed
        return next((version for version in self.versions_for(document.id) if version.is_current), None)

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
        """Extraction cache lookup: identical bytes + identical extractor = reuse.

        The cache table is the *index*; the artefact it points at is a normal
        ``extraction`` row, so a hit costs one indexed lookup plus one primary-key read -
        no parsing, and no copy of the document JSON.  A pointer whose artefact was
        deleted (document removal cascades) is cleaned up here rather than served, so a
        stale entry degrades into a fresh extraction instead of a crash.
        """
        entry = self.cache_entry(
            content_sha256=content_sha256,
            extractor=extractor,
            extractor_version=extractor_version,
            config_hash=config_hash,
        )
        if entry is None:
            return None
        artefact = self.session.get(Extraction, entry.extraction_id) if entry.extraction_id else None
        if artefact is None or not artefact.document_json:
            self.session.delete(entry)
            self.session.flush()
            return None
        return artefact

    def cache_entry(self, *, content_sha256: str, extractor: str, extractor_version: str, config_hash: str) -> ExtractionCache | None:
        return self.session.execute(
            select(ExtractionCache).where(
                ExtractionCache.content_sha256 == content_sha256,
                ExtractionCache.extractor == extractor,
                ExtractionCache.extractor_version == extractor_version,
                ExtractionCache.config_hash == (config_hash or ""),
            )
        ).scalar_one_or_none()

    def cache_hit(self, *, content_sha256: str, extractor: str, extractor_version: str, config_hash: str) -> None:
        """Count a reuse.  Best-effort: the count is diagnostics, never correctness."""
        entry = self.cache_entry(
            content_sha256=content_sha256,
            extractor=extractor,
            extractor_version=extractor_version,
            config_hash=config_hash,
        )
        if entry is not None:
            entry.hits = int(entry.hits or 0) + 1
            self.session.flush()

    def remember_extraction_in_cache(
        self,
        extraction: Extraction,
        *,
        content_sha256: str,
        extractor: str,
        extractor_version: str,
        config_hash: str,
        document_version_id: str | None,
    ) -> ExtractionCache:
        """Publish ``extraction`` as the artefact for its cache key.

        Upsert under the unique constraint ``uq_extraction_cache_key``: two runs that
        extract the same bytes at the same time must not both create an entry, so the
        insert happens in a savepoint and an IntegrityError is answered by re-reading the
        winner and pointing at this row instead.  The entry then records *this* artefact
        (a refresh after a parser fix legitimately replaces what the cache serves) while
        the superseded artefact row itself stays on disk untouched - history is never
        rewritten, only what the cache points at moves forward.
        """
        key = {
            "content_sha256": content_sha256,
            "extractor": extractor,
            "extractor_version": extractor_version,
            "config_hash": config_hash or "",
        }
        last_error: Exception | None = None
        for _attempt in range(2):
            savepoint = self.session.begin_nested()
            try:
                entry = self.cache_entry(**key)
                if entry is None:
                    entry = ExtractionCache(id=new_id("extcache"), hits=0, **key)
                    self.session.add(entry)
                else:
                    entry.refreshed = entry.extraction_id is not None and entry.extraction_id != extraction.id
                entry.extraction_id = extraction.id
                entry.produced_by_version_id = document_version_id
                self.session.flush()
            except IntegrityError as exc:
                savepoint.rollback()
                last_error = exc
                continue
            savepoint.commit()
            return entry
        raise DrillingIntelligenceError("could not publish the extraction to the cache", error=str(last_error))

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
        cache: bool = True,
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
        # Only a *produced* artefact joins the cache: a CACHE_HIT row is a pointer to an
        # artefact that is already there, and re-publishing it would make the cache entry
        # point at a copy rather than at what the run actually parsed.
        if cache and status == "OK":
            self.remember_extraction_in_cache(
                extraction,
                content_sha256=content_sha256,
                extractor=extractor,
                extractor_version=extractor_version,
                config_hash=config_hash or "",
                document_version_id=version.id,
            )
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
        source = self.find_source(kind=kind, reference=reference)
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
        return self._insert_source(
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

    def find_source(self, *, kind: str, reference: str) -> Source | None:
        """The source row for a citation key, if this transaction can already see one.

        A method rather than an inline query because the get-or-create below has to look
        twice - once to decide, once to re-read after a conflicting insert - and a
        caller that wants to *simulate* the race (a test, a retry harness) needs one seam
        to override rather than the whole method.
        """
        return self.session.execute(select(Source).where(Source.kind == kind, Source.reference == reference)).scalar_one_or_none()

    def _insert_source(self, **values: Any) -> Source:
        """Insert one source row, resolving the select-then-insert race by re-reading.

        ``(kind, reference)`` is unique in the schema, so a concurrent writer that got
        there first does not lose: the failed insert is rolled back to a savepoint (the
        surrounding transaction survives) and the row that exists is the one returned.
        A duplicate key here is therefore a *reuse*, never an ingestion failure.
        """
        last_error: Exception | None = None
        for _attempt in range(2):
            savepoint = self.session.begin_nested()
            try:
                source = Source(id=new_id("src"), **values)
                self.session.add(source)
                self.session.flush()
            except IntegrityError as exc:
                savepoint.rollback()
                last_error = exc
                existing = self.find_source(kind=values["kind"], reference=values["reference"])
                if existing is not None:
                    return existing
                continue
            savepoint.commit()
            return source
        raise DrillingIntelligenceError(
            f"source {values.get('kind')}:{values.get('reference')} could not be created",
            error=str(last_error),
        )

    # -- audit --------------------------------------------------------------
    def audit(self, *, action: str, subject_type: str, subject_id: str, detail: dict[str, Any] | None = None, actor: str = "system") -> AuditEvent:
        """Append one audit event (the only audit write the repository offers)."""
        return self.audit_log.record(action=action, subject_type=subject_type, subject_id=subject_id, detail=detail, actor=actor)

    def audit_trail(self, subject_type: str, subject_id: str, limit: int = 50) -> list[AuditEvent]:
        return self.audit_log.trail(subject_type, subject_id, limit=limit)

    # -- consistency --------------------------------------------------------
    def check_current_version_invariants(self) -> list[Any]:
        """Cross-row check of the "exactly one current version" rule (see ``database.integrity``)."""
        from ..database.integrity import check_current_version_invariants

        self.session.flush()
        return check_current_version_invariants(self.session)

    def check_extraction_cache(self) -> list[Any]:
        """Report cache keys that hold more than one entry (the unique constraint says: never)."""
        from ..database.integrity import check_extraction_cache

        self.session.flush()
        return check_extraction_cache(self.session)


__all__ = ["DOCUMENT_LEVEL_FIELDS", "DocumentRepository", "identity_for"]
