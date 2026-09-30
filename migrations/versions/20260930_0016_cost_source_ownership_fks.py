"""cost rows gain the source-ownership foreign keys

Revision ID: 0016
Revises: 0015
Create Date: 2026-09-30

0013 gave ``cost_item`` a ``document_id`` and a ``document_version_id`` and deliberately left them
without a database-level constraint, on the reasoning that SQLite cannot add a constraint to an
existing table, that adding one means rebuilding the table, and that a rebuild needs a live
connection to reflect the table - which breaks ``alembic upgrade head --sql``.  That last step was
the one that was wrong: alembic only needs to reflect the table when nobody tells it what the
destination schema is.  Supplying ``copy_from`` makes the rebuild render offline, so the reason for
leaving the constraint out no longer holds, and this migration closes the gap.

The gap was real rather than cosmetic.  ``tests/integration/test_migration_0004.py`` compares a
workspace built by ``create_all`` with one brought up by the migration chain, table by table, and
it reported ``cost_item`` differing: a fresh workspace carries
``fk_cost_item_document_id_document`` and ``fk_cost_item_document_version_id_document_version``,
and a migrated one does not.  Both accept the same writes today, because the promotion writer only
ever stores an id it has just read back.  The difference would only have shown up as behaviour -
deleting a document version sets the link to NULL in one workspace and leaves it pointing at a
row that no longer exists in the other - which is precisely the kind of divergence that test exists
to catch before it becomes two workspaces that answer differently.

The rebuild is a copy of ``cost_item`` with the two constraints present: the table is recreated,
the rows are copied across, and the old table is dropped.  Row content, ``status``, ``origin``,
``identity_key``, ``is_current`` and every index (``ix_cost_project_cbs``, ``ix_cost_version``,
``ix_cost_well_category``, ``ix_cost_well_current``) come through unchanged; the destination schema
is the model's own table, so the migration cannot drift from the models by describing the columns
itself.  The downgrade performs the same rebuild with the two constraints dropped, returning the
table to exactly the shape 0015 left it in.
"""

from __future__ import annotations

from alembic import op

from drilling_intelligence.database.models import Base

revision = "0016"
down_revision = "0015"
branch_labels = None
depends_on = None

#: The two constraints 0013 added columns for but could not attach.
_SOURCE_OWNERSHIP_FKS = (
    "fk_cost_item_document_id_document",
    "fk_cost_item_document_version_id_document_version",
)


def _table():
    """``cost_item`` as the models define it, which is what the table is rebuilt to match."""
    return Base.metadata.tables["cost_item"]


def upgrade() -> None:
    # ``copy_from`` supplies the destination schema, so alembic never reflects the live table and
    # ``alembic upgrade head --sql`` still renders the whole chain.
    with op.batch_alter_table("cost_item", copy_from=_table(), recreate="always"):
        pass


def downgrade() -> None:
    with op.batch_alter_table("cost_item", copy_from=_table(), recreate="always") as batch:
        for name in _SOURCE_OWNERSHIP_FKS:
            batch.drop_constraint(name, type_="foreignkey")
