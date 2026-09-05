"""The schema is created by migrations and nothing else (spec section 31).

A fresh database must end up at the Alembic head, the models must not drift from the
migration, reopening a workspace must not touch the schema again, and the offline
``--sql`` path must keep working because that is how a DBA reviews a migration.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

from sqlalchemy import inspect

from drilling_intelligence.database.migrations import (
    current_revision,
    ensure_schema,
    find_migrations_dir,
    heads,
    schema_diff,
    upgrade,
)
from drilling_intelligence.database.session import Database

ROOT = Path(__file__).resolve().parents[2]
REQUIRED_TABLES = {"workspace", "well", "document", "document_version", "source", "extraction", "audit_event"}


def test_the_migrations_tree_is_found_from_the_installed_package() -> None:
    directory = find_migrations_dir()
    assert directory is not None and directory.name == "migrations"
    assert (directory / "env.py").exists() and (directory / "versions").is_dir()
    assert heads(), "there must be at least one migration in the chain"


def test_a_fresh_database_is_created_by_upgrade(tmp_path) -> None:
    database = Database.from_url(f"sqlite:///{tmp_path / 'fresh.db'}")
    try:
        status = upgrade(database.engine)
        assert status.mode in {"migrated", "stamped-from-metadata"}, status.detail
        assert status.up_to_date, status.to_dict()
        assert current_revision(database.engine) == heads()[0]
        tables = set(inspect(database.engine).get_table_names())
        assert tables >= REQUIRED_TABLES, sorted(REQUIRED_TABLES - tables)
    finally:
        database.dispose()


def test_migrated_schema_matches_the_models_exactly(tmp_path) -> None:
    """The drift check CI must never lose: models changed without a migration is fatal."""
    database = Database.from_url(f"sqlite:///{tmp_path / 'diff.db'}")
    try:
        upgrade(database.engine)
        diff = schema_diff(database.engine)
        offending = {key: value for key, value in diff.items() if value}
        assert not offending, f"models drifted away from the migration: {offending}"
    finally:
        database.dispose()


def test_ensure_schema_is_idempotent(tmp_path) -> None:
    database = Database.from_url(f"sqlite:///{tmp_path / 'idem.db'}")
    try:
        first = ensure_schema(database.engine)
        second = ensure_schema(database.engine)
        assert first.mode in {"migrated", "stamped-from-metadata", "already-current"}, first.to_dict()
        assert second.mode == "already-current", second.to_dict()
        assert second.up_to_date
    finally:
        database.dispose()


def test_a_workspace_is_migrated_when_it_is_opened(workspace) -> None:
    """Opening a workspace is the path users actually take, so it runs the migrations."""
    assert workspace.database is not None  # the engine is built lazily, on first use
    assert workspace.database_path.exists()
    status = ensure_schema(workspace.database.engine)
    assert status.mode == "already-current", status.to_dict()
    assert current_revision(workspace.database.engine) == heads()[0]
    tables = set(inspect(workspace.database.engine).get_table_names())
    assert {"document", "document_version", "extraction"} <= tables


def test_offline_sql_generation_still_works(tmp_path) -> None:
    env = dict(os.environ, DRILLINTEL_DATABASE__URL=f"sqlite:///{tmp_path / 'offline.db'}")
    result = subprocess.run(
        [sys.executable, "-m", "alembic", "-c", str(ROOT / "alembic.ini"), "upgrade", "head", "--sql"],
        cwd=str(ROOT),
        capture_output=True,
        text=True,
        env=env,
        check=False,
    )
    assert result.returncode == 0, result.stderr[-1200:]
    assert "CREATE TABLE document" in result.stdout, result.stdout[:600]
    assert "alembic_version" in result.stdout


def test_the_initial_migration_starts_the_chain_and_can_be_reversed() -> None:
    versions = sorted((ROOT / "migrations" / "versions").glob("*.py"))
    assert versions, "the chain must exist"
    first = next(path for path in versions if "initial_schema" in path.name)
    text = first.read_text(encoding="utf-8")
    assert "down_revision = None" in text.replace('"', "'"), "the chain must start here"
    assert "op.create_table" in text and "def downgrade" in text, "every migration needs a downgrade"


def test_the_database_url_is_never_baked_into_the_migration_config() -> None:
    """The URL comes from settings or the engine Alembic was handed, never from the ini."""
    assert "sqlite:///" not in (ROOT / "alembic.ini").read_text(encoding="utf-8")
    env = (ROOT / "migrations" / "env.py").read_text(encoding="utf-8")
    assert 'attributes.get("engine")' in env, "ensure_schema() hands over a live engine and env.py must use it"
