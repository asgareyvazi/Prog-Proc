"""Programmatic access to the Alembic migration history.

``ensure_schema`` is the single entry point the application uses to get a
usable database:

*   an empty file  -> run migrations to head;
*   an existing DB -> upgrade to head, refusing to touch an unknown schema;
*   no migration scripts (an installed wheel without ``migrations/``) -> create
    from metadata and *stamp* it so the next real migration still applies.

The fallback is deliberate and loud: it is logged and the returned status
records which path was taken, so nobody can mistake a stamped schema for a
migrated one.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from sqlalchemy import Engine, inspect, text

from ..core.logging import get_logger
from .base import Base

log = get_logger("database.migrations")

ENV_MIGRATIONS_DIR = "DRILLINTEL_MIGRATIONS_DIR"


def find_migrations_dir(start: Path | None = None) -> Path | None:
    """Locate the Alembic script directory (works from a source checkout)."""
    env = os.environ.get(ENV_MIGRATIONS_DIR, "").strip()
    if env:
        candidate = Path(env).expanduser()
        return candidate if (candidate / "env.py").exists() else None
    here = (start or Path(__file__).resolve()).parent
    candidates = [
        # <repo>/src/drilling_intelligence/database/migrations.py -> <repo>/migrations
        here.parents[3] / "migrations",
        Path.cwd() / "migrations",
    ]
    for candidate in candidates:
        if (candidate / "env.py").exists():
            return candidate
    return None


@dataclass
class MigrationStatus:
    #: ``migrated`` (forward), ``downgraded`` (back, only via allow_downgrade),
    #: ``downgrade-required`` (an older revision was asked for without permission),
    #: ``already-current``, ``stamped-from-metadata``, ``unavailable``.
    mode: str = "unknown"
    current: str = ""
    head: str = ""
    detail: str = ""

    @property
    def up_to_date(self) -> bool:
        return bool(self.head) and self.current == self.head

    def to_dict(self) -> dict[str, object]:
        return {
            "mode": self.mode,
            "current": self.current,
            "head": self.head,
            "detail": self.detail,
            "up_to_date": self.up_to_date,
        }


def _bare_config(directory: Path) -> object:
    from alembic.config import Config

    config = Config()
    config.set_main_option("script_location", str(directory))
    config.attributes["configure_logger"] = False
    return config


def heads(migrations_dir: Path | None = None) -> list[str]:
    from alembic.script import ScriptDirectory

    directory = migrations_dir or find_migrations_dir()
    if directory is None:
        return []
    return list(ScriptDirectory.from_config(_bare_config(directory)).get_heads())


def current_revision(engine: Engine) -> str:
    from alembic.runtime.migration import MigrationContext

    try:
        with engine.connect() as conn:
            return str(MigrationContext.configure(conn).get_current_revision() or "")
    except Exception:  # noqa: BLE001 - alembic_version may not exist yet
        return ""


def upgrade(
    engine: Engine, revision: str = "head", *, allow_downgrade: bool = False
) -> MigrationStatus:
    """Bring the schema to ``revision``; report how it got there.

    Forward is the normal direction and the only one a workspace opening ever needs.
    Moving *back* is a deliberate act - a repair tool rolling a half-finished migration off
    a database - so it requires ``allow_downgrade``; without it the status says
    ``downgrade-required`` instead of silently reporting that nothing needed doing.
    """
    directory = find_migrations_dir()
    existing = set(inspect(engine).get_table_names())
    if not existing:
        if directory is None:
            return _create_and_stamp(engine, "no migration scripts available (installed wheel)")
        return _run_upgrade(engine, directory, revision, allow_downgrade=allow_downgrade)
    if "alembic_version" not in existing:
        if not _looks_like_initial_schema(engine):
            return MigrationStatus(
                mode="unavailable",
                detail="Database has neither alembic_version nor the platform tables",
            )
        return _create_and_stamp(engine, "pre-migration database stamped at head", stamp_only=True)
    if directory is None:
        head = ""
        return MigrationStatus(
            mode="stamped-from-metadata",
            current=current_revision(engine),
            head=head,
            detail="migration scripts unavailable",
        )
    return _run_upgrade(engine, directory, revision, allow_downgrade=allow_downgrade)


def _run_upgrade(
    engine: Engine, directory: Path, revision: str, *, allow_downgrade: bool = False
) -> MigrationStatus:
    from alembic import command
    from alembic.config import Config
    from alembic.script import ScriptDirectory

    config = Config()
    config.set_main_option("script_location", str(directory))
    config.set_main_option("sqlalchemy.url", str(engine.url))
    config.attributes["engine"] = engine
    config.attributes["configure_logger"] = False
    before = current_revision(engine)
    # ``command.upgrade`` only walks forward: asking it for an older revision is a silent
    # no-op, which would be reported as "already-current" - the last thing a person
    # reviewing a failed migration needs.  So the direction is decided here, explicitly.
    # ``walk_revisions`` yields head-first, so a *higher* index means an *older* revision.
    order = [step.revision for step in ScriptDirectory.from_config(config).walk_revisions()]
    head = ",".join(ScriptDirectory.from_config(config).get_heads())
    behind = (
        bool(before)
        and revision in order
        and before in order
        and order.index(revision) > order.index(before)
    )
    if behind and not allow_downgrade:
        return MigrationStatus(
            mode="downgrade-required",
            current=before,
            head=head,
            detail=f"{revision} is older than the current revision {before}; pass allow_downgrade=True to roll back",
        )
    if behind:
        command.downgrade(config, revision)
    else:
        command.upgrade(config, revision)
    after = current_revision(engine)
    if before and before == after == head:
        mode = "already-current"
    elif (
        before
        and after
        and before in order
        and after in order
        and order.index(after) > order.index(before)
    ):
        mode = "downgraded"
    else:
        mode = "migrated"
    log.event("db.migrate", before=before or None, after=after, head=head, mode=mode)
    return MigrationStatus(mode=mode, current=after, head=head)


def _create_and_stamp(engine: Engine, reason: str, *, stamp_only: bool = False) -> MigrationStatus:
    from alembic import command
    from alembic.config import Config
    from alembic.script import ScriptDirectory

    head = ",".join(heads()) or "0001_initial"
    if not stamp_only:
        Base.metadata.create_all(engine)
    directory = find_migrations_dir()
    if directory is not None:
        config = Config()
        config.set_main_option("script_location", str(directory))
        config.set_main_option("sqlalchemy.url", str(engine.url))
        config.attributes["engine"] = engine
        config.attributes["configure_logger"] = False
        command.stamp(config, "head")
        head = ",".join(ScriptDirectory.from_config(config).get_heads())
    else:
        with engine.begin() as conn:
            conn.execute(
                text(
                    "CREATE TABLE IF NOT EXISTS alembic_version (version_num VARCHAR(32) NOT NULL PRIMARY KEY)"
                )
            )
            conn.execute(text("DELETE FROM alembic_version"))
            conn.execute(
                text("INSERT INTO alembic_version (version_num) VALUES (:rev)"), {"rev": head}
            )
    log.warning(
        "schema stamped at %s: %s", head, reason, extra={"event": "db.stamp", "revision": head}
    )
    return MigrationStatus(mode="stamped-from-metadata", current=head, head=head, detail=reason)


def _looks_like_initial_schema(engine: Engine) -> bool:
    tables = set(inspect(engine).get_table_names())
    return {"well", "document", "document_version"} <= tables


def ensure_schema(engine: Engine) -> MigrationStatus:
    """Idempotent schema readiness check used when a workspace is opened."""
    status = upgrade(engine)
    if status.mode == "unavailable":
        raise RuntimeError(f"Cannot manage this database schema: {status.detail}")
    return status


def schema_diff(engine: Engine) -> dict[str, list[str]]:
    """Compare live tables/columns with ORM metadata (``doctor`` and CI use this).

    It catches the classic drift failure - models changed, migration not
    written - before it corrupts a user's workspace.
    """
    inspector = inspect(engine)
    live_tables = set(inspector.get_table_names())
    model_tables = set(Base.metadata.tables)
    result: dict[str, list[str]] = {
        "missing_tables": sorted(model_tables - live_tables),
        "extra_tables": sorted(t for t in live_tables - model_tables if t != "alembic_version"),
        "missing_columns": [],
        "extra_columns": [],
    }
    for table in sorted(model_tables & live_tables):
        model_cols = {c.name for c in Base.metadata.tables[table].columns}
        live_cols = {c["name"] for c in inspector.get_columns(table)}
        result["missing_columns"].extend(f"{table}.{c}" for c in sorted(model_cols - live_cols))
        result["extra_columns"].extend(f"{table}.{c}" for c in sorted(live_cols - model_cols))
    return result


__all__ = [
    "MigrationStatus",
    "current_revision",
    "ensure_schema",
    "find_migrations_dir",
    "heads",
    "schema_diff",
    "upgrade",
]
