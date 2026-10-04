"""V7.2 in the review surface: their own records, the existing lifecycle, nothing weakened.

The repository's domain review *read* projects documents, versions and knowledge items; operational
rows are reviewed through the confirmation lifecycle in ``review/actions.py``.  That is where these
two domains are added, using the machinery the other source-derived rows already use.  No second
review framework is introduced and no bound is relaxed.

Site-only HSE has no well, so it cannot be read through a well-scoped review.  That limitation is
asserted here rather than hidden: the rows remain correct in the database, in provenance and in
search, and the review surface does not gain a well for them.
"""

from __future__ import annotations

from tests.fixtures.fieldops import fetch, ingest_v72, promote_file, well_id_for

from drilling_intelligence.database.models import HseIncident, WellControlEvent
from drilling_intelligence.review import DomainReviewRequest, DomainReviewService
from drilling_intelligence.review.actions import (
    _LIFECYCLES,
    _OPERATIONAL_MODELS,
    ReviewActionRequest,
    ReviewActionService,
)


def _service(workspace):
    return ReviewActionService.for_workspace(workspace)


def test_both_domains_are_registered_as_operational_review_records(workspace) -> None:
    assert "well_control_event" in _OPERATIONAL_MODELS
    assert "hse_incident" in _OPERATIONAL_MODELS
    assert _LIFECYCLES["well_control_event"].name == "confirmation"
    assert _LIFECYCLES["hse_incident"].name == "confirmation"


def test_a_promoted_well_control_row_is_reviewable_as_itself(workspace) -> None:
    ingest_v72(workspace)
    promote_file(workspace, "well_control_log_well-a3.xlsx")
    row = next(r for r in fetch(workspace, WellControlEvent) if r.event_label == "WC-01")

    result = _service(workspace).execute(
        ReviewActionRequest(
            record_type="well_control_event",
            record_id=row.id,
            action="confirm",
            actor="driller",
            expected_status="CANDIDATE",
            reason="verified against the driller's log",
        )
    )
    assert result.status == "CONFIRMED", result.to_dict()
    stored = {r.id: r for r in fetch(workspace, WellControlEvent)}[row.id]
    assert stored.status == "CONFIRMED"


def test_a_promoted_hse_row_is_reviewable_as_itself(workspace) -> None:
    ingest_v72(workspace)
    promote_file(workspace, "hse_register_well-a3.xlsx")
    row = next(r for r in fetch(workspace, HseIncident) if r.incident_reference == "HSE-102")

    result = _service(workspace).execute(
        ReviewActionRequest(
            record_type="hse_incident",
            record_id=row.id,
            action="confirm",
            actor="hse-advisor",
            expected_status="CANDIDATE",
            reason="confirmed against the incident report",
        )
    )
    assert result.status == "CONFIRMED", result.to_dict()
    stored = {r.id: r for r in fetch(workspace, HseIncident)}[row.id]
    assert stored.status == "CONFIRMED"


def test_a_stale_expected_status_is_refused(workspace) -> None:
    """The precondition is real: a row that moved on cannot be confirmed against an old read."""
    ingest_v72(workspace)
    promote_file(workspace, "well_control_log_well-a3.xlsx")
    row = next(r for r in fetch(workspace, WellControlEvent) if r.event_label == "WC-01")
    try:
        _service(workspace).execute(
            ReviewActionRequest(
                record_type="well_control_event",
                record_id=row.id,
                action="confirm",
                actor="driller",
                expected_status="CONFIRMED",
            )
        )
    except Exception as error:  # noqa: BLE001 - the point is that it is refused, not how
        assert "CONFIRMED" in str(error) or "status" in str(error).lower()
    else:  # pragma: no cover
        raise AssertionError("a stale expected_status must be refused")


def test_well_control_rows_are_visible_in_the_well_review(workspace) -> None:
    """The read surface projects the rows, not merely the actions that could be taken on them."""
    ingest_v72(workspace)
    promote_file(workspace, "well_control_log_well-a3.xlsx")
    review = DomainReviewService.for_workspace(workspace).review(
        DomainReviewRequest(well_id=well_id_for(workspace, "A-3"), lifecycle="current")
    )
    wc = [record for record in review.records if record.record_type == "well_control_event"]
    assert len(wc) == 5, [record.record_id for record in wc]
    assert all(record.provenance for record in wc), "a reviewed row must carry its source evidence"


def test_well_scoped_hse_is_visible_and_site_only_hse_is_not(workspace) -> None:
    """Site-only HSE is excluded by the well scope itself, not by a special case."""
    ingest_v72(workspace)
    promote_file(workspace, "hse_register_well-a3.xlsx")
    promote_file(workspace, "hse_site_register.xlsx")
    review = DomainReviewService.for_workspace(workspace).review(
        DomainReviewRequest(well_id=well_id_for(workspace, "A-3"), lifecycle="current")
    )
    hse = [record for record in review.records if record.record_type == "hse_incident"]
    # Three from the A-3 register; the three site rows have well_id NULL and are not this well's.
    assert len(hse) == 3, [record.record_id for record in hse]
    site_ids = {row.id for row in fetch(workspace, HseIncident) if row.well_id is None}
    assert site_ids, "the site register must actually have produced site-scoped rows"
    assert not any(record.record_id in site_ids for record in hse), (
        "a site-only incident was presented as a well-scoped review record"
    )


def test_a_superseded_row_leaves_the_current_review_but_stays_in_history(workspace) -> None:
    from openpyxl import load_workbook
    from tests.fixtures.fieldops import reingest

    ingest_v72(workspace)
    promote_file(workspace, "well_control_log_well-a3.xlsx")

    path = workspace.root / "corpus" / "well_control_log_well-a3.xlsx"
    workbook = load_workbook(path)
    workbook.active["E5"] = 1325
    workbook.save(path)
    reingest(workspace)
    promote_file(workspace, "well_control_log_well-a3.xlsx")

    service = DomainReviewService.for_workspace(workspace)
    well = well_id_for(workspace, "A-3")
    current = service.review(DomainReviewRequest(well_id=well, lifecycle="current"))
    history = service.review(DomainReviewRequest(well_id=well, lifecycle="history"))

    def wc_ids(review):
        return {
            record.record_id
            for record in review.records
            if record.record_type == "well_control_event"
        }

    assert len(wc_ids(current)) == 5, "the current review shows the current rows"
    assert len(wc_ids(history)) > len(wc_ids(current)), (
        "history must still expose the superseded reading"
    )


def test_the_review_read_does_not_mutate_the_database(workspace) -> None:
    ingest_v72(workspace)
    promote_file(workspace, "well_control_log_well-a3.xlsx")
    before = sorted(
        (row.id, row.status, row.is_current) for row in fetch(workspace, WellControlEvent)
    )
    service = DomainReviewService.for_workspace(workspace)
    well = well_id_for(workspace, "A-3")
    service.review(DomainReviewRequest(well_id=well, lifecycle="current"))
    service.review(DomainReviewRequest(well_id=well, lifecycle="history"))
    after = sorted(
        (row.id, row.status, row.is_current) for row in fetch(workspace, WellControlEvent)
    )
    assert before == after, "a review is a read"


def test_current_and_history_are_both_readable_and_differ_only_by_lifecycle(workspace) -> None:
    from openpyxl import load_workbook
    from tests.fixtures.fieldops import reingest

    ingest_v72(workspace)
    promote_file(workspace, "well_control_log_well-a3.xlsx")

    path = workspace.root / "corpus" / "well_control_log_well-a3.xlsx"
    workbook = load_workbook(path)
    workbook.active["E5"] = 1325
    workbook.save(path)
    reingest(workspace)
    promote_file(workspace, "well_control_log_well-a3.xlsx")

    service = DomainReviewService.for_workspace(workspace)
    well = well_id_for(workspace, "A-3")
    current = service.review(DomainReviewRequest(well_id=well, lifecycle="current"))
    history = service.review(DomainReviewRequest(well_id=well, lifecycle="history"))
    assert len(history.records) >= len(current.records)

    # The database is the authority on the lifecycle, and it kept the superseded row readable.
    rows = fetch(workspace, WellControlEvent)
    assert any(row.is_current for row in rows if row.event_label == "WC-01")
    assert any(
        not row.is_current and row.sidpp_value == 1200.0
        for row in rows
        if row.event_label == "WC-01"
    )
