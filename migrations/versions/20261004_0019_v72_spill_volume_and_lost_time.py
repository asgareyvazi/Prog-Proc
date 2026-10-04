"""close the half-typed V7.2 boundary: spill volume becomes a fact, lost time stays words

Revision ID: 0019
Revises: 0018
Create Date: 2026-10-04

V7.2 shipped parsers that produced typed quantities the models could not hold.  ``HseIncidentEntry``
parsed ``spill_volume_text/value/unit`` and both entries parsed ``npt_hours_value``, while
``hse_incident`` and ``well_control_event`` had no columns for any of them - so every one of those
numbers was silently dropped on the way to the database.  A parser abstraction that looks
authoritative but never reaches the model is worse than one that was never written, because it reads
like a stored fact.

The two quantities are not alike, so they do not get the same answer.

**Spill volume is promoted.**  How much was released is a first-class fact of an environmental
incident, the source states it directly, and the repository already keeps quantities as a
text/value/unit triple - ``well_control_event.sidpp_text/sidpp_value/sidpp_unit`` is exactly that
shape.  The value is populated only where the header also stated a unit; ``Spill Volume = 3`` with no
unit stays text, because guessing bbl for an environmental quantity is the guess that gets reported
in m3 downstream.  No conversion is applied.

**Lost time is not promoted.**  ``NPT Hours = 6.5`` states a duration.  It does not authorise an
``NptRecord`` - NPT is a classified record with its own contract, code and problem definition - and
it does not identify one either, because nothing in the cell names a row.  ``npt_hours_value`` is
therefore removed from both parsers rather than stored: a number with no unit discipline and no
relationship to keep would only invite someone to read it as one.  The source's own wording survives
in ``npt_hours_text``, and ``npt_id`` stays NULL unless the source named an actual NPT row.

``server_default`` is used here and nowhere in the ORM, because these are ``add_column`` operations
against tables that may already hold rows and a ``NOT NULL`` column needs a default to be added at
all.  The tables are new in this same wave, so the default is only ever load-bearing for an empty
table.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0019"
down_revision = "0018"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("hse_incident", sa.Column("spill_volume_text", sa.Text(), nullable=True))
    op.add_column("hse_incident", sa.Column("spill_volume_value", sa.Float(), nullable=True))
    op.add_column(
        "hse_incident",
        sa.Column("spill_volume_unit", sa.String(length=24), nullable=False, server_default=""),
    )
    op.add_column("hse_incident", sa.Column("npt_hours_text", sa.Text(), nullable=True))
    op.add_column("well_control_event", sa.Column("npt_hours_text", sa.Text(), nullable=True))


def downgrade() -> None:
    # Plain drops: these are nullable leaf columns with no index, constraint or dependent row, so
    # there is nothing to unwind in order.
    op.drop_column("well_control_event", "npt_hours_text")
    op.drop_column("hse_incident", "npt_hours_text")
    op.drop_column("hse_incident", "spill_volume_unit")
    op.drop_column("hse_incident", "spill_volume_value")
    op.drop_column("hse_incident", "spill_volume_text")
