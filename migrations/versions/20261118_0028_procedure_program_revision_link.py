"""link a procedure to one exact program revision

Revision ID: 0028
Revises: 0027
Create Date: 2026-11-18
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0028"
down_revision = "0027"
branch_labels = None
depends_on = None


def upgrade() -> None:
    if op.get_bind().dialect.name != "sqlite":
        raise RuntimeError(
            "Program/Procedure link migration 0028 is qualified for SQLite only; "
            "PostgreSQL is NOT QUALIFIED"
        )
    # SQLite cannot ALTER a table to add a FK/CHECK constraint without rebuilding it.
    # Add plain columns (as 0026 does) and enforce the reference plus the status
    # vocabulary with triggers in both migrated and directly-created workspaces.
    op.add_column(
        "procedure_record",
        sa.Column("program_revision_id", sa.String(36), nullable=True),
    )
    op.add_column(
        "procedure_record",
        sa.Column("program_link_status", sa.String(24), nullable=False, server_default="NONE"),
    )
    op.create_index(
        "ix_procedure_record_program_revision",
        "procedure_record",
        ["program_revision_id"],
    )
    op.execute(
        "CREATE TRIGGER IF NOT EXISTS trg_procedure_program_link_insert BEFORE INSERT ON procedure_record "
        "WHEN NEW.program_revision_id IS NOT NULL AND NOT EXISTS "
        "(SELECT 1 FROM drilling_program WHERE id = NEW.program_revision_id) "
        "BEGIN SELECT RAISE(ABORT, 'linked Program revision does not exist'); END"
    )
    op.execute(
        "CREATE TRIGGER IF NOT EXISTS trg_procedure_program_link_update BEFORE UPDATE OF program_revision_id ON procedure_record "
        "WHEN NEW.program_revision_id IS NOT NULL AND NOT EXISTS "
        "(SELECT 1 FROM drilling_program WHERE id = NEW.program_revision_id) "
        "BEGIN SELECT RAISE(ABORT, 'linked Program revision does not exist'); END"
    )
    op.execute(
        "CREATE TRIGGER IF NOT EXISTS trg_procedure_program_link_delete AFTER DELETE ON drilling_program "
        "BEGIN UPDATE procedure_record SET program_revision_id = NULL, program_link_status = 'STALE' "
        "WHERE program_revision_id = OLD.id; END"
    )
    op.execute(
        "CREATE TRIGGER IF NOT EXISTS trg_procedure_link_status_insert BEFORE INSERT ON procedure_record "
        "WHEN NEW.program_link_status NOT IN ('NONE', 'LINKED', 'STALE') "
        "BEGIN SELECT RAISE(ABORT, 'invalid procedure Program link status'); END"
    )
    op.execute(
        "CREATE TRIGGER IF NOT EXISTS trg_procedure_link_status_update BEFORE UPDATE OF program_link_status ON procedure_record "
        "WHEN NEW.program_link_status NOT IN ('NONE', 'LINKED', 'STALE') "
        "BEGIN SELECT RAISE(ABORT, 'invalid procedure Program link status'); END"
    )


def downgrade() -> None:
    bind = op.get_bind()
    linked = int(
        bind.execute(
            sa.text("SELECT count(*) FROM procedure_record WHERE program_revision_id IS NOT NULL")
        ).scalar_one()
    )
    if linked:
        raise RuntimeError(
            "cannot downgrade 0028 while procedures are linked to exact Program revisions; "
            "the linkage and its review state must not be discarded"
        )
    op.drop_index("ix_procedure_record_program_revision", table_name="procedure_record")
    op.execute("DROP TRIGGER IF EXISTS trg_procedure_link_status_update")
    op.execute("DROP TRIGGER IF EXISTS trg_procedure_link_status_insert")
    op.execute("DROP TRIGGER IF EXISTS trg_procedure_program_link_delete")
    op.execute("DROP TRIGGER IF EXISTS trg_procedure_program_link_update")
    op.execute("DROP TRIGGER IF EXISTS trg_procedure_program_link_insert")
    op.drop_column("procedure_record", "program_link_status")
    op.drop_column("procedure_record", "program_revision_id")
