"""0010 → 0011: the BHA, bit record and directional survey domains.

The five tables are the persistence half of the V4 contracts, so the migration is checked the way a
schema change has to be: a workspace upgraded in place gets the tables, a workspace built fresh gets the
same tables, the downgrade takes exactly those tables away and leaves every other row alone, and the
upgrade can be replayed.  A migration that only passes ``create_all`` is a migration that has never been
applied to somebody's file.
"""

from __future__ import annotations

from pathlib import Path

from sqlalchemy import create_engine, inspect, text
from sqlalchemy.engine import Engine

from drilling_intelligence.database.migrations import (
    METADATA_REVISION,
    heads,
    schema_diff,
    upgrade,
)

ROOT = Path(__file__).resolve().parents[2]
PREVIOUS_HEAD = "0010"
NEW_TABLES = ("bha_report", "bha_component", "bit_record", "survey_run", "survey_station")


def build_legacy_database(engine: Engine) -> None:
    """A 0010 schema, which is where a workspace is before this migration."""
    status = upgrade(engine, PREVIOUS_HEAD)
    assert status.mode == "migrated" and status.current == PREVIOUS_HEAD, status.to_dict()
    tables = set(inspect(engine).get_table_names())
    assert not set(NEW_TABLES) & tables, sorted(set(NEW_TABLES) & tables)


def table_set(engine: Engine) -> set[str]:
    return set(inspect(engine).get_table_names())


def columns(engine: Engine, table: str) -> set[str]:
    return {column["name"] for column in inspect(engine).get_columns(table)}


def index_names(engine: Engine, table: str) -> set[str]:
    return {index["name"] for index in inspect(engine).get_indexes(table)}


def unique_names(engine: Engine, table: str) -> set[str]:
    return {c["name"] for c in inspect(engine).get_unique_constraints(table)} | index_names(
        engine, table
    )


def snapshot(engine: Engine) -> dict[str, list[tuple]]:
    """Every row of every table the migration must not disturb, in a stable order."""
    with engine.connect() as connection:
        return {
            table: connection.execute(
                text(f'select rowid, * from "{table}" order by rowid')  # noqa: S608
            ).fetchall()
            for table in sorted(table_set(engine))
            if table not in NEW_TABLES and table != "alembic_version"
        }


def test_the_revision_is_a_real_head_of_a_single_headed_chain() -> None:
    assert METADATA_REVISION == "0011"
    assert heads() == [METADATA_REVISION], heads()


def test_the_upgrade_creates_the_five_tables_with_the_columns_the_models_declare(tmp_path) -> None:
    from drilling_intelligence.database.models import Base

    engine = create_engine(f"sqlite:///{tmp_path / 'legacy.db'}")
    try:
        build_legacy_database(engine)
        status = upgrade(engine, "0011")
        assert status.mode == "migrated" and status.current == "0011", status.to_dict()
        assert set(NEW_TABLES) <= table_set(engine)

        for table in NEW_TABLES:
            assert columns(engine, table) == {
                column.name for column in Base.metadata.tables[table].columns
            }, table
        # And nothing else in the schema moved: a fresh ``create_all`` and this file agree exactly.
        assert schema_diff(engine) == {
            "missing_tables": [],
            "extra_tables": [],
            "missing_columns": [],
            "extra_columns": [],
        }
    finally:
        engine.dispose()


def test_identity_uniqueness_and_the_indexes_the_queries_need(tmp_path) -> None:
    engine = create_engine(f"sqlite:///{tmp_path / 'legacy.db'}")
    try:
        build_legacy_database(engine)
        upgrade(engine, "0011")
        for table in NEW_TABLES:
            uniques = unique_names(engine, table)
            assert f"uq_{table}_identity" in uniques, (table, sorted(uniques))
        for table, expected in (
            ("bha_report", {"ix_bha_report_well", "ix_bha_report_version", "ix_bha_report_number"}),
            (
                "bha_component",
                {"ix_bha_component_report", "ix_bha_component_well", "ix_bha_component_version"},
            ),
            ("bit_record", {"ix_bit_record_well", "ix_bit_record_version", "ix_bit_record_number"}),
            ("survey_run", {"ix_survey_run_well", "ix_survey_run_version"}),
            (
                "survey_station",
                {"ix_survey_station_run", "ix_survey_station_well", "ix_survey_station_version"},
            ),
        ):
            missing = expected - index_names(engine, table)
            assert not missing, (table, sorted(missing))
    finally:
        engine.dispose()


def test_the_migrated_indexes_match_the_models_column_for_column(tmp_path) -> None:
    """An index whose columns are in a different order is a different index.

    ``schema_diff`` compares table and column names, so it cannot see this: a migration that created
    ``(is_current, well_id)`` where the model declares ``(well_id, is_current)`` would pass every other
    parity check and still plan the well-scoped listing differently on a migrated file than on a fresh
    one.  This migration shipped with exactly that drift on four of the five tables, which is why the
    column order is compared here and not left to the name match above.
    """
    from drilling_intelligence.database.models import Base

    engine = create_engine(f"sqlite:///{tmp_path / 'legacy.db'}")
    try:
        build_legacy_database(engine)
        upgrade(engine, "0011")
        for table in NEW_TABLES:
            declared = {
                index.name: [column.name for column in index.columns]
                for index in Base.metadata.tables[table].indexes
            }
            migrated = {
                index["name"]: list(index["column_names"])
                for index in inspect(engine).get_indexes(table)
            }
            assert declared == migrated, (table, declared, migrated)
    finally:
        engine.dispose()


def test_a_promoted_corpus_survives_the_upgrade_with_every_row_and_value_intact(workspace) -> None:
    """The case that matters in production: real promoted engineering data, then a new migration."""
    from tests.fixtures.fieldops import fetch, ingest, promote

    from drilling_intelligence.database.models import MudMeasurement, MudReport, NptRecord

    status = upgrade(workspace.database.engine, PREVIOUS_HEAD, allow_downgrade=True)
    assert status.mode == "downgraded" and status.current == PREVIOUS_HEAD, status.to_dict()

    ingest(workspace)
    summary = promote(workspace)
    assert summary["outcomes"]["error"] == 0, summary["outcomes"]
    mud_before = fetch(workspace, MudMeasurement)
    assert mud_before, "the pre-upgrade workspace must hold real promoted engineering rows"
    npt_before = len(fetch(workspace, NptRecord))
    assert not set(NEW_TABLES) & table_set(workspace.database.engine)

    before = snapshot(workspace.database.engine)
    status = upgrade(workspace.database.engine, "0011")
    assert status.mode == "migrated" and status.current == "0011", status.to_dict()
    assert snapshot(workspace.database.engine) == before, "the upgrade must not touch existing data"

    mud_after = fetch(workspace, MudMeasurement)
    assert [(row.identity_key, row.value) for row in mud_after] == [
        (row.identity_key, row.value) for row in mud_before
    ], "a promoted mud report must be identical across the migration"
    assert len(fetch(workspace, NptRecord)) == npt_before
    with workspace.database.read_only() as session:
        assert len(list(session.scalars(MudReport.__table__.select()))) == 1
        from drilling_intelligence.database.integrity import check_operational_integrity

        assert check_operational_integrity(session) == []


def test_the_downgrade_removes_exactly_the_new_tables_and_keeps_every_other_row(workspace) -> None:
    """Rolling 0011 back loses the hardware history - loudly, and only that.

    The downgrade drops five tables that hold promoted engineering records, so it is not reversible
    without re-promoting.  What it must not do is take anything else with it: a rollback that also
    disturbed the mud report, the NPT records or the documents would be an outage, not a rollback.
    """
    from tests.fixtures.fieldops import promote, register_wells, well_id_for
    from tests.fixtures.generate import build_v4_forensic_corpus

    from drilling_intelligence.ingestion.pipeline import IngestionPipeline

    register_wells(workspace)
    corpus = workspace.root / "corpus"
    build_v4_forensic_corpus(corpus)
    result = IngestionPipeline(
        settings=workspace.settings,
        workspace_root=workspace.root,
        database=workspace.database,
    ).run(root=corpus, well_id=well_id_for(workspace, "A-3"))
    assert result.ok, result.error
    summary = promote(workspace)
    assert summary["counts"]["bha_component"]["created"] == 6, summary["counts"]
    assert summary["counts"]["bit_record"]["created"] == 2
    assert summary["counts"]["survey_station"]["created"] == 5

    engine = workspace.database.engine
    before = snapshot(engine)
    status = upgrade(engine, PREVIOUS_HEAD, allow_downgrade=True)
    assert status.mode == "downgraded" and status.current == PREVIOUS_HEAD, status.to_dict()
    assert not set(NEW_TABLES) & table_set(engine), sorted(set(NEW_TABLES) & table_set(engine))
    assert snapshot(engine) == before, "a rollback must not disturb any other table"

    again = upgrade(engine, "0011")
    assert again.mode == "migrated" and again.current == "0011", again.to_dict()
    assert set(NEW_TABLES) <= table_set(engine)
    with engine.connect() as connection:
        # The tables come back empty: a downgrade is not a backup.
        assert connection.execute(text("select count(*) from bha_report")).scalar_one() == 0
        assert connection.execute(text("select count(*) from survey_station")).scalar_one() == 0


def test_a_fresh_workspace_and_an_upgraded_one_describe_the_same_schema(tmp_path) -> None:
    """``create_all`` and the migration chain must agree, or the tests are not testing production."""
    from drilling_intelligence.database.models import Base

    fresh = create_engine(f"sqlite:///{tmp_path / 'fresh.db'}")
    migrated = create_engine(f"sqlite:///{tmp_path / 'migrated.db'}")
    try:
        Base.metadata.create_all(fresh)
        build_legacy_database(migrated)
        assert upgrade(migrated, heads()[0]).current == heads()[0]
        assert schema_diff(migrated) == {
            "missing_tables": [],
            "extra_tables": [],
            "missing_columns": [],
            "extra_columns": [],
        }
        # ``alembic_version`` is the migration bookkeeping table; ``create_all`` has no reason to have one.
        assert table_set(fresh) == table_set(migrated) - {"alembic_version"}
        for table in NEW_TABLES:
            assert columns(fresh, table) == columns(migrated, table), table
    finally:
        fresh.dispose()
        migrated.dispose()


def test_the_upgrade_is_replayable(tmp_path) -> None:
    engine = create_engine(f"sqlite:///{tmp_path / 'legacy.db'}")
    try:
        build_legacy_database(engine)
        assert upgrade(engine, "0011").mode == "migrated"
        assert upgrade(engine, "0011").up_to_date is True
        assert upgrade(engine, heads()[0]).up_to_date is True
    finally:
        engine.dispose()


def test_offline_sql_for_this_migration_is_renderable(tmp_path) -> None:
    """``--sql`` has to produce this migration: that is how a DBA reviews it before running it."""
    import os
    import subprocess
    import sys

    env = {
        **os.environ,
        "PYTHONPATH": str(ROOT / "src"),
        "DRILLING_INTEL_HOME": str(tmp_path),
        "DRILLING_INTEL_DATABASE_SQLITE_FILENAME": "offline.db",
    }
    completed = subprocess.run(
        [
            sys.executable,
            "-m",
            "alembic",
            "-c",
            "alembic.ini",
            "upgrade",
            f"{PREVIOUS_HEAD}:head",
            "--sql",
        ],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr
    for table in NEW_TABLES:
        assert f"CREATE TABLE {table}" in completed.stdout, table
    assert "DROP TABLE" not in completed.stdout
