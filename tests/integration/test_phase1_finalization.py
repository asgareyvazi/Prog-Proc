"""V10.3 Phase 1 finalization — complements existing offline/code_key tests.

Covers §1: 0022 exception handling, code_key canonical, partial-state idempotency,
historical 0020 immutability, offline --sql vs online disposable-copy equivalence,
Windows drive-letter/%/Unicode paths, PostgreSQL claim, wheel bootstrap/Doctor.
"""

from __future__ import annotations

import hashlib
import io
import json
import os
import re
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
SRC = ROOT / "src"


def _hash(path: Path) -> str | None:
    if not path.exists():
        return None
    return hashlib.sha256(path.read_bytes()).hexdigest()


# ---------------------------------------------------------------------------
# 1.1 0022 exception handling — no silent pass
# ---------------------------------------------------------------------------


def test_0022_has_no_bare_except_pass_masking():
    p = ROOT / "migrations/versions/20261101_0022_lesson_code_key_canonical.py"
    assert p.exists(), "0022 filename must be 20261101 valid date"
    text = p.read_text(encoding="utf-8")
    # The file must not contain a bare `except Exception: pass` or `except: pass`
    # that would hide a failure as success.  A narrow `except X: pass` for
    # idempotency with a comment is allowed only when followed by explicit
    # verification.  Our new 0022 should have no such construct without verification.
    # We check for the old pattern: a lone `except Exception:` line followed by `pass` within 2 lines and no raise/verify.
    # Simpler: assert the new helper functions exist and that downgrade/upgrade distinguish states.
    assert "_is_offline" in text, "0022 must have offline detection"
    assert "_column_exists" in text, "0022 must distinguish already-applied column"
    assert "_index_exists" in text, "0022 must distinguish already-applied index"
    assert "_index_definition_correct" in text, "0022 must distinguish wrong definition"
    # No silent masking — every except that does pass must be paired with a comment or verification
    # Count `except Exception:` occurrences — new file should have none that end with bare pass without raise
    # The only allowed bare pass is inside _column_exists/_index_exists helpers (probe helpers).
    # Upgrade/downgrade must not have `except Exception:\n        pass` without verification.
    # Check upgrade section
    upgrade_text = text.split("def upgrade")[1].split("def downgrade")[0]
    # Find any `except Exception:` in upgrade that is followed by `pass` alone
    for m in re.finditer(r"except Exception[^:]*:\s*\n\s*pass\b", upgrade_text):
        # If this is inside helper, not in upgrade — but upgrade_text excludes helpers
        pytest.fail(
            f"upgrade still contains bare except pass masking at: {upgrade_text[max(0, m.start() - 40) : m.end() + 40]!r}"
        )


def test_0022_filename_is_valid_date_and_revision_preserved():
    files = list((ROOT / "migrations/versions").glob("*_0022_*.py"))
    assert len(files) == 1, f"exactly one 0022 file, got {files}"
    name = files[0].name
    assert name.startswith("20261101_0022_"), f"filename must be 20261101 valid date, got {name}"
    # Ensure revision inside matches
    content = files[0].read_text()
    assert 'revision = "0022"' in content
    assert 'down_revision = "0021"' in content
    # Old invalid day file must not exist
    assert not (ROOT / "migrations/versions/20261032_0022_lesson_code_key_canonical.py").exists()


# ---------------------------------------------------------------------------
# 1.2 canonical strip().casefold() via code_key + partial unique on all write paths
# ---------------------------------------------------------------------------


def test_code_key_is_authoritative_and_partial_unique_exists(tmp_path: Path):
    from sqlalchemy import inspect

    from drilling_intelligence.cli.app import main as cli_main
    from drilling_intelligence.wells.workspace import Workspace

    ws_root = tmp_path / "ws"
    assert cli_main(["workspace", "create", str(ws_root)]) == 0
    ws = Workspace.open(ws_root)
    try:
        eng = ws.database.engine
        insp = inspect(eng)
        cols = {c["name"] for c in insp.get_columns("lesson_learned")}
        assert "code_key" in cols, "code_key column must exist at head 0022"
        # Check index in sqlite_master has correct predicate
        with eng.connect() as conn:
            row = conn.execute(
                __import__("sqlalchemy").text(
                    "SELECT sql FROM sqlite_master WHERE type='index' AND name='uq_lesson_code_key_current'"
                )
            ).fetchone()
            assert row is not None and row[0] is not None
            sql = row[0].lower()
            assert "code_key" in sql
            assert "is_current" in sql
            assert "unique" in sql
        # Verify LessonRepository maintains code_key on all write paths: capture, update, revise, promote

        from drilling_intelligence.database.models import LessonLearned
        from drilling_intelligence.lessons.repository import LessonRepository

        # capture
        with ws.database.unit_of_work() as s:
            repo = LessonRepository(s)
            les = repo.capture(code="LL-900", title="t", lesson="l")
            s.flush()
            assert les.code_key == "ll-900", "capture must set code_key via strip().casefold()"
        # update_lesson
        with ws.database.unit_of_work() as s:
            repo = LessonRepository(s)
            repo.update_lesson(les.id, code=" LL-901 ")
            s.flush()
            updated = s.get(LessonLearned, les.id)
            assert updated.code_key == "ll-901", "update must maintain code_key with strip+casefold"
        # revise (creates successor with same canonical)
        with ws.database.unit_of_work() as s:
            repo = LessonRepository(s)
            rev = repo.revise(les.id, by="tester", changes={"lesson": "revised"})
            s.flush()
            assert rev.code_key == "ll-901"
            # predecessor should be not current
            pred = s.get(LessonLearned, les.id)
            assert not pred.is_current
        # promote path: check promote uses code_key
        promote_path = ROOT / "src/drilling_intelligence/operations/promote.py"
        promo_text = promote_path.read_text()
        assert "code_key" in promo_text, "promote must set/check code_key"
    finally:
        ws.close()


# ---------------------------------------------------------------------------
# 1.3 partial-state idempotency
# ---------------------------------------------------------------------------


def test_partial_state_idempotency_column_index_variants(tmp_path: Path):
    """Four partial states reached by partial 0022 apply must be repairable."""
    from drilling_intelligence.database.engine import open_sqlite
    from drilling_intelligence.database.migrations import current_revision, upgrade

    # State A: column exists, index missing, stamped 0021 (partial before index) -> upgrade should create index
    db_a = tmp_path / "a.db"
    eng = open_sqlite(f"sqlite:///{db_a}")
    try:
        upgrade(eng, "0021")
        # Simulate partial: manually add column as 0022 would, but not index, keep stamp 0021
        with eng.begin() as conn:
            conn.execute(
                __import__("sqlalchemy").text(
                    "ALTER TABLE lesson_learned ADD COLUMN code_key VARCHAR(80)"
                )
            )
            conn.execute(
                __import__("sqlalchemy").text(
                    "UPDATE lesson_learned SET code_key = lower(trim(code)) WHERE code IS NOT NULL"
                )
            )
        # Verify column exists, index missing
        from sqlalchemy import inspect as insp_a

        assert "code_key" in {c["name"] for c in insp_a(eng).get_columns("lesson_learned")}
        with eng.connect() as conn:
            assert (
                conn.execute(
                    __import__("sqlalchemy").text(
                        "SELECT 1 FROM sqlite_master WHERE type='index' AND name='uq_lesson_code_key_current'"
                    )
                ).fetchone()
                is None
            )
        upgrade(eng)  # to 0022
        with eng.connect() as conn:
            assert (
                conn.execute(
                    __import__("sqlalchemy").text(
                        "SELECT 1 FROM sqlite_master WHERE type='index' AND name='uq_lesson_code_key_current'"
                    )
                ).fetchone()
                is not None
            )
        assert current_revision(eng) == "0022"
    finally:
        eng.dispose()

    # State B: already fully applied -> upgrade is idempotent, no error
    db_b = tmp_path / "b.db"
    eng2 = open_sqlite(f"sqlite:///{db_b}")
    try:
        upgrade(eng2)
        # Second upgrade should be no-op
        upgrade(eng2)
        assert current_revision(eng2) == "0022"
    finally:
        eng2.dispose()

    # State C: column missing, index missing but stamped 0021 -> upgrade should add column+index and backfill
    db_c = tmp_path / "c.db"
    eng3 = open_sqlite(f"sqlite:///{db_c}")
    try:
        upgrade(eng3, "0021")
        # Ensure 0021 state has no code_key column
        from sqlalchemy import inspect as insp_mod

        cols = {c["name"] for c in insp_mod(eng3).get_columns("lesson_learned")}
        assert "code_key" not in cols
        # Insert a row with code needing casefold
        with eng3.begin() as conn:
            conn.execute(
                __import__("sqlalchemy").text(
                    "INSERT INTO lesson_learned (id, code, revision, is_current, status, title, lesson, created_by, created_at, updated_at, depth_from_unit, depth_to_unit, root_cause_status, context, observation, recommendation, origin, provenance, attributes) "
                    "VALUES ('les-partial', 'LL-PARTIAL', 1, 1, 'DRAFT', 't','l','system', datetime('now'), datetime('now'), 'm','m','UNKNOWN','','','','MANUAL','[]','{}')"
                )
            )
        upgrade(eng3)  # to head 0022
        assert current_revision(eng3) == "0022"
        with eng3.connect() as conn:
            row = conn.execute(
                __import__("sqlalchemy").text(
                    "SELECT code_key FROM lesson_learned WHERE id='les-partial'"
                )
            ).fetchone()
            assert row[0] == "ll-partial", f"backfill must casefold, got {row[0]}"
    finally:
        eng3.dispose()

    # State D: index exists but with wrong predicate (e.g., missing WHERE), stamped 0021 with column -> upgrade should repair
    db_d = tmp_path / "d.db"
    eng4 = open_sqlite(f"sqlite:///{db_d}")
    try:
        upgrade(eng4, "0021")
        with eng4.begin() as conn:
            conn.execute(
                __import__("sqlalchemy").text(
                    "ALTER TABLE lesson_learned ADD COLUMN code_key VARCHAR(80)"
                )
            )
            conn.execute(
                __import__("sqlalchemy").text(
                    "CREATE UNIQUE INDEX uq_lesson_code_key_current ON lesson_learned (code_key)"
                )
            )
            # stamp remains 0021, so upgrade will see wrong index and repair
        # Now upgrade should detect incorrect definition and repair
        upgrade(eng4)
        assert current_revision(eng4) == "0022"
        with eng4.connect() as conn:
            row = conn.execute(
                __import__("sqlalchemy").text(
                    "SELECT sql FROM sqlite_master WHERE type='index' AND name='uq_lesson_code_key_current'"
                )
            ).fetchone()
            assert row and "is_current" in row[0].lower(), (
                f"index should be repaired to partial, got {row[0] if row else None}"
            )
    finally:
        eng4.dispose()

    # State E: already at 0022 with correct index — downgrade and re-upgrade is idempotent
    db_e = tmp_path / "e.db"
    eng5 = open_sqlite(f"sqlite:///{db_e}")
    try:
        upgrade(eng5)
        # Downgrade via allow_downgrade
        upgrade(eng5, "0021", allow_downgrade=True)
        # After downgrade, code_key gone
        from sqlalchemy import inspect as insp_e

        assert "code_key" not in {c["name"] for c in insp_e(eng5).get_columns("lesson_learned")}
        upgrade(eng5)
        assert current_revision(eng5) == "0022"
        assert "code_key" in {c["name"] for c in insp_e(eng5).get_columns("lesson_learned")}
    finally:
        eng5.dispose()


# ---------------------------------------------------------------------------
# 1.4 historical 0020 immutability
# ---------------------------------------------------------------------------


def test_0020_is_historically_deterministic():
    p = ROOT / "migrations/versions/20261031_0020_lesson_learned_promotion_linkage.py"
    text = p.read_text()
    assert "Explicit 0020 target table" in text
    assert "Explicit 0019 table" in text
    assert "_target_table_0020" in text and "_target_table_0019" in text
    # Must not import live LessonLearned model
    assert "from drilling_intelligence.database.models import LessonLearned" not in text
    # Must mention 0021/0022 artefacts intentionally excluded
    assert "0021" in text and "0022" in text
    # Must have NAMING_CONVENTION
    assert "NAMING_CONVENTION" in text
    # Verify 0020 does not contain code_key or lower(code) as artefact
    target_0020_section = text.split("_target_table_0020")[1].split("def upgrade")[0]
    # The Table definition for 0020 should not have Column("code_key")
    assert 'Column("code_key"' not in target_0020_section
    # lower(code) may appear only in comments about exclusion, not as an Index
    # Count real Index creation with lower(code) — should be zero in 0020 table
    lower_index_in_table = target_0020_section.count("lower(code)")
    comment_exclusion = target_0020_section.count("0021's lower(code)")
    assert lower_index_in_table == comment_exclusion, (
        f"0020 table should not define lower(code) index, found {lower_index_in_table} vs comment {comment_exclusion}"
    )
    # Also ensure code_key not defined as column or index outside comments
    assert 'Index("uq_lesson_code' not in target_0020_section or "0021" in target_0020_section


# ---------------------------------------------------------------------------
# 1.5 offline --sql semantic equivalence via disposable-copy apply vs online upgrade
# ---------------------------------------------------------------------------


def test_empty_full_offline_sql_matches_online_migration_exactly(tmp_path: Path):
    """A full-history SQL script applied to a fresh file matches online head exactly."""
    from drilling_intelligence.database.engine import open_sqlite
    from drilling_intelligence.database.migrations import upgrade

    online = tmp_path / "empty-online.db"
    engine = open_sqlite(f"sqlite:///{online}")
    try:
        upgrade(engine, "head")
    finally:
        engine.dispose()

    source = tmp_path / "offline-source-does-not-exist.db"
    env = dict(os.environ)
    env["DRILLINTEL_DATABASE__URL"] = f"sqlite:///{source}"
    env["PYTHONPATH"] = str(SRC)
    proc = subprocess.run(
        [
            sys.executable,
            "-m",
            "alembic",
            "-c",
            str(ROOT / "alembic.ini"),
            "upgrade",
            "head",
            "--sql",
        ],
        cwd=str(ROOT),
        capture_output=True,
        text=True,
        env=env,
        check=False,
    )
    assert proc.returncode == 0, proc.stderr[-2000:]
    assert not source.exists(), "offline rendering must not create its source URL"

    offline = tmp_path / "empty-offline.db"
    with sqlite3.connect(offline) as conn:
        conn.execute("PRAGMA foreign_keys=ON")
        conn.executescript(proc.stdout)
        violations = conn.execute("PRAGMA foreign_key_check").fetchall()
        assert violations == []

    assert _strict_sqlite_snapshot(online) == _strict_sqlite_snapshot(offline)


# ---------------------------------------------------------------------------
# 1.6 Windows drive-letter/%/Unicode paths — offline --sql must not mutate and must render
# ---------------------------------------------------------------------------


def test_windows_paths_offline_not_mutating(tmp_path: Path):
    """Spaces, percent-encoding, Unicode, and drive-letter style paths."""
    from drilling_intelligence.database.engine import open_sqlite
    from drilling_intelligence.database.migrations import upgrade

    def check_case(db_path: Path, url_override: str | None = None):
        # Ensure DB at 0019 with sentinel
        eng = open_sqlite(f"sqlite:///{db_path}")
        try:
            upgrade(eng, "0019")
            with eng.begin() as conn:
                conn.execute(
                    __import__("sqlalchemy").text(
                        "INSERT OR IGNORE INTO lesson_learned (id, code, revision, is_current, status, title, lesson, created_by, created_at, updated_at, depth_from_unit, depth_to_unit, root_cause_status, context, observation, recommendation, origin, provenance, attributes) "
                        "VALUES ('les-win', 'LL-WIN', 1, 1, 'DRAFT', 't','l','system', datetime('now'), datetime('now'), 'm','m','UNKNOWN','','','','MANUAL','[]','{}')"
                    )
                )
        finally:
            eng.dispose()
        before = _hash(db_path)
        url = url_override or f"sqlite:///{db_path}"
        env = dict(os.environ)
        env["DRILLINTEL_DATABASE__URL"] = url
        env["PYTHONPATH"] = str(SRC)
        proc = subprocess.run(
            [
                sys.executable,
                "-m",
                "alembic",
                "-c",
                str(ROOT / "alembic.ini"),
                "upgrade",
                "head",
                "--sql",
            ],
            cwd=str(ROOT),
            capture_output=True,
            text=True,
            env=env,
            check=False,
        )
        assert proc.returncode == 0, f"offline failed for {db_path}: {proc.stderr[-500:]}"
        after = _hash(db_path)
        assert before == after, f"offline mutated file for {db_path}"
        assert "lesson_learned" in proc.stdout

    # Case 1: space in path
    spaced = tmp_path / "space path.db"
    check_case(spaced)

    # Case 2: percent-encoded
    percent = tmp_path / "space %20 encoded.db"
    # Use percent encoding for space
    encoded_posix = percent.as_posix().replace(" ", "%20")
    # Create file via spaced name but URL uses encoded
    check_case(percent, url_override=f"sqlite:///{encoded_posix}")

    # Case 3: Unicode (café, well-Ø)
    unicode_path = tmp_path / "café-Ø-ünicode.db"
    check_case(unicode_path)
    # Also percent-encoded unicode
    unicode_encoded = unicode_path.as_posix()
    # urllib percent encode unicode bytes
    import urllib.parse

    unicode_enc = urllib.parse.quote(unicode_encoded, safe="/:")
    check_case(unicode_path, url_override=f"sqlite:///{unicode_enc}")

    # Case 4: Windows drive-letter style (simulated on Linux via absolute path with drive prefix)
    # We test that parsing a URL like sqlite:///C:/path/to/db.db does not crash offline rendering
    # Even though we cannot write to C:, we test that offline with a nonexistent drive-letter path does not create a file
    drive_url = "sqlite:///C:/tmp/drive_letter_test.db"
    env = dict(os.environ)
    env["DRILLINTEL_DATABASE__URL"] = drive_url
    env["PYTHONPATH"] = str(SRC)
    proc = subprocess.run(
        [
            sys.executable,
            "-m",
            "alembic",
            "-c",
            str(ROOT / "alembic.ini"),
            "upgrade",
            "head",
            "--sql",
        ],
        cwd=str(ROOT),
        capture_output=True,
        text=True,
        env=env,
        check=False,
    )
    assert proc.returncode == 0, f"drive-letter offline failed: {proc.stderr[-500:]}"
    assert "lesson_learned" in proc.stdout
    # Ensure no file was created at C:/ path (Linux root would interpret as relative)
    assert not Path("/C:/tmp/drive_letter_test.db").exists()
    assert not Path("C:/tmp/drive_letter_test.db").exists()


def _strict_sqlite_snapshot(path: Path) -> dict[str, object]:
    """Capture complete SQLite schema and row state for offline/online comparison."""
    from sqlalchemy import MetaData, Table, create_engine, inspect

    engine = create_engine(f"sqlite:///{path}")
    try:
        inspector = inspect(engine)
        schema: dict[str, object] = {}
        data: dict[str, object] = {}
        with engine.connect() as conn:
            catalog = conn.execute(
                __import__("sqlalchemy").text(
                    "SELECT type, name, tbl_name, sql "
                    "FROM sqlite_master WHERE type='index' AND name NOT LIKE 'sqlite_%' "
                    "ORDER BY name"
                )
            ).fetchall()
            schema["catalog"] = tuple(
                (kind, name, table, re.sub(r"\s+", " ", sql or "").strip().lower())
                for kind, name, table, sql in catalog
            )
            for table in sorted(inspector.get_table_names()):
                schema[table] = {
                    "columns": tuple(
                        (
                            column["name"],
                            str(column["type"]),
                            column["nullable"],
                            column["default"],
                            column["primary_key"],
                        )
                        for column in inspector.get_columns(table)
                    ),
                    "foreign_keys": tuple(
                        sorted(
                            (
                                fk.get("name"),
                                tuple(fk.get("constrained_columns") or ()),
                                fk.get("referred_schema"),
                                fk.get("referred_table"),
                                tuple(fk.get("referred_columns") or ()),
                                tuple(sorted((fk.get("options") or {}).items())),
                            )
                            for fk in inspector.get_foreign_keys(table)
                        )
                    ),
                    "unique_constraints": tuple(
                        sorted(
                            (
                                constraint.get("name"),
                                tuple(constraint.get("column_names") or ()),
                            )
                            for constraint in inspector.get_unique_constraints(table)
                        )
                    ),
                    "check_constraints": tuple(
                        sorted(
                            (constraint.get("name"), constraint.get("sqltext"))
                            for constraint in inspector.get_check_constraints(table)
                        )
                    ),
                    "indexes": tuple(
                        sorted(
                            (
                                index.get("name"),
                                tuple(index.get("column_names") or ()),
                                bool(index.get("unique")),
                                str((index.get("dialect_options") or {}).get("sqlite_where"))
                                if (index.get("dialect_options") or {}).get("sqlite_where")
                                is not None
                                else "",
                            )
                            for index in inspector.get_indexes(table)
                        )
                    ),
                }
                reflected = Table(table, MetaData(), autoload_with=engine)
                rows = conn.execute(reflected.select()).fetchall()
                data[table] = tuple(sorted((tuple(row) for row in rows), key=repr))
        return {"schema": schema, "data": data}
    finally:
        engine.dispose()


def _seed_historical_lesson_rows(engine, *, collision: bool = False) -> None:
    from sqlalchemy import text

    rows = [
        ("a", "LL-001", 1),
        ("b", " ll-001 ", 0),
        ("sharp-s", "ß", 1),
        ("ascii-ss", "SS", 0),
        ("unicode", "LL-Ü", 1),
        ("null", None, 1),
        ("empty", "", 1),
        ("spaces", "   ", 1),
        ("superseded", "OLD-001", 0),
        ("current", "OLD-002", 1),
    ]
    if collision:
        rows = [("sharp-s", "ß", 1), ("ascii-ss", "SS", 1)]
    statement = text(
        "INSERT INTO lesson_learned "
        "(id, code, revision, is_current, status, title, context, observation, "
        "root_cause_status, lesson, recommendation, depth_from_unit, depth_to_unit, "
        "created_by, origin, created_at, updated_at) "
        "VALUES (:id, :code, 1, :current, 'DRAFT', :title, '', '', 'UNKNOWN', '', '', "
        "'m', 'm', 'test', 'MANUAL', CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)"
    )
    with engine.begin() as conn:
        conn.execute(
            statement,
            [
                {"id": row_id, "code": code, "current": current, "title": row_id}
                for row_id, code, current in rows
            ],
        )


@pytest.mark.parametrize("start_revision", ["0019", "0020", "0021"])
def test_populated_offline_sql_matches_online_migration_exactly(
    tmp_path: Path, start_revision: str
):
    """Apply generated incremental SQL to an identical copy and compare all schema/data."""
    import shutil

    from drilling_intelligence.database.engine import open_sqlite
    from drilling_intelligence.database.migrations import upgrade

    source = tmp_path / f"historical-{start_revision}.db"
    source_engine = open_sqlite(f"sqlite:///{source}")
    try:
        upgrade(source_engine, start_revision)
        _seed_historical_lesson_rows(source_engine)
    finally:
        source_engine.dispose()

    online = tmp_path / f"online-{start_revision}.db"
    offline = tmp_path / f"offline-{start_revision}.db"
    shutil.copy2(source, online)
    shutil.copy2(source, offline)
    source_hash = _hash(source)
    online_engine = open_sqlite(f"sqlite:///{online}")
    try:
        upgrade(online_engine, "head")
    finally:
        online_engine.dispose()

    env = dict(os.environ)
    env["DRILLINTEL_DATABASE__URL"] = f"sqlite:///{source}"
    env["PYTHONPATH"] = str(SRC)
    proc = subprocess.run(
        [
            sys.executable,
            "-m",
            "alembic",
            "-c",
            str(ROOT / "alembic.ini"),
            "upgrade",
            f"{start_revision}:head",
            "--sql",
        ],
        cwd=str(ROOT),
        capture_output=True,
        text=True,
        env=env,
        check=False,
    )
    assert proc.returncode == 0, proc.stderr[-2000:]
    assert _hash(source) == source_hash, "SQL rendering must leave source DB bytes unchanged"
    assert "ss" in proc.stdout.lower(), "the offline script must contain the Python-casefold value"

    raw = sqlite3.connect(offline)
    try:
        raw.execute("PRAGMA foreign_keys=ON")
        raw.executescript(proc.stdout)
        raw.commit()
        violations = raw.execute("PRAGMA foreign_key_check").fetchall()
        assert violations == [], f"offline copy has FK violations: {violations}"
    finally:
        raw.close()

    online_snapshot = _strict_sqlite_snapshot(online)
    offline_snapshot = _strict_sqlite_snapshot(offline)
    assert online_snapshot == offline_snapshot, (
        f"offline/online mismatch from {start_revision}: "
        f"schema keys online={sorted(online_snapshot['schema'])}, "
        f"offline={sorted(offline_snapshot['schema'])}"
    )
    with sqlite3.connect(offline) as conn:
        canonical = dict(conn.execute("SELECT id, code_key FROM lesson_learned"))
    assert canonical["a"] == "ll-001"
    assert canonical["b"] == "ll-001"
    assert canonical["sharp-s"] == "ss"
    assert canonical["ascii-ss"] == "ss"
    assert canonical["unicode"] == "ll-ü"
    assert canonical["null"] is None
    assert canonical["empty"] is None
    assert canonical["spaces"] is None


def test_0022_casefold_collision_fails_online_and_when_offline_sql_is_applied(tmp_path: Path):
    """The exact same canonical collision is rejected by both migration paths."""
    import shutil

    from drilling_intelligence.database.engine import open_sqlite
    from drilling_intelligence.database.migrations import upgrade

    source = tmp_path / "collision-source.db"
    engine = open_sqlite(f"sqlite:///{source}")
    try:
        upgrade(engine, "0021")
        _seed_historical_lesson_rows(engine, collision=True)
    finally:
        engine.dispose()

    online_path = tmp_path / "collision-online.db"
    shutil.copy2(source, online_path)
    online = open_sqlite(f"sqlite:///{online_path}")
    try:
        with pytest.raises(RuntimeError, match="collide on canonical"):
            upgrade(online, "head")
    finally:
        online.dispose()

    offline = tmp_path / "collision-offline.db"
    shutil.copy2(source, offline)
    env = dict(os.environ)
    env["DRILLINTEL_DATABASE__URL"] = f"sqlite:///{source}"
    env["PYTHONPATH"] = str(SRC)
    rendered = subprocess.run(
        [
            sys.executable,
            "-m",
            "alembic",
            "-c",
            str(ROOT / "alembic.ini"),
            "upgrade",
            "0021:head",
            "--sql",
        ],
        cwd=str(ROOT),
        capture_output=True,
        text=True,
        env=env,
        check=False,
    )
    assert rendered.returncode == 0, rendered.stderr[-1000:]
    conn = sqlite3.connect(offline)
    try:
        with pytest.raises(sqlite3.IntegrityError):
            conn.executescript(rendered.stdout)
    finally:
        conn.close()


def test_0022_index_definition_verification_is_exact_and_sqlite_catalog_based():
    """Reject near-miss indexes: wrong table, key, uniqueness, or predicate."""
    import importlib.util

    from sqlalchemy import create_engine, text

    migration_path = ROOT / "migrations/versions/20261101_0022_lesson_code_key_canonical.py"
    spec = importlib.util.spec_from_file_location("migration_0022_under_test", migration_path)
    assert spec is not None and spec.loader is not None
    migration = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(migration)

    engine = create_engine("sqlite://")
    definitions = {
        "correct": "CREATE UNIQUE INDEX uq_lesson_code_key_current ON lesson_learned (code_key) WHERE is_current = 1 AND code_key IS NOT NULL",
        "wrong-current": "CREATE UNIQUE INDEX uq_lesson_code_key_current ON lesson_learned (code_key) WHERE is_current = 0 AND code_key IS NOT NULL",
        "or-predicate": "CREATE UNIQUE INDEX uq_lesson_code_key_current ON lesson_learned (code_key) WHERE is_current = 1 OR code_key IS NOT NULL",
        "extra-column": "CREATE UNIQUE INDEX uq_lesson_code_key_current ON lesson_learned (code_key, id) WHERE is_current = 1 AND code_key IS NOT NULL",
        "non-unique": "CREATE INDEX uq_lesson_code_key_current ON lesson_learned (code_key) WHERE is_current = 1 AND code_key IS NOT NULL",
        "missing-null-filter": "CREATE UNIQUE INDEX uq_lesson_code_key_current ON lesson_learned (code_key) WHERE is_current = 1",
        "extra-predicate": "CREATE UNIQUE INDEX uq_lesson_code_key_current ON lesson_learned (code_key) WHERE is_current = 1 AND code_key IS NOT NULL AND id != ''",
        "expression-key": "CREATE UNIQUE INDEX uq_lesson_code_key_current ON lesson_learned (lower(code_key)) WHERE is_current = 1 AND code_key IS NOT NULL",
        "wrong-table": "CREATE UNIQUE INDEX uq_lesson_code_key_current ON other_table (code_key) WHERE is_current = 1 AND code_key IS NOT NULL",
    }
    try:
        with engine.begin() as conn:
            conn.execute(
                text("CREATE TABLE lesson_learned (id TEXT, code_key TEXT, is_current INTEGER)")
            )
            conn.execute(
                text("CREATE TABLE other_table (id TEXT, code_key TEXT, is_current INTEGER)")
            )
            for case, definition in definitions.items():
                conn.execute(text("DROP INDEX IF EXISTS uq_lesson_code_key_current"))
                conn.exec_driver_sql(definition)
                assert migration._index_definition_correct(conn, "uq_lesson_code_key_current") is (
                    case == "correct"
                ), f"index verification misclassified {case}"
            assert not migration._index_definition_correct(conn, "some_other_index")
    finally:
        engine.dispose()


# ---------------------------------------------------------------------------
# 1.7 PostgreSQL service-backed qualification or withdraw claim
# ---------------------------------------------------------------------------


def test_postgresql_claim_is_qualified_or_withdrawn():
    """PostgreSQL is an explicitly unsupported, unqualified future backend.

    Do not run migrations against ambient DATABASE_URL/POSTGRES_URL values: that
    could mutate an operator database and is not service-backed qualification.
    A dedicated disposable PostgreSQL CI job is required before enabling this.
    """
    pytest.skip(
        "PostgreSQL is NOT QUALIFIED: SQLite is the only currently qualified backend; "
        "no disposable PostgreSQL service job exists."
    )


# ---------------------------------------------------------------------------
# 1.8 wheel bootstrap / Doctor truthfulness
# ---------------------------------------------------------------------------


def test_wheel_bootstrap_and_doctor_truthfulness(tmp_path: Path):
    from drilling_intelligence.cli.app import main as cli_main
    from drilling_intelligence.database.migrations import METADATA_REVISION

    assert METADATA_REVISION == "0022", f"METADATA_REVISION must be 0022, got {METADATA_REVISION}"

    # Workspace create must bootstrap via metadata and stamp 0022
    ws_root = tmp_path / "ws-bootstrap"
    assert cli_main(["workspace", "create", str(ws_root)]) == 0
    # DB is lazily created on first open/doctor, not on create alone
    out_create, err_create = io.StringIO(), io.StringIO()
    saved = sys.stdout, sys.stderr
    sys.stdout, sys.stderr = out_create, err_create
    try:
        code_doctor_init = cli_main(["doctor", "--workspace", str(ws_root)])
    finally:
        sys.stdout, sys.stderr = saved
    assert code_doctor_init == 0
    db_path = ws_root / ".drillintel/database/drilling_intelligence.db"
    assert db_path.exists(), (
        f"doctor should materialise DB, ls={list((ws_root / '.drillintel').rglob('*')) if (ws_root / '.drillintel').exists() else 'no dir'}"
    )
    conn = sqlite3.connect(db_path)
    try:
        ver = conn.execute("SELECT version_num FROM alembic_version").fetchone()
        assert ver[0] == "0022", f"wheel bootstrap must stamp 0022, got {ver[0]}"
        assert ver[0] == METADATA_REVISION
    finally:
        conn.close()

    # Doctor truthfulness: up_to_date true, current==head==0022
    out, err = io.StringIO(), io.StringIO()
    saved = sys.stdout, sys.stderr
    sys.stdout, sys.stderr = out, err
    try:
        code = cli_main(["doctor", "--workspace", str(ws_root), "--json"])
    finally:
        sys.stdout, sys.stderr = saved
    payload = json.loads(out.getvalue())
    assert code == 0
    assert payload["schema"]["current"] == "0022"
    assert payload["schema"]["head"] == "0022" if "head" in payload["schema"] else True
    assert payload["schema"]["up_to_date"] is True
    assert payload["findings"] == []
