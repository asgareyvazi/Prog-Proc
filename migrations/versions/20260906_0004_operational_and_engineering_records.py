"""operational and engineering records: the spine from a report to a lesson

Revision ID: 0004
Revises: 0003
Create Date: 2026-09-06

Phase 0 could say what a corpus contained and (since 0003) what it asserted.  This revision adds the
third thing a drilling organisation keeps, which is neither a document nor a value: the record of
what was planned, what was done, what went wrong, what it cost in time and money, and what was learnt.
Sixteen tables, in five groups:

*   **The operational spine** - ``ddr_report``, ``well_operation``, ``well_event``, ``npt_record``,
    ``problem_occurrence``.  A report is evidence; a row here is the platform's record of an occurrence,
    and it keeps the report's identity (``document_version_id``, a provenance list) so "who says" is
    answerable from the row itself.  ``duration_hours`` sits beside ``started_at``/``ended_at`` with a
    ``duration_basis`` saying which kind of claim it is, because a stated 6.5 h and a computed 6.5 h
    are not the same evidence.
*   **The engineering record** - ``procedure_record``, ``drilling_program``, ``program_target``,
    ``risk_record``.  Each is versioned with ``revision``/``supersedes_id``/``is_current`` and carries
    its own approval state, because "which revision was approved" is a question about the record: the
    document's filename says ``Rev B``, and the record says who approved it, when, and what it was
    written against.  ``program_target`` is where the plan lives, which is what makes plan-vs-actual a
    join rather than a memory.
*   **What was learnt** - ``lesson_learned``.  A lesson with a review state and no evidence column of
    its own, because the evidence is ``knowledge_relation`` edges to the document versions that support
    it: one edge vocabulary for one graph, rather than a second one that could disagree with the first.
*   **What is done next time** - ``best_practice``, ``recommendation``.  A practice is a lesson the
    field has approved and put its owner's name to; a recommendation is advice the records support,
    kept until a person decides about it.  Both carry an approval or a decision column rather than a
    score, because the platform may derive a suggestion but may not approve one on anybody's behalf.
*   **Who and how much** - ``rig``, ``service_company``, ``cost_item``, ``field_pattern``.  The first
    two are identity only; performance is a join over the rows that name them, since a cached
    "average NPT" column is a number nobody can recompute.  ``field_pattern`` stores an aggregation
    *and the query that produced it*, so a pattern can be checked against the records rather than
    believed.

Nothing is backfilled from the document corpus: promoted rows are written by
:mod:`drilling_intelligence.operations.promote`, which is deterministic, idempotent and refuses to
invent a timestamp, a cause or a severity that the source did not state.  A legacy workspace therefore
gets sixteen empty tables and stays exactly as readable as it was, which is the whole point of making
promotion a separate step (ADR-0010).

The three one-current-revision rules use the same partial unique index that 0002 introduced for
``document_version`` - a database rule rather than a convention, portable to PostgreSQL, and
deliberately paired with :func:`drilling_intelligence.database.integrity.check_revision_chains`, which
checks the parts a constraint cannot: no cycle, revisions increasing along the chain, and a
superseded row that still claims to be current.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0004"
down_revision = "0003"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "service_company",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("company_id", sa.String(length=36), nullable=True),
        sa.Column("name", sa.String(length=200), nullable=False),
        sa.Column("service_type", sa.String(length=64), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("contract_reference", sa.String(length=200), nullable=True),
        sa.Column("notes", sa.Text(), nullable=True),
        sa.Column("created_by", sa.String(length=80), nullable=False),
        sa.Column("attributes", sa.JSON(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["company_id"],
            ["company.id"],
            name=op.f("fk_service_company_company_id_company"),
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_service_company")),
        sa.UniqueConstraint("company_id", "name", name="uq_service_company_name"),
    )
    with op.batch_alter_table("service_company", schema=None) as batch_op:
        batch_op.create_index("ix_service_company_type", ["service_type"], unique=False)

    op.create_table(
        "rig",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("name", sa.String(length=200), nullable=False),
        sa.Column("company_id", sa.String(length=36), nullable=True),
        sa.Column("model", sa.String(length=120), nullable=True),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("horsepower", sa.Float(), nullable=True),
        sa.Column("specifications", sa.JSON(), nullable=True),
        sa.Column("source_id", sa.String(length=36), nullable=True),
        sa.Column("notes", sa.Text(), nullable=True),
        sa.Column("created_by", sa.String(length=80), nullable=False),
        sa.Column("attributes", sa.JSON(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["company_id"],
            ["company.id"],
            name=op.f("fk_rig_company_id_company"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["source_id"], ["source.id"], name=op.f("fk_rig_source_id_source"), ondelete="SET NULL"
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_rig")),
        sa.UniqueConstraint("name", name=op.f("uq_rig_name")),
    )
    with op.batch_alter_table("rig", schema=None) as batch_op:
        batch_op.create_index("ix_rig_company", ["company_id", "name"], unique=False)

    op.create_table(
        "field_pattern",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("signature", sa.String(length=200), nullable=False),
        sa.Column("project_id", sa.String(length=36), nullable=True),
        sa.Column("field_id", sa.String(length=36), nullable=True),
        sa.Column("problem_type", sa.String(length=64), nullable=False),
        sa.Column("operation_type", sa.String(length=48), nullable=True),
        sa.Column("hole_size_in", sa.Float(), nullable=True),
        sa.Column("depth_from_value", sa.Float(), nullable=True),
        sa.Column("depth_from_unit", sa.String(length=16), nullable=False),
        sa.Column("depth_to_value", sa.Float(), nullable=True),
        sa.Column("depth_to_unit", sa.String(length=16), nullable=False),
        sa.Column("occurrence_count", sa.Integer(), nullable=False),
        sa.Column("event_count", sa.Integer(), nullable=False),
        sa.Column("well_count", sa.Integer(), nullable=False),
        sa.Column("total_npt_hours", sa.Float(), nullable=True),
        sa.Column("evidence", sa.JSON(), nullable=True),
        sa.Column("well_ids", sa.JSON(), nullable=True),
        sa.Column("query", sa.JSON(), nullable=True),
        sa.Column("first_seen_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_seen_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("status", sa.String(length=24), nullable=False),
        sa.Column("confidence", sa.Float(), nullable=True),
        sa.Column("stale_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("stale_snapshot", sa.JSON(), nullable=True),
        sa.Column("detected_by", sa.String(length=64), nullable=False),
        sa.Column("note", sa.Text(), nullable=True),
        sa.Column("computed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("attributes", sa.JSON(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["field_id"],
            ["field.id"],
            name=op.f("fk_field_pattern_field_id_field"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["project_id"],
            ["project.id"],
            name=op.f("fk_field_pattern_project_id_project"),
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_field_pattern")),
        sa.UniqueConstraint("signature", name="uq_pattern_signature"),
    )
    with op.batch_alter_table("field_pattern", schema=None) as batch_op:
        batch_op.create_index("ix_pattern_field", ["field_id", "problem_type"], unique=False)

    op.create_table(
        "ddr_report",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("well_id", sa.String(length=36), nullable=False),
        sa.Column("document_id", sa.String(length=36), nullable=True),
        sa.Column("document_version_id", sa.String(length=36), nullable=True),
        sa.Column("report_number", sa.String(length=64), nullable=True),
        sa.Column("report_date", sa.DateTime(timezone=True), nullable=True),
        sa.Column("report_date_text", sa.String(length=80), nullable=True),
        sa.Column("shift", sa.String(length=40), nullable=True),
        sa.Column("record_state", sa.String(length=16), nullable=False),
        sa.Column("status", sa.String(length=24), nullable=False),
        sa.Column("document_status", sa.String(length=32), nullable=True),
        sa.Column("summary", sa.Text(), nullable=False),
        sa.Column("provenance", sa.JSON(), nullable=True),
        sa.Column("origin", sa.String(length=16), nullable=False),
        sa.Column("created_by", sa.String(length=80), nullable=False),
        sa.Column("attributes", sa.JSON(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["document_id"],
            ["document.id"],
            name=op.f("fk_ddr_report_document_id_document"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["document_version_id"],
            ["document_version.id"],
            name=op.f("fk_ddr_report_document_version_id_document_version"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["well_id"], ["well.id"], name=op.f("fk_ddr_report_well_id_well"), ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_ddr_report")),
        sa.UniqueConstraint("document_version_id", "well_id", name="uq_ddr_report_version_well"),
    )
    with op.batch_alter_table("ddr_report", schema=None) as batch_op:
        batch_op.create_index("ix_ddr_report_well_date", ["well_id", "report_date"], unique=False)

    op.create_table(
        "drilling_program",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("code", sa.String(length=80), nullable=True),
        sa.Column("title", sa.String(length=400), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("revision_label", sa.String(length=64), nullable=True),
        sa.Column("is_current", sa.Boolean(), nullable=False),
        sa.Column("supersedes_id", sa.String(length=36), nullable=True),
        sa.Column("status", sa.String(length=24), nullable=False),
        sa.Column("summary", sa.Text(), nullable=False),
        sa.Column("project_id", sa.String(length=36), nullable=True),
        sa.Column("field_id", sa.String(length=36), nullable=True),
        sa.Column("well_id", sa.String(length=36), nullable=True),
        sa.Column("author", sa.String(length=200), nullable=True),
        sa.Column("reviewer", sa.String(length=200), nullable=True),
        sa.Column("approver", sa.String(length=200), nullable=True),
        sa.Column("submitted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("approved_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("planned_spud_date", sa.DateTime(timezone=True), nullable=True),
        sa.Column("planned_completion_date", sa.DateTime(timezone=True), nullable=True),
        sa.Column("document_id", sa.String(length=36), nullable=True),
        sa.Column("document_version_id", sa.String(length=36), nullable=True),
        sa.Column("provenance", sa.JSON(), nullable=True),
        sa.Column("origin", sa.String(length=16), nullable=False),
        sa.Column("created_by", sa.String(length=200), nullable=False),
        sa.Column("attributes", sa.JSON(), nullable=True),
        sa.Column("status_note", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["document_id"],
            ["document.id"],
            name=op.f("fk_drilling_program_document_id_document"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["document_version_id"],
            ["document_version.id"],
            name=op.f("fk_drilling_program_document_version_id_document_version"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["field_id"],
            ["field.id"],
            name=op.f("fk_drilling_program_field_id_field"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["project_id"],
            ["project.id"],
            name=op.f("fk_drilling_program_project_id_project"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["supersedes_id"],
            ["drilling_program.id"],
            name=op.f("fk_drilling_program_supersedes_id_drilling_program"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["well_id"],
            ["well.id"],
            name=op.f("fk_drilling_program_well_id_well"),
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_drilling_program")),
        sa.UniqueConstraint("code", "revision", name="uq_program_code_revision"),
    )
    with op.batch_alter_table("drilling_program", schema=None) as batch_op:
        batch_op.create_index("ix_program_well", ["well_id", "status"], unique=False)
        batch_op.create_index(
            "uq_program_one_current",
            ["code"],
            unique=True,
            sqlite_where=sa.text("is_current = 1 AND code IS NOT NULL"),
            postgresql_where=sa.text("is_current AND code IS NOT NULL"),
        )

    op.create_table(
        "best_practice",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("code", sa.String(length=80), nullable=True),
        sa.Column("title", sa.String(length=400), nullable=False),
        sa.Column("practice_type", sa.String(length=64), nullable=False),
        sa.Column("statement", sa.Text(), nullable=False),
        sa.Column("rationale", sa.Text(), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("revision_label", sa.String(length=64), nullable=True),
        sa.Column("is_current", sa.Boolean(), nullable=False),
        sa.Column("supersedes_id", sa.String(length=36), nullable=True),
        sa.Column("status", sa.String(length=24), nullable=False),
        sa.Column("owner", sa.String(length=120), nullable=True),
        sa.Column("reviewer", sa.String(length=120), nullable=True),
        sa.Column("reviewed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("approved_by", sa.String(length=120), nullable=True),
        sa.Column("approved_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("project_id", sa.String(length=36), nullable=True),
        sa.Column("field_id", sa.String(length=36), nullable=True),
        sa.Column("well_id", sa.String(length=36), nullable=True),
        sa.Column("section_id", sa.String(length=36), nullable=True),
        sa.Column("applicable_operations", sa.JSON(), nullable=True),
        sa.Column("applicable_formations", sa.JSON(), nullable=True),
        sa.Column("hole_size_in", sa.Float(), nullable=True),
        sa.Column("conditions", sa.JSON(), nullable=True),
        sa.Column("not_applicable_when", sa.Text(), nullable=True),
        sa.Column("document_id", sa.String(length=36), nullable=True),
        sa.Column("document_version_id", sa.String(length=36), nullable=True),
        sa.Column("provenance", sa.JSON(), nullable=True),
        sa.Column("origin", sa.String(length=16), nullable=False),
        sa.Column("created_by", sa.String(length=80), nullable=False),
        sa.Column("attributes", sa.JSON(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["document_id"],
            ["document.id"],
            name=op.f("fk_best_practice_document_id_document"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["document_version_id"],
            ["document_version.id"],
            name=op.f("fk_best_practice_document_version_id_document_version"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["field_id"],
            ["field.id"],
            name=op.f("fk_best_practice_field_id_field"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["project_id"],
            ["project.id"],
            name=op.f("fk_best_practice_project_id_project"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["section_id"],
            ["well_section.id"],
            name=op.f("fk_best_practice_section_id_well_section"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["supersedes_id"],
            ["best_practice.id"],
            name=op.f("fk_best_practice_supersedes_id_best_practice"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["well_id"],
            ["well.id"],
            name=op.f("fk_best_practice_well_id_well"),
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_best_practice")),
        sa.UniqueConstraint("code", "revision", name="uq_practice_code_revision"),
    )
    with op.batch_alter_table("best_practice", schema=None) as batch_op:
        batch_op.create_index("ix_practice_field_status", ["field_id", "status"], unique=False)
        batch_op.create_index("ix_practice_well", ["well_id", "status"], unique=False)
        batch_op.create_index(
            "uq_practice_one_current",
            ["code"],
            unique=True,
            sqlite_where=sa.text("is_current = 1 AND code IS NOT NULL"),
            postgresql_where=sa.text("is_current AND code IS NOT NULL"),
        )

    op.create_table(
        "lesson_learned",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("code", sa.String(length=80), nullable=True),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("revision_label", sa.String(length=64), nullable=True),
        sa.Column("is_current", sa.Boolean(), nullable=False),
        sa.Column("supersedes_id", sa.String(length=36), nullable=True),
        sa.Column("status", sa.String(length=24), nullable=False),
        sa.Column("title", sa.String(length=400), nullable=False),
        sa.Column("problem_type", sa.String(length=64), nullable=True),
        sa.Column("context", sa.Text(), nullable=False),
        sa.Column("observation", sa.Text(), nullable=False),
        sa.Column("root_cause", sa.Text(), nullable=True),
        sa.Column("root_cause_status", sa.String(length=16), nullable=False),
        sa.Column("lesson", sa.Text(), nullable=False),
        sa.Column("recommendation", sa.Text(), nullable=False),
        sa.Column("project_id", sa.String(length=36), nullable=True),
        sa.Column("field_id", sa.String(length=36), nullable=True),
        sa.Column("well_id", sa.String(length=36), nullable=True),
        sa.Column("section_id", sa.String(length=36), nullable=True),
        sa.Column("applicable_operations", sa.JSON(), nullable=True),
        sa.Column("applicable_formations", sa.JSON(), nullable=True),
        sa.Column("hole_size_in", sa.Float(), nullable=True),
        sa.Column("depth_from_value", sa.Float(), nullable=True),
        sa.Column("depth_from_unit", sa.String(length=16), nullable=False),
        sa.Column("depth_to_value", sa.Float(), nullable=True),
        sa.Column("depth_to_unit", sa.String(length=16), nullable=False),
        sa.Column("conditions", sa.Text(), nullable=True),
        sa.Column("created_by", sa.String(length=200), nullable=False),
        sa.Column("reviewer", sa.String(length=200), nullable=True),
        sa.Column("reviewed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("approved_by", sa.String(length=200), nullable=True),
        sa.Column("approved_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("confidence", sa.Float(), nullable=True),
        sa.Column("provenance", sa.JSON(), nullable=True),
        sa.Column("origin", sa.String(length=16), nullable=False),
        sa.Column("attributes", sa.JSON(), nullable=True),
        sa.Column("status_note", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["field_id"],
            ["field.id"],
            name=op.f("fk_lesson_learned_field_id_field"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["project_id"],
            ["project.id"],
            name=op.f("fk_lesson_learned_project_id_project"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["section_id"],
            ["well_section.id"],
            name=op.f("fk_lesson_learned_section_id_well_section"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["supersedes_id"],
            ["lesson_learned.id"],
            name=op.f("fk_lesson_learned_supersedes_id_lesson_learned"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["well_id"],
            ["well.id"],
            name=op.f("fk_lesson_learned_well_id_well"),
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_lesson_learned")),
        sa.UniqueConstraint("code", "revision", name="uq_lesson_code_revision"),
    )
    with op.batch_alter_table("lesson_learned", schema=None) as batch_op:
        batch_op.create_index("ix_lesson_problem", ["problem_type", "status"], unique=False)
        batch_op.create_index("ix_lesson_scope", ["field_id", "status"], unique=False)
        batch_op.create_index(
            "uq_lesson_one_current",
            ["code"],
            unique=True,
            sqlite_where=sa.text("is_current = 1 AND code IS NOT NULL"),
            postgresql_where=sa.text("is_current AND code IS NOT NULL"),
        )

    op.create_table(
        "procedure_record",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("code", sa.String(length=80), nullable=True),
        sa.Column("title", sa.String(length=400), nullable=False),
        sa.Column("procedure_type", sa.String(length=64), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("revision_label", sa.String(length=64), nullable=True),
        sa.Column("is_current", sa.Boolean(), nullable=False),
        sa.Column("supersedes_id", sa.String(length=36), nullable=True),
        sa.Column("status", sa.String(length=24), nullable=False),
        sa.Column("description", sa.Text(), nullable=False),
        sa.Column("project_id", sa.String(length=36), nullable=True),
        sa.Column("field_id", sa.String(length=36), nullable=True),
        sa.Column("well_id", sa.String(length=36), nullable=True),
        sa.Column("section_id", sa.String(length=36), nullable=True),
        sa.Column("created_by", sa.String(length=200), nullable=False),
        sa.Column("reviewer", sa.String(length=200), nullable=True),
        sa.Column("reviewed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("approved_by", sa.String(length=200), nullable=True),
        sa.Column("approved_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("source_reference", sa.String(length=300), nullable=True),
        sa.Column("effective_from", sa.DateTime(timezone=True), nullable=True),
        sa.Column("document_id", sa.String(length=36), nullable=True),
        sa.Column("document_version_id", sa.String(length=36), nullable=True),
        sa.Column("provenance", sa.JSON(), nullable=True),
        sa.Column("origin", sa.String(length=16), nullable=False),
        sa.Column("attributes", sa.JSON(), nullable=True),
        sa.Column("status_note", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["document_id"],
            ["document.id"],
            name=op.f("fk_procedure_record_document_id_document"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["document_version_id"],
            ["document_version.id"],
            name=op.f("fk_procedure_record_document_version_id_document_version"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["field_id"],
            ["field.id"],
            name=op.f("fk_procedure_record_field_id_field"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["project_id"],
            ["project.id"],
            name=op.f("fk_procedure_record_project_id_project"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["section_id"],
            ["well_section.id"],
            name=op.f("fk_procedure_record_section_id_well_section"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["supersedes_id"],
            ["procedure_record.id"],
            name=op.f("fk_procedure_record_supersedes_id_procedure_record"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["well_id"],
            ["well.id"],
            name=op.f("fk_procedure_record_well_id_well"),
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_procedure_record")),
        sa.UniqueConstraint("code", "revision", name="uq_procedure_code_revision"),
    )
    with op.batch_alter_table("procedure_record", schema=None) as batch_op:
        batch_op.create_index(
            "ix_procedure_field_type", ["field_id", "procedure_type"], unique=False
        )
        batch_op.create_index("ix_procedure_well", ["well_id", "status"], unique=False)
        batch_op.create_index(
            "uq_procedure_one_current",
            ["code"],
            unique=True,
            sqlite_where=sa.text("is_current = 1 AND code IS NOT NULL"),
            postgresql_where=sa.text("is_current AND code IS NOT NULL"),
        )

    op.create_table(
        "program_target",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("program_id", sa.String(length=36), nullable=False),
        sa.Column("section_id", sa.String(length=36), nullable=True),
        sa.Column("sequence", sa.Integer(), nullable=False),
        sa.Column("name", sa.String(length=200), nullable=False),
        sa.Column("hole_size_in", sa.Float(), nullable=True),
        sa.Column("casing_program", sa.String(length=120), nullable=True),
        sa.Column("formation_top", sa.String(length=120), nullable=True),
        sa.Column("planned_depth_md_value", sa.Float(), nullable=True),
        sa.Column("planned_depth_md_unit", sa.String(length=16), nullable=False),
        sa.Column("planned_duration_days", sa.Float(), nullable=True),
        sa.Column("planned_mud_weight_value", sa.Float(), nullable=True),
        sa.Column("planned_mud_weight_unit", sa.String(length=16), nullable=False),
        sa.Column("planned_npt_hours", sa.Float(), nullable=True),
        sa.Column("planned_cost_value", sa.Float(), nullable=True),
        sa.Column("planned_cost_unit", sa.String(length=16), nullable=False),
        sa.Column("provenance", sa.JSON(), nullable=True),
        sa.Column("origin", sa.String(length=16), nullable=False),
        sa.Column("attributes", sa.JSON(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["program_id"],
            ["drilling_program.id"],
            name=op.f("fk_program_target_program_id_drilling_program"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["section_id"],
            ["well_section.id"],
            name=op.f("fk_program_target_section_id_well_section"),
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_program_target")),
    )
    with op.batch_alter_table("program_target", schema=None) as batch_op:
        batch_op.create_index("ix_program_target_program", ["program_id", "sequence"], unique=False)
        batch_op.create_index("ix_program_target_section", ["section_id"], unique=False)

    op.create_table(
        "risk_record",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("code", sa.String(length=80), nullable=True),
        sa.Column("title", sa.String(length=400), nullable=False),
        sa.Column("category", sa.String(length=48), nullable=False),
        sa.Column("description", sa.Text(), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("revision_label", sa.String(length=64), nullable=True),
        sa.Column("supersedes_id", sa.String(length=36), nullable=True),
        sa.Column("status", sa.String(length=24), nullable=False),
        sa.Column("project_id", sa.String(length=36), nullable=True),
        sa.Column("field_id", sa.String(length=36), nullable=True),
        sa.Column("well_id", sa.String(length=36), nullable=True),
        sa.Column("section_id", sa.String(length=36), nullable=True),
        sa.Column("depth_from_value", sa.Float(), nullable=True),
        sa.Column("depth_from_unit", sa.String(length=16), nullable=False),
        sa.Column("depth_to_value", sa.Float(), nullable=True),
        sa.Column("depth_to_unit", sa.String(length=16), nullable=False),
        sa.Column("probability", sa.Integer(), nullable=True),
        sa.Column("impact", sa.Integer(), nullable=True),
        sa.Column("severity", sa.Integer(), nullable=True),
        sa.Column("severity_band", sa.String(length=16), nullable=True),
        sa.Column("scale", sa.String(length=24), nullable=False),
        sa.Column("causes", sa.JSON(), nullable=True),
        sa.Column("consequences", sa.JSON(), nullable=True),
        sa.Column("mitigation", sa.Text(), nullable=True),
        sa.Column("contingency", sa.Text(), nullable=True),
        sa.Column("owner", sa.String(length=200), nullable=True),
        sa.Column("source_note", sa.Text(), nullable=True),
        sa.Column("provenance", sa.JSON(), nullable=True),
        sa.Column("origin", sa.String(length=16), nullable=False),
        sa.Column("created_by", sa.String(length=200), nullable=False),
        sa.Column("attributes", sa.JSON(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "impact IS NULL OR (impact BETWEEN 1 AND 5)", name=op.f("ck_risk_record_impact_scale")
        ),
        sa.CheckConstraint(
            "probability IS NULL OR (probability BETWEEN 1 AND 5)",
            name=op.f("ck_risk_record_probability_scale"),
        ),
        sa.ForeignKeyConstraint(
            ["field_id"],
            ["field.id"],
            name=op.f("fk_risk_record_field_id_field"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["project_id"],
            ["project.id"],
            name=op.f("fk_risk_record_project_id_project"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["section_id"],
            ["well_section.id"],
            name=op.f("fk_risk_record_section_id_well_section"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["supersedes_id"],
            ["risk_record.id"],
            name=op.f("fk_risk_record_supersedes_id_risk_record"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["well_id"], ["well.id"], name=op.f("fk_risk_record_well_id_well"), ondelete="SET NULL"
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_risk_record")),
        sa.UniqueConstraint("code", "revision", name="uq_risk_code_revision"),
    )
    with op.batch_alter_table("risk_record", schema=None) as batch_op:
        batch_op.create_index("ix_risk_scope", ["field_id", "status"], unique=False)
        batch_op.create_index("ix_risk_well", ["well_id", "status"], unique=False)

    op.create_table(
        "well_operation",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("well_id", sa.String(length=36), nullable=False),
        sa.Column("section_id", sa.String(length=36), nullable=True),
        sa.Column("report_id", sa.String(length=36), nullable=True),
        sa.Column("operation_type", sa.String(length=48), nullable=False),
        sa.Column("label", sa.String(length=200), nullable=False),
        sa.Column("description", sa.Text(), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("ended_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("period_text", sa.String(length=200), nullable=True),
        sa.Column("depth_md_value", sa.Float(), nullable=True),
        sa.Column("depth_md_unit", sa.String(length=16), nullable=False),
        sa.Column("record_state", sa.String(length=16), nullable=False),
        sa.Column("status", sa.String(length=24), nullable=False),
        sa.Column("rig_id", sa.String(length=36), nullable=True),
        sa.Column("service_company_id", sa.String(length=36), nullable=True),
        sa.Column("provenance", sa.JSON(), nullable=True),
        sa.Column("origin", sa.String(length=16), nullable=False),
        sa.Column("created_by", sa.String(length=80), nullable=False),
        sa.Column("identity_key", sa.String(length=160), nullable=True),
        sa.Column("document_id", sa.String(length=36), nullable=True),
        sa.Column("document_version_id", sa.String(length=36), nullable=True),
        sa.Column("attributes", sa.JSON(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["document_id"],
            ["document.id"],
            name=op.f("fk_well_operation_document_id_document"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["document_version_id"],
            ["document_version.id"],
            name=op.f("fk_well_operation_document_version_id_document_version"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["report_id"],
            ["ddr_report.id"],
            name=op.f("fk_well_operation_report_id_ddr_report"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["rig_id"], ["rig.id"], name=op.f("fk_well_operation_rig_id_rig"), ondelete="SET NULL"
        ),
        sa.ForeignKeyConstraint(
            ["section_id"],
            ["well_section.id"],
            name=op.f("fk_well_operation_section_id_well_section"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["service_company_id"],
            ["service_company.id"],
            name=op.f("fk_well_operation_service_company_id_service_company"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["well_id"],
            ["well.id"],
            name=op.f("fk_well_operation_well_id_well"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_well_operation")),
        sa.UniqueConstraint("identity_key", name="uq_operation_identity"),
    )
    with op.batch_alter_table("well_operation", schema=None) as batch_op:
        batch_op.create_index("ix_operation_report", ["report_id"], unique=False)
        batch_op.create_index("ix_operation_type", ["operation_type", "well_id"], unique=False)
        batch_op.create_index("ix_operation_version", ["document_version_id"], unique=False)
        batch_op.create_index("ix_operation_well_start", ["well_id", "started_at"], unique=False)

    op.create_table(
        "well_event",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("well_id", sa.String(length=36), nullable=False),
        sa.Column("operation_id", sa.String(length=36), nullable=True),
        sa.Column("section_id", sa.String(length=36), nullable=True),
        sa.Column("report_id", sa.String(length=36), nullable=True),
        sa.Column("category", sa.String(length=48), nullable=False),
        sa.Column("event_type", sa.String(length=64), nullable=False),
        sa.Column("label", sa.String(length=200), nullable=False),
        sa.Column("description", sa.Text(), nullable=False),
        sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("ended_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("occurred_at_text", sa.String(length=200), nullable=True),
        sa.Column("severity", sa.String(length=16), nullable=True),
        sa.Column("depth_md_value", sa.Float(), nullable=True),
        sa.Column("depth_md_unit", sa.String(length=16), nullable=False),
        sa.Column("equipment_item_id", sa.String(length=36), nullable=True),
        sa.Column("rig_id", sa.String(length=36), nullable=True),
        sa.Column("service_company_id", sa.String(length=36), nullable=True),
        sa.Column("status", sa.String(length=24), nullable=False),
        sa.Column("record_state", sa.String(length=16), nullable=False),
        sa.Column("provenance", sa.JSON(), nullable=True),
        sa.Column("origin", sa.String(length=16), nullable=False),
        sa.Column("created_by", sa.String(length=80), nullable=False),
        sa.Column("identity_key", sa.String(length=160), nullable=True),
        sa.Column("document_id", sa.String(length=36), nullable=True),
        sa.Column("document_version_id", sa.String(length=36), nullable=True),
        sa.Column("attributes", sa.JSON(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["document_id"],
            ["document.id"],
            name=op.f("fk_well_event_document_id_document"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["document_version_id"],
            ["document_version.id"],
            name=op.f("fk_well_event_document_version_id_document_version"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["equipment_item_id"],
            ["knowledge_item.id"],
            name=op.f("fk_well_event_equipment_item_id_knowledge_item"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["operation_id"],
            ["well_operation.id"],
            name=op.f("fk_well_event_operation_id_well_operation"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["report_id"],
            ["ddr_report.id"],
            name=op.f("fk_well_event_report_id_ddr_report"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["rig_id"], ["rig.id"], name=op.f("fk_well_event_rig_id_rig"), ondelete="SET NULL"
        ),
        sa.ForeignKeyConstraint(
            ["section_id"],
            ["well_section.id"],
            name=op.f("fk_well_event_section_id_well_section"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["service_company_id"],
            ["service_company.id"],
            name=op.f("fk_well_event_service_company_id_service_company"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["well_id"], ["well.id"], name=op.f("fk_well_event_well_id_well"), ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_well_event")),
        sa.UniqueConstraint("identity_key", name="uq_event_identity"),
    )
    with op.batch_alter_table("well_event", schema=None) as batch_op:
        batch_op.create_index("ix_event_category", ["category", "well_id"], unique=False)
        batch_op.create_index("ix_event_operation", ["operation_id"], unique=False)
        batch_op.create_index("ix_event_version", ["document_version_id"], unique=False)
        batch_op.create_index("ix_event_well_time", ["well_id", "occurred_at"], unique=False)

    op.create_table(
        "npt_record",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("well_id", sa.String(length=36), nullable=False),
        sa.Column("event_id", sa.String(length=36), nullable=True),
        sa.Column("operation_id", sa.String(length=36), nullable=True),
        sa.Column("section_id", sa.String(length=36), nullable=True),
        sa.Column("report_id", sa.String(length=36), nullable=True),
        sa.Column("category", sa.String(length=48), nullable=False),
        sa.Column("subcategory", sa.String(length=80), nullable=True),
        sa.Column("description", sa.Text(), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("ended_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("started_at_text", sa.String(length=200), nullable=True),
        sa.Column("duration_hours", sa.Float(), nullable=True),
        sa.Column("duration_text", sa.String(length=80), nullable=True),
        sa.Column("duration_basis", sa.String(length=16), nullable=False),
        sa.Column("cause", sa.Text(), nullable=True),
        sa.Column("immediate_cause", sa.Text(), nullable=True),
        sa.Column("root_cause", sa.Text(), nullable=True),
        sa.Column("root_cause_status", sa.String(length=16), nullable=False),
        sa.Column("immediate_cause_status", sa.String(length=16), nullable=False),
        sa.Column("cost_impact_value", sa.Float(), nullable=True),
        sa.Column("cost_impact_unit", sa.String(length=16), nullable=False),
        sa.Column("rig_id", sa.String(length=36), nullable=True),
        sa.Column("service_company_id", sa.String(length=36), nullable=True),
        sa.Column("equipment_item_id", sa.String(length=36), nullable=True),
        sa.Column("status", sa.String(length=24), nullable=False),
        sa.Column("confidence", sa.Float(), nullable=True),
        sa.Column("provenance", sa.JSON(), nullable=True),
        sa.Column("origin", sa.String(length=16), nullable=False),
        sa.Column("created_by", sa.String(length=80), nullable=False),
        sa.Column("identity_key", sa.String(length=160), nullable=True),
        sa.Column("document_id", sa.String(length=36), nullable=True),
        sa.Column("document_version_id", sa.String(length=36), nullable=True),
        sa.Column("attributes", sa.JSON(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "duration_hours IS NULL OR duration_hours >= 0",
            name=op.f("ck_npt_record_duration_not_negative"),
        ),
        sa.ForeignKeyConstraint(
            ["document_id"],
            ["document.id"],
            name=op.f("fk_npt_record_document_id_document"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["document_version_id"],
            ["document_version.id"],
            name=op.f("fk_npt_record_document_version_id_document_version"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["equipment_item_id"],
            ["knowledge_item.id"],
            name=op.f("fk_npt_record_equipment_item_id_knowledge_item"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["event_id"],
            ["well_event.id"],
            name=op.f("fk_npt_record_event_id_well_event"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["operation_id"],
            ["well_operation.id"],
            name=op.f("fk_npt_record_operation_id_well_operation"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["report_id"],
            ["ddr_report.id"],
            name=op.f("fk_npt_record_report_id_ddr_report"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["rig_id"], ["rig.id"], name=op.f("fk_npt_record_rig_id_rig"), ondelete="SET NULL"
        ),
        sa.ForeignKeyConstraint(
            ["section_id"],
            ["well_section.id"],
            name=op.f("fk_npt_record_section_id_well_section"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["service_company_id"],
            ["service_company.id"],
            name=op.f("fk_npt_record_service_company_id_service_company"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["well_id"], ["well.id"], name=op.f("fk_npt_record_well_id_well"), ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_npt_record")),
        sa.UniqueConstraint("identity_key", name="uq_npt_identity"),
    )
    with op.batch_alter_table("npt_record", schema=None) as batch_op:
        batch_op.create_index("ix_npt_category", ["category", "well_id"], unique=False)
        batch_op.create_index("ix_npt_event", ["event_id"], unique=False)
        batch_op.create_index("ix_npt_version", ["document_version_id"], unique=False)
        batch_op.create_index("ix_npt_well_start", ["well_id", "started_at"], unique=False)

    op.create_table(
        "cost_item",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("project_id", sa.String(length=36), nullable=True),
        sa.Column("field_id", sa.String(length=36), nullable=True),
        sa.Column("well_id", sa.String(length=36), nullable=True),
        sa.Column("program_id", sa.String(length=36), nullable=True),
        sa.Column("wbs_code", sa.String(length=40), nullable=True),
        sa.Column("cbs_code", sa.String(length=40), nullable=True),
        sa.Column("cbs_path", sa.String(length=300), nullable=True),
        sa.Column("category", sa.String(length=48), nullable=False),
        sa.Column("description", sa.Text(), nullable=False),
        sa.Column("planned_value", sa.Float(), nullable=True),
        sa.Column("planned_unit", sa.String(length=16), nullable=False),
        sa.Column("actual_value", sa.Float(), nullable=True),
        sa.Column("actual_unit", sa.String(length=16), nullable=False),
        sa.Column("record_state", sa.String(length=16), nullable=False),
        sa.Column("npt_id", sa.String(length=36), nullable=True),
        sa.Column("status", sa.String(length=24), nullable=False),
        sa.Column("provenance", sa.JSON(), nullable=True),
        sa.Column("origin", sa.String(length=16), nullable=False),
        sa.Column("created_by", sa.String(length=80), nullable=False),
        sa.Column("identity_key", sa.String(length=160), nullable=True),
        sa.Column("attributes", sa.JSON(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["field_id"],
            ["field.id"],
            name=op.f("fk_cost_item_field_id_field"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["npt_id"],
            ["npt_record.id"],
            name=op.f("fk_cost_item_npt_id_npt_record"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["program_id"],
            ["drilling_program.id"],
            name=op.f("fk_cost_item_program_id_drilling_program"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["project_id"],
            ["project.id"],
            name=op.f("fk_cost_item_project_id_project"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["well_id"], ["well.id"], name=op.f("fk_cost_item_well_id_well"), ondelete="SET NULL"
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_cost_item")),
        sa.UniqueConstraint("identity_key", name="uq_cost_identity"),
    )
    with op.batch_alter_table("cost_item", schema=None) as batch_op:
        batch_op.create_index("ix_cost_project_cbs", ["project_id", "cbs_code"], unique=False)
        batch_op.create_index("ix_cost_well_category", ["well_id", "category"], unique=False)

    op.create_table(
        "problem_occurrence",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("well_id", sa.String(length=36), nullable=False),
        sa.Column("section_id", sa.String(length=36), nullable=True),
        sa.Column("operation_id", sa.String(length=36), nullable=True),
        sa.Column("event_id", sa.String(length=36), nullable=True),
        sa.Column("npt_id", sa.String(length=36), nullable=True),
        sa.Column("problem_type", sa.String(length=64), nullable=False),
        sa.Column("code", sa.String(length=80), nullable=True),
        sa.Column("description", sa.Text(), nullable=False),
        sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("depth_from_value", sa.Float(), nullable=True),
        sa.Column("depth_from_unit", sa.String(length=16), nullable=False),
        sa.Column("depth_to_value", sa.Float(), nullable=True),
        sa.Column("depth_to_unit", sa.String(length=16), nullable=False),
        sa.Column("hole_size_in", sa.Float(), nullable=True),
        sa.Column("formation", sa.String(length=120), nullable=True),
        sa.Column("immediate_cause", sa.Text(), nullable=True),
        sa.Column("immediate_cause_status", sa.String(length=16), nullable=False),
        sa.Column("root_cause", sa.Text(), nullable=True),
        sa.Column("root_cause_status", sa.String(length=16), nullable=False),
        sa.Column("contributing_factors", sa.JSON(), nullable=True),
        sa.Column("corrective_action", sa.Text(), nullable=True),
        sa.Column("preventive_action", sa.Text(), nullable=True),
        sa.Column("status", sa.String(length=24), nullable=False),
        sa.Column("confidence", sa.Float(), nullable=True),
        sa.Column("provenance", sa.JSON(), nullable=True),
        sa.Column("origin", sa.String(length=16), nullable=False),
        sa.Column("created_by", sa.String(length=80), nullable=False),
        sa.Column("identity_key", sa.String(length=160), nullable=True),
        sa.Column("document_id", sa.String(length=36), nullable=True),
        sa.Column("document_version_id", sa.String(length=36), nullable=True),
        sa.Column("attributes", sa.JSON(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["document_id"],
            ["document.id"],
            name=op.f("fk_problem_occurrence_document_id_document"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["document_version_id"],
            ["document_version.id"],
            name=op.f("fk_problem_occurrence_document_version_id_document_version"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["event_id"],
            ["well_event.id"],
            name=op.f("fk_problem_occurrence_event_id_well_event"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["npt_id"],
            ["npt_record.id"],
            name=op.f("fk_problem_occurrence_npt_id_npt_record"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["operation_id"],
            ["well_operation.id"],
            name=op.f("fk_problem_occurrence_operation_id_well_operation"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["section_id"],
            ["well_section.id"],
            name=op.f("fk_problem_occurrence_section_id_well_section"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["well_id"],
            ["well.id"],
            name=op.f("fk_problem_occurrence_well_id_well"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_problem_occurrence")),
        sa.UniqueConstraint("identity_key", name="uq_problem_identity"),
    )
    with op.batch_alter_table("problem_occurrence", schema=None) as batch_op:
        batch_op.create_index("ix_problem_type_time", ["problem_type", "occurred_at"], unique=False)
        batch_op.create_index("ix_problem_version", ["document_version_id"], unique=False)
        batch_op.create_index("ix_problem_well", ["well_id", "problem_type"], unique=False)

    op.create_table(
        "recommendation",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("signature", sa.String(length=200), nullable=False),
        sa.Column("statement", sa.Text(), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("evidence", sa.JSON(), nullable=True),
        sa.Column("query", sa.JSON(), nullable=True),
        sa.Column("status", sa.String(length=24), nullable=False),
        sa.Column("confidence", sa.Float(), nullable=True),
        sa.Column("applicability", sa.JSON(), nullable=True),
        sa.Column("pattern_id", sa.String(length=36), nullable=True),
        sa.Column("lesson_id", sa.String(length=36), nullable=True),
        sa.Column("practice_id", sa.String(length=36), nullable=True),
        sa.Column("problem_id", sa.String(length=36), nullable=True),
        sa.Column("risk_id", sa.String(length=36), nullable=True),
        sa.Column("procedure_id", sa.String(length=36), nullable=True),
        sa.Column("program_id", sa.String(length=36), nullable=True),
        sa.Column("project_id", sa.String(length=36), nullable=True),
        sa.Column("field_id", sa.String(length=36), nullable=True),
        sa.Column("well_id", sa.String(length=36), nullable=True),
        sa.Column("section_id", sa.String(length=36), nullable=True),
        sa.Column("operation_id", sa.String(length=36), nullable=True),
        sa.Column("generated_by", sa.String(length=80), nullable=False),
        sa.Column("decided_by", sa.String(length=120), nullable=True),
        sa.Column("decided_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("decline_reason", sa.Text(), nullable=True),
        sa.Column("attributes", sa.JSON(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["field_id"],
            ["field.id"],
            name=op.f("fk_recommendation_field_id_field"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["lesson_id"],
            ["lesson_learned.id"],
            name=op.f("fk_recommendation_lesson_id_lesson_learned"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["operation_id"],
            ["well_operation.id"],
            name=op.f("fk_recommendation_operation_id_well_operation"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["pattern_id"],
            ["field_pattern.id"],
            name=op.f("fk_recommendation_pattern_id_field_pattern"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["practice_id"],
            ["best_practice.id"],
            name=op.f("fk_recommendation_practice_id_best_practice"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["problem_id"],
            ["problem_occurrence.id"],
            name=op.f("fk_recommendation_problem_id_problem_occurrence"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["procedure_id"],
            ["procedure_record.id"],
            name=op.f("fk_recommendation_procedure_id_procedure_record"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["program_id"],
            ["drilling_program.id"],
            name=op.f("fk_recommendation_program_id_drilling_program"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["project_id"],
            ["project.id"],
            name=op.f("fk_recommendation_project_id_project"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["risk_id"],
            ["risk_record.id"],
            name=op.f("fk_recommendation_risk_id_risk_record"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["section_id"],
            ["well_section.id"],
            name=op.f("fk_recommendation_section_id_well_section"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["well_id"],
            ["well.id"],
            name=op.f("fk_recommendation_well_id_well"),
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_recommendation")),
        sa.UniqueConstraint("signature", name="uq_recommendation_signature"),
    )
    with op.batch_alter_table("recommendation", schema=None) as batch_op:
        batch_op.create_index(
            "ix_recommendation_field_status", ["field_id", "status"], unique=False
        )
        batch_op.create_index("ix_recommendation_well", ["well_id", "status"], unique=False)


def downgrade() -> None:
    # ### commands auto generated by Alembic - please adjust! ###
    with op.batch_alter_table("recommendation", schema=None) as batch_op:
        batch_op.drop_index("ix_recommendation_well")
        batch_op.drop_index("ix_recommendation_field_status")

    op.drop_table("recommendation")
    with op.batch_alter_table("problem_occurrence", schema=None) as batch_op:
        batch_op.drop_index("ix_problem_well")
        batch_op.drop_index("ix_problem_version")
        batch_op.drop_index("ix_problem_type_time")

    op.drop_table("problem_occurrence")
    with op.batch_alter_table("cost_item", schema=None) as batch_op:
        batch_op.drop_index("ix_cost_well_category")
        batch_op.drop_index("ix_cost_project_cbs")

    op.drop_table("cost_item")
    with op.batch_alter_table("npt_record", schema=None) as batch_op:
        batch_op.drop_index("ix_npt_well_start")
        batch_op.drop_index("ix_npt_version")
        batch_op.drop_index("ix_npt_event")
        batch_op.drop_index("ix_npt_category")

    op.drop_table("npt_record")
    with op.batch_alter_table("well_event", schema=None) as batch_op:
        batch_op.drop_index("ix_event_well_time")
        batch_op.drop_index("ix_event_version")
        batch_op.drop_index("ix_event_operation")
        batch_op.drop_index("ix_event_category")

    op.drop_table("well_event")
    with op.batch_alter_table("well_operation", schema=None) as batch_op:
        batch_op.drop_index("ix_operation_well_start")
        batch_op.drop_index("ix_operation_version")
        batch_op.drop_index("ix_operation_type")
        batch_op.drop_index("ix_operation_report")

    op.drop_table("well_operation")
    with op.batch_alter_table("risk_record", schema=None) as batch_op:
        batch_op.drop_index("ix_risk_well")
        batch_op.drop_index("ix_risk_scope")

    op.drop_table("risk_record")
    with op.batch_alter_table("program_target", schema=None) as batch_op:
        batch_op.drop_index("ix_program_target_section")
        batch_op.drop_index("ix_program_target_program")

    op.drop_table("program_target")
    with op.batch_alter_table("procedure_record", schema=None) as batch_op:
        batch_op.drop_index(
            "uq_procedure_one_current",
            sqlite_where=sa.text("is_current = 1 AND code IS NOT NULL"),
            postgresql_where=sa.text("is_current AND code IS NOT NULL"),
        )
        batch_op.drop_index("ix_procedure_well")
        batch_op.drop_index("ix_procedure_field_type")

    op.drop_table("procedure_record")
    with op.batch_alter_table("lesson_learned", schema=None) as batch_op:
        batch_op.drop_index(
            "uq_lesson_one_current",
            sqlite_where=sa.text("is_current = 1 AND code IS NOT NULL"),
            postgresql_where=sa.text("is_current AND code IS NOT NULL"),
        )
        batch_op.drop_index("ix_lesson_scope")
        batch_op.drop_index("ix_lesson_problem")

    op.drop_table("lesson_learned")
    with op.batch_alter_table("best_practice", schema=None) as batch_op:
        batch_op.drop_index(
            "uq_practice_one_current",
            sqlite_where=sa.text("is_current = 1 AND code IS NOT NULL"),
            postgresql_where=sa.text("is_current AND code IS NOT NULL"),
        )
        batch_op.drop_index("ix_practice_well")
        batch_op.drop_index("ix_practice_field_status")

    op.drop_table("best_practice")
    with op.batch_alter_table("drilling_program", schema=None) as batch_op:
        batch_op.drop_index(
            "uq_program_one_current",
            sqlite_where=sa.text("is_current = 1 AND code IS NOT NULL"),
            postgresql_where=sa.text("is_current AND code IS NOT NULL"),
        )
        batch_op.drop_index("ix_program_well")

    op.drop_table("drilling_program")
    with op.batch_alter_table("ddr_report", schema=None) as batch_op:
        batch_op.drop_index("ix_ddr_report_well_date")

    op.drop_table("ddr_report")
    with op.batch_alter_table("field_pattern", schema=None) as batch_op:
        batch_op.drop_index("ix_pattern_field")

    op.drop_table("field_pattern")
    with op.batch_alter_table("rig", schema=None) as batch_op:
        batch_op.drop_index("ix_rig_company")

    op.drop_table("rig")
    with op.batch_alter_table("service_company", schema=None) as batch_op:
        batch_op.drop_index("ix_service_company_type")

    op.drop_table("service_company")
    # ### end Alembic commands ###
