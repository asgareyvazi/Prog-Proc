"""the cement job domain

Revision ID: 0015
Revises: 0014
Create Date: 2026-09-30

A cement job is its own engineering record and every quantity in it is a different thing:
top of cement is not shoe depth, lead volume is not tail volume, displacement is not slurry
volume, and wait-on-cement is wording rather than a calculation.  Storing those in one
anonymous volume column, or in a general report table, would lose exactly the distinctions
that make the record worth keeping.

The table is created rather than an existing one reused, for the reason the V7.1 inventory settled:
the word cement appeared in the codebase only as vocabulary - an operation type
(``cementing``) and a cost category.  There was no cement model, table or migration.

Columns are written straight from the model metadata, so a migration and ``create_all`` cannot
describe different schemas - and the test suite checks that they do not.  Nothing here touches
existing data: this is a new table, and no migration may quietly rewrite what a well already
recorded.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0015"
down_revision = "0014"
branch_labels = None
depends_on = None


def upgrade() -> None:

    op.create_table(
        "cement_job",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("well_id", sa.String(length=36), nullable=False),
        sa.Column("casing_run_id", sa.String(length=36), nullable=True),
        sa.Column("document_id", sa.String(length=36), nullable=True),
        sa.Column("document_version_id", sa.String(length=36), nullable=True),
        sa.Column("job_label", sa.String(length=160), nullable=True),
        sa.Column("stage_text", sa.String(length=40), nullable=False),
        sa.Column("stage_number", sa.Integer(), nullable=True),
        sa.Column("job_type", sa.String(length=60), nullable=False),
        sa.Column("job_date", sa.DateTime(timezone=True), nullable=True),
        sa.Column("job_date_text", sa.String(length=200), nullable=True),
        sa.Column("lead_slurry", sa.String(length=200), nullable=False),
        sa.Column("tail_slurry", sa.String(length=200), nullable=False),
        sa.Column("lead_volume_text", sa.String(length=40), nullable=True),
        sa.Column("lead_volume_value", sa.Float(), nullable=True),
        sa.Column("lead_volume_unit", sa.String(length=12), nullable=False),
        sa.Column("tail_volume_text", sa.String(length=40), nullable=True),
        sa.Column("tail_volume_value", sa.Float(), nullable=True),
        sa.Column("tail_volume_unit", sa.String(length=12), nullable=False),
        sa.Column("lead_density_text", sa.String(length=40), nullable=True),
        sa.Column("lead_density_value", sa.Float(), nullable=True),
        sa.Column("lead_density_unit", sa.String(length=12), nullable=False),
        sa.Column("tail_density_text", sa.String(length=40), nullable=True),
        sa.Column("tail_density_value", sa.Float(), nullable=True),
        sa.Column("tail_density_unit", sa.String(length=12), nullable=False),
        sa.Column("toc_depth_text", sa.String(length=40), nullable=True),
        sa.Column("toc_depth_value", sa.Float(), nullable=True),
        sa.Column("toc_depth_unit", sa.String(length=12), nullable=False),
        sa.Column("shoe_depth_text", sa.String(length=40), nullable=True),
        sa.Column("shoe_depth_value", sa.Float(), nullable=True),
        sa.Column("shoe_depth_unit", sa.String(length=12), nullable=False),
        sa.Column("displacement_text", sa.String(length=40), nullable=True),
        sa.Column("displacement_value", sa.Float(), nullable=True),
        sa.Column("displacement_unit", sa.String(length=12), nullable=False),
        sa.Column("pressure_text", sa.String(length=40), nullable=True),
        sa.Column("pressure_value", sa.Float(), nullable=True),
        sa.Column("pressure_unit", sa.String(length=12), nullable=False),
        sa.Column("woc_text", sa.String(length=80), nullable=True),
        sa.Column("returns_status", sa.String(length=60), nullable=False),
        sa.Column("casing_resolution", sa.String(length=24), nullable=False),
        sa.Column("record_state", sa.String(length=16), nullable=False),
        sa.Column("status", sa.String(length=24), nullable=False),
        sa.Column("origin", sa.String(length=16), nullable=False),
        sa.Column("created_by", sa.String(length=80), nullable=False),
        sa.Column("provenance", sa.JSON(), nullable=True),
        sa.Column("identity_key", sa.String(length=200), nullable=True),
        sa.Column("is_current", sa.Boolean(), nullable=False),
        sa.Column("attributes", sa.JSON(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["casing_run_id"], ["casing_run.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["document_id"], ["document.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(
            ["document_version_id"], ["document_version.id"], ondelete="SET NULL"
        ),
        sa.ForeignKeyConstraint(["well_id"], ["well.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("identity_key", name="uq_cement_job_identity"),
    )
    op.create_index("ix_cement_job_version", "cement_job", ["document_version_id"], unique=False)
    op.create_index("ix_cement_job_well", "cement_job", ["well_id", "is_current"], unique=False)


def downgrade() -> None:
    op.drop_index("ix_cement_job_version", table_name="cement_job")
    op.drop_index("ix_cement_job_well", table_name="cement_job")
    op.drop_table("cement_job")
