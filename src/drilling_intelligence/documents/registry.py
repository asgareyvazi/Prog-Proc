"""Document registry service: identity, versions, extraction, classification.

This is the module that realises sections 12, 13, 14, 16, 17 and 58 of the
master specification in one place, so the invariants are enforced once:

*   a document slot is identified by its normalised path inside the workspace;
*   content changes create a **new immutable version**, never an overwrite;
*   extraction is cached on ``(sha256, extractor, extractor_version, options)``;
*   classification and authority are recorded with their evidence;
*   every step is audited (``audit_event``) so a transformation can be explained
    months later.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from ..__init__ import CLASSIFIER_VERSION, EXTRACTION_ENGINE_VERSION
from ..classification.rules import DeterministicClassifier
from ..core.enums import DataQuality, DocumentClassification, FileChangeKind, ProcessingStatus
from ..core.errors import DrillingIntelligenceError, ExtractionError
from ..core.hashing import sha256_obj
from ..core.logging import get_logger
from ..core.units import format_number
from ..database.models import Document, DocumentVersion, Extraction
from ..extraction.fields import FieldExtractor, merge_field_sets
from ..extraction.interfaces import ExtractionContext
from ..extraction.normalized import NormalizedDocument, structure_digest
from .repository import DocumentRepository, identity_for
from .versioning import parse_revision

log = get_logger("documents.registry")


@dataclass
class RegistrationResult:
    """Outcome of registering (and processing) one file."""

    filename: str
    change: FileChangeKind
    document_id: str = ""
    version_id: str = ""
    extraction_id: str = ""
    #: Cache reuse rather than fresh parsing.
    from_cache: bool = False
    #: Extractor actually used (after any router fallback).
    extractor: str = ""
    classification: str = DocumentClassification.OTHER.value
    classification_confidence: float = 0.0
    title: str = ""
    sha256: str = ""
    pages: int = 0
    tables: int = 0
    paragraphs: int = 0
    fields: int = 0
    fields_unverified: int = 0
    duration_ms: float = 0.0
    warnings: list[str] = field(default_factory=list)
    error: str = ""
    error_code: str = ""

    @property
    def ok(self) -> bool:
        return not self.error

    def to_dict(self) -> dict[str, Any]:
        return {
            "filename": self.filename,
            "change": self.change.value,
            "document_id": self.document_id,
            "version_id": self.version_id,
            "extraction_id": self.extraction_id,
            "from_cache": self.from_cache,
            "extractor": self.extractor,
            "classification": self.classification,
            "classification_confidence": round(self.classification_confidence, 3),
            "title": self.title,
            "sha256": self.sha256[:16],
            "pages": self.pages,
            "tables": self.tables,
            "paragraphs": self.paragraphs,
            "fields": self.fields,
            "fields_unverified": self.fields_unverified,
            "duration_ms": round(self.duration_ms, 1),
            "warnings": list(self.warnings),
            "error": self.error,
            "error_code": self.error_code,
        }


#: Strings the extractors synthesise from page/sheet structure.  They are locators, not
#: titles, and must never be stored as a document title (section 22).
_SYNTHETIC_TITLE_MARKERS = ("sheet:", "page ", "lines ", "table of contents", "paragraph ")


class DocumentRegistry:
    """Registers files, extracts them and stores normalised content."""

    def __init__(
        self,
        repository: DocumentRepository,
        *,
        router: Any,
        settings: Any,
        classifier: DeterministicClassifier | None = None,
        field_extractor: FieldExtractor | None = None,
    ) -> None:
        self.repository = repository
        self.router = router
        self.settings = settings
        self.classifier = classifier or DeterministicClassifier()
        self.field_extractor = field_extractor or FieldExtractor()

    # -- registration -------------------------------------------------------
    def register(
        self,
        *,
        path: Path | str,
        workspace_root: Path | str,
        workspace_id: str | None,
        change: FileChangeKind = FileChangeKind.NEW,
        well_id: str | None = None,
        project_id: str | None = None,
        document_id: str | None = None,
        carry_forward: dict[str, Any] | None = None,
        duplicate_of: tuple[str, str] | None = None,
        precomputed_sha: str = "",
        extract: bool = True,
        force: bool = False,
    ) -> RegistrationResult:
        """Register one file (creating a version and, optionally, extracting it)."""
        import time

        started = time.perf_counter()
        source = Path(path)
        filename = source.name
        try:
            stat = source.stat()
        except OSError as exc:
            return RegistrationResult(
                filename=filename,
                change=change,
                error=f"file unreadable: {type(exc).__name__}: {exc}",
                error_code="SCANNER",
            )
        sha256 = precomputed_sha or self._sha(source, stat.st_size)
        identity = identity_for(workspace_root, source)
        result = RegistrationResult(filename=filename, change=change, sha256=sha256)

        document: Document | None = self.repository.get(document_id) if document_id else None
        if document is None:
            document = self.repository.by_identity(workspace_id, identity)
        if document is None:
            document = self.repository.create_document(
                workspace_id=workspace_id,
                identity_path=identity,
                filename=filename,
                extension=source.suffix.lower(),
                mime_type="",
                size_bytes=stat.st_size,
                sha256=sha256,
                file_created_at=datetime.fromtimestamp(stat.st_ctime, tz=UTC),
                file_modified_at=datetime.fromtimestamp(stat.st_mtime, tz=UTC),
                well_id=well_id,
                project_id=project_id,
                classification=DocumentClassification.OTHER,
            )
            self.repository.audit(action="document.registered", subject_type="document", subject_id=document.id, detail={"identity": identity, "sha256": sha256})
        elif carry_forward:
            applied = self.repository.update_document_metadata(document, {k: v for k, v in carry_forward.items() if v is not None})
            if applied:
                self.repository.audit(action="document.carry_forward", subject_type="document", subject_id=document.id, detail={"fields": sorted(applied)})

        result.document_id = document.id
        current = self.repository.version(document.current_version_id or "") if document.current_version_id else None
        needs_version = force or current is None or current.sha256 != sha256

        revision = parse_revision(filename, "", file_modified=datetime.fromtimestamp(stat.st_mtime, tz=UTC))
        version: DocumentVersion
        if needs_version:
            version = self.repository.create_version(
                document,
                sha256=sha256,
                source_path=str(source),
                size_bytes=stat.st_size,
                parser="",
                parser_version=EXTRACTION_ENGINE_VERSION,
                extraction_version=EXTRACTION_ENGINE_VERSION,
                origin=change,
                mime_type=document.mime_type,
                file_modified_at=datetime.fromtimestamp(stat.st_mtime, tz=UTC),
                revision=revision.revision or None,
                revision_key=revision.revision_key,
                status=revision.status.value if revision.status.value != "UNKNOWN" else document.status,
                supersedes_version_id=current.id if current is not None else None,
                duplicate_of_version_id=duplicate_of[1] if duplicate_of and duplicate_of[1] else None,
                metadata_json={"revision_notes": list(revision.notes), "revision_source": revision.source},
            )
            self.repository.touch_document_from_version(document, version)
            self.repository.audit(
                action="document.version_added",
                subject_type="document",
                subject_id=document.id,
                detail={"version": version.version_number, "origin": change.value, "sha256": sha256[:16], "supersedes": (current.id if current else None)},
            )
        elif current is not None:
            version = current
        else:  # pragma: no cover - defensive: the branch is unreachable by construction
            return RegistrationResult(
                filename=filename,
                change=change,
                document_id=document.id,
                error="registry inconsistency: no current version while the hash matched",
                error_code="REGISTRY",
            )
        result.version_id = version.id
        result.from_cache = not needs_version

        if not extract:
            self.repository.session.flush()
            result.duration_ms = (time.perf_counter() - started) * 1000.0
            return result

        # -- extraction (cached by content hash) ----------------------------
        # ``document`` is the registry row, ``parsed`` the normalised content; the
        # two are deliberately named apart because conflating them once meant the
        # ORM row was overwritten by an extractor result.
        config_hash = self._extraction_config_hash(source.suffix.lower())
        options = self._extractor_options()
        try:
            context = ExtractionContext(
                path=source,
                filename=filename,
                sha256=sha256,
                extension=source.suffix.lower(),
                size_bytes=stat.st_size,
                mime_type=document.mime_type or "",
                document_id=document.id,
                document_version_id=version.id,
                options=options,
            )
            context.complexity = self.router.probe(context)
            parsed, decision, extractor = self.router.extract(context, options=options)
            result.extractor = decision.extractor

            cached = None if force else self.repository.find_cached_extraction(
                content_sha256=sha256,
                extractor=extractor.name,
                extractor_version=extractor.version,
                config_hash=config_hash,
            )
            if cached is not None and cached.document_json:
                # Same bytes, same extractor, same options: reuse the stored
                # extraction instead of re-parsing (section 12 - incremental and
                # idempotent).  The row is still written for this version so the
                # audit trail shows which artefact each version used.
                result.from_cache = True
                payload = cached.document_json
                stats = dict(cached.stats or {})
                stats["reused_from_extraction_id"] = cached.id
                extraction = self.repository.save_extraction(
                    document=document,
                    version=version,
                    extractor=extractor.name,
                    extractor_version=extractor.version,
                    content_sha256=sha256,
                    config_hash=config_hash,
                    document_json=payload,
                    text=cached.text_blob or "",
                    stats=stats,
                    router_decision=decision.to_dict(),
                    status="CACHE_HIT",
                    duration_ms=0.0,
                )
                stored_fields = payload.get("extracted_fields") or []
                result.fields = len(stored_fields)
                result.fields_unverified = sum(1 for item in stored_fields if (item or {}).get("quality") in (DataQuality.UNVERIFIED.value, DataQuality.MISSING.value))
                result.pages = len(payload.get("pages") or [])
                result.tables = len(payload.get("tables") or [])
                result.paragraphs = len(payload.get("paragraphs") or [])
                result.warnings.append("extraction reused from cache (identical content hash)")
            else:
                # The extractor's own fields are cited to the exact cell or structured
                # item they came from; the rule pass adds what prose mentions.  Assigning
                # the rule output alone would throw the better evidence away.
                parsed.extracted_fields = merge_field_sets(
                    parsed.extracted_fields, self.field_extractor.apply(parsed)
                )
                stats = {
                    "chars": parsed.char_count,
                    "words": parsed.word_count,
                    "paragraphs": len(parsed.paragraphs),
                    "tables": len(parsed.tables),
                    "sections": len(parsed.sections),
                    "pages": len(parsed.pages),
                    "fields": len(parsed.extracted_fields),
                    "fields_valid": sum(1 for f in parsed.extracted_fields if f.quality is DataQuality.VALID),
                    "fields_unverified": sum(1 for f in parsed.extracted_fields if f.quality is DataQuality.UNVERIFIED),
                    "fields_invalid": sum(1 for f in parsed.extracted_fields if f.quality is DataQuality.INVALID),
                    "structure_digest": structure_digest(parsed),
                    "diagnostics": list(parsed.diagnostics),
                    "provenance_count": len(parsed.provenance),
                }
                extraction = self.repository.save_extraction(
                    document=document,
                    version=version,
                    extractor=decision.extractor,
                    extractor_version=extractor.version,
                    content_sha256=sha256,
                    config_hash=config_hash,
                    document_json=parsed.to_dict(),
                    text=parsed.text,
                    stats=stats,
                    router_decision=decision.to_dict(),
                    status="OK",
                    duration_ms=None,
                )
                result.pages = stats["pages"] or parsed.metadata.page_count
                result.tables = stats["tables"]
                result.paragraphs = stats["paragraphs"]
                result.fields = stats["fields"]
                result.fields_unverified = stats["fields_unverified"]
                result.warnings.extend(parsed.diagnostics)
                self.repository.set_processing(document, ProcessingStatus.EXTRACTED)

            version.parser = decision.extractor
            version.parser_version = extractor.version
            version.extraction_version = EXTRACTION_ENGINE_VERSION
            version.page_count = parsed.metadata.page_count or len(parsed.pages)
            version.word_count = parsed.word_count
            version.sheet_count = len(((parsed.metadata.extra.get("workbook") or {}).get("sheets")) or []) or None
            self.repository.session.flush()

            self._refine_revision(document, version, extraction, stat)
        except (ExtractionError, DrillingIntelligenceError) as exc:
            self.repository.set_processing(document, ProcessingStatus.FAILED, str(exc))
            self.repository.audit(action="extraction.failed", subject_type="document", subject_id=document.id, detail={"error": str(exc), "code": exc.code})
            result.error = str(exc)
            result.error_code = exc.code
            result.duration_ms = (time.perf_counter() - started) * 1000.0
            return result
        except Exception as exc:  # noqa: BLE001 - third-party parser boundary
            self.repository.set_processing(document, ProcessingStatus.FAILED, f"{type(exc).__name__}: {exc}")
            self.repository.audit(action="extraction.crashed", subject_type="document", subject_id=document.id, detail={"error": str(exc)})
            log.error_event("extraction.crashed", filename=filename, error=str(exc), exc_info=True)
            result.error = f"{type(exc).__name__}: {exc}"
            result.error_code = "EXTRACTION"
            result.duration_ms = (time.perf_counter() - started) * 1000.0
            return result

        # -- classification -------------------------------------------------
        classification, confidence, authority, notes, title = self._classify(document, version, extraction, filename)
        applied = self.repository.update_document_metadata(
            document,
            {
                "classification": classification,
                "classification_confidence": confidence,
                "source_authority": authority,
                "title": title,
            },
        )
        self.repository.set_processing(document, ProcessingStatus.CLASSIFIED)
        self.repository.audit(
            action="document.classified",
            subject_type="document",
            subject_id=document.id,
            detail={"classification": classification, "confidence": format_number(confidence), "applied": sorted(applied), "notes": notes},
        )
        result.classification = classification
        result.classification_confidence = confidence
        result.title = title
        result.warnings.extend(notes)
        self.repository.session.flush()
        result.duration_ms = (time.perf_counter() - started) * 1000.0
        log.event(
            "document.registered",
            level=15 if not result.error else 40,
            document_id=document.id,
            filename=filename,
            change=change.value,
            extractor=result.extractor,
            classification=classification,
            cache=result.from_cache,
            fields=result.fields,
            duration_ms=result.duration_ms,
        )
        return result

    def _refine_revision(self, document: Document, version: DocumentVersion, extraction: Extraction, stat: Any) -> None:
        """Prefer a revision stated inside the document over the filename guess.

        ``well_a3_program_rev12.pdf`` is evidence; a body line reading "Revision 14"
        is better evidence.  Only a *stronger* key replaces the filename one, and
        the replacement is recorded as an audit event - the registry never quietly
        rewrites history (section 85).
        """
        file_modified = datetime.fromtimestamp(stat.st_mtime, tz=UTC)
        refined = parse_revision("", extraction.text_blob or "", file_modified=file_modified)
        if refined.revision_key <= (version.revision_key or 0):
            return
        self.repository.audit(
            action="document.revision_refined",
            subject_type="document_version",
            subject_id=version.id,
            detail={
                "from": version.revision,
                "to": refined.revision,
                "from_key": version.revision_key,
                "to_key": refined.revision_key,
                "evidence": "revision marker found in the document body",
            },
        )
        version.revision = refined.revision
        version.revision_key = refined.revision_key
        if refined.status.value != "UNKNOWN":
            version.status = refined.status.value
            if document.status in {"", "UNKNOWN"}:
                # The registry row is what the list views read: an "Approved for
                # drilling" stamp found in the body has to reach it, or the badge and
                # the authority tier keep describing a document we have already read.
                document.status = refined.status.value
        if refined.revision_date is not None and document.document_date is None:
            document.document_date = refined.revision_date
        if refined.revision:
            document.revision = refined.revision
            document.revision_key = refined.revision_key
        self.repository.session.flush()

    # -- helpers ------------------------------------------------------------
    def _classify(self, document: Document, version: DocumentVersion, extraction: Extraction | None, filename: str) -> tuple[str, float, str, list[str], str]:
        """Deterministic classification over the stored extraction."""
        document_json = (extraction.document_json if extraction else None) or {}
        normalized = NormalizedDocument.from_dict(document_json) if document_json else NormalizedDocument()
        result = self.classifier.classify(
            filename=filename,
            text=normalized.text or (extraction.text_blob if extraction else "") or "",
            extension=Path(filename).suffix,
            document=normalized,
            declared_status=document.status,
            is_current=bool(version.is_current),
        )
        title = self._detect_title(normalized, filename, version)
        return result.classification.value, result.confidence, result.authority_tier, list(result.notes), title

    @staticmethod
    def _detect_title(normalized: NormalizedDocument, filename: str, version: DocumentVersion) -> str:
        """Prefer a real document title over the filename (which lies often)."""
        pdf_title = str((normalized.metadata.extra.get("pdf_metadata") or {}).get("title") or "").strip()
        core_title = str((normalized.metadata.extra.get("core_properties") or {}).get("title") or "").strip()
        if pdf_title and len(pdf_title) > 3:
            return pdf_title[:400]
        if core_title and len(core_title) > 3:
            return core_title[:400]
        # A workbook or CSV has no prose title: its name is the title the reader sees,
        # and "Sheet: Summary" is a locator rather than something a human wrote.
        if Path(filename).suffix.lower() in {".csv", ".tsv", ".xlsx", ".xlsm", ".xls"}:
            return core_title[:400] if core_title and len(core_title) > 3 else Path(filename).stem[:400]
        for paragraph in normalized.paragraphs[:12]:
            text = (paragraph.text or "").strip()
            if paragraph.is_heading and paragraph.style != "sheet" and len(text) > 5:
                return text[:400]
        for line in (normalized.text or "").splitlines()[:8]:
            stripped = line.strip()
            if len(stripped) > 12 and not stripped.lower().startswith((*_SYNTHETIC_TITLE_MARKERS, "http", "www.")):
                return stripped[:400]
        return Path(filename).stem[:400]

    def _sha(self, path: Path, size: int) -> str:
        from ..core.hashing import sha256_file

        chunk = int(getattr(getattr(self.settings, "ingestion", None), "hash_chunk_bytes", 1 << 20) or (1 << 20))
        try:
            return sha256_file(path, chunk)
        except OSError as exc:
            log.warning_event("hash.failed", path=str(path), error=str(exc))
            return ""

    def _extractor_options(self) -> dict[str, Any]:
        extraction = getattr(self.settings, "extraction", None)
        if extraction is None:
            return {}
        return {
            "pdf_max_pages": extraction.pdf_max_pages,
            "pdf_extract_tables": extraction.pdf_extract_tables,
            "pdf_min_table_rows": extraction.pdf_min_table_rows,
            "excel_max_sheets": extraction.excel_max_sheets,
            "excel_read_formulas": extraction.excel_read_formulas,
            "excel_read_hidden": extraction.excel_read_hidden,
            "text_max_bytes": extraction.text_max_bytes,
        }

    def _extraction_config_hash(self, extension: str) -> str:
        """Hash of the options that legitimately change extraction output.

        Including this in the cache key means a config change forces
        reprocessing instead of silently reusing an extraction made under
        different rules.
        """
        mineru = getattr(self.settings, "mineru", None)
        payload = {
            "options": self._extractor_options(),
            "mineru": {"mode": getattr(mineru, "mode", ""), "backend": getattr(mineru, "backend", "")},
            "engine": EXTRACTION_ENGINE_VERSION,
            "classifier": CLASSIFIER_VERSION,
            "extension": extension,
        }
        return sha256_obj(payload)[:16]

    # -- reprocessing -------------------------------------------------------
    def reprocess(self, document_id: str, *, workspace_root: Path | str, force: bool = True) -> RegistrationResult:
        document = self.repository.get(document_id)
        if document is None:
            return RegistrationResult(filename="(unknown)", change=FileChangeKind.NEW, error=f"document {document_id} not found", error_code="NOT_FOUND")
        version = self.repository.version(document.current_version_id or "")
        source = Path(version.source_path) if version and version.source_path else Path(workspace_root) / document.identity_path
        return self.register(
            path=source,
            workspace_root=workspace_root,
            workspace_id=document.workspace_id,
            change=FileChangeKind.UNCHANGED,
            document_id=document.id,
            precomputed_sha=document.sha256,
            force=force,
        )

    def extraction_document(self, document_id: str) -> NormalizedDocument | None:
        """Load the stored normalised document for a registry entry."""
        extraction = self.repository.latest_extraction(document_id)
        if extraction is None or not extraction.document_json:
            return None
        try:
            return NormalizedDocument.from_dict(extraction.document_json)
        except (json.JSONDecodeError, KeyError) as exc:  # pragma: no cover - corrupt store
            log.warning_event("extraction.unreadable", document_id=document_id, error=str(exc))
            return None


__all__ = ["DocumentRegistry", "RegistrationResult"]
