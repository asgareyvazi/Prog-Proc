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


def _problem_occurrence_before_required_fk() -> sa.Table:
    """Exact 0006 table plus the nullable column added earlier in this revision.

    Offline SQLite has no reflection connection for Alembic batch mode. Supplying
    this historical intermediate definition lets the offline script emit the same
    table rebuild, NOT NULL constraint, and FK as the online path instead of
    silently omitting the relationship.
    """
    metadata = sa.MetaData()
    table = sa.Table(
        "problem_occurrence",
        metadata,
        sa.Column("id", sa.String(36), nullable=False),
        sa.Column("well_id", sa.String(36), nullable=False),
        sa.Column("section_id", sa.String(36), nullable=True),
        sa.Column("operation_id", sa.String(36), nullable=True),
        sa.Column("event_id", sa.String(36), nullable=True),
        sa.Column("npt_id", sa.String(36), nullable=True),
        sa.Column("problem_type", sa.String(64), nullable=False),
        sa.Column("code", sa.String(80), nullable=True),
        sa.Column("description", sa.Text(), nullable=False),
        sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("depth_from_value", sa.Float(), nullable=True),
        sa.Column("depth_from_unit", sa.String(16), nullable=False),
        sa.Column("depth_to_value", sa.Float(), nullable=True),
        sa.Column("depth_to_unit", sa.String(16), nullable=False),
        sa.Column("hole_size_in", sa.Float(), nullable=True),
        sa.Column("formation", sa.String(120), nullable=True),
        sa.Column("immediate_cause", sa.Text(), nullable=True),
        sa.Column("immediate_cause_status", sa.String(16), nullable=False),
        sa.Column("root_cause", sa.Text(), nullable=True),
        sa.Column("root_cause_status", sa.String(16), nullable=False),
        sa.Column("contributing_factors", sa.JSON(), nullable=True),
        sa.Column("corrective_action", sa.Text(), nullable=True),
        sa.Column("preventive_action", sa.Text(), nullable=True),
        sa.Column("status", sa.String(24), nullable=False),
        sa.Column("confidence", sa.Float(), nullable=True),
        sa.Column("provenance", sa.JSON(), nullable=True),
        sa.Column("origin", sa.String(16), nullable=False),
        sa.Column("created_by", sa.String(80), nullable=False),
        sa.Column("identity_key", sa.String(160), nullable=True),
        sa.Column("document_id", sa.String(36), nullable=True),
        sa.Column("document_version_id", sa.String(36), nullable=True),
        sa.Column("attributes", sa.JSON(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("problem_definition_id", sa.String(36), nullable=True),
        sa.ForeignKeyConstraint(
            ["document_id"],
            ["document.id"],
            name="fk_problem_occurrence_document_id_document",
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["document_version_id"],
            ["document_version.id"],
            name="fk_problem_occurrence_document_version_id_document_version",
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["event_id"],
            ["well_event.id"],
            name="fk_problem_occurrence_event_id_well_event",
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["npt_id"],
            ["npt_record.id"],
            name="fk_problem_occurrence_npt_id_npt_record",
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["operation_id"],
            ["well_operation.id"],
            name="fk_problem_occurrence_operation_id_well_operation",
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["section_id"],
            ["well_section.id"],
            name="fk_problem_occurrence_section_id_well_section",
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["well_id"], ["well.id"], name="fk_problem_occurrence_well_id_well", ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id", name="pk_problem_occurrence"),
        sa.UniqueConstraint("identity_key", name="uq_problem_identity"),
    )
    sa.Index("ix_problem_type_time", table.c.problem_type, table.c.occurred_at)
    sa.Index("ix_problem_version", table.c.document_version_id)
    sa.Index("ix_problem_well", table.c.well_id, table.c.problem_type)
    return table


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
    if context.is_offline_mode():
        with op.batch_alter_table(
            "problem_occurrence",
            copy_from=_problem_occurrence_before_required_fk(),
            recreate="always",
        ) as batch:
            batch.alter_column(
                "problem_definition_id",
                existing_type=sa.String(36),
                nullable=False,
            )
            batch.create_foreign_key(
                "fk_problem_occurrence_problem_definition_id_problem_definition",
                "problem_definition",
                ["problem_definition_id"],
                ["id"],
                ondelete="RESTRICT",
            )
    else:
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
