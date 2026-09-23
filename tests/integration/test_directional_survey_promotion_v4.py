"""Directional survey promotion: stations in source order, with no trajectory mathematics anywhere.

The one property this domain exists to protect is that ingestion does not *calculate*.  A survey that
gives MD, inclination and azimuth stores MD, inclination and azimuth; the TVD beside them is there
because the surveying company printed it, and a survey that omits it stores a NULL rather than a
plausible-looking number from a method nobody chose.  Every test here checks a real promoted row.
"""

from __future__ import annotations

from sqlalchemy import select
from tests.fixtures.fieldops import fetch, ingest_v4, promote, promote_file, reingest

from drilling_intelligence.core.enums import KnowledgeRelationType
from drilling_intelligence.database.integrity import check_domain_identities
from drilling_intelligence.database.models import KnowledgeRelation, SurveyRun, SurveyStation

FILE = "directional_survey_well-a3.csv"


def _run(workspace) -> SurveyRun:
    """The current survey run.

    A re-ingested survey is a new document version, so a superseded run stays in the history beside
    it; "the run" means the current one, and a test that means otherwise has to say so.
    """
    runs = [row for row in fetch(workspace, SurveyRun) if row.is_current]
    assert len(runs) == 1, runs
    return runs[0]


def _write(workspace, text: str) -> None:
    (workspace.root / "corpus" / FILE).write_text(text, encoding="utf-8")


def test_the_stations_are_promoted_in_the_sources_own_order(workspace) -> None:
    ingest_v4(workspace)
    result = promote_file(workspace, FILE)
    assert result.outcome == "PROMOTED", result.to_dict()
    assert result.counts["survey_run"]["created"] == 1
    assert result.counts["survey_station"]["created"] == 5

    run = _run(workspace)
    assert run.station_count == 5
    assert run.min_md_value == 9000.0
    assert run.max_md_value == 10000.0
    assert run.md_unit == "ft"
    assert run.station_identity == "NUMBERED"
    assert run.run_label == ""
    assert run.attributes["run_identity"] == "TABLE"
    assert run.origin == "DERIVED"
    assert run.status == "CANDIDATE"

    # ``fetch`` orders by primary key, which is a content hash; the survey's order is ``sequence``.
    stations = sorted(fetch(workspace, SurveyStation), key=lambda row: row.sequence)
    assert [row.station_number_text for row in stations] == ["1", "2", "3", "4", "5"]
    assert [row.sequence for row in stations] == [1, 2, 3, 4, 5]
    assert [row.md_value for row in stations] == [9000.0, 9250.0, 9500.0, 9750.0, 10000.0]
    assert [row.inclination_value for row in stations] == [1.2, 2.4, 4.1, 6.3, 8.2]
    assert [row.azimuth_value for row in stations] == [140.5, 141.8, 143.2, 144.0, 142.4]
    for row in stations:
        assert row.md_unit == "ft"
        assert row.inclination_unit == "deg"
        assert row.azimuth_unit == "deg"
        assert row.quality == "VALID"
        assert row.provenance
        assert row.survey_run_id == run.id
        assert row.well_id == run.well_id


def test_source_supplied_tvd_northing_easting_and_dls_are_preserved_not_recomputed(workspace) -> None:
    ingest_v4(workspace)
    promote_file(workspace, FILE)
    first = sorted(fetch(workspace, SurveyStation), key=lambda row: row.sequence)[0]
    assert first.station_number_text == "1"
    # The fixture's own numbers, exactly as the survey company printed them.
    assert (first.tvd_value, first.tvd_unit) == (8995.4, "ft")
    assert (first.northing_value, first.northing_unit) == (6712345.1, "ft")
    assert (first.easting_value, first.easting_unit) == (432156.2, "ft")
    assert first.dls_value == 0.4
    assert first.toolface_value == 0.0


def test_a_survey_that_omits_the_vertical_columns_stores_nothing_for_them(workspace) -> None:
    """MD/inclination/azimuth only: the platform does not fill in a trajectory."""
    ingest_v4(workspace)
    _write(
        workspace,
        "Station,MD (ft),Inclination (deg),Azimuth (deg)\n"
        "1,9000,1.2,140.5\n"
        "2,9250,2.4,141.8\n",
    )
    reingest(workspace)
    result = promote_file(workspace, FILE)
    assert result.counts["survey_station"]["created"] == 2, result.to_dict()

    for station in fetch(workspace, SurveyStation):
        assert station.tvd_value is None
        assert station.tvd_text == ""
        assert station.northing_value is None
        assert station.easting_value is None
        assert station.dls_value is None
        assert station.toolface_value is None
        # The measured values keep their units; nothing was converted.
        assert station.md_unit == "ft"
        assert station.inclination_unit == "deg"


def test_a_station_whose_units_the_source_never_stated_is_unverified_not_assumed(workspace) -> None:
    ingest_v4(workspace)
    _write(
        workspace,
        "Station,MD,Inclination,Azimuth\n1,9000,1.2,140.5\n2,9250,2.4,141.8\n",
    )
    reingest(workspace)
    result = promote_file(workspace, FILE)
    assert {item["reason"] for item in result.skipped} >= {"MISSING_UNIT"}
    for station in fetch(workspace, SurveyStation):
        assert station.quality == "UNVERIFIED"
        assert station.md_unit == ""
        assert station.md_value == 9000.0 or station.md_value == 9250.0


def test_a_depth_table_is_not_a_survey(workspace) -> None:
    """The contract needs three distinct columns: depth alone is a tally, not a survey."""
    ingest_v4(workspace)
    _write(workspace, "Station,MD (ft),TVD (ft)\n1,9000,8995\n2,9250,9244\n")
    reingest(workspace)
    result = promote_file(workspace, FILE)
    assert result.outcome == "UNSUPPORTED", result.to_dict()
    assert {item["reason"] for item in result.skipped} == {"NO_RECOGNISED_TABLE"}
    assert fetch(workspace, SurveyStation) == []


def test_one_column_recognised_three_times_is_not_a_survey(workspace) -> None:
    ingest_v4(workspace)
    _write(
        workspace,
        "MD (ft),Inclination (deg),Azimuth (deg)\n"
        "9000,1.2,140.5\n",
    )
    reingest(workspace)
    # This one *is* a survey - three real columns - so the guard that matters is the one below.
    assert promote_file(workspace, FILE).counts["survey_station"]["created"] == 1

    _write(
        workspace,
        "Depth (ft),Depth (deg),Depth\n9000,1.2,140.5\n",
    )
    reingest(workspace)
    # Not a survey: the vocabulary would have to be invented to read it as one.
    result = promote_file(workspace, FILE)
    assert result.outcome == "UNSUPPORTED", result.to_dict()


def test_two_source_labelled_sets_are_never_merged_into_one_survey(workspace) -> None:
    ingest_v4(workspace)
    _write(
        workspace,
        "Survey Run,Station,MD (ft),Inclination (deg),Azimuth (deg)\n"
        "GYRO-1,1,9000,1.2,140.5\n"
        "GYRO-1,2,9250,2.4,141.8\n"
        "MWD-2,1,9500,4.1,143.2\n"
        "MWD-2,2,9750,6.3,144.0\n",
    )
    reingest(workspace)
    result = promote_file(workspace, FILE)
    assert result.counts["survey_run"]["created"] == 2, result.to_dict()
    assert result.counts["survey_station"]["created"] == 4, result.to_dict()

    runs = {row.run_label: row for row in fetch(workspace, SurveyRun)}
    assert set(runs) == {"GYRO-1", "MWD-2"}
    assert runs["GYRO-1"].station_count == 2
    assert runs["MWD-2"].station_count == 2
    # Each set carries its own depth range, so "which survey said 8 degrees" stays answerable.
    assert (runs["GYRO-1"].min_md_value, runs["GYRO-1"].max_md_value) == (9000.0, 9250.0)
    assert (runs["MWD-2"].min_md_value, runs["MWD-2"].max_md_value) == (9500.0, 9750.0)
    # Station numbering restarts per set and is scoped by the set's identity, so two stations both
    # numbered "1" are two rows, not one.
    stations = fetch(workspace, SurveyStation)
    assert len(stations) == 4
    assert len({str(row.identity_key) for row in stations}) == 4


def test_repeated_station_numbers_are_reported_and_identity_falls_back_to_position(workspace) -> None:
    ingest_v4(workspace)
    _write(
        workspace,
        "Station,MD (ft),Inclination (deg),Azimuth (deg)\n"
        "1,9000,1.2,140.5\n"
        "1,9250,2.4,141.8\n"
        "2,9500,4.1,143.2\n",
    )
    reingest(workspace)
    result = promote_file(workspace, FILE)
    assert {item["reason"] for item in result.skipped} >= {"AMBIGUOUS_STATIONS"}
    assert _run(workspace).station_identity == "AMBIGUOUS"
    stations = fetch(workspace, SurveyStation)
    # Both rows survive: an ambiguous source is not a licence to drop one of them silently.
    assert len(stations) == 3
    assert len({str(row.identity_key) for row in stations}) == 3


def test_an_unnumbered_survey_is_promoted_and_says_it_was_unnumbered(workspace) -> None:
    ingest_v4(workspace)
    _write(
        workspace,
        "MD (ft),Inclination (deg),Azimuth (deg)\n9000,1.2,140.5\n9250,2.4,141.8\n",
    )
    reingest(workspace)
    promote_file(workspace, FILE)
    assert _run(workspace).station_identity == "UNNUMBERED"
    stations = fetch(workspace, SurveyStation)
    ordered = sorted(stations, key=lambda row: row.sequence)
    assert [row.station_number_text for row in ordered] == ["", ""]
    assert [row.sequence for row in ordered] == [1, 2]


def test_a_row_without_a_numeric_depth_is_not_a_station(workspace) -> None:
    ingest_v4(workspace)
    _write(
        workspace,
        "Station,MD (ft),Inclination (deg),Azimuth (deg)\n"
        "1,9000,1.2,140.5\n"
        ",,,, \n"
        ",Total,,, \n"
        "2,9250,2.4,141.8\n",
    )
    reingest(workspace)
    result = promote_file(workspace, FILE)
    assert result.counts["survey_station"]["created"] == 2, result.to_dict()
    assert [
        row.station_number_text
        for row in sorted(fetch(workspace, SurveyStation), key=lambda row: row.sequence)
    ] == ["1", "2"]


def test_a_survey_naming_another_well_is_refused(workspace) -> None:
    """A survey filed against A-3 that says B-11 inside is a conflict, not a re-filing.

    The document is linked to A-3 by the operator who ingested it; the source's own header block names
    B-11.  The platform reports the disagreement and writes nothing - it does not quietly attach the
    stations to the well the file happened to be filed under, and it does not move the file to B-11.
    """
    from openpyxl import Workbook

    # Written before the scan, so the pipeline links it to A-3 like any other source.
    corpus = workspace.root / "corpus"
    corpus.mkdir(parents=True, exist_ok=True)
    workbook = Workbook()
    summary = workbook.active
    summary.title = "Header"
    summary["A3"], summary["B3"] = "Well", "B-11"
    data = workbook.create_sheet("Survey")
    data.append(["Station", "MD (ft)", "Inclination (deg)", "Azimuth (deg)"])
    data.append([1, 9000, 1.2, 140.5])
    workbook.save(corpus / "directional_survey_well_b11.xlsx")

    ingest_v4(workspace)
    promote(workspace)  # the well's own survey is promoted first, as it would be in practice
    assert len(fetch(workspace, SurveyRun)) == 1
    outcome = promote_file(workspace, "directional_survey_well_b11.xlsx")
    assert outcome.error == "WELL_SCOPE_CONFLICT", outcome.to_dict()
    assert {item["reason"] for item in outcome.skipped} >= {"WELL_SCOPE_CONFLICT"}
    # The well's own survey is untouched, and nothing was written for the conflicting one.
    assert len(fetch(workspace, SurveyRun)) == 1
    with workspace.database.read_only() as session:
        from sqlalchemy import select

        from drilling_intelligence.database.models import Document

        conflicting = session.scalar(
            select(Document).where(Document.filename == "directional_survey_well_b11.xlsx")
        )
    assert conflicting is not None
    stations = fetch(workspace, SurveyStation)
    assert all(
        str(row.document_version_id) != str(conflicting.current_version_id) for row in stations
    ), "no station may be written from a source that names another well"


def test_repromoting_the_same_survey_is_a_no_op(workspace) -> None:
    ingest_v4(workspace)
    first = promote_file(workspace, FILE)
    assert first.counts["survey_station"]["created"] == 5
    second = promote_file(workspace, FILE)
    assert second.outcome == "UNCHANGED", second.to_dict()
    assert second.counts["survey_station"]["unchanged"] == 5
    assert len(fetch(workspace, SurveyStation)) == 5


def test_a_station_the_source_drops_is_swept_and_the_rest_are_untouched(workspace) -> None:
    ingest_v4(workspace)
    promote_file(workspace, FILE)
    _write(
        workspace,
        "Station,MD (ft),TVD (ft),Inclination (deg),Azimuth (deg),"
        "Toolface (deg),DLS (deg/100ft),Northing (ft),Easting (ft),Date\n"
        "1,9000,8995.4,1.2,140.5,0.0,0.4,6712345.1,432156.2,2025-06-12\n"
        "2,9250,9244.1,2.4,141.8,12.0,0.9,6712594.6,432171.4,2025-06-12\n",
    )
    reingest(workspace)
    result = promote_file(workspace, FILE)
    # The shorter survey is a new document version, so its two stations are written under it and the
    # earlier five are stood down rather than deleted: the deeper stations are still readable history.
    assert result.counts["survey_station"]["created"] == 2, result.to_dict()
    current = sorted(
        (row for row in fetch(workspace, SurveyStation) if row.is_current),
        key=lambda row: row.sequence,
    )
    assert [row.station_number_text for row in current] == ["1", "2"]
    assert current[0].survey_run_id == _run(workspace).id
    assert _run(workspace).station_count == 2
    history = [row for row in fetch(workspace, SurveyStation) if not row.is_current]
    assert len(history) == 5
    assert all(row.status == "SUPERSEDED" for row in history)


def test_the_graph_carries_the_survey_and_its_stations(workspace) -> None:
    ingest_v4(workspace)
    promote_file(workspace, FILE)
    run = _run(workspace)
    with workspace.database.read_only() as session:
        relations = list(session.scalars(select(KnowledgeRelation)))
    kinds = {(row.relation, row.source_id, row.target_type) for row in relations}
    assert (KnowledgeRelationType.WELL_HAS_SURVEY.value, str(run.well_id), "survey_run") in kinds
    station_edges = [
        row for row in relations if row.relation == KnowledgeRelationType.SURVEY_HAS_STATION.value
    ]
    assert len(station_edges) == 5
    assert {row.source_id for row in station_edges} == {str(run.id)}


def test_a_promoted_survey_leaves_the_domain_invariants_clean(workspace) -> None:
    ingest_v4(workspace)
    promote_file(workspace, FILE)
    with workspace.database.read_only() as session:
        problems = check_domain_identities(session)
    assert problems == [], [str(problem) for problem in problems]


def test_the_workspace_summary_reports_the_survey_domain(workspace) -> None:
    ingest_v4(workspace)
    promote(workspace)
    from drilling_intelligence.operations.service import OperationalService

    summary = OperationalService.for_workspace(workspace).report()
    assert summary["survey_runs"] == 1
    assert summary["survey"] == {"runs": 1, "stations": 5}
