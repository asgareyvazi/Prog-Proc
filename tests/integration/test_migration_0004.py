"""0003 → 0004: the engineering domain's sixteen tables, on a file that already holds work.

``tests/integration/test_migration_0003.py`` covers the same ground one revision back.  What this file
insists on is the properties only *this* migration can break:

*   the migrated schema is the schema the models describe - table for table, index for index, unique
    constraint for unique constraint, foreign key for foreign key.  An index that exists in the models and
    not in the revision is invisible to ``schema_diff``, which compares columns; it shows up as a field
    engineer waiting four seconds for a well query over 40,000 NPT rows;
*   a workspace that already has documents, wells and knowledge is untouched by the upgrade - byte for
    byte, and again after the downgrade and the way back up;
*   the constraints are real on a migrated file, not only on a freshly created one: the CHECK that keeps a
    5×5 score inside 1..5 and the partial unique that keeps one revision of a code current are both
    exercised here, because a repository's validation is not the database's;
*   and the deviation is on the record: ``ddr_report.report_date`` stays nullable, which is exactly what the
    brief asked not to allow and exactly what a report that writes "14 June 2025" in prose requires.
"""

from __future__ import annotations

import os
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path

import pytest
from sqlalchemy import MetaData, column, create_engine, func, inspect, select
from sqlalchemy import table as sqltable
from sqlalchemy.engine import Engine
from sqlalchemy.exc import IntegrityError

from drilling_intelligence.core.enums import RecordState
from drilling_intelligence.core.ids import new_id
from drilling_intelligence.database.migrations import heads, schema_diff, upgrade

ROOT = Path(__file__).resolve().parents[2]
NOW = datetime(2026, 1, 1, tzinfo=UTC)

#: The tables 0004 adds, in the order the migration declares them.
NEW_TABLES = (
    "ddr_report",
    "well_operation",
    "well_event",
    "npt_record",
    "problem_occurrence",
    "procedure_record",
    "drilling_program",
    "program_target",
    "risk_record",
    "lesson_learned",
    "best_practice",
    "recommendation",
    "field_pattern",
    "rig",
    "service_company",
    "cost_item",
)

#: Indexes whose absence a column comparison cannot see and a query feels immediately.
IMPORTANT_INDEXES = (
    "ix_npt_well_start",
    "ix_npt_category",
    "ix_problem_well",
    "ix_problem_type_time",
    "ix_ddr_report_well_date",
    "ix_event_well_time",
    "ix_operation_well_start",
    "ix_pattern_field",
    "ix_lesson_scope",
    "ix_cost_well_category",
)
#: Business uniqueness, stated by name so a typo in the migration is a failing test and not a duplicate row.
IMPORTANT_UNIQUES = (
    "uq_ddr_report_version_well",
    "uq_npt_identity",
    "uq_procedure_code_revision",
    "uq_program_code_revision",
    "uq_risk_code_revision",
    "uq_lesson_code_revision",
    "uq_recommendation_signature",
    "uq_pattern_signature",
    "uq_rig_name",
    "uq_service_company_name",
)


def build_legacy_database(engine: Engine) -> None:
    """A 0003 schema, before the domain exists at all."""
    status = upgrade(engine, "0003")
    assert status.mode == "migrated" and status.current == "0003", status.to_dict()
    tables = MetaData()
    tables.reflect(bind=engine)
    assert "npt_record" not in tables.tables and "ddr_report" not in tables.tables, (
        "the point of the test is that the domain tables do not exist yet"
    )


def seed_legacy_data(url: str) -> None:
    """A hierarchy and two knowledge facts, written by the code that owns them.

    Going through the repositories rather than inserting literal rows means this is a workspace in the
    sense the application means: a project, a field, a well, a hole section with a planned and an actual
    depth, and facts about it.  If 0004 damaged a pre-existing table - repointed a foreign key, dropped an
    index, rewrote a default - it shows up here.
    """
    from drilling_intelligence.database.integrity import create_knowledge_relation
    from drilling_intelligence.database.session import Database
    from drilling_intelligence.knowledge.entities import EntityRef
    from drilling_intelligence.knowledge.facts import KnowledgeFact
    from drilling_intelligence.knowledge.repository import KnowledgeRepository
    from drilling_intelligence.wells.repository import WellRepository

    database = Database.from_url(url)
    try:
        with database.session() as session:
            wells = WellRepository(session)
            wells.get_or_create_workspace("/legacy", name="Legacy Field")
            project = wells.get_or_create_project("North Cormorant")
            field = wells.get_or_create_field("North Cormorant", project=project)
            well = wells.create_well("A-3", project_id=project.id, field_id=field.id)
            section = wells.get_or_create_section(well, "8 1/2 in", sequence=1, hole_size_in=8.5)
            wells.update_section(
                section,
                {
                    "top_depth": (9000.0, "ft"),
                    "bottom_depth": (9850.0, "ft"),
                    "planned_duration_days": 12.0,
                },
                state=RecordState.PLANNED,
            )
            wells.update_section(section, {"actual_duration_days": 14.5}, state=RecordState.ACTUAL)
            session.flush()
            repository = KnowledgeRepository(session)
            subject = EntityRef("well", str(well.id), label="A-3")
            for predicate, value, unit in (
                ("mud_weight", 10.2, "ppg"),
                ("hole_depth", 9850.0, "ft"),
            ):
                repository.manual_fact(
                    KnowledgeFact(
                        subject=subject,
                        predicate=predicate,
                        value_type="quantity",
                        original_value=f"{value} {unit}",
                        original_unit=unit,
                        value=value,
                        unit=unit,
                        normalized_value=value,
                        normalized_unit=unit,
                        text=f"{value} {unit}",
                        status="ACTIVE",
                        valid_from=NOW,
                    )
                )
            create_knowledge_relation(
                session,
                source_type="well",
                source_id=str(well.id),
                relation="WELL_HAS_SECTION",
                target_type="well_section",
                target_id=str(section.id),
                note="the hierarchy the legacy workspace already stated",
            )
            session.commit()
    finally:
        database.dispose()


def snapshot(engine: Engine) -> dict[str, list[tuple]]:
    """Every row the legacy workspace holds, across the tables 0003 already had.

    Written against reflected tables rather than SQL strings: the comparison is only worth making if it
    cannot itself be the thing that breaks when a column is renamed.
    """
    metadata = MetaData()
    metadata.reflect(bind=engine)
    wanted = {
        "project": ("name", "code", "status"),
        "field": ("name", "basin", "project_id"),
        "well": ("id", "name", "project_id", "field_id", "lifecycle_status"),
        "well_section": ("id", "well_id", "sequence", "name", "hole_size_in"),
        "knowledge_item": ("id", "predicate", "value", "unit", "status", "origin"),
        "knowledge_relation": ("id", "source_type", "relation", "target_type"),
    }
    with engine.connect() as connection:
        result = {}
        for name, columns in wanted.items():
            if name not in metadata.tables:
                continue
            relation = metadata.tables[name]
            statement = select(*(relation.columns[c] for c in columns)).order_by(
                relation.columns[0]
            )
            result[name] = list(connection.execute(statement).all())
        return result


def names(engine: Engine, method: str, relation: str) -> set[str]:
    getter = getattr(inspect(engine), method)
    return {item["name"] for item in getter(relation) if item.get("name")}


def table(engine: Engine, name: str):
    """The reflected table, so a read can name a column and fail loudly if it has been renamed.

    ``sqltable("well")`` carries no columns unless they are spelled out, which turns a typo into an
    ``AttributeError`` in the test rather than a passing test that selected nothing.
    """
    metadata = MetaData()
    metadata.reflect(bind=engine)
    return metadata.tables[name]


def count(engine: Engine, relation: str) -> int:
    """How many rows a table holds, without writing its name into a SQL string."""
    with engine.connect() as connection:
        return int(
            connection.execute(select(func.count()).select_from(sqltable(relation))).scalar_one()
        )


def migrated_engine(tmp_path: Path, *, seeded: bool = False) -> Engine:
    """A 0003 workspace taken up to head: the state a real file reaches after this migration."""
    path = tmp_path / "migrated.db"
    url = f"sqlite:///{path}"
    engine = create_engine(url)
    build_legacy_database(engine)
    engine.dispose()
    if seeded:
        seed_legacy_data(url)
    engine = create_engine(url)
    status = upgrade(engine, "head")
    assert status.up_to_date and status.current == heads()[0], status.to_dict()
    return engine


def test_the_upgrade_creates_every_table_the_models_declare(tmp_path) -> None:
    engine = create_engine(f"sqlite:///{tmp_path / 'empty.db'}")
    try:
        build_legacy_database(engine)
        assert sorted(schema_diff(engine)["missing_tables"]) == sorted(NEW_TABLES), schema_diff(
            engine
        )

        status = upgrade(engine, "0004")
        assert status.mode == "migrated" and status.current == "0004", status.to_dict()

        tables = set(inspect(engine).get_table_names())
        assert set(NEW_TABLES) <= tables, sorted(set(NEW_TABLES) - tables)
        assert schema_diff(engine) == {
            "missing_tables": [],
            "extra_tables": [],
            "missing_columns": [],
            "extra_columns": [],
        }
        indexes = {
            name for relation in NEW_TABLES for name in names(engine, "get_indexes", relation)
        }
        assert set(IMPORTANT_INDEXES) <= indexes, sorted(set(IMPORTANT_INDEXES) - indexes)
        constraints = {
            name
            for relation in NEW_TABLES
            for name in (*names(engine, "get_unique_constraints", relation), *indexes)
        }
        assert set(IMPORTANT_UNIQUES) <= constraints, sorted(set(IMPORTANT_UNIQUES) - constraints)
    finally:
        engine.dispose()


@pytest.mark.parametrize("relation", NEW_TABLES)
def test_the_migrated_schema_matches_the_models_index_for_index(tmp_path, relation: str) -> None:
    """Migration 0004 and ``create_all`` have to produce the same table, not two similar ones.

    A new workspace is created from the models; an existing one is upgraded.  If the two differ - an index
    the migration forgot, a foreign key with the wrong ``ondelete``, a CHECK that exists in Python and not
    in SQL - then the same query means different things depending on how a workspace was made, which is the
    kind of difference that stays invisible until two answers disagree.  One parameter per table so a
    failure names the table instead of a dict.
    """
    from drilling_intelligence.database.models import Base

    migrated = migrated_engine(tmp_path)
    from_models = create_engine(f"sqlite:///{tmp_path / 'from-models.db'}")
    try:
        Base.metadata.create_all(from_models, tables=[Base.metadata.tables[relation]])
        for getter in (
            "get_indexes",
            "get_unique_constraints",
            "get_foreign_keys",
            "get_check_constraints",
        ):
            assert names(migrated, getter, relation) == names(from_models, getter, relation), (
                f"{relation}: {getter} differs between the migrated schema and the models"
            )
        assert {c["name"] for c in inspect(migrated).get_columns(relation)} == {
            c["name"] for c in inspect(from_models).get_columns(relation)
        }, f"{relation}: the columns differ"
    finally:
        from_models.dispose()
        migrated.dispose()


def test_an_existing_workspace_is_left_alone_by_the_new_tables(tmp_path) -> None:
    """Sixteen tables added next door, and not one byte of the old data changed.

    This is what the owner of a legacy workspace actually cares about: their wells and their facts are
    still there, still valid, still reachable - and the knowledge graph they already built is the one the
    new tables link into, rather than a second one to keep in step.
    """
    from drilling_intelligence.database.session import Database
    from drilling_intelligence.operations.repository import OperationsRepository
    from drilling_intelligence.wells.repository import WellRepository

    path = tmp_path / "survivor.db"
    url = f"sqlite:///{path}"
    engine = create_engine(url)
    try:
        build_legacy_database(engine)
    finally:
        engine.dispose()
    seed_legacy_data(url)

    engine = create_engine(url)
    try:
        before = snapshot(engine)
        assert before["well"] and before["well_section"], "the fixture is meant to have data in it"
        assert len(before["knowledge_item"]) == 2
        assert before["knowledge_relation"], "the legacy workspace has edges to keep"

        upgrade(engine, "0004")
        assert snapshot(engine) == before, (
            "0004 rewrote a row in a table it was only meant to add to"
        )
        for relation in NEW_TABLES:
            assert count(engine, relation) == 0, (
                f"{relation} was backfilled, and nothing in this migration is allowed to invent a record"
            )
    finally:
        engine.dispose()

    # The migrated file is a working workspace, not a schema.  And the one thing the domain must never do
    # is date a day nobody dated: a report whose date is prose is stored with the prose.
    database = Database.from_url(url)
    try:
        with database.session() as session:
            well = WellRepository(session).find_well("A-3")
            assert well is not None, "the well registry cannot read its own table after the upgrade"
            report = OperationsRepository(session).register_report(
                well_id=str(well.id), report_date_text="14 June 2025", summary="legacy day"
            )
            assert report.report_date is None, (
                "a day with no parseable date is stored as no date plus the wording, never as a guess"
            )
            session.commit()
        with database.session() as session:
            reports = table(session.get_bind(), "ddr_report")
            stored = session.execute(
                select(reports.columns.report_date, reports.columns.report_date_text)
            ).all()
            assert stored == [(None, "14 June 2025")], stored
            assert (
                session.scalar(
                    select(func.count()).select_from(table(session.get_bind(), "knowledge_item"))
                )
                == 2
            )
    finally:
        database.dispose()

    engine = create_engine(url)
    try:
        assert snapshot(engine) == before
    finally:
        engine.dispose()


def test_the_constraints_the_repositories_rely_on_are_real_on_a_migrated_file(tmp_path) -> None:
    """The database says no too - and it says no on a file that was upgraded rather than created.

    Each of these is also enforced by a repository, which is exactly why it is in the schema: a bug in
    either layer, or a row edited by hand, meets the same constraint.  The bad rows are written through the
    ORM rather than a hand-typed column list, because the mapper applies the schema's own defaults
    (``duration_basis``, ``description``) - so what fails here is the CHECK or the partial unique, and not
    my guess at what a legal row needs.
    """
    from sqlalchemy.orm import Session

    from drilling_intelligence.database.models import NptRecord, ProcedureRecord, RiskRecord

    engine = migrated_engine(tmp_path, seeded=True)
    try:
        with engine.connect() as connection:
            wells = table(engine, "well")
            well_id = connection.execute(select(wells.columns.id).limit(1)).scalar_one()

        code = "PROC-STALL"
        with Session(engine) as session:
            # A 5x5 scale is a 5x5 scale, whatever a spreadsheet said.
            session.add(
                RiskRecord(
                    id=new_id("risk"),
                    title="lost circulation",
                    probability=6,
                    impact=3,
                    scale="MATRIX_5X5",
                )
            )
            with pytest.raises(IntegrityError, match="ck_risk_record_probability_scale"):
                session.flush()
            session.rollback()

            # A negative duration cannot be stored: a minus half hour is a claim, not a rounding error.
            session.add(
                NptRecord(
                    id=new_id("npt"),
                    well_id=well_id,
                    category="other",
                    description="stuck bit, reaming",
                    duration_hours=-1.0,
                )
            )
            with pytest.raises(IntegrityError, match="ck_npt_record_duration_not_negative"):
                session.flush()
            session.rollback()

            # One code, one current revision - a partial unique index, not an application convention.  Note
            # that SQLite reports the violation against ``procedure_record.code``: the index's own name does
            # not appear in the message, which is why the match is on the table and the proof is below.
            # Rev 1 goes in and stays; rev 2, marked current as well, is the row the index refuses.
            # They cannot both be added before the flush, because a rollback of a failed flush discards the
            # whole pending set - and then this test would prove nothing about which side was refused.
            session.add(
                ProcedureRecord(
                    id=new_id("proc"),
                    title="clean the hole",
                    code=code,
                    revision=1,
                    is_current=True,
                )
            )
            session.commit()
            session.add(
                ProcedureRecord(
                    id=new_id("proc"),
                    title="clean the hole",
                    code=code,
                    revision=2,
                    is_current=True,
                )
            )
            with pytest.raises(IntegrityError, match="procedure_record"):
                session.flush()
            session.rollback()

        assert count(engine, "risk_record") == 0, "a refused row leaves nothing behind"
        assert count(engine, "npt_record") == 0
        with engine.connect() as connection:
            procedures = table(connection, "procedure_record")
            current = connection.execute(
                select(func.count()).select_from(procedures).where(procedures.columns.code == code)
            ).scalar_one()
        assert current == 1, "the first revision stayed and the second was refused"
    finally:
        engine.dispose()


def test_the_downgrade_removes_the_domain_and_keeps_the_workspace(tmp_path) -> None:
    """Rolling back is the exact inverse: the sixteen tables go, the wells and knowledge stay.

    The order matters in that direction too - tables have to be dropped in dependency order - so this also
    proves ``downgrade`` was written that way, and that a person who upgraded, decided against it and rolled
    back is left with the workspace they had rather than a half-removed schema.
    """
    from sqlalchemy.orm import Session

    from drilling_intelligence.database.models import NptRecord

    path = tmp_path / "reverse.db"
    url = f"sqlite:///{path}"
    engine = create_engine(url)
    try:
        build_legacy_database(engine)
    finally:
        engine.dispose()
    seed_legacy_data(url)

    engine = create_engine(url)
    try:
        upgrade(engine, "0004")
        before = snapshot(engine)
        with Session(engine) as session:
            session.add(
                NptRecord(
                    id=new_id("npt"),
                    well_id=session.execute(
                        select(table(engine, "well").columns.id).limit(1)
                    ).scalar_one(),
                    category="other",
                    description="trip time",
                    duration_hours=1.5,
                )
            )
            session.commit()
        assert count(engine, "npt_record") == 1

        status = upgrade(engine, "0003", allow_downgrade=True)
        assert status.mode == "downgraded" and status.current == "0003", status.to_dict()
        tables = set(inspect(engine).get_table_names())
        assert not set(NEW_TABLES) & tables, sorted(set(NEW_TABLES) & tables)
        assert snapshot(engine) == before, "downgrading the schema must not downgrade the data"
        diff = schema_diff(engine)
        assert sorted(diff["missing_tables"]) == sorted(NEW_TABLES), diff

        again = upgrade(engine, "0004")
        assert again.mode == "migrated" and again.current == "0004", again.to_dict()
        assert snapshot(engine) == before
        assert schema_diff(engine) == {
            "missing_tables": [],
            "extra_tables": [],
            "missing_columns": [],
            "extra_columns": [],
        }
        # The domain row the downgrade removed with its table does not come back - and nothing duplicates
        # the rows the migration never touched.
        assert count(engine, "npt_record") == 0
    finally:
        engine.dispose()


def test_offline_sql_for_this_migration_creates_sixteen_tables(tmp_path) -> None:
    """``--sql`` has to render this migration, because that is how a DBA reviews it before running it.

    Nothing in 0004 rewrites a row, so the generated script is the honest statement of what the migration
    does: sixteen ``CREATE TABLE``s, their indexes, and the partial uniques that carry the one-current
    rule.  A ``CREATE UNIQUE INDEX ... WHERE`` that SQLite accepts and PostgreSQL rejects would be caught
    here, on the SQL, rather than on someone's server.
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
            "0003:0004",
            "--sql",
        ],
        cwd=str(ROOT),
        capture_output=True,
        text=True,
        env=env,
        check=False,
    )
    assert result.returncode == 0, result.stderr[-1500:]
    script = result.stdout
    for relation in NEW_TABLES:
        assert f"CREATE TABLE {relation} (" in script, f"{relation} is not in the generated SQL"
    assert "CREATE INDEX ix_npt_well_start" in script
    assert "CREATE UNIQUE INDEX uq_procedure_one_current" in script
    assert "WHERE is_current" in script, (
        "the one-current rule has to be in the SQL, not only in Python"
    )
    assert "ck_npt_record_duration_not_negative" in script or "duration_minutes >= 0" in script

    down = subprocess.run(
        [
            sys.executable,
            "-m",
            "alembic",
            "-c",
            str(ROOT / "alembic.ini"),
            "downgrade",
            "0004:0003",
            "--sql",
        ],
        cwd=str(ROOT),
        capture_output=True,
        text=True,
        env=env,
        check=False,
    )
    assert down.returncode == 0, down.stderr[-800:]
    for relation in NEW_TABLES:
        assert f"DROP TABLE {relation}" in down.stdout, (
            f"{relation} is left behind by the downgrade"
        )
    assert "DROP TABLE knowledge_item" not in down.stdout, (
        "the downgrade removes only what 0004 added"
    )


def test_the_record_paths_work_on_a_migrated_workspace(tmp_path) -> None:
    """The point of the tables is that the code writes and reads them - on a file that was *upgraded*.

    A freshly created database proves the models; a migrated one proves 0004 produced the same table, with
    the defaults, checks and indexes the repositories assume.  The read is the summary path a person would
    actually run rather than a hand-written ``select``, so a column the migration typed differently from the
    model - a duration stored as text, say - fails here instead of in a report.
    """
    from drilling_intelligence.database.session import Database
    from drilling_intelligence.operations.repository import OperationsRepository
    from drilling_intelligence.wells.repository import WellRepository

    path = tmp_path / "records.db"
    url = f"sqlite:///{path}"
    engine = create_engine(url)
    try:
        build_legacy_database(engine)
        upgrade(engine, "0004")
    finally:
        engine.dispose()

    database = Database.from_url(url)
    try:
        with database.session() as session:
            wells = WellRepository(session)
            project = wells.get_or_create_project("North Cormorant")
            field = wells.get_or_create_field("North Cormorant", project=project)
            well = wells.create_well("MIGRATED-1", project_id=project.id, field_id=field.id)
            operations = OperationsRepository(session)
            report = operations.register_report(
                well_id=str(well.id),
                report_date=datetime(2025, 6, 13, tzinfo=UTC),
                summary="one day, after the upgrade",
            )
            operations.record_npt(
                well_id=str(well.id),
                category="stuck_pipe",
                duration_hours=22.25,
                started_at=datetime(2025, 6, 13, 2, 0, tzinfo=UTC),
                report_id=str(report.id),
            )
            operations.record_npt(
                well_id=str(well.id),
                category="stuck_pipe",
                duration_hours=6.5,
                duration_text="the evening",
                report_id=str(report.id),
            )
            session.commit()
        with database.session() as session:
            summary = OperationsRepository(session).record_summary(field_id=str(field.id))
            # The migration interprets nothing: two rows, the hours the file stated, one of them undated,
            # and both still CANDIDATE because no person has confirmed them.
            assert summary["npt"]["rows"] == 2, summary
            assert summary["npt"]["total_hours"] == pytest.approx(28.75), summary
            assert summary["npt"]["undated"] == 1, summary
            assert summary["reports"] == 1, summary
            assert summary["npt"]["promoted"] == 0, summary
            assert summary["npt_by_category"][0]["hours"] == pytest.approx(28.75), summary
        with database.session() as session, pytest.raises(IntegrityError):
            session.execute(
                sqltable(
                    "npt_record",
                    column("id"),
                    column("well_id"),
                    column("category"),
                    column("duration_hours"),
                )
                .insert()
                .values(id=new_id("npt"), well_id="whatever", category="other", duration_hours=-1.0)
            )
    finally:
        database.dispose()
