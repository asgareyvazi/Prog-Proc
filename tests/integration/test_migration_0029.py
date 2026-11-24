"""D-06 acceptance: migration 0029 upgrade/downgrade/upgrade preserves the full schema contract."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest
import sqlalchemy.exc
from sqlalchemy import create_engine, text

from drilling_intelligence.database.migrations import current_revision, upgrade

ROOT = Path(__file__).resolve().parents[2]


def _open(tmp_path, name="w.db"):
    return create_engine(f"sqlite:///{tmp_path / name}")


def _procedure_and_steps(eng) -> None:
    with eng.begin() as c:
        c.execute(
            text(
                "INSERT INTO procedure_record (id, title, code, description, procedure_type, status, "
                "revision, is_current, created_by, origin, program_link_status, created_at, updated_at) "
                "VALUES ('proc-1', 'Synthetic proc', 'SYN-1', 'synthetic fixture', 'general', 'DRAFT', 1, 1, "
                "'tester', 'USER_CREATED', 'NONE', '2026-01-01 00:00:00+00:00', '2026-01-01 00:00:00+00:00')"
            )
        )
        rows = [
            ("step-1", 1, "First", "do one", 1),
            ("step-2", 2, "Second", "do two", 0),
            ("step-3", 3, "Third", "do three", 1),
        ]
        for sid, seq, title, instr, hold in rows:
            c.execute(
                text(
                    "INSERT INTO procedure_step (id, procedure_id, sequence, title, "
                    "instructions, responsible_role, equipment, parameters, hold_point, "
                    "safety_controls, source_reference, provenance, created_at, updated_at) "
                    "VALUES (:id, 'proc-1', :seq, :title, :instr, 'driller', 'rig', "
                    '\'[{"name":"p","value":"1","unit":"m"}]\', :hold, '
                    "'synthetic control', 'document:doc-1#v1', "
                    "(:prov), '2026-01-02 00:00:00+00:00', "
                    "'2026-01-03 00:00:00+00:00')"
                ),
                {
                    "id": sid,
                    "seq": seq,
                    "title": title,
                    "instr": instr,
                    "hold": hold,
                    "prov": '[{"extracted": true}]',
                },
            )


def _schema_objects(db_path: Path) -> dict:
    con = sqlite3.connect(db_path)
    try:
        return {
            "tables": {
                r[0]
                for r in con.execute(
                    "SELECT name FROM sqlite_master WHERE type='table' AND name LIKE 'procedure%'"
                )
            },
            "indexes": {
                r[0]
                for r in con.execute(
                    "SELECT name FROM sqlite_master WHERE type='index' AND tbl_name='procedure_step'"
                )
            },
            "triggers": {
                r[0]
                for r in con.execute(
                    "SELECT name FROM sqlite_master WHERE type='trigger' AND tbl_name='procedure_step'"
                )
            },
            "fk": list(con.execute("PRAGMA foreign_key_list(procedure_step)")),
            "table_info": list(con.execute("PRAGMA table_info(procedure_step)")),
            "ddl": con.execute(
                "SELECT sql FROM sqlite_master WHERE type='table' AND name='procedure_step'"
            )
            .fetchone()[0]
            .replace('"', "")
            .replace("procedure_step_rebuild", "procedure_step"),
        }
    finally:
        con.close()


def _rows(db_path: Path) -> list:
    con = sqlite3.connect(db_path)
    try:
        return list(
            con.execute(
                "SELECT id, procedure_id, sequence, title, instructions, parameters, "
                "hold_point, source_reference, provenance, created_at, updated_at "
                "FROM procedure_step ORDER BY sequence"
            )
        )
    finally:
        con.close()


def test_0029_round_trip_preserves_schema_and_data(tmp_path) -> None:
    db = tmp_path / "rt.db"
    eng = _open(tmp_path, "rt.db")
    upgrade(eng, "0028")  # build a real prior-schema database
    assert current_revision(eng) == "0028"
    _procedure_and_steps(eng)
    before_rows = _rows(db)
    before_schema = _schema_objects(db)
    assert len(before_rows) == 3

    # 0028 -> 0029
    upgrade(eng, "head")
    assert current_revision(eng) == "0030"
    con = sqlite3.connect(db)
    cols = {r[1] for r in con.execute("PRAGMA table_info(procedure_step)")}
    con.close()
    assert "completion_state" in cols
    # legacy rows got the default DRAFT state
    assert _rows(db)[0][0] == "step-1"
    con = sqlite3.connect(db)
    states = [
        r[0] for r in con.execute("SELECT completion_state FROM procedure_step ORDER BY sequence")
    ]
    con.close()
    assert states == ["DRAFT", "DRAFT", "DRAFT"]
    # invalid completion state rejected by trigger
    with (
        pytest.raises(
            sqlalchemy.exc.IntegrityError, match="invalid procedure step completion state"
        ),
        eng.begin() as c,
    ):
        c.execute(text("UPDATE procedure_step SET completion_state='BOGUS' WHERE id='step-1'"))
    # duplicate sequence still rejected
    with eng.begin() as c, pytest.raises(sqlalchemy.exc.IntegrityError):
        c.execute(
            text(
                "INSERT INTO procedure_step (id, procedure_id, sequence, title, "
                "instructions, hold_point, completion_state, created_at, updated_at) "
                "VALUES ('step-dup', 'proc-1', 1, 'dup', 'x', 0, 'DRAFT', "
                "'2026-01-01 00:00:00+00:00', '2026-01-01 00:00:00+00:00')"
            )
        )
    # non-positive sequence rejected
    with eng.begin() as c, pytest.raises(sqlalchemy.exc.IntegrityError):
        c.execute(
            text(
                "INSERT INTO procedure_step (id, procedure_id, sequence, title, "
                "instructions, hold_point, completion_state, created_at, updated_at) "
                "VALUES ('step-zero', 'proc-1', 0, 'zero', 'x', 0, 'DRAFT', "
                "'2026-01-01 00:00:00+00:00', '2026-01-01 00:00:00+00:00')"
            )
        )
    # orphaned step rejected (FK enforced)
    with eng.begin() as c:
        c.execute(text("PRAGMA foreign_keys=ON"))
    with eng.begin() as c, pytest.raises(sqlalchemy.exc.IntegrityError):
        c.execute(
            text(
                "INSERT INTO procedure_step (id, procedure_id, sequence, title, "
                "instructions, hold_point, completion_state, created_at, updated_at) "
                "VALUES ('step-orphan', 'no-such-proc', 9, 'orphan', 'x', 0, 'DRAFT', "
                "'2026-01-01 00:00:00+00:00', '2026-01-01 00:00:00+00:00')"
            )
        )
    eng.dispose()

    # 0029 -> 0028: full schema contract must survive
    eng = _open(tmp_path, "rt.db")
    upgrade(eng, "0028", allow_downgrade=True)
    assert current_revision(eng) == "0028"
    after_schema = _schema_objects(db)
    after_rows = _rows(db)
    assert after_rows == before_rows  # no row or JSON/provenance value lost
    assert after_schema["fk"] == before_schema["fk"]  # FK + CASCADE preserved
    assert after_schema["table_info"] == before_schema["table_info"]
    assert after_schema["ddl"] == before_schema["ddl"]  # exact original DDL restored
    assert after_schema["indexes"] == before_schema["indexes"]
    assert after_schema["triggers"] == before_schema["triggers"] == set()
    con = sqlite3.connect(db)
    con.execute("PRAGMA foreign_keys=ON")
    assert con.execute("PRAGMA foreign_key_check").fetchall() == []
    assert con.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    # cascade delete still works
    con.execute("DELETE FROM procedure_record WHERE id='proc-1'")
    con.commit()
    assert con.execute("SELECT count(*) FROM procedure_step").fetchone()[0] == 0
    con.close()
    eng.dispose()

    # 0028 -> 0029 again: contract restored
    eng = _open(tmp_path, "rt.db")
    upgrade(eng, "head")
    assert current_revision(eng) == "0030"
    con = sqlite3.connect(db)
    cols = {r[1] for r in con.execute("PRAGMA table_info(procedure_step)")}
    trig = {
        r[0]
        for r in con.execute(
            "SELECT name FROM sqlite_master WHERE type='trigger' AND tbl_name='procedure_step'"
        )
    }
    con.close()
    assert "completion_state" in cols
    assert trig == {"trg_procedure_step_state_insert", "trg_procedure_step_state_update"}
    eng.dispose()


def test_0029_repeated_upgrade_is_idempotent(tmp_path) -> None:
    eng = _open(tmp_path, "idem.db")
    upgrade(eng, "head")
    upgrade(eng, "head")  # repeated upgrade must be a no-op
    assert current_revision(eng) == "0030"
    eng.dispose()
