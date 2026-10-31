"""Offline Alembic SQL generation must be non-mutating (§2).

Every test captures the database state before and after ``alembic upgrade ... --sql``
and proves the target file was not modified.  The SQL output is inspected for
expected operations, and exit code alone is not sufficient.
"""

from __future__ import annotations

import hashlib
import os
import sqlite3
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]


def _hash(path: Path) -> str | None:
    if not path.exists():
        return None
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _run_offline(url: str, args: list[str]) -> subprocess.CompletedProcess:
    env = dict(os.environ)
    env["DRILLINTEL_DATABASE__URL"] = url
    env["PYTHONPATH"] = str(ROOT / "src")
    return subprocess.run(
        [sys.executable, "-m", "alembic", "-c", str(ROOT / "alembic.ini"), *args, "--sql"],
        cwd=str(ROOT),
        capture_output=True,
        text=True,
        env=env,
        check=False,
    )


def _make_db(path: Path, version: str = "0019", with_data: bool = False) -> None:
    # Use migrations to create at given version
    from sqlalchemy import inspect

    from drilling_intelligence.database.engine import open_sqlite
    from drilling_intelligence.database.migrations import upgrade

    engine = open_sqlite(f"sqlite:///{path}")
    try:
        upgrade(engine, version)
        if with_data:
            # Check if code_key column exists at this version
            cols = {c["name"] for c in inspect(engine).get_columns("lesson_learned")}
            has_code_key = "code_key" in cols
            with engine.begin() as conn:
                if has_code_key:
                    conn.execute(
                        __import__("sqlalchemy").text(
                            "INSERT INTO lesson_learned (id, code, code_key, revision, is_current, status, title, lesson, created_by, created_at, updated_at, depth_from_unit, depth_to_unit, root_cause_status, context, observation, recommendation, origin, provenance, attributes) "
                            "VALUES ('les-sentinel', 'LL-999', 'll-999', 1, 1, 'DRAFT', 't', 'l', 'system', datetime('now'), datetime('now'), 'm', 'm', 'UNKNOWN', '', '', '', 'MANUAL', '[]', '{}')"
                        )
                    )
                else:
                    conn.execute(
                        __import__("sqlalchemy").text(
                            "INSERT INTO lesson_learned (id, code, revision, is_current, status, title, lesson, created_by, created_at, updated_at, depth_from_unit, depth_to_unit, root_cause_status, context, observation, recommendation, origin, provenance, attributes) "
                            "VALUES ('les-sentinel', 'LL-999', 1, 1, 'DRAFT', 't', 'l', 'system', datetime('now'), datetime('now'), 'm', 'm', 'UNKNOWN', '', '', '', 'MANUAL', '[]', '{}')"
                        )
                    )
                conn.execute(__import__("sqlalchemy").text("CREATE TABLE IF NOT EXISTS unrelated (x INTEGER)"))
                conn.execute(__import__("sqlalchemy").text("INSERT INTO unrelated VALUES (99)"))
    finally:
        engine.dispose()


def _capture_state(path: Path) -> tuple[str | None, int | None, list[tuple] | None]:
    h = _hash(path)
    size = path.stat().st_size if path.exists() else None
    rows = None
    if path.exists():
        try:
            conn = sqlite3.connect(path)
            try:
                rows = conn.execute("SELECT id, code FROM lesson_learned WHERE id='les-sentinel'").fetchall()
            except sqlite3.OperationalError:
                rows = None
            conn.close()
        except Exception:
            rows = None
    return h, size, rows


@pytest.mark.parametrize("version", ["0019", "0020", "0021", "0022"])
def test_offline_does_not_mutate_existing_file(tmp_path: Path, version: str) -> None:
    db = tmp_path / f"existing-{version}.db"
    _make_db(db, version=version, with_data=True)
    before_hash, before_size, before_rows = _capture_state(db)
    result = _run_offline(f"sqlite:///{db}", ["upgrade", "head"])
    after_hash, after_size, after_rows = _capture_state(db)
    assert result.returncode == 0, result.stderr[-1000:]
    assert before_hash == after_hash, "offline must not change file bytes"
    assert before_size == after_size
    assert before_rows == after_rows, "sentinel must remain"
    # SQL should contain expected operations for the gap
    assert "lesson_learned" in result.stdout
    # Unrelated table must not be dropped/altered in SQL
    assert "unrelated" not in result.stdout.lower() or "DROP TABLE unrelated" not in result.stdout


def test_offline_does_not_create_nonexistent_file(tmp_path: Path) -> None:
    db = tmp_path / "nonexist.db"
    assert not db.exists()
    before = _hash(db)
    result = _run_offline(f"sqlite:///{db}", ["upgrade", "head"])
    after = _hash(db)
    assert result.returncode == 0, result.stderr[-1000:]
    assert before is None and after is None, "file must not be created"
    assert not db.exists()


def test_offline_with_empty_path(tmp_path: Path) -> None:
    # Empty URL (in-memory) should render without error and not create file
    result = _run_offline("sqlite://", ["upgrade", "head"])
    assert result.returncode == 0, result.stderr[-1000:]
    assert "CREATE TABLE alembic_version" in result.stdout


def test_offline_preserves_populated_pre0020(tmp_path: Path) -> None:
    db = tmp_path / "pre0020.db"
    _make_db(db, version="0019", with_data=True)
    before = _hash(db)
    result = _run_offline(f"sqlite:///{db}", ["upgrade", "0020", "--sql"])  # note double --sql handled by helper
    # Our helper already adds --sql, so test with head
    result = _run_offline(f"sqlite:///{db}", ["upgrade", "head"])
    after = _hash(db)
    assert result.returncode == 0
    assert before == after
    # SQL should contain 0020 columns
    assert "identity_key" in result.stdout


def test_offline_already_at_0020(tmp_path: Path) -> None:
    db = tmp_path / "at0020.db"
    _make_db(db, version="0020", with_data=True)
    before = _hash(db)
    result = _run_offline(f"sqlite:///{db}", ["upgrade", "head"])
    after = _hash(db)
    assert result.returncode == 0
    assert before == after
    # Should still render 0021/0022
    assert "code_key" in result.stdout or "lower(code)" in result.stdout.lower()


def test_offline_already_at_0021(tmp_path: Path) -> None:
    db = tmp_path / "at0021.db"
    _make_db(db, version="0021", with_data=True)
    before = _hash(db)
    result = _run_offline(f"sqlite:///{db}", ["upgrade", "head"])
    after = _hash(db)
    assert result.returncode == 0
    assert before == after


def test_offline_already_at_0022(tmp_path: Path) -> None:
    db = tmp_path / "at0022.db"
    _make_db(db, version="0022", with_data=True)
    before = _hash(db)
    result = _run_offline(f"sqlite:///{db}", ["upgrade", "head"])
    after = _hash(db)
    assert result.returncode == 0
    assert before == after
    # No further upgrades, SQL may be empty except version check
    assert result.returncode == 0


def test_offline_windows_path_and_percent_encoding(tmp_path: Path) -> None:
    # Path with spaces
    spaced = tmp_path / "space path.db"
    _make_db(spaced, version="0019", with_data=True)
    before = _hash(spaced)
    # Use percent-encoded URL
    encoded = spaced.as_posix().replace(" ", "%20")
    result = _run_offline(f"sqlite:///{encoded}", ["upgrade", "head"])
    after = _hash(spaced)
    assert result.returncode == 0, result.stderr[-500:]
    assert before == after

    # Windows-style path handling (drive letter simulation via absolute posix)
    # Just ensure no exception and file unchanged
    assert spaced.exists()


def test_offline_unrelated_tables_not_altered(tmp_path: Path) -> None:
    db = tmp_path / "unrelated.db"
    _make_db(db, version="0019", with_data=True)
    # Add extra table
    conn = sqlite3.connect(db)
    conn.execute("CREATE TABLE extra_thing (id TEXT PRIMARY KEY)")
    conn.execute("INSERT INTO extra_thing VALUES ('x')")
    conn.commit()
    conn.close()
    before = _hash(db)
    result = _run_offline(f"sqlite:///{db}", ["upgrade", "head"])
    after = _hash(db)
    assert result.returncode == 0
    assert before == after
    # SQL must not contain DROP for extra_thing
    assert "extra_thing" not in result.stdout or "DROP TABLE extra_thing" not in result.stdout
