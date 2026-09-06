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
from ..core.filesystem import file_timestamps, posix_relative
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

    @property
    def cache_enabled(self) -> bool:
        """``[extraction] cache_enabled`` - off means "parse everything, store nothing for reuse".

        Read per call rather than captured in ``__init__`` because the settings object is
        the same one the UI mutates: turning the cache off has to take effect on the next
        file, not after a restart.
        """
        return bool(getattr(getattr(self.settings, "extraction", None), "cache_enabled", True))

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
        # The hash is always computed from the bytes on disk.  A caller *may* offer a
        # precomputed hash (the scanner already hashed the file for the change plan) and
        # it is then verified against a fresh read, because a hash that is merely
        # believed - rather than measured - is how a modified file gets registered under
        # the previous version's identity.
        sha256 = self._sha(source, stat.st_size)
        if not sha256:
            return RegistrationResult(
                filename=filename,
                change=change,
                error=f"cannot read {filename} to hash it",
                error_code="SCANNER",
            )
        if precomputed_sha and precomputed_sha != sha256:
            log.warning_event(
                "identity.hash_disagrees",
                filename=filename,
                claimed=precomputed_sha[:16],
                measured=sha256[:16],
                note="the file changed between scanning and registration; the measured hash wins",
            )
        identity = identity_for(workspace_root, source)
        relative = posix_relative(source, workspace_root)
        timestamps = file_timestamps(source, stat=stat)
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
                file_created_at=timestamps.created_at,
                file_modified_at=timestamps.modified_at,
                fs_metadata_changed_at=timestamps.metadata_changed_at,
                well_id=well_id,
                project_id=project_id,
                classification=DocumentClassification.OTHER,
            )
            self.repository.audit(
                action="document.registered",
                subject_type="document",
                subject_id=document.id,
                detail={"identity": identity, "sha256": sha256},
            )
        elif carry_forward:
            applied = self.repository.update_document_metadata(
                document, {k: v for k, v in carry_forward.items() if v is not None}
            )
            if applied:
                self.repository.audit(
                    action="document.carry_forward",
                    subject_type="document",
                    subject_id=document.id,
                    detail={"fields": sorted(applied)},
                )

        result.document_id = document.id
        # A version means "a content state", so only a content difference creates one.
        # ``force`` re-extracts (it skips the cache and republishes the artefact) but it
        # must not invent a second version with the same hash: that would leave two
        # "different" versions of the document that a reviewer cannot tell apart, and it
        # would make ``version_by_sha`` dedupe meaningless.
        current = self.repository.current_version(document)
        needs_version = current is None or current.sha256 != sha256
        result.from_cache = False
        if needs_version and current is not None and change is FileChangeKind.UNCHANGED:
            # The file changed between the plan and this registration - the plan's hash
            # said "same content", the bytes on disk now say otherwise.  The measured hash
            # wins, and so does the honest change kind: recording this version as
            # UNCHANGED would leave a supersede chain that contradicts its own hashes.
            change = FileChangeKind.MODIFIED
            result.change = change
            log.warning_event(
                "plan.stale",
                file=filename,
                note="content changed after planning; version recorded as MODIFIED",
            )

        # Only the *modification* time is passed to the revision parser, and only as a
        # tie-breaker for a revision that the text itself states: filesystem times say
        # when this copy was written, which is not when the document was issued.
        revision = parse_revision(filename, "", file_modified=timestamps.modified_at)
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
                file_modified_at=timestamps.modified_at,
                source_relative_path=relative,
                revision=revision.revision or None,
                revision_key=revision.revision_key,
                status=revision.status.value
                if revision.status.value != "UNKNOWN"
                else document.status,
                supersedes_version_id=current.id if current is not None else None,
                duplicate_of_version_id=duplicate_of[1]
                if duplicate_of and duplicate_of[1]
                else None,
                metadata_json={
                    "revision_notes": list(revision.notes),
                    "revision_source": revision.source,
                    # Recorded, never relied on: the timestamps are properties of this
                    # copy on this disk, not of the document.
                    "filesystem": timestamps.to_dict(),
                    "relative_path": relative,
                },
            )
            self.repository.touch_document_from_version(document, version)
            self.repository.audit(
                action="document.version_added",
                subject_type="document",
                subject_id=document.id,
                detail={
                    "version": version.version_number,
                    "origin": change.value,
                    "sha256": sha256[:16],
                    "supersedes": (current.id if current else None),
                    "relative_path": relative,
                },
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
        if not needs_version:
            # No new version: the bytes are what the current version already records.
            result.from_cache = True

        if not extract:
            self.repository.session.flush()
            result.duration_ms = (time.perf_counter() - started) * 1000.0
            return result

        # -- extraction (cached by content hash) ----------------------------
        # ``document`` is the registry row, ``parsed`` the normalised content; the
        # two are deliberately named apart because conflating them once meant the
        # ORM row was overwritten by an extractor result.
        #
        # Order matters, and it is the whole point of this block:
        #
        #   1. hash the bytes                     (streaming, I/O only)
        #   2. route: pick the extractor cheaply  (extension + structural probe)
        #   3. ask the cache for that artefact    (one indexed row)
        #   4. only on a miss, run the parser
        #
        # The probe in step 2 is what makes the cache usable at all: without it we could
        # not know *which* extractor's output to look for, and looking up the cache after
        # parsing meant a duplicate file paid for a full reparse every single time.
        config_hash = self._extraction_config_hash(source.suffix.lower())
        options = self._extractor_options()
        extraction: Extraction | None = None
        decision: Any = None
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
            decision = self.router.route(context, options=options)

            cached = (
                None
                if (force or not self.cache_enabled)
                else self.find_cached(sha256=sha256, decision=decision, config_hash=config_hash)
            )
            if cached is not None:
                extraction = self._store_cached_extraction(
                    document=document,
                    version=version,
                    context=context,
                    decision=decision,
                    cached=cached,
                    config_hash=config_hash,
                    result=result,
                )
                self._apply_cache_hit_counts(version, extraction, decision)
            else:
                extraction = self._extract_and_store(
                    document=document,
                    version=version,
                    context=context,
                    decision=decision,
                    options=options,
                    config_hash=config_hash,
                    sha256=sha256,
                    result=result,
                    force=force,
                )

            result.extraction_id = extraction.id
            self._refine_revision(document, version, extraction, stat)
        except (ExtractionError, DrillingIntelligenceError) as exc:
            self._record_extraction_failure(document, exc)
            result.error = str(exc)
            result.error_code = exc.code
            result.duration_ms = (time.perf_counter() - started) * 1000.0
            return result
        except Exception as exc:  # noqa: BLE001 - third-party parser boundary
            self.repository.set_processing(
                document, ProcessingStatus.FAILED, f"{type(exc).__name__}: {exc}"
            )
            self.repository.audit(
                action="extraction.crashed",
                subject_type="document",
                subject_id=document.id,
                detail={"error": str(exc)},
            )
            log.error_event("extraction.crashed", file=filename, error=str(exc), exc_info=True)
            result.error = f"{type(exc).__name__}: {exc}"
            result.error_code = "EXTRACTION"
            result.duration_ms = (time.perf_counter() - started) * 1000.0
            return result

        # -- classification -------------------------------------------------
        classification, confidence, authority, notes, title = self._classify(
            document, version, extraction, filename
        )
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
            detail={
                "classification": classification,
                "confidence": format_number(confidence),
                "applied": sorted(applied),
                "notes": notes,
            },
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
            # ``file``, not ``filename``: the latter is an attribute of ``logging``'s
            # LogRecord and passing it in ``extra`` raises.  The level-40 branch below is
            # exactly where that would have surfaced - a crashed parser reporting itself
            # through a second exception.
            file=filename,
            change=change.value,
            extractor=result.extractor,
            classification=classification,
            cache=result.from_cache,
            fields=result.fields,
            duration_ms=result.duration_ms,
        )
        return result

    # -- extraction: cache lookup, reuse, and the parse-on-miss path ---------
    def find_cached(self, *, sha256: str, decision: Any, config_hash: str) -> Extraction | None:
        """The stored artefact this file would be extracted into, if one exists.

        Keyed exactly the way the artefact is identified on disk - content hash, the
        extractor that *would* run, that extractor's version, and the option hash - so a
        hit means "byte-for-byte the same result", and any change to what could change
        the result (parser code, options) misses and reprocesses.
        """
        if not sha256:  # pragma: no cover - unreadable files are reported earlier
            return None
        return self.repository.find_cached_extraction(
            content_sha256=sha256,
            extractor=decision.extractor,
            extractor_version=decision.extractor_version,
            config_hash=config_hash,
        )

    def _store_cached_extraction(
        self,
        *,
        document: Document,
        version: DocumentVersion,
        context: ExtractionContext,
        decision: Any,
        cached: Extraction,
        config_hash: str,
        result: RegistrationResult,
    ) -> Extraction:
        """Write this version's extraction row from the cached artefact - no parsing.

        The row still exists per version, because the audit trail has to answer "what did
        *this* version contain" without walking the cache; but its payload is the cached
        document and its duration is zero, and the pointer to the source artefact is kept
        in ``stats`` so a reviewer can follow it back to the run that actually parsed.
        """
        result.from_cache = True
        payload = cached.document_json or {}
        stats = dict(cached.stats or {})
        stats["reused_from_extraction_id"] = cached.id
        stats["cache_hit"] = True
        stats["diagnostics"] = [
            *stats.get("diagnostics", []),
            "extraction reused from cache (identical content hash)",
        ]
        extraction = self.repository.save_extraction(
            document=document,
            version=version,
            extractor=decision.extractor,
            extractor_version=decision.extractor_version,
            content_sha256=context.sha256,
            config_hash=config_hash,
            document_json=payload,
            text=cached.text_blob or "",
            stats=stats,
            # The routing decision is recorded even though nothing ran: "why was this
            # version read this way" has the same answer as "why was the cached artefact
            # produced this way", and the cache pointer makes that traceable.
            router_decision=decision.to_dict(),
            status="CACHE_HIT",
            duration_ms=0.0,
            cache=False,
        )
        self.repository.cache_hit(
            content_sha256=context.sha256,
            extractor=decision.extractor,
            extractor_version=decision.extractor_version,
            config_hash=config_hash,
        )
        stored_fields = payload.get("extracted_fields") or []
        result.extractor = decision.extractor
        result.fields = len(stored_fields)
        result.fields_unverified = sum(
            1
            for item in stored_fields
            if (item or {}).get("quality")
            in (DataQuality.UNVERIFIED.value, DataQuality.MISSING.value)
        )
        result.pages = len(payload.get("pages") or [])
        result.tables = len(payload.get("tables") or [])
        result.paragraphs = len(payload.get("paragraphs") or [])
        result.warnings.append("extraction reused from cache (identical content hash)")
        return extraction

    @staticmethod
    def _apply_cache_hit_counts(
        version: DocumentVersion, extraction: Extraction, decision: Any
    ) -> None:
        """Stamp the version from the *stored* artefact, never from a fresh parse."""
        payload = extraction.document_json or {}
        stats = extraction.stats or {}
        version.parser = decision.extractor
        version.parser_version = decision.extractor_version
        version.extraction_version = EXTRACTION_ENGINE_VERSION
        version.page_count = int(stats.get("pages") or 0) or None
        version.word_count = int(stats.get("words") or 0) or None
        sheets = (
            ((payload.get("metadata") or {}).get("extra") or {}).get("workbook", {}).get("sheets")
        )
        version.sheet_count = len(sheets or []) or None

    def _extract_and_store(
        self,
        *,
        document: Document,
        version: DocumentVersion,
        context: ExtractionContext,
        decision: Any,
        options: dict[str, Any],
        config_hash: str,
        sha256: str,
        result: RegistrationResult,
        force: bool,
    ) -> Extraction:
        """Run the parser (cache miss / forced reprocess) and store what it produced."""
        parsed, routed, extractor = self.router.extract(context, options=options, decision=decision)
        decision = routed
        result.extractor = decision.extractor

        # The extractor's own fields are cited to the exact cell or structured item they
        # came from; the rule pass adds what prose mentions.  Assigning the rule output
        # alone would throw the better evidence away.
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
            "fields_valid": sum(
                1 for f in parsed.extracted_fields if f.quality is DataQuality.VALID
            ),
            "fields_unverified": sum(
                1 for f in parsed.extracted_fields if f.quality is DataQuality.UNVERIFIED
            ),
            "fields_invalid": sum(
                1 for f in parsed.extracted_fields if f.quality is DataQuality.INVALID
            ),
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
            # A forced reprocess deliberately republishes the artefact: what the cache
            # serves has to be the newest parse, while the superseded row stays on disk.
            # With ``cache_enabled`` off, nothing is published at all.
            cache=self.cache_enabled,
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
        version.sheet_count = (
            len(((parsed.metadata.extra.get("workbook") or {}).get("sheets")) or []) or None
        )
        if force:
            result.warnings.append(
                "forced reprocess: the extractor ran even though a cached artefact existed"
            )
            self.repository.audit(
                action="extraction.reprocessed",
                subject_type="document_version",
                subject_id=version.id,
                detail={
                    "extractor": decision.extractor,
                    "extractor_version": extractor.version,
                    "sha256": sha256[:16],
                    "note": "cache deliberately bypassed; the artefact for this version was rewritten",
                },
            )
        self.repository.session.flush()
        return extraction

    def _record_extraction_failure(self, document: Document, exc: Exception) -> None:
        """Record a failed extraction so "why is this document empty" has an answer.

        No extraction row is written: a row without content would be indistinguishable
        from "this document has no text" (a scanned page), and that difference is exactly
        what a reviewer needs to see.  The failure lives on the registry row
        (``processing_status`` + ``processing_error``) and in the audit trail.
        """
        self.repository.set_processing(document, ProcessingStatus.FAILED, str(exc))
        self.repository.audit(
            action="extraction.failed",
            subject_type="document",
            subject_id=document.id,
            detail={"error": str(exc), "code": getattr(exc, "code", "")},
        )
        log.warning_event(
            "extraction.failed", document_id=document.id, file=document.filename, error=str(exc)
        )

    def _refine_revision(
        self, document: Document, version: DocumentVersion, extraction: Extraction, stat: Any
    ) -> None:
        """Prefer a revision stated inside the document over the filename guess.

        ``well_a3_program_rev12.pdf`` is evidence; a body line reading "Revision 14"
        is better evidence.  Only a *stronger* key replaces the filename one, and
        the replacement is recorded as an audit event - the registry never quietly
        rewrites history (section 85).
        """
        file_modified = version.file_modified_at or (
            datetime.fromtimestamp(stat.st_mtime, tz=UTC) if stat is not None else None
        )
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
    def _classify(
        self,
        document: Document,
        version: DocumentVersion,
        extraction: Extraction | None,
        filename: str,
    ) -> tuple[str, float, str, list[str], str]:
        """Deterministic classification over the stored extraction."""
        document_json = (extraction.document_json if extraction else None) or {}
        normalized = (
            NormalizedDocument.from_dict(document_json) if document_json else NormalizedDocument()
        )
        result = self.classifier.classify(
            filename=filename,
            text=normalized.text or (extraction.text_blob if extraction else "") or "",
            extension=Path(filename).suffix,
            document=normalized,
            declared_status=document.status,
            is_current=bool(version.is_current),
        )
        title = self._detect_title(normalized, filename, version)
        return (
            result.classification.value,
            result.confidence,
            result.authority_tier,
            list(result.notes),
            title,
        )

    @staticmethod
    def _detect_title(
        normalized: NormalizedDocument, filename: str, version: DocumentVersion
    ) -> str:
        """Prefer a real document title over the filename (which lies often)."""
        pdf_title = str(
            (normalized.metadata.extra.get("pdf_metadata") or {}).get("title") or ""
        ).strip()
        core_title = str(
            (normalized.metadata.extra.get("core_properties") or {}).get("title") or ""
        ).strip()
        if pdf_title and len(pdf_title) > 3:
            return pdf_title[:400]
        if core_title and len(core_title) > 3:
            return core_title[:400]
        # A workbook or CSV has no prose title: its name is the title the reader sees,
        # and "Sheet: Summary" is a locator rather than something a human wrote.
        if Path(filename).suffix.lower() in {".csv", ".tsv", ".xlsx", ".xlsm", ".xls"}:
            return (
                core_title[:400]
                if core_title and len(core_title) > 3
                else Path(filename).stem[:400]
            )
        for paragraph in normalized.paragraphs[:12]:
            text = (paragraph.text or "").strip()
            if paragraph.is_heading and paragraph.style != "sheet" and len(text) > 5:
                return text[:400]
        for line in (normalized.text or "").splitlines()[:8]:
            stripped = line.strip()
            if len(stripped) > 12 and not stripped.lower().startswith(
                (*_SYNTHETIC_TITLE_MARKERS, "http", "www.")
            ):
                return stripped[:400]
        return Path(filename).stem[:400]

    def _sha(self, path: Path, size: int) -> str:
        from ..core.hashing import sha256_file

        chunk = int(
            getattr(getattr(self.settings, "ingestion", None), "hash_chunk_bytes", 1 << 20)
            or (1 << 20)
        )
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
            "excel_max_cells": getattr(extraction, "excel_max_cells", 60000),
            "excel_max_bytes": getattr(extraction, "excel_max_bytes", 64 * 1024 * 1024),
            "excel_read_formulas": extraction.excel_read_formulas,
            "excel_read_hidden": extraction.excel_read_hidden,
            "text_max_bytes": extraction.text_max_bytes,
            "pdf_probe_pages": getattr(extraction, "pdf_probe_pages", 12),
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
            "mineru": {
                "mode": getattr(mineru, "mode", ""),
                "backend": getattr(mineru, "backend", ""),
            },
            "engine": EXTRACTION_ENGINE_VERSION,
            "classifier": CLASSIFIER_VERSION,
            "extension": extension,
        }
        return sha256_obj(payload)[:16]

    # -- reprocessing -------------------------------------------------------
    def reprocess(
        self, document_id: str, *, workspace_root: Path | str, force: bool = True
    ) -> RegistrationResult:
        """Re-register a document from the file on disk.

        Three rules make this safe to expose in a UI:

        *   the source is resolved through the *durable* reference (workspace-relative
            path) when the recorded absolute path no longer exists, so a moved workspace
            folder does not strand the history;
        *   if the file is gone or unreadable, that is reported as an error and **no
            version is created or updated** - a reprocess must never stamp the registry
            with "content I could not read";
        *   the content hash is measured from the current bytes, never taken from the
            database.  The whole point of reprocess is that the file may have changed
            while we were not looking, and a version recorded from the stored hash would
            be a version of a document we no longer have.
        """
        document = self.repository.get(document_id)
        if document is None:
            return RegistrationResult(
                filename="(unknown)",
                change=FileChangeKind.NEW,
                error=f"document {document_id} not found",
                error_code="NOT_FOUND",
            )
        version = self.repository.current_version(document)
        if version is None:
            return RegistrationResult(
                filename=document.filename,
                change=FileChangeKind.NEW,
                document_id=document.id,
                error=f"document {document.filename} has no version to reprocess",
                error_code="REGISTRY",
            )
        source = self.repository.resolve_source_path(
            version, document, workspace_root=workspace_root
        )
        if source is None:
            return RegistrationResult(
                filename=document.filename,
                change=FileChangeKind.UNCHANGED,
                document_id=document.id,
                version_id=version.id,
                error=(
                    f"source file for {document.filename} is not reachable (recorded path "
                    f"{version.source_path or 'missing'} and no match under the workspace root)"
                ),
                error_code="SCANNER",
            )
        return self.register(
            path=source,
            workspace_root=workspace_root,
            workspace_id=document.workspace_id,
            change=FileChangeKind.UNCHANGED,
            document_id=document.id,
            # Deliberately no ``precomputed_sha``: reprocess must measure the file as it
            # is now, which is also what turns a changed file into a new version.
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
