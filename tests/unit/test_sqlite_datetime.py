"""SQLite datetime binds stay text and do not use the deprecated adapter."""

from __future__ import annotations

import warnings
from datetime import UTC, datetime

from sqlalchemy import Column, DateTime, MetaData, Table, select, text

from drilling_intelligence.config.settings import Settings
from drilling_intelligence.database.engine import (
    adapt_sqlite_value,
    build_engine,
    open_sqlite,
    sqlite_datetime_bind_installed,
)
from drilling_intelligence.database.session import Database


def _assert_typed_and_raw(engine, moment: datetime, naive: datetime) -> None:
    metadata = MetaData()
    events = Table("events", metadata, Column("at", DateTime(timezone=True)))
    metadata.create_all(engine)
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always", DeprecationWarning)
        with engine.begin() as connection:
            connection.execute(events.insert(), {"at": moment})
            connection.execute(events.insert(), [{"at": naive}, {"at": moment}])
            connection.execute(text("insert into events (at) values (:at)"), {"at": moment})
            connection.execute(
                text("insert into events (at) values (:at)"),
                [{"at": naive}],
            )
            connection.exec_driver_sql("insert into events (at) values (?)", (naive,))
            typed = connection.execute(select(events.c.at).limit(1)).scalar_one()
            stored = connection.execute(text("select at from events")).scalars().all()
            foreign_keys = connection.execute(text("pragma foreign_keys")).scalar()
    assert typed == datetime(2026, 1, 2, 3, 4, 5, 6)
    assert stored[0] == "2026-01-02 03:04:05.000006"
    assert moment.isoformat(" ") in stored
    assert naive.isoformat(" ") in stored
    assert not any("datetime adapter" in str(item.message) for item in caught)
    return foreign_keys


def test_sqlite_datetime_bind_keeps_existing_text_and_avoids_the_adapter(tmp_path) -> None:
    moment = datetime(2026, 1, 2, 3, 4, 5, 6, tzinfo=UTC)
    naive = datetime(2026, 1, 2, 3, 4, 5, 6)
    assert adapt_sqlite_value(moment) == moment.isoformat(" ")
    assert adapt_sqlite_value(naive) == naive.isoformat(" ")
    assert adapt_sqlite_value("already-text") == "already-text"
    assert adapt_sqlite_value(7) == 7
    assert adapt_sqlite_value(None) is None
    assert adapt_sqlite_value(b"bytes") == b"bytes"
    database = Database.from_url(f"sqlite:///{tmp_path / 'dates.db'}")
    try:
        _assert_typed_and_raw(database.engine, moment, naive)
    finally:
        database.engine.dispose()
    engine = build_engine(Settings(), f"sqlite:///{tmp_path / 'workspace.db'}")
    try:
        _assert_typed_and_raw(engine, moment, naive)
    finally:
        engine.dispose()
    migration = open_sqlite(f"sqlite:///{tmp_path / 'migration.db'}")
    try:
        foreign_keys = _assert_typed_and_raw(migration, moment, naive)
        assert foreign_keys in (0, None)
        assert sqlite_datetime_bind_installed(migration) is True
    finally:
        migration.dispose()

    class _Dialect:
        name = "postgresql"

    class _Engine:
        dialect = _Dialect()

    other = _Engine()
    from drilling_intelligence.database.engine import install_sqlite_datetime_bind

    install_sqlite_datetime_bind(other)  # type: ignore[arg-type]
    assert sqlite_datetime_bind_installed(other) is False  # type: ignore[arg-type]
