"""a calculation input names the durable thing it consumed, or admits it cannot

Revision ID: 0008
Revises: 0007
Create Date: 2026-09-09

``calculation_input.subject_key`` has been a free-form string since 0001: whatever the caller
passed as ``subject_key`` (or, failing that, ``source``), stripped and truncated to 300
characters.  ``EngineeringRepository.calculations_using`` then matched it with ``=``.  That pair
answers the change-impact question - "this input changed; what has to be re-run?" - and answers
it wrongly whenever the two spellings differ: ``property:MUD_WEIGHT`` does not find
``property:mud_weight``, a permuted component order finds nothing, and a key longer than the
column was truncated on the way in and so was unfindable by the key its own author used.  A
question that returns an empty list when the honest answer is "three calculations" is the one
failure mode this platform exists to prevent.

This revision adds the two columns that turn that string into a reference:

*   ``subject_kind`` - the anchor kind (``well``, ``section``, ``document_version``,
    ``document``, ``project``), or the literal ``legacy`` for a value that cannot be recognised;
*   ``subject_id`` - the anchor's id, NULL whenever the kind is ``legacy``;

plus ``ix_calc_input_subject_ref`` over the pair.  Both columns are nullable, neither carries a
foreign key - there is no single FK target, since a subject may be a well, a section, a document
version, a document or a project, and a constraint would have to name one and forbid the rest
(the same reasoning 0005 recorded for ``calculation``'s citation columns) - and no existing
column is renamed, widened or dropped.

**The backfill never guesses.**  It resolves a value only when the leading component is exactly
``<known anchor>:<id>`` with a non-empty id and no escape character anywhere in the string - the
shape the knowledge layer's ``lookup_key`` has always produced.  Everything else
(``well:A-3|mud_weight`` with no ``property:`` component, ``mud_report.xlsx!Summary!B9``, a bare
token) keeps its ``subject_key`` byte-for-byte and is labelled ``subject_kind = 'legacy'`` with a
NULL ``subject_id``.  That is a label, not an interpretation: the impact query reports such rows
as *unresolved*, which is a different answer from *not affected*.

**No calculation identity is touched.**  ``calculation.identity_key`` is a hash over the payload
(method, version, scope, inputs, outputs, assumptions, validation, uncertainty, confidence,
triggered-by, provenance).  This revision writes only to ``calculation_input``, adds no column to
``calculation`` and rewrites no ``inputs`` JSON, so a historical record keeps the identity it was
recorded under and re-recording it stays a no-op.

The statements use ``substr``, ``length`` and the dialect's position function (``instr`` on
SQLite, ``strpos`` on PostgreSQL) chosen from the migration context, so ``alembic upgrade --sql``
renders a script a DBA can read and offline output matches what a live run does.  The downgrade
is the exact inverse - drop the index, drop the two columns - which is possible precisely because
the backfill only ever added information *beside* the original string.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0008"
down_revision = "0007"
branch_labels = None
depends_on = None

#: The anchor kinds the backfill recognises, longest first so ``document_version`` is matched
#: before ``document`` would swallow its prefix.  Kept in step with ``core.ids.ANCHOR_KINDS``.
_ANCHOR_KINDS = ("document_version", "document", "section", "project", "well")

_INDEX = "ix_calc_input_subject_ref"

#: The table as this migration sees it: only the columns it reads and writes.
_INPUT = sa.table(
    "calculation_input",
    sa.column("subject_key", sa.String),
    sa.column("subject_kind", sa.String),
    sa.column("subject_id", sa.String),
)


def _columns() -> list[sa.Column]:
    """The columns this revision adds, in declaration order.

    A function rather than a module constant: a ``sa.Column`` is bound to the table it is first
    emitted against, so reusing one object across upgrade and downgrade is a subtle bug.
    """
    return [
        sa.Column("subject_kind", sa.String(length=32), nullable=True),
        sa.Column("subject_id", sa.String(length=64), nullable=True),
    ]


def _position_function() -> object:
    """The dialect's "index of substring" function, resolved without needing a live connection."""
    dialect = op.get_context().dialect.name
    return sa.func.instr if dialect == "sqlite" else sa.func.strpos


def upgrade() -> None:
    for column in _columns():
        op.add_column("calculation_input", column)
    op.create_index(_INDEX, "calculation_input", ["subject_kind", "subject_id"])

    position = _position_function()
    for kind in _ANCHOR_KINDS:
        prefix = f"{kind}:"
        offset = len(prefix) + 1
        remainder = sa.func.substr(_INPUT.c.subject_key, offset)
        separator_at = position(remainder, "|")
        # The anchor id is everything after "<kind>:" and before the first "|" - or the whole
        # remainder when the key has no further component.  One statement, no dialect branch
        # beyond the position function itself.
        anchor_id = sa.case(
            (separator_at > 0, sa.func.substr(remainder, 1, separator_at - 1)),
            else_=remainder,
        )
        # What follows the anchor, "" when the anchor is the whole key.
        tail = sa.case(
            (separator_at > 0, sa.func.substr(remainder, separator_at + 1)),
            else_=sa.literal(""),
        )
        op.execute(
            _INPUT.update()
            .where(
                _INPUT.c.subject_kind.is_(None),
                _INPUT.c.subject_key.is_not(None),
                _INPUT.c.subject_key.like(f"{prefix}%"),
                # A backslash anywhere means a component was escaped, and naive string surgery
                # would no longer agree with core.ids.SubjectKey.parse.  Such a row is left for
                # the legacy pass rather than resolved approximately.
                sa.not_(_INPUT.c.subject_key.like("%\\%")),
                # The anchor id must be a single opaque token.  A ":" inside it means the string
                # is not the shape it looks like ("well:a:b|property:x" is not a well called
                # "a:b" - core.ids would have escaped that colon), so resolving it would invent
                # an identity the runtime rule does not agree with.
                position(anchor_id, ":") == 0,
                anchor_id != "",
                # Whatever follows the anchor must itself be a recognised component.  Without
                # this, free text that merely starts with a known prefix - "well:A-3|mud_weight",
                # which has no "property:" component and is therefore *not* a canonical key -
                # would be silently promoted to a real well reference.  That is precisely the
                # guessing this migration promises not to do, so such a row stays legacy.
                sa.or_(
                    tail == "",
                    *(
                        tail.like(f"{field}:%")
                        for field in ("well", "section", "property", "state", "document", "project")
                    ),
                ),
            )
            .values(subject_kind=kind, subject_id=anchor_id)
        )

    # Anything still unresolved - or resolved to an empty id, as ``well:|property:x`` would be -
    # is legacy, and is labelled rather than dropped or invented.
    op.execute(
        _INPUT.update()
        .where(
            _INPUT.c.subject_key.is_not(None),
            _INPUT.c.subject_key != "",
            sa.or_(
                _INPUT.c.subject_kind.is_(None),
                _INPUT.c.subject_id.is_(None),
                _INPUT.c.subject_id == "",
            ),
        )
        .values(subject_kind="legacy", subject_id=None)
    )


def downgrade() -> None:
    op.drop_index(_INDEX, table_name="calculation_input")
    for column in reversed(_columns()):
        op.drop_column("calculation_input", column.name)
