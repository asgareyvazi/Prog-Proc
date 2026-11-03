"""``doctor`` checks SQLite before migrations can touch an existing damaged file."""

from __future__ import annotations

import json
from pathlib import Path

from drilling_intelligence.cli.app import main
from drilling_intelligence.wells.workspace import Workspace


def test_doctor_reports_healthy_sqlite_storage(tmp_path: Path, capsys) -> None:
    workspace = Workspace.create(tmp_path / "healthy")
    workspace.close()

    code = main(["doctor", "--workspace", str(tmp_path / "healthy"), "--json"])
    payload = json.loads(capsys.readouterr().out)

    assert code == 0
    assert payload["storage"]["status"] == "healthy"
    assert payload["storage"]["integrity_messages"] == ["ok"]
    assert payload["storage"]["foreign_key_violation_count"] == 0


def test_doctor_fails_closed_before_migration_on_malformed_database(tmp_path: Path, capsys) -> None:
    root = tmp_path / "damaged"
    workspace = Workspace.create(root)
    database = workspace.database_path
    workspace.close()
    database.parent.mkdir(parents=True, exist_ok=True)
    database.write_bytes(b"preserve this damaged database")
    original = database.read_bytes()

    code = main(["doctor", "--workspace", str(root), "--json"])
    output = capsys.readouterr().out
    payload = json.loads(output)

    assert code == 1
    assert payload["storage"]["status"] == "failed"
    assert payload["checks_not_run"] == ["schema migrations", "domain integrity", "search index"]
    assert "SQLite storage integrity/foreign-key check failed" in payload["findings"][0]
    assert database.read_bytes() == original
    assert not Path(f"{database}-wal").exists()
    assert "schema" not in payload


def test_doctor_fails_closed_on_declared_foreign_key_violation(tmp_path: Path, capsys) -> None:
    import sqlite3

    root = tmp_path / "broken-link"
    workspace = Workspace.create(root)
    database = workspace.database
    database_path = workspace.database_path
    with database.engine.begin() as connection:
        connection.exec_driver_sql("CREATE TABLE health_parent (id INTEGER PRIMARY KEY)")
        connection.exec_driver_sql(
            "CREATE TABLE health_child (id INTEGER PRIMARY KEY, parent_id INTEGER "
            "REFERENCES health_parent(id))"
        )
    workspace.close()
    raw = sqlite3.connect(database_path)
    try:
        raw.execute("PRAGMA foreign_keys=OFF")
        raw.execute("INSERT INTO health_child (id, parent_id) VALUES (1, 404)")
        raw.commit()
    finally:
        raw.close()
    original = database_path.read_bytes()

    code = main(["doctor", "--workspace", str(root), "--json"])
    payload = json.loads(capsys.readouterr().out)

    assert code == 1
    assert payload["storage"]["status"] == "failed"
    assert payload["storage"]["foreign_key_violation_count"] == 1
    assert "1 foreign-key violation" in payload["storage"]["detail"]
    assert payload["checks_not_run"] == [
        "schema migrations",
        "domain integrity",
        "search index",
    ]
    assert database_path.read_bytes() == original
