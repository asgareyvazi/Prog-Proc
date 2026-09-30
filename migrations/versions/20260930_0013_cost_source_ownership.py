"""cost rows gain source ownership

Revision ID: 0013
Revises: 0012
Create Date: 2026-09-30

``cost_item`` had no link to the artefact that produced a line, and no ``is_current`` flag.  That
was harmless while cost lines were only ever typed in by hand, and it became a defect the moment a
promotion writer existed: ``CostRepository`` identifies a line by its content, so a corrected sheet
does not update the line it corrects - the changed amount is a different content, therefore a
different identity, therefore a second row.  Both rows stayed ``CURRENT``, and a reader totalling
the well's actuals counted the line twice.

Measured on the real ingest path: a ledger totalling NOK 2 060 500 in actuals, with one figure
corrected and the file re-imported, totals **4 060 499** afterwards - four lines, five ``CURRENT``
rows, one line counted at both its old and its new value.  Nothing in the row set said which of the
two was the statement to follow.

The fix is the same one every other promoted domain already uses: the row belongs to the document
version that produced it, a newer version stands the previous one down, and the stood-down row stays
in the database as history rather than being edited.  So this adds source ownership to the table
rather than inventing a cost-specific rule.

This migration adds the columns and their indexes but not the two foreign keys, and that gap is
closed by 0016 rather than left open.  The reasoning here at the time was that SQLite cannot add a
constraint to an existing table without rebuilding it, that a rebuild needs a live connection to
reflect the table, and that reflection breaks ``alembic upgrade head --sql`` - an invariant this
repository tests, so it won.  The middle step was the wrong one: alembic only reflects when nobody
tells it the destination schema, and supplying ``copy_from`` makes the rebuild render offline.  So
the constraint could have been attached after all, and 0016 attaches it.  Until 0016 runs, a
database migrated up from 0012 carries ``cost_item.document_id`` and ``document_version_id``
without a database-level constraint while a workspace built fresh by ``create_all`` has them -
which is exactly the divergence ``test_migration_0004.py`` compares for and reported.

Existing rows are backfilled ``is_current = 1``.  Every row written before this migration was the
only statement of its line, so marking it current is what the data already meant, and no row's
figures, codes, currencies or provenance are touched.  Manually entered lines keep all three new
columns empty or defaulted and are never superseded or swept, because they belong to no artefact.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0013"
down_revision = "0012"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "cost_item",
        sa.Column("document_id", sa.String(length=36), nullable=True),
    )
    op.add_column(
        "cost_item",
        sa.Column("document_version_id", sa.String(length=36), nullable=True),
    )
    # NOT NULL needs a default to be added to a populated SQLite table.  ``1`` is the truth the
    # existing rows already had: each was the only statement of its line.
    op.add_column(
        "cost_item",
        sa.Column(
            "is_current",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("1"),
        ),
    )
    op.create_index("ix_cost_well_current", "cost_item", ["well_id", "is_current"])
    op.create_index("ix_cost_version", "cost_item", ["document_version_id"])


def downgrade() -> None:
    op.drop_index("ix_cost_version", table_name="cost_item")
    op.drop_index("ix_cost_well_current", table_name="cost_item")
    # Dropping a column is a table rebuild on SQLite, so this needs batch mode.  A downgrade is run
    # against a live database by definition - nobody reviews one as SQL first - so batch mode is
    # fine here even though the upgrade avoids it.
    with op.batch_alter_table("cost_item") as batch:
        batch.drop_column("is_current")
        batch.drop_column("document_version_id")
        batch.drop_column("document_id")
