"""Read-only physical and foreign-key health checks for the SQLite system of record.

The cross-row domain checks in :mod:`database.integrity` cannot detect a damaged SQLite
page or a row whose declared foreign key is broken.  This module checks those storage-level
invariants before ``doctor`` asks SQLAlchemy or Alembic to open (and potentially migrate) the
file.  It never repairs, creates, migrates, or writes to the database.
"""

from __future__ import annotations

import os
import sqlite3
import stat
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

__all__ = ["SQLiteHealth", "inspect_sqlite_database"]


@dataclass(frozen=True)
class SQLiteHealth:
    """A bounded report from SQLite's integrity and foreign-key checkers."""

    status: Literal["healthy", "failed", "not_run"]
    integrity_messages: tuple[str, ...] = ()
    integrity_issue_count: int = 0
    integrity_messages_truncated: bool = False
    database_bytes: int | None = None
    sidecars: tuple[dict[str, Any], ...] = ()
    foreign_key_violations: tuple[dict[str, Any], ...] = ()
    foreign_key_violation_count: int = 0
    foreign_key_violations_truncated: bool = False
    detail: str = ""

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-safe representation with capped row-level details."""
        return {
            "status": self.status,
            "integrity_messages": list(self.integrity_messages),
            "integrity_issue_count": self.integrity_issue_count,
            "integrity_messages_truncated": self.integrity_messages_truncated,
            "database_bytes": self.database_bytes,
            "sidecars": [dict(item) for item in self.sidecars],
            "foreign_key_violation_count": self.foreign_key_violation_count,
            "foreign_key_violations": [dict(row) for row in self.foreign_key_violations],
            "foreign_key_violations_truncated": self.foreign_key_violations_truncated,
            "detail": self.detail,
        }


def _readonly_uri(path: Path) -> str:
    """Build a platform-safe SQLite URI without opening the file for writing."""
    return f"{path.resolve().as_uri()}?mode=ro"


def _sidecar_state(path: Path, suffix: str) -> dict[str, Any]:
    """Describe SQLite's WAL/SHM files without following links or opening them."""
    result: dict[str, Any] = {"name": suffix, "present": False, "state": "absent", "bytes": 0}
    if not os.path.lexists(path):
        return result
    result["present"] = True
    try:
        info = path.lstat()
    except OSError as exc:
        result.update(state="unreadable", detail=str(exc))
        return result
    if stat.S_ISLNK(info.st_mode):
        result.update(state="symlink", bytes=info.st_size)
    elif stat.S_ISREG(info.st_mode):
        result.update(state="regular", bytes=info.st_size)
    else:
        result.update(state="not_regular", bytes=info.st_size)
    return result


def inspect_sqlite_database(path: Path | str, *, detail_limit: int = 100) -> SQLiteHealth:
    """Run ``integrity_check`` and ``foreign_key_check`` without modifying ``path``.

    A missing file is ``not_run`` so a caller such as ``doctor`` can retain its existing
    first-open/bootstrap behavior and check the newly created database afterwards.  A
    malformed file or a failed pragma is reported as ``failed``; callers must not continue
    with migrations or domain queries against that file.
    """
    database_path = Path(path).expanduser()
    if detail_limit < 0:
        raise ValueError("detail_limit must not be negative")
    sidecars = tuple(
        _sidecar_state(Path(f"{database_path}{suffix}"), suffix) for suffix in ("-wal", "-shm")
    )
    try:
        database_info = database_path.lstat()
    except FileNotFoundError:
        return SQLiteHealth(
            status="not_run", sidecars=sidecars, detail="database file does not exist yet"
        )
    except OSError as exc:
        return SQLiteHealth(
            status="failed", sidecars=sidecars, detail=f"database path cannot be inspected: {exc}"
        )
    if stat.S_ISLNK(database_info.st_mode):
        return SQLiteHealth(
            status="failed",
            database_bytes=database_info.st_size,
            sidecars=sidecars,
            detail="database path is a symbolic link; storage preflight refuses linked databases",
        )
    if not stat.S_ISREG(database_info.st_mode):
        return SQLiteHealth(
            status="failed",
            database_bytes=database_info.st_size,
            sidecars=sidecars,
            detail="database path is not a regular file",
        )
    unsafe_sidecars = [item for item in sidecars if item["state"] not in {"absent", "regular"}]
    if unsafe_sidecars:
        labels = ", ".join(f"{item['name']}={item['state']}" for item in unsafe_sidecars)
        return SQLiteHealth(
            status="failed",
            database_bytes=database_info.st_size,
            sidecars=sidecars,
            detail=f"SQLite sidecar path is not a regular file: {labels}",
        )

    connection: sqlite3.Connection | None = None
    try:
        connection = sqlite3.connect(_readonly_uri(database_path), uri=True, timeout=5.0)
        integrity_rows: list[str] = []
        integrity_issue_count = 0
        integrity_cursor = connection.execute("PRAGMA integrity_check")
        try:
            for row in integrity_cursor:
                message = str(row[0])
                if message != "ok":
                    integrity_issue_count += 1
                if len(integrity_rows) < detail_limit or message == "ok":
                    integrity_rows.append(message)
        finally:
            integrity_cursor.close()
        integrity_messages = tuple(integrity_rows)
        foreign_key_rows: list[dict[str, Any]] = []
        foreign_key_count = 0
        cursor = connection.execute("PRAGMA foreign_key_check")
        try:
            for table, rowid, parent, foreign_key_index in cursor:
                foreign_key_count += 1
                if len(foreign_key_rows) < detail_limit:
                    foreign_key_rows.append(
                        {
                            "table": str(table),
                            "rowid": rowid,
                            "parent": str(parent),
                            "foreign_key_index": int(foreign_key_index),
                        }
                    )
        finally:
            cursor.close()
    except sqlite3.Error as exc:
        return SQLiteHealth(
            status="failed",
            database_bytes=database_info.st_size,
            sidecars=sidecars,
            detail=f"SQLite could not check the database (permissions, lock, or integrity preflight): {exc}",
        )
    finally:
        if connection is not None:
            connection.close()

    physical_ok = integrity_issue_count == 0 and integrity_messages == ("ok",)
    foreign_keys_ok = foreign_key_count == 0
    details: list[str] = []
    if not physical_ok:
        if not integrity_messages:
            details.append("SQLite integrity_check returned no result")
        else:
            details.append(f"SQLite integrity_check reported {integrity_issue_count} issue(s)")
            details.extend(message for message in integrity_messages if message != "ok")
    if not foreign_keys_ok:
        details.append(f"SQLite reports {foreign_key_count} foreign-key violation(s)")
    return SQLiteHealth(
        status="healthy" if physical_ok and foreign_keys_ok else "failed",
        integrity_messages=integrity_messages,
        integrity_issue_count=integrity_issue_count,
        database_bytes=database_info.st_size,
        sidecars=sidecars,
        integrity_messages_truncated=integrity_issue_count
        > len([message for message in integrity_messages if message != "ok"]),
        foreign_key_violations=tuple(foreign_key_rows),
        foreign_key_violation_count=foreign_key_count,
        foreign_key_violations_truncated=foreign_key_count > len(foreign_key_rows),
        detail="; ".join(details),
    )
