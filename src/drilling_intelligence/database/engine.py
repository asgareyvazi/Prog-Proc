"""Engine/session construction.

SQLite is configured for a desktop workload (WAL, foreign keys on, busy
timeout).  PostgreSQL/other dialects take the generic path - nothing in this
module may assume SQLite beyond the pragma block, which is guarded.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping, Sequence
from contextlib import closing
from datetime import datetime
from pathlib import Path
from weakref import WeakSet

from sqlalchemy import create_engine, event, make_url, text
from sqlalchemy.engine import URL, Engine

from ..config.settings import Settings

log = logging.getLogger("drilling_intelligence.database.engine")


def ensure_parent_dir(url: str | URL) -> Path | None:
    """SQLite needs its directory to exist; return it for the caller to create."""
    url_obj = url if isinstance(url, URL) else make_url(str(url))
    if url_obj.get_backend_name() != "sqlite":
        return None
    database = url_obj.database or ""
    if not database or database == ":memory:":
        return None
    path = Path(database).expanduser()
    return path.parent


def adapt_sqlite_value(value: object) -> object:
    """Match the historical sqlite3 datetime adapter without registering one.

    The default adapter writes ``isoformat(" ")`` and is deprecated on CPython
    3.12+. SQLAlchemy's DateTime bind processor already returns its own text;
    this only converts a datetime that would otherwise reach sqlite3.
    """
    if isinstance(value, datetime):
        return value.isoformat(" ")
    return value


def adapt_sqlite_parameters(parameters: object, *, executemany: bool) -> object:
    if executemany and isinstance(parameters, Sequence) and not isinstance(parameters, str | bytes):
        return type(parameters)(
            adapt_sqlite_parameters(item, executemany=False) for item in parameters
        )
    if isinstance(parameters, Mapping):
        return {key: adapt_sqlite_value(value) for key, value in parameters.items()}
    if isinstance(parameters, Sequence) and not isinstance(parameters, str | bytes):
        return type(parameters)(adapt_sqlite_value(item) for item in parameters)
    return adapt_sqlite_value(parameters)


_BOUND_ENGINES: WeakSet[Engine] = WeakSet()


def sqlite_datetime_bind_installed(engine: Engine) -> bool:
    return engine in _BOUND_ENGINES


def install_sqlite_datetime_bind(engine: Engine) -> None:
    """Bind datetimes on this SQLite engine only. Do not change the process-global adapter.

    A non-SQLite engine is left alone. A second call on the same engine does not
    add another listener, so a migration runner can install the bind on an engine
    that already has it.
    """
    if engine.dialect.name != "sqlite" or engine in _BOUND_ENGINES:
        return
    _BOUND_ENGINES.add(engine)

    @event.listens_for(engine, "before_cursor_execute", retval=True)
    def _bind_datetimes(
        _conn: object,
        _cursor: object,
        statement: str,
        parameters: object,
        _context: object,
        executemany: bool,
    ) -> tuple[str, object]:
        return statement, adapt_sqlite_parameters(parameters, executemany=executemany)


def open_sqlite(url: str) -> Engine:
    """Open a SQLite file the way the migration fixtures do, with the datetime bind.

    Desktop pragmas stay off. A legacy insert must keep SQLite's default foreign-key
    behavior. The datetime bind is still required: these fixtures pass Python
    datetime values through ``text()``, and a bare ``create_engine`` lets those
    values hit the deprecated stdlib adapter.
    """
    engine = create_engine(url)
    install_sqlite_datetime_bind(engine)
    return engine


def build_engine(settings: Settings, url: str | None = None, *, echo: bool | None = None) -> Engine:
    target = url or settings.database_url_for(Path.cwd())
    parent = ensure_parent_dir(target)
    if parent is not None:
        parent.mkdir(parents=True, exist_ok=True)
    kwargs: dict[str, object] = {
        "echo": settings.database.echo_sql if echo is None else echo,
        "future": True,
        # A desktop app runs its long jobs in worker threads; a small pool plus
        # WAL keeps writers from tripping over each other.
        "pool_pre_ping": True,
    }
    url_obj = make_url(str(target))
    if url_obj.get_backend_name() == "sqlite":
        kwargs["connect_args"] = {
            "check_same_thread": False,
            "timeout": max(1.0, settings.database.sqlite_busy_timeout_ms / 1000.0),
        }
    engine = create_engine(target, **kwargs)  # type: ignore[arg-type]

    if url_obj.get_backend_name() == "sqlite":
        busy_timeout = int(settings.database.sqlite_busy_timeout_ms)

        @event.listens_for(engine, "connect")
        def _sqlite_pragmas(
            dbapi_connection: object, _record: object
        ) -> None:  # pragma: no cover - thin wrapper
            with closing(dbapi_connection.cursor()) as cursor:  # type: ignore[attr-defined]
                cursor.execute("PRAGMA foreign_keys=ON")
                cursor.execute("PRAGMA journal_mode=WAL")
                cursor.execute("PRAGMA synchronous=NORMAL")
                cursor.execute(f"PRAGMA busy_timeout={busy_timeout}")

        install_sqlite_datetime_bind(engine)

    return engine


def database_dialect(engine: Engine) -> str:
    return engine.dialect.name


def sqlite_version(engine: Engine) -> str:
    if engine.dialect.name != "sqlite":
        return ""
    with engine.connect() as conn:
        return str(conn.execute(text("SELECT sqlite_version()")).scalar_one())


__all__ = ["build_engine", "database_dialect", "ensure_parent_dir", "sqlite_version"]
