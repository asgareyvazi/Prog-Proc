"""the HSE incident domain

Revision ID: 0018
Revises: 0017
Create Date: 2026-09-30

``hse_incident`` holds an HSE event as its own record, at the place the source said it happened.

It is not ``well_event`` with ``category="safety"``, and the blocker is a constraint rather than a
taste. ``WellEvent.well_id`` is ``NOT NULL`` with a cascading foreign key, so every row must belong
to a well - and a slip on the camp steps, a spill at the mud warehouse, a vehicle incident on the
access road and a dropped object in the base laydown area are all reportable HSE events with no well
at all. Filing those against a well would invent a scope the source never stated, and making
``WellEvent.well_id`` nullable would silently change the meaning of every event row the DDR writer
already produces. So ``well_id`` is nullable here on purpose, ``location_text`` keeps the source's
own wording for where it happened, and ``project_id`` / ``field_id`` carry site scope when the source
gave one.

``severity`` is what the source reported. It is never calculated from a probability/impact pair,
never defaulted when absent, and never converted into a ``risk_record`` score: a reported severity is
a statement about what happened, and a risk score is a judgement about what might.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0018"
down_revision = "0017"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "hse_incident",
        sa.Column("id", sa.String(length=36), primary_key=True, nullable=False),
        sa.Column("well_id", sa.String(length=36), sa.ForeignKey("well.id", ondelete="CASCADE")),
        sa.Column(
            "project_id", sa.String(length=36), sa.ForeignKey("project.id", ondelete="SET NULL")
        ),
        sa.Column("field_id", sa.String(length=36), sa.ForeignKey("field.id", ondelete="SET NULL")),
        sa.Column(
            "document_id", sa.String(length=36), sa.ForeignKey("document.id", ondelete="SET NULL")
        ),
        sa.Column(
            "document_version_id",
            sa.String(length=36),
            sa.ForeignKey("document_version.id", ondelete="SET NULL"),
        ),
        sa.Column("incident_reference", sa.String(length=80)),
        sa.Column("incident_type", sa.String(length=32)),
        sa.Column("description", sa.String(), nullable=False),
        sa.Column("location_text", sa.String(length=200), nullable=False),
        sa.Column("occurred_at", sa.DateTime()),
        sa.Column("occurred_at_text", sa.String(length=200), nullable=False),
        sa.Column("severity", sa.String(length=16)),
        sa.Column("consequence", sa.String()),
        sa.Column("immediate_cause", sa.String()),
        sa.Column("immediate_cause_status", sa.String(length=16), nullable=False),
        sa.Column("root_cause", sa.String()),
        sa.Column("root_cause_status", sa.String(length=16), nullable=False),
        sa.Column("corrective_action", sa.String()),
        sa.Column("preventive_action", sa.String()),
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
        sa.UniqueConstraint("identity_key", name="uq_hse_incident_identity"),
    )
    op.create_index("ix_hse_incident_site", "hse_incident", ["project_id", "field_id"])
    op.create_index("ix_hse_incident_version", "hse_incident", ["document_version_id"])
    op.create_index("ix_hse_incident_well", "hse_incident", ["well_id", "is_current"])


def downgrade() -> None:
    op.drop_index("ix_hse_incident_well", table_name="hse_incident")
    op.drop_index("ix_hse_incident_version", table_name="hse_incident")
    op.drop_index("ix_hse_incident_site", table_name="hse_incident")
    op.drop_table("hse_incident")
