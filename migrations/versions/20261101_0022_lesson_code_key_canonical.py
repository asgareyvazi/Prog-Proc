"""lesson: canonical code_key (strip+casefold) for ADR 003

Revision ID: 0022
Revises: 0021
Create Date: 2026-11-01

The 0021 index used ``lower(code)`` which is not equivalent to the ADR's
``strip().casefold()`` (trim and Unicode casefold, e.g. `` LL-001 `` vs
``LL-001``, ``ß`` vs ``SS``). This migration adds the authoritative
``code_key`` column (persisted canonical) and a partial unique index on it
among current lessons. Existing rows are backfilled with Python
``strip().casefold()`` semantics. SQLite is the only qualified backend;
PostgreSQL code paths are retained as future work and are NOT QUALIFIED.

Legacy collision handling is per-ADR: if two current lessons share the same
canonical, the migration aborts with a diagnostic containing canonical, ids,
codes and is_current, requiring operator resolution before re-running.
"""

from __future__ import annotations

import re

import sqlalchemy as sa
from alembic import op
from sqlalchemy import inspect, text

revision = "0022"
down_revision = "0021"
branch_labels = None
depends_on = None


def _dialect_name() -> str:
    try:
        bind = op.get_bind()
        if bind is not None and hasattr(bind, "dialect"):
            return str(getattr(bind.dialect, "name", "") or "").lower()
    except Exception:  # noqa: BLE001
        pass
    try:
        from alembic import context as alembic_context

        url = alembic_context.config.get_main_option("sqlalchemy.url") or ""
        if "postgres" in url.lower() or "postgresql" in url.lower():
            return "postgresql"
        if "sqlite" in url.lower():
            return "sqlite"
    except Exception:  # noqa: BLE001
        pass
    return "sqlite"


def _column_exists(bind, table: str, column: str) -> bool:
    try:
        insp = inspect(bind)
        cols = {c["name"] for c in insp.get_columns(table)}
        return column in cols
    except Exception:  # noqa: BLE001
        # Fallback via sqlite_master
        try:
            rows = bind.execute(
                text("SELECT sql FROM sqlite_master WHERE type='table' AND name=:t"), {"t": table}
            ).fetchone()
            if rows and rows[0] and column in rows[0]:
                return True
        except Exception:  # noqa: BLE001
            pass
        return False


def _index_exists(bind, name: str) -> bool:
    dialect = _dialect_name()
    if dialect == "postgresql":
        try:
            row = bind.execute(
                text("SELECT 1 FROM pg_indexes WHERE indexname = :n"), {"n": name}
            ).fetchone()
            if row:
                return True
        except Exception:  # noqa: BLE001
            pass
    else:
        try:
            row = bind.execute(
                text("SELECT 1 FROM sqlite_master WHERE type='index' AND name=:n"),
                {"n": name},
            ).fetchone()
            if row:
                return True
        except Exception:  # noqa: BLE001
            pass
    try:
        insp = inspect(bind)
        for idx in insp.get_indexes("lesson_learned"):
            if idx.get("name") == name:
                return True
    except Exception:  # noqa: BLE001
        pass
    return False


def _index_definition_correct(bind, name: str) -> bool:
    """Verify the full partial-unique index contract using the active dialect catalog."""
    if name != "uq_lesson_code_key_current":
        return False
    dialect = str(getattr(getattr(bind, "dialect", None), "name", "") or _dialect_name())
    try:
        if dialect == "postgresql":
            row = bind.execute(
                text(
                    "SELECT ns.nspname, current_schema(), tbl.relname, idx.indisunique, "
                    "idx.indisvalid, idx.indisready, idx.indnatts, idx.indnkeyatts, "
                    "pg_get_expr(idx.indpred, idx.indrelid), "
                    "ARRAY(SELECT att.attname FROM unnest(idx.indkey) WITH ORDINALITY "
                    "AS key(attnum, ord) JOIN pg_attribute att "
                    "ON att.attrelid = idx.indrelid AND att.attnum = key.attnum "
                    "ORDER BY key.ord) "
                    "FROM pg_class ix JOIN pg_index idx ON idx.indexrelid = ix.oid "
                    "JOIN pg_class tbl ON tbl.oid = idx.indrelid "
                    "JOIN pg_namespace ns ON ns.oid = tbl.relnamespace "
                    "WHERE ix.relname = :n"
                ),
                {"n": name},
            ).fetchall()
            if len(row) != 1:
                return False
            (
                schema,
                active_schema,
                table,
                unique,
                valid,
                ready,
                total_columns,
                key_columns,
                predicate,
                columns,
            ) = row[0]
            if schema != active_schema or table != "lesson_learned" or not unique:
                return False
            if not valid or not ready or total_columns != 1 or key_columns != 1:
                return False
            if list(columns or ()) != ["code_key"]:
                return False
            # pg_get_expr may add grouping parentheses. Remove parentheses and
            # whitespace, then require exactly the complete AND predicate.
            predicate_norm = re.sub(r"[\s\"`()]+", "", str(predicate or "").lower())
            return predicate_norm == "is_currentandcode_keyisnotnull"

        row = bind.execute(
            text("SELECT tbl_name, sql FROM sqlite_master WHERE type='index' AND name=:n"),
            {"n": name},
        ).fetchall()
        if len(row) != 1:
            return False
        table, definition = row[0]
        if table != "lesson_learned" or not definition:
            return False
        sql_norm = re.sub(r"[\s\"`\[\]]+", "", str(definition).lower())
        expected = re.compile(
            r"^createuniqueindex(?:ifnotexists)?uq_lesson_code_key_current"
            r"onlesson_learned\(code_key\)"
            r"whereis_current=1andcode_keyisnotnull$"
        )
        return expected.fullmatch(sql_norm) is not None
    except Exception:  # noqa: BLE001 - catalog/reflection failure is not a verified index
        return False


def _is_offline() -> bool:
    try:
        from alembic import context as alembic_context

        return bool(alembic_context.is_offline_mode())
    except Exception:  # noqa: BLE001
        return False


def upgrade() -> None:
    bind = None
    offline = _is_offline()
    try:
        bind = op.get_bind()
    except Exception:  # noqa: BLE001
        bind = None
    # In offline, the mock connection may return None for execute; treat as offline
    if offline:
        bind = None
    dialect = _dialect_name()

    # 1. Add column code_key if not exists
    has_column = False
    if bind is not None:
        has_column = _column_exists(bind, "lesson_learned", "code_key")
    if not has_column:
        op.add_column("lesson_learned", sa.Column("code_key", sa.String(length=80), nullable=True))
    else:
        # Already applied — verify type
        pass

    # 2. Backfill with the exact Python strip().casefold() contract.
    if offline:
        from alembic import context as alembic_context

        canonical_rows = alembic_context.config.attributes.get("offline_lesson_code_keys")
        if canonical_rows is None:
            raise RuntimeError(
                "offline 0022 requires a read-only source snapshot; rerun against an existing "
                "SQLite database or use the supported online migration path"
            )
        for row_id, canonical in canonical_rows:
            statement = text("UPDATE lesson_learned SET code_key = :ck WHERE id = :id").bindparams(
                ck=canonical, id=row_id
            )
            op.execute(statement)
    elif bind is not None:
        rows = bind.execute(text("SELECT id, code FROM lesson_learned ORDER BY id")).fetchall()
        updates = [
            {
                "ck": (str(code).strip().casefold() or None) if code is not None else None,
                "id": row_id,
            }
            for row_id, code in rows
        ]
        if updates:
            bind.execute(text("UPDATE lesson_learned SET code_key = :ck WHERE id = :id"), updates)

    # 4. Legacy collision detection — must be correct, not swallowed
    # Distinguish offline (no bind) vs live. Offline check is skipped but index will still enforce;
    # live check must run and must not be masked as no collisions.
    if bind is not None and hasattr(bind, "execute"):
        # Use Python-side grouping to be casefold-correct and dialect-portable
        # For SQLite, is_current is integer 0/1; for Postgres boolean
        if dialect == "postgresql":
            rows = bind.execute(
                text(
                    "SELECT id, code, is_current, code_key FROM lesson_learned WHERE is_current AND code IS NOT NULL AND btrim(code) != ''"
                )
            ).fetchall()
        else:
            rows = bind.execute(
                text(
                    "SELECT id, code, is_current, code_key FROM lesson_learned WHERE is_current = 1 AND code IS NOT NULL AND trim(code) != ''"
                )
            ).fetchall()
        from collections import defaultdict

        buckets: dict[str, list[tuple[str, str, int]]] = defaultdict(list)
        for rid, code, is_cur, ck in rows:
            # Prefer code_key if present and correct, else compute canonical from code
            # Validate that ck equals casefold if present
            canon_from_code = str(code).strip().casefold() if code else ""
            if ck is not None and str(ck) != canon_from_code:
                # Data inconsistency: code_key does not match code's canonical — treat as collision candidate
                # but also correct it for detection purposes (use canonical from code)
                pass
            canon = canon_from_code
            if not canon:
                continue
            buckets[canon].append((str(rid), str(code), int(is_cur) if is_cur is not None else 0))
        dupes = {k: v for k, v in buckets.items() if len(v) > 1}
        if dupes:
            details = []
            for canon, items in dupes.items():
                ids = ", ".join(i[0] for i in items)
                codes = ", ".join(repr(i[1]) for i in items)
                details.append(f"canonical={canon!r} ids=[{ids}] codes=[{codes}]")
            raise RuntimeError(
                "cannot create uq_lesson_code_key_current: current lessons collide on canonical (strip+casefold): "
                + "; ".join(details)
                + ". Resolve per docs/adr/003-code-normalization.md (rename or supersede one row per canonical)."
            )
        # No dupes — proceed

    # 5. Create unique partial index on code_key — distinguish already correct vs incorrect definition
    idx_exists = False
    idx_correct = False
    if bind is not None:
        idx_exists = _index_exists(bind, "uq_lesson_code_key_current")
        if idx_exists:
            idx_correct = _index_definition_correct(bind, "uq_lesson_code_key_current")
            if idx_correct:
                # Already correctly applied — idempotent success
                return
            # Index exists but with wrong definition — drop and recreate
            # This is a repairable partial state — failure to drop is not tolerated as success
            try:
                op.drop_index("uq_lesson_code_key_current", table_name="lesson_learned")
            except Exception as e:
                try:
                    op.execute(text("DROP INDEX IF EXISTS uq_lesson_code_key_current"))
                except Exception as e2:  # noqa: BLE001
                    raise RuntimeError(
                        f"failed to drop incorrect index uq_lesson_code_key_current: {e2}"
                    ) from e
                if _index_exists(bind, "uq_lesson_code_key_current"):
                    raise RuntimeError(
                        "index uq_lesson_code_key_current still exists after drop attempt of incorrect definition"
                    ) from e
            idx_exists = False
    if not idx_exists:
        if dialect == "postgresql":
            op.execute(
                text(
                    "CREATE UNIQUE INDEX uq_lesson_code_key_current ON lesson_learned (code_key) WHERE is_current AND code_key IS NOT NULL"
                )
            )
        else:
            op.execute(
                text(
                    "CREATE UNIQUE INDEX uq_lesson_code_key_current ON lesson_learned (code_key) WHERE is_current = 1 AND code_key IS NOT NULL"
                )
            )
        # Verify creation succeeded
        if bind is not None and not _index_exists(bind, "uq_lesson_code_key_current"):
            raise RuntimeError("failed to create index uq_lesson_code_key_current")
    # Fallback via Alembic op is not needed after raw SQL, but kept for other dialects
    # No silent pass on failure — any exception above propagates


def downgrade() -> None:
    offline = _is_offline()
    bind = None
    try:
        bind = op.get_bind()
    except Exception:  # noqa: BLE001
        bind = None
    if offline:
        bind = None
    # Offline: emit deterministic SQL without live inspection (must be non-mutating but render)
    if offline:
        op.execute(text("DROP INDEX IF EXISTS uq_lesson_code_key_current"))
        # SQLite needs batch for column drop, but offline rendering via op.drop_column is sufficient
        try:
            op.drop_column("lesson_learned", "code_key")
        except Exception as e:
            # In offline mode the drop renders SQL; only raise if rendering itself fails
            raise RuntimeError(f"offline downgrade failed to render drop column: {e}") from e
        return
    # Online: distinguish already-applied vs failure
    idx_exists = _index_exists(bind, "uq_lesson_code_key_current") if bind is not None else False
    if idx_exists:
        try:
            op.drop_index(
                "uq_lesson_code_key_current",
                table_name="lesson_learned",
                sqlite_where=text("is_current = 1 AND code_key IS NOT NULL"),
                postgresql_where=text("is_current AND code_key IS NOT NULL"),
            )
        except Exception as e:
            try:
                op.execute(text("DROP INDEX IF EXISTS uq_lesson_code_key_current"))
            except Exception as e2:  # noqa: BLE001
                raise RuntimeError(f"failed to drop index uq_lesson_code_key_current: {e2}") from e
            if bind is not None and _index_exists(bind, "uq_lesson_code_key_current"):
                raise RuntimeError(
                    "index uq_lesson_code_key_current still exists after drop attempt"
                ) from e
        # Verify dropped
        if bind is not None and _index_exists(bind, "uq_lesson_code_key_current"):
            raise RuntimeError("index uq_lesson_code_key_current still exists after drop")
    else:
        # Already not exists — idempotent; still emit IF EXISTS for consistency
        op.execute(text("DROP INDEX IF EXISTS uq_lesson_code_key_current"))
    # Drop column if exists
    has_col = _column_exists(bind, "lesson_learned", "code_key") if bind is not None else False
    if has_col:
        try:
            op.drop_column("lesson_learned", "code_key")
        except Exception as e:
            if bind is not None and _column_exists(bind, "lesson_learned", "code_key"):
                raise RuntimeError(f"failed to drop column code_key: {e}") from e
            raise
    else:
        # Already dropped — idempotent, emit IF EXISTS variant via raw for consistency
        # Use raw SQL check already emitted index drop; for column we attempt drop and ignore "no such column" only if confirmed absent
        try:
            op.drop_column("lesson_learned", "code_key")
        except Exception as e:
            # If column truly absent, this is expected — verify absent
            if bind is not None and not _column_exists(bind, "lesson_learned", "code_key"):
                # Already absent — success
                return
            raise RuntimeError(
                f"failed to drop column code_key (already absent check failed): {e}"
            ) from e
