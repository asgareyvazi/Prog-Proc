"""persist exact lifecycle state before a lesson revision is superseded

Revision ID: 0023
Revises: 0022
Create Date: 2026-11-02
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy import text

revision = "0023"
down_revision = "0022"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("lesson_learned", sa.Column("prior_status", sa.String(24), nullable=True))
    # Existing superseded rows cannot in general be reconstructed. Preserve only
    # positive approval evidence; never infer REVIEW or REJECTED from reviewer fields.
    op.execute(
        text(
            "UPDATE lesson_learned SET prior_status = CASE "
            "WHEN approved_by IS NOT NULL THEN 'APPROVED' ELSE 'UNKNOWN' END "
            "WHERE status = 'SUPERSEDED' AND prior_status IS NULL"
        )
    )


def downgrade() -> None:
    op.drop_column("lesson_learned", "prior_status")
