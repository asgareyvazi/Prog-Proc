"""lesson learned: global case-insensitive code uniqueness (M4)

Revision ID: 0021
Revises: 0020
Create Date: 2026-10-31

Codes are globally unique ignoring case among current lessons
(``is_current = 1``).  The existing ``uq_lesson_one_current`` is case-
sensitive, so ``LL-001`` and ``ll-001`` would collide only at the Python
layer.  This revision adds a DB-level partial unique index on
``LOWER(code)`` to make the invariant storage-layer enforced, matching the
application check in ``LessonRepository._ensure_code_unique`` (strip +
``casefold``).

Legacy data: if two current lessons already have the same code
case-insensitively, the migration refuses to create the index and reports the
conflicting ids.  The operator must resolve the collision before re-running
(``docs/adr/003-code-normalization.md`` — "legacy collision procedure"):
rename or supersede one of the rows so only one current row remains per
canonical code.

SQLite's ``LOWER()`` is used for the index; Python's ``casefold`` is the
authoritative application check for Unicode edge cases (e.g. ``ß`` vs ``SS``).
The index is partial (``WHERE is_current = 1 AND code IS NOT NULL``) so
superseded revisions and null codes are unrestricted.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0021"
down_revision = "0020"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # ``IF NOT EXISTS`` makes the migration idempotent when 0020's
    # ``copy_from=_table()`` already created the index (metadata now contains
    # ``uq_lesson_code_current_ci``).  Online we still want a diagnostic for
    # legacy collisions, so run that check when a live connection is present.
    is_offline = False
    try:
        from alembic import context as alembic_context

        is_offline = bool(alembic_context.is_offline_mode())
    except Exception:  # noqa: BLE001
        try:
            ctx = op.get_context()
            is_offline = bool(ctx is not None and getattr(ctx, "is_offline_mode", lambda: False)())
        except Exception:  # noqa: BLE001
            is_offline = False
    if not is_offline:
        # Online live checks: existence check to skip quickly, then legacy
        # collision check for case-insensitive duplicates.
        try:
            bind = op.get_bind()
        except Exception:  # noqa: BLE001
            bind = None
        if bind is not None and getattr(bind, "execute", None) is not None:
            try:
                exists = bind.execute(
                    sa.text(
                        "SELECT 1 FROM sqlite_master WHERE type='index' AND name='uq_lesson_code_current_ci'"
                    )
                ).fetchone()
                if exists:
                    return
            except Exception:  # noqa: BLE001
                pass
            try:
                dupes = bind.execute(
                    sa.text(
                        """
                        SELECT lower(trim(code)) AS cf,
                               count(*) AS c,
                               group_concat(id, ', ') AS ids,
                               group_concat(code, ', ') AS codes
                        FROM lesson_learned
                        WHERE is_current = 1 AND code IS NOT NULL AND trim(code) != ''
                        GROUP BY lower(trim(code))
                        HAVING c > 1
                        """
                    )
                ).fetchall()
            except Exception:  # noqa: BLE001
                dupes = []
            if dupes:
                details = "; ".join(
                    f"canonical={row[0]!r} ids=[{row[2]}] codes=[{row[3]}]" for row in dupes
                )
                raise RuntimeError(
                    "cannot create uq_lesson_code_current_ci: current lessons collide case-insensitively: "
                    + details
                    + ". Resolve per docs/adr/003-code-normalization.md (rename or supersede one row per canonical code)."
                )
    # Expression partial unique index on lower(code).  SQLite supports
    # ``CREATE UNIQUE INDEX ... ON (lower(code)) WHERE ...``; PostgreSQL
    # uses the same predicate with ``is_current`` boolean.
    # Use IF NOT EXISTS so the migration is safe to re-run after 0020's copy.
    try:
        op.execute(
            sa.text(
                "CREATE UNIQUE INDEX IF NOT EXISTS uq_lesson_code_current_ci "
                "ON lesson_learned (lower(code)) WHERE is_current = 1 AND code IS NOT NULL"
            )
        )
        return
    except Exception:  # noqa: BLE001
        pass
    op.create_index(
        "uq_lesson_code_current_ci",
        "lesson_learned",
        [sa.text("lower(code)")],
        unique=True,
        sqlite_where=sa.text("is_current = 1 AND code IS NOT NULL"),
        postgresql_where=sa.text("is_current AND code IS NOT NULL"),
    )


def downgrade() -> None:
    op.drop_index(
        "uq_lesson_code_current_ci",
        table_name="lesson_learned",
        sqlite_where=sa.text("is_current = 1 AND code IS NOT NULL"),
        postgresql_where=sa.text("is_current AND code IS NOT NULL"),
    )
