"""The SQLite storage check is read-only and detects declared-constraint damage."""

from __future__ import annotations

import sqlite3
from pathlib import Path

from drilling_intelligence.database.sqlite_health import inspect_sqlite_database


def test_sqlite_health_checks_pages_and_foreign_keys_without_writing(tmp_path: Path) -> None:
    database = tmp_path / "registry.sqlite"
    connection = sqlite3.connect(database)
    try:
        connection.executescript(
            """
            CREATE TABLE parent (id INTEGER PRIMARY KEY);
            CREATE TABLE child (
                id INTEGER PRIMARY KEY,
                parent_id INTEGER NOT NULL REFERENCES parent(id)
            );
            INSERT INTO parent (id) VALUES (1);
            INSERT INTO child (id, parent_id) VALUES (1, 1);
            """
        )
    finally:
        connection.close()
    before = database.read_bytes()

    report = inspect_sqlite_database(database)

    assert report.status == "healthy"
    assert report.integrity_messages == ("ok",)
    assert report.foreign_key_violation_count == 0
    assert database.read_bytes() == before


def test_sqlite_health_reports_file_and_wal_sidecar_state(tmp_path: Path) -> None:
    database = tmp_path / "registry.sqlite"
    connection = sqlite3.connect(database)
    connection.execute("CREATE TABLE sample (id INTEGER PRIMARY KEY)")
    connection.close()

    report = inspect_sqlite_database(database)

    assert report.status == "healthy"
    payload = report.to_dict()
    assert payload["database_bytes"] > 0
    assert payload["sidecars"] == [
        {"name": "-wal", "present": False, "state": "absent", "bytes": 0},
        {"name": "-shm", "present": False, "state": "absent", "bytes": 0},
    ]


def test_sqlite_health_refuses_symlinked_database_and_wal_sidecar(tmp_path: Path) -> None:
    database = tmp_path / "registry.sqlite"
    sqlite3.connect(database).close()
    linked_database = tmp_path / "linked.sqlite"
    try:
        linked_database.symlink_to(database)
    except OSError as exc:
        import pytest

        pytest.skip(f"this platform cannot create test symlinks: {exc}")

    database_report = inspect_sqlite_database(linked_database)
    assert database_report.status == "failed"
    assert "symbolic link" in database_report.detail
    linked_database.unlink()

    wal_target = tmp_path / "wal-target"
    wal_target.write_bytes(b"not a WAL database")
    wal_path = Path(f"{database}-wal")
    try:
        wal_path.symlink_to(wal_target)
    except OSError as exc:
        import pytest

        pytest.skip(f"this platform cannot create test symlinks: {exc}")
    sidecar_report = inspect_sqlite_database(database)
    assert sidecar_report.status == "failed"
    assert "-wal=symlink" in sidecar_report.detail
    assert sidecar_report.to_dict()["sidecars"][0]["state"] == "symlink"


def test_sqlite_health_reports_foreign_key_violation_instead_of_repairing(tmp_path: Path) -> None:
    database = tmp_path / "registry.sqlite"
    connection = sqlite3.connect(database)
    try:
        connection.execute("CREATE TABLE parent (id INTEGER PRIMARY KEY)")
        connection.execute(
            "CREATE TABLE child (id INTEGER PRIMARY KEY, parent_id INTEGER REFERENCES parent(id))"
        )
        connection.execute("PRAGMA foreign_keys=OFF")
        connection.execute("INSERT INTO child (id, parent_id) VALUES (7, 404)")
        connection.commit()
    finally:
        connection.close()
    before = database.read_bytes()

    report = inspect_sqlite_database(database)

    assert report.status == "failed"
    assert report.integrity_messages == ("ok",)
    assert report.foreign_key_violation_count == 1
    assert report.foreign_key_violations == (
        {"table": "child", "rowid": 7, "parent": "parent", "foreign_key_index": 0},
    )
    assert database.read_bytes() == before


def test_sqlite_health_caps_details_without_hiding_the_violation_count(tmp_path: Path) -> None:
    database = tmp_path / "registry.sqlite"
    connection = sqlite3.connect(database)
    try:
        connection.execute("CREATE TABLE parent (id INTEGER PRIMARY KEY)")
        connection.execute(
            "CREATE TABLE child (id INTEGER PRIMARY KEY, parent_id INTEGER REFERENCES parent(id))"
        )
        connection.execute("PRAGMA foreign_keys=OFF")
        connection.execute("INSERT INTO child (id, parent_id) VALUES (7, 404)")
        connection.commit()
    finally:
        connection.close()

    report = inspect_sqlite_database(database, detail_limit=0)

    assert report.status == "failed"
    assert report.foreign_key_violation_count == 1
    assert report.foreign_key_violations == ()
    assert report.foreign_key_violations_truncated is True
    payload = report.to_dict()
    assert payload["integrity_issue_count"] == 0
    assert payload["integrity_messages_truncated"] is False
    assert payload["foreign_key_violations_truncated"] is True


def test_sqlite_health_reports_a_bounded_lock_failure(tmp_path: Path) -> None:
    database = tmp_path / "locked.sqlite"
    sqlite3.connect(database).close()
    blocker = sqlite3.connect(database, timeout=0.0)
    try:
        blocker.execute("BEGIN EXCLUSIVE")
        report = inspect_sqlite_database(database)
    finally:
        blocker.rollback()
        blocker.close()

    assert report.status == "failed"
    assert "locked" in report.detail.lower() or "busy" in report.detail.lower()


def test_sqlite_health_refuses_malformed_database_as_not_qualified(tmp_path: Path) -> None:
    database = tmp_path / "registry.sqlite"
    database.write_bytes(b"not a sqlite database")
    before = database.read_bytes()

    report = inspect_sqlite_database(database)

    assert report.status == "failed"
    assert "could not check" in report.detail
    assert database.read_bytes() == before


def test_missing_database_is_not_run_and_is_not_created(tmp_path: Path) -> None:
    database = tmp_path / "missing.sqlite"

    report = inspect_sqlite_database(database)

    assert report.status == "not_run"
    assert not database.exists()
