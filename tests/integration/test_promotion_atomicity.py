"""Promotion atomicity: a failure part-way through must leave the database as it was.

``VersionPromoter`` takes a session and never commits — the caller owns the transaction, and
``Database.unit_of_work`` rolls back on any exception. That is only a guarantee if it holds against a
failure raised *after* real rows have been written, which is the case that matters: a promoter that
validates everything up front and then writes cannot leak, but one that deletes, writes, reads and
writes again can.

The dangerous shape here is ``replace=True``, which deletes this version's promoted rows *before*
rewriting them. If the rewrite dies part-way, the delete must roll back with it — otherwise a failed
re-promotion leaves the well with nothing at all, which is worse than never having run.

These tests force the failure at that moment and then inspect the database, not merely that an
exception was raised.
"""

from __future__ import annotations

from typing import Any

import pytest
from sqlalchemy import func, select
from tests.fixtures.fieldops import ingest, promote_file, well_id_for

from drilling_intelligence.database.models import (
    DdrReport,
    Document,
    NptRecord,
    WellEvent,
    WellOperation,
)
from drilling_intelligence.operations.repository import OperationsRepository


def _counts(workspace) -> dict[str, int]:
    with workspace.database.read_only() as session:
        return {
            model.__tablename__: session.execute(
                select(func.count()).select_from(model)
            ).scalar_one()
            for model in (DdrReport, WellOperation, WellEvent, NptRecord)
        }


def _first_file(workspace) -> str:
    with workspace.database.read_only() as session:
        return str(session.execute(select(Document.filename)).scalars().first())


class _Explode(RuntimeError):
    """Raised from inside a repository write to simulate a late failure."""


class TestPromotionIsAtomic:
    @staticmethod
    def _populated(workspace) -> tuple[str, dict[str, int]]:
        ingest(workspace)
        filename = _first_file(workspace)
        promote_file(workspace, filename)
        counts = _counts(workspace)
        assert any(counts.values()), "the first promotion must have produced rows to lose"
        return filename, counts

    def test_a_failed_re_promotion_does_not_lose_the_rows_it_deleted(
        self, workspace, monkeypatch
    ) -> None:
        filename, before = self._populated(workspace)

        calls = {"n": 0}
        real = OperationsRepository.record_operation

        def exploding(self: Any, **kwargs: Any) -> Any:
            calls["n"] += 1
            if calls["n"] >= 2:
                raise _Explode("forced failure after the first operation was written")
            return real(self, **kwargs)

        monkeypatch.setattr(OperationsRepository, "record_operation", exploding)
        with pytest.raises(_Explode):
            promote_file(workspace, filename)
        assert calls["n"] >= 2, "the failure must land after at least one row was written"

        after = _counts(workspace)
        assert after == before, (
            f"a promotion that failed part-way changed the database: before={before} after={after}"
        )

    def test_a_failure_while_writing_children_leaves_no_partial_parent(
        self, workspace, monkeypatch
    ) -> None:
        """A parent must not survive holding only some of the children it was given."""
        filename, before = self._populated(workspace)

        def exploding(self: Any, **kwargs: Any) -> Any:
            raise _Explode("forced failure while writing children")

        monkeypatch.setattr(OperationsRepository, "record_npt", exploding)
        with pytest.raises(_Explode):
            promote_file(workspace, filename)

        after = _counts(workspace)
        assert after == before, f"partial rows survived a rollback: {before} -> {after}"

    def test_a_successful_retry_after_a_rollback_restores_the_same_state(
        self, workspace, monkeypatch
    ) -> None:
        """The rollback must not poison the next attempt."""
        filename, before = self._populated(workspace)

        real = OperationsRepository.record_operation
        state = {"fail": True}

        def maybe_explode(self: Any, **kwargs: Any) -> Any:
            if state["fail"]:
                raise _Explode("first attempt fails")
            return real(self, **kwargs)

        monkeypatch.setattr(OperationsRepository, "record_operation", maybe_explode)
        with pytest.raises(_Explode):
            promote_file(workspace, filename)

        state["fail"] = False
        promote_file(workspace, filename)
        after = _counts(workspace)
        assert after == before, (
            f"a successful retry did not restore the same state: {before} -> {after}"
        )
        assert well_id_for(workspace, "A-3"), "the well the corpus names must still resolve"
