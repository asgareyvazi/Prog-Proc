"""add versioned DDR authoring and operation-linked activity rows

Revision ID: 0026
Revises: 0025
Create Date: 2026-11-01
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0026"
down_revision = "0025"
branch_labels = None
depends_on = None


def upgrade() -> None:
    if op.get_bind().dialect.name != "sqlite":
        raise RuntimeError(
            "DDR authoring migration 0026 is qualified for SQLite only; PostgreSQL is NOT QUALIFIED"
        )
    # Add nullable/defaulted columns directly so SQLite online and offline migration paths share
    # the same DDL. Existing imported reports are represented as submitted revision 1 records.
    op.add_column(
        "well_operation",
        sa.Column("is_current", sa.Boolean(), nullable=False, server_default=sa.true()),
    )
    op.add_column(
        "ddr_report",
        sa.Column("authoring_state", sa.String(16), nullable=False, server_default="SUBMITTED"),
    )
    op.add_column("ddr_report", sa.Column("revision_group_id", sa.String(36), nullable=True))
    op.add_column(
        "ddr_report", sa.Column("revision_number", sa.Integer(), nullable=False, server_default="1")
    )
    op.add_column(
        "ddr_report",
        sa.Column("supersedes_report_id", sa.String(36), nullable=True),
    )
    op.add_column(
        "ddr_report",
        sa.Column("is_current", sa.Boolean(), nullable=False, server_default=sa.true()),
    )
    op.create_index("ix_ddr_report_revision_parent", "ddr_report", ["supersedes_report_id"])
    op.create_index(
        "uq_ddr_report_revision_number",
        "ddr_report",
        ["revision_group_id", "revision_number"],
        unique=True,
    )
    op.create_index(
        "uq_ddr_report_revision_current",
        "ddr_report",
        ["revision_group_id"],
        unique=True,
        sqlite_where=sa.text("revision_group_id IS NOT NULL AND is_current = 1"),
        postgresql_where=sa.text("revision_group_id IS NOT NULL AND is_current"),
    )
    # SQLite cannot ALTER a table to add a self-referential FK without rebuilding it. These
    # triggers enforce the same reference and authoring-state invariants in both migrated and
    # directly-created SQLite workspaces; this is intentionally not advertised as a PostgreSQL path.
    op.execute(
        "CREATE TRIGGER IF NOT EXISTS trg_ddr_supersedes_insert BEFORE INSERT ON ddr_report "
        "WHEN NEW.supersedes_report_id IS NOT NULL AND NOT EXISTS "
        "(SELECT 1 FROM ddr_report WHERE id = NEW.supersedes_report_id) "
        "BEGIN SELECT RAISE(ABORT, 'superseded DDR report does not exist'); END"
    )
    op.execute(
        "CREATE TRIGGER IF NOT EXISTS trg_ddr_supersedes_update BEFORE UPDATE OF supersedes_report_id ON ddr_report "
        "WHEN NEW.supersedes_report_id IS NOT NULL AND NOT EXISTS "
        "(SELECT 1 FROM ddr_report WHERE id = NEW.supersedes_report_id) "
        "BEGIN SELECT RAISE(ABORT, 'superseded DDR report does not exist'); END"
    )
    op.execute(
        "CREATE TRIGGER IF NOT EXISTS trg_ddr_supersedes_delete AFTER DELETE ON ddr_report "
        "BEGIN UPDATE ddr_report SET supersedes_report_id = NULL "
        "WHERE supersedes_report_id = OLD.id; END"
    )
    op.execute(
        "CREATE TRIGGER IF NOT EXISTS trg_ddr_authoring_state_insert BEFORE INSERT ON ddr_report "
        "WHEN NEW.authoring_state NOT IN ('DRAFT', 'SUBMITTED') "
        "BEGIN SELECT RAISE(ABORT, 'invalid DDR authoring state'); END"
    )
    op.execute(
        "CREATE TRIGGER IF NOT EXISTS trg_ddr_authoring_state_update BEFORE UPDATE OF authoring_state ON ddr_report "
        "WHEN NEW.authoring_state NOT IN ('DRAFT', 'SUBMITTED') "
        "BEGIN SELECT RAISE(ABORT, 'invalid DDR authoring state'); END"
    )

    op.create_table(
        "ddr_activity",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("ddr_report_id", sa.String(36), nullable=False),
        sa.Column("sequence", sa.Integer(), nullable=False),
        sa.Column("operation_type", sa.String(48), nullable=False),
        sa.Column("label", sa.String(200), nullable=False),
        sa.Column("description", sa.Text(), nullable=False, server_default=""),
        sa.Column("period_text", sa.String(200), nullable=True),
        sa.Column("operation_id", sa.String(36), nullable=True),
        sa.Column("provenance", sa.JSON(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["ddr_report_id"], ["ddr_report.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["operation_id"], ["well_operation.id"], ondelete="SET NULL"),
        sa.UniqueConstraint("ddr_report_id", "sequence", name="uq_ddr_activity_sequence"),
        sa.CheckConstraint("sequence > 0", name="ck_ddr_activity_sequence_positive"),
    )
    op.create_index("ix_ddr_activity_operation", "ddr_activity", ["operation_id"])


def downgrade() -> None:
    bind = op.get_bind()
    authored = bind.execute(
        sa.text(
            "SELECT count(*) FROM ddr_report "
            "WHERE revision_group_id IS NOT NULL OR authoring_state = 'DRAFT' "
            "OR supersedes_report_id IS NOT NULL OR is_current = 0"
        )
    ).scalar_one()
    activities = bind.execute(sa.text("SELECT count(*) FROM ddr_activity")).scalar_one()
    historical_operations = bind.execute(
        sa.text("SELECT count(*) FROM well_operation WHERE is_current = 0")
    ).scalar_one()
    if int(authored) or int(activities) or int(historical_operations):
        raise RuntimeError(
            "cannot downgrade 0026 while authored DDR revisions, activities, or historical "
            "operations exist; their history and links must not be discarded"
        )

    op.execute("DROP TRIGGER IF EXISTS trg_ddr_supersedes_insert")
    op.execute("DROP TRIGGER IF EXISTS trg_ddr_supersedes_update")
    op.execute("DROP TRIGGER IF EXISTS trg_ddr_supersedes_delete")
    op.execute("DROP TRIGGER IF EXISTS trg_ddr_authoring_state_insert")
    op.execute("DROP TRIGGER IF EXISTS trg_ddr_authoring_state_update")
    op.drop_index("ix_ddr_activity_operation", table_name="ddr_activity")
    op.drop_table("ddr_activity")
    op.drop_index("uq_ddr_report_revision_current", table_name="ddr_report")
    op.drop_index("uq_ddr_report_revision_number", table_name="ddr_report")
    op.drop_index("ix_ddr_report_revision_parent", table_name="ddr_report")
    op.drop_column("ddr_report", "is_current")
    op.drop_column("ddr_report", "supersedes_report_id")
    op.drop_column("ddr_report", "revision_number")
    op.drop_column("ddr_report", "revision_group_id")
    op.drop_column("ddr_report", "authoring_state")
    op.drop_column("well_operation", "is_current")
