"""add the certified V3 mud report and repeated measurement domain

Revision ID: 0010
Revises: 0009
Create Date: 2026-09-20

The stored extraction corpus contains one deterministic mud shape: a summary label/value/unit table and
repeated daily-test rows.  This migration adds exactly the two tables required to preserve that shape.
The parent is well-scoped; ``section_id`` is nullable because the corpus has MD but no section
identifier or hole-size match.  The child rows retain source units and raw value text; no conversion is
performed for units outside the existing registry.

``is_current`` separates source-version replacement from confirmation.  A reprocessing pass may mark a
derived row historical, but the promoter never overwrites or deletes a human-confirmed row.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0010"
down_revision = "0009"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "mud_report",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("well_id", sa.String(length=36), nullable=False),
        sa.Column("section_id", sa.String(length=36), nullable=True),
        sa.Column("document_id", sa.String(length=36), nullable=True),
        sa.Column("document_version_id", sa.String(length=36), nullable=True),
        sa.Column("report_date", sa.DateTime(timezone=True), nullable=True),
        sa.Column("report_date_text", sa.String(length=80), nullable=True),
        sa.Column("report_number", sa.String(length=64), nullable=True),
        sa.Column("revision", sa.String(length=64), nullable=True),
        sa.Column("depth_md_value", sa.Float(), nullable=True),
        sa.Column("depth_md_unit", sa.String(length=16), nullable=False),
        sa.Column("depth_tvd_value", sa.Float(), nullable=True),
        sa.Column("depth_tvd_unit", sa.String(length=16), nullable=False),
        sa.Column("record_state", sa.String(length=16), nullable=False),
        sa.Column("status", sa.String(length=24), nullable=False),
        sa.Column("document_status", sa.String(length=32), nullable=True),
        sa.Column("origin", sa.String(length=16), nullable=False),
        sa.Column("created_by", sa.String(length=80), nullable=False),
        sa.Column("provenance", sa.JSON(), nullable=True),
        sa.Column("identity_key", sa.String(length=160), nullable=True),
        sa.Column("is_current", sa.Boolean(), nullable=False),
        sa.Column("attributes", sa.JSON(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["well_id"], ["well.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["section_id"], ["well_section.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["document_id"], ["document.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(
            ["document_version_id"], ["document_version.id"], ondelete="SET NULL"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("identity_key", name="uq_mud_report_identity"),
    )
    op.create_index(
        "ix_mud_report_well_date", "mud_report", ["well_id", "report_date"], unique=False
    )
    op.create_index("ix_mud_report_version", "mud_report", ["document_version_id"], unique=False)
    op.create_index("ix_mud_report_current", "mud_report", ["well_id", "is_current"], unique=False)

    op.create_table(
        "mud_measurement",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("mud_report_id", sa.String(length=36), nullable=False),
        sa.Column("well_id", sa.String(length=36), nullable=False),
        sa.Column("section_id", sa.String(length=36), nullable=True),
        sa.Column("document_id", sa.String(length=36), nullable=True),
        sa.Column("document_version_id", sa.String(length=36), nullable=True),
        sa.Column("property_name", sa.String(length=64), nullable=False),
        sa.Column("source_label", sa.String(length=160), nullable=False),
        sa.Column("sample_key", sa.String(length=160), nullable=False),
        sa.Column("sample_index", sa.Integer(), nullable=False),
        sa.Column("sample_label", sa.String(length=80), nullable=True),
        sa.Column("measured_at_text", sa.String(length=80), nullable=True),
        sa.Column("value", sa.Float(), nullable=True),
        sa.Column("unit", sa.String(length=32), nullable=False),
        sa.Column("normalized_value", sa.Float(), nullable=True),
        sa.Column("normalized_unit", sa.String(length=32), nullable=True),
        sa.Column("source_value_text", sa.String(length=200), nullable=False),
        sa.Column("source_remark", sa.Text(), nullable=True),
        sa.Column("quality", sa.String(length=24), nullable=False),
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
        sa.ForeignKeyConstraint(["mud_report_id"], ["mud_report.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["well_id"], ["well.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["section_id"], ["well_section.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["document_id"], ["document.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(
            ["document_version_id"], ["document_version.id"], ondelete="SET NULL"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("identity_key", name="uq_mud_measurement_identity"),
    )
    op.create_index(
        "ix_mud_measurement_report",
        "mud_measurement",
        ["mud_report_id", "property_name", "sample_key"],
        unique=False,
    )
    op.create_index(
        "ix_mud_measurement_well", "mud_measurement", ["well_id", "property_name"], unique=False
    )
    op.create_index(
        "ix_mud_measurement_version", "mud_measurement", ["document_version_id"], unique=False
    )


def downgrade() -> None:
    op.drop_index("ix_mud_measurement_version", table_name="mud_measurement")
    op.drop_index("ix_mud_measurement_well", table_name="mud_measurement")
    op.drop_index("ix_mud_measurement_report", table_name="mud_measurement")
    op.drop_table("mud_measurement")
    op.drop_index("ix_mud_report_current", table_name="mud_report")
    op.drop_index("ix_mud_report_version", table_name="mud_report")
    op.drop_index("ix_mud_report_well_date", table_name="mud_report")
    op.drop_table("mud_report")
