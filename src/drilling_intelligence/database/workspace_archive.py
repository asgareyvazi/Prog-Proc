"""Versioned archives of workspace-owned files, separate from database-only snapshots.

Version 1 archives regular files below the workspace root plus a transaction-consistent SQLite
snapshot. Configured external source paths are reported but never followed or copied implicitly.
Search indexes, caches, logs, credentials, VCS state, and transient files are explicitly excluded.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import sqlite3
import stat
import struct
import tempfile
import tomllib
import unicodedata
import zipfile
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import TYPE_CHECKING, Any
from urllib.parse import urlsplit, urlunsplit

from .. import __version__
from .backup import (
    _MAX_DATABASE_BYTES,
    BackupError,
    _database_path_from_marker,
    _digest_bytes,
    _health,
    _marker_config,
    _publish_directory_no_replace,
    _read_database_backup,
    _schema_revision,
    _sha256,
    _validate_restore_identity,
    _workspace_files,
)

if TYPE_CHECKING:
    from ..wells.workspace import Workspace

__all__ = [
    "WorkspaceArchiveError",
    "WorkspaceArchiveResult",
    "create_workspace_archive",
    "restore_workspace_archive",
]

_FORMAT = "drillintel-full-workspace-archive"
_FORMAT_VERSION = 1
_META_MEMBER = "__drillintel_archive__/manifest.json"
_DATA_DIR = ".drillintel"
_CHUNK_BYTES = 1024 * 1024
_MAX_ENTRIES = 50_000
_MAX_FILE_BYTES = 8 * 1024 * 1024 * 1024
_MAX_TOTAL_BYTES = 32 * 1024 * 1024 * 1024
_MAX_ARCHIVE_BYTES = _MAX_TOTAL_BYTES + 64 * 1024 * 1024
_MAX_MANIFEST_BYTES = 16 * 1024 * 1024
_MAX_REPORTED_OMISSIONS = 10_000
_MAX_PATH_BYTES = 4096

_IGNORED_DIRECTORY_NAMES = frozenset(
    {
        ".git",
        ".hg",
        ".svn",
        ".bzr",
        ".ssh",
        ".aws",
        ".azure",
        ".kube",
        ".venv",
        "venv",
        "venv3",
        "node_modules",
        "__pycache__",
        ".pytest_cache",
        ".mypy_cache",
        ".ruff_cache",
        ".cache",
        ".tox",
        ".nox",
        ".next",
        ".nuxt",
        ".svelte-kit",
        ".turbo",
        ".vite",
        "htmlcov",
        "coverage",
        "dist",
        "build",
        "target",
    }
)
_IGNORED_FILE_PATTERNS = (
    re.compile(r"^\.env(?:\..+)?$", re.IGNORECASE),
    re.compile(r"^\.netrc$", re.IGNORECASE),
    re.compile(r"^\.npmrc$", re.IGNORECASE),
    re.compile(
        r"^(?:[A-Za-z0-9_-]+[._-])*(?:credentials?|secrets?|tokens?|private[_-]?keys?)(?:[._-].+)?$",
        re.IGNORECASE,
    ),
    re.compile(r"^(?:id_rsa|id_ed25519)(?:\..+)?$", re.IGNORECASE),
    re.compile(r"^.*\.(?:pem|key|p12|pfx|crt|cer|der)$", re.IGNORECASE),
    re.compile(r"^.*\.py[co]$", re.IGNORECASE),
    re.compile(r"^~\$.*$"),
    re.compile(r"^.*\.(?:tmp|part|lock)$", re.IGNORECASE),
    re.compile(r"^\.DS_Store$", re.IGNORECASE),
    re.compile(r"^Thumbs\.db$", re.IGNORECASE),
)
_WINDOWS_RESERVED = frozenset(
    {
        "CON",
        "PRN",
        "AUX",
        "NUL",
        *(f"COM{n}" for n in range(1, 10)),
        *(f"LPT{n}" for n in range(1, 10)),
    }
)


class WorkspaceArchiveError(BackupError):
    """A complete workspace archive cannot be created or restored safely."""

    code = "WORKSPACE_ARCHIVE"
    hint = (
        "Use workspace archive/restore-archive for workspace-owned files. External source paths "
        "are reported, not followed; restore is restricted to the recorded workspace root."
    )


@dataclass(frozen=True)
class WorkspaceArchiveResult:
    """Reproducible result and scope details for one full workspace archive operation."""

    action: str
    path: str
    workspace_name: str
    workspace_root: str
    archive_sha256: str
    schema_revision: str
    included_files: int
    included_directories: int
    included_bytes: int
    database_sha256: str
    excluded_items: tuple[dict[str, str], ...]
    unarchived_source_references: tuple[dict[str, str], ...]
    backup_scope: str = "full_workspace_owned_files"
    search_index_included: bool = False
    external_sources_included: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "action": self.action,
            "path": self.path,
            "workspace_name": self.workspace_name,
            "workspace_root": self.workspace_root,
            "archive_sha256": self.archive_sha256,
            "schema_revision": self.schema_revision,
            "included_files": self.included_files,
            "included_directories": self.included_directories,
            "included_bytes": self.included_bytes,
            "database_sha256": self.database_sha256,
            "excluded_items": [dict(item) for item in self.excluded_items],
            "unarchived_source_references": [
                dict(item) for item in self.unarchived_source_references
            ],
            "backup_scope": self.backup_scope,
            "search_index_included": self.search_index_included,
            "external_sources_included": self.external_sources_included,
        }


def _portable_member_path(raw: str, *, allow_directory: bool = False) -> str:
    try:
        encoded_length = len(raw.encode("utf-8")) if isinstance(raw, str) else _MAX_PATH_BYTES + 1
    except UnicodeEncodeError as exc:
        raise WorkspaceArchiveError("archive path is not valid Unicode", path=str(raw)) from exc
    if (
        not isinstance(raw, str)
        or not raw
        or "\\" in raw
        or "\x00" in raw
        or encoded_length > _MAX_PATH_BYTES
        or unicodedata.normalize("NFC", raw) != raw
    ):
        raise WorkspaceArchiveError(
            "archive path is not a portable normalized relative path", path=str(raw)
        )
    candidate = PurePosixPath(raw)
    windows = PureWindowsPath(raw)
    if (
        candidate.is_absolute()
        or windows.drive
        or windows.root
        or candidate.as_posix() != raw
        or any(part in {"", ".", ".."} for part in candidate.parts)
    ):
        raise WorkspaceArchiveError(
            "archive path is absolute, noncanonical, or traversing", path=raw
        )
    for part in candidate.parts:
        if (
            any(char in part for char in ':*?"<>|')
            or any(unicodedata.category(char) == "Cc" for char in part)
            or len(part.encode("utf-8")) > 255
            or part.endswith((" ", "."))
        ):
            raise WorkspaceArchiveError(
                "archive path contains a platform-invalid component", path=raw
            )
        if part.split(".", 1)[0].upper() in _WINDOWS_RESERVED:
            raise WorkspaceArchiveError(
                "archive path uses a reserved Windows device name", path=raw
            )
    normalized = candidate.as_posix()
    if normalized.split("/", 1)[0].casefold() == "__drillintel_archive__":
        raise WorkspaceArchiveError("workspace uses a reserved archive metadata path", path=raw)
    if not allow_directory and raw.endswith("/"):
        raise WorkspaceArchiveError("file path must not end in a directory separator", path=raw)
    return normalized


def _ignored_directory(relative: str, name: str) -> str | None:
    if name.casefold() in _IGNORED_DIRECTORY_NAMES:
        return "development cache or environment"
    parts = PurePosixPath(relative).parts
    if len(parts) == 2 and parts[0] == _DATA_DIR and parts[1] in {"cache", "index", "logs"}:
        return {
            "cache": "rebuildable cache",
            "index": "rebuildable search index",
            "logs": "runtime logs",
        }[parts[1]]
    return None


def _ignored_file(name: str) -> str | None:
    for pattern in _IGNORED_FILE_PATTERNS:
        if pattern.fullmatch(name):
            if name.casefold().startswith(".env") or name.casefold() in {".netrc", ".npmrc"}:
                return "environment or package-manager credential file"
            if re.search(
                r"(?:credential|secret|token|private[_-]?key|id_rsa|id_ed25519|\.(?:pem|key|p12|pfx|crt|cer|der))",
                name,
                re.I,
            ):
                return "credential or private-key file"
            if name.casefold().endswith((".tmp", ".part", ".lock")) or name.startswith("~$"):
                return "transient or office lock file"
            return "runtime bytecode or operating-system metadata"
    return None


def _safe_file_hash(path: Path) -> tuple[str, int, int]:
    before = path.lstat()
    if not stat.S_ISREG(before.st_mode):
        raise WorkspaceArchiveError("workspace entry is not a regular file", path=str(path))
    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(path, flags)
    digest = hashlib.sha256()
    byte_count = 0
    try:
        opened = os.fstat(descriptor)
        if not stat.S_ISREG(opened.st_mode) or (opened.st_dev, opened.st_ino) != (
            before.st_dev,
            before.st_ino,
        ):
            raise WorkspaceArchiveError(
                "workspace file changed identity while opening", path=str(path)
            )
        with os.fdopen(descriptor, "rb", closefd=False) as handle:
            while chunk := handle.read(_CHUNK_BYTES):
                byte_count += len(chunk)
                if byte_count > _MAX_FILE_BYTES:
                    raise WorkspaceArchiveError(
                        "workspace file exceeds the per-file archive limit",
                        path=str(path),
                        limit_bytes=_MAX_FILE_BYTES,
                    )
                digest.update(chunk)
        after = os.fstat(descriptor)
        if byte_count != opened.st_size or (
            opened.st_dev,
            opened.st_ino,
            opened.st_size,
            opened.st_mtime_ns,
            opened.st_ctime_ns,
        ) != (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns, after.st_ctime_ns):
            raise WorkspaceArchiveError(
                "workspace file changed while it was being hashed", path=str(path)
            )
    finally:
        os.close(descriptor)
    return digest.hexdigest(), byte_count, stat.S_IMODE(before.st_mode) & 0o777


def _entry_role(relative: str, corpus_roots: tuple[str, ...], database_member: str) -> str:
    if relative == "workspace.toml":
        return "workspace_marker"
    if relative == database_member:
        return "database_snapshot"
    if any(relative == root or relative.startswith(root + "/") for root in corpus_roots):
        return "source_or_corpus_file"
    if relative.startswith(f"{_DATA_DIR}/exports/"):
        return "workspace_export_or_attachment"
    if relative.startswith(f"{_DATA_DIR}/"):
        return "workspace_owned_data"
    return "workspace_owned_file"


def _casefold_and_parent_check(entries: list[dict[str, Any]]) -> None:
    by_fold: dict[str, str] = {}
    kinds: dict[str, str] = {}
    for entry in entries:
        relative = str(entry["path"])
        folded = relative.casefold()
        previous = by_fold.get(folded)
        if previous is not None:
            if previous != relative:
                raise WorkspaceArchiveError(
                    "workspace contains paths that collide on a case-insensitive filesystem",
                    first=previous,
                    second=relative,
                )
            raise WorkspaceArchiveError(
                "workspace contains a duplicate archive path", path=relative
            )
        by_fold[folded] = relative
        kinds[folded] = str(entry["kind"])
        parts = PurePosixPath(relative).parts
        for index in range(1, len(parts)):
            prefix = "/".join(parts[:index]).casefold()
            if kinds.get(prefix) == "file":
                raise WorkspaceArchiveError(
                    "workspace file path is also a parent directory", path=relative
                )
    for relative, kind in tuple(kinds.items()):
        if kind != "file":
            continue
        prefix = relative + "/"
        if any(other.startswith(prefix) for other in kinds):
            raise WorkspaceArchiveError(
                "workspace file path conflicts with a child path", path=by_fold[relative]
            )


def _collect_workspace_entries(
    root: Path,
    database_source: Path,
    database_snapshot: Path,
    index_path: Path,
    corpus_roots: tuple[str, ...],
) -> tuple[list[dict[str, Any]], list[dict[str, str]], int]:
    database_member = database_source.relative_to(root).as_posix()
    entries: list[dict[str, Any]] = []
    excluded: list[dict[str, str]] = []
    total_bytes = 0

    def note_exclusion(relative: str, reason: str) -> None:
        if len(excluded) >= _MAX_REPORTED_OMISSIONS:
            raise WorkspaceArchiveError("workspace has too many excluded entries to report safely")
        excluded.append({"path": relative, "reason": reason})

    def walk(directory: Path) -> None:
        nonlocal total_bytes
        try:
            with os.scandir(directory) as scanner:
                children = sorted(scanner, key=lambda item: item.name)
        except OSError as exc:
            raise WorkspaceArchiveError(
                f"workspace directory cannot be inspected: {exc}", path=str(directory)
            ) from exc
        for child in children:
            path = Path(child.path)
            relative = path.relative_to(root).as_posix()
            _portable_member_path(relative, allow_directory=True)
            if path.is_relative_to(root / _DATA_DIR) and path in {
                database_source,
                Path(str(database_source) + "-wal"),
                Path(str(database_source) + "-shm"),
                index_path,
                Path(str(index_path) + "-wal"),
                Path(str(index_path) + "-shm"),
            }:
                note_exclusion(
                    relative,
                    "live SQLite file captured through the online snapshot or rebuildable index",
                )
                continue
            if relative == f"{_DATA_DIR}/index" or relative.startswith(f"{_DATA_DIR}/index/"):
                note_exclusion(relative, "rebuildable search index")
                continue
            is_directory = child.is_dir(follow_symlinks=False)
            directory_reason = _ignored_directory(relative, child.name) if is_directory else None
            if directory_reason:
                note_exclusion(relative, directory_reason)
                continue
            file_reason = _ignored_file(child.name) if not is_directory else None
            if file_reason:
                note_exclusion(relative, file_reason)
                continue
            if child.is_symlink():
                raise WorkspaceArchiveError(
                    "workspace contains a symlink in the archive scope; refusing to follow or omit it",
                    path=relative,
                )
            try:
                metadata = child.stat(follow_symlinks=False)
            except OSError as exc:
                raise WorkspaceArchiveError(
                    f"workspace entry cannot be inspected: {exc}", path=relative
                ) from exc
            if is_directory:
                mode = stat.S_IMODE(metadata.st_mode) & 0o777
                entries.append(
                    {
                        "path": relative,
                        "kind": "directory",
                        "mode": mode,
                        "role": "workspace_directory",
                    }
                )
                walk(path)
                continue
            if not stat.S_ISREG(metadata.st_mode):
                raise WorkspaceArchiveError(
                    "workspace contains a special file outside the archive scope", path=relative
                )
            _portable_member_path(relative)
            digest, size, mode = _safe_file_hash(path)
            if total_bytes + size > _MAX_TOTAL_BYTES:
                raise WorkspaceArchiveError(
                    "workspace exceeds the total uncompressed archive limit",
                    limit_bytes=_MAX_TOTAL_BYTES,
                    path=relative,
                )
            total_bytes += size
            entries.append(
                {
                    "path": relative,
                    "kind": "file",
                    "size_bytes": size,
                    "sha256": digest,
                    "mode": mode,
                    "role": _entry_role(relative, corpus_roots, database_member),
                }
            )

    walk(root)
    database_digest = _sha256(database_snapshot)
    database_size = database_snapshot.stat().st_size
    if database_size > _MAX_DATABASE_BYTES:
        raise WorkspaceArchiveError("consistent SQLite snapshot exceeds the 8 GiB limit")
    if database_size > _MAX_FILE_BYTES:
        raise WorkspaceArchiveError("consistent SQLite snapshot exceeds the per-file archive limit")
    total_bytes += database_size
    entries.append(
        {
            "path": database_member,
            "kind": "file",
            "size_bytes": database_size,
            "sha256": database_digest,
            "mode": 0o600,
            "role": "database_snapshot",
        }
    )
    if total_bytes > _MAX_TOTAL_BYTES:
        raise WorkspaceArchiveError(
            "workspace including its consistent database snapshot exceeds the total size limit"
        )
    _casefold_and_parent_check(entries)
    entries_by_path = {str(entry["path"]): entry for entry in entries}
    for entry in entries:
        parts = PurePosixPath(str(entry["path"])).parts
        for index in range(1, len(parts)):
            parent = entries_by_path.get("/".join(parts[:index]))
            if parent is None or parent["kind"] != "directory":
                raise WorkspaceArchiveError(
                    "workspace archive scope omits a required parent directory",
                    path=str(entry["path"]),
                )
    if len(entries) + 1 > _MAX_ENTRIES:
        raise WorkspaceArchiveError(
            "workspace exceeds the archive entry-count limit", limit=_MAX_ENTRIES
        )
    return (
        sorted(entries, key=lambda item: (str(item["path"]).casefold(), str(item["path"]))),
        excluded,
        total_bytes,
    )


def _redact_source_reference(value: str) -> str:
    """Avoid persisting URL user-info, query tokens, and fragments in archive metadata."""
    try:
        parsed = urlsplit(value)
    except ValueError:
        return "[unparseable source reference]"
    if parsed.netloc or parsed.scheme.casefold() in {"http", "https", "ftp", "s3", "gs", "ssh"}:
        authority = parsed.netloc.rsplit("@", 1)[-1]
        return urlunsplit((parsed.scheme, authority, parsed.path, "", ""))
    return value


def _source_reference_issues(
    database: Path,
    root: Path,
    archived_paths: set[str],
) -> tuple[dict[str, str], ...]:
    uri = f"{database.resolve().as_uri()}?mode=ro"
    try:
        connection = sqlite3.connect(uri, uri=True, timeout=5.0)
        rows: list[tuple[str, str, str, str]] = []
        rows.extend(
            ("document_version", str(record_id), str(source_path or ""), str(relative_path or ""))
            for record_id, source_path, relative_path in connection.execute(
                "SELECT id, source_path, source_relative_path FROM document_version ORDER BY id"
            )
        )
        rows.extend(
            ("intake_queue_item", str(record_id), str(source_path or ""), "")
            for record_id, source_path in connection.execute(
                "SELECT id, source_path FROM intake_queue_item ORDER BY id"
            )
        )
    except sqlite3.Error as exc:
        raise WorkspaceArchiveError(
            f"source references could not be audited from the database snapshot: {exc}"
        ) from exc
    finally:
        if "connection" in locals():
            connection.close()

    issues: list[dict[str, str]] = []
    root_resolved = root.resolve()
    for table, record_id, source_path, relative_path in rows:
        candidates: list[Path] = []
        if relative_path:
            raw_relative = Path(relative_path)
            if not raw_relative.is_absolute():
                candidates.append(root / raw_relative)
        has_external_reference = False
        if source_path:
            try:
                parsed_source = urlsplit(source_path)
            except ValueError:
                parsed_source = None
            windows_source = PureWindowsPath(source_path)
            if (parsed_source is not None and parsed_source.netloc and parsed_source.scheme) or (
                windows_source.drive and not Path(source_path).is_absolute()
            ):
                has_external_reference = True
            else:
                raw_source = Path(source_path).expanduser()
                candidates.append(raw_source if raw_source.is_absolute() else root / raw_source)
        recorded_path = _redact_source_reference(source_path or relative_path)
        if len(recorded_path) > 4096:
            raise WorkspaceArchiveError(
                "source reference path exceeds the report limit", record_id=record_id
            )
        if not candidates and not has_external_reference:
            reason = "source path is empty"
        else:
            external = has_external_reference
            missing = False
            invalid_resolution: str | None = None
            found_archived_member = False
            for candidate in candidates:
                try:
                    resolved = candidate.resolve(strict=False)
                except (OSError, RuntimeError, ValueError) as exc:
                    invalid_resolution = type(exc).__name__
                    continue
                try:
                    relative = resolved.relative_to(root_resolved).as_posix()
                except ValueError:
                    external = True
                else:
                    if relative in archived_paths:
                        found_archived_member = True
                        break
                    missing = True
            if found_archived_member:
                continue
            if external:
                reason = "external source path is not copied by default"
            elif missing:
                reason = "workspace source path is missing or explicitly excluded"
            else:
                reason = f"recorded source path cannot be resolved safely: {invalid_resolution or 'unknown'}"
        issues.append(
            {
                "table": table,
                "record_id": record_id,
                "recorded_path": recorded_path,
                "reason": reason,
            }
        )
        if len(issues) > _MAX_REPORTED_OMISSIONS:
            raise WorkspaceArchiveError("too many unarchived source references to report safely")
    return tuple(issues)


def _write_archive(
    archive_path: Path,
    entries: list[dict[str, Any]],
    manifest_bytes: bytes,
    root: Path,
    database_snapshot: Path,
) -> None:
    with zipfile.ZipFile(archive_path, "w", allowZip64=True) as archive:
        for entry in entries:
            relative = str(entry["path"])
            path = root / Path(*PurePosixPath(relative).parts)
            if entry["kind"] == "directory":
                info = zipfile.ZipInfo(relative + "/")
                info.create_system = 3
                info.external_attr = (stat.S_IFDIR | int(entry["mode"])) << 16
                info.compress_type = zipfile.ZIP_STORED
                archive.writestr(info, b"")
                continue
            source = database_snapshot if entry["role"] == "database_snapshot" else path
            info = zipfile.ZipInfo(relative)
            info.create_system = 3
            info.external_attr = (stat.S_IFREG | int(entry["mode"])) << 16
            info.compress_type = zipfile.ZIP_STORED
            digest = hashlib.sha256()
            copied = 0
            flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
            try:
                descriptor = os.open(source, flags)
            except OSError as exc:
                raise WorkspaceArchiveError(
                    f"workspace file changed before it could be archived: {exc}", path=relative
                ) from exc
            try:
                opened = os.fstat(descriptor)
                if not stat.S_ISREG(opened.st_mode):
                    raise WorkspaceArchiveError(
                        "archive source is no longer a regular file", path=relative
                    )
                with (
                    os.fdopen(descriptor, "rb", closefd=False) as handle,
                    archive.open(info, "w", force_zip64=True) as member,
                ):
                    while chunk := handle.read(_CHUNK_BYTES):
                        copied += len(chunk)
                        if copied > int(entry["size_bytes"]):
                            raise WorkspaceArchiveError(
                                "workspace file grew while it was archived", path=relative
                            )
                        digest.update(chunk)
                        member.write(chunk)
                after = os.fstat(descriptor)
            finally:
                os.close(descriptor)
            if copied != int(entry["size_bytes"]) or digest.hexdigest() != entry["sha256"]:
                raise WorkspaceArchiveError(
                    "workspace file changed after its manifest hash was measured", path=relative
                )
            if (
                opened.st_dev,
                opened.st_ino,
                opened.st_size,
                opened.st_mtime_ns,
                opened.st_ctime_ns,
            ) != (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns, after.st_ctime_ns):
                raise WorkspaceArchiveError(
                    "workspace file changed during archive creation", path=relative
                )
        archive.writestr(_META_MEMBER, manifest_bytes, compress_type=zipfile.ZIP_STORED)


def _preflight_zip(path: Path, archive_bytes: int) -> None:
    """Bound central-directory parsing before ``ZipFile`` allocates member metadata."""
    tail_size = min(archive_bytes, 22 + zipfile.ZIP_MAX_COMMENT + 20 + 56)
    with path.open("rb") as handle:
        handle.seek(archive_bytes - tail_size)
        tail = handle.read(tail_size)
    signature = b"PK\x05\x06"
    offset = tail.rfind(signature)
    if offset < 0 or len(tail) - offset < 22:
        raise WorkspaceArchiveError("archive has no complete end-of-central-directory record")
    try:
        (
            _signature,
            disk_number,
            central_disk,
            disk_entries,
            total_entries,
            central_size,
            central_offset,
            comment_length,
        ) = struct.unpack_from("<4s4H2LH", tail, offset)
    except struct.error as exc:
        raise WorkspaceArchiveError("archive end-of-central-directory record is malformed") from exc
    if offset + 22 + comment_length != len(tail):
        raise WorkspaceArchiveError("archive has trailing or truncated directory data")
    if disk_number or central_disk or disk_entries != total_entries:
        raise WorkspaceArchiveError("multi-disk archives are not supported")
    directory_start = archive_bytes - tail_size + offset
    if total_entries == 0xFFFF or central_size == 0xFFFFFFFF or central_offset == 0xFFFFFFFF:
        locator_offset = offset - 20
        if locator_offset < 0:
            raise WorkspaceArchiveError("ZIP64 archive locator is missing")
        try:
            locator_signature, locator_disk, zip64_offset, disk_count = struct.unpack_from(
                "<4sLQL", tail, locator_offset
            )
        except struct.error as exc:
            raise WorkspaceArchiveError("ZIP64 archive locator is malformed") from exc
        if locator_signature != b"PK\x06\x07" or locator_disk != 0 or disk_count != 1:
            raise WorkspaceArchiveError("ZIP64 multi-disk or invalid locator is not supported")
        zip64_absolute = int(zip64_offset)
        if zip64_absolute + 56 > archive_bytes:
            raise WorkspaceArchiveError("ZIP64 directory record points outside the archive")
        handle = path.open("rb")
        try:
            handle.seek(zip64_absolute)
            zip64 = handle.read(56)
        finally:
            handle.close()
        try:
            (
                zsig,
                record_size,
                _made,
                _needed,
                zdisk,
                zcentral_disk,
                zdisk_entries,
                ztotal,
                zsize,
                zoffset,
            ) = struct.unpack("<4sQ2H2L4Q", zip64)
        except struct.error as exc:
            raise WorkspaceArchiveError("ZIP64 end record is malformed") from exc
        if (
            zsig != b"PK\x06\x06"
            or record_size != 44
            or zdisk
            or zcentral_disk
            or zdisk_entries != ztotal
        ):
            raise WorkspaceArchiveError("ZIP64 end record is inconsistent or uses extended data")
        total_entries, central_size, central_offset = ztotal, zsize, zoffset
        directory_start = zip64_absolute
    if total_entries < 1 or total_entries > _MAX_ENTRIES + 1:
        raise WorkspaceArchiveError("archive exceeds the configured entry count")
    if central_size > 64 * 1024 * 1024 or central_offset + central_size > directory_start:
        raise WorkspaceArchiveError("archive central directory exceeds the safe parse bound")


def _unique_json_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise WorkspaceArchiveError("archive manifest contains duplicate JSON keys", key=key)
        result[key] = value
    return result


def _validate_manifest_and_members(
    archive: zipfile.ZipFile,
) -> tuple[dict[str, Any], list[dict[str, Any]], zipfile.ZipInfo]:
    infos = archive.infolist()
    if len(infos) > _MAX_ENTRIES + 1:
        raise WorkspaceArchiveError("archive exceeds the configured entry count")
    names = [info.filename for info in infos]
    if len(names) != len(set(names)) or names.count(_META_MEMBER) != 1:
        raise WorkspaceArchiveError("archive members are duplicated or lack exactly one manifest")
    meta_info = next(info for info in infos if info.filename == _META_MEMBER)
    if meta_info.file_size > _MAX_MANIFEST_BYTES or meta_info.compress_type != zipfile.ZIP_STORED:
        raise WorkspaceArchiveError("archive manifest exceeds its size bound or uses compression")
    try:
        manifest = json.loads(archive.read(meta_info), object_pairs_hook=_unique_json_object)
    except (
        zipfile.BadZipFile,
        UnicodeDecodeError,
        json.JSONDecodeError,
        OSError,
        RecursionError,
    ) as exc:
        raise WorkspaceArchiveError(f"archive manifest cannot be read: {exc}") from exc
    if not isinstance(manifest, dict):
        raise WorkspaceArchiveError("archive manifest root must be a JSON object")
    if manifest.get("format") != _FORMAT or manifest.get("format_version") != _FORMAT_VERSION:
        raise WorkspaceArchiveError("unsupported full workspace archive format")
    if manifest.get("backup_scope") != "full_workspace_owned_files":
        raise WorkspaceArchiveError("archive does not declare the full workspace-owned-files scope")
    entries = manifest.get("entries")
    if not isinstance(entries, list) or not entries or len(entries) > _MAX_ENTRIES:
        raise WorkspaceArchiveError("archive manifest has no entries or exceeds the entry limit")
    if len(entries) + 1 != len(infos):
        raise WorkspaceArchiveError("archive member count does not match the manifest")
    normalized: list[dict[str, Any]] = []
    expected_names = {_META_MEMBER}
    total_bytes = 0
    for raw in entries:
        if not isinstance(raw, dict):
            raise WorkspaceArchiveError("archive manifest entry must be an object")
        kind = raw.get("kind")
        if kind not in {"file", "directory"}:
            raise WorkspaceArchiveError("archive manifest entry has an invalid kind")
        raw_path = raw.get("path")
        if not isinstance(raw_path, str):
            raise WorkspaceArchiveError("archive manifest entry path must be a string")
        member = _portable_member_path(raw_path, allow_directory=kind == "directory")
        mode = raw.get("mode")
        if isinstance(mode, bool) or not isinstance(mode, int) or mode < 0 or mode > 0o777:
            raise WorkspaceArchiveError(
                "archive manifest entry has invalid permission bits", path=member
            )
        item = dict(raw)
        item["path"] = member
        item["mode"] = mode
        if kind == "file":
            size = raw.get("size_bytes")
            digest = raw.get("sha256")
            if (
                isinstance(size, bool)
                or not isinstance(size, int)
                or size < 0
                or size > _MAX_FILE_BYTES
                or not isinstance(digest, str)
                or re.fullmatch(r"[0-9a-f]{64}", digest) is None
            ):
                raise WorkspaceArchiveError(
                    "archive manifest file size or digest is invalid", path=member
                )
            total_bytes += size
            expected_names.add(member)
        else:
            expected_names.add(member + "/")
        normalized.append(item)
    if total_bytes > _MAX_TOTAL_BYTES:
        raise WorkspaceArchiveError("archive manifest exceeds the total uncompressed size limit")
    _casefold_and_parent_check(normalized)
    entries_by_path = {str(entry["path"]): entry for entry in normalized}
    valid_file_roles = {
        "workspace_marker",
        "database_snapshot",
        "source_or_corpus_file",
        "workspace_export_or_attachment",
        "workspace_owned_data",
        "workspace_owned_file",
    }
    for entry in normalized:
        role = entry.get("role")
        valid_role = (
            role == "workspace_directory"
            if entry["kind"] == "directory"
            else role in valid_file_roles
        )
        if not valid_role:
            raise WorkspaceArchiveError(
                "archive entry has an invalid declared file role", path=str(entry["path"])
            )
        if entry["path"] == "workspace.toml" and role != "workspace_marker":
            raise WorkspaceArchiveError("workspace.toml has an invalid declared file role")
        parts = PurePosixPath(str(entry["path"])).parts
        for index in range(1, len(parts)):
            parent = entries_by_path.get("/".join(parts[:index]))
            if parent is None or parent["kind"] != "directory":
                raise WorkspaceArchiveError(
                    "archive manifest omits an explicit parent directory", path=str(entry["path"])
                )
    expected_files = sum(entry["kind"] == "file" for entry in normalized)
    expected_directories = sum(entry["kind"] == "directory" for entry in normalized)
    for key, expected in (
        ("included_file_count", expected_files),
        ("included_directory_count", expected_directories),
        ("included_bytes", total_bytes),
    ):
        actual = manifest.get(key)
        if isinstance(actual, bool) or not isinstance(actual, int) or actual != expected:
            raise WorkspaceArchiveError(f"archive manifest {key} does not match its entries")
    if set(names) != expected_names:
        raise WorkspaceArchiveError("archive members do not match the manifest paths")
    by_name = {info.filename: info for info in infos}
    for entry in normalized:
        member_name = entry["path"] + ("/" if entry["kind"] == "directory" else "")
        info = by_name[member_name]
        mode = (info.external_attr >> 16) & 0xFFFF
        file_type = stat.S_IFMT(mode)
        if info.flag_bits & 0x1 or info.compress_type != zipfile.ZIP_STORED:
            raise WorkspaceArchiveError(
                "archive members must be unencrypted and uncompressed", path=member_name
            )
        expected_type = stat.S_IFDIR if entry["kind"] == "directory" else stat.S_IFREG
        if file_type != expected_type:
            raise WorkspaceArchiveError(
                "archive member is not the declared regular file/directory type", path=member_name
            )
        if entry["kind"] == "directory" and info.file_size != 0:
            raise WorkspaceArchiveError("archive directory entry contains data", path=member_name)
        if entry["kind"] == "file" and info.file_size != entry["size_bytes"]:
            raise WorkspaceArchiveError(
                "archive member size differs from the manifest", path=member_name
            )
    marker_info = by_name.get("workspace.toml")
    if marker_info is None or marker_info.file_size > 1024 * 1024:
        raise WorkspaceArchiveError("archive lacks a bounded workspace.toml marker")
    database_path = manifest.get("database_path")
    if not isinstance(database_path, str) or database_path not in {
        str(entry["path"])
        for entry in normalized
        if entry["kind"] == "file" and entry.get("role") == "database_snapshot"
    }:
        raise WorkspaceArchiveError("archive manifest does not identify its database snapshot")
    database_entries = [
        entry
        for entry in normalized
        if entry["kind"] == "file" and entry.get("role") == "database_snapshot"
    ]
    if len(database_entries) != 1:
        raise WorkspaceArchiveError("archive must contain exactly one database snapshot")
    if database_entries[0]["path"] != database_path:
        raise WorkspaceArchiveError("database member path and its declared role disagree")
    if database_entries[0]["size_bytes"] > _MAX_DATABASE_BYTES:
        raise WorkspaceArchiveError("database snapshot exceeds the 8 GiB database limit")
    if (
        manifest.get("search_index_included") is not False
        or manifest.get("external_sources_included") is not False
    ):
        raise WorkspaceArchiveError(
            "archive manifest makes an unsupported index or external-source claim"
        )
    root_name = manifest.get("workspace_root")
    root_mode = manifest.get("workspace_root_mode")
    if not isinstance(root_name, str) or not Path(root_name).is_absolute():
        raise WorkspaceArchiveError("archive manifest has no absolute workspace-root identity")
    if isinstance(root_mode, bool) or not isinstance(root_mode, int) or not 0 <= root_mode <= 0o777:
        raise WorkspaceArchiveError("archive root permission bits are invalid")
    schema_revision = manifest.get("schema_revision")
    if not isinstance(schema_revision, str) or not schema_revision:
        raise WorkspaceArchiveError("archive manifest has no schema revision")
    health = manifest.get("storage_health")
    fk_violations = health.get("foreign_key_violation_count") if isinstance(health, dict) else None
    if (
        not isinstance(health, dict)
        or health.get("status") != "healthy"
        or isinstance(fk_violations, bool)
        or fk_violations != 0
    ):
        raise WorkspaceArchiveError("archive manifest does not record a healthy database snapshot")
    if (
        manifest.get("database_sha256") != database_entries[0]["sha256"]
        or manifest.get("database_bytes") != database_entries[0]["size_bytes"]
    ):
        raise WorkspaceArchiveError("database summary differs from the database file entry")
    excluded = manifest.get("excluded_items")
    external = manifest.get("unarchived_source_references")
    if not isinstance(excluded, list) or len(excluded) > _MAX_REPORTED_OMISSIONS:
        raise WorkspaceArchiveError(
            "archive exclusion report is missing or exceeds the report limit"
        )
    if not all(
        isinstance(item, dict)
        and isinstance(item.get("path"), str)
        and isinstance(item.get("reason"), str)
        for item in excluded
    ):
        raise WorkspaceArchiveError("archive exclusion report contains malformed items")
    if not isinstance(external, list) or len(external) > _MAX_REPORTED_OMISSIONS:
        raise WorkspaceArchiveError(
            "archive source-reference report is missing or exceeds the report limit"
        )
    if not all(
        isinstance(item, dict)
        and all(
            isinstance(item.get(key), str)
            for key in ("table", "record_id", "recorded_path", "reason")
        )
        for item in external
    ):
        raise WorkspaceArchiveError("archive source-reference report contains malformed items")
    try:
        marker = archive.read(marker_info)
    except (zipfile.BadZipFile, OSError) as exc:
        raise WorkspaceArchiveError(f"workspace marker cannot be read: {exc}") from exc
    if _digest_bytes(marker) != next(
        str(entry["sha256"]) for entry in normalized if entry["path"] == "workspace.toml"
    ):
        raise WorkspaceArchiveError("workspace marker hash differs from the archive manifest")
    return manifest, normalized, by_name[database_path]


def create_workspace_archive(
    workspace: Workspace, destination: Path | str
) -> WorkspaceArchiveResult:
    """Create a hashed archive of workspace-owned files and a consistent SQLite snapshot.

    Source paths outside the workspace are listed in the manifest, never followed or included.
    Restore is bound to the exact recorded workspace root to preserve the database identity.
    """
    root = Path(workspace.root).resolve()
    requested_output = Path(destination).expanduser().absolute()
    if os.path.lexists(requested_output):
        raise WorkspaceArchiveError(
            "archive destination already exists or is a symlink; refusing to overwrite/follow it",
            path=str(requested_output),
        )
    if not requested_output.parent.is_dir():
        raise WorkspaceArchiveError(
            "archive destination parent must already exist", path=str(requested_output)
        )
    output = requested_output.parent.resolve() / requested_output.name
    if output.is_relative_to(root):
        raise WorkspaceArchiveError(
            "archive destination must be outside the workspace root", path=str(output)
        )
    database_source, marker, _database_relative, revision = _workspace_files(workspace)
    database_member = database_source.relative_to(root).as_posix()
    index_path = Path(workspace.index_database_path)
    if index_path.is_symlink():
        raise WorkspaceArchiveError(
            "configured search index path must not be a symlink", path=str(index_path)
        )
    if not index_path.is_relative_to(root):
        raise WorkspaceArchiveError(
            "full archive requires the rebuildable index path to be workspace-owned"
        )
    marker_config = _marker_config(marker)
    corpus_roots = tuple(
        _portable_member_path(str(item)) for item in marker_config.get("corpus_dirs", ["corpus"])
    )
    if (
        not workspace.data_dir.resolve().is_relative_to(root)
        or workspace.data_dir.resolve() != (root / _DATA_DIR).resolve()
    ):
        raise WorkspaceArchiveError(
            "full archive supports only the default workspace-owned storage directory (.drillintel)"
        )
    try:
        output.relative_to(root)
    except ValueError:
        pass
    else:
        raise WorkspaceArchiveError("archive destination must be outside the workspace root")

    with tempfile.TemporaryDirectory(
        prefix=".drillintel-workspace-archive-", dir=output.parent
    ) as temporary:
        staging = Path(temporary)
        database_snapshot = staging / "database.snapshot"
        database_snapshot.touch(mode=0o600)
        _read_database_backup(database_source, database_snapshot)
        health = _health(database_snapshot)
        if health.status != "healthy":
            raise WorkspaceArchiveError(
                "workspace database failed integrity/foreign-key checks; full archive was not published",
                storage_health=health.to_dict(),
            )
        snapshot_revision = _schema_revision(database_snapshot)
        if snapshot_revision != revision:
            raise WorkspaceArchiveError(
                "database revision changed while snapshotting the workspace"
            )
        _validate_restore_identity(database_snapshot, root)
        entries, excluded, total_bytes = _collect_workspace_entries(
            root, database_source, database_snapshot, index_path, corpus_roots
        )
        archived_paths = {str(entry["path"]) for entry in entries if entry["kind"] == "file"}
        source_issues = _source_reference_issues(database_snapshot, root, archived_paths)
        manifest: dict[str, Any] = {
            "format": _FORMAT,
            "format_version": _FORMAT_VERSION,
            "created_at": datetime.now(UTC).isoformat(),
            "application_version": __version__,
            "workspace_name": workspace.config.name,
            "workspace_root": str(root),
            "workspace_root_mode": stat.S_IMODE(root.stat().st_mode) & 0o777,
            "backup_scope": "full_workspace_owned_files",
            "schema_revision": revision,
            "database_path": database_member,
            "database_sha256": next(
                str(entry["sha256"]) for entry in entries if entry["path"] == database_member
            ),
            "database_bytes": next(
                int(entry["size_bytes"]) for entry in entries if entry["path"] == database_member
            ),
            "storage_health": health.to_dict(),
            "entries": entries,
            "included_file_count": sum(entry["kind"] == "file" for entry in entries),
            "included_directory_count": sum(entry["kind"] == "directory" for entry in entries),
            "included_bytes": total_bytes,
            "excluded_items": excluded,
            "unarchived_source_references": list(source_issues),
            "external_sources_included": False,
            "search_index_included": False,
            "scope_notes": [
                "all eligible regular files below the workspace root are included",
                "SQLite is captured with the online backup API",
                "external source references are listed but are not followed or copied",
                "the rebuildable search index, runtime cache/logs, transient files and credential files are excluded",
            ],
        }
        manifest_bytes = (
            json.dumps(manifest, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n"
        ).encode("utf-8")
        if len(manifest_bytes) > _MAX_MANIFEST_BYTES:
            raise WorkspaceArchiveError("archive manifest exceeds the 16 MiB metadata limit")
        temporary_archive = staging / "workspace.diwa"
        try:
            _write_archive(temporary_archive, entries, manifest_bytes, root, database_snapshot)
            archive_bytes = temporary_archive.stat().st_size
            if archive_bytes > _MAX_ARCHIVE_BYTES:
                raise WorkspaceArchiveError("created archive exceeds the total archive-size limit")
            with temporary_archive.open("rb+") as handle:
                handle.flush()
                os.fsync(handle.fileno())
            temporary_archive.chmod(0o600)
            os.link(temporary_archive, output)
        except FileExistsError as exc:
            raise WorkspaceArchiveError(
                "archive destination appeared during publication; refusing to overwrite",
                path=str(output),
            ) from exc
        except OSError as exc:
            raise WorkspaceArchiveError(
                f"workspace archive could not be published: {exc}", path=str(output)
            ) from exc
        return WorkspaceArchiveResult(
            action="archive",
            path=str(output),
            workspace_name=workspace.config.name,
            workspace_root=str(root),
            archive_sha256=_sha256(output),
            schema_revision=revision,
            included_files=int(manifest["included_file_count"]),
            included_directories=int(manifest["included_directory_count"]),
            included_bytes=total_bytes,
            database_sha256=str(manifest["database_sha256"]),
            excluded_items=tuple(dict(item) for item in excluded),
            unarchived_source_references=source_issues,
        )


def restore_workspace_archive(
    archive_path: Path | str, destination: Path | str
) -> WorkspaceArchiveResult:
    """Verify and atomically restore all archived workspace-owned files to the recorded root."""
    source = Path(archive_path).expanduser()
    if source.is_symlink() or not source.is_file():
        raise WorkspaceArchiveError(
            "archive must be an existing regular file, not a symlink", path=str(source)
        )
    source = source.resolve()
    requested_target = Path(destination).expanduser().absolute()
    if os.path.lexists(requested_target):
        raise WorkspaceArchiveError(
            "restore destination already exists or is a symlink; refusing to modify/follow it",
            path=str(requested_target),
        )
    if not requested_target.parent.is_dir():
        raise WorkspaceArchiveError(
            "restore destination parent directory must already exist", path=str(requested_target)
        )
    target = requested_target.parent.resolve() / requested_target.name
    archive_size = source.stat().st_size
    if archive_size <= 0 or archive_size > _MAX_ARCHIVE_BYTES:
        raise WorkspaceArchiveError(
            "archive exceeds the configured byte limit", size_bytes=archive_size
        )
    _preflight_zip(source, archive_size)
    staging: Path | None = None
    directory_modes: list[tuple[Path, int]] = []
    try:
        with zipfile.ZipFile(source, "r", allowZip64=True) as archive:
            manifest, entries, _database_info = _validate_manifest_and_members(archive)
            expected_root = manifest.get("workspace_root")
            if not isinstance(expected_root, str) or not Path(expected_root).is_absolute():
                raise WorkspaceArchiveError("archive manifest lacks an absolute workspace identity")
            if os.path.normcase(str(Path(expected_root).resolve(strict=False))) != os.path.normcase(
                str(target)
            ):
                raise WorkspaceArchiveError(
                    "restore destination must match the recorded workspace root; re-homing is refused",
                    recorded_root=expected_root,
                    destination=str(target),
                )
            marker_info = next(
                info for info in archive.infolist() if info.filename == "workspace.toml"
            )
            marker = archive.read(marker_info)
            config = _marker_config(marker)
            if str(manifest.get("workspace_name")) != str(config.get("name", "")):
                raise WorkspaceArchiveError(
                    "workspace marker name differs from the archive manifest"
                )
            database_relative, _index_relative = _database_path_from_marker(marker)
            expected_database = PurePosixPath(_DATA_DIR, database_relative).as_posix()
            if manifest.get("database_path") != expected_database:
                raise WorkspaceArchiveError(
                    "database path in manifest does not match workspace.toml"
                )
            entries_by_path = {str(entry["path"]): entry for entry in entries}
            database_entry = entries_by_path.get(expected_database)
            marker_entry = entries_by_path.get("workspace.toml")
            if database_entry is None or marker_entry is None:
                raise WorkspaceArchiveError(
                    "archive does not contain its marker and SQLite snapshot"
                )
            if manifest.get("database_sha256") != database_entry.get("sha256") or manifest.get(
                "database_bytes"
            ) != database_entry.get("size_bytes"):
                raise WorkspaceArchiveError("database summary differs from the per-file manifest")
            staging = Path(
                tempfile.mkdtemp(prefix=".drillintel-workspace-restore-", dir=target.parent)
            )
            file_modes: list[tuple[Path, int]] = []
            copied_total = 0
            infos = {info.filename: info for info in archive.infolist()}
            for entry in sorted(
                entries,
                key=lambda item: (
                    item["kind"] != "directory",
                    len(PurePosixPath(item["path"]).parts),
                    item["path"],
                ),
            ):
                relative = PurePosixPath(str(entry["path"]))
                output_path = staging.joinpath(*relative.parts)
                if entry["kind"] == "directory":
                    output_path.mkdir(parents=True, exist_ok=True, mode=0o700)
                    directory_modes.append((output_path, int(entry["mode"])))
                    continue
                output_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
                member_name = relative.as_posix()
                info = infos[member_name]
                expected_size = int(entry["size_bytes"])
                digest = hashlib.sha256()
                written = 0
                with archive.open(info, "r") as member, output_path.open("xb") as output:
                    while chunk := member.read(_CHUNK_BYTES):
                        written += len(chunk)
                        copied_total += len(chunk)
                        if written > expected_size or copied_total > _MAX_TOTAL_BYTES:
                            raise WorkspaceArchiveError(
                                "archive member exceeded the declared resource bound",
                                path=member_name,
                            )
                        digest.update(chunk)
                        output.write(chunk)
                    output.flush()
                    os.fsync(output.fileno())
                if written != expected_size or digest.hexdigest() != entry["sha256"]:
                    raise WorkspaceArchiveError(
                        "restored file hash or size differs from manifest", path=member_name
                    )
                file_modes.append((output_path, int(entry["mode"])))
            snapshot = staging / expected_database
            health = _health(snapshot)
            if health.status != "healthy":
                raise WorkspaceArchiveError(
                    "restored SQLite snapshot failed integrity/foreign-key checks",
                    storage_health=health.to_dict(),
                )
            revision = _schema_revision(snapshot)
            if revision != manifest.get("schema_revision"):
                raise WorkspaceArchiveError(
                    "restored database revision differs from archive manifest",
                    expected=manifest.get("schema_revision"),
                    actual=revision,
                )
            _validate_restore_identity(snapshot, target)
            for file_path, mode in file_modes:
                file_path.chmod(mode)
            for directory_path, mode in sorted(
                directory_modes, key=lambda pair: len(pair[0].parts), reverse=True
            ):
                directory_path.chmod(mode)
            root_mode = manifest.get("workspace_root_mode")
            if (
                isinstance(root_mode, bool)
                or not isinstance(root_mode, int)
                or not 0 <= root_mode <= 0o777
            ):
                raise WorkspaceArchiveError("archive root permission bits are invalid")
            staging.chmod(root_mode)
            _publish_directory_no_replace(staging, target)
            staging = None
    except WorkspaceArchiveError:
        raise
    except BackupError as exc:
        raise WorkspaceArchiveError(
            f"workspace archive database validation or publication failed: {exc}"
        ) from exc
    except (
        zipfile.BadZipFile,
        zipfile.LargeZipFile,
        sqlite3.Error,
        OSError,
        UnicodeError,
        tomllib.TOMLDecodeError,
    ) as exc:
        raise WorkspaceArchiveError(
            f"workspace archive could not be verified/restored atomically: {exc}"
        ) from exc
    finally:
        if staging is not None and staging.exists():
            # Restore writable traversal permissions before cleanup if an archive deliberately
            # recorded restrictive directory modes and atomic publication then failed.
            staging.chmod(0o700)
            for directory_path, _mode in directory_modes:
                directory_path.chmod(0o700)
            shutil.rmtree(staging)

    external = tuple(dict(item) for item in manifest.get("unarchived_source_references", []))
    return WorkspaceArchiveResult(
        action="restore",
        path=str(target),
        workspace_name=str(manifest["workspace_name"]),
        workspace_root=str(target),
        archive_sha256=_sha256(source),
        schema_revision=revision,
        included_files=int(manifest["included_file_count"]),
        included_directories=int(manifest["included_directory_count"]),
        included_bytes=int(manifest["included_bytes"]),
        database_sha256=str(manifest["database_sha256"]),
        excluded_items=tuple(dict(item) for item in manifest["excluded_items"]),
        unarchived_source_references=external,
    )
