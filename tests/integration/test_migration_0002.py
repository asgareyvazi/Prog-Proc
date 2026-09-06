"""0001 → 0002 on a database that already holds documents: repair, constrain, survive.

``tests/integration/test_migrations.py`` proves the empty-file cases (create, stamp, drift,
offline SQL).  This file covers the case that can only be tested with data in the way: a
Phase-0 workspace that was written *before* the invariants existed.  Migration 0002 adds a
partial unique index and a real foreign key to ``document``, so it has to fix rows it is
about to constrain - and it must not lose anything while rebuilding ``document`` in order
to add the foreign key (SQLite has no ``ADD CONSTRAINT``).

The legacy rows are inserted against the *reflected* 0001 tables, so this test keeps working
when the model grows: it is deliberately written in 0001's vocabulary, violating exactly the
invariants the review asked about (two current versions, a pointer naming the older one, a
version with no durable relative path, extractions that should become cache entries).
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from sqlalchemy import MetaData, create_engine, inspect, text
from sqlalchemy.engine import Engine
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from drilling_intelligence.database.integrity import (
    check_current_version_invariants,
    check_extraction_cache,
    check_knowledge_relations,
)
from drilling_intelligence.database.migrations import current_revision, heads, schema_diff, upgrade

#: The legacy rows are inserted against the *reflected* 0001 tables, so nothing here depends
#: on the ORM classes (which describe 0002, not what a 0001 database actually accepts).
NOW = "2026-01-01 00:00:00"
SHA = "a" * 64
#: ``revision`` of the initial migration, read from the file it lives in.
FIRST_REVISION = "0001"
#: Reflected columns carry the ORM's ``DateTime`` type, which on SQLite accepts Python
#: datetimes and nothing else - so the inserts below use objects, while the raw ``text()``
#: statements further down stay strings (no type is known to bind against there).
NOW_DT = datetime(2026, 1, 1, tzinfo=UTC)
LATER_DT = datetime(2026, 1, 2, tzinfo=UTC)


def legacy_tables(engine: Engine) -> MetaData:
    metadata = MetaData()
    metadata.reflect(bind=engine)
    return metadata


def build_legacy_database(engine: Engine) -> None:
    """A 0001 schema plus the rows a Phase-0 run would have left behind."""
    status = upgrade(engine, "0001")
    assert status.mode == "migrated" and status.current == FIRST_REVISION, status.to_dict()
    assert current_revision(engine) == FIRST_REVISION, current_revision(engine)
    tables = legacy_tables(engine).tables
    assert "extraction_cache" not in tables, "the point of the test is that this table is missing"

    versions = [
        {
            "id": "ver-1",
            "document_id": "doc-1",
            "version_number": 1,
            "revision_key": "rev-12",
            "revision": 12,
            "status": "DRAFT",
            # An absolute path from another machine: the shape that must not be the only
            # durable reference left in the row.
            "source_path": "/home/other/imports/mud_report.xlsx",
            "sha256": SHA,
            "size_bytes": 20480,
            "mime_type": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            "parser": "excel",
            "parser_version": "1",
            "extraction_version": "1",
            "origin": "NEW",
            # Both versions are current: the bug the review asked about.
            "is_current": True,
            "superseded_by_version_id": "ver-2",
            "created_at": NOW_DT,
            "updated_at": NOW_DT,
        },
        {
            "id": "ver-2",
            "document_id": "doc-1",
            "version_number": 2,
            "revision_key": "rev-13",
            "revision": 13,
            "status": "DRAFT",
            "source_path": "/home/other/imports/mud_report.xlsx",
            "sha256": SHA,
            "size_bytes": 20480,
            "mime_type": "",
            "parser": "excel",
            "parser_version": "1",
            "extraction_version": "1",
            "origin": "MODIFIED",
            "is_current": True,
            "superseded_by_version_id": None,
            "created_at": NOW_DT,
            "updated_at": NOW_DT,
        },
    ]
    document = {
        "id": "doc-1",
        "identity_path": "documents/mud_report.xlsx",
        "filename": "mud_report.xlsx",
        "extension": ".xlsx",
        "mime_type": "",
        "size_bytes": 20480,
        "sha256": SHA,
        "imported_at": NOW_DT,
        "classification": "MUD_REPORT",
        "revision_key": "rev-13",
        "status": "CURRENT",
        "processing_status": "PROCESSED",
        "change_count": 2,
        # The pointer names the *superseded* version: a mismatch the repair must fix.
        "current_version_id": "ver-1",
        "created_at": NOW_DT,
        "updated_at": NOW_DT,
    }
    extractions = [
        {
            "id": "ext-1",
            "document_id": "doc-1",
            "document_version_id": "ver-1",
            "content_sha256": SHA,
            "extractor": "excel",
            "extractor_version": "1",
            "config_hash": "cfg",
            "status": "OK",
            "error": None,
            "document_json": {"text": "mud weight 10.2 ppg"},
            "created_at": NOW_DT,
            "updated_at": NOW_DT,
        },
        {
            # A second artefact for the same key: the backfill must pick one entry only.
            "id": "ext-2",
            "document_id": "doc-1",
            "document_version_id": "ver-2",
            "content_sha256": SHA,
            "extractor": "excel",
            "extractor_version": "1",
            "config_hash": "cfg",
            "status": "OK",
            "error": None,
            "document_json": {"text": "mud weight 10.2 ppg"},
            "created_at": LATER_DT,
            "updated_at": LATER_DT,
        },
        {
            "id": "ext-3",
            "document_id": "doc-1",
            "document_version_id": "ver-2",
            "content_sha256": SHA,
            "extractor": "mineru",
            "extractor_version": "1",
            "config_hash": "cfg",
            "status": "FAILED",
            "error": "mineru unavailable",
            "document_json": None,
            "created_at": NOW_DT,
            "updated_at": NOW_DT,
        },
    ]
    with engine.begin() as connection:
        connection.execute(tables["document_version"].insert().values(versions))
        connection.execute(tables["document"].insert().values(document))
        connection.execute(tables["extraction"].insert().values(extractions))


def test_the_invariants_of_a_phase_zero_workspace_are_repaired_on_upgrade(tmp_path) -> None:
    engine = create_engine(f"sqlite:///{tmp_path / 'legacy.db'}")
    try:
        build_legacy_database(engine)
        # Sanity: the broken state really is broken before the migration runs.
        with engine.connect() as connection:
            current = connection.execute(
                text("select count(*) from document_version where is_current in (1, 't', 'T')")
            ).scalar_one()
        assert current == 2, "the fixture must violate the invariant it is testing"

        status = upgrade(engine, "head")
        assert status.up_to_date, status.to_dict()
        assert current_revision(engine) == heads()[0]

        with engine.connect() as connection:
            rows = connection.execute(
                text(
                    "select id, version_number, is_current, source_relative_path, superseded_by_version_id"
                    " from document_version order by version_number"
                )
            ).all()
            pointer = connection.execute(
                text("select current_version_id from document")
            ).scalar_one()
            cached = connection.execute(
                text(
                    "select id, extraction_id, hits, produced_by_version_id, content_sha256 from extraction_cache"
                )
            ).all()

        assert [(row[0], row[1], bool(row[2])) for row in rows] == [
            ("ver-1", 1, False),
            ("ver-2", 2, True),
        ], rows
        assert pointer == "ver-2", pointer
        assert all(row[3] == "documents/mud_report.xlsx" for row in rows), (
            "backfilled from the document identity"
        )
        assert rows[0][4] == "ver-2", (
            "the supersede chain that was already there survives the rebuild"
        )

        assert len(cached) == 1, "one entry per key, whatever the number of artefact rows"
        entry_id, extraction_id, hits, produced_by, sha = cached[0]
        assert extraction_id == "ext-1", "the oldest good artefact is the first producer"
        assert len(entry_id) <= 36, (
            f"{entry_id!r} must fit the String(36) id column (PostgreSQL would reject it)"
        )
        assert hits == 0 and sha == SHA
        assert produced_by == "ver-1"

        # The checkers the repair is supposed to satisfy, run against the migrated file.
        with Session(engine) as session:
            assert check_current_version_invariants(session) == []
            assert check_extraction_cache(session) == []
            assert check_knowledge_relations(session) == []
            # ...and the data is still there afterwards (the table rebuild copied it).
            row = (
                session.execute(
                    text("select identity_path, filename, classification, size_bytes from document")
                )
                .mappings()
                .one()
            )
            assert dict(row) == {
                "identity_path": "documents/mud_report.xlsx",
                "filename": "mud_report.xlsx",
                "classification": "MUD_REPORT",
                "size_bytes": 20480,
            }
            assert schema_diff(engine) == {
                "missing_tables": [],
                "extra_tables": [],
                "missing_columns": [],
                "extra_columns": [],
            }, "the migrated schema must match the models exactly"
    finally:
        engine.dispose()


def test_the_migrated_database_refuses_a_second_current_version(tmp_path) -> None:
    engine = create_engine(f"sqlite:///{tmp_path / 'guard.db'}")
    insert = text(
        "insert into document_version (id, document_id, version_number, revision_key, status, source_path, sha256,"
        " size_bytes, mime_type, parser, parser_version, extraction_version, origin, is_current, source_relative_path,"
        " created_at, updated_at) values ('ver-3', 'doc-1', 3, 'rev-14', 'DRAFT', 'documents/x.xlsx', :sha, 1, '',"
        " 'excel', '1', '1', 'MODIFIED', :is_current, 'documents/x.xlsx', :now, :now)"
    )
    try:
        build_legacy_database(engine)
        upgrade(engine, "head")
        index_names = {index["name"] for index in inspect(engine).get_indexes("document_version")}
        assert "uq_document_version_one_current" in index_names, index_names
        # The partial unique index is what refuses, and it refuses only the second *current*.
        # SQLite names the *columns* of a unique index in its error text rather than the
        # index, so the two halves of the claim are checked separately: the index exists, and
        # a second current row for the same document trips it.
        with (
            pytest.raises(
                IntegrityError, match=r"UNIQUE constraint failed: document_version\.document_id"
            ),
            engine.begin() as connection,
        ):
            connection.execute(insert, {"sha": SHA, "is_current": 1, "now": NOW})
        with engine.begin() as connection:
            connection.execute(insert, {"sha": SHA, "is_current": 0, "now": NOW})
            rows = connection.execute(
                text("select id, is_current from document_version order by id")
            ).all()
        assert [(row[0], bool(row[1])) for row in rows] == [
            ("ver-1", False),
            ("ver-2", True),
            ("ver-3", False),
        ], rows
    finally:
        engine.dispose()


def test_the_foreign_key_on_the_pointer_is_real_after_the_rebuild(tmp_path) -> None:
    """``current_version_id`` must be a foreign key, not a hopeful string (P0-3)."""
    engine = create_engine(f"sqlite:///{tmp_path / 'fk.db'}")
    try:
        build_legacy_database(engine)
        upgrade(engine, "head")
        keys = inspect(engine).get_foreign_keys("document")
        named = {key["name"] for key in keys if key.get("name")}
        assert "fk_document_current_version_id_document_version" in named, named
        target = next((key for key in keys if key["referred_table"] == "document_version"), None)
        assert target is not None and target["constrained_columns"] == ["current_version_id"], (
            target
        )
        assert target["options"].get("ondelete") == "SET NULL", target["options"]

        # Deleting the version the registry points at must not orphan the pointer.  SQLite
        # enforces foreign keys per connection and only when the pragma is on, which is what
        # ``Database.from_url`` does for the application; a bare ``create_engine`` has to say
        # so too or this test would silently prove nothing.
        with engine.connect() as connection:
            connection.execute(text("PRAGMA foreign_keys = ON"))
            connection.execute(text("delete from document_version where id = 'ver-2'"))
            connection.commit()
            still = connection.execute(text("select current_version_id from document")).scalar_one()
        assert still is None, f"ON DELETE SET NULL, so the pointer never dangles (got {still!r})"
        assert schema_diff(engine) == {
            "missing_tables": [],
            "extra_tables": [],
            "missing_columns": [],
            "extra_columns": [],
        }
    finally:
        engine.dispose()


def test_migrating_back_to_0001_keeps_the_data(tmp_path) -> None:
    """The downgrade rebuilds ``document`` too; a repair tool must be able to roll back.

    Dropping the new columns and the constraints is expected - losing rows or leaving the
    remaining columns empty is not, and ``copy_from`` is exactly the place where such a
    mistake would happen silently.
    """
    engine = create_engine(f"sqlite:///{tmp_path / 'roundtrip.db'}")
    try:
        build_legacy_database(engine)
        upgrade(engine, "head")
        with engine.connect() as connection:
            before = connection.execute(
                text("select id, identity_path, filename, change_count from document")
            ).all()
        # Rolling back is a deliberate act, so it has to be asked for; and the status says
        # so instead of reporting that nothing needed doing.
        refused = upgrade(engine, FIRST_REVISION)
        # "current" is whatever head the database sits on - read from the database rather than
        # hardcoded, because this file is about the 0001 -> 0002 repair, and a third migration
        # must not make it lie about the version it refused to roll back from.
        assert refused.mode == "downgrade-required" and refused.current == current_revision(
            engine
        ), refused.to_dict()
        assert "allow_downgrade" in refused.detail, refused.detail
        status = upgrade(engine, FIRST_REVISION, allow_downgrade=True)
        assert status.mode == "downgraded" and not status.up_to_date, status.to_dict()
        assert current_revision(engine) == FIRST_REVISION, current_revision(engine)
        tables = legacy_tables(engine)
        columns = {column.name for column in tables.tables["document"].columns}
        versions = {column.name for column in tables.tables["document_version"].columns}
        assert "fs_metadata_changed_at" not in columns
        assert "source_relative_path" not in versions
        assert "extraction_cache" not in tables.tables
        index_names = {index["name"] for index in inspect(engine).get_indexes("document_version")}
        assert "uq_document_version_one_current" not in index_names, index_names
        with engine.connect() as connection:
            after = connection.execute(
                text("select id, identity_path, filename, change_count from document")
            ).all()
            count = connection.execute(text("select count(*) from document_version")).scalar_one()
        assert after == before, "a downgrade must not lose or rewrite rows"
        assert count == 2
    finally:
        engine.dispose()


def test_a_timestamp_only_difference_still_needs_no_migration(tmp_path) -> None:
    """``fs_metadata_changed_at`` is nullable on purpose: old rows simply do not have one."""
    engine = create_engine(f"sqlite:///{tmp_path / 'timestamps.db'}")
    try:
        build_legacy_database(engine)
        upgrade(engine, "head")
        with engine.connect() as connection:
            value = connection.execute(
                text("select fs_metadata_changed_at from document")
            ).scalar_one()
        assert value is None, "the repair must not invent a filesystem timestamp it never measured"
    finally:
        engine.dispose()


def test_the_migration_does_not_touch_rows_it_should_not(tmp_path) -> None:
    """Idempotence: running the pipeline's own writes afterwards still validates."""
    engine = create_engine(f"sqlite:///{tmp_path / 'again.db'}")
    try:
        build_legacy_database(engine)
        upgrade(engine, "head")
        before = current_revision(engine)
        status = upgrade(engine, "head")
        assert status.up_to_date and current_revision(engine) == before, status.to_dict()
        with engine.connect() as connection:
            count = connection.execute(text("select count(*) from extraction_cache")).scalar_one()
        assert count == 1, "re-running the head migration must not double the cache entries"
    finally:
        engine.dispose()
