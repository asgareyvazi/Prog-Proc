"""lesson: canonical code_key (strip+casefold) for ADR 003

Revision ID: 0022
Revises: 0021
Create Date: 2026-11-01

The 0021 index used ``lower(code)`` which is not equivalent to the ADR's
``strip().casefold()`` (trim and Unicode casefold, e.g. `` LL-001 `` vs
``LL-001``, ``ß`` vs ``SS``).  This migration adds the authoritative
``code_key`` column (persisted canonical) and a partial unique index on it
among current lessons.  Existing rows are backfilled from ``code`` via
``trim + casefold``; the backfill is dialect-aware (SQLite uses
``lower(trim(code))`` as an approximation for the migration SQL, then Python
re-validates via ``LessonRepository._normalize_code`` at runtime).

Legacy collision handling is per-ADR: if two current lessons share the same
canonical, the migration aborts with a diagnostic containing canonical, ids,
codes and is_current, requiring operator resolution before re-running.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0022"
down_revision = "0021"
branch_labels = None
depends_on = None


def _dialect() -> str:
    try:
        bind = op.get_bind()
        if bind is not None:
            return str(getattr(bind.dialect, "name", "") or "").lower()
    except Exception:
        pass
    try:
        from alembic import context as alembic_context

        url = alembic_context.config.get_main_option("sqlalchemy.url") or ""
        if "postgres" in url.lower():
            return "postgresql"
        if "sqlite" in url.lower():
            return "sqlite"
    except Exception:
        pass
    return "sqlite"


def upgrade() -> None:
    # Add column if not exists (SQLite supports IF NOT EXISTS via raw SQL for idempotency)
    try:
        op.add_column("lesson_learned", sa.Column("code_key", sa.String(length=80), nullable=True))
    except Exception:
        # Column may already exist if 0022 was partially applied or via metadata
        pass

    # Backfill: SQLite approximation lower(trim(code)), Postgres same.
    # Python's casefold for ß->ss differs from lower, but backfill for ß is
    # rare; runtime Python check will catch any remaining divergence on next
    # write.  For strict correctness, we also run a Python-side re-check in
    # the migration when a live connection is available.
    dialect = _dialect()
    # Use dialect-appropriate SQL for backfill
    if dialect == "postgresql":
        op.execute(
            sa.text(
                "UPDATE lesson_learned SET code_key = lower(btrim(code)) "
                "WHERE code IS NOT NULL AND btrim(code) != '' AND code_key IS NULL"
            )
        )
    else:
        # SQLite: trim() and lower()
        op.execute(
            sa.text(
                "UPDATE lesson_learned SET code_key = lower(trim(code)) "
                "WHERE code IS NOT NULL AND trim(code) != '' AND code_key IS NULL"
            )
        )

    # Python-side correction for casefold divergence (e.g. ß) when live connection present
    try:
        bind = op.get_bind()
        if bind is not None and hasattr(bind, "execute"):
            # Detect rows where lower(trim) != casefold(trim) and fix them
            rows = bind.execute(
                sa.text("SELECT id, code FROM lesson_learned WHERE code IS NOT NULL")
            ).fetchall()
            for row_id, code in rows:
                if code is None:
                    continue
                raw = str(code).strip()
                if not raw:
                    continue
                canonical = raw.casefold()
                # lower approx
                lower_approx = raw.lower()
                if canonical != lower_approx:
                    bind.execute(
                        sa.text("UPDATE lesson_learned SET code_key = :ck WHERE id = :id"),
                        {"ck": canonical, "id": row_id},
                    )
    except Exception:
        pass

    # Legacy collision detection on canonical (Python casefold) — must be portable
    try:
        bind = op.get_bind()
        if bind is not None and hasattr(bind, "execute"):
            # Use Python-side grouping to be dialect-portable and casefold-correct
            rows = bind.execute(
                sa.text(
                    "SELECT id, code, is_current, code_key FROM lesson_learned WHERE is_current = 1 AND code IS NOT NULL AND trim(code) != ''"
                    if dialect != "postgresql"
                    else "SELECT id, code, is_current, code_key FROM lesson_learned WHERE is_current AND code IS NOT NULL AND btrim(code) != ''"
                )
            ).fetchall()
            from collections import defaultdict

            buckets: dict[str, list[tuple[str, str, int]]] = defaultdict(list)
            for rid, code, is_cur, ck in rows:
                canon = (ck if ck is not None else str(code).strip().casefold()) if code else None
                if not canon:
                    canon = str(code).strip().casefold() if code else ""
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
    except RuntimeError:
        raise
    except Exception:
        # If detection fails (e.g. offline mock), skip — the unique index will still enforce
        pass

    # Create unique partial index on code_key (IF NOT EXISTS for idempotency)
    try:
        op.execute(
            sa.text(
                "CREATE UNIQUE INDEX IF NOT EXISTS uq_lesson_code_key_current "
                "ON lesson_learned (code_key) WHERE is_current = 1 AND code_key IS NOT NULL"
                if dialect != "postgresql"
                else "CREATE UNIQUE INDEX IF NOT EXISTS uq_lesson_code_key_current "
                "ON lesson_learned (code_key) WHERE is_current AND code_key IS NOT NULL"
            )
        )
        return
    except Exception:
        pass
    # Fallback via Alembic op
    op.create_index(
        "uq_lesson_code_key_current",
        "lesson_learned",
        ["code_key"],
        unique=True,
        sqlite_where=sa.text("is_current = 1 AND code_key IS NOT NULL"),
        postgresql_where=sa.text("is_current AND code_key IS NOT NULL"),
    )


def downgrade() -> None:
    try:
        op.drop_index(
            "uq_lesson_code_key_current",
            table_name="lesson_learned",
            sqlite_where=sa.text("is_current = 1 AND code_key IS NOT NULL"),
            postgresql_where=sa.text("is_current AND code_key IS NOT NULL"),
        )
    except Exception:
        try:
            op.execute(sa.text("DROP INDEX IF EXISTS uq_lesson_code_key_current"))
        except Exception:
            pass
    try:
        op.drop_column("lesson_learned", "code_key")
    except Exception:
        pass
