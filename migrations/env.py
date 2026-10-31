"""Alembic environment.

The database URL is deliberately **not** stored in ``alembic.ini``: a workspace
owns its registry database, so the URL is resolved per invocation from

1. ``-x url=...`` (explicit, used by the CLI and the tests),
2. ``DRILLINTEL_DATABASE__URL`` (the same override path the app uses),
3. ``DRILLINTEL_CONFIG`` / ``configs/development.toml``.

Batch mode is always on: SQLite cannot alter a column in place, and every schema
change from now on has to remain appliable on the desktop database as well as on
PostgreSQL.
"""

from __future__ import annotations

import os
import sys
from logging.config import fileConfig
from pathlib import Path

from alembic import context
from sqlalchemy import engine_from_config, pool

config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from drilling_intelligence.config.settings import Settings  # noqa: E402
from drilling_intelligence.database import models  # noqa: F401,E402  (registers the tables)
from drilling_intelligence.database.base import Base  # noqa: E402
from drilling_intelligence.database.engine import install_sqlite_datetime_bind  # noqa: E402

target_metadata = Base.metadata


def _shared_engine() -> object | None:
    """An engine handed in by :func:`drilling_intelligence.database.migrations.upgrade`.

    Reusing the caller's engine keeps a migration on the same connection the
    application opened - important for SQLite, where a second connection to a
    freshly created file can race the WAL.
    """
    return context.config.attributes.get("engine")


def _resolve_url() -> str:
    x_args = context.get_x_argument(as_dictionary=True)
    explicit = (x_args.get("url") or "").strip()
    if explicit:
        return explicit
    from_env = (os.environ.get("DRILLINTEL_DATABASE__URL") or "").strip()
    if from_env:
        return from_env
    config_path = (os.environ.get("DRILLINTEL_CONFIG") or "").strip() or None
    settings = Settings.load(config_path)
    url = settings.database_url_for(Path.cwd())
    if not url.startswith("sqlite"):
        return url
    # A relative SQLite path is resolved against the working directory and the
    # parent directory must exist before SQLAlchemy tries to open the file.
    from drilling_intelligence.database.engine import ensure_parent_dir

    parent = ensure_parent_dir(url)
    if parent is not None:
        parent.mkdir(parents=True, exist_ok=True)
    return url


def _resolve_url_no_side_effect() -> str:
    """Resolve URL without creating parent directories (for offline safety)."""
    x_args = context.get_x_argument(as_dictionary=True)
    explicit = (x_args.get("url") or "").strip()
    if explicit:
        return explicit
    from_env = (os.environ.get("DRILLINTEL_DATABASE__URL") or "").strip()
    if from_env:
        return from_env
    config_path = (os.environ.get("DRILLINTEL_CONFIG") or "").strip() or None
    settings = Settings.load(config_path)
    url = settings.database_url_for(Path.cwd())
    return url


def _configure() -> None:
    from drilling_intelligence.database.migrations import alembic_config_url

    config.set_main_option("sqlalchemy.url", alembic_config_url(_resolve_url()))


def run_migrations_offline() -> None:
    """Emit SQL to stdout (``alembic upgrade head --sql``) for review before apply.

    SQLite batch migrations (``op.batch_alter_table(recreate=\"auto\")``) need a
    live connection to reflect the existing table.  Offline mode normally has
    only a URL, so batch would fail with
    ``batch mode with dialect sqlite requires a live database connection``.

    The previous implementation created a temporary engine from the configured
    file URL and called ``Base.metadata.create_all(tmp_engine)``, which for a
    file-backed URL points at the real workspace database — an offline SQL
    render must never mutate a user's database or create a file that did not
    exist.  This implementation uses a genuinely isolated in-memory SQLite
    database for reflection, never touching the workspace path.
    """
    # Resolve URL without side effects (no mkdir, no file creation).
    url = _resolve_url_no_side_effect()
    # Keep config url in sync for Alembic's bookkeeping, but do not create parent dirs.
    try:
        from drilling_intelligence.database.migrations import alembic_config_url

        config.set_main_option("sqlalchemy.url", alembic_config_url(url))
    except Exception:
        config.set_main_option("sqlalchemy.url", url.replace("%", "%%"))
    # SQLite batch offline needs a live connection for reflection; provide an isolated one.
    if url and url.startswith("sqlite"):
        from sqlalchemy import create_engine, text as sa_text

        # Always use an isolated in-memory database for reflection — never the
        # workspace file, even when the URL is file-backed.  Creating the schema
        # from Base.metadata gives batch the table to reflect without touching
        # the user's file.  To make offline incremental (only pending migrations)
        # we must seed the in-memory alembic_version from the real file if it
        # exists, without mutating the file.
        real_version = None
        file_path = None
        try:
            if url not in ("sqlite://", "sqlite:///:memory:", "sqlite:////", "sqlite:///"):
                raw = url.split(":///", 1)[-1] if ":///" in url else ""
                from urllib.parse import unquote

                raw = raw.split("?")[0].split("#")[0]
                raw = unquote(raw)
                candidate = Path(raw)
                if not candidate.is_absolute():
                    candidate = (Path.cwd() / candidate).resolve()
                if candidate.exists() and candidate.is_file():
                    file_path = candidate
        except Exception:
            file_path = None
        if file_path is not None:
            try:
                import sqlite3

                conn = sqlite3.connect(str(file_path))
                try:
                    cur = conn.execute("SELECT version_num FROM alembic_version LIMIT 1")
                    row = cur.fetchone()
                    if row:
                        real_version = row[0]
                except Exception:
                    real_version = None
                finally:
                    conn.close()
            except Exception:
                real_version = None
        # Prepare isolated in-memory DB for reflection.
        # For offline incremental, Alembic needs to know the current heads (real_version)
        # but the reflection DB must have tables so batch can reflect.  Using head
        # schema for reflection while seeding alembic_version to real_version makes
        # Alembic render only pending migrations (it reads version from the
        # connection's alembic_version) while batch's explicit _target_table_*
        # definitions ensure the DDL is correct regardless of the reflection DB's
        # column set.  This avoids mutating the user's file while keeping offline
        # incremental.
        tmp_engine = create_engine("sqlite://")
        if real_version:
            Base.metadata.create_all(tmp_engine)
            with tmp_engine.begin() as conn:
                # Ensure alembic_version table exists (Base.metadata does not include it)
                conn.execute(
                    sa_text(
                        "CREATE TABLE IF NOT EXISTS alembic_version (version_num VARCHAR(32) NOT NULL PRIMARY KEY)"
                    )
                )
                conn.execute(sa_text("DELETE FROM alembic_version"))
                conn.execute(sa_text("INSERT INTO alembic_version (version_num) VALUES (:v)"), {"v": real_version})
        else:
            # Empty / no version: leave empty so offline renders full creation from scratch.
            # Create an empty alembic_version so Alembic can treat it as base
            with tmp_engine.begin() as conn:
                conn.execute(
                    sa_text(
                        "CREATE TABLE IF NOT EXISTS alembic_version (version_num VARCHAR(32) NOT NULL PRIMARY KEY)"
                    )
                )
        # For empty DB, batch still needs a connection; Alembic will generate CREATE TABLEs
        with tmp_engine.connect() as connection:
            context.configure(
                connection=connection,
                url=url,
                target_metadata=target_metadata,
                literal_binds=True,
                dialect_opts={"paramstyle": "named"},
                compare_type=True,
                render_as_batch=True,
            )
            with context.begin_transaction():
                context.run_migrations()
        tmp_engine.dispose()
        return
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        compare_type=True,
        render_as_batch=True,
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    shared = _shared_engine()
    if shared is not None:
        with shared.connect() as connection:
            context.configure(
                connection=connection,
                target_metadata=target_metadata,
                compare_type=True,
                compare_server_default=True,
                render_as_batch=True,
            )
            with context.begin_transaction():
                context.run_migrations()
        return
    _configure()
    connectable = engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    install_sqlite_datetime_bind(connectable)
    with connectable.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            compare_type=True,
            compare_server_default=True,
            render_as_batch=True,
        )
        with context.begin_transaction():
            context.run_migrations()
    connectable.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
