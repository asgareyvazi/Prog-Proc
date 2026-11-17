"""add revision-owned program sections and structured procedure steps

Revision ID: 0027
Revises: 0026
Create Date: 2026-11-16
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0027"
down_revision = "0026"
branch_labels = None
depends_on = None


def upgrade() -> None:
    if op.get_bind().dialect.name != "sqlite":
        raise RuntimeError(
            "Program/Procedure authoring migration 0027 is qualified for SQLite only; "
            "PostgreSQL is NOT QUALIFIED"
        )
    op.create_table(
        "program_section",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("program_id", sa.String(36), nullable=False),
        sa.Column("sequence", sa.Integer(), nullable=False),
        sa.Column("section_key", sa.String(80), nullable=False),
        sa.Column("title", sa.String(240), nullable=False),
        sa.Column("content", sa.Text(), nullable=False, server_default=""),
        sa.Column("completion_state", sa.String(32), nullable=False, server_default="DRAFT"),
        sa.Column("source_reference", sa.String(500), nullable=True),
        sa.Column("provenance", sa.JSON(), nullable=True),
        sa.Column("origin", sa.String(16), nullable=False, server_default="MANUAL"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["program_id"], ["drilling_program.id"], ondelete="CASCADE"),
        sa.UniqueConstraint("program_id", "sequence", name="uq_program_section_sequence"),
        sa.CheckConstraint("sequence > 0", name="ck_program_section_sequence_positive"),
        sa.CheckConstraint(
            "completion_state IN ('MISSING','DRAFT','SOURCE_PROVIDED','EXTRACTED_UNVERIFIED',"
            "'TECHNICALLY_REVIEWED','APPROVED','NOT_APPLICABLE','OWNER_DECISION_REQUIRED')",
            name="ck_program_section_completion_state",
        ),
    )
    op.create_index("ix_program_section_program", "program_section", ["program_id", "sequence"])

    op.create_table(
        "procedure_step",
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
    op.create_index("ix_procedure_step_procedure", "procedure_step", ["procedure_id", "sequence"])


def downgrade() -> None:
    bind = op.get_bind()
    sections = int(bind.execute(sa.text("SELECT count(*) FROM program_section")).scalar_one())
    steps = int(bind.execute(sa.text("SELECT count(*) FROM procedure_step")).scalar_one())
    if sections or steps:
        raise RuntimeError(
            "cannot downgrade 0027 while authored Program sections or Procedure steps exist; "
            "their engineering content and revision history must not be discarded"
        )
    op.drop_index("ix_procedure_step_procedure", table_name="procedure_step")
    op.drop_table("procedure_step")
    op.drop_index("ix_program_section_program", table_name="program_section")
    op.drop_table("program_section")
