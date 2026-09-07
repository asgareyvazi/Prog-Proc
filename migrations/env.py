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


def _configure() -> None:
    config.set_main_option("sqlalchemy.url", _resolve_url().replace("%", "%%"))


def run_migrations_offline() -> None:
    """Emit SQL to stdout (``alembic upgrade head --sql``) for review before apply."""
    _configure()
    context.configure(
        url=config.get_main_option("sqlalchemy.url"),
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
