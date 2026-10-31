"""lesson learned: promotion linkage for source-grounded writer (M6)

Revision ID: 0020
Revises: 0019
Create Date: 2026-10-31

LESSON_LEARNED moves from KNOWLEDGE_SUPPORTED to END_TO_END_CERTIFIED with a
narrow source-shaped writer.  A promoted lesson must carry the artefact it
came from and a stable identity so that re-promoting the same version is
idempotent and does not create duplicates.

The lesson table already has `origin`, `provenance` and `attributes`, but it
has no `document_id` / `document_version_id` / `identity_key` columns that
every other promoted domain table uses.  Those are added here as nullable
leaf columns, so an existing manual lesson keeps its values and a promoted
lesson can be addressed, superseded and pruned by version.

No data is backfilled: manual lessons stay manual, and a promoted lesson's
identity is written only when its contract handler creates it.  The unique
constraint on `identity_key` is the same as every other promoted table —
`promotion_identity(version_id, kind, table_id, row_index, well_id, extra)`
— and a second promotion of the same version therefore reports UNCHANGED
rather than creating a second row.  A superseded version leaves the old row
as history via `is_current = 0` and `SUPERSEDED` status, the same rule the
other domains already enforce.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

from drilling_intelligence.database.models import Base

revision = "0020"
down_revision = "0019"
branch_labels = None
depends_on = None


def _table():
    return Base.metadata.tables["lesson_learned"]


def upgrade() -> None:
    # SQLite can add nullable columns without a table rebuild, and --sql must
    # render without a live connection.  Use plain ADD COLUMN for the three
    # new leaf columns, then a copy_from batch for the constraints/indexes
    # which do need a rebuild but can now render offline (the destination
    # table already has ix_lesson_version, uq_lesson_identity and the two FKs).
    op.add_column("lesson_learned", sa.Column("identity_key", sa.String(length=200), nullable=True))
    op.add_column("lesson_learned", sa.Column("document_id", sa.String(length=36), nullable=True))
    op.add_column(
        "lesson_learned", sa.Column("document_version_id", sa.String(length=36), nullable=True)
    )
    # FKs/indexes/unique need a table rebuild on SQLite; copy_from makes it offline-renderable.
    with op.batch_alter_table(
        "lesson_learned", copy_from=_table(), recreate="always"
    ):
        pass


def downgrade() -> None:
    with op.batch_alter_table(
        "lesson_learned", copy_from=_table(), recreate="always"
    ) as batch_op:
        batch_op.drop_constraint(
            "fk_lesson_learned_document_version_id_document_version", type_="foreignkey"
        )
        batch_op.drop_constraint("fk_lesson_learned_document_id_document", type_="foreignkey")
        batch_op.drop_constraint("uq_lesson_identity", type_="unique")
        batch_op.drop_index("ix_lesson_version")
        batch_op.drop_column("document_version_id")
        batch_op.drop_column("document_id")
        batch_op.drop_column("identity_key")
