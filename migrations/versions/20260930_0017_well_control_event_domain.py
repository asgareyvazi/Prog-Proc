"""the well-control event domain

Revision ID: 0017
Revises: 0016
Create Date: 2026-09-30

``well_control_event`` holds a well-control event as its own record.

It is not ``well_event`` with a category, and the reason is a measurement, not a preference.
``WellEvent`` carries exactly one measured quantity - a depth - and has no pressure or volume field,
so a kick record's SIDPP, SICP and pit gain would have had to be written into ``attributes``. Three
measured quantities, each with a unit the source stated, sitting outside the schema's unit
discipline, invisible to a query and to the search projection, with nothing to stop a promoter from
defaulting a bare ``1200`` to psi. A JSON field is not a substitute for modelling a real measured
quantity, so the quantities get columns and each keeps text, value and unit as three separate facts.

Every column is nullable except identity and provenance plumbing. A source that states a pit gain and
no shut-in pressures produces a row with the gain and NULL pressures, not zero pressures, because a
missing value is not zero.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0017"
down_revision = "0016"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "well_control_event",
        sa.Column("id", sa.String(length=36), primary_key=True, nullable=False),
        sa.Column(
            "well_id",
            sa.String(length=36),
            sa.ForeignKey("well.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "section_id",
            sa.String(length=36),
            sa.ForeignKey("well_section.id", ondelete="SET NULL"),
        ),
        sa.Column(
            "document_id", sa.String(length=36), sa.ForeignKey("document.id", ondelete="SET NULL")
        ),
        sa.Column(
            "document_version_id",
            sa.String(length=36),
            sa.ForeignKey("document_version.id", ondelete="SET NULL"),
        ),
        sa.Column("event_label", sa.String(length=200)),
        sa.Column("event_type", sa.String(length=32)),
        sa.Column("description", sa.String(), nullable=False),
        sa.Column("occurred_at", sa.DateTime()),
        sa.Column("occurred_at_text", sa.String(length=200), nullable=False),
        sa.Column("severity", sa.String(length=16)),
        sa.Column("depth_text", sa.String(length=40), nullable=False),
        sa.Column("depth_value", sa.Float()),
        sa.Column("depth_unit", sa.String(length=12), nullable=False),
        sa.Column("sidpp_text", sa.String(length=40), nullable=False),
        sa.Column("sidpp_value", sa.Float()),
        sa.Column("sidpp_unit", sa.String(length=12), nullable=False),
        sa.Column("sicp_text", sa.String(length=40), nullable=False),
        sa.Column("sicp_value", sa.Float()),
        sa.Column("sicp_unit", sa.String(length=12), nullable=False),
        sa.Column("pit_gain_text", sa.String(length=40), nullable=False),
        sa.Column("pit_gain_value", sa.Float()),
        sa.Column("pit_gain_unit", sa.String(length=12), nullable=False),
        sa.Column("kill_method", sa.String(length=120)),
        sa.Column("outcome", sa.String(length=120)),
        sa.Column("cause", sa.String()),
        sa.Column("cause_status", sa.String(length=16), nullable=False),
        sa.Column("corrective_action", sa.String()),
        sa.Column(
            "npt_id", sa.String(length=36), sa.ForeignKey("npt_record.id", ondelete="SET NULL")
        ),
        sa.Column("status", sa.String(length=24), nullable=False),
        sa.Column("record_state", sa.String(length=16), nullable=False),
        sa.Column("provenance", sa.JSON()),
        sa.Column("origin", sa.String(length=16), nullable=False),
        sa.Column("created_by", sa.String(length=80), nullable=False),
        sa.Column("identity_key", sa.String(length=160)),
        sa.Column("is_current", sa.Boolean(), nullable=False),
        sa.Column("attributes", sa.JSON()),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.UniqueConstraint("identity_key", name="uq_well_control_event_identity"),
    )
    op.create_index("ix_well_control_event_version", "well_control_event", ["document_version_id"])
    op.create_index("ix_well_control_event_well", "well_control_event", ["well_id", "is_current"])


def downgrade() -> None:
    op.drop_index("ix_well_control_event_well", table_name="well_control_event")
    op.drop_index("ix_well_control_event_version", table_name="well_control_event")
    op.drop_table("well_control_event")
