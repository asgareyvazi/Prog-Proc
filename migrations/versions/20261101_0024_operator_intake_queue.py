"""add a local-first, hash-addressed operator intake queue

Revision ID: 0024
Revises: 0023
Create Date: 2026-11-01
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0024"
down_revision = "0023"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "intake_queue_item",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("source_path", sa.String(4096), nullable=False),
        sa.Column("filename", sa.String(512), nullable=False),
        sa.Column("extension", sa.String(16), nullable=False),
        sa.Column("size_bytes", sa.Integer(), nullable=False),
        sa.Column("sha256", sa.String(64), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("enqueued_by", sa.String(80), nullable=False),
        sa.Column("enqueued_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("reviewed_by", sa.String(80), nullable=True),
        sa.Column("reviewed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("review_reason", sa.Text(), nullable=True),
        sa.UniqueConstraint("sha256", name="uq_intake_queue_item_sha256"),
        sa.CheckConstraint(
            "status IN ('QUEUED', 'REVIEWED', 'REJECTED')", name="ck_intake_queue_status"
        ),
    )
    op.create_index(
        "ix_intake_queue_status_enqueued",
        "intake_queue_item",
        ["status", "enqueued_at"],
    )


def downgrade() -> None:
    op.drop_index("ix_intake_queue_status_enqueued", table_name="intake_queue_item")
    op.drop_table("intake_queue_item")
