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

revision = "0020"
down_revision = "0019"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("lesson_learned", recreate="auto") as batch_op:
        batch_op.add_column(sa.Column("identity_key", sa.String(length=200), nullable=True))
        batch_op.add_column(sa.Column("document_id", sa.String(length=36), nullable=True))
        batch_op.add_column(sa.Column("document_version_id", sa.String(length=36), nullable=True))
        batch_op.create_index("ix_lesson_version", ["document_version_id"])
        batch_op.create_unique_constraint("uq_lesson_identity", ["identity_key"])


def downgrade() -> None:
    with op.batch_alter_table("lesson_learned", recreate="auto") as batch_op:
        batch_op.drop_constraint("uq_lesson_identity", type_="unique")
        batch_op.drop_index("ix_lesson_version")
        batch_op.drop_column("document_version_id")
        batch_op.drop_column("document_id")
        batch_op.drop_column("identity_key")
