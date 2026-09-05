"""Incremental ingestion decisions (master spec section 13).

    NEW        -> no document slot with this identity, content hash unknown
    UNCHANGED  -> same identity, same content hash            -> not reprocessed
    MODIFIED   -> same identity, different content hash       -> new version
    DUPLICATE  -> identical content hash already registered   -> link, no reparse
    REMOVED    -> registered but no longer on disk            -> reported, never deleted

The planner is pure with respect to the *registry read* and produces a plan the
pipeline executes; that split is what makes the decision logic unit-testable
without touching the extractors, and it lets the UI show a preview ("3 new,
1 modified, 40 unchanged") before any work happens.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from ..core.enums import FileChangeKind
from ..core.hashing import sha256_file
from ..core.logging import get_logger
from ..documents.repository import DocumentRepository
from .scanner import ScannedFile

log = get_logger("ingestion.planner")


@dataclass
class PlannedFile:
    """A decision for one discovered file."""

    file: ScannedFile
    change: FileChangeKind
    identity: str
    sha256: str = ""
    document_id: str | None = None
    current_version_id: str | None = None
    #: ``(document_id, version_id)`` of an existing identical artefact.
    duplicate_of: tuple[str, str] | None = None
    reason: str = ""
    #: Extraction is only skipped when a usable cached extraction exists.
    needs_extraction: bool = True
    #: Registry metadata that should be preserved across a new version.
    carry_forward: dict[str, Any] = field(default_factory=dict)
    error: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "filename": self.file.filename,
            "relative_path": self.file.relative_path,
            "change": self.change.value,
            "reason": self.reason,
            "sha256": self.sha256[:16],
            "size_bytes": self.file.size_bytes,
            "document_id": self.document_id,
            "needs_extraction": self.needs_extraction,
            "duplicate_of": list(self.duplicate_of) if self.duplicate_of else None,
            "error": self.error,
        }


@dataclass
class RemovedFile:
    """Registered document that is no longer present in the scan root."""

    document_id: str
    identity_path: str
    filename: str
    last_seen: str
    reason: str = "not present in the current scan"

    def to_dict(self) -> dict[str, Any]:
        return {
            "document_id": self.document_id,
            "identity_path": self.identity_path,
            "filename": self.filename,
            "last_seen": self.last_seen,
            "reason": self.reason,
            "change": FileChangeKind.REMOVED.value,
        }


@dataclass
class ScanPlan:
    """Everything needed to execute an ingestion step, with the counts up front."""

    root: str
    items: list[PlannedFile] = field(default_factory=list)
    removed: list[RemovedFile] = field(default_factory=list)
    skipped: list[tuple[str, str]] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    duration_ms: float = 0.0
    hashed_bytes: int = 0

    def counts(self) -> dict[str, int]:
        tally = {kind.value: 0 for kind in FileChangeKind}
        for item in self.items:
            tally[item.change.value] += 1
        tally["SKIPPED"] = len(self.skipped)
        tally["TO_PROCESS"] = sum(1 for item in self.items if item.needs_extraction or item.change in (FileChangeKind.NEW, FileChangeKind.MODIFIED))
        return tally

    @property
    def work_items(self) -> list[PlannedFile]:
        return [item for item in self.items if item.change in (FileChangeKind.NEW, FileChangeKind.MODIFIED) or item.needs_extraction]

    def to_dict(self) -> dict[str, Any]:
        return {
            "root": self.root,
            "counts": self.counts(),
            "items": [item.to_dict() for item in self.items],
            "removed": [item.to_dict() for item in self.removed],
            "skipped": [{"path": path, "reason": reason} for path, reason in self.skipped[:200]],
            "warnings": list(self.warnings),
            "duration_ms": round(self.duration_ms, 1),
            "hashed_bytes": self.hashed_bytes,
        }

    def summary_line(self) -> str:
        counts = self.counts()
        return (
            f"{counts['NEW']} new, {counts['MODIFIED']} modified, {counts['UNCHANGED']} unchanged, "
            f"{counts['DUPLICATE']} duplicate, {counts['REMOVED']} missing"
            + (f", {counts['SKIPPED']} skipped" if counts["SKIPPED"] else "")
        )


class IngestionPlanner:
    """Compares a scan against the registry and produces a :class:`ScanPlan`."""

    def __init__(self, repository: DocumentRepository, *, settings: Any = None, hash_chunk_bytes: int = 1 << 20) -> None:
        self.repository = repository
        self.settings = settings
        self.hash_chunk_bytes = hash_chunk_bytes

    def plan(
        self,
        *,
        workspace_id: str | None,
        files: list[ScannedFile],
        root: str = "",
        force_reprocess: bool = False,
        cancel: Any = None,
        on_progress: Any = None,
        #: Workspace root the registry keys identities on.  The scanner reports paths
        #: relative to the *scan* root, so when the two differ the planner would label
        #: every file NEW on run 1 and REMOVED on run 2.  Passing the workspace root
        #: here (and using the registry's own function) makes that impossible.
        identity_root: Any = None,
        #: Files the scan found but this run was told not to touch (a per-run ``limit``).  Their
        #: identities still count as present: removal detection compares the registry with *the
        #: scan*, and a capped run that compares it with its own work list reports every file
        #: beyond the cap as missing.
        seen_but_not_processed: list[ScannedFile] | None = None,
    ) -> ScanPlan:
        import time

        started = time.perf_counter()
        plan = ScanPlan(root=root)
        seen_hashes: dict[str, tuple[str, str]] = {}  # sha -> (document_id, version_id)
        identities_in_scan: set[str] = set()

        for index, file in enumerate(files, start=1):
            if cancel is not None and cancel():
                plan.warnings.append("planning cancelled")
                break
            if file.excluded_reason:
                plan.skipped.append((str(file.path), file.excluded_reason))
                continue
            identity = self._identity(file, identity_root)
            identities_in_scan.add(identity)
            try:
                digest = sha256_file(file.path, self.hash_chunk_bytes)
                plan.hashed_bytes += file.size_bytes
            except OSError as exc:
                plan.items.append(
                    PlannedFile(
                        file=file,
                        change=FileChangeKind.NEW,
                        identity=identity,
                        reason="unreadable file",
                        needs_extraction=False,
                        error=f"{type(exc).__name__}: {exc}",
                    )
                )
                continue

            document = self.repository.by_identity(workspace_id, identity)
            if document is not None:
                plan.items.append(self._plan_known(file, document, digest, force_reprocess, seen_hashes, workspace_id, identity))
                continue

            # Unknown slot: is the *content* already registered somewhere else?
            existing_version = seen_hashes.get(digest) or self._existing_by_sha(digest)
            if existing_version is not None:
                seen_hashes[digest] = existing_version
                plan.items.append(
                    PlannedFile(
                        file=file,
                        change=FileChangeKind.DUPLICATE,
                        identity=identity,
                        sha256=digest,
                        duplicate_of=existing_version,
                        reason="identical content already registered under another path",
                        needs_extraction=True,  # a duplicate still needs its own registry row
                        carry_forward={},
                    )
                )
            else:
                seen_hashes[digest] = ("", "")
                plan.items.append(
                    PlannedFile(
                        file=file,
                        change=FileChangeKind.NEW,
                        identity=identity,
                        sha256=digest,
                        reason="no registry entry for this path",
                        needs_extraction=True,
                    )
                )
            if on_progress is not None:
                on_progress(index, file.relative_path)

        for file in seen_but_not_processed or ():
            if not file.excluded_reason:
                identities_in_scan.add(self._identity(file, identity_root))
        plan.removed = self._detect_removed(workspace_id, identities_in_scan)
        plan.duration_ms = (time.perf_counter() - started) * 1000.0
        log.event(
            "plan.created",
            new=plan.counts()["NEW"],
            modified=plan.counts()["MODIFIED"],
            unchanged=plan.counts()["UNCHANGED"],
            duplicate=plan.counts()["DUPLICATE"],
            removed=len(plan.removed),
            duration_ms=plan.duration_ms,
            hashed_mb=round(plan.hashed_bytes / 1e6, 2),
        )
        return plan

    # -- internals ----------------------------------------------------------
    @staticmethod
    def _identity(file: ScannedFile, identity_root: Any) -> str:
        """Identity exactly as the registry computes it for this path."""
        if identity_root is None:
            return file.identity()
        from ..documents.repository import identity_for

        return identity_for(identity_root, file.path)

    def _plan_known(
        self,
        file: ScannedFile,
        document: Any,
        digest: str,
        force_reprocess: bool,
        seen_hashes: dict[str, tuple[str, str]],
        workspace_id: str | None,
        identity: str = "",
    ) -> PlannedFile:
        identity = identity or self._identity(file, None)
        current_version_id = document.current_version_id or ""
        if document.sha256 == digest and not force_reprocess:
            extraction = self.repository.latest_extraction(document.id) if hasattr(self.repository, "latest_extraction") else None
            # A CACHE_HIT row *is* a usable extraction: it is the same content, already
            # parsed and stored.  Treating it as missing re-extracts forever.
            usable = extraction is not None and extraction.status in {"OK", "CACHE_HIT"}
            return PlannedFile(
                file=file,
                change=FileChangeKind.UNCHANGED,
                identity=identity,
                sha256=digest,
                document_id=document.id,
                current_version_id=current_version_id or None,
                reason="content hash matches the registry" + ("" if usable else "; no stored extraction, will extract"),
                needs_extraction=not usable,
            )
        if document.sha256 == digest and force_reprocess:
            return PlannedFile(
                file=file,
                change=FileChangeKind.UNCHANGED,
                identity=identity,
                sha256=digest,
                document_id=document.id,
                current_version_id=current_version_id or None,
                reason="content unchanged; forced reprocessing requested",
                needs_extraction=True,
            )
        duplicate = seen_hashes.get(digest) or self._existing_by_sha(digest)
        if duplicate and duplicate[0] and duplicate[0] != document.id:
            seen_hashes[digest] = duplicate
            return PlannedFile(
                file=file,
                change=FileChangeKind.DUPLICATE,
                identity=identity,
                sha256=digest,
                document_id=document.id,
                current_version_id=current_version_id or None,
                duplicate_of=duplicate,
                reason="new revision is byte-identical to an already registered document",
                needs_extraction=False,
            )
        return PlannedFile(
            file=file,
            change=FileChangeKind.MODIFIED,
            identity=identity,
            sha256=digest,
            document_id=document.id,
            current_version_id=current_version_id or None,
            reason="content hash differs from the registry: new document version",
            needs_extraction=True,
            carry_forward={
                "well_id": document.well_id,
                "project_id": document.project_id,
                "classification": document.classification,
                "title": document.title,
                "status": document.status,
                "source_authority": document.source_authority,
                "wellbore": document.wellbore,
                "notes": document.notes,
                "tags": list(document.tags or []),
            },
        )

    def _existing_by_sha(self, digest: str) -> tuple[str, str] | None:
        version = self.repository.version_by_sha(digest)
        if version is None:
            return None
        return (version.document_id, version.id)

    def _detect_removed(self, workspace_id: str | None, identities_in_scan: set[str]) -> list[RemovedFile]:
        if workspace_id is None:
            return []
        removed: list[RemovedFile] = []
        for document in self.repository.list_documents(workspace_id=workspace_id, limit=100000):
            if document.identity_path in identities_in_scan:
                continue
            removed.append(
                RemovedFile(
                    document_id=document.id,
                    identity_path=document.identity_path,
                    filename=document.filename,
                    last_seen=document.imported_at.isoformat() if document.imported_at else "",
                )
            )
        return removed


__all__ = ["IngestionPlanner", "PlannedFile", "RemovedFile", "ScanPlan"]
