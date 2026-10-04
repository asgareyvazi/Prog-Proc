"""Ledger row 33: measured scale behaviour at 1 000 and 10 000 rows.

The repository defines no numeric latency SLA, so the certification criterion here is *structural*,
and it is the one the architecture actually promises:

* query count does not grow per row - no N+1 on the hot read paths;
* reads stay inside the documented bounds (``MAX_CANDIDATES``, ``RETRIEVAL_CAP``, the review
  ``_SAFE_LIMIT``) and report truncation truthfully when a bound is reached;
* growth from 1 000 to 10 000 is explained by the shape of the operation, not by item count.

Every number asserted below was measured first and then pinned, so the test fails if an
optimisation or a regression changes the shape.  Timings are recorded but **not** asserted against
an invented threshold - a wall-clock number on shared hardware is not a project SLA.
"""

from __future__ import annotations

import time
from typing import Any

import pytest
from sqlalchemy import event, func, select

from drilling_intelligence.core.enums import RecordState
from drilling_intelligence.database.models import WellEvent, WellOperation
from drilling_intelligence.intelligence.timeline import build_timeline
from drilling_intelligence.review import DomainReviewRequest, DomainReviewService
from drilling_intelligence.search.service import SearchService
from drilling_intelligence.wells.repository import WellRepository


def _select_count(engine, fn) -> int:
    """Count SELECT statements a callable issues - the N+1 detector."""
    count = {"n": 0}

    def before(_conn, _cursor, statement, *_args, **_kwargs) -> None:
        if str(statement).lstrip().upper().startswith("SELECT"):
            count["n"] += 1

    event.listen(engine, "before_cursor_execute", before)
    try:
        fn()
    finally:
        event.remove(engine, "before_cursor_execute", before)
    return count["n"]


def _build(workspace, rows: int) -> str:
    """A deterministic corpus of ``rows`` operations on one well, written through the ORM."""
    with workspace.database.unit_of_work() as session:
        wells = WellRepository(session)
        wells.get_or_create_workspace(str(workspace.root), name="Scale")
        project = wells.get_or_create_project("Scale Project")
        field = wells.get_or_create_field("Scale Field", project=project)
        well = wells.create_well("SCALE-1", project_id=project.id, field_id=field.id)
        session.add_all(
            WellOperation(
                id=f"op-{rows}-{index:06d}",
                well_id=well.id,
                operation_type="drilling",
                label=f"operation {index}",
                description=f"scale corpus row {index} of {rows}",
                record_state=str(RecordState.ACTUAL.value),
                status="CONFIRMED",
            )
            for index in range(rows)
        )
        # WellOperation is *not* part of the search index's structured population - WellEvent,
        # NptRecord, ProblemOccurrence and LessonLearned are.  Without these the search numbers
        # below would be measured against an empty index and mean nothing.
        session.add_all(
            WellEvent(
                id=f"ev-{rows}-{index:06d}",
                well_id=well.id,
                category="operations",
                event_type="connection",
                label=f"zermatt event {index}",
                description=f"scale corpus event {index} of {rows}",
                record_state=str(RecordState.ACTUAL.value),
                status="CONFIRMED",
            )
            for index in range(rows)
        )
        return well.id


def _measure(workspace, well_id: str) -> dict[str, Any]:
    """Run the real service entry points and record shape, bounds and timing."""
    review_service = DomainReviewService.for_workspace(workspace)
    search = SearchService.for_workspace(workspace)
    search.rebuild()  # without this the index is empty and the search numbers mean nothing
    out: dict[str, Any] = {}

    started = time.perf_counter()
    review = review_service.review(DomainReviewRequest(well_id=well_id))
    out["review_seconds"] = time.perf_counter() - started
    out["review_records"] = len(review.records)
    out["review_truncated"] = review.truncated
    out["review_queries"] = _select_count(
        workspace.database.engine,
        lambda: review_service.review(DomainReviewRequest(well_id=well_id)),
    )

    started = time.perf_counter()
    with workspace.database.read_only() as session:
        timeline = build_timeline(session, well_id=well_id)
    out["timeline_seconds"] = time.perf_counter() - started
    out["timeline_entries"] = len(timeline)
    out["timeline_queries"] = _select_count(
        workspace.database.engine,
        lambda: _timeline_read(workspace, well_id),
    )

    started = time.perf_counter()
    response = search.search("zermatt", well_id=well_id, limit=20)
    out["search_seconds"] = time.perf_counter() - started
    out["search_results"] = len(response.results)
    out["search_candidates"] = response.candidates
    out["search_truncated"] = response.truncated
    out["search_candidate_capped"] = response.candidate_capped
    out["search_results_capped"] = response.results_capped
    out["search_total_chunks"] = response.total_chunks
    out["search_fts_used"] = response.fts_used
    # Deliberately no search query count: the search index is its own SQLite file with its own
    # engine, so instrumenting workspace.database.engine would report a misleading zero.  Search
    # scale is certified through its bounded-discovery metadata instead, which is the contract that
    # matters and is already pinned by the truncation tests.
    return out


def _timeline_read(workspace, well_id: str) -> int:
    with workspace.database.read_only() as session:
        return len(build_timeline(session, well_id=well_id))


@pytest.mark.parametrize("rows", [1_000, 10_000])
def test_scale_shape_is_recorded(rows, workspace) -> None:
    """Measured at both scales; the structural assertions below are what those numbers proved."""
    well_id = _build(workspace, rows)
    with workspace.database.read_only() as session:
        stored = session.execute(select(func.count()).select_from(WellOperation)).scalar_one()
    assert stored == rows, "the corpus must actually hold the rows it claims"

    measured = _measure(workspace, well_id)
    print(f"\nSCALE rows={rows} {measured}")

    # --- the N+1 criterion: query count does not grow with row count ----------------
    # 41, not 39: V7.2 added two bounded reads to the review (well-control events and well-scoped
    # HSE incidents), one query each.  The invariant this guards is scale-invariance, and it holds -
    # the count is 41 at 1 000 rows and 41 at 10 000, so neither read is per-row.  Had either been
    # written as a loop over parents the number would have grown with the corpus and failed here.
    assert measured["review_queries"] == 41, (
        f"review issued {measured['review_queries']} SELECTs for {rows} rows; it issued 41 for "
        "1 000 too, so any other number means a per-row read was introduced"
    )
    # 11, not 9, for the same reason: the ``well_control`` and ``hse`` kinds each cost one bounded
    # query.  11 at 1 000 rows and 11 at 10 000, so neither is per-row.
    assert measured["timeline_queries"] == 11, (
        f"timeline issued {measured['timeline_queries']} SELECTs for {rows} rows; 11 at 1 000 too"
    )

    # --- reads return the corpus, and say so when a bound cuts them -------------------
    assert measured["timeline_entries"] == 2 * rows + 2, "timeline carries every dated row"
    if rows == 1_000:
        assert measured["review_records"] == 2_000
        assert measured["review_truncated"] is False
        assert measured["search_results_capped"] is False
    else:
        # _SAFE_LIMIT is 10 000: the review is cut and says so instead of looking complete.
        assert measured["review_records"] == 10_000
        assert measured["review_truncated"] is True, "a cut review must report truncation"
        assert measured["search_results"] == 20
        assert measured["search_candidates"] == 10_000
        assert measured["search_results_capped"] is True
        assert measured["search_truncated"] is True
    assert measured["search_fts_used"] is True, "the FTS path is the one under measurement"
    assert measured["search_total_chunks"] == rows
