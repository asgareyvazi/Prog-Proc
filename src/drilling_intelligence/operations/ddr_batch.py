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
import uuid
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


class _DirFdUnsupported(Exception):
    """Raised when the platform cannot open relative to a directory fd (Windows)."""


def _open_entry_fd_unix(base_resolved: Path, parts: list[str], raw: str) -> int:
    """Unix path: openat-style fd walk with O_NOFOLLOW at every component."""
    import os

    # B1: capability detection BEFORE touching platform-specific flags — on Windows
    # os.O_DIRECTORY does not exist and an AttributeError must never escape; the
    # portable handle-based path is selected instead.
    if not hasattr(os, "O_DIRECTORY"):
        raise _DirFdUnsupported("os.O_DIRECTORY is not available on this platform")
    try:
        dir_fd = os.open(
            str(base_resolved), os.O_RDONLY | os.O_DIRECTORY | getattr(os, "O_NOFOLLOW", 0)
        )
    except OSError as exc:
        raise ValidationError(
            f"manifest base cannot be safely opened: {exc.strerror}", path=raw
        ) from exc
    try:
        for component in parts[:-1]:
            try:
                nxt = os.open(
                    component,
                    os.O_RDONLY | os.O_DIRECTORY | getattr(os, "O_NOFOLLOW", 0),
                    dir_fd=dir_fd,
                )
            except (NotImplementedError, OSError) as exc:
                if isinstance(exc, NotImplementedError) or getattr(exc, "errno", None) in (22, 38):
                    raise _DirFdUnsupported(str(exc)) from exc
                raise ValidationError(
                    f"manifest path cannot be safely opened (symlink or missing component): {exc}",
                    path=raw,
                ) from exc
            os.close(dir_fd)
            dir_fd = nxt
        try:
            return os.open(parts[-1], os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0), dir_fd=dir_fd)
        except (NotImplementedError, OSError) as exc:
            if isinstance(exc, NotImplementedError) or getattr(exc, "errno", None) in (22, 38):
                raise _DirFdUnsupported(str(exc)) from exc
            raise ValidationError(
                f"manifest path cannot be safely opened (symlink or missing component): {exc}",
                path=raw,
            ) from exc
    finally:
        try:
            os.close(dir_fd)
        except OSError:
            pass


def _open_entry_fd_portable(base_resolved: Path, parts: list[str], raw: str) -> int:
    """B2: Windows handle-based safe traversal — NOT a check-then-use lstat walk.

    Each component is opened with ``CreateFileW`` using
    ``FILE_FLAG_BACKUP_SEMANTICS`` (directories) and ``FILE_FLAG_OPEN_REPARSE_POINT``
    (never follow a symlink/reparse point), and the handle is validated with
    ``GetFileType`` before use. The final component is opened as a file handle and
    converted to a CRT fd with ``msvcrt.open_osfhandle``, so the fd the caller reads
    from is the very handle that was validated — a path swapped between check and use
    cannot slip through. On any non-Windows platform that lacks dir_fd support this
    fails closed: secure Batch Import is NOT QUALIFIED there rather than silently
    downgraded.
    """
    import os

    if os.name != "nt":
        raise ValidationError(
            "no fd-relative traversal available on this platform; refusing insecure fallback",
            path=raw,
        )
    import ctypes
    from ctypes import wintypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    msvcrt = ctypes.CDLL("msvcrt")

    GENERIC_READ = 0x80000000
    FILE_SHARE_READ = 0x00000001
    FILE_SHARE_WRITE = 0x00000002
    OPEN_EXISTING = 3
    FILE_FLAG_BACKUP_SEMANTICS = 0x02000000
    FILE_FLAG_OPEN_REPARSE_POINT = 0x00200000
    FILE_TYPE_DISK = 0x0001
    INVALID_HANDLE_VALUE = wintypes.HANDLE(-1).value

    def _create(path: str, is_dir: bool) -> int:
        flags = FILE_FLAG_OPEN_REPARSE_POINT | (FILE_FLAG_BACKUP_SEMANTICS if is_dir else 0)
        handle = kernel32.CreateFileW(
            str(path),
            GENERIC_READ,
            FILE_SHARE_READ | FILE_SHARE_WRITE,
            None,
            OPEN_EXISTING,
            flags,
            None,
        )
        if handle == INVALID_HANDLE_VALUE:
            err = ctypes.get_last_error()
            raise ValidationError(
                f"manifest path component cannot be opened (reparse point or missing): win32 error {err}",
                path=raw,
            )
        if kernel32.GetFileType(handle) != FILE_TYPE_DISK:
            kernel32.CloseHandle(handle)
            raise ValidationError(
                "manifest path component is not a regular disk file/directory", path=raw
            )
        return handle

    handles: list[int] = []
    try:
        current = str(base_resolved)
        for component in parts[:-1]:
            current = current + "\\" + component
            handles.append(_create(current, is_dir=True))
        final = _create(current + "\\" + parts[-1], is_dir=False)
        handles.append(final)
        fd = msvcrt.open_osfhandle(final, os.O_RDONLY | os.O_BINARY)
        handles.remove(final)  # the CRT fd now owns the handle
        return fd
    finally:
        for handle in handles:
            kernel32.CloseHandle(handle)


def _open_entry_fd(base_resolved: Path, raw: str) -> int:
    """E2: TOCTOU-safe fd-based traversal of a manifest path — not O_NOFOLLOW-only.

    The base directory is opened once; every intermediate component is then opened
    relative to the *current directory fd* with ``O_NOFOLLOW | O_DIRECTORY`` and the
    final component with ``O_RDONLY | O_NOFOLLOW``. A symlink planted at ANY path
    component (including one swapped in after path validation) fails the open, and
    because no component is ever resolved through the filesystem by name alone, there
    is no check-then-use window. Returns the open fd of the regular file.
    """
    import os
    import stat as _stat

    parts = [p for p in Path(raw).parts if p not in (".", "..", "/")]
    if not parts or any(p == ".." for p in Path(raw).parts):
        raise ValidationError("manifest path must not contain '..' traversal", path=raw)
    try:
        fd = _open_entry_fd_unix(base_resolved, parts, raw)
    except _DirFdUnsupported:
        # B2: platforms without dir_fd-relative open (Windows) use the handle-based
        # CreateFileW traversal with reparse-point refusal — not a check-then-use walk.
        fd = _open_entry_fd_portable(base_resolved, parts, raw)
    try:
        info = os.fstat(fd)
        if not _stat.S_ISREG(info.st_mode):
            os.close(fd)
            raise ValidationError("manifest entry is not a regular file", path=raw)
    except Exception:
        try:
            os.close(fd)
        except OSError:
            pass
        raise
    return fd


def _stage_atomic(data: bytes, name: str, workspace_root: Path) -> Path:
    """E3: atomic, collision-safe staging — O_EXCL create, fsync, then rename.

    The temporary name is unique per attempt (O_EXCL refuses to clobber an existing
    file), the payload is fsynced before the atomic ``os.replace`` into the final
    digest-addressed name, so a crash can never leave a half-written file at the
    registered path and concurrent batches never overwrite each other's temp files.
    """
    import os

    staging_dir = Path(workspace_root) / ".drillintel" / "batch_imports" / "staging"
    staging_dir.mkdir(parents=True, exist_ok=True)
    digest = hashlib.sha256(data).hexdigest()
    staged = staging_dir / f"{digest[:16]}-{name}"
    tmp = staging_dir / f".{digest[:16]}.{uuid.uuid4().hex[:12]}.partial"
    fd = os.open(str(tmp), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        tmp.replace(staged)  # atomic rename into the digest-addressed final name
    except Exception:
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass
        raise
    return staged


def _read_verified_entry(
    source: Path, workspace_root: Path, base_resolved: Path
) -> tuple[str, Path]:
    """D2/E2/E3: read an entry exactly once through an fd-traversed safe handle.

    The manifest path is opened via :func:`_open_entry_fd` (fd-based, symlink-safe at
    every component); the fd is fstat-ed to a regular file and read once, bounded by
    the per-file limit. The SHA-256 is computed over those exact bytes, which are
    staged atomically (O_EXCL + fsync + rename) and the staged file is what actually
    gets registered — so the bytes hashed are the bytes registered.
    Returns ``(sha256, staged_path)``.
    """
    import os
    import stat as _stat

    fd = _open_entry_fd(
        base_resolved,
        str(source.relative_to(base_resolved))
        if str(source).startswith(str(base_resolved))
        else str(source),
    )
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
    staged = _stage_atomic(data, Path(source).name, workspace_root)
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
    # DDR candidate dispositions (Phase G) live in the append-only ddr_disposition
    # table, scoped to the exact current version id.
    from ..database.models import DdrDisposition

    rows = (
        session.query(DdrDisposition)
        .filter(DdrDisposition.document_version_id == current.id)
        .order_by(DdrDisposition.created_at.desc(), DdrDisposition.id.desc())
        .all()
    )
    if rows:
        return str(rows[0].decision)
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
        # E3: per-entry transaction — a SAVEPOINT wraps each entry so a failed entry
        # rolls back only its own partial writes; earlier committed entries survive.
        session = getattr(registry.repository, "session", None)
        savepoint = session.begin_nested() if session is not None else None
        try:
            source = _validate_entry_path(entry.path, base_resolved)
            if source.suffix.lower() != ".pdf":
                result.status = "UNSUPPORTED"
                result.error = "only PDF files are accepted for DDR batch import"
            else:
                result.sha256, staged = _read_verified_entry(
                    source, Path(workspace_root), base_resolved
                )
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
            if savepoint is not None:
                savepoint.rollback()
        except Exception as exc:  # noqa: BLE001 - batch must survive a single bad entry
            result.status = "FAILED"
            result.error = f"{type(exc).__name__}: {exc}"
            if savepoint is not None:
                savepoint.rollback()
        else:
            if savepoint is not None:
                savepoint.commit()
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
        # Phase H: candidate-level integration — the queue entry carries each
        # candidate's field, latest decision and real page locator, so the review
        # queue is a candidate-level worklist, not just a version-level flag.
        candidates: list[dict[str, Any]] = []
        try:
            from ..database.models import DdrDisposition

            rows = (
                registry.repository.session.query(DdrDisposition)
                .filter(DdrDisposition.document_version_id == current.id)
                .order_by(DdrDisposition.created_at, DdrDisposition.id)
                .all()
            )
            latest: dict[str, Any] = {}
            for row in rows:
                latest[row.candidate_id] = row
            for row in latest.values():
                candidates.append(
                    {
                        "candidate_id": row.candidate_id,
                        "field": row.field,
                        "decision": row.decision,
                        "extracted_value": row.extracted_value,
                        "edited_value": row.edited_value,
                        "locator": row.locator,
                        "actor": row.actor,
                    }
                )
        except Exception:  # noqa: BLE001 - queue must never fail because of candidate detail
            candidates = []
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
                "candidates": candidates,
            }
        )
    return queue
