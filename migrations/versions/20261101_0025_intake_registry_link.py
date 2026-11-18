"""link reviewed intake items to the registered document version

Revision ID: 0025
Revises: 0024
Create Date: 2026-11-01
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0025"
down_revision = "0024"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Keep registration in its own FK-protected table so SQLite needs no table-copy migration.
    # The intake status constraint remains QUEUED/REVIEWED/REJECTED; the application derives the
    # REGISTERED display state only when this durable link exists.
    op.create_table(
        "intake_registration",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("intake_item_id", sa.String(36), nullable=False),
        sa.Column("document_id", sa.String(36), nullable=False),
        sa.Column("document_version_id", sa.String(36), nullable=False),
        sa.Column("registered_well_id", sa.String(36), nullable=True),
        sa.Column("registered_by", sa.String(80), nullable=False),
        sa.Column("registered_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("registration_reason", sa.Text(), nullable=False),
        sa.ForeignKeyConstraint(["intake_item_id"], ["intake_queue_item.id"], ondelete="RESTRICT"),
        sa.UniqueConstraint("intake_item_id", name="uq_intake_registration_intake_item_id"),
        sa.ForeignKeyConstraint(["document_id"], ["document.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(
            ["document_version_id"], ["document_version.id"], ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(["registered_well_id"], ["well.id"], ondelete="SET NULL"),
    )
    op.create_index(
        "ix_intake_registration_document",
        "intake_registration",
        ["document_id", "document_version_id"],
    )


def downgrade() -> None:
    bind = op.get_bind()
    registered = bind.execute(sa.text("SELECT count(*) FROM intake_registration")).scalar_one()
    if int(registered):
        raise RuntimeError(
            "cannot downgrade 0025 while registered intake items exist; their registry links "
            "must not be discarded"
        )
    op.drop_index("ix_intake_registration_document", table_name="intake_registration")
    op.drop_table("intake_registration")
