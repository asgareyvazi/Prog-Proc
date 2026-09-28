"""A date the caller supplied must not be laundered into "no date".

Two related defects in the operations write path, both silent:

*   ``_stamp`` handled ``datetime`` and ``str`` but not a bare ``datetime.date``.  ``date`` is the
    *parent* of ``datetime``, so ``isinstance(value, datetime)`` is False for it, and the value fell
    through to ``None``.  Recording a problem with ``occurred_at=date(2025, 7, 1)`` stored no
    timestamp at all — and a stale-snapshot test had been written against that behaviour, asserting
    that adding a July occurrence did not move ``last_seen_at``.
*   A string that was supplied but could not be parsed also returned ``None``, so
    ``ended_at="14 June 2025"`` was stored as ``ended_at IS NULL``: a mistyped end date became an
    operation that never ended, with nothing anywhere saying a date had been dropped.  The same file
    already refuses to do this for a *query* bound, because "both a wrong answer and no answer look
    like an answer"; a record is worse, because the silence is persisted.

Real SQLite, real repository, real timeline read-back.
"""

from __future__ import annotations

from datetime import UTC, date, datetime

import pytest
from sqlalchemy import select

from drilling_intelligence.core.errors import ValidationError
from drilling_intelligence.database.models import WellOperation
from drilling_intelligence.operations.repository import OperationsRepository
from drilling_intelligence.wells.repository import WellRepository


def _well(session, tmp_path) -> str:
    repository = WellRepository(session)
    repository.get_or_create_workspace(str(tmp_path), name="Dates")
    project = repository.get_or_create_project("Date Block")
    return repository.create_well("D-1", project_id=project.id).id


class TestASuppliedDateIsNeverDropped:
    def test_a_bare_date_is_stored_as_that_day(self, session, tmp_path) -> None:
        well = _well(session, tmp_path)
        OperationsRepository(session).record_operation(
            well_id=well,
            operation_type="drilling",
            label="dated by day",
            started_at=date(2025, 7, 1),
        )
        session.flush()
        row = session.execute(select(WellOperation)).scalars().first()
        assert row.started_at is not None, "a supplied date was stored as no date at all"
        assert row.started_at.date() == date(2025, 7, 1), row.started_at

    def test_an_unreadable_date_is_refused_not_stored_as_null(self, session, tmp_path) -> None:
        well = _well(session, tmp_path)
        with pytest.raises(ValidationError, match="not a date or ISO timestamp"):
            OperationsRepository(session).record_operation(
                well_id=well,
                operation_type="drilling",
                label="mistyped end",
                started_at=datetime(2025, 6, 12, 8, 0, tzinfo=UTC),
                ended_at="14 June 2025",
            )

    def test_an_absent_date_is_still_legal(self, session, tmp_path) -> None:
        """Refusing an unreadable value must not turn into refusing an absent one."""
        well = _well(session, tmp_path)
        OperationsRepository(session).record_operation(
            well_id=well, operation_type="drilling", label="open-ended"
        )
        session.flush()
        row = session.execute(select(WellOperation)).scalars().first()
        assert row.started_at is None and row.ended_at is None

    def test_an_iso_string_is_still_read(self, session, tmp_path) -> None:
        well = _well(session, tmp_path)
        OperationsRepository(session).record_operation(
            well_id=well,
            operation_type="drilling",
            label="iso",
            started_at="2025-06-12T08:00:00",
        )
        session.flush()
        row = session.execute(select(WellOperation)).scalars().first()
        assert row.started_at is not None and row.started_at.year == 2025
