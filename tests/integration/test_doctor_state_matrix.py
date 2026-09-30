"""`doctor` against real database states: no false "healthy", no false "behind".

A diagnostic is only useful if it is truthful in both directions.  Every state here is a real
SQLite file on disk - built by running the real migrations, or by corrupting a real one - and each
is checked for the three things an operator actually depends on: the verdict, the exit code, and
whether the message describes something they can act on.

One state is deliberately absent: a database stamped one revision behind *while already carrying
the newer schema*.  It cannot be reached except by hand-editing ``alembic_version``, and the
platform is not obliged to survive a database that lies about itself in a way no real upgrade
produces.  The genuine one-revision-behind case is covered, and it upgrades cleanly.
"""

from __future__ import annotations

import io
import json
import shutil
import sqlite3
import sys
from pathlib import Path

import pytest

from drilling_intelligence.cli.app import main
from drilling_intelligence.database.migrations import (
    METADATA_REVISION,
)

DB_RELATIVE = Path(".drillintel/database/drilling_intelligence.db")


def _doctor(root: Path) -> tuple[int, dict | None, str]:
    """Run the real CLI in JSON mode and return its exit code, payload and combined text."""
    out, err = io.StringIO(), io.StringIO()
    saved = sys.stdout, sys.stderr
    sys.stdout, sys.stderr = out, err
    try:
        code = main(["doctor", "--workspace", str(root), "--json"])
    finally:
        sys.stdout, sys.stderr = saved
    text = out.getvalue()
    try:
        return code, json.loads(text), text
    except json.JSONDecodeError:
        return code, None, text + err.getvalue()


def _db(root: Path) -> Path:
    return root / DB_RELATIVE


def _execute(root: Path, sql: str) -> None:
    connection = sqlite3.connect(_db(root))
    try:
        connection.execute(sql)
        connection.commit()
    finally:
        connection.close()


def _stamp(root: Path) -> str:
    connection = sqlite3.connect(_db(root))
    try:
        return connection.execute("select version_num from alembic_version").fetchone()[0]
    finally:
        connection.close()


@pytest.fixture
def workspace(tmp_path: Path) -> Path:
    """A real workspace, opened once so its database actually exists on disk."""
    root = tmp_path / "ws"
    root.mkdir()
    buf_out, buf_err = io.StringIO(), io.StringIO()
    saved = sys.stdout, sys.stderr
    sys.stdout, sys.stderr = buf_out, buf_err
    try:
        assert main(["workspace", "create", str(root)]) == 0
        main(["doctor", "--workspace", str(root)])  # materialises the database
    finally:
        sys.stdout, sys.stderr = saved
    assert _db(root).exists(), "the fixture must leave a real database behind"
    return root


def test_a_healthy_workspace_reports_no_findings_and_exits_zero(workspace) -> None:
    code, payload, _text = _doctor(workspace)
    assert code == 0
    assert payload["schema"]["up_to_date"] is True
    assert payload["schema"]["current"] == METADATA_REVISION
    assert payload["findings"] == []


def test_a_genuine_one_revision_behind_database_upgrades_cleanly(workspace) -> None:
    """The real behind case: stamped 0011 with the 0011 schema, not a hand-edited stamp.

    Opening the workspace is allowed to migrate - that is the documented behaviour - so the
    honest outcome is a clean upgrade reported as up to date, not a scary "behind" finding about
    a problem that opening the workspace just fixed.
    """
    import argparse

    from alembic import command
    from alembic.config import Config

    behind = workspace.parent / "behind.db"
    config = Config(str(Path(__file__).resolve().parents[2] / "alembic.ini"))
    config.set_main_option(
        "script_location", str(Path(__file__).resolve().parents[2] / "migrations")
    )
    # migrations/env.py resolves its URL from `-x url=...` first, which is how the CLI and the
    # tests both target a specific database; programmatically that arrives via cmd_opts.
    config.cmd_opts = argparse.Namespace(x=[f"url=sqlite:///{behind}"])
    command.upgrade(config, "0011")

    connection = sqlite3.connect(behind)
    assert connection.execute("select version_num from alembic_version").fetchone()[0] == "0011"
    has_new_index = connection.execute(
        "select 1 from sqlite_master where name='uq_calculation_one_superseding_revision'"
    ).fetchall()
    connection.close()
    assert not has_new_index, "the fixture must be a genuine 0011 schema, not a relabelled 0012"

    shutil.copy(behind, _db(workspace))
    assert _stamp(workspace) == "0011"

    code, payload, _text = _doctor(workspace)
    assert code == 0
    assert payload["schema"]["up_to_date"] is True
    assert payload["schema"]["current"] == METADATA_REVISION
    assert _stamp(workspace) == METADATA_REVISION, "the upgrade must actually have been applied"


def test_a_database_whose_version_table_was_dropped_is_repaired_not_condemned(workspace) -> None:
    """Schema intact but ``alembic_version`` gone: stamped from metadata, which is the honest read."""
    _execute(workspace, "DROP TABLE alembic_version")
    code, payload, _text = _doctor(workspace)

    assert code == 0
    assert payload["schema"]["mode"] == "stamped-from-metadata"
    assert payload["schema"]["up_to_date"] is True


def test_an_unexpected_extra_table_is_tolerated(workspace) -> None:
    """An extra table is not corruption; refusing to run would be crying wolf."""
    _execute(workspace, "CREATE TABLE surprise (id TEXT)")
    code, payload, _text = _doctor(workspace)

    assert code == 0
    assert payload["schema"]["up_to_date"] is True


def test_a_malformed_migration_stamp_is_an_error_not_a_guess(workspace) -> None:
    """An unreadable stamp must fail loudly rather than be interpreted as something."""
    _execute(workspace, "UPDATE alembic_version SET version_num='not-a-revision'")
    code, payload, text = _doctor(workspace)

    assert code != 0, "an unusable stamp must not report success"
    assert payload is None or not payload.get("schema", {}).get("up_to_date"), payload or text
    assert "not-a-revision" in text or "revision" in text.lower(), text[-400:]


def test_a_missing_expected_table_is_an_error_not_a_silent_zero(workspace) -> None:
    """A dropped core table must not read as an empty-but-healthy registry."""
    _execute(workspace, "DROP TABLE document")
    code, _payload, text = _doctor(workspace)

    assert code != 0, "counting zero documents in a schema with no document table is a lie"
    assert "document" in text, text[-400:]


def test_an_unrecognisable_database_is_refused_with_a_reason(workspace) -> None:
    """A SQLite file with none of the platform's tables is not a workspace database."""
    _db(workspace).write_bytes(b"")
    connection = sqlite3.connect(_db(workspace))
    connection.execute("CREATE TABLE unrelated (x INTEGER)")
    connection.commit()
    connection.close()

    code, _payload, text = _doctor(workspace)
    assert code != 0
    assert "neither alembic_version nor the platform tables" in text or "schema" in text.lower(), (
        text[-400:]
    )


def test_an_empty_database_is_bootstrapped_rather_than_reported_broken(workspace) -> None:
    """Zero bytes is a new workspace, not a corrupt one - and the schema is built."""
    _db(workspace).write_bytes(b"")
    code, payload, _text = _doctor(workspace)

    assert code == 0
    assert payload["schema"]["mode"] == "migrated"
    assert payload["schema"]["current"] == METADATA_REVISION
