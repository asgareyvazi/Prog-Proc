"""knowledge facts: subject, predicate, normalised value, validity window, origin

Revision ID: 0003
Revises: 0002
Create Date: 2026-09-05

The knowledge layer (:mod:`drilling_intelligence.knowledge`) turns deterministic extraction
output into typed facts, and it needs columns that ``knowledge_item`` - created by 0001 as a
generic "structured knowledge object" - does not have:

*   ``entity_type`` / ``entity_id``: *what the value belongs to*.  A fact about a well, a hole
    section, a bit or a document version is addressed the same way, and it is the same pair a
    ``knowledge_relation`` edge carries, so facts and edges can be walked together without a
    table per entity type.
*   ``predicate``: the assertion (``mud_weight``, ``casing_size``).  Conflict detection groups
    by this, so it is indexed with ``status`` rather than hidden inside ``payload`` JSON.
*   ``value_type``: how a value is compared and normalised (quantity, text, date, boolean,
    ratio).
*   ``original_value`` / ``original_unit``: the source's own wording.  ``value``/``unit`` are
    the parsed pair, ``normalized_value``/``normalized_unit`` the comparable one - and a reader
    reconciling 12.5 ppg against a program's "12,50 kg/m3" needs the string the document
    actually contained.  Normalisation must never destroy the evidence it started from.
*   ``valid_from`` / ``valid_to``: an operational value is reported *for* something (a shift, a
    section, a bit run).  A fact without a window silently reads as permanent.
*   ``origin``: EXTRACTED / DERIVED / MANUAL.  This is what makes ``knowledge rebuild`` safe:
    it regenerates what a parser produced and never touches what a person typed.

Rows written before the knowledge layer existed are backfilled as ``MANUAL``: nobody can prove
an extractor wrote them, and an unattributed row must be treated as something a person made -
the one thing a rebuild is never allowed to delete.

Columns are added nullable-with-server-default where the model wants NOT NULL, which is the only
form SQLite accepts on a table that already has rows; the defaults keep existing rows valid and
the ORM always supplies a value for new ones.  Indexes are created after every column exists -
``ix_knowledge_subject`` spans two of them - and the downgrade drops indexes before columns,
because SQLite refuses to drop a column an index still names.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0003"
down_revision = "0002"
branch_labels = None
depends_on = None


#: Added in this order; the downgrade reverses it.
COLUMNS: tuple[sa.Column, ...] = (
    sa.Column("entity_type", sa.String(length=32), nullable=True),
    sa.Column("entity_id", sa.String(length=36), nullable=True),
    sa.Column("predicate", sa.String(length=120), nullable=True),
    sa.Column("value_type", sa.String(length=16), nullable=False, server_default="text"),
    sa.Column("original_value", sa.Text(), nullable=False, server_default=""),
    sa.Column("original_unit", sa.String(length=24), nullable=False, server_default=""),
    sa.Column("normalized_value", sa.Float(), nullable=True),
    sa.Column("normalized_unit", sa.String(length=24), nullable=False, server_default=""),
    sa.Column("valid_from", sa.DateTime(timezone=True), nullable=True),
    sa.Column("valid_to", sa.DateTime(timezone=True), nullable=True),
    sa.Column("origin", sa.String(length=16), nullable=False, server_default="MANUAL"),
)

#: ``(name, columns)`` - every column named here already exists by the time they are created.
INDEXES: tuple[tuple[str, list[str]], ...] = (
    ("ix_knowledge_subject", ["entity_type", "entity_id"]),
    ("ix_knowledge_predicate", ["predicate", "status"]),
    ("ix_knowledge_version", ["document_version_id", "origin"]),
)


def upgrade() -> None:
    for column in COLUMNS:
        op.add_column("knowledge_item", column)
    for name, columns in INDEXES:
        op.create_index(name, "knowledge_item", columns, unique=False)
    op.execute("UPDATE knowledge_item SET origin = 'MANUAL' WHERE origin IS NULL OR origin = ''")


def downgrade() -> None:
    for name, _columns in reversed(INDEXES):
        op.drop_index(name, table_name="knowledge_item")
    for column in reversed(COLUMNS):
        op.drop_column("knowledge_item", column.name)
