"""make reusable problem definitions first-class

Revision ID: 0007
Revises: 0006
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import context, op

revision = "0007"
down_revision = "0006"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "problem_definition",
        sa.Column("id", sa.String(36), nullable=False),
        sa.Column("canonical_key", sa.String(64), nullable=False),
        sa.Column("problem_type", sa.String(64), nullable=False),
        sa.Column("name", sa.String(200), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("status", sa.String(24), nullable=False, server_default="ACTIVE"),
        sa.Column("provenance", sa.JSON(), nullable=True),
        sa.Column("origin", sa.String(16), nullable=False, server_default="MANUAL"),
        sa.Column("created_by", sa.String(80), nullable=False, server_default="system"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("canonical_key", name="uq_problem_definition_canonical_key"),
    )
    op.create_index("ix_problem_definition_type", "problem_definition", ["problem_type"])
    op.add_column(
        "problem_occurrence", sa.Column("problem_definition_id", sa.String(36), nullable=True)
    )
    # The id is deliberately a deterministic, human-auditable key.  This keeps
    # online and offline migration SQL semantically identical.
    op.execute(
        "INSERT INTO problem_definition "
        "(id, canonical_key, problem_type, name, status, origin, created_by, created_at, updated_at) "
        "SELECT 'pdef-' || problem_type, problem_type, problem_type, "
        "replace(problem_type, '_', ' '), 'ACTIVE', 'MANUAL', 'migration-0007', "
        "CURRENT_TIMESTAMP, CURRENT_TIMESTAMP FROM problem_occurrence "
        "WHERE problem_type IS NOT NULL GROUP BY problem_type"
    )
    op.execute(
        "UPDATE problem_occurrence SET problem_definition_id = "
        "(SELECT id FROM problem_definition WHERE canonical_key = problem_occurrence.problem_type)"
    )
    if not context.is_offline_mode():
        with op.batch_alter_table("problem_occurrence") as batch:
            batch.alter_column("problem_definition_id", nullable=False)
            batch.create_foreign_key(
                "fk_problem_occurrence_problem_definition_id_problem_definition",
                "problem_definition",
                ["problem_definition_id"],
                ["id"],
                ondelete="RESTRICT",
            )
    op.create_index(
        "ix_problem_occurrence_definition", "problem_occurrence", ["problem_definition_id"]
    )


def downgrade() -> None:
    op.drop_index("ix_problem_occurrence_definition", table_name="problem_occurrence")
    with op.batch_alter_table("problem_occurrence") as batch:
        batch.drop_constraint(
            "fk_problem_occurrence_problem_definition_id_problem_definition", type_="foreignkey"
        )
        batch.drop_column("problem_definition_id")
    op.drop_index("ix_problem_definition_type", table_name="problem_definition")
    op.drop_table("problem_definition")
