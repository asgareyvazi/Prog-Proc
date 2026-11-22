"""add completion_state to procedure_step (shared extraction disposition state)

Revision ID: 0029
Revises: 0028
Create Date: 2026-11-22
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0029"
down_revision = "0028"
branch_labels = None
depends_on = None

_STATES = (
    "'MISSING','DRAFT','SOURCE_PROVIDED','EXTRACTED_UNVERIFIED',"
    "'TECHNICALLY_REVIEWED','APPROVED','NOT_APPLICABLE','OWNER_DECISION_REQUIRED'"
)


def upgrade() -> None:
    if op.get_bind().dialect.name != "sqlite":
        raise RuntimeError(
            "ProcedureStep completion-state migration 0029 is qualified for SQLite only; "
            "PostgreSQL is NOT QUALIFIED"
        )
    # Offline (--sql) mode binds a MockConnection that cannot be inspected; treat the
    # column as absent there. Online, skip when it already exists (wheel-stamped DBs).
    try:
        existing = {col["name"] for col in sa.inspect(op.get_bind()).get_columns("procedure_step")}
    except sa.exc.NoInspectionAvailable:
        existing = set()
    if "completion_state" not in existing:
        op.add_column(
            "procedure_step",
            sa.Column("completion_state", sa.String(32), nullable=False, server_default="DRAFT"),
        )
    # SQLite cannot ALTER a table to add a CHECK constraint; enforce the same vocabulary as
    # program_section with triggers (models.py installs the same guards for fresh schemas).
    op.execute(
        "CREATE TRIGGER IF NOT EXISTS trg_procedure_step_state_insert BEFORE INSERT ON procedure_step "
        f"WHEN NEW.completion_state NOT IN ({_STATES}) "
        "BEGIN SELECT RAISE(ABORT, 'invalid procedure step completion state'); END"
    )
    op.execute(
        "CREATE TRIGGER IF NOT EXISTS trg_procedure_step_state_update BEFORE UPDATE OF completion_state "
        f"ON procedure_step WHEN NEW.completion_state NOT IN ({_STATES}) "
        "BEGIN SELECT RAISE(ABORT, 'invalid procedure step completion state'); END"
    )


def downgrade() -> None:
    op.execute("DROP TRIGGER IF EXISTS trg_procedure_step_state_update")
    op.execute("DROP TRIGGER IF EXISTS trg_procedure_step_state_insert")
    existing = {col["name"] for col in sa.inspect(op.get_bind()).get_columns("procedure_step")}
    if "completion_state" not in existing:
        return
    # SQLite cannot DROP COLUMN while a CHECK constraint references it, so rebuild the
    # table without the column (table-copy), preserving every row and the sequence index.
    op.execute(
        "CREATE TABLE procedure_step_rebuild AS SELECT "
        "id, procedure_id, sequence, title, instructions, responsible_role, equipment, "
        "preconditions, parameters, expected_result, acceptance_criteria, evidence_required, "
        "hold_point, safety_controls, contingency, source_reference, provenance, "
        "created_at, updated_at FROM procedure_step"
    )
    op.execute("DROP TABLE procedure_step")
    op.execute("ALTER TABLE procedure_step_rebuild RENAME TO procedure_step")
    op.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS uq_procedure_step_sequence "
        "ON procedure_step (procedure_id, sequence)"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_procedure_step_procedure "
        "ON procedure_step (procedure_id, sequence)"
    )
