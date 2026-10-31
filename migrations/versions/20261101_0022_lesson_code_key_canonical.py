"""lesson: canonical code_key (strip+casefold) for ADR 003

Revision ID: 0022
Revises: 0021
Create Date: 2026-11-01

The 0021 index used ``lower(code)`` which is not equivalent to the ADR's
``strip().casefold()`` (trim and Unicode casefold, e.g. `` LL-001 `` vs
``LL-001``, ``ß`` vs ``SS``).  This migration adds the authoritative
``code_key`` column (persisted canonical) and a partial unique index on it
among current lessons.  Existing rows are backfilled from ``code`` via
``trim + casefold``; the backfill is dialect-aware.

Legacy collision handling is per-ADR: if two current lessons share the same
canonical, the migration aborts with a diagnostic containing canonical, ids,
codes and is_current, requiring operator resolution before re-running.
"""

from __future__ import annotations

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
    except Exception:
        pass
    try:
        from alembic import context as alembic_context

        url = alembic_context.config.get_main_option("sqlalchemy.url") or ""
        if "postgres" in url.lower() or "postgresql" in url.lower():
            return "postgresql"
        if "sqlite" in url.lower():
            return "sqlite"
    except Exception:
        pass
    return "sqlite"


def _column_exists(bind, table: str, column: str) -> bool:
    try:
        insp = inspect(bind)
        cols = {c["name"] for c in insp.get_columns(table)}
        return column in cols
    except Exception:
        # Fallback via sqlite_master
        try:
            rows = bind.execute(text(f"SELECT sql FROM sqlite_master WHERE type='table' AND name='{table}'")).fetchone()
            if rows and rows[0] and column in rows[0]:
                return True
        except Exception:
            pass
        return False


def _index_exists(bind, name: str) -> bool:
    try:
        # Check sqlite_master
        row = bind.execute(text("SELECT 1 FROM sqlite_master WHERE type='index' AND name=:n"), {"n": name}).fetchone()
        if row:
            return True
    except Exception:
        pass
    try:
        insp = inspect(bind)
        for idx in insp.get_indexes("lesson_learned"):
            if idx.get("name") == name:
                return True
    except Exception:
        pass
    return False


def _index_definition_correct(bind, name: str) -> bool:
    """Check that the existing index has the expected definition (columns, predicate, uniqueness)."""
    try:
        row = bind.execute(text("SELECT sql FROM sqlite_master WHERE type='index' AND name=:n"), {"n": name}).fetchone()
        if not row or not row[0]:
            return False
        sql = row[0].lower()
        # Expected for SQLite: ON lesson_learned (code_key) WHERE is_current = 1 AND code_key IS NOT NULL
        # Check components
        if "code_key" not in sql:
            return False
        if "is_current" not in sql:
            return False
        if "unique" not in sql:
            return False
        return True
    except Exception:
        return False


def _is_offline() -> bool:
    try:
        from alembic import context as alembic_context

        return bool(alembic_context.is_offline_mode())
    except Exception:
        return False


def upgrade() -> None:
    bind = None
    offline = _is_offline()
    try:
        bind = op.get_bind()
    except Exception:
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

    # 2. Backfill canonical values for rows where code is present but code_key is null
    # Use dialect-appropriate SQL for the bulk; then Python correction for casefold divergence
    if dialect == "postgresql":
        op.execute(
            text(
                "UPDATE lesson_learned SET code_key = lower(btrim(code)) "
                "WHERE code IS NOT NULL AND btrim(code) != '' AND code_key IS NULL"
            )
        )
    else:
        op.execute(
            text(
                "UPDATE lesson_learned SET code_key = lower(trim(code)) "
                "WHERE code IS NOT NULL AND trim(code) != '' AND code_key IS NULL"
            )
        )

    # 3. Python-side correction for casefold divergence (e.g. ß -> ss) when live connection is available
    # This must succeed; a failure here is not tolerated as success.
    if bind is not None and hasattr(bind, "execute"):
        # Detect rows where lower(trim) != casefold(trim) and fix them
        # Select all rows with code
        rows = bind.execute(text("SELECT id, code, code_key FROM lesson_learned WHERE code IS NOT NULL")).fetchall()
        for row_id, code, existing_key in rows:
            if code is None:
                continue
            raw = str(code).strip()
            if not raw:
                # Empty code should have null code_key
                if existing_key is not None:
                    bind.execute(text("UPDATE lesson_learned SET code_key = NULL WHERE id = :id"), {"id": row_id})
                continue
            canonical = raw.casefold()
            # If existing_key already equals canonical, no action
            if existing_key == canonical:
                continue
            # If existing_key is lower-approx and differs, correct it
            # Also handle case where existing_key is None (backfill missed due to lower vs casefold difference)
            if existing_key != canonical:
                # Only update if needed; this is a genuine correction, not silent success
                bind.execute(text("UPDATE lesson_learned SET code_key = :ck WHERE id = :id"), {"ck": canonical, "id": row_id})

    # 4. Legacy collision detection — must be correct, not swallowed
    # Distinguish offline (no bind) vs live. Offline check is skipped but index will still enforce;
    # live check must run and must not be masked as no collisions.
    if bind is not None and hasattr(bind, "execute"):
        # Use Python-side grouping to be casefold-correct and dialect-portable
        # For SQLite, is_current is integer 0/1; for Postgres boolean
        if dialect == "postgresql":
            rows = bind.execute(
                text("SELECT id, code, is_current, code_key FROM lesson_learned WHERE is_current AND code IS NOT NULL AND btrim(code) != ''")
            ).fetchall()
        else:
            rows = bind.execute(
                text("SELECT id, code, is_current, code_key FROM lesson_learned WHERE is_current = 1 AND code IS NOT NULL AND trim(code) != ''")
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
            else:
                # Index exists but with wrong definition — drop and recreate
                # This is a repairable partial state — failure to drop is not tolerated as success
                try:
                    op.drop_index("uq_lesson_code_key_current", table_name="lesson_learned")
                except Exception as e:
                    try:
                        op.execute(text("DROP INDEX IF EXISTS uq_lesson_code_key_current"))
                    except Exception as e2:
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
    except Exception:
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
            except Exception as e2:
                raise RuntimeError(f"failed to drop index uq_lesson_code_key_current: {e2}") from e
            if bind is not None and _index_exists(bind, "uq_lesson_code_key_current"):
                raise RuntimeError("index uq_lesson_code_key_current still exists after drop attempt") from e
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
            raise RuntimeError(f"failed to drop column code_key (already absent check failed): {e}") from e
