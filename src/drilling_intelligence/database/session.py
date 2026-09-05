"""Session and unit-of-work management.

Repository code always runs inside :meth:`Database.unit_of_work`, which commits
on success and rolls back on any exception.  Services never call ``commit()``
themselves, so a multi-step operation (register document -> version -> extract
-> classify -> index) is atomic by construction.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from sqlalchemy import Engine, create_engine, event, inspect, text
from sqlalchemy.orm import Session, sessionmaker

from ..config.settings import Settings
from .base import Base
from .engine import ensure_parent_dir


class Database:
    """Owns the engine and session factory for one workspace database."""

    def __init__(self, engine: Engine, *, settings: Settings | None = None, echo: bool = False) -> None:
        self.engine = engine
        self.settings = settings
        self._factory = sessionmaker(bind=engine, expire_on_commit=False, future=True, autoflush=False)

    # -- construction -------------------------------------------------------
    @classmethod
    def from_url(cls, url: str, settings: Settings | None = None) -> Database:
        echo = bool(settings and settings.database.echo_sql)
        engine = create_engine(url, echo=echo, future=True, pool_pre_ping=True)
        if engine.dialect.name == "sqlite":
            busy = int(settings.database.sqlite_busy_timeout_ms) if settings else 5000

            @event.listens_for(engine, "connect")
            def _pragmas(dbapi_conn: object, _rec: object) -> None:  # pragma: no cover - thin wrapper
                # These pragmas are what makes foreign keys enforced and concurrent
                # readers wait instead of failing, so every connection has to run them.
                # sqlite3.Cursor only learned the context-manager protocol in 3.12 and
                # this handler receives the raw DBAPI connection, so close it by hand.
                cursor = dbapi_conn.cursor()  # type: ignore[attr-defined]
                try:
                    cursor.execute("PRAGMA foreign_keys=ON")
                    cursor.execute("PRAGMA journal_mode=WAL")
                    cursor.execute(f"PRAGMA busy_timeout={busy}")
                finally:
                    cursor.close()
        return cls(engine, settings=settings)

    @classmethod
    def for_workspace(cls, workspace_root: Path | str, settings: Settings) -> Database:
        url = settings.database_url_for(Path(workspace_root))
        parent = ensure_parent_dir(url)
        if parent is not None:
            parent.mkdir(parents=True, exist_ok=True)
        return cls.from_url(url, settings)

    # -- access -------------------------------------------------------------
    @property
    def url(self) -> str:
        return str(self.engine.url)

    def session(self) -> Session:
        return self._factory()

    @contextmanager
    def unit_of_work(self) -> Iterator[Session]:
        session = self._factory()
        try:
            yield session
            session.commit()
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()

    @contextmanager
    def read_only(self) -> Iterator[Session]:
        """Explicit read path: no accidental writes from UI/presentation code."""
        session = self._factory()
        try:
            yield session
        finally:
            session.close()

    def create_all(self) -> None:
        """Direct metadata creation - for tests and bootstrapping only.

        Production initialisation goes through Alembic (see
        :mod:`drilling_intelligence.database.migrations`) so that the deployed
        schema and the migration history can never disagree silently.
        """
        Base.metadata.create_all(self.engine)

    def drop_all(self) -> None:
        Base.metadata.drop_all(self.engine)

    def missing_tables(self) -> list[str]:
        inspector = inspect(self.engine)
        existing = set(inspector.get_table_names())
        return sorted(set(Base.metadata.tables) - existing)

    def has_data(self) -> bool:
        try:
            with self.engine.connect() as conn:
                return bool(conn.execute(text("SELECT 1 FROM document LIMIT 1")).first())
        except Exception:  # noqa: BLE001 - absent table simply means "no data"
            return False

    def dispose(self) -> None:
        self.engine.dispose()


__all__ = ["Database"]
