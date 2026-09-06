"""0002 → 0003: the knowledge columns, added to a table that already has rows.

Migration 0003 widens ``knowledge_item`` - a generic "structured knowledge object" in 0001 - into
something the knowledge layer can query: subject, predicate, typed value, normalised value, the
source's own wording, a validity window and an origin.  Two properties matter and only a migration
test can prove them:

*   a workspace that already holds knowledge rows keeps them, byte for byte, and gains the new
    columns with defaults that do not invent a source - the backfill calls every pre-existing row
    ``MANUAL``, because nobody can prove an extractor wrote it, and the one thing ``knowledge
    rebuild`` must never delete is what a person typed;
*   the migrated schema is the schema the models describe, in both directions.
"""

from __future__ import annotations

import os
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path

import pytest
from sqlalchemy import MetaData, create_engine, inspect, text
from sqlalchemy.engine import Engine

from drilling_intelligence.database.migrations import (
    heads,
    schema_diff,
    upgrade,
)

ROOT = Path(__file__).resolve().parents[2]
NOW = datetime(2026, 1, 1, tzinfo=UTC)

#: The columns 0003 adds, in the order the migration declares them.
ADDED_COLUMNS = (
    "entity_type",
    "entity_id",
    "predicate",
    "value_type",
    "original_value",
    "original_unit",
    "normalized_value",
    "normalized_unit",
    "valid_from",
    "valid_to",
    "origin",
)
ADDED_INDEXES = ("ix_knowledge_subject", "ix_knowledge_predicate", "ix_knowledge_version")


def legacy_knowledge_rows() -> list[dict]:
    """Two ``knowledge_item`` rows in 0002's vocabulary - the shape an old workspace holds.

    One carries a numeric ``value`` and a citation in ``payload``; the other is prose.  Neither has
    an origin, because the column does not exist yet: that is what the backfill has to get right.
    """
    return [
        {
            "id": "ki-legacy-1",
            "item_type": "OBSERVATION",
            "title": "A-3 · Mud weight = 10.2 ppg",
            "content": "10.2 ppg",
            "domain": "mud",
            "applicability": None,
            "assumptions": {"note": "written by hand"},
            "payload": {
                "predicate": "mud_weight",
                "provenance": {"sheet": "Summary", "cell": "B9"},
            },
            "lookup_key": "well:well-1|property:mud_weight|state:ACTUAL",
            "value": 10.2,
            "unit": "ppg",
            "record_state": "ACTUAL",
            "status": "ACTIVE",
            "confidence": 0.9,
            "revision": 1,
            "well_id": "well-1",
            "section_id": None,
            "project_id": None,
            "source_id": None,
            "document_id": None,
            "document_version_id": None,
            "provenance": None,
            "evidence": None,
            "superseded_by": None,
            "created_by": "someone",
            "created_at": NOW,
            "updated_at": NOW,
        },
        {
            "id": "ki-legacy-2",
            "item_type": "LESSON",
            "title": "Pump lost primer on the third run",
            "content": "the crew re-primed between runs",
            "domain": "lessons",
            "applicability": "NORTH CORMORANT",
            "assumptions": None,
            "payload": {"text": "the crew re-primed between runs"},
            "lookup_key": "",
            "value": None,
            "unit": "",
            "record_state": "ACTUAL",
            "status": "UNVERIFIED",
            "confidence": None,
            "revision": 1,
            "well_id": None,
            "section_id": None,
            "project_id": None,
            "source_id": None,
            "document_id": None,
            "document_version_id": None,
            "provenance": None,
            "evidence": None,
            "superseded_by": None,
            "created_by": "someone",
            "created_at": NOW,
            "updated_at": NOW,
        },
    ]


def build_legacy_database(engine: Engine) -> None:
    """A 0002 schema with knowledge rows in it: the state a workspace is in before this migration."""
    status = upgrade(engine, "0002")
    assert status.mode == "migrated" and status.current == "0002", status.to_dict()
    tables = MetaData()
    tables.reflect(bind=engine)
    columns = {column.name for column in tables.tables["knowledge_item"].columns}
    assert "origin" not in columns and "predicate" not in columns, (
        "the point of the test is that these columns are missing"
    )
    with engine.begin() as connection:
        connection.execute(tables.tables["knowledge_item"].insert().values(legacy_knowledge_rows()))


def snapshot(engine: Engine) -> list[tuple]:
    """Everything 0002 already stored, so "nothing was lost" is a comparison and not a hope."""
    with engine.connect() as connection:
        return list(
            connection.execute(
                text(
                    "select id, item_type, title, content, payload, lookup_key, value, unit,"
                    " record_state, status, confidence, revision, well_id, created_by"
                    " from knowledge_item order by id"
                )
            ).all()
        )


def test_the_upgrade_adds_the_columns_it_promises_and_keeps_every_row(tmp_path) -> None:
    engine = create_engine(f"sqlite:///{tmp_path / 'knowledge.db'}")
    try:
        build_legacy_database(engine)
        before = snapshot(engine)

        # Up to *this* revision, not to head: a later migration must not be able to make the 0003 test
        # fail, and it must not quietly stop checking what 0003 itself promised.
        status = upgrade(engine, "0003")
        assert status.mode == "migrated" and status.current == "0003", status.to_dict()
        assert status.head != "0003", (
            "head has moved on, which is the whole reason this pins a revision"
        )

        columns = {column["name"] for column in inspect(engine).get_columns("knowledge_item")}
        assert set(ADDED_COLUMNS) <= columns, sorted(set(ADDED_COLUMNS) - columns)
        indexes = {index["name"] for index in inspect(engine).get_indexes("knowledge_item")}
        assert set(ADDED_INDEXES) <= indexes, sorted(set(ADDED_INDEXES) - indexes)

        assert snapshot(engine) == before, "the pre-existing columns must be byte-identical"
        with engine.connect() as connection:
            migrated = connection.execute(
                text(
                    "select id, origin, predicate, value_type, original_value, normalized_value,"
                    " normalized_unit, entity_type, entity_id, status from knowledge_item order by id"
                )
            ).mappings()
            rows = list(migrated)
        assert [row["origin"] for row in rows] == ["MANUAL", "MANUAL"], (
            "an unattributed row is treated as something a person made - the one kind a rebuild never deletes"
        )
        assert [row["predicate"] for row in rows] == [None, None], (
            "a migration does not interpret a payload; re-deriving columns from stored facts is a"
            " rebuild's job, and doing it in SQL would be a second, dumber implementation of the same rule"
        )
        assert [row["status"] for row in rows] == ["ACTIVE", "UNVERIFIED"], (
            "statuses are not rewritten"
        )
        assert [row["value_type"] for row in rows] == ["text", "text"], "the server default applies"
        assert [row["original_value"] for row in rows] == ["", ""], "no wording is invented"
        assert [row["normalized_unit"] for row in rows] == ["", ""], "no unit is invented"
        assert rows[0]["normalized_value"] is None, (
            "a normalised value is not computed during a migration; re-deriving it is a rebuild's job"
        )
        assert rows[0]["entity_type"] is None and rows[0]["entity_id"] is None, (
            "a legacy row is not silently given a subject it never claimed"
        )
        # The models-versus-schema comparison only means something at head: at 0003 the later
        # migrations' tables are legitimately absent.  Running it here after taking the database the rest
        # of the way up is what catches a 0003 column that was added by hand to the models but never put
        # into a revision.
        status = upgrade(engine, "head")
        assert status.up_to_date and status.current == heads()[0], status.to_dict()
        assert schema_diff(engine) == {
            "missing_tables": [],
            "extra_tables": [],
            "missing_columns": [],
            "extra_columns": [],
        }, "the migrated schema must match the models exactly"
        assert snapshot(engine) == before, "the later migrations must not touch what 0003 carried"
    finally:
        engine.dispose()


def test_a_migrated_database_is_usable_by_the_knowledge_layer(tmp_path) -> None:
    """The point of the new columns is that the write path fills them - on an *upgraded* file.

    A freshly created database proves the models; a migrated one proves that the migration produced
    the same table, with types and defaults the repository can live with.  This writes a manual note
    and a derived fact through the real repository and reads both back.
    """
    from drilling_intelligence.database.session import Database
    from drilling_intelligence.knowledge.entities import EntityRef
    from drilling_intelligence.knowledge.facts import KnowledgeFact
    from drilling_intelligence.knowledge.repository import KnowledgeRepository
    from drilling_intelligence.wells.repository import WellRepository

    path = tmp_path / "usable.db"
    engine = create_engine(f"sqlite:///{path}")
    try:
        build_legacy_database(engine)
        upgrade(engine, "0003")
    finally:
        engine.dispose()

    database = Database.from_url(f"sqlite:///{path}")
    try:
        with database.session() as session:
            wells = WellRepository(session)
            wells.get_or_create_workspace(str(tmp_path), name="Migrated")
            project = wells.get_or_create_project("Migrated")
            well = wells.create_well("A-3", project_id=project.id)
            repository = KnowledgeRepository(session)
            session.flush()
            subject = EntityRef("well", str(well.id), label="A-3")
            note = KnowledgeFact(
                subject=subject,
                predicate="mud_weight",
                value_type="quantity",
                original_value="10.9",
                original_unit="ppg",
                value=10.9,
                unit="ppg",
                normalized_value=10.9,
                normalized_unit="ppg",
                text="10.9 ppg",
                status="ACTIVE",
                valid_from=datetime(2026, 1, 2, tzinfo=UTC),
                note="read off the gauge the same evening",
            )
            row = repository.manual_fact(note)
            assert row.entity_type == "well"
            assert row.entity_id == str(well.id)
            assert row.predicate == "mud_weight"
            assert row.value_type == "quantity"
            assert row.original_value == "10.9" and row.original_unit == "ppg"
            assert row.normalized_unit == "ppg"
            assert row.origin == "MANUAL"
            assert row.valid_from is not None, "the validity window has a column to live in now"
            session.commit()

            [fact] = repository.facts_for_well(str(well.id))
            assert fact.item_id == row.id
            assert fact.status_reason() == "", "an ACTIVE fact has nothing to apologise for"
            # The legacy row is still there and still readable by the same queries.
            legacy = session.execute(
                text(
                    "select title, origin, value_type from knowledge_item where id = 'ki-legacy-1'"
                )
            ).one()
            assert legacy[1] == "MANUAL"
            assert legacy[0].startswith("A-3 · Mud weight"), "an old title is not rewritten"
    finally:
        database.dispose()


def test_the_downgrade_is_the_exact_inverse(tmp_path) -> None:
    """Rolling back must leave the table as 0002 built it, with the rows still in it.

    The indexes are dropped before the columns because SQLite refuses to drop a column an index
    names - so this test is also the check that the migration's own ordering is the one it claims.
    """
    engine = create_engine(f"sqlite:///{tmp_path / 'reverse.db'}")
    try:
        build_legacy_database(engine)
        upgrade(engine, "0003")
        before = snapshot(engine)

        status = upgrade(engine, "0002", allow_downgrade=True)
        assert status.mode == "downgraded" and status.current == "0002", status.to_dict()

        columns = {column["name"] for column in inspect(engine).get_columns("knowledge_item")}
        assert not set(ADDED_COLUMNS) & columns, sorted(set(ADDED_COLUMNS) & columns)
        indexes = {index["name"] for index in inspect(engine).get_indexes("knowledge_item")}
        assert not set(ADDED_INDEXES) & indexes, sorted(set(ADDED_INDEXES) & indexes)
        assert "ix_knowledge_lookup" in indexes, "0002's own indexes must survive"
        assert snapshot(engine) == before, "downgrading the schema must not downgrade the data"
        diff = schema_diff(engine)
        assert sorted(diff["missing_columns"]) == sorted(
            f"knowledge_item.{column}" for column in ADDED_COLUMNS
        ), diff
        # And the repair tool can run it forward again: same rows, same columns.
        again = upgrade(engine, "0003")
        assert again.mode == "migrated" and again.current == "0003", again.to_dict()
        assert snapshot(engine) == before
        assert not [
            item
            for item in schema_diff(engine)["missing_columns"]
            if item.startswith("knowledge_item.")
        ], schema_diff(engine)
    finally:
        engine.dispose()


def test_offline_sql_for_this_migration_carries_the_defaults(tmp_path) -> None:
    """``--sql`` has to render this migration, because that is how a DBA reviews it before running it.

    A ``NOT NULL`` column added to a populated table is the case SQLite is fussy about: the
    statement needs a server default or it cannot run at all.  Seeing the defaults in the generated
    script is the cheap proof that the migration will not fail halfway on someone's workspace.
    """
    env = dict(os.environ, DRILLINTEL_DATABASE__URL=f"sqlite:///{tmp_path / 'offline.db'}")
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "alembic",
            "-c",
            str(ROOT / "alembic.ini"),
            "upgrade",
            "0002:0003",
            "--sql",
        ],
        cwd=str(ROOT),
        capture_output=True,
        text=True,
        env=env,
        check=False,
    )
    assert result.returncode == 0, result.stderr[-1200:]
    script = result.stdout
    assert "ALTER TABLE knowledge_item ADD COLUMN entity_type" in script, script[:800]
    assert "CREATE INDEX ix_knowledge_predicate" in script
    assert "'text'" in script and "'MANUAL'" in script, (
        "the NOT NULL columns need server defaults in the generated SQL too"
    )

    down = subprocess.run(
        [
            sys.executable,
            "-m",
            "alembic",
            "-c",
            str(ROOT / "alembic.ini"),
            "downgrade",
            "0003:0002",
            "--sql",
        ],
        cwd=str(ROOT),
        capture_output=True,
        text=True,
        env=env,
        check=False,
    )
    assert down.returncode == 0, down.stderr[-800:]
    assert "DROP INDEX ix_knowledge_subject" in down.stdout
    assert "DROP COLUMN entity_type" in down.stdout or "entity_type" in down.stdout, down.stdout[
        :800
    ]


@pytest.mark.parametrize("column", ADDED_COLUMNS)
def test_every_added_column_is_declared_by_the_models_too(tmp_path, column: str) -> None:
    """No column may exist on only one side of the migration.

    A column the migration adds and the models do not know about is invisible to every query; one
    the models expect and the migration forgets is an ``OperationalError`` for whoever upgrades
    rather than creating.  ``schema_diff`` is the same comparison, run here one column at a time so
    a failure names the column instead of a dict.
    """
    from drilling_intelligence.database.models import KnowledgeItem

    assert column in KnowledgeItem.__table__.columns, f"{column} is not on the model"

    engine = create_engine(f"sqlite:///{tmp_path / 'columns.db'}")
    try:
        build_legacy_database(engine)
        upgrade(engine, "0003")
        assert column in {c["name"] for c in inspect(engine).get_columns("knowledge_item")}
    finally:
        engine.dispose()
