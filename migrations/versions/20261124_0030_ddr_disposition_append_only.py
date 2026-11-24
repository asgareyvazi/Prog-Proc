"""append-only ddr_disposition table for DDR extraction candidate dispositions

Revision ID: 0030
Revises: 0029
Create Date: 2026-11-24
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0030"
down_revision = "0029"
branch_labels = None
depends_on = None


def upgrade() -> None:
    if op.get_bind().dialect.name != "sqlite":
        raise RuntimeError(
            "DDR disposition migration 0030 is qualified for SQLite only; "
            "PostgreSQL is NOT QUALIFIED"
        )
    bind = op.get_bind()
    try:
        existing = set(sa.inspect(bind).get_table_names())
    except sa.exc.NoInspectionAvailable:
        existing = set()
    if "ddr_disposition" not in existing:
        op.create_table(
            "ddr_disposition",
            sa.Column("id", sa.String(36), primary_key=True),
            sa.Column(
                "document_version_id",
                sa.String(36),
                sa.ForeignKey("document_version.id", ondelete="CASCADE"),
                nullable=False,
            ),
            sa.Column("candidate_id", sa.String(128), nullable=False),
            sa.Column("field", sa.String(64), nullable=False),
            sa.Column("decision", sa.String(16), nullable=False),
            sa.Column("extracted_value", sa.Text(), nullable=False, server_default=""),
            sa.Column("edited_value", sa.Text(), nullable=False, server_default=""),
            sa.Column("locator", sa.JSON(), nullable=True),
            sa.Column("actor", sa.String(200), nullable=False),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        )
        op.create_index("ix_ddr_disposition_version", "ddr_disposition", ["document_version_id"])
        op.create_index("ix_ddr_disposition_candidate", "ddr_disposition", ["candidate_id"])
    # Append-only: UPDATE and DELETE are forbidden at the database level.
    op.execute(
        "CREATE TRIGGER IF NOT EXISTS trg_ddr_disposition_no_update BEFORE UPDATE ON ddr_disposition "
        "BEGIN SELECT RAISE(ABORT, 'ddr_disposition is append-only: updates are forbidden'); END"
    )
    op.execute(
        "CREATE TRIGGER IF NOT EXISTS trg_ddr_disposition_no_delete BEFORE DELETE ON ddr_disposition "
        "BEGIN SELECT RAISE(ABORT, 'ddr_disposition is append-only: deletes are forbidden'); END"
    )


def downgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name != "sqlite":
        raise RuntimeError(
            "DDR disposition migration 0030 is qualified for SQLite only; "
            "PostgreSQL is NOT QUALIFIED"
        )
    op.execute("DROP TRIGGER IF EXISTS trg_ddr_disposition_no_delete")
    op.execute("DROP TRIGGER IF EXISTS trg_ddr_disposition_no_update")
    op.drop_index("ix_ddr_disposition_candidate", table_name="ddr_disposition")
    op.drop_index("ix_ddr_disposition_version", table_name="ddr_disposition")
    op.drop_table("ddr_disposition")
