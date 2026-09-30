"""the casing run domain

Revision ID: 0014
Revises: 0013
Create Date: 2026-09-30

A casing report states what was actually run.  The platform could already store a plan -
``well_section`` carries an intended interval and a ``casing_program`` label - but it had no
place for the string that went in, its grade, its connection and the depth its shoe landed
at.  Filing a casing tally into ``well_section`` would have overwritten the plan with what
happened and left the two indistinguishable, which is the one thing a plan-versus-actual
system exists to keep apart.

The table is created rather than an existing one reused, for the reason the V7.1 inventory settled:
``well_section.casing_program`` is a 120-character label on a *hole section*, not a casing
run, and the section's own docstring says its depths are as-drilled with no planned pair on
purpose.  There was no casing table of any kind.

Columns are written straight from the model metadata, so a migration and ``create_all`` cannot
describe different schemas - and the test suite checks that they do not.  Nothing here touches
existing data: this is a new table, and no migration may quietly rewrite what a well already
recorded.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0014"
down_revision = "0013"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "casing_run",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("well_id", sa.String(length=36), nullable=False),
        sa.Column("section_id", sa.String(length=36), nullable=True),
        sa.Column("document_id", sa.String(length=36), nullable=True),
        sa.Column("document_version_id", sa.String(length=36), nullable=True),
        sa.Column("string_label", sa.String(length=160), nullable=True),
        sa.Column("string_type", sa.String(length=32), nullable=True),
        sa.Column("size_text", sa.String(length=40), nullable=False),
        sa.Column("size_value", sa.Float(), nullable=True),
        sa.Column("size_unit", sa.String(length=12), nullable=False),
        sa.Column("weight_text", sa.String(length=40), nullable=True),
        sa.Column("weight_value", sa.Float(), nullable=True),
        sa.Column("weight_unit", sa.String(length=12), nullable=False),
        sa.Column("grade", sa.String(length=40), nullable=False),
        sa.Column("connection", sa.String(length=80), nullable=False),
        sa.Column("top_depth_text", sa.String(length=40), nullable=True),
        sa.Column("top_depth_value", sa.Float(), nullable=True),
        sa.Column("top_depth_unit", sa.String(length=12), nullable=False),
        sa.Column("shoe_depth_text", sa.String(length=40), nullable=True),
        sa.Column("shoe_depth_value", sa.Float(), nullable=True),
        sa.Column("shoe_depth_unit", sa.String(length=12), nullable=False),
        sa.Column("run_date", sa.DateTime(timezone=True), nullable=True),
        sa.Column("run_date_text", sa.String(length=200), nullable=True),
        sa.Column("section_resolution", sa.String(length=24), nullable=False),
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
        sa.ForeignKeyConstraint(["document_id"], ["document.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(
            ["document_version_id"], ["document_version.id"], ondelete="SET NULL"
        ),
        sa.ForeignKeyConstraint(["section_id"], ["well_section.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["well_id"], ["well.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("identity_key", name="uq_casing_run_identity"),
    )
    op.create_index("ix_casing_run_version", "casing_run", ["document_version_id"], unique=False)
    op.create_index("ix_casing_run_well", "casing_run", ["well_id", "is_current"], unique=False)


def downgrade() -> None:
    op.drop_index("ix_casing_run_well", table_name="casing_run")
    op.drop_index("ix_casing_run_version", table_name="casing_run")
    op.drop_table("casing_run")
