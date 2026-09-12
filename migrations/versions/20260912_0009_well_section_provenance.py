"""a hole section can say why it exists

Revision ID: 0009
Revises: 0008
Create Date: 2026-09-12

``well_section`` has carried the hierarchy since 0001 and, alone among the tables a document can
populate, it has never been able to answer "which source says this section exists".  Every other
promoted row - ``ddr_report``, ``well_operation``, ``well_event``, ``npt_record``,
``problem_occurrence``, ``procedure_record``, ``drilling_program``, ``calculation`` - carries
``origin`` plus a ``provenance`` list and, since 0005 for calculations, the document and version it
was read from; ``check_promoted_evidence`` holds all of them to that promise.  A section could not
join that check because it had nowhere to record the answer.

This revision adds the four columns that let it:

*   ``origin`` - the existing ``KnowledgeOrigin`` vocabulary (``MANUAL``/``EXTRACTED``/``DERIVED``),
    not a new one, defaulted to ``MANUAL`` and NOT NULL;
*   ``provenance`` - the same JSON list of locator dictionaries every other row uses;
*   ``document_id`` / ``document_version_id`` - nullable ``SET NULL`` citations, the shape 0005 used
    for ``calculation``;

plus ``ix_section_version`` over ``document_version_id``, matching ``ix_operation_version`` and its
siblings from 0004.

**Every existing row is labelled ``MANUAL``, and that is a statement rather than a guess.**  A
section that predates this revision was written by a person or a fixture through
``get_or_create_section``; no document promotion has ever created one (there is no such writer in
the codebase at this revision).  ``MANUAL`` is precisely the origin that means "the platform did not
derive this", it is the one origin a rebuild never deletes, and it is the same backfill 0005 chose
for the same reason.  No section's name, sequence, depths, durations or mud weights are read or
rewritten here, so no row changes what it means.

**No depth column is added, renamed or dropped.**  The planned/actual depth defect this revision
accompanies is not a storage problem: ``top_depth_value``/``bottom_depth_value`` have always been the
as-drilled interval (``_section_actuals`` reports them as the actual depth, and
``PLAN_ACTUAL_METRICS`` reads the *planned* depth from ``program_target.planned_depth_md_value``).
The defect was that ``WellRepository.update_section`` also accepted them under ``state=PLANNED``,
which let an intention be stored in the column a comparison reports as fact.  That is fixed in the
repository, where the rule lives; inventing ``planned_top_depth``/``planned_bottom_depth`` columns
would have created a second home for a number the program target already owns.

The downgrade is the exact inverse - drop the index, drop the four columns - which is possible
because this revision only ever adds information beside what was already there.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0009"
down_revision = "0008"
branch_labels = None
depends_on = None

_TABLE = "well_section"
_INDEX = "ix_section_version"

#: Added in this order; dropped in the reverse.
ADDED: tuple[str, ...] = ("origin", "provenance", "document_id", "document_version_id")


def _columns() -> list[sa.Column]:
    """The four columns, in the shape 0005 added the same citation pair to ``calculation``.

    The citations are declared without an inline ``ForeignKey``: SQLite cannot ``ALTER TABLE`` a
    constraint into an existing table, so a migration that named one here would be unrunnable on the
    dialect every workspace uses (the alternative, a batch copy-and-move of ``well_section``, would
    rebuild a table eleven other tables point at, to add a constraint 0005 already decided it could
    live without).  The relationship is declared on the model, which is what the application reads.
    """
    return [
        sa.Column("origin", sa.String(length=16), nullable=False, server_default="MANUAL"),
        sa.Column("provenance", sa.JSON(), nullable=True),
        sa.Column("document_id", sa.String(length=36), nullable=True),
        sa.Column("document_version_id", sa.String(length=36), nullable=True),
    ]


def upgrade() -> None:
    for column in _columns():
        op.add_column(_TABLE, column)
    op.create_index(_INDEX, _TABLE, ["document_version_id"])
    # A row that arrived before this revision was not derived from a document: no promoter has ever
    # written a section.  ``MANUAL`` states that, and is the origin a rebuild never touches.
    op.execute("UPDATE well_section SET origin = 'MANUAL' WHERE origin IS NULL OR origin = ''")


def downgrade() -> None:
    op.drop_index(_INDEX, table_name=_TABLE)
    for name in reversed(ADDED):
        op.drop_column(_TABLE, name)
