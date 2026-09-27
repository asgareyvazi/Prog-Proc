"""one superseding revision per calculation

Revision ID: 0012
Revises: 0011
Create Date: 2026-09-27

``calculation.supersedes_id`` carried a foreign key and nothing else, so the rule that a
calculation chain has exactly one leaf was enforced only by an application pre-check inside
``record_calculation``: look for an existing child, and refuse if one is there.  That check is
correct for the single writer ADR-0003 describes, and useless for two of them - both can look,
both can see nothing, and both can insert.  Measured with two independent sessions on the same
SQLite file, **7 of 8 attempts left the parent with two children**, each carrying a different
``identity_key`` so the existing unique index did not catch it, and both reporting themselves
current.  Nothing in the row set says which of the two is the revision to follow.

No application-level check can close that: the gap is between the read and the write, and it is
only the database that sees both.  So the invariant moves to the database, as a partial unique
index - the same mechanism ``uq_document_version_one_current`` and the programme/procedure
current-revision indexes already use.  The loser now fails on a constraint instead of quietly
forking the chain, and a chain that was already forked before this migration cannot be created
again.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0012"
down_revision = "0011"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_index(
        "uq_calculation_one_superseding_revision",
        "calculation",
        ["supersedes_id"],
        unique=True,
        sqlite_where=sa.text("supersedes_id IS NOT NULL"),
        postgresql_where=sa.text("supersedes_id IS NOT NULL"),
    )


def downgrade() -> None:
    op.drop_index("uq_calculation_one_superseding_revision", table_name="calculation")
