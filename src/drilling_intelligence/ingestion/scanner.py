"""Filesystem discovery (section 13).

The scanner answers "which files in this folder could be drilling documents?" -
nothing else.  It does not hash, does not open files and does not touch the
database, so it can run in a background thread and be cancelled cheaply.

Safety rules:
*   symlink loops are refused (``follow_symlinks=False`` by default);
*   oversized files are reported as SKIPPED with a reason, never silently dropped;
*   ignore patterns cover Office lock/temp files (``~$DDR.xlsx``) which would
    otherwise be registered as corrupt documents.
"""

from __future__ import annotations

import fnmatch
import os
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from ..core.errors import ScannerError
from ..core.logging import get_logger

log = get_logger("ingestion.scanner")

MIME_BY_EXTENSION = {
    ".pdf": "application/pdf",
    ".xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    ".xlsm": "application/vnd.ms-excel.sheet.macroEnabled.12",
    ".xls": "application/vnd.ms-excel",
    ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    ".doc": "application/msword",
    ".pptx": "application/vnd.openxmlformats-officedocument.presentationml.presentation",
    ".txt": "text/plain",
    ".md": "text/markdown",
    ".csv": "text/csv",
    ".tsv": "text/tab-separated-values",
    ".log": "text/plain",
    ".json": "application/json",
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".tif": "image/tiff",
    ".tiff": "image/tiff",
}


@dataclass
class ScannedFile:
    """One candidate document on disk."""

    path: Path
    relative_path: str
    filename: str
    extension: str
    size_bytes: int
    modified_at: datetime
    #: Genuine creation time only: ``None`` on platforms whose ``stat`` has no birth
    #: time (see :mod:`drilling_intelligence.core.filesystem`) - never an inode change time.
    created_at: datetime | None = None
    #: ``st_ctime``: when the inode last changed.  Recorded, never presented as creation.
    metadata_changed_at: datetime | None = None
    mime_type: str = ""
    #: Set when the file was excluded but still reported (auditable skips).
    excluded_reason: str = ""

    def identity(self) -> str:
        """Registry identity: normalised relative path (section 13)."""
        return self.relative_path.casefold().replace("\\", "/")

    def to_dict(self) -> dict[str, object]:
        return {
            "path": str(self.path),
            "relative_path": self.relative_path,
            "filename": self.filename,
            "extension": self.extension,
            "size_bytes": self.size_bytes,
            "modified_at": self.modified_at.isoformat(),
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "metadata_changed_at": self.metadata_changed_at.isoformat() if self.metadata_changed_at else None,
            "mime_type": self.mime_type,
            "excluded_reason": self.excluded_reason,
        }


@dataclass
class ScanResult:
    root: str
    files: list[ScannedFile] = field(default_factory=list)
    skipped: list[tuple[str, str]] = field(default_factory=list)
    #: Directories that could not be walked (permissions, loops).
    warnings: list[str] = field(default_factory=list)
    duration_ms: float = 0.0
    traversed_directories: int = 0

    @property
    def candidates(self) -> list[ScannedFile]:
        return [item for item in self.files if not item.excluded_reason]

    def by_extension(self) -> dict[str, int]:
        tally: dict[str, int] = {}
        for item in self.candidates:
            tally[item.extension or "(none)"] = tally.get(item.extension or "(none)", 0) + 1
        return tally

    def to_dict(self) -> dict[str, object]:
        return {
            "root": self.root,
            "candidates": len(self.candidates),
            "skipped": len(self.skipped),
            "by_extension": self.by_extension(),
            "warnings": list(self.warnings),
            "duration_ms": round(self.duration_ms, 1),
            "directories": self.traversed_directories,
        }


@dataclass
class FileScanner:
    """Walks a workspace corpus and yields candidate documents."""

    supported_extensions: tuple[str, ...] = (".pdf", ".xlsx", ".xlsm", ".docx", ".txt", ".md", ".csv", ".tsv")
    ignore_dir_names: tuple[str, ...] = (".git", ".drillintel", "node_modules", "__pycache__", ".venv", "venv", "dist", "build")
    ignore_file_patterns: tuple[str, ...] = ("~$*", "*.tmp", "*.part", "*.lock", ".DS_Store", "Thumbs.db")
    max_file_size_bytes: int = 512 * 1024 * 1024
    follow_symlinks: bool = False
    #: Extensions that should be reported as skipped even though we cannot parse them.
    report_unsupported: bool = True
    #: Optional cooperative cancellation hook used by the UI worker.
    cancel: Callable[[], bool] = lambda: False
    on_progress: Callable[[int, str], None] | None = None

    def __post_init__(self) -> None:
        self.supported_extensions = tuple(ext.lower() for ext in self.supported_extensions)
        self.ignore_file_patterns = tuple(self.ignore_file_patterns)

    # -- public -------------------------------------------------------------
    def scan(self, root: Path | str, *, extra_extensions: Iterable[str] | None = None) -> ScanResult:
        root_path = Path(root).expanduser().resolve()
        if not root_path.exists():
            raise ScannerError(f"scan root does not exist: {root_path}", root=str(root_path))
        extensions = set(self.supported_extensions) | {ext.lower() for ext in (extra_extensions or [])}
        result = ScanResult(root=str(root_path))
        import time

        started = time.perf_counter()
        seen_realpaths: set[str] = {str(root_path)}
        count = 0
        for dirpath, dirnames, filenames in os.walk(root_path, followlinks=self.follow_symlinks):
            if self.cancel():
                result.warnings.append("scan cancelled by user")
                break
            current = Path(dirpath)
            result.traversed_directories += 1
            # prune ignored directories in place (os.walk contract)
            kept: list[str] = []
            for name in dirnames:
                if name in self.ignore_dir_names or name.startswith("."):
                    continue
                target = current / name
                if target.is_symlink() and not self.follow_symlinks:
                    result.skipped.append((str(target), "symlink directory (follow_symlinks=false)"))
                    continue
                if target.is_symlink():
                    resolved = str(target.resolve())
                    if resolved in seen_realpaths:
                        result.warnings.append(f"symlink loop avoided: {target}")
                        continue
                    seen_realpaths.add(resolved)
                kept.append(name)
            dirnames[:] = sorted(kept)

            for filename in sorted(filenames):
                path = current / filename
                if self._ignored(filename):
                    result.skipped.append((str(path), "matches ignore pattern"))
                    continue
                extension = Path(filename).suffix.lower()
                if extension not in extensions:
                    if self.report_unsupported and extension:
                        result.skipped.append((str(path), f"unsupported extension {extension}"))
                    continue
                try:
                    stat = path.stat()
                except OSError as exc:
                    result.warnings.append(f"cannot stat {path}: {exc}")
                    continue
                if not self.follow_symlinks and path.is_symlink():
                    result.skipped.append((str(path), "symlink file (follow_symlinks=false)"))
                    continue
                modified = datetime.fromtimestamp(stat.st_mtime, tz=UTC)
                created = datetime.fromtimestamp(stat.st_ctime, tz=UTC) if hasattr(stat, "st_ctime") else None
                excluded_reason = ""
                if stat.st_size > self.max_file_size_bytes:
                    excluded_reason = f"exceeds max_file_size_mb ({stat.st_size} bytes > {self.max_file_size_bytes})"
                    result.skipped.append((str(path), excluded_reason))
                try:
                    relative = str(path.relative_to(root_path)).replace("\\", "/")
                except ValueError:
                    relative = path.name
                item = ScannedFile(
                    path=path,
                    relative_path=relative,
                    filename=filename,
                    extension=extension,
                    size_bytes=int(stat.st_size),
                    modified_at=modified,
                    created_at=created,
                    mime_type=MIME_BY_EXTENSION.get(extension, ""),
                    excluded_reason=excluded_reason,
                )
                result.files.append(item)
                count += 1
                if self.on_progress is not None and count % 25 == 0:
                    self.on_progress(count, relative)
        result.duration_ms = (time.perf_counter() - started) * 1000.0
        log.event(
            "scan.completed",
            root=str(root_path),
            candidates=len(result.candidates),
            skipped=len(result.skipped),
            directories=result.traversed_directories,
            duration_ms=result.duration_ms,
        )
        return result

    # -- helpers ------------------------------------------------------------
    def _ignored(self, filename: str) -> bool:
        for pattern in self.ignore_file_patterns:
            if fnmatch.fnmatch(filename, pattern):
                return True
        return filename.startswith(".")

    def summary(self) -> dict[str, object]:
        return {
            "supported_extensions": list(self.supported_extensions),
            "ignore_dir_names": list(self.ignore_dir_names),
            "ignore_file_patterns": list(self.ignore_file_patterns),
            "max_file_size_bytes": self.max_file_size_bytes,
            "follow_symlinks": self.follow_symlinks,
        }


__all__ = ["MIME_BY_EXTENSION", "FileScanner", "ScanResult", "ScannedFile"]
