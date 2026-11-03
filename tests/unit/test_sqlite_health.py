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
