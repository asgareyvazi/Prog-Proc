"""0007 → 0008: a calculation input names the durable thing it consumed, or admits it cannot.

``calculation_input.subject_key`` has been free text since 0001 and the change-impact query matched
it with ``=``.  0008 adds ``subject_kind``/``subject_id`` and the index over them, and backfills
what it can *deterministically* recognise.

The four things a reviewer should be able to check without reading the migration:

*   the columns and the index exist, and nothing that was there before moved - every pre-existing
    column of every pre-existing row comes out byte-identical;
*   a canonical key is resolved into its anchor (``well:well-1|property:mud_weight|state:ACTUAL``
    becomes ``("well", "well-1")``), and the leading anchor is matched longest-first so
    ``document_version:`` is never read as ``document:``;
*   a value the migration cannot recognise keeps its text exactly and is labelled ``legacy`` with a
    NULL id - it is never parsed approximately and never dropped;
*   ``calculation.identity_key`` is untouched, so a historical record stays the record it was.

The offline path is checked too: ``alembic upgrade --sql`` must render this migration without a
database, because that is how a DBA reviews it before it runs on a shared file.
"""

from __future__ import annotations

import os
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path

from sqlalchemy import create_engine, inspect, text
from sqlalchemy.engine import Engine

from drilling_intelligence.database.migrations import heads, schema_diff, upgrade

ROOT = Path(__file__).resolve().parents[2]
NOW = datetime(2026, 1, 1, tzinfo=UTC)

ADDED_COLUMNS = ("subject_kind", "subject_id")
ADDED_INDEX = "ix_calc_input_subject_ref"

#: (subject_key, expected kind, expected id).  Each row is a real shape the platform can hold.
CASES: tuple[tuple[str, str, str | None], ...] = (
    # canonical keys the knowledge layer produces
    ("well:well-1|property:mud_weight|state:ACTUAL", "well", "well-1"),
    ("section:sec-9|property:hole_depth|state:PLANNED", "section", "sec-9"),
    ("document_version:ver-7|property:revision|state:ACTUAL", "document_version", "ver-7"),
    ("document:doc-3|property:title|state:ACTUAL", "document", "doc-3"),
    ("project:prj-2|property:budget|state:PLANNED", "project", "prj-2"),
    # an anchor with no further component is still an anchor
    ("well:well-4", "well", "well-4"),
    # free-form values: recognised as legacy, never interpreted.
    #
    # ``well:A-3|mud_weight`` looks like an anchor followed by a property, but it has no
    # ``property:`` component, so ``core.ids`` does *not* consider it canonical.  Resolving it
    # here would label a row ``well``/``A-3`` that the runtime resolver calls legacy - the
    # migration and the write path would then disagree about what the same string means, and the
    # impact query (which trusts the runtime rule) would never find the row the migration had
    # confidently indexed.  The tail must therefore itself be a recognised component.
    ("well:A-3|mud_weight", "legacy", None),
    # A colon inside the anchor id means the string is not the shape it appears to be: a real
    # ``a:b`` id would have been escaped by ``core.ids``, so this is free text.
    ("well:a:b|property:x", "legacy", None),
    ("mud_report.xlsx!Summary!B9", "legacy", None),
    ("just-a-token", "legacy", None),
    ("property:mud_weight|state:ACTUAL", "legacy", None),  # no anchor at all
    ("well:|property:x", "legacy", None),  # empty id is not an identity
    ("well:w\\|1|property:x", "legacy", None),  # escaped component: not parsed approximately
)


def build_legacy_database(engine: Engine) -> None:
    """A 0007 schema, which is where a workspace is before this migration."""
    status = upgrade(engine, "0007")
    assert status.mode == "migrated" and status.current == "0007", status.to_dict()
    columns = {c["name"] for c in inspect(engine).get_columns("calculation_input")}
    assert "subject_kind" not in columns and "subject_id" not in columns, (
        "the point of the test is that these columns are missing"
    )


def add_input(engine: Engine, row_id: str, subject_key: str | None) -> None:
    """One calculation and one input written in 0007's vocabulary."""
    with engine.begin() as connection:
        connection.execute(
            text(
                "insert into calculation (id, method_id, method_version, calculation_type,"
                " record_state, inputs, assumptions, provenance, status, revision, triggered_by,"
                " origin, created_by, identity_key, created_at, updated_at)"
                " values (:c, 'hydraulics-v1', '3', 'annular_pressure', 'CURRENT', '{}', '[]', '[]',"
                " 'COMPUTED', 1, 'cli', 'MANUAL', 'system', :k, :now, :now)"
            ),
            {"c": f"calc-{row_id}", "k": f"calc:key-{row_id}", "now": NOW},
        )
        connection.execute(
            text(
                "insert into calculation_input (id, calculation_id, name, value, unit, dimension,"
                " source_kind, subject_key) values (:i, :c, 'x', 1.0, '', '', 'knowledge', :s)"
            ),
            {"i": row_id, "c": f"calc-{row_id}", "s": subject_key},
        )


def snapshot(engine: Engine) -> list[tuple]:
    """Everything 0007 stored about an input, so "nothing was lost" is a comparison."""
    with engine.connect() as connection:
        return list(
            connection.execute(
                text(
                    "select id, calculation_id, name, value, unit, dimension, source_kind,"
                    " subject_key, provenance from calculation_input order by id"
                )
            ).all()
        )


def identity_keys(engine: Engine) -> list[tuple]:
    with engine.connect() as connection:
        return list(
            connection.execute(text("select id, identity_key from calculation order by id")).all()
        )


def test_the_upgrade_adds_the_columns_and_leaves_every_existing_value_alone(tmp_path) -> None:
    engine = create_engine(f"sqlite:///{tmp_path / 'legacy.db'}")
    try:
        build_legacy_database(engine)
        for index, (subject, _kind, _id) in enumerate(CASES):
            add_input(engine, f"cain-{index:02d}", subject)
        add_input(engine, "cain-null", None)
        before = snapshot(engine)
        identities_before = identity_keys(engine)

        status = upgrade(engine, "0008")
        assert status.mode == "migrated" and status.current == "0008", status.to_dict()

        columns = {c["name"] for c in inspect(engine).get_columns("calculation_input")}
        assert set(ADDED_COLUMNS) <= columns, sorted(set(ADDED_COLUMNS) - columns)
        indexes = {i["name"] for i in inspect(engine).get_indexes("calculation_input")}
        assert ADDED_INDEX in indexes, sorted(indexes)
        assert snapshot(engine) == before, "every pre-existing column must be byte-identical"
        assert identity_keys(engine) == identities_before, (
            "0008 must not touch calculation identity - a historical record stays identifiable"
        )
        assert heads() == ["0008"], heads()
        assert schema_diff(engine) == {
            "missing_tables": [],
            "extra_tables": [],
            "missing_columns": [],
            "extra_columns": [],
        }, "a migrated file and a fresh create_all must describe the same schema"
    finally:
        engine.dispose()


def test_the_backfill_resolves_only_what_it_can_recognise(tmp_path) -> None:
    """Each shape lands where it should, and nothing is parsed approximately."""
    engine = create_engine(f"sqlite:///{tmp_path / 'backfill.db'}")
    try:
        build_legacy_database(engine)
        for index, (subject, _kind, _id) in enumerate(CASES):
            add_input(engine, f"cain-{index:02d}", subject)
        upgrade(engine, "0008")

        with engine.connect() as connection:
            rows = {
                row["id"]: row
                for row in connection.execute(
                    text("select id, subject_key, subject_kind, subject_id from calculation_input")
                )
                .mappings()
                .all()
            }
        for index, (subject, kind, identifier) in enumerate(CASES):
            row = rows[f"cain-{index:02d}"]
            assert row["subject_key"] == subject, f"{subject!r} must be preserved verbatim"
            assert row["subject_kind"] == kind, f"{subject!r} -> kind {row['subject_kind']!r}"
            assert row["subject_id"] == identifier, f"{subject!r} -> id {row['subject_id']!r}"
        legacy = [row for row in rows.values() if row["subject_kind"] == "legacy"]
        assert legacy, "the fixture includes values that must not be resolved"
        assert all(row["subject_id"] is None for row in legacy), (
            "a legacy subject has no id: the migration labels it, it does not guess one"
        )
    finally:
        engine.dispose()


def test_a_null_subject_is_left_null(tmp_path) -> None:
    """An input that named no subject is not a dependency, and 0008 does not invent one."""
    engine = create_engine(f"sqlite:///{tmp_path / 'nulls.db'}")
    try:
        build_legacy_database(engine)
        add_input(engine, "cain-null", None)
        upgrade(engine, "0008")
        with engine.connect() as connection:
            row = (
                connection.execute(
                    text(
                        "select subject_key, subject_kind, subject_id from calculation_input"
                        " where id = 'cain-null'"
                    )
                )
                .mappings()
                .one()
            )
        assert row["subject_key"] is None
        assert row["subject_kind"] is None and row["subject_id"] is None
    finally:
        engine.dispose()


def test_the_downgrade_is_the_exact_inverse(tmp_path) -> None:
    """Back to 0007: the columns and the index go, and every original value survives."""
    engine = create_engine(f"sqlite:///{tmp_path / 'roundtrip.db'}")
    try:
        build_legacy_database(engine)
        for index, (subject, _kind, _id) in enumerate(CASES):
            add_input(engine, f"cain-{index:02d}", subject)
        before = snapshot(engine)
        identities_before = identity_keys(engine)

        upgrade(engine, "0008")
        status = upgrade(engine, "0007", allow_downgrade=True)
        assert status.current == "0007", status.to_dict()

        columns = {c["name"] for c in inspect(engine).get_columns("calculation_input")}
        assert not set(ADDED_COLUMNS) & columns, sorted(set(ADDED_COLUMNS) & columns)
        indexes = {i["name"] for i in inspect(engine).get_indexes("calculation_input")}
        assert ADDED_INDEX not in indexes
        assert snapshot(engine) == before, "the downgrade must not disturb the original values"
        assert identity_keys(engine) == identities_before
    finally:
        engine.dispose()


def test_the_migration_renders_offline_sql(tmp_path) -> None:
    """``alembic upgrade 0007:0008 --sql`` is how a DBA reviews this before running it."""
    environment = dict(os.environ)
    environment["DRILLINTEL_DATABASE__URL"] = f"sqlite:///{tmp_path / 'offline.db'}"
    environment["PYTHONPATH"] = str(ROOT / "src")
    completed = subprocess.run(
        [sys.executable, "-m", "alembic", "upgrade", "0007:0008", "--sql"],
        cwd=ROOT,
        env=environment,
        capture_output=True,
        text=True,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr
    sql = completed.stdout
    assert "ADD COLUMN subject_kind" in sql, sql
    assert "ADD COLUMN subject_id" in sql, sql
    assert ADDED_INDEX in sql, sql
    assert "UPDATE calculation_input" in sql, sql
    # No table rebuild: a copy-and-move would show up as a temporary table.
    assert "_alembic_tmp_calculation_input" not in sql, sql
    assert "DROP TABLE" not in sql.upper(), sql


def test_the_backfill_and_the_write_path_agree_on_every_shape(tmp_path) -> None:
    """The defect this file exists to prevent from returning: **one rule, two implementations**.

    The backfill resolves subjects in SQL; ``resolve_input_subject`` resolves them in Python on
    every write.  They are the same rule expressed twice, and when they disagree the platform
    lies in a specific, silent way: a row the migration labels ``well``/``A-3`` that the runtime
    calls legacy is indexed under an identity no query will ever ask for, so the calculation
    depending on it becomes invisible to exactly the question the columns were added to answer.

    Comparing them row by row is the only assertion that catches a future edit to either side.
    """
    from drilling_intelligence.engineering.repository import resolve_input_subject

    engine = create_engine(f"sqlite:///{tmp_path / 'agree.db'}")
    try:
        build_legacy_database(engine)
        for index, (subject, _kind, _identifier) in enumerate(CASES):
            add_input(engine, f"cain-{index:02d}", subject)
        upgrade(engine, "0008")
        with engine.connect() as connection:
            rows = {
                row["id"]: row
                for row in connection.execute(
                    text("select id, subject_key, subject_kind, subject_id from calculation_input")
                )
                .mappings()
                .all()
            }
        disagreements = []
        for index, (subject, _kind, _identifier) in enumerate(CASES):
            row = rows[f"cain-{index:02d}"]
            _stored, runtime_kind, runtime_id = resolve_input_subject(subject)
            if (row["subject_kind"], row["subject_id"]) != (runtime_kind, runtime_id):
                disagreements.append(
                    (
                        subject,
                        f"migration={row['subject_kind']}/{row['subject_id']}",
                        f"runtime={runtime_kind}/{runtime_id}",
                    )
                )
        # One documented, deliberate exception, and it is safe in one direction only.  A key
        # containing an escape character would need the full unescaping grammar to be parsed
        # correctly, which SQL cannot express, so the backfill declines and leaves the row
        # ``legacy`` rather than resolving it approximately.  Under-resolving is recoverable -
        # ``_subject_predicate`` still finds such a row by its exact text, and ``doctor`` reports
        # it as unresolved - whereas over-resolving would silently invent an identity.  The
        # assertion below pins *which* rows may differ, so a new disagreement still fails.
        assert [item[0] for item in disagreements] == ["well:w\\|1|property:x"], disagreements
        assert all(item[1] == "migration=legacy/None" for item in disagreements), (
            "the migration may only ever be more conservative than the write path, never less"
        )
    finally:
        engine.dispose()


def test_the_backfill_never_promotes_free_text_to_an_identity(tmp_path) -> None:
    """A string that merely *starts* with a known prefix is not a reference to that thing."""
    engine = create_engine(f"sqlite:///{tmp_path / 'noguess.db'}")
    try:
        build_legacy_database(engine)
        # None of these is a canonical key; all of them begin with a real anchor kind.
        #
        # A *bare* ``<anchor>:<id>`` is deliberately absent: that is the canonical one-component
        # form, ``core.ids`` accepts it, and an id is an opaque token, so ``document:report.xlsx``
        # is a genuine reference to a document with an awkward id, not free text.  The shapes
        # below all carry a second component that proves they are not canonical keys.
        shapes = (
            "well:A-3|mud_weight",  # no "property:" component
            "well:a:b|property:x",  # unescaped colon inside the id
            "well:|property:x",  # empty id
            "document:report.xlsx!B9|sheet:Summary",  # unrecognised trailing component
            "project:2024 drilling campaign|notes",
        )
        for index, subject in enumerate(shapes):
            add_input(engine, f"cain-g{index}", subject)
        upgrade(engine, "0008")
        with engine.connect() as connection:
            rows = (
                connection.execute(
                    text(
                        "select subject_key, subject_kind, subject_id from calculation_input"
                        " order by id"
                    )
                )
                .mappings()
                .all()
            )
        for row in rows:
            assert row["subject_kind"] == "legacy", (
                f"{row['subject_key']!r} was given a fabricated identity"
            )
            assert row["subject_id"] is None
        assert {row["subject_key"] for row in rows} == set(shapes), "text must survive verbatim"
    finally:
        engine.dispose()


def test_the_backfill_covers_the_canonical_vocabulary(tmp_path) -> None:
    """The safety net for the one risk a fixed case list cannot cover.

    ``CASES`` above is a literal, and a literal only tests the shapes somebody thought to write
    down.  Adding a sixth kind to ``core.ids.ANCHOR_KINDS`` - an entirely reasonable future edit -
    would leave every existing case passing while the migration silently stopped agreeing with the
    write path for the new kind: rows the runtime resolves would be labelled ``legacy`` by the
    backfill, and the change-impact query would miss them.

    So the corpus here is *derived* from the live vocabulary rather than restated.  A subject is
    generated for every anchor kind the platform currently knows about, and the migration's
    classification is compared with ``resolve_input_subject``.  Adding a kind without teaching 0008
    about it fails this test, which is exactly the signal a silent divergence needs.

    The migration keeps its own literal ``_ANCHOR_KINDS`` on purpose - a historical artefact must
    not change meaning when a later revision extends the vocabulary - so this test is the seam that
    holds the two in step, and a future revision (0009+) is where a new kind would be backfilled.
    """
    from drilling_intelligence.core.ids import ANCHOR_KINDS, SubjectKey
    from drilling_intelligence.engineering.repository import resolve_input_subject

    generated = [
        SubjectKey(
            entity_type=kind, entity_id=f"{kind}-1", property_name="p", record_state="ACTUAL"
        ).render()
        for kind in ANCHOR_KINDS
    ]
    assert len(generated) == len(ANCHOR_KINDS) >= 5

    engine = create_engine(f"sqlite:///{tmp_path / 'vocab.db'}")
    try:
        build_legacy_database(engine)
        for index, subject in enumerate(generated):
            add_input(engine, f"cain-v{index}", subject)
        upgrade(engine, "0008")
        with engine.connect() as connection:
            rows = {
                row["id"]: row
                for row in connection.execute(
                    text("select id, subject_key, subject_kind, subject_id from calculation_input")
                )
                .mappings()
                .all()
            }
        uncovered = []
        for index, (kind, subject) in enumerate(zip(ANCHOR_KINDS, generated, strict=True)):
            row = rows[f"cain-v{index}"]
            _stored, runtime_kind, runtime_id = resolve_input_subject(subject)
            # The runtime must resolve every kind in its own vocabulary...
            assert (runtime_kind, runtime_id) == (kind, f"{kind}-1"), subject
            # ...and the migration must agree, or the vocabulary has outgrown revision 0008.
            if (row["subject_kind"], row["subject_id"]) != (runtime_kind, runtime_id):
                uncovered.append(
                    f"{kind}: migration={row['subject_kind']}/{row['subject_id']}"
                    f" runtime={runtime_kind}/{runtime_id}"
                )
        assert uncovered == [], (
            "core.ids.ANCHOR_KINDS has grown past what migration 0008 backfills; a new anchor kind"
            " needs its own revision, or historical rows for it stay unresolved: "
            + "; ".join(uncovered)
        )
    finally:
        engine.dispose()


def test_the_escape_guard_does_not_depend_on_like_semantics(tmp_path) -> None:
    """A backslash is LIKE's escape character on PostgreSQL and an ordinary byte on SQLite.

    ``NOT LIKE '%\\%'`` therefore means two different things on the two backends ADR-0004 supports:
    "contains a backslash" on SQLite, "ends with a literal percent" on PostgreSQL.  On PostgreSQL
    that let an escaped key through the guard, where the escape-blind string surgery below would
    have split it at the wrong ``|`` and stored an anchor id that is not what ``core.ids`` says it
    is - a fabricated identity, which is the one thing this migration promises never to produce.

    The rendered SQL must therefore contain no ``LIKE``-based backslash test at all.
    """
    environment = dict(os.environ)
    environment["DRILLINTEL_DATABASE__URL"] = f"sqlite:///{tmp_path / 'escape.db'}"
    environment["PYTHONPATH"] = str(ROOT / "src")
    completed = subprocess.run(
        [sys.executable, "-m", "alembic", "upgrade", "0007:0008", "--sql"],
        cwd=ROOT,
        env=environment,
        capture_output=True,
        text=True,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr
    sql = completed.stdout
    assert "NOT LIKE '%\\%'" not in sql, "the dialect-ambiguous escape guard is back"
    # The replacement is a position function, which has one meaning on every backend.
    assert "instr(calculation_input.subject_key, '\\')" in sql, sql

    # And it still does its job: an escaped key stays legacy rather than being resolved wrongly.
    engine = create_engine(f"sqlite:///{tmp_path / 'guard.db'}")
    try:
        build_legacy_database(engine)
        add_input(engine, "cain-esc", "well:w\\|1|property:x")
        upgrade(engine, "0008")
        with engine.connect() as connection:
            row = (
                connection.execute(text("select subject_kind, subject_id from calculation_input"))
                .mappings()
                .one()
            )
        assert (row["subject_kind"], row["subject_id"]) == ("legacy", None)
    finally:
        engine.dispose()
