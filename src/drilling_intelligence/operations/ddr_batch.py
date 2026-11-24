"""DDR batch PDF import (Phase C hardened / D-14..D-18).

Manifest-driven import of daily drilling report PDFs into the document registry.
Guarantees:

* **Strict manifest contract** — versioned JSON manifest; unknown keys, duplicate
  entries, absolute paths, `..` traversal and over-limit batches are rejected
  deterministically before any file is read (D-18).
* **Path containment (D-15)** — the base directory is resolved once; every manifest
  entry must be a relative, traversal-free, non-symlink-escaping regular file whose
  canonical resolved path stays inside the base. No string-prefix comparison.
* **Workspace-scoped identity (D-16)** — duplicate detection only considers versions
  owned by the same workspace; identical bytes in another workspace are a legitimate
  separate document, never a borrowed document id.
* **Exact-version review queue (D-17)** — the queue is keyed by
  (document_id, current document_version_id, sha256); a disposition on an older
  version never disposes the current one.
* **Resource bounds (D-18)** — max file count, per-file size, aggregate batch size,
  PDF signature check; streaming SHA-256; per-entry failure isolation.
* **Partial-failure reporting** — a bad entry is recorded FAILED and never aborts the
  batch or leaves a half-registered document.

Imported DDRs stay evidence/candidates until an explicit disposition — import is
not approval, and classification is not authorship.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ..classification.taxonomy import DocumentClassification
from ..core.errors import ValidationError

MANIFEST_FORMAT = "ddr-batch-manifest/v1"

#: Resource bounds (D-18): a manifest larger than this is rejected before any I/O.
MAX_MANIFEST_ENTRIES = 500
MAX_FILE_BYTES = 64 * 1024 * 1024  # 64 MiB per file
MAX_BATCH_BYTES = 512 * 1024 * 1024  # 512 MiB aggregate
MAX_MANIFEST_BYTES = 1024 * 1024  # 1 MiB manifest JSON

_PDF_SIGNATURE = b"%PDF-"

_ALLOWED_ENTRY_KEYS = {"path", "well_id", "note"}


@dataclass(frozen=True)
class ManifestEntry:
    path: str
    well_id: str | None = None
    note: str = ""


@dataclass
class BatchEntryResult:
    filename: str
    sha256: str = ""
    status: str = "FAILED"  # IMPORTED | SKIPPED_DUPLICATE | FAILED | UNSUPPORTED
    document_id: str | None = None
    document_version_id: str | None = None
    classification: str | None = None
    pending_review: bool = False
    error: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "filename": self.filename,
            "sha256": self.sha256,
            "status": self.status,
            "document_id": self.document_id,
            "document_version_id": self.document_version_id,
            "classification": self.classification,
            "pending_review": self.pending_review,
            "error": self.error,
        }


@dataclass
class BatchImportSummary:
    manifest_format: str = MANIFEST_FORMAT
    total: int = 0
    imported: int = 0
    skipped_duplicate: int = 0
    failed: int = 0
    pending_review: int = 0
    cancelled: int = 0
    entries: list[BatchEntryResult] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "manifest_format": self.manifest_format,
            "total": self.total,
            "imported": self.imported,
            "skipped_duplicate": self.skipped_duplicate,
            "failed": self.failed,
            "pending_review": self.pending_review,
            "cancelled": self.cancelled,
            "entries": [e.to_dict() for e in self.entries],
        }


def _validate_entry_path(raw: str, base_resolved: Path) -> Path:
    """D-15: prove the entry resolves to a regular file strictly inside base_dir."""
    if not isinstance(raw, str) or not raw.strip():
        raise ValidationError("manifest file entry 'path' must be a non-empty string")
    if Path(raw).is_absolute() or raw.startswith("~"):
        raise ValidationError(
            "manifest paths must be relative to the batch base directory", path=raw
        )
    parts = Path(raw).parts
    if any(part == ".." for part in parts):
        raise ValidationError("manifest path must not contain '..' traversal", path=raw)
    candidate = base_resolved / raw
    # Resolve and require canonical containment — never a string-prefix check.
    resolved = candidate.resolve(strict=False)
    if resolved != base_resolved and base_resolved not in resolved.parents:
        raise ValidationError("manifest path escapes the batch base directory", path=raw)
    # Symlink policy: no component of the relative path may be a symlink.
    probe = base_resolved
    for part in parts:
        probe = probe / part
        if probe.is_symlink():
            raise ValidationError("symlinks are not allowed in batch import paths", path=raw)
    if not resolved.is_file():
        raise ValidationError("manifest entry is not an existing regular file", path=raw)
    return resolved


def load_manifest(path: Path | str, *, base_dir: Path | str | None = None) -> list[ManifestEntry]:
    """Load and strictly validate a batch manifest JSON file (D-18)."""
    manifest_path = Path(path)
    try:
        if manifest_path.stat().st_size > MAX_MANIFEST_BYTES:
            raise ValidationError(
                "manifest file exceeds the maximum allowed size", limit_bytes=MAX_MANIFEST_BYTES
            )
        raw = json.loads(manifest_path.read_text(encoding="utf-8"))
    except ValidationError:
        raise
    except (OSError, json.JSONDecodeError) as exc:
        raise ValidationError(
            "batch manifest is unreadable or not valid JSON",
            path=str(manifest_path),
            error=str(exc),
        ) from exc
    if not isinstance(raw, dict) or raw.get("format") != MANIFEST_FORMAT:
        raise ValidationError(
            "unsupported batch manifest format",
            expected=MANIFEST_FORMAT,
            got=raw.get("format") if isinstance(raw, dict) else None,
        )
    unknown_top = sorted(set(raw) - {"format", "files"})
    if unknown_top:
        raise ValidationError("unknown manifest top-level key(s)", keys=unknown_top)
    files = raw.get("files")
    if not isinstance(files, list) or not files:
        raise ValidationError("batch manifest must list at least one file under 'files'")
    if len(files) > MAX_MANIFEST_ENTRIES:
        raise ValidationError(
            "manifest exceeds the maximum entry count",
            limit=MAX_MANIFEST_ENTRIES,
            got=len(files),
        )
    entries: list[ManifestEntry] = []
    seen: set[str] = set()
    for item in files:
        if not isinstance(item, dict):
            raise ValidationError("every manifest file entry must be an object")
        unknown = sorted(set(item) - _ALLOWED_ENTRY_KEYS)
        if unknown:
            raise ValidationError("unknown manifest entry key(s)", keys=unknown)
        entry_path = item.get("path")
        if not isinstance(entry_path, str) or not entry_path:
            raise ValidationError("every manifest file entry must have a non-empty 'path'")
        if entry_path in seen:
            raise ValidationError("duplicate manifest entry", path=entry_path)
        seen.add(entry_path)
        entries.append(
            ManifestEntry(
                path=entry_path,
                well_id=item.get("well_id") if isinstance(item.get("well_id"), str) else None,
                note=item.get("note") if isinstance(item.get("note"), str) else "",
            )
        )
    if base_dir is not None:
        base_resolved = Path(base_dir).resolve()
        for entry in entries:
            _validate_entry_path(entry.path, base_resolved)
    return entries


def _sha256_of(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _has_pdf_signature(path: Path) -> bool:
    try:
        with path.open("rb") as handle:
            return handle.read(5) == _PDF_SIGNATURE
    except OSError:
        return False


def _read_verified_entry(source: Path, workspace_root: Path) -> tuple[str, Path]:
    """D2: read an entry exactly once through a safe handle and stage the verified bytes.

    The path is validated (D-15) and then opened with ``O_NOFOLLOW``; the fd is
    fstat-ed to a regular file and read once, bounded by the per-file limit. The
    SHA-256 is computed over those exact bytes, which are staged to a private file
    that is what actually gets registered — so the bytes hashed are the bytes
    registered, and a file swapped between validation and use cannot slip through.
    Returns ``(sha256, staged_path)``; the caller must unlink the staged file.
    """
    import os
    import stat as _stat

    fd = os.open(str(source), os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    try:
        info = os.fstat(fd)
        if not _stat.S_ISREG(info.st_mode):
            raise ValidationError("manifest entry is not a regular file", path=str(source))
        if info.st_size > MAX_FILE_BYTES:
            raise ValidationError(
                f"file exceeds the per-file size limit ({MAX_FILE_BYTES} bytes)",
                path=str(source),
            )
        chunks = []
        total = 0
        while True:
            chunk = os.read(fd, 1 << 20)
            if not chunk:
                break
            total += len(chunk)
            if total > MAX_FILE_BYTES:
                raise ValidationError(
                    f"file exceeds the per-file size limit ({MAX_FILE_BYTES} bytes)",
                    path=str(source),
                )
            chunks.append(chunk)
    finally:
        os.close(fd)
    data = b"".join(chunks)
    if data[:5] != _PDF_SIGNATURE:
        raise ValidationError("file does not carry a PDF signature", path=str(source))
    digest = hashlib.sha256(data).hexdigest()
    staging_dir = Path(workspace_root) / ".drillintel" / "batch_imports" / "staging"
    staging_dir.mkdir(parents=True, exist_ok=True)
    staged = staging_dir / f"{digest[:16]}-{Path(source).name}"
    tmp = staged.with_suffix(staged.suffix + ".partial")
    tmp.write_bytes(data)
    tmp.replace(staged)
    return digest, staged


def _current_version_disposition(registry: Any, document_id: str) -> str | None:
    """D-17: disposition of the exact CURRENT version only, or None if pending.

    Extraction dispositions are provenance events on extracted candidates, and the
    event's locator names the exact ``document_version_id`` it disposes. A decision
    on version 1 therefore never disposes version 2.
    """
    from sqlalchemy import text

    doc = registry.repository.get(document_id)
    if doc is None:
        return None
    current = registry.repository.current_version(doc)
    if current is None:
        return None
    needle = f'"document_version_id": "{current.id}"'
    session = registry.repository.session
    # Table names are a fixed internal constant pair, never user input.
    for table in ("program_section", "procedure_step"):
        rows = session.execute(
            text("SELECT provenance FROM " + table + " WHERE provenance LIKE :needle"),  # noqa: S608
            {"needle": f"%{needle}%"},
        ).fetchall()
        for (raw_provenance,) in rows:
            provenance = (
                json.loads(raw_provenance) if isinstance(raw_provenance, str) else raw_provenance
            )
            for item in reversed(provenance or []):
                if isinstance(item, dict) and item.get("event") == "extraction_disposition":
                    locator = item.get("locator") or {}
                    if str(locator.get("document_version_id") or "") == current.id:
                        return str(item.get("decision", ""))
    return None


def import_ddr_batch(
    registry: Any,
    *,
    manifest_path: Path | str,
    base_dir: Path | str,
    workspace_root: Path | str,
    workspace_id: str,
    default_well_id: str | None = None,
    should_cancel: Any = None,
    on_entry: Any = None,
) -> BatchImportSummary:
    """Import every manifest entry with per-entry failure isolation (D-18).

    Duplicate detection is workspace-scoped (D-16); path containment is enforced
    per entry (D-15); pending-review counting uses the exact current version (D-17).
    ``workspace_id`` is mandatory (D1): a batch must never run against an absent or
    false workspace identity, and an unscoped lookup must never match orphaned rows.
    ``on_entry`` (D7) is a completion-only callback: it fires exactly once per
    manifest entry with that entry's final result (including CANCELLED).
    """
    if not isinstance(workspace_id, str) or not workspace_id.strip():
        raise ValidationError("workspace_id is required; batch import fails closed without it")
    base_resolved = Path(base_dir).resolve()
    # Manifest structure is validated wholesale; each path is validated per entry so
    # one bad path is a FAILED entry, never a whole-batch abort (D-18 partial failure).
    entries = load_manifest(manifest_path)
    summary = BatchImportSummary(total=len(entries))
    total_bytes = 0
    for entry in entries:
        if should_cancel is not None and should_cancel():
            result = BatchEntryResult(filename=Path(entry.path).name, status="CANCELLED")
            summary.entries.append(result)
            summary.cancelled += 1
            if on_entry is not None:
                on_entry(result)  # completion callback fires for cancelled entries too
            continue
        result = BatchEntryResult(filename=Path(entry.path).name)
        summary.entries.append(result)
        try:
            source = _validate_entry_path(entry.path, base_resolved)
            if source.suffix.lower() != ".pdf":
                result.status = "UNSUPPORTED"
                result.error = "only PDF files are accepted for DDR batch import"
            else:
                result.sha256, staged = _read_verified_entry(source, Path(workspace_root))
                size = len(staged.read_bytes())
                if total_bytes + size > MAX_BATCH_BYTES:
                    result.error = "batch aggregate size limit exceeded"
                else:
                    total_bytes += size
                    # D-16: duplicates are workspace-scoped, never borrowed across workspaces.
                    existing = registry.repository.version_by_sha_in_workspace(
                        result.sha256, workspace_id
                    )
                    if existing is not None:
                        result.status = "SKIPPED_DUPLICATE"
                        result.document_id = existing.document_id
                        result.document_version_id = existing.id
                        doc = registry.repository.get(existing.document_id)
                        result.classification = getattr(doc, "classification", None)
                        result.pending_review = (
                            result.classification == DocumentClassification.DDR.value
                            and _current_version_disposition(registry, existing.document_id) is None
                        )
                        summary.skipped_duplicate += 1
                        if result.pending_review:
                            summary.pending_review += 1
                    else:
                        registered = registry.register(
                            path=staged,
                            workspace_root=workspace_root,
                            workspace_id=workspace_id,
                            well_id=entry.well_id or default_well_id,
                            precomputed_sha=result.sha256,
                        )
                        # The staged copy stays: it is the registered source path, so
                        # later physical-byte re-verification still has a real file.
                        if not registered.ok:
                            result.error = registered.error or "registry refused the file"
                        else:
                            result.status = "IMPORTED"
                            result.document_id = registered.document_id
                            version = registry.repository.current_version(
                                registry.repository.get(registered.document_id)
                            )
                            result.document_version_id = version.id if version else None
                            result.classification = registered.classification
                            result.pending_review = (
                                registered.classification == DocumentClassification.DDR.value
                                and _current_version_disposition(registry, registered.document_id)
                                is None
                            )
                            summary.imported += 1
                            if result.pending_review:
                                summary.pending_review += 1
        except ValidationError as exc:
            # A bad PDF signature or oversize read is an unsupported entry, not a
            # processing failure; every other validation error fails the entry.
            if "PDF signature" in str(exc) or "size limit" in str(exc):
                result.status = "UNSUPPORTED"
            else:
                result.status = "FAILED"
            result.error = str(exc)
        except Exception as exc:  # noqa: BLE001 - batch must survive a single bad entry
            result.status = "FAILED"
            result.error = f"{type(exc).__name__}: {exc}"
        if result.status in ("FAILED", "UNSUPPORTED"):
            summary.failed += 1
        if on_entry is not None:
            on_entry(result)  # exactly once per entry, after its final outcome
    return summary


def review_queue(registry: Any, *, workspace_id: str | None = None) -> list[dict[str, Any]]:
    """D-17: exact-current-version review queue.

    Each entry identifies the exact document, its current version, the SHA-256 and
    the disposition recorded for THAT version. A decision on an older version never
    disposes the current one; historical decisions stay readable in version history.
    """
    documents = registry.repository.list_documents(
        workspace_id=workspace_id, classification=DocumentClassification.DDR.value
    )
    queue: list[dict[str, Any]] = []
    for doc in documents:
        current = registry.repository.current_version(doc)
        if current is None:
            continue
        disposition = _current_version_disposition(registry, doc.id)
        queue.append(
            {
                "document_id": doc.id,
                "document_version_id": current.id,
                "sha256": current.sha256,
                "filename": doc.filename,
                "classification": doc.classification,
                "version_disposition": disposition or "PENDING_REVIEW",
                "pending_review": disposition is None,
                "well_id": doc.well_id,
            }
        )
    return queue
