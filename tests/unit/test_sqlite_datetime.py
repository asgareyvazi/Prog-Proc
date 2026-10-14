"""SQLite datetime binds stay text and do not use the deprecated adapter."""

from __future__ import annotations

import warnings
from datetime import UTC, datetime

from sqlalchemy import Column, DateTime, MetaData, Table, select, text

from drilling_intelligence.config.settings import Settings
from drilling_intelligence.database.engine import adapt_sqlite_value, build_engine
from drilling_intelligence.database.session import Database


def _assert_binds(engine, moment: datetime) -> None:
    metadata = MetaData()
    events = Table("events", metadata, Column("at", DateTime(timezone=True)))
    metadata.create_all(engine)
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always", DeprecationWarning)
        with engine.begin() as connection:
            connection.execute(events.insert(), {"at": moment})
            connection.execute(text("insert into events (at) values (:at)"), {"at": moment})
            typed = connection.execute(select(events.c.at).limit(1)).scalar_one()
            stored = connection.execute(text("select at from events")).scalars().all()
    assert typed == datetime(2026, 1, 2, 3, 4, 5, 6)
    assert stored == [
        "2026-01-02 03:04:05.000006",
        moment.isoformat(" "),
    ]
    assert not any("datetime adapter" in str(item.message) for item in caught)


def test_sqlite_datetime_bind_keeps_existing_text_and_avoids_the_adapter(tmp_path) -> None:
    moment = datetime(2026, 1, 2, 3, 4, 5, 6, tzinfo=UTC)
    naive = datetime(2026, 1, 2, 3, 4, 5, 6)
    assert adapt_sqlite_value(moment) == moment.isoformat(" ")
    assert adapt_sqlite_value(naive) == naive.isoformat(" ")
    assert adapt_sqlite_value("already-text") == "already-text"
    database = Database.from_url(f"sqlite:///{tmp_path / 'dates.db'}")
    _assert_binds(database.engine, moment)
    engine = build_engine(Settings(), f"sqlite:///{tmp_path / 'workspace.db'}")
    _assert_binds(engine, moment)
