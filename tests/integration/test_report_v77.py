"""V7.7 engineering reports against the certified A-3 / B-11 / C-17 world.

The report must match the decision or comparison pack it wraps, write nothing,
and add no SQL while rendering.
"""

from __future__ import annotations

import json

import pytest
from sqlalchemy import event, func, select
from tests.fixtures.fieldops import well_id_for
from tests.integration.test_cli_domain import _capture, call

from drilling_intelligence.database.base import Base
from drilling_intelligence.intelligence.comparison import UNDATED_BEHAVIOR, ComparisonIntelligence
from drilling_intelligence.intelligence.service import IntelligenceService
from drilling_intelligence.reporting import render_html, render_svg
from drilling_intelligence.reporting.layout import layout_exhibit
from drilling_intelligence.reporting.service import build_report

_FORBIDDEN = ("better", "safer", "cheaper", "optimal", "winner", "recommend a", "likely")


def _select_count(engine, fn) -> int:
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


def _counts(session) -> dict[str, int]:
    out: dict[str, int] = {}
    for mapper in Base.registry.mappers:
        name = mapper.local_table.name
        out[name] = int(
            session.execute(select(func.count()).select_from(mapper.local_table)).scalar_one()
        )
    return out


def _matrix_cell(report, metric: str, well_id: str) -> dict:
    for section in report["sections"]:
        for table in section["tables"]:
            if table["table_id"] != "comparison-matrix":
                continue
            for row in table["rows"]:
                if row["metric"] == metric:
                    return row[well_id]
    raise AssertionError(metric)


def _exhibit(report, metric: str) -> dict:
    for item in report["exhibits"]:
        if item["metric"] == metric:
            return item
    raise AssertionError(metric)


def test_single_well_report_copies_the_decision_pack(session, golden):
    from drilling_intelligence.intelligence.decision import DecisionIntelligence

    decision = DecisionIntelligence(session).pack(well_id=golden["a3"])
    report = build_report(session, well_ids=[golden["a3"]])
    payload = report.to_dict()
    assert payload["schema"] == "engineering-report/1"
    assert payload["mode"] == "single_well"
    assert payload["source_packs"][0]["identity"] == decision.identity
    assert payload["source_packs"][0]["schema"] == "decision-pack/1"
    summary = next(section for section in payload["sections"] if section["section_id"] == "summary")
    copied = {row["field"]: row["value"] for row in summary["tables"][0]["rows"]}
    assert copied["npt_hours"] == decision.summary["npt_hours"]
    assert copied["hse_incidents"] == decision.summary["hse_incidents"]
    assert payload["observations"] == list(decision.observations)
    assert payload["freshness"] == dict(decision.freshness)
    hse = next(section for section in payload["sections"] if section["section_id"] == "hse")
    fields = {row["field"]: row["value"] for row in hse["tables"][0]["rows"]}
    assert fields["incidents"] == decision.operations.hse["incidents"]
    assert "site_scoped_incidents" in fields
    assert "site-scoped" in hse["note"]
    depth = _exhibit(payload, "depth.series")
    assert depth["state"] == "UNSUPPORTED"
    timeline = next(
        section for section in payload["sections"] if section["section_id"] == "timeline"
    )
    assert timeline["state"] == "UNSUPPORTED"
    again = build_report(session, well_ids=[golden["a3"]])
    assert again.identity == report.identity


def test_two_and_three_well_reports_match_comparison_cells(session, golden):
    for ids in (
        [golden["a3"], golden["b11"]],
        [golden["a3"], golden["b11"], golden["c17"]],
    ):
        comparison = ComparisonIntelligence(session).compare(well_ids=ids)
        report = build_report(session, well_ids=ids).to_dict()
        assert report["mode"] == "explicit_well_set"
        assert report["source_packs"][0]["identity"] == comparison.identity
        assert [subject["well_id"] for subject in report["subject"]["subjects"]] == list(
            comparison.well_ids
        )
        for section in comparison.sections:
            for row in section.metrics:
                exhibit = _exhibit(report, row.metric)
                assert exhibit["data"]["values"] == {
                    key: dict(value) for key, value in row.values.items()
                }
                assert exhibit["data"]["comparability"] == row.comparability
                cell = _matrix_cell(report, row.metric, ids[0])
                assert cell["value"] == row.values[ids[0]]["value"]
                assert cell["value_state"] == row.values[ids[0]]["value_state"]
        hours = _exhibit(report, "npt.hours")
        points = hours["series"][0]["points"]
        assert [point["subject_id"] for point in points] == ids
        if golden["c17"] in ids:
            c17 = next(point for point in points if point["subject_id"] == golden["c17"])
            assert c17["raw_value"] is None
            assert c17["plottable"] is False
            if hours["state"] == "RENDERED":
                layout = layout_exhibit(hours)
                assert golden["c17"] not in {bar["subject_id"] for bar in layout["bars"]}


def test_named_and_discovered_offsets_reuse_comparison_selection(session, golden):
    named = build_report(session, anchor=golden["a3"], offsets=[golden["b11"]]).to_dict()
    assert named["mode"] == "named_offsets"
    assert named["subject"]["basis_kind"] == "explicit_wells"
    selections = {row["well_id"]: row["selection"] for row in named["subject"]["subjects"]}
    assert selections[golden["a3"]] == "anchor"
    assert selections[golden["b11"]] == "named_offset"
    discovered = build_report(session, anchor=golden["a3"]).to_dict()
    assert discovered["mode"] == "discovered_offsets"
    assert discovered["subject"]["basis_kind"] == "offset_candidates"
    rows = next(
        table
        for section in discovered["sections"]
        if section["section_id"] == "scope"
        for table in section["tables"]
        if table["table_id"] == "discovered-offsets"
    )
    assert rows["rows"][0]["name"] == "B-11"
    assert "shared_problem_types" in rows["columns"]
    assert "npt_hours" in rows["columns"]
    assert "best" not in json.dumps(rows)


def test_currency_risk_hse_and_window_semantics_survive(session, golden):
    report = build_report(session, well_ids=[golden["a3"], golden["b11"]]).to_dict()
    comparison = ComparisonIntelligence(session).compare(well_ids=[golden["a3"], golden["b11"]])
    for section in comparison.sections:
        for row in section.metrics:
            exhibit = _exhibit(report, row.metric)
            if row.comparability != "COMPARABLE":
                assert layout_exhibit(exhibit)["bars"] == []
    usd = _exhibit(report, "cost.planned.USD")
    nok = _exhibit(report, "cost.planned.NOK")
    assert usd["exhibit_id"] != nok["exhibit_id"]
    assert usd["data"]["values"] == {
        key: dict(value)
        for section in comparison.sections
        for row in section.metrics
        if row.metric == "cost.planned.USD"
        for key, value in row.values.items()
    }
    rendered = [item for item in report["exhibits"] if item["state"] == "RENDERED"]
    for item in rendered:
        units = {
            point.get("unit")
            for series in item["series"]
            for point in series["points"]
            if point.get("plottable")
        }
        units.discard(None)
        assert len(units) <= 1, item["metric"]
    hse = _exhibit(report, "hse.incidents")
    assert "site-scoped" in hse["caption"]
    comparison = ComparisonIntelligence(session).compare(
        well_ids=[golden["a3"], golden["b11"]], since="2099-01-01", until="2099-01-02"
    )
    windowed = build_report(
        session, well_ids=[golden["a3"], golden["b11"]], since="2099-01-01", until="2099-01-02"
    ).to_dict()
    assert windowed["subject"]["window"]["applied"] is True
    assert UNDATED_BEHAVIOR in windowed["subject"]["window"]["undated_behavior"]
    assert _exhibit(windowed, "npt.hours")["data"]["values"] == {
        key: dict(value)
        for section in comparison.sections
        if section.section == "operations"
        for row in section.metrics
        if row.metric == "npt.hours"
        for key, value in row.values.items()
    }
    single = build_report(session, well_ids=[golden["a3"]]).to_dict()
    assert "site_scoped_incidents" in json.dumps(
        next(section for section in single["sections"] if section["section_id"] == "hse")
    )
    text = " ".join(item.get("caption", "") + item.get("reason", "") for item in report["exhibits"])
    for word in _FORBIDDEN:
        assert word not in text.lower()


def test_open_conflict_is_preserved_and_rendering_issues_no_sql(session, golden):
    before = _counts(session)
    report = build_report(session, well_ids=[golden["a3"], golden["b11"]])
    payload = report.to_dict()
    html = render_html(payload)
    for exhibit in payload["exhibits"]:
        render_svg(exhibit)
        layout_exhibit(exhibit)
    assert _counts(session) == before
    assert "unresolved_conflict" in payload["limitations"] or any(
        "conflict" in str(row.get("metric"))
        for section in payload["sections"]
        for table in section["tables"]
        for row in table["rows"]
    )
    conflicts = next(
        section for section in payload["sections"] if section["section_id"] == "conflicts"
    )
    assert conflicts["tables"][0]["rows"]
    assert "winner" not in conflicts["note"]
    assert payload["identity"] in html
    assert payload["schema"] in html
    assert "<script" not in html.lower()


def test_rendering_does_not_query_and_report_matches_compare_query_count(workspace, corpus):
    a3 = well_id_for(corpus, "A-3")
    b11 = well_id_for(corpus, "B-11")
    engine = corpus.database.engine
    with corpus.database.read_only() as session:
        compared = _select_count(
            engine, lambda: ComparisonIntelligence(session).compare(well_ids=[a3, b11])
        )
        reported = {"n": 0}

        def before(_conn, _cursor, statement, *_args, **_kwargs) -> None:
            if str(statement).lstrip().upper().startswith("SELECT"):
                reported["n"] += 1

        event.listen(engine, "before_cursor_execute", before)
        try:
            pack = build_report(session, well_ids=[a3, b11])
            composed = reported["n"]
            render_html(pack)
            for exhibit in pack.exhibits:
                render_svg(exhibit.payload())
            rendered = reported["n"]
        finally:
            event.remove(engine, "before_cursor_execute", before)
    assert composed == compared, (composed, compared)
    assert rendered == composed
    assert composed <= 6 + 42 * 2


def test_report_scale_matches_comparison_at_two_five_and_ten(workspace, corpus):
    from tests.integration.test_comparison_v76 import _bare_wells

    with corpus.database.unit_of_work() as session:
        extra = _bare_wells(corpus, session, count=10)
    a3 = well_id_for(corpus, "A-3")
    b11 = well_id_for(corpus, "B-11")
    counts = {}
    for size, ids in (
        (2, [a3, b11]),
        (5, [a3, b11, *extra[:3]]),
        (10, [a3, b11, *extra[:8]]),
    ):
        with corpus.database.read_only() as session:
            counts[size] = _select_count(
                corpus.database.engine, lambda ids=ids: build_report(session, well_ids=ids)
            )
        print(f"\nREPORT_SCALE subjects={size} queries={counts[size]}")
    assert counts[2] <= 6 + 42 * 2, counts
    assert counts[5] <= 6 + 42 * 5, counts
    assert counts[10] <= 6 + 42 * 10, counts
    per_extra = (counts[10] - counts[2]) / 8
    assert per_extra <= 40, counts


@pytest.mark.parametrize("candidates", [5, 20, 40])
def test_offset_report_scale_is_bounded(workspace, candidates):
    from tests.integration.test_comparison_v76 import _sharing_world

    with workspace.database.unit_of_work() as session:
        anchor = _sharing_world(workspace, session, count=candidates)
    with workspace.database.read_only() as session:
        measured = _select_count(
            workspace.database.engine,
            lambda: build_report(session, anchor=anchor, offset_limit=candidates, detail=0),
        )
        pack = build_report(session, anchor=anchor, offset_limit=candidates, detail=0)
    print(f"\nREPORT_OFFSET_SCALE candidates={candidates} queries={measured}")
    envelope = 20 + 40 * (candidates + 1)
    assert measured <= envelope, (measured, envelope)
    assert pack.subject["offset_returned"] == candidates
    assert pack.subject["profiles_truncated"] is (candidates > 16)
    if candidates > 16:
        assert "truncated_detail" in pack.limitations


def test_cli_report_json_matches_the_service_and_html_is_path_independent(
    workspace, corpus, tmp_path
):
    a3 = well_id_for(corpus, "A-3")
    b11 = well_id_for(corpus, "B-11")
    left = tmp_path / "left.html"
    right = tmp_path / "right.html"
    payload = call(corpus, "fields", "report", "--well", "A-3", "--output", str(left))
    with corpus.database.read_only() as session:
        pack = IntelligenceService.for_workspace(corpus).report(well_ids=[a3], session=session)
    assert payload == pack.to_dict()
    assert left.read_text(encoding="utf-8") == render_html(payload)
    assert str(left) not in left.read_text(encoding="utf-8")
    again = call(corpus, "fields", "report", "--well", "A-3", "--output", str(right))
    assert again["identity"] == payload["identity"]
    assert right.read_bytes() == left.read_bytes()
    compared = call(corpus, "fields", "report", "--well", "A-3", "--well", "B-11")
    assert compared["mode"] == "explicit_well_set"
    named = call(corpus, "fields", "report", "--anchor", "A-3", "--offsets", "B-11")
    assert named["mode"] == "named_offsets"
    discovered = call(corpus, "fields", "report", "--anchor", "A-3")
    assert discovered["mode"] == "discovered_offsets"
    with corpus.database.read_only() as session:
        service_named = IntelligenceService.for_workspace(corpus).report(
            anchor=a3, offsets=[b11], session=session
        )
    assert named["identity"] == service_named.identity


def test_cli_report_rejects_bad_requests_without_writing(workspace, corpus, tmp_path):
    missing = tmp_path / "missing" / "report.html"
    code, out, err = _capture(corpus, "fields", "report", "--well", "A-3", "--output", str(missing))
    assert code != 0
    assert not missing.exists()
    assert "directory does not exist" in (out + err)
    code, out, err = _capture(corpus, "fields", "report", "--well", "A-3", "--well", "A-3")
    assert code != 0
    assert "more than once" in (out + err)
    code, out, err = _capture(corpus, "fields", "report", "--well", "A-3", "--anchor", "B-11")
    assert code != 0
    assert "contradictory" in (out + err)
    code, out, err = _capture(corpus, "fields", "report", "--well", "NO-SUCH")
    assert code != 0
    assert "no well matches" in (out + err)
    code, out, err = _capture(corpus, "fields", "report", "--well", "A-3", "--since", "not-a-date")
    assert code != 0
    code, out, err = _capture(corpus, "fields", "report", "--offsets", "B-11")
    assert code != 0
    assert "requires --anchor" in (out + err)


def test_cli_report_on_an_empty_workspace_does_not_invent_a_well(workspace):
    code, out, err = _capture(workspace, "fields", "report", "--well", "A-3")
    assert code != 0
    assert "no well matches" in (out + err)


def test_database_fingerprint_is_unchanged_by_cli_export(workspace, corpus, tmp_path):
    path = tmp_path / "export.html"

    def counts() -> dict[str, int]:
        with corpus.database.read_only() as session:
            return _counts(session)

    before = counts()
    call(corpus, "fields", "report", "--well", "A-3", "--well", "B-11", "--output", str(path))
    assert path.is_file()
    assert counts() == before
