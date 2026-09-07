"""the engineering record joins the evidence convention: origin, author, content identity, citation

Revision ID: 0005
Revises: 0004
Create Date: 2026-09-06

``calculation`` has been in the schema since 0001, and it is the right shape for an engineering number:
method and version, inputs with units, outputs, assumptions, a validation payload, an uncertainty block, a
status that says whether anyone has checked it, and a revision chain.  What it lacked is what everything in
this platform has had to carry since the knowledge layer, and since 0004 every operational record:
*evidence conventions*.  No ``origin``, so an extracted number and a hand-typed one were indistinguishable;
no ``created_by``, so "who wrote this" was not answerable; no ``document_version_id``, so a value read out
of a report could not name the report; and no identity key, so the only way to record a re-run was to append
a twin of the row.

That gap mattered as soon as the write path was built.  :meth:`EngineeringRepository.record_calculation` is
persistence only - the platform computes nothing, per the phase boundary - so the row's entire value is its
accountability, and a table that cannot record where a number came from cannot supply it.  This revision
adds six columns and a unique content-identity index.  Nothing is backfilled with an invented source: every
pre-existing row becomes ``MANUAL``/``system``, which is the same honest default 0003 chose for a
``knowledge_item`` of unknown parentage, and its citation columns stay NULL because a migration that guesses
a citation is worse than one that admits it does not know.

The two citation columns carry no foreign key, and that is the one deliberate exception to this schema's
convention.  SQLite cannot add a constraint to an existing table, so the only way to have it on a *migrated*
file is ``batch_alter_table``'s copy-and-move - which needs a live connection and so makes
``alembic upgrade --sql`` fail, taking away the way a DBA reviews a migration before running it; and adding
the key to the models without the migration would leave a freshly created workspace with a constraint an
upgraded one does not have, which is the kind of difference that shows up as two answers to one question.
So a dangling citation is caught where this platform already catches what a constraint cannot state:
:func:`drilling_intelligence.database.integrity.check_promoted_evidence` reports a derived row that cites
nothing, the document-version check in
:func:`drilling_intelligence.database.integrity.check_cross_well_links` reports a citation that cannot be
opened, ``doctor`` runs both through ``check_operational_integrity``, and the repository refuses an extracted
or derived record with no provenance at all.  A unique *index* rather than a table constraint, for the same reason and
one more: ``identity_key`` is nullable, a NULL is distinct from every other NULL on both servers, and a
hand-entered one-off with no content identity to claim must not block the next one.

"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0005"
down_revision = "0004"
branch_labels = None
depends_on = None


def _columns() -> list[sa.Column]:
    """The columns this revision adds, in order, each with the server default its backfill needs.

    Written as a function rather than a module constant because a ``sa.Column`` object is bound to the
    table it is created against once it has been emitted, and ``downgrade`` needs the *names* only.
    """
    return [
        sa.Column("origin", sa.String(length=16), nullable=False, server_default="MANUAL"),
        sa.Column("created_by", sa.String(length=80), nullable=False, server_default="system"),
        sa.Column("identity_key", sa.String(length=160), nullable=True),
        sa.Column("document_id", sa.String(length=36), nullable=True),
        sa.Column("document_version_id", sa.String(length=36), nullable=True),
        sa.Column("attributes", sa.JSON(), nullable=True),
    ]


#: The columns in drop order; the constraints and the index go with them.
ADDED = (
    "attributes",
    "document_version_id",
    "document_id",
    "identity_key",
    "created_by",
    "origin",
)


def upgrade() -> None:
    for column in _columns():
        op.add_column("calculation", column)
    # Content identity is what makes the write path idempotent, so it belongs in the database rather than
    # in a "check then insert" that two concurrent batches can both win.
    op.create_index("uq_calculation_identity", "calculation", ["identity_key"], unique=True)
    # A row that arrived before this revision has no citation and no author.  ``MANUAL`` is not a guess
    # about where it came from; it is the statement that the platform did not, and it is the one origin a
    # rebuild never deletes.
    op.execute("UPDATE calculation SET origin = 'MANUAL' WHERE origin IS NULL OR origin = ''")


def downgrade() -> None:
    op.drop_index("uq_calculation_identity", table_name="calculation")
    for name in ADDED:
        op.drop_column("calculation", name)
