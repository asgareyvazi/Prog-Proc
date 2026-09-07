"""0004 → 0005: the engineering record gets the evidence columns it arrived without.

``calculation`` has been in the schema since 0001 - method, version, inputs, outputs, validation, an
uncertainty block, a revision chain - and it is the right shape for an engineering number.  What it never
had is the convention every other row in this platform carries: ``origin``, ``created_by``, an identity key
and a citation of the document version a value was read out of.  Without those, a write path can only
append, and "who says" has no column to live in.

This migration adds six columns and a unique index, and does four things a reviewer should be able to check
without reading the file:

*   every pre-existing row comes out ``MANUAL``/``system`` - a calculation whose parentage is unknown is
    treated as something a person put there, which is the one kind a rebuild never deletes (0003 made the
    same choice for ``knowledge_item``);
*   no source is invented for it: ``document_id``, ``document_version_id`` and ``identity_key`` stay NULL,
    because a migration that guesses a citation is worse than one that admits it does not know;
*   the content-identity index is unique but NULL-friendly, so hand-entered one-offs with no identity do not
    block each other while two rows claiming the same identity cannot coexist;
*   and the round trip is exact, with the rows and the four foreign keys that were there before 0005
    surviving the downgrade.
"""

from __future__ import annotations

import os
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path

import pytest
from sqlalchemy import MetaData, create_engine, func, inspect, select, text
from sqlalchemy.engine import Engine
from sqlalchemy.exc import IntegrityError

from drilling_intelligence.database.migrations import heads, schema_diff, upgrade

ROOT = Path(__file__).resolve().parents[2]
NOW = datetime(2026, 1, 1, tzinfo=UTC)

#: The columns 0005 adds, in the order the migration declares them.
ADDED_COLUMNS = (
    "origin",
    "created_by",
    "identity_key",
    "document_id",
    "document_version_id",
    "attributes",
)
ADDED_INDEX = "uq_calculation_identity"
ADDED_FOREIGN_KEYS = ("fk_calculation_document_id", "fk_calculation_document_version_id")
#: The foreign keys ``calculation`` already had; a table rebuild that loses one of these is a data bug.
PRE_EXISTING_FOREIGN_KEYS = (
    "fk_calculation_well_id_well",
    "fk_calculation_section_id_well_section",
    "fk_calculation_project_id_project",
    "fk_calculation_source_id_source",
    "fk_calculation_supersedes_id_calculation",
)


def build_legacy_database(engine: Engine) -> None:
    """A 0004 schema with calculation rows in it: the state a workspace is in before this migration."""
    status = upgrade(engine, "0004")
    assert status.mode == "migrated" and status.current == "0004", status.to_dict()
    tables = MetaData()
    tables.reflect(bind=engine)
    columns = {column.name for column in tables.tables["calculation"].columns}
    assert "origin" not in columns and "identity_key" not in columns, (
        "the point of the test is that these columns are missing"
    )


def add_calculation(engine: Engine, key: str, *, identity: str | None = None) -> None:
    """One legacy row, written in 0004's vocabulary: no origin, no author, no citation.

    The insert names every column the old table required and nothing else, so if 0004's ``calculation`` ever
    grows a required column this helper fails rather than quietly testing a different table.
    """
    with engine.begin() as connection:
        connection.execute(
            text(
                "insert into calculation (id, method_id, method_version, calculation_type, record_state,"
                " inputs, assumptions, provenance, status, revision, triggered_by, created_at, updated_at)"
                " values (:id, 'hydraulics-v1', '3', 'annular_pressure', 'CURRENT', '{}', '[]', '[]',"
                " 'COMPUTED', 1, 'cli', :now, :now)"
            ),
            {"id": key, "now": NOW},
        )


def names(engine: Engine, method: str, relation: str) -> set[str]:
    getter = getattr(inspect(engine), method)
    return {item["name"] for item in getter(relation) if item.get("name")}


def snapshot(engine: Engine) -> list[tuple]:
    """Everything 0004 stored about a calculation, so "nothing was lost" is a comparison."""
    with engine.connect() as connection:
        return list(
            connection.execute(
                text(
                    "select id, method_id, method_version, calculation_type, record_state, inputs,"
                    " outputs, assumptions, validation, provenance, status, revision, triggered_by"
                    " from calculation order by id"
                )
            ).all()
        )


def test_the_upgrade_adds_the_columns_and_leaves_the_rows_alone(tmp_path) -> None:
    engine = create_engine(f"sqlite:///{tmp_path / 'legacy.db'}")
    try:
        build_legacy_database(engine)
        add_calculation(engine, "calc-legacy-1")
        add_calculation(engine, "calc-legacy-2")
        before = snapshot(engine)

        status = upgrade(engine, "0005")
        assert status.mode == "migrated" and status.current == "0005", status.to_dict()

        columns = {c["name"] for c in inspect(engine).get_columns("calculation")}
        assert set(ADDED_COLUMNS) <= columns, sorted(set(ADDED_COLUMNS) - columns)
        assert snapshot(engine) == before, "the pre-existing columns must be byte-identical"
        with engine.connect() as connection:
            rows = (
                connection.execute(
                    text(
                        "select id, origin, created_by, identity_key, document_id, document_version_id,"
                        " attributes from calculation order by id"
                    )
                )
                .mappings()
                .all()
            )
        assert [row["origin"] for row in rows] == ["MANUAL", "MANUAL"], (
            "an unattributed row is treated as something a person wrote - the one kind a rebuild leaves"
        )
        assert [row["created_by"] for row in rows] == ["system", "system"]
        assert all(row["identity_key"] is None for row in rows), (
            "a migration does not mint a content identity it cannot derive from what the row claimed"
        )
        assert all(
            row["document_id"] is None and row["document_version_id"] is None for row in rows
        ), "no citation is invented for a row that never had one"
        assert schema_diff(engine) == {
            "missing_tables": [],
            "extra_tables": [],
            "missing_columns": [],
            "extra_columns": [],
        }, "the migrated schema must match the models exactly"
    finally:
        engine.dispose()


def test_the_identity_index_is_unique_and_null_friendly(tmp_path) -> None:
    """Two rows may not claim one identity, and rows that claim none must not collide.

    The whole idempotence story for the engineering record rests on this index: a batch that runs twice has
    to find the row it wrote rather than append a twin.  And a hand-entered one-off has no content identity
    to claim, so a unique index that treated NULLs as equal would make the second one an error.
    """
    engine = create_engine(f"sqlite:///{tmp_path / 'identity.db'}")
    try:
        build_legacy_database(engine)
        upgrade(engine, "0005")
        indexes = {index["name"]: index for index in inspect(engine).get_indexes("calculation")}
        assert ADDED_INDEX in indexes, sorted(indexes)
        assert bool(indexes[ADDED_INDEX]["unique"]), indexes[ADDED_INDEX]

        insert = text(
            "insert into calculation (id, method_id, method_version, calculation_type, record_state,"
            " inputs, assumptions, provenance, status, revision, triggered_by, created_at, updated_at,"
            " identity_key) values (:id, 'm', '', 't', 'CURRENT', '{}', '[]', '[]', 'COMPUTED', 1, 'cli',"
            " :now, :now, :k)"
        )
        with engine.begin() as connection:
            connection.execute(insert, {"id": "a", "k": "same", "now": NOW})
        with pytest.raises(IntegrityError), engine.begin() as connection:
            connection.execute(insert, {"id": "b", "k": "same", "now": NOW})

        with engine.begin() as connection:
            connection.execute(insert, {"id": "c", "k": None, "now": NOW})
            connection.execute(insert, {"id": "d", "k": None, "now": NOW})
        with engine.connect() as connection:
            total = connection.execute(
                select(func.count()).select_from(text("calculation"))
            ).scalar_one()
        assert total == 3, "two rows with no identity blocked each other"
    finally:
        engine.dispose()


def test_the_migrated_calculation_table_is_the_one_the_models_describe(tmp_path) -> None:
    """No FK on the two new citation columns - and that has to be true on *both* kinds of workspace.

    The exception this migration makes (see its docstring) is only safe if a fresh `create_all` file and an
    upgraded file end up identical; otherwise the same dangling citation is an insert error in one workspace
    and a silent row in the next.  So the comparison is not a formality: it is the test that keeps the
    exception honest, and the one that fails first if someone adds the constraint to the models and not to
    the migration.
    """
    from drilling_intelligence.database.models import Base

    migrated = create_engine(f"sqlite:///{tmp_path / 'parity-migrated.db'}")
    try:
        build_legacy_database(migrated)
        upgrade(migrated, "0005")
    finally:
        migrated.dispose()
    from_models = create_engine(f"sqlite:///{tmp_path / 'parity-models.db'}")
    try:
        Base.metadata.create_all(from_models, tables=[Base.metadata.tables["calculation"]])
        for getter in (
            "get_indexes",
            "get_unique_constraints",
            "get_foreign_keys",
            "get_check_constraints",
        ):
            assert names(migrated, getter, "calculation") == names(
                from_models, getter, "calculation"
            ), f"calculation: {getter} differs between the migrated schema and the models"
        assert {c["name"] for c in inspect(migrated).get_columns("calculation")} == {
            c["name"] for c in inspect(from_models).get_columns("calculation")
        }
        foreign_keys = {item["name"] for item in inspect(migrated).get_foreign_keys("calculation")}
        assert set(PRE_EXISTING_FOREIGN_KEYS) <= foreign_keys, sorted(
            set(PRE_EXISTING_FOREIGN_KEYS) - foreign_keys
        )
        indexes = {index["name"] for index in inspect(migrated).get_indexes("calculation")}
        assert ADDED_INDEX in indexes and "ix_calculation_well" in indexes, sorted(indexes)
    finally:
        from_models.dispose()
        migrated.dispose()


def test_a_dangling_citation_is_reported_on_a_migrated_file(tmp_path) -> None:
    """Where the constraint is not, the checker is - which is what the exception above trades on.

    A calculation whose evidence cannot be opened is a number with no source, and `doctor` is where that has
    to surface.  Two shapes of it, both written in SQL the way a corruption arrives: a derived row that
    cites nothing, and a row that cites a document version which does not exist.  The second is the one the
    missing foreign key would otherwise have caught at insert time, so this test is the price of the
    exception being acceptable rather than merely noted.
    """
    from sqlalchemy.orm import Session

    from drilling_intelligence.database.integrity import check_operational_integrity

    engine = create_engine(f"sqlite:///{tmp_path / 'dangling.db'}")
    try:
        build_legacy_database(engine)
        add_calculation(engine, "calc-no-evidence")
        add_calculation(engine, "calc-bad-version")
        upgrade(engine, "0005")
        with engine.begin() as connection:
            connection.execute(
                text("update calculation set origin = 'DERIVED' where id = 'calc-no-evidence'")
            )
            connection.execute(
                text(
                    "update calculation set origin = 'DERIVED',"
                    ' provenance = \'[{"ref": "Summary!B9"}]\','
                    " document_version_id = 'no-such-version' where id = 'calc-bad-version'"
                )
            )
        # A real Session, not a bare Connection: the checker walks ORM entities, and Core would hand it
        # the first column of each row instead - which would be a bug in the test, not in the product.
        with Session(engine) as session:
            problems = [item.to_dict() for item in check_operational_integrity(session)]
        by_row = {
            item["row_id"]: item["problem"] for item in problems if item["table"] == "calculation"
        }
        assert by_row.get("calc-no-evidence") == (
            "is derived from a document and cites no evidence"
        ), problems
        assert by_row.get("calc-bad-version") == ("cites a document version that does not exist"), (
            problems
        )
    finally:
        engine.dispose()


def test_the_downgrade_is_the_exact_inverse(tmp_path) -> None:
    """Rolling back leaves the table as 0004 built it - rows, foreign keys and all - and the way up works."""
    engine = create_engine(f"sqlite:///{tmp_path / 'reverse.db'}")
    try:
        build_legacy_database(engine)
        add_calculation(engine, "calc-1")
        upgrade(engine, "0005")
        add_calculation(engine, "calc-2")
        before = snapshot(engine)

        status = upgrade(engine, "0004", allow_downgrade=True)
        assert status.mode == "downgraded" and status.current == "0004", status.to_dict()
        columns = {c["name"] for c in inspect(engine).get_columns("calculation")}
        assert not set(ADDED_COLUMNS) & columns, sorted(set(ADDED_COLUMNS) & columns)
        assert ADDED_INDEX not in {i["name"] for i in inspect(engine).get_indexes("calculation")}
        foreign_keys = {item["name"] for item in inspect(engine).get_foreign_keys("calculation")}
        assert not set(ADDED_FOREIGN_KEYS) & foreign_keys, sorted(
            set(ADDED_FOREIGN_KEYS) & foreign_keys
        )
        assert set(PRE_EXISTING_FOREIGN_KEYS) <= foreign_keys, (
            "the downgrade must not remove constraints it did not add"
        )
        assert snapshot(engine) == before, "downgrading the schema must not downgrade the data"
        diff = schema_diff(engine)
        assert sorted(diff["missing_columns"]) == sorted(
            f"calculation.{column}" for column in ADDED_COLUMNS
        ), diff

        again = upgrade(engine, "0005")
        assert again.mode == "migrated" and again.current == "0005", again.to_dict()
        assert snapshot(engine) == before
        assert schema_diff(engine) == {
            "missing_tables": [],
            "extra_tables": [],
            "missing_columns": [],
            "extra_columns": [],
        }
    finally:
        engine.dispose()


def test_offline_sql_for_this_migration_carries_the_defaults(tmp_path) -> None:
    """``--sql`` has to render this migration, because that is how a DBA reviews it before running it.

    Six ``NOT NULL``-with-default columns on a populated table is the case SQLite is fussy about: without the
    server default the statements cannot run at all.  Seeing them in the generated script is the cheap proof
    that the migration will not fail halfway on someone's workspace, and seeing the unique index is the
    proof the idempotence rule is in the SQL rather than only in the repository.
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
            "0004:0005",
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
    assert "ALTER TABLE calculation ADD COLUMN origin" in script, script[:600]
    assert "'MANUAL'" in script and "'system'" in script, (
        "the NOT NULL columns need server defaults in the generated SQL too"
    )
    assert "CREATE UNIQUE INDEX uq_calculation_identity" in script

    down = subprocess.run(
        [
            sys.executable,
            "-m",
            "alembic",
            "-c",
            str(ROOT / "alembic.ini"),
            "downgrade",
            "0005:0004",
            "--sql",
        ],
        cwd=str(ROOT),
        capture_output=True,
        text=True,
        env=env,
        check=False,
    )
    assert down.returncode == 0, down.stderr[-800:]
    assert "DROP INDEX uq_calculation_identity" in down.stdout
    assert "DROP COLUMN origin" in down.stdout or "origin" in down.stdout, down.stdout[:600]


def test_head_still_matches_the_models_and_the_knowledge_layer_is_intact(tmp_path) -> None:
    """The revision this file pins is not the head any more, and that has to stay true.

    Two checks in one: the workspace ends up at ``heads()[0]`` with an empty ``schema_diff`` - so 0005 did
    not leave a column the models expect uncreated - and the tables 0004 and 0003 built come through the
    round trip untouched, which is what "no loss of Knowledge facts" has to mean for a migration this far
    along the chain.
    """
    engine = create_engine(f"sqlite:///{tmp_path / 'head.db'}")
    try:
        build_legacy_database(engine)
        add_calculation(engine, "calc-1")
        with engine.begin() as connection:
            connection.execute(
                text(
                    "insert into knowledge_item (id, item_type, title, content, domain, lookup_key,"
                    " value, unit, record_state, status, revision, origin, value_type, original_value,"
                    " original_unit, normalized_unit, created_by, created_at, updated_at)"
                    " values ('ki-1','FACT','t','c','mud','','',  '', 'ACTUAL', 'ACTIVE', 1, 'MANUAL',"
                    " 'text', '', '', '', 'someone', :n, :n)"
                ),
                {"n": NOW},
            )
        status = upgrade(engine, "head")
        assert status.up_to_date and status.current == heads()[0], status.to_dict()
        assert heads() == ["0006"], heads()
        assert schema_diff(engine) == {
            "missing_tables": [],
            "extra_tables": [],
            "missing_columns": [],
            "extra_columns": [],
        }
        with engine.connect() as connection:
            assert connection.execute(
                text("select origin, status from knowledge_item where id = 'ki-1'")
            ).one() == ("MANUAL", "ACTIVE")
            assert connection.execute(text("select count(*) from npt_record")).scalar_one() == 0
            assert (
                connection.execute(
                    text("select count(*) from calculation where origin = 'MANUAL'")
                ).scalar_one()
                == 1
            )
    finally:
        engine.dispose()
