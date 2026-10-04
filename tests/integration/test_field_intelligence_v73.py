"""V7.3A field intelligence: what the field recorded, without losing scope, units or lifecycle.

Every number asserted here is a count of rows the source produced, read out of SQLite by a GROUP BY
rather than by loading the table into Python.  The boundaries that matter are the ones V7.2 spent a
wave establishing, and the aggregate must not quietly undo them:

* a well scope never picks up a site-only incident;
* a site-only incident keeps an empty well key instead of borrowing one;
* ``with_pit_gain`` means a pit gain was stated, not that the event was a kick;
* ``with_lost_time_wording`` is not an NPT total and is never summed;
* measurements are never added across units;
* a superseded row is history, not a second live count.
"""

from __future__ import annotations

from tests.fixtures.fieldops import fetch, ingest_v72, promote_file, reingest, well_id_for

from drilling_intelligence.core.errors import ValidationError
from drilling_intelligence.database.models import (
    NptRecord,
    ProblemOccurrence,
    Well,
    WellControlEvent,
    WellEvent,
)
from drilling_intelligence.intelligence.field import FieldIntelligence

WC_LOG = "well_control_log_well-a3.xlsx"
WC_NO_UNITS = "well_control_no_units_well-a3.xlsx"
HSE_REGISTER = "hse_register_well-a3.xlsx"
HSE_SITE = "hse_site_register.xlsx"


def _fi(workspace):
    class _Ctx:
        def __enter__(self_inner):
            self_inner.session = workspace.database.read_only().__enter__()
            return FieldIntelligence(self_inner.session)

        def __exit__(self_inner, *exc):
            self_inner.session.close()

    return _Ctx()


def _project_id(workspace) -> str:
    with workspace.database.read_only() as session:
        well = session.get(Well, well_id_for(workspace, "A-3"))
        return str(well.project_id)


def _promote_all(workspace) -> None:
    ingest_v72(workspace)
    for file_name in (WC_LOG, HSE_REGISTER, HSE_SITE):
        promote_file(workspace, file_name)


# --------------------------------------------------------------------- well control


def test_well_control_aggregate_counts_the_rows_the_source_produced(workspace) -> None:
    _promote_all(workspace)
    with _fi(workspace) as fi:
        result = fi.well_control(well_id=well_id_for(workspace, "A-3"))
    assert result["events"] == 5
    assert result["wells"] == 1
    assert result["undated"] == 0
    assert str(result["first_seen_at"]).startswith("2026-03-14")
    assert str(result["last_seen_at"]).startswith("2026-04-18")


def test_well_control_event_type_counts_never_bend_an_unknown_type(workspace) -> None:
    """Two rows state a type the contract does not know; they stay blank rather than becoming kicks."""
    _promote_all(workspace)
    with _fi(workspace) as fi:
        result = fi.well_control(well_id=well_id_for(workspace, "A-3"))
    assert result["by_event_type"]["kick"] == 1
    assert result["by_event_type"]["influx"] == 1
    assert result["by_event_type"]["loss"] == 1
    assert result["by_event_type"][""] == 2, "unstated types stay unstated"


def test_presence_counts_are_facts_about_columns_not_diagnoses(workspace) -> None:
    """``with_pit_gain`` counts rows carrying a pit gain.  It does not claim any of them was a kick."""
    _promote_all(workspace)
    with _fi(workspace) as fi:
        result = fi.well_control(well_id=well_id_for(workspace, "A-3"))
    assert result["with_pit_gain"] == 5
    assert result["with_sidpp"] == 5
    assert result["with_sicp"] == 5
    assert result["with_depth"] == 5
    # Only WC-05 has a cause column with text in it.
    assert result["with_explicit_cause"] == 1
    assert result["with_npt_wording"] == 1
    assert result["by_event_type"]["kick"] == 1, "five pit gains, one stated kick"


def test_a_unitless_reading_is_counted_but_never_normalised(workspace) -> None:
    """The no-units log adds two rows whose measurements stayed text; they are present, not summed."""
    _promote_all(workspace)
    promote_file(workspace, WC_NO_UNITS)
    with _fi(workspace) as fi:
        result = fi.well_control(well_id=well_id_for(workspace, "A-3"))
    assert result["events"] == 7
    # The aggregate exposes presence counts and grouped counts only - there is deliberately no
    # total pressure or volume key that could have added psi to a unitless number.
    assert not any("total" in key for key in result), sorted(result)


def test_a_well_scope_excludes_every_other_well(workspace) -> None:
    _promote_all(workspace)
    with _fi(workspace) as fi:
        result = fi.well_control(well_id=well_id_for(workspace, "A-3"))
    assert set(result["by_well"]) == {well_id_for(workspace, "A-3")}


def test_project_scope_reaches_the_same_rows_as_the_well_scope_here(workspace) -> None:
    _promote_all(workspace)
    project = _project_id(workspace)
    with _fi(workspace) as fi:
        by_well = fi.well_control(well_id=well_id_for(workspace, "A-3"))
        by_project = fi.well_control(project_id=project)
    assert by_project["events"] == by_well["events"] == 5


def test_a_date_window_uses_the_stored_timestamp(workspace) -> None:
    _promote_all(workspace)
    with _fi(workspace) as fi:
        wide = fi.well_control(well_id=well_id_for(workspace, "A-3"))
        early = fi.well_control(
            well_id=well_id_for(workspace, "A-3"), since="2026-04-01", until="2026-04-30"
        )
        before = fi.well_control(well_id=well_id_for(workspace, "A-3"), until="2026-03-31")
        same_day = fi.well_control(
            well_id=well_id_for(workspace, "A-3"), since="2026-03-14", until="2026-03-14"
        )
    assert wide["events"] == 5
    # WC-03 (04-01), WC-04 (04-09) and WC-05 (04-18) all fall inside April.
    assert early["events"] == 3, sorted(early["by_event_type"].items())
    # WC-01 (03-14) and WC-02 (03-22) are the two events on or before the end of March.
    assert before["events"] == 2, sorted(before["by_event_type"].items())
    assert same_day["events"] == 1, "a same-day window is inclusive at both ends"


def test_an_undated_row_is_counted_as_undated_and_not_invented_a_date(workspace) -> None:
    _promote_all(workspace)
    promote_file(workspace, WC_NO_UNITS)
    with _fi(workspace) as fi:
        result = fi.well_control(well_id=well_id_for(workspace, "A-3"))
    assert result["undated"] == 1, "WC-N2's date does not parse, so it stays undated"
    assert result["events"] == 7, "an undated row is still a row"


def test_an_aggregate_needs_a_scope(workspace) -> None:
    _promote_all(workspace)
    with _fi(workspace) as fi:
        try:
            fi.well_control()
        except ValidationError as error:
            assert "scope" in str(error).lower()
        else:  # pragma: no cover - the guard is the point
            raise AssertionError("an unscoped aggregation must be refused")


def test_a_superseded_row_is_not_counted_twice(workspace) -> None:
    from openpyxl import load_workbook

    _promote_all(workspace)
    with _fi(workspace) as fi:
        assert fi.well_control(well_id=well_id_for(workspace, "A-3"))["events"] == 5

    path = workspace.root / "corpus" / WC_LOG
    workbook = load_workbook(path)
    workbook.active["E5"] = 1325
    workbook.save(path)
    reingest(workspace)
    promote_file(workspace, WC_LOG)

    rows = fetch(workspace, WellControlEvent)
    assert len(rows) > 5, "history is preserved in the database"
    with _fi(workspace) as fi:
        result = fi.well_control(well_id=well_id_for(workspace, "A-3"))
    assert result["events"] == 5, "the superseded reading must not join the live count"


# --------------------------------------------------------------------- HSE


def test_hse_well_scope_excludes_site_only_incidents(workspace) -> None:
    """The core scope rule: a camp slip is not filed against a hole it never happened at."""
    _promote_all(workspace)
    with _fi(workspace) as fi:
        result = fi.hse(well_id=well_id_for(workspace, "A-3"))
    assert result["incidents"] == 3
    assert result["well_scoped_incidents"] == 3
    assert result["site_scoped_incidents"] == 0
    assert set(result["by_well"]) == {well_id_for(workspace, "A-3")}


def test_hse_project_scope_keeps_site_rows_visible_and_separate(workspace) -> None:
    """``well_id`` NULL means site-scoped, not "every well" - so the key stays empty."""
    _promote_all(workspace)
    with _fi(workspace) as fi:
        result = fi.hse(project_id=_project_id(workspace))
    assert result["incidents"] == 6
    assert result["well_scoped_incidents"] == 3
    assert result["site_scoped_incidents"] == 3
    assert result["by_well"][""] == 3, "site rows keep an empty well key"
    assert result["by_well"][well_id_for(workspace, "A-3")] == 3


def test_site_locations_are_reported_without_manufacturing_a_well(workspace) -> None:
    _promote_all(workspace)
    with _fi(workspace) as fi:
        result = fi.hse(project_id=_project_id(workspace))
    for location in ("Camp", "Access road", "Base laydown area"):
        assert result["by_location"][location] == 1, location


def test_a_refused_conflict_row_never_reaches_an_aggregate(workspace) -> None:
    """HSE-104 names B-11 inside an A-3 register, so it was never written - and so cannot be counted."""
    _promote_all(workspace)
    with _fi(workspace) as fi:
        well = fi.hse(well_id=well_id_for(workspace, "A-3"))
        project = fi.hse(project_id=_project_id(workspace))
    assert well["incidents"] == 3
    assert project["incidents"] == 6
    assert "B-11 cellar deck" not in project["by_location"]


def test_hse_presence_counts_are_source_facts(workspace) -> None:
    _promote_all(workspace)
    with _fi(workspace) as fi:
        result = fi.hse(well_id=well_id_for(workspace, "A-3"))
    assert result["with_spill_volume"] == 1, "only HSE-102 states a release volume"
    assert result["with_lost_time_wording"] == 1
    assert result["with_root_cause"] == 1
    assert result["with_immediate_cause"] == 2
    assert result["by_incident_type"] == {"first aid": 1, "near miss": 1, "spill": 1}
    assert result["by_severity"] == {"Low": 2, "Medium": 1}


def test_lost_time_wording_is_never_presented_as_an_npt_total(workspace) -> None:
    """ADR-32 holds at the aggregate layer: a count of wordings, never a sum of hours."""
    _promote_all(workspace)
    with _fi(workspace) as fi:
        result = fi.hse(well_id=well_id_for(workspace, "A-3"))
    assert result["with_lost_time_wording"] == 1
    assert not any("hour" in key.lower() for key in result), sorted(result)
    assert fetch(workspace, NptRecord) == []


def test_hse_date_window_and_undated(workspace) -> None:
    _promote_all(workspace)
    with _fi(workspace) as fi:
        wide = fi.hse(project_id=_project_id(workspace))
        march = fi.hse(project_id=_project_id(workspace), until="2026-03-31")
    assert wide["incidents"] == 6
    # HSE-101 (03-20, well A-3) and SITE-01 (03-05, site) - project scope includes both, and the
    # breakdown shows they were not merged: one is a near miss, one an unsafe condition.
    assert march["incidents"] == 2, sorted(march["by_incident_type"].items())
    assert march["well_scoped_incidents"] == 1
    assert march["site_scoped_incidents"] == 1


# --------------------------------------------------------------------- cross-domain


def test_summary_carries_every_domain_without_dropping_a_legacy_key(workspace) -> None:
    _promote_all(workspace)
    with _fi(workspace) as fi:
        summary = fi.summary(project_id=_project_id(workspace))
    for key in (
        "wells",
        "npt_hours",
        "problems",
        "events",
        "lessons",
        "mud_reports",
        "mud_by_property",
    ):
        assert key in summary, f"legacy key {key} disappeared"
    assert summary["well_control_events"] == 5
    assert summary["hse_incidents"] == 6
    assert summary["hse_well_scoped"] == 3
    assert summary["hse_site_scoped"] == 3
    assert summary["well_control"]["events"] == 5
    assert summary["hse"]["incidents"] == 6


def test_well_control_is_not_counted_as_a_generic_well_event(workspace) -> None:
    """A kick stays in its own section; ``events`` remains the WellEvent aggregation it always was."""
    _promote_all(workspace)
    with _fi(workspace) as fi:
        summary = fi.summary(project_id=_project_id(workspace))
    assert summary["well_control_events"] == 5
    assert fetch(workspace, WellEvent) == [], "no WellEvent stand-in was ever written"
    assert summary["events"] == 0, "five kicks must not appear in the generic event count"


def test_hse_is_neither_a_well_event_nor_npt(workspace) -> None:
    _promote_all(workspace)
    with _fi(workspace) as fi:
        summary = fi.summary(project_id=_project_id(workspace))
    assert summary["hse_incidents"] == 6
    assert fetch(workspace, WellEvent) == []
    assert fetch(workspace, NptRecord) == []
    assert fetch(workspace, ProblemOccurrence) == []
    assert summary["npt_rows"] == 0, "six incidents are not NPT rows"


def test_counts_reconcile_across_surfaces(workspace) -> None:
    """DB == review == current timeline == search == field intelligence, for one real fixture."""
    from drilling_intelligence.intelligence.timeline import build_timeline
    from drilling_intelligence.review import DomainReviewRequest, DomainReviewService
    from drilling_intelligence.search.service import SearchService
    from drilling_intelligence.search.structured import structured_records

    _promote_all(workspace)
    well = well_id_for(workspace, "A-3")

    db_wc = len(fetch(workspace, WellControlEvent))
    with _fi(workspace) as fi:
        intel_wc = fi.well_control(well_id=well)["events"]
    review = DomainReviewService.for_workspace(workspace).review(
        DomainReviewRequest(well_id=well, lifecycle="current")
    )
    review_wc = len(
        [record for record in review.records if record.record_type == "well_control_event"]
    )
    with workspace.database.read_only() as session:
        timeline_wc = len(
            [
                entry
                for entry in build_timeline(session, well_id=well)
                if entry.kind == "well_control"
            ]
        )
    service = SearchService.for_workspace(workspace)
    service.rebuild()
    with workspace.database.read_only() as session:
        search_wc = len(
            [
                record
                for record in structured_records(session)
                if record.record_id.startswith("structured:well_control_event:")
            ]
        )

    assert db_wc == intel_wc == review_wc == timeline_wc == search_wc == 5, (
        db_wc,
        intel_wc,
        review_wc,
        timeline_wc,
        search_wc,
    )


def test_offset_profiles_are_descriptive_and_not_part_of_the_match(workspace) -> None:
    _promote_all(workspace)
    with _fi(workspace) as fi:
        candidates = fi.offset_candidates(well_id=well_id_for(workspace, "A-3"), limit=5)
    assert isinstance(candidates, list)
    for candidate in candidates:
        # The profile is present but the match keys are unchanged: shared problem types and holes.
        assert "shared_problem_types" in candidate
        assert "shared_hole_sizes" in candidate
        profile = candidate.get("profile", {})
        assert not any(key.startswith("shared_") for key in profile), (
            "a profile must not smuggle itself into the equivalence key"
        )


def test_aggregation_is_grouped_sql_not_a_python_scan(workspace) -> None:
    """Query count must not grow with row count: the aggregate is a constant number of GROUP BYs."""
    from sqlalchemy import event

    _promote_all(workspace)
    counts: list[int] = []

    with workspace.database.read_only() as session:
        engine = session.get_bind()

        def _count(_connection, _cursor, _statement, _parameters, _context, _executemany):
            counts.append(1)

        event.listen(engine, "before_cursor_execute", _count)
        try:
            intelligence = FieldIntelligence(session)
            counts.clear()
            intelligence.well_control(well_id=well_id_for(workspace, "A-3"))
            well_control_queries = len(counts)
            counts.clear()
            intelligence.hse(well_id=well_id_for(workspace, "A-3"))
            hse_queries = len(counts)
        finally:
            event.remove(engine, "before_cursor_execute", _count)

    # The constants, named: one table-existence probe, one combined aggregate (totals + presence
    # counts + min/max in a single SELECT), one GROUP BY per breakdown, one undated count.  Nothing
    # here is per-row, so the same numbers hold at ten thousand rows - which is the invariant.
    assert well_control_queries == 7, well_control_queries
    assert hse_queries == 8, hse_queries
