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
    bind = op.get_bind()
    if bind.dialect.name != "sqlite":
        raise RuntimeError(
            "ProcedureStep completion-state migration 0029 is qualified for SQLite only; "
            "PostgreSQL is NOT QUALIFIED"
        )
    op.execute("DROP TRIGGER IF EXISTS trg_procedure_step_state_update")
    op.execute("DROP TRIGGER IF EXISTS trg_procedure_step_state_insert")
    existing = {col["name"] for col in sa.inspect(bind).get_columns("procedure_step")}
    if "completion_state" not in existing:
        return
    # SQLite cannot DROP COLUMN while a CHECK constraint references it. Rebuild the table
    # with the EXACT pre-0029 DDL (migration 0027's contract: PK, FK with CASCADE, NOT NULL,
    # defaults, CHECK(sequence > 0), unique (procedure_id, sequence)), copy every row, then
    # restore the index. CTAS is deliberately NOT used: it would drop all constraints.
    op.drop_index("ix_procedure_step_procedure", table_name="procedure_step")
    op.create_table(
        "procedure_step_rebuild",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("procedure_id", sa.String(36), nullable=False),
        sa.Column("sequence", sa.Integer(), nullable=False),
        sa.Column("title", sa.String(240), nullable=False),
        sa.Column("instructions", sa.Text(), nullable=False, server_default=""),
        sa.Column("responsible_role", sa.String(160), nullable=True),
        sa.Column("equipment", sa.Text(), nullable=True),
        sa.Column("preconditions", sa.Text(), nullable=True),
        sa.Column("parameters", sa.JSON(), nullable=True),
        sa.Column("expected_result", sa.Text(), nullable=True),
        sa.Column("acceptance_criteria", sa.Text(), nullable=True),
        sa.Column("evidence_required", sa.Text(), nullable=True),
        sa.Column("hold_point", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("safety_controls", sa.Text(), nullable=True),
        sa.Column("contingency", sa.Text(), nullable=True),
        sa.Column("source_reference", sa.String(500), nullable=True),
        sa.Column("provenance", sa.JSON(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["procedure_id"], ["procedure_record.id"], ondelete="CASCADE"),
        sa.UniqueConstraint("procedure_id", "sequence", name="uq_procedure_step_sequence"),
        sa.CheckConstraint("sequence > 0", name="ck_procedure_step_sequence_positive"),
    )
    op.execute(
        "INSERT INTO procedure_step_rebuild "
        "(id, procedure_id, sequence, title, instructions, responsible_role, equipment, "
        "preconditions, parameters, expected_result, acceptance_criteria, evidence_required, "
        "hold_point, safety_controls, contingency, source_reference, provenance, "
        "created_at, updated_at) "
        "SELECT id, procedure_id, sequence, title, instructions, responsible_role, equipment, "
        "preconditions, parameters, expected_result, acceptance_criteria, evidence_required, "
        "hold_point, safety_controls, contingency, source_reference, provenance, "
        "created_at, updated_at FROM procedure_step"
    )
    op.drop_table("procedure_step")
    op.execute("ALTER TABLE procedure_step_rebuild RENAME TO procedure_step")
    op.create_index(
        "ix_procedure_step_procedure", "procedure_step", ["procedure_id", "sequence"]
    )
