"""Portable, non-destructive SQLite workspace snapshots.

A backup contains the exact workspace marker and a transaction-consistent SQLite snapshot.
It deliberately excludes corpus files and the disposable search index. Restore always targets
an entirely new directory; this module has no in-place replace or overwrite path.
"""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import tempfile
import tomllib
import zipfile
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import TYPE_CHECKING, Any

from .. import __version__
from ..core.errors import DrillingIntelligenceError
from .sqlite_health import SQLiteHealth, inspect_sqlite_database

if TYPE_CHECKING:
    from ..wells.workspace import Workspace

__all__ = [
    "BackupError",
    "WorkspaceBackupResult",
    "create_workspace_backup",
    "restore_workspace_backup",
]

_FORMAT = "drillintel-workspace-backup"
_FORMAT_VERSION = 1
_DATABASE_MEMBER = "database.sqlite"
_MARKER_MEMBER = "workspace.toml"
_MANIFEST_MEMBER = "manifest.json"
_DATA_DIRECTORY = ".drillintel"
_DEFAULT_DATABASE_PATH = "database/drilling_intelligence.db"
_DEFAULT_INDEX_PATH = "index/search_index.db"
_MAX_METADATA_BYTES = 1024 * 1024
_COPY_CHUNK_BYTES = 1024 * 1024


class BackupError(DrillingIntelligenceError):
    """A snapshot cannot be created or restored without risking user data."""

    code = "WORKSPACE_BACKUP"
    hint = (
        "Backups require the local SQLite workspace layout. Restore is only allowed into a "
        "new directory; the existing workspace and database are never replaced."
    )


@dataclass(frozen=True)
class WorkspaceBackupResult:
    """Auditable result for one backup or restore operation."""

    action: str
    path: str
    workspace_name: str
    database_sha256: str
    archive_sha256: str
    database_bytes: int
    schema_revision: str
    storage_health: dict[str, Any]
    corpus_included: bool = False
    search_index_included: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "action": self.action,
            "path": self.path,
            "workspace_name": self.workspace_name,
            "database_sha256": self.database_sha256,
            "archive_sha256": self.archive_sha256,
            "database_bytes": self.database_bytes,
            "schema_revision": self.schema_revision,
            "storage_health": dict(self.storage_health),
            "corpus_included": self.corpus_included,
            "search_index_included": self.search_index_included,
        }


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(_COPY_CHUNK_BYTES):
            digest.update(chunk)
    return digest.hexdigest()


def _digest_bytes(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _safe_relative_path(value: Any, *, label: str, default: str) -> PurePosixPath:
    """Validate a workspace-config path before storing or restoring it."""
    raw = value if isinstance(value, str) and value.strip() else default
    if "\\" in raw:
        raise BackupError(f"{label} must use portable relative path separators", path=raw)
    candidate = PurePosixPath(raw)
    windows_candidate = PureWindowsPath(raw)
    if (
        candidate.is_absolute()
        or windows_candidate.drive
        or windows_candidate.root
        or not candidate.parts
        or any(part in {"", ".", ".."} for part in candidate.parts)
    ):
        raise BackupError(f"{label} must be a safe relative path", path=raw)
    return candidate


def _marker_config(marker: bytes) -> dict[str, Any]:
    try:
        config = tomllib.loads(marker.decode("utf-8"))
    except (UnicodeDecodeError, tomllib.TOMLDecodeError) as exc:
        raise BackupError(f"workspace marker is not valid UTF-8 TOML: {exc}") from exc
    corpus_dirs = config.get("corpus_dirs", ["corpus"])
    if not isinstance(corpus_dirs, list) or any(not isinstance(item, str) for item in corpus_dirs):
        raise BackupError("workspace corpus_dirs must be a list of relative paths")
    for index, corpus_dir in enumerate(corpus_dirs):
        _safe_relative_path(corpus_dir, label=f"corpus_dirs[{index}]", default="corpus")
    return config


def _database_path_from_marker(marker: bytes) -> tuple[str, str]:
    config = _marker_config(marker)
    database = _safe_relative_path(
        config.get("database_path"), label="database_path", default=_DEFAULT_DATABASE_PATH
    )
    index = _safe_relative_path(
        config.get("index_database_path"),
        label="index_database_path",
        default=_DEFAULT_INDEX_PATH,
    )
    # Workspace._resolve treats a leading ``data/`` as relative to the data directory.
    if database.parts[:1] == ("data",):
        database = PurePosixPath(*database.parts[1:])
    if not database.parts:
        raise BackupError("database_path resolves to the workspace data directory")
    return database.as_posix(), index.as_posix()


def _schema_revision(path: Path) -> str:
    """Read the single Alembic revision without opening or migrating the workspace."""
    uri = f"{path.resolve().as_uri()}?mode=ro"
    connection: sqlite3.Connection | None = None
    try:
        connection = sqlite3.connect(uri, uri=True, timeout=5.0)
        row = connection.execute("SELECT version_num FROM alembic_version").fetchall()
    except sqlite3.Error as exc:
        raise BackupError(f"database has no readable Alembic revision: {exc}") from exc
    finally:
        if connection is not None:
            connection.close()
    if len(row) != 1 or not isinstance(row[0][0], str) or not row[0][0]:
        raise BackupError("database must have exactly one non-empty Alembic revision")
    return row[0][0]


def _validate_restore_identity(database: Path, destination: Path) -> None:
    """Keep the database's workspace identity bound to its original resolved path."""
    uri = f"{database.resolve().as_uri()}?mode=ro"
    connection: sqlite3.Connection | None = None
    try:
        connection = sqlite3.connect(uri, uri=True, timeout=5.0)
        rows = connection.execute("SELECT root_path FROM workspace ORDER BY root_path").fetchall()
    except sqlite3.Error as exc:
        raise BackupError(f"restored database has no readable workspace identity: {exc}") from exc
    finally:
        if connection is not None:
            connection.close()
    roots = [str(row[0]) for row in rows]
    if roots and (len(set(roots)) != 1 or roots[0] != str(destination.resolve())):
        raise BackupError(
            "restore destination must match the workspace path recorded in the database; "
            "re-homing would invalidate workspace identity and provenance",
            recorded_roots=roots,
            destination=str(destination.resolve()),
        )


def _workspace_files(workspace: Workspace) -> tuple[Path, bytes, str, str]:
    root = Path(workspace.root).resolve()
    marker_path = root / "workspace.toml"
    data_root = root / _DATA_DIRECTORY
    if data_root.is_symlink() or Path(workspace.data_dir).resolve() != data_root.resolve():
        raise BackupError(
            "portable backup requires the workspace data directory to be <workspace>/.drillintel",
            data_directory=str(workspace.data_dir),
        )
    if marker_path.is_symlink() or not marker_path.is_file():
        raise BackupError("workspace.toml must be a regular file", path=str(marker_path))
    try:
        marker = marker_path.read_bytes()
    except OSError as exc:
        raise BackupError(f"cannot read workspace marker: {exc}", path=str(marker_path)) from exc
    database_relative, _index_relative = _database_path_from_marker(marker)
    database_path = Path(workspace.database_path)
    expected_path = data_root.joinpath(*PurePosixPath(database_relative).parts)
    if database_path.resolve() != expected_path.resolve():
        raise BackupError(
            "workspace database path does not match the portable workspace marker",
            configured=str(database_path),
            expected=str(expected_path),
        )
    try:
        database_path.relative_to(data_root)
    except ValueError as exc:
        raise BackupError("database path escapes the workspace data directory") from exc
    current = data_root
    for part in PurePosixPath(database_relative).parts:
        current = current / part
        if current.is_symlink():
            raise BackupError(
                "database path must not pass through a symbolic link", path=str(current)
            )
    if not database_path.is_file():
        raise BackupError(
            "workspace database does not exist; open the workspace and complete schema setup first",
            path=str(database_path),
        )
    return database_path, marker, database_relative, _schema_revision(database_path)


def _health(path: Path) -> SQLiteHealth:
    return inspect_sqlite_database(path)


def _read_database_backup(source: Path, destination: Path) -> None:
    """Copy a consistent view of a live SQLite database using SQLite's backup API."""
    source_connection: sqlite3.Connection | None = None
    destination_connection: sqlite3.Connection | None = None
    try:
        source_connection = sqlite3.connect(
            f"{source.resolve().as_uri()}?mode=ro", uri=True, timeout=30.0
        )
        destination_connection = sqlite3.connect(destination, timeout=30.0)
        source_connection.backup(destination_connection, pages=256)
        destination_connection.commit()
    except sqlite3.Error as exc:
        raise BackupError(f"SQLite could not create a consistent backup: {exc}") from exc
    finally:
        if destination_connection is not None:
            destination_connection.close()
        if source_connection is not None:
            source_connection.close()


def _write_zip(archive_path: Path, database: Path, marker: bytes, manifest: dict[str, Any]) -> None:
    with zipfile.ZipFile(
        archive_path, mode="w", compression=zipfile.ZIP_DEFLATED, compresslevel=6
    ) as archive:
        archive.write(database, arcname=_DATABASE_MEMBER)
        archive.writestr(_MARKER_MEMBER, marker)
        archive.writestr(
            _MANIFEST_MEMBER,
            json.dumps(manifest, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
        )


def create_workspace_backup(workspace: Workspace, destination: Path | str) -> WorkspaceBackupResult:
    """Create a new, hashed backup archive without migrating or modifying the source DB.

    The snapshot is preserved even when its SQLite integrity check finds problems. Such a
    backup is reported as unhealthy and restore refuses it; preserving a damaged source is
    preferable to silently discarding its evidence.
    """
    output = Path(destination).expanduser().absolute()
    if not output.parent.is_dir():
        raise BackupError(
            "backup destination parent directory must already exist", path=str(output)
        )
    if os.path.lexists(output):
        raise BackupError(
            "backup destination already exists; refusing to overwrite it", path=str(output)
        )
    source_database, marker, database_relative, revision = _workspace_files(workspace)
    if output.resolve(strict=False) == source_database.resolve():
        raise BackupError("backup destination must not be the workspace database", path=str(output))

    with tempfile.TemporaryDirectory(prefix=".drillintel-backup-", dir=output.parent) as temporary:
        staging = Path(temporary)
        database_snapshot = staging / _DATABASE_MEMBER
        database_snapshot.touch(mode=0o600)
        _read_database_backup(source_database, database_snapshot)
        database_snapshot.chmod(0o600)
        health = _health(database_snapshot)
        snapshot_revision = _schema_revision(database_snapshot)
        if snapshot_revision != revision:
            raise BackupError(
                "source schema revision changed while the SQLite snapshot was being created",
                source_revision=revision,
                snapshot_revision=snapshot_revision,
            )
        manifest: dict[str, Any] = {
            "format": _FORMAT,
            "format_version": _FORMAT_VERSION,
            "created_at": datetime.now(UTC).isoformat(),
            "application_version": __version__,
            "workspace_name": workspace.config.name,
            "database_member": _DATABASE_MEMBER,
            "database_path": database_relative,
            "database_bytes": database_snapshot.stat().st_size,
            "database_sha256": _sha256(database_snapshot),
            "workspace_sha256": _digest_bytes(marker),
            "schema_revision": revision,
            "storage_health": health.to_dict(),
            "included": [_MARKER_MEMBER, _DATABASE_MEMBER],
            "omitted": ["corpus files", "rebuildable search index"],
        }
        temporary_archive = staging / "snapshot.zip"
        _write_zip(temporary_archive, database_snapshot, marker, manifest)
        with temporary_archive.open("rb+") as handle:
            handle.flush()
            os.fsync(handle.fileno())
        temporary_archive.chmod(0o600)
        try:
            # A hard link publishes the completed archive atomically and cannot replace
            # a destination created by another process between the preflight and publish.
            os.link(temporary_archive, output)
        except FileExistsError as exc:
            raise BackupError(
                "backup destination appeared during creation; refusing to overwrite",
                path=str(output),
            ) from exc
        except OSError as exc:
            raise BackupError(
                f"cannot safely publish backup without overwrite: {exc}", path=str(output)
            ) from exc
        output.chmod(0o600)
        archive_digest = _sha256(output)
        return WorkspaceBackupResult(
            action="backup",
            path=str(output),
            workspace_name=workspace.config.name,
            database_sha256=manifest["database_sha256"],
            archive_sha256=archive_digest,
            database_bytes=manifest["database_bytes"],
            schema_revision=revision,
            storage_health=health.to_dict(),
        )


def _manifest_from_archive(
    archive: zipfile.ZipFile,
) -> tuple[dict[str, Any], bytes, zipfile.ZipInfo]:
    infos = archive.infolist()
    names = [info.filename for info in infos]
    expected = {_MANIFEST_MEMBER, _MARKER_MEMBER, _DATABASE_MEMBER}
    if len(names) != len(set(names)) or set(names) != expected:
        raise BackupError(
            "backup archive must contain exactly one manifest, workspace marker, and database"
        )
    if any(info.is_dir() or info.flag_bits & 0x1 for info in infos):
        raise BackupError("backup archive contains a directory or encrypted member")
    by_name = {info.filename: info for info in infos}
    if by_name[_MANIFEST_MEMBER].file_size > _MAX_METADATA_BYTES:
        raise BackupError("backup manifest exceeds the allowed metadata size")
    if by_name[_MARKER_MEMBER].file_size > _MAX_METADATA_BYTES:
        raise BackupError("workspace marker exceeds the allowed metadata size")
    try:
        manifest = json.loads(archive.read(_MANIFEST_MEMBER))
        marker = archive.read(_MARKER_MEMBER)
    except (json.JSONDecodeError, UnicodeDecodeError, zipfile.BadZipFile, OSError) as exc:
        raise BackupError(f"backup metadata could not be read: {exc}") from exc
    if not isinstance(manifest, dict):
        raise BackupError("backup manifest must be a JSON object")
    if manifest.get("format") != _FORMAT or manifest.get("format_version") != _FORMAT_VERSION:
        raise BackupError("unsupported workspace backup format")
    if manifest.get("database_member") != _DATABASE_MEMBER:
        raise BackupError("backup manifest names an unsupported database member")
    if manifest.get("workspace_sha256") != _digest_bytes(marker):
        raise BackupError("workspace marker hash does not match the backup manifest")
    marker_config = _marker_config(marker)
    database_relative, _index_relative = _database_path_from_marker(marker)
    if manifest.get("workspace_name") != marker_config.get("name", ""):
        raise BackupError("workspace name in the marker differs from the backup manifest")
    if manifest.get("database_path") != database_relative:
        raise BackupError("database path in the workspace marker differs from the backup manifest")
    database_info = by_name[_DATABASE_MEMBER]
    database_bytes = manifest.get("database_bytes")
    if (
        not isinstance(database_bytes, int)
        or database_bytes < 1
        or database_info.file_size != database_bytes
    ):
        raise BackupError("database size does not match the backup manifest")
    digest = manifest.get("database_sha256")
    if not isinstance(digest, str) or len(digest) != 64:
        raise BackupError("backup manifest has no valid database SHA-256")
    health = manifest.get("storage_health")
    if not isinstance(health, dict) or health.get("status") != "healthy":
        raise BackupError("backup snapshot failed its recorded SQLite storage health checks")
    if health.get("foreign_key_violation_count") != 0:
        raise BackupError("backup snapshot contains foreign-key violations")
    revision = manifest.get("schema_revision")
    if not isinstance(revision, str) or not revision:
        raise BackupError("backup manifest has no schema revision")
    return manifest, marker, database_info


def restore_workspace_backup(
    archive_path: Path | str, destination: Path | str
) -> WorkspaceBackupResult:
    """Restore a verified backup into a new directory; never replace an existing workspace."""
    archive_source = Path(archive_path).expanduser().resolve()
    target = Path(destination).expanduser().absolute()
    if not archive_source.is_file():
        raise BackupError(
            "backup archive does not exist or is not a regular file", path=str(archive_source)
        )
    if not target.parent.is_dir():
        raise BackupError(
            "restore destination parent directory must already exist", path=str(target)
        )
    if os.path.lexists(target):
        raise BackupError(
            "restore destination already exists; refusing to modify it", path=str(target)
        )

    try:
        with zipfile.ZipFile(archive_source, mode="r") as archive:
            manifest, marker, database_info = _manifest_from_archive(archive)
            database_relative, _index_relative = _database_path_from_marker(marker)
            with tempfile.TemporaryDirectory(
                prefix=".drillintel-restore-", dir=target.parent
            ) as temporary:
                staging = Path(temporary)
                data_stage = staging / _DATA_DIRECTORY
                database_stage = data_stage.joinpath(*PurePosixPath(database_relative).parts)
                database_stage.parent.mkdir(parents=True, exist_ok=True)
                digest = hashlib.sha256()
                copied_bytes = 0
                with (
                    archive.open(database_info, mode="r") as source,
                    database_stage.open("xb") as output,
                ):
                    database_stage.chmod(0o600)
                    while chunk := source.read(_COPY_CHUNK_BYTES):
                        output.write(chunk)
                        digest.update(chunk)
                        copied_bytes += len(chunk)
                    output.flush()
                    os.fsync(output.fileno())
                if (
                    copied_bytes != manifest["database_bytes"]
                    or digest.hexdigest() != manifest["database_sha256"]
                ):
                    raise BackupError("database bytes do not match the backup manifest")
                health = _health(database_stage)
                if health.status != "healthy":
                    raise BackupError(
                        "restored database failed SQLite integrity/foreign-key checks",
                        storage_health=health.to_dict(),
                    )
                revision = _schema_revision(database_stage)
                _validate_restore_identity(database_stage, target)
                if revision != manifest["schema_revision"]:
                    raise BackupError(
                        "restored database revision differs from the backup manifest",
                        expected=manifest["schema_revision"],
                        actual=revision,
                    )
                marker_stage = staging / _MARKER_MEMBER
                marker_stage.write_bytes(marker)
                marker_stage.chmod(0o600)
                data_stage.chmod(0o700)
                try:
                    # Reserve the user-requested directory exclusively. An existing empty
                    # directory is still user data and is never replaced.
                    target.mkdir(mode=0o700)
                except FileExistsError as exc:
                    raise BackupError(
                        "restore destination appeared during validation; refusing to overwrite",
                        path=str(target),
                    ) from exc
                try:
                    data_stage.replace(target / _DATA_DIRECTORY)
                    # The marker is moved last: until then, an interrupted restore is not a
                    # discoverable Drilling Intelligence workspace.
                    marker_stage.replace(target / _MARKER_MEMBER)
                except OSError as exc:
                    raise BackupError(
                        f"restore was interrupted; the incomplete directory is preserved at {target}: {exc}",
                        path=str(target),
                    ) from exc
    except BackupError:
        raise
    except (zipfile.BadZipFile, zipfile.LargeZipFile, sqlite3.Error, OSError) as exc:
        raise BackupError(f"workspace backup could not be verified or restored: {exc}") from exc

    return WorkspaceBackupResult(
        action="restore",
        path=str(target),
        workspace_name=str(manifest["workspace_name"]),
        database_sha256=str(manifest["database_sha256"]),
        archive_sha256=_sha256(archive_source),
        database_bytes=int(manifest["database_bytes"]),
        schema_revision=revision,
        storage_health=health.to_dict(),
    )
