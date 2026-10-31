"""lesson learned: promotion linkage for source-grounded writer (M6)

Revision ID: 0020
Revises: 0019
Create Date: 2026-10-31

LESSON_LEARNED moves from KNOWLEDGE_SUPPORTED to END_TO_END_CERTIFIED with a
narrow source-shaped writer.  A promoted lesson must carry the artefact it
came from and a stable identity so that re-promoting the same version is
idempotent and does not create duplicates.

The lesson table already has `origin`, `provenance` and `attributes`, but it
has no `document_id` / `document_version_id` / `identity_key` columns that
every other promoted domain table uses.  Those are added here as nullable
leaf columns, so an existing manual lesson keeps its values and a promoted
lesson can be addressed, superseded and pruned by version.

No data is backfilled: manual lessons stay manual, and a promoted lesson's
identity is written only when its contract handler creates it.  The unique
constraint on `identity_key` is the same as every other promoted table —
`promotion_identity(version_id, kind, table_id, row_index, well_id, extra)`
— and a second promotion of the same version therefore reports UNCHANGED
rather than creating a second row.  A superseded version leaves the old row
as history via `is_current = 0` and `SUPERSEDED` status, the same rule the
other domains already enforce.

This migration is historically deterministic: it does NOT import the live
``LessonLearned`` model for its target schema.  The target table is defined
explicitly within the migration, so a future model change (e.g. 0021's
``lower(code)`` index or 0022's ``code_key``) does not retroactively alter
what 0020 creates.  Downgrade likewise removes only 0020's artefacts.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy import Column, ForeignKey, String, Table, MetaData
from sqlalchemy import Integer, Boolean, Float, JSON, Text, DateTime

revision = "0020"
down_revision = "0019"
branch_labels = None
depends_on = None


def _target_table_0020() -> Table:
    """Explicit 0020 target table — does NOT include 0021/0022 artefacts."""
    from drilling_intelligence.database.base import NAMING_CONVENTION

    meta = MetaData(naming_convention=NAMING_CONVENTION)
    return Table(
        "lesson_learned",
        meta,
        Column("id", String(36), primary_key=True),
        Column("code", String(80)),
        Column("revision", Integer, nullable=False),
        Column("revision_label", String(64)),
        Column("is_current", Boolean, nullable=False),
        Column("supersedes_id", String(36), ForeignKey("lesson_learned.id", ondelete="SET NULL")),
        Column("status", String(24), nullable=False),
        Column("title", String(400), nullable=False),
        Column("problem_type", String(64)),
        Column("context", Text, nullable=False),
        Column("observation", Text, nullable=False),
        Column("root_cause", Text),
        Column("root_cause_status", String(16), nullable=False),
        Column("lesson", Text, nullable=False),
        Column("recommendation", Text, nullable=False),
        Column("project_id", String(36), ForeignKey("project.id", ondelete="SET NULL")),
        Column("field_id", String(36), ForeignKey("field.id", ondelete="SET NULL")),
        Column("well_id", String(36), ForeignKey("well.id", ondelete="SET NULL")),
        Column("section_id", String(36), ForeignKey("well_section.id", ondelete="SET NULL")),
        Column("applicable_operations", JSON),
        Column("applicable_formations", JSON),
        Column("hole_size_in", Float),
        Column("depth_from_value", Float),
        Column("depth_from_unit", String(16), nullable=False),
        Column("depth_to_value", Float),
        Column("depth_to_unit", String(16), nullable=False),
        Column("conditions", Text),
        Column("created_by", String(200), nullable=False),
        Column("reviewer", String(200)),
        Column("reviewed_at", DateTime(timezone=True)),
        Column("approved_by", String(200)),
        Column("approved_at", DateTime(timezone=True)),
        Column("confidence", Float),
        Column("provenance", JSON),
        Column("origin", String(16), nullable=False),
        Column("attributes", JSON),
        Column("status_note", Text),
        Column("identity_key", String(200)),
        Column("document_id", String(36), ForeignKey("document.id", ondelete="SET NULL")),
        Column("document_version_id", String(36), ForeignKey("document_version.id", ondelete="SET NULL")),
        Column("created_at", DateTime, nullable=False),
        Column("updated_at", DateTime, nullable=False),
        sa.UniqueConstraint("code", "revision", name="uq_lesson_code_revision"),
        sa.Index("ix_lesson_scope", "field_id", "status"),
        sa.Index("ix_lesson_problem", "problem_type", "status"),
        sa.Index(
            "uq_lesson_one_current",
            "code",
            unique=True,
            sqlite_where=sa.text("is_current = 1 AND code IS NOT NULL"),
            postgresql_where=sa.text("is_current AND code IS NOT NULL"),
        ),
        sa.UniqueConstraint("identity_key", name="uq_lesson_identity"),
        sa.Index("ix_lesson_version", "document_version_id"),
        # NOTE: 0021's lower(code) and 0022's code_key indexes are intentionally not here
    )


def upgrade() -> None:
    # SQLite can add nullable columns without a table rebuild, and --sql must
    # render without a live connection.  Use plain ADD COLUMN for the three
    # new leaf columns.
    op.add_column("lesson_learned", sa.Column("identity_key", sa.String(length=200), nullable=True))
    op.add_column("lesson_learned", sa.Column("document_id", sa.String(length=36), nullable=True))
    op.add_column(
        "lesson_learned", sa.Column("document_version_id", sa.String(length=36), nullable=True)
    )
    # FKs/indexes/unique need a table rebuild on SQLite.  Use an explicit
    # 0020 target table for the rebuild so the migration does not inherit
    # future schema (0021/0022) from the live model.  The batch will create
    # exactly the 0020 artefacts: uq_lesson_identity, ix_lesson_version,
    # fk_lesson_learned_document_id_document, fk_lesson_learned_document_version_id_document_version.
    # ``recreate="always"`` forces the rebuild even when the live DB is empty
    # (needed for offline --sql rendering where the reflection DB is in-memory).
    with op.batch_alter_table("lesson_learned", copy_from=_target_table_0020(), recreate="always"):
        pass


def _target_table_0019() -> Table:
    """Explicit 0019 table — 0020 columns not yet present."""
    from drilling_intelligence.database.base import NAMING_CONVENTION

    meta = MetaData(naming_convention=NAMING_CONVENTION)
    return Table(
        "lesson_learned",
        meta,
        Column("id", String(36), primary_key=True),
        Column("code", String(80)),
        Column("revision", Integer, nullable=False),
        Column("revision_label", String(64)),
        Column("is_current", Boolean, nullable=False),
        Column("supersedes_id", String(36), ForeignKey("lesson_learned.id", ondelete="SET NULL")),
        Column("status", String(24), nullable=False),
        Column("title", String(400), nullable=False),
        Column("problem_type", String(64)),
        Column("context", Text, nullable=False),
        Column("observation", Text, nullable=False),
        Column("root_cause", Text),
        Column("root_cause_status", String(16), nullable=False),
        Column("lesson", Text, nullable=False),
        Column("recommendation", Text, nullable=False),
        Column("project_id", String(36), ForeignKey("project.id", ondelete="SET NULL")),
        Column("field_id", String(36), ForeignKey("field.id", ondelete="SET NULL")),
        Column("well_id", String(36), ForeignKey("well.id", ondelete="SET NULL")),
        Column("section_id", String(36), ForeignKey("well_section.id", ondelete="SET NULL")),
        Column("applicable_operations", JSON),
        Column("applicable_formations", JSON),
        Column("hole_size_in", Float),
        Column("depth_from_value", Float),
        Column("depth_from_unit", String(16), nullable=False),
        Column("depth_to_value", Float),
        Column("depth_to_unit", String(16), nullable=False),
        Column("conditions", Text),
        Column("created_by", String(200), nullable=False),
        Column("reviewer", String(200)),
        Column("reviewed_at", DateTime(timezone=True)),
        Column("approved_by", String(200)),
        Column("approved_at", DateTime(timezone=True)),
        Column("confidence", Float),
        Column("provenance", JSON),
        Column("origin", String(16), nullable=False),
        Column("attributes", JSON),
        Column("status_note", Text),
        Column("created_at", DateTime, nullable=False),
        Column("updated_at", DateTime, nullable=False),
        sa.UniqueConstraint("code", "revision", name="uq_lesson_code_revision"),
        sa.Index("ix_lesson_scope", "field_id", "status"),
        sa.Index("ix_lesson_problem", "problem_type", "status"),
        sa.Index(
            "uq_lesson_one_current",
            "code",
            unique=True,
            sqlite_where=sa.text("is_current = 1 AND code IS NOT NULL"),
            postgresql_where=sa.text("is_current AND code IS NOT NULL"),
        ),
    )


def downgrade() -> None:
    # Downgrade to 0019: rebuild without the three 0020 columns and their
    # constraints.  Using the explicit 0019 target ensures we do not attempt
    # to drop 0021/0022 artefacts that 0020 never created.
    with op.batch_alter_table("lesson_learned", copy_from=_target_table_0019(), recreate="always"):
        pass
