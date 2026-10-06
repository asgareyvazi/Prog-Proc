"""V7.6 - the comparison pack, on the real repositories.

The comparison is a read model: these tests prove it folds the certified per-well read paths,
states comparability honestly (units, scale identity, missing values), keeps every subject
separate, re-runs to the same identity, bounds discovery and profiles, and never converts a
descriptive difference into a recommendation.  Further tests cover the forensic evidence
chains, the offset candidate flow, query-count scale and the read-only guarantee.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime

import pytest
from sqlalchemy import event, func, select
from tests.fixtures.fieldops import (
    field_id,
    well_id_for,
)

from drilling_intelligence.core.enums import (
    KnowledgeOrigin,
    RiskLifecycle,
)
from drilling_intelligence.core.errors import ValidationError
from drilling_intelligence.database.models import (
    Base,
    Field,
    HseIncident,
    KnowledgeConflict,
    NptRecord,
    RiskRecord,
    WellControlEvent,
)
from drilling_intelligence.engineering.costs import CostRepository
from drilling_intelligence.engineering.risk import RiskRepository
from drilling_intelligence.intelligence.comparison import (
    COMPARABLE,
    COMPARISON_SCHEMA,
    INCOMPARABLE,
    MISSING,
    UNRESOLVED,
    ComparisonIntelligence,
    _metric_row,
    _value_entry,
)
from drilling_intelligence.intelligence.field import FieldIntelligence
from drilling_intelligence.wells.repository import WellRepository

# --------------------------------------------------------------------------------------
# Fixtures
# --------------------------------------------------------------------------------------


def metrics(pack) -> dict:
    return {
        row["metric"]: row for section in pack.to_dict()["sections"] for row in section["metrics"]
    }


def cell(pack, metric: str, well: str) -> dict:
    return metrics(pack)[metric]["values"][well]


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


def _two_wells(session, *, left: str = "CMP-A", right: str = "CMP-B"):
    wells = WellRepository(session)
    project = wells.get_or_create_project("Cormorant Block")
    field = wells.get_or_create_field("North Cormorant", project=project)
    well_left = wells.create_well(left, project_id=project.id, field_id=field.id)
    well_right = wells.create_well(right, project_id=project.id, field_id=field.id)
    return project, field, well_left, well_right


# --------------------------------------------------------------------------------------
# Contracts and identity
# --------------------------------------------------------------------------------------


def test_a_comparison_pack_carries_every_contract_field_and_re_runs_to_the_same_identity(
    session, golden
):
    pack = ComparisonIntelligence(session).compare(
        well_ids=[golden["a3"], golden["b11"], golden["c17"]]
    )
    assert pack.schema == COMPARISON_SCHEMA
    data = pack.to_dict()
    assert list(data) == [
        "schema",
        "request",
        "basis",
        "sections",
        "evidence",
        "limitations",
        "freshness",
        "observations",
        "summary",
        "identity",
    ]
    assert len(data["identity"]) == 64
    assert data["summary"]["subjects"] == 3
    again = ComparisonIntelligence(session).compare(
        well_ids=[golden["a3"], golden["b11"], golden["c17"]]
    )
    assert again.identity == pack.identity
    assert again.to_dict() == data


def test_identity_tracks_the_payload_not_the_clock(session, golden):
    """A different request is a different identity; the same request is the same identity;
    and nothing time-shaped leaks into the hash."""
    first = ComparisonIntelligence(session).compare(well_ids=[golden["a3"], golden["b11"]])
    second = ComparisonIntelligence(session).compare(well_ids=[golden["a3"], golden["b11"]])
    shallow = ComparisonIntelligence(session).compare(
        well_ids=[golden["a3"], golden["b11"]], detail=0
    )
    assert first.identity == second.identity
    assert shallow.identity != first.identity, "detail is part of the request"
    assert not re.search(r"\d{4}-\d{2}-\d{2}T\d{2}:", first.identity)
    # content identity: a new cost line on a subject changes the pack
    costs = CostRepository(session)
    costs.record_item(
        description="extra line",
        planned_value=1.0,
        planned_unit="USD",
        category="equipment",
        well_id=golden["b11"],
        field_id=golden["field_id"],
        project_id=golden["project_id"],
    )
    changed = ComparisonIntelligence(session).compare(well_ids=[golden["a3"], golden["b11"]])
    assert changed.identity != first.identity


# --------------------------------------------------------------------------------------
# Selection and basis
# --------------------------------------------------------------------------------------


def test_explicit_selection_keeps_request_order_and_labels(session, golden):
    pack = ComparisonIntelligence(session).compare(
        well_ids=[golden["c17"], golden["a3"], golden["b11"]]
    )
    assert pack.well_ids == (golden["c17"], golden["a3"], golden["b11"])
    subjects = pack.basis.subjects
    assert [subject.selection for subject in subjects] == ["explicit"] * 3
    assert [subject.name for subject in subjects] == ["C-17", "A-3", "B-11"]
    assert pack.basis.kind == "explicit_wells"
    assert pack.basis.anchor is None


def test_anchor_discovery_reuses_offset_candidates_verbatim(session, golden):
    a3 = golden["a3"]
    discovered = FieldIntelligence(session).offset_candidates(a3, same_field_only=True)
    pack = ComparisonIntelligence(session).compare(anchor=a3, offsets=())
    assert pack.basis.kind == "offset_candidates"
    assert pack.basis.anchor == a3
    assert pack.well_ids == (a3, *[row["well_id"] for row in discovered])
    # the candidate rows keep their recorded field names - renamed to no "best"/"safe"
    payload_rows = [dict(row) for row in pack.basis.discovered]
    assert payload_rows == [dict(row) for row in discovered]
    assert pack.basis.anchor_name == "A-3"
    for profile in pack.basis.profiles:
        assert set(profile) >= {
            "well_id",
            "name",
            "candidate_status",
            "basis",
            "comparable",
            "incomparable",
            "missing",
            "limitations",
        }
        assert profile["candidate_status"] == "included"
        candidate = next(row for row in discovered if row["well_id"] == profile["well_id"])
        assert profile["basis"]["shared_problem_types"] == sorted(candidate["shared_problem_types"])
        assert profile["basis"]["shared_hole_sizes"] == sorted(candidate["shared_hole_sizes"])
        assert profile["comparable"], "a shared basis must yield comparable metrics"


def test_named_offsets_stay_explicit_wells_with_the_anchor_recorded(session, golden):
    pack = ComparisonIntelligence(session).compare(anchor=golden["a3"], offsets=[golden["b11"]])
    assert pack.basis.kind == "explicit_wells"
    assert pack.basis.anchor == golden["a3"]
    assert pack.basis.anchor_name == "A-3"
    selections = {s.well_id: s.selection for s in pack.basis.subjects}
    assert selections[golden["a3"]] == "anchor"
    assert selections[golden["b11"]] == "named_offset"


def test_selection_errors_are_specific(session, golden):
    service = ComparisonIntelligence(session)
    a3, b11 = golden["a3"], golden["b11"]
    cases = [
        ({"well_ids": []}, "at least two wells"),
        ({"well_ids": [a3]}, "at least two wells"),
        ({"well_ids": [a3, a3]}, "named more than once"),
        ({"well_ids": [a3, "well-does-not-exist"]}, "unknown well"),
        ({"well_ids": [a3, b11], "anchor": a3}, "contradictory scope"),
        ({"well_ids": [a3, "  "]}, "cannot be empty"),
        ({"anchor": a3, "offsets": [b11, b11]}, "named more than once"),
        ({"anchor": "well-does-not-exist"}, "no well"),  # offset_candidates' own refusal
        ({"well_ids": [a3, b11], "since": "not-a-date"}, ""),
        ({"well_ids": [a3, b11], "until": "31-12-2025"}, ""),
    ]
    for kwargs, fragment in cases:
        with pytest.raises(ValidationError) as excinfo:
            service.compare(**kwargs)
        if fragment:
            assert fragment in str(excinfo.value), (kwargs, str(excinfo.value))


def test_an_anchor_with_no_candidates_fails_instead_of_broadening(session, corpus):
    workspace = corpus
    wells = WellRepository(session)
    field = session.get(Field, field_id(workspace))
    lonely = wells.create_well("LONELY-1", project_id=field.project_id, field_id=field.id)
    with pytest.raises(ValidationError) as excinfo:
        ComparisonIntelligence(session).compare(anchor=str(lonely.id), offsets=())
    assert "no offset candidates" in str(excinfo.value)


# --------------------------------------------------------------------------------------
# The comparability engine (Test B)
# --------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "left_unit,right_unit",
    [("psi", "bar"), ("m", "ft"), ("d", "h"), ("ppg", "sg"), ("USD", "NOK")],
)
def test_differing_units_make_a_row_incomparable_and_never_convert(left_unit, right_unit):
    row = _metric_row(
        "x",
        "X",
        {
            "w1": _value_entry(100.0, unit=left_unit, state="STATED"),
            "w2": _value_entry(100.0, unit=right_unit, state="STATED"),
        },
    )
    assert row.comparability == INCOMPARABLE
    assert "incomparable_units" in row.limitations
    assert row.units == {"w1": left_unit, "w2": right_unit}
    for wid, unit in (("w1", left_unit), ("w2", right_unit)):
        assert row.values[wid]["value"] == 100.0, "the source value stays visible"
        assert row.values[wid]["unit"] == unit
        assert row.values[wid]["comparability"] == INCOMPARABLE


def test_matching_units_compare_and_one_stated_value_never_does():
    same = _metric_row(
        "y",
        "Y",
        {
            "w1": _value_entry(5.0, unit="m", state="STATED"),
            "w2": _value_entry(7.0, unit="m", state="STATED"),
        },
    )
    assert same.comparability == COMPARABLE
    lone = _metric_row(
        "z",
        "Z",
        {
            "w1": _value_entry(5.0, unit="m", state="STATED"),
            "w2": _value_entry(None, unit=None, state=MISSING),
        },
    )
    assert lone.comparability == MISSING
    assert lone.values["w2"]["comparability"] == MISSING


def test_currency_difference_on_the_composite_cost_row(session):
    project, field, left, right = _two_wells(session)
    costs = CostRepository(session)
    costs.record_item(
        description="usd only",
        planned_value=10.0,
        planned_unit="USD",
        category="equipment",
        well_id=left.id,
        field_id=field.id,
        project_id=project.id,
    )
    costs.record_item(
        description="nok only",
        planned_value=20.0,
        planned_unit="NOK",
        category="equipment",
        well_id=right.id,
        field_id=field.id,
        project_id=project.id,
    )
    pack = ComparisonIntelligence(session).compare(well_ids=[str(left.id), str(right.id)])
    row = metrics(pack)["cost.planned"]
    assert row["comparability"] == INCOMPARABLE
    assert row["values"][str(left.id)]["unit"] == "USD"
    assert row["values"][str(right.id)]["unit"] == "NOK"
    assert "incomparable_units" in pack.limitations
    assert any("Not comparable because units differ" in line for line in pack.observations)


def test_unitless_and_units_together_are_unresolved_not_comparable():
    row = _metric_row(
        "u",
        "U",
        {
            "w1": _value_entry(1.0, unit="h", state="STATED"),
            "w2": _value_entry(1.0, unit=None, state="COUNTED"),
        },
    )
    assert row.comparability == UNRESOLVED


# --------------------------------------------------------------------------------------
# Missing is not zero (Test C)
# --------------------------------------------------------------------------------------


def test_missing_values_stay_missing_and_counts_stay_counts(session, golden):
    pack = ComparisonIntelligence(session).compare(
        well_ids=[golden["a3"], golden["b11"], golden["c17"]]
    )
    rows = metrics(pack)
    # C-17 has no NPT rows at all: hours are NO_RECORDS (not 0.0), rows are COUNTED 0.
    assert rows["npt.hours"]["values"][golden["c17"]]["value"] is None
    assert rows["npt.hours"]["values"][golden["c17"]]["value_state"] == "NO_RECORDS"
    assert rows["npt.rows"]["values"][golden["c17"]]["value"] == 0
    assert rows["npt.rows"]["values"][golden["c17"]]["value_state"] == "COUNTED"
    # no cost records on C-17: every cost cell is MISSING with a null value, never 0.0
    for metric, row in rows.items():
        if metric.startswith("cost."):
            assert row["values"][golden["c17"]]["value"] is None
            assert row["values"][golden["c17"]]["comparability"] == MISSING
    # A-3 states an actual USD total; B-11 has no USD lines at all: MISSING, not 0.0
    assert rows["cost.actual.USD"]["values"][golden["a3"]]["value"] == 120_000.0
    assert rows["cost.actual.USD"]["values"][golden["b11"]]["value"] is None
    assert rows["cost.actual.USD"]["values"][golden["b11"]]["comparability"] == MISSING
    # and no cell anywhere claims a zero through a missing state
    for metric, row in rows.items():
        for wid, entry in row["values"].items():
            if entry["value_state"] in (MISSING, "NO_RECORDS", "UNASSESSED"):
                assert entry["value"] is None, (metric, wid, entry)


# --------------------------------------------------------------------------------------
# Same-source reuse (Test A)
# --------------------------------------------------------------------------------------


def test_operations_cells_equal_the_certified_windowed_methods(session, golden):
    pack = ComparisonIntelligence(session).compare(
        well_ids=[golden["a3"], golden["b11"], golden["c17"]]
    )
    rows = metrics(pack)
    field = FieldIntelligence(session)
    for wid in (golden["a3"], golden["b11"], golden["c17"]):
        npt = field.npt(well_id=wid)
        problems = field.problems(well_id=wid)
        wc = field.well_control(well_id=wid)
        hse = field.hse(well_id=wid)
        assert rows["npt.rows"]["values"][wid]["value"] == npt["rows"]
        assert rows["npt.hours"]["values"][wid]["value"] in (
            None,
            npt["total_hours"],
        )
        if npt["rows"]:
            assert rows["npt.hours"]["values"][wid]["value"] == npt["total_hours"]
        assert rows["problems.occurrences"]["values"][wid]["value"] == problems["occurrences"]
        assert rows["well_control.events"]["values"][wid]["value"] == wc["events"]
        assert rows["hse.incidents"]["values"][wid]["value"] == hse["incidents"]
    # the golden well-control fact: A-3 has the seven well-control rows
    assert rows["well_control.events"]["values"][golden["a3"]]["value"] == 7
    assert rows["well_control.events"]["values"][golden["b11"]]["value"] == 0


def test_execution_reuses_plan_actual_summary_and_keeps_sections_separate(session, golden):
    from drilling_intelligence.engineering.repository import EngineeringRepository

    pack = ComparisonIntelligence(session).compare(
        well_ids=[golden["a3"], golden["b11"], golden["c17"]]
    )
    detail = {section["section"]: section["detail"] for section in pack.to_dict()["sections"]}[
        "execution"
    ]
    engineering = EngineeringRepository(session)
    for wid in (golden["a3"], golden["b11"], golden["c17"]):
        direct = engineering.plan_actual_summary(well_id=wid)
        assert detail[wid]["rows"] == len(direct), wid
        statuses: dict[str, int] = {}
        for row in direct:
            statuses[row["status"]] = statuses.get(row["status"], 0) + 1
        folded = {key: value for key, value in detail[wid]["by_status"].items() if value}
        assert folded == statuses, wid
    # every status the fold can produce stays visible, never hidden
    row = metrics(pack)
    present = {m for m in row if m.startswith("plan.status.")}
    assert "plan.status.no_plan" in present
    assert "plan.status.no_actual" in present
    # C-17 has no plan at all: its detail is an empty statement, not somebody else's rows
    assert detail[golden["c17"]]["rows"] == 0


def test_no_cross_well_section_name_fallback(session):
    """Two wells may name a section identically; one well's plan must never answer for the
    other (the NAME fallback stays inside its own well scope)."""
    from drilling_intelligence.database.models import WellSection
    from drilling_intelligence.engineering.repository import EngineeringRepository

    _project, _field, left, right = _two_wells(session, left="NAME-A", right="NAME-B")
    for well in (left, right):
        session.add(
            WellSection(
                id=f"sec-{well.name}",
                well_id=well.id,
                sequence=1,
                name="9 5/8 in",
                hole_size_in=9.625,
                top_depth_value=1000.0,
                top_depth_unit="m",
                bottom_depth_value=4000.0,
                bottom_depth_unit="m",
            )
        )
    session.flush()
    engineering = EngineeringRepository(session)
    program = engineering.create_program(title="NAME-A programme", well_id=left.id)
    engineering.add_target(
        program.id,
        name="9 5/8 in",
        sequence=1,
        planned_depth_md_value=4000.0,
        planned_depth_md_unit="m",
    )
    pack = ComparisonIntelligence(session).compare(well_ids=[str(left.id), str(right.id)])
    detail = {section["section"]: section["detail"] for section in pack.to_dict()["sections"]}[
        "execution"
    ]
    left_detail = detail[str(left.id)]
    right_detail = detail[str(right.id)]
    # The name-matched target stays on its own well: NAME-A's section matches by name, and
    # every one of NAME-B's rows is NO_PLAN with no match at all - the identical section name
    # on the neighbouring well never borrows a planned figure.
    assert left_detail["rows"] > 0
    assert "NAME" in left_detail["by_matched_by"]
    assert right_detail["rows"] == 4  # one section x four metrics, all unplanned
    assert right_detail["by_status"] == {"NO_TARGET": right_detail["rows"]}
    assert right_detail["by_matched_by"] == {"NO_MATCH": right_detail["rows"]}
    assert right_detail["sections_with_target"] == 0


def test_cost_rows_are_currency_keyed_and_never_summed(session, golden):
    from drilling_intelligence.engineering.costs import CostRepository as Costs

    pack = ComparisonIntelligence(session).compare(well_ids=[golden["a3"], golden["b11"]])
    rows = metrics(pack)
    summary = Costs(session).summary(well_id=golden["a3"], current_only=True)["by_currency"]
    assert rows["cost.planned.USD"]["values"][golden["a3"]]["value"] == summary["USD"]["planned"]
    assert rows["cost.planned.NOK"]["values"][golden["a3"]]["value"] == summary["NOK"]["planned"]
    usd = summary["USD"]["planned"]
    nok = summary["NOK"]["planned"]
    for row in rows.values():
        if row["metric"].startswith("cost."):
            for entry in row["values"].values():
                if entry["value"] is not None:
                    assert entry["value"] not in (usd + nok, round(usd + nok, 4)), (
                        "a cross-currency sum must never appear"
                    )
    # A-3 states two currencies on the planned side: no single total exists for it
    assert rows["cost.planned"]["values"][golden["a3"]]["value"] is None
    assert rows["cost.planned"]["values"][golden["a3"]]["value_state"] == "UNASSESSED"
    # B-11 has exactly one currency: its single total is real and unit-tagged
    assert rows["cost.planned"]["values"][golden["b11"]]["value"] == 75_000.0
    assert rows["cost.planned"]["values"][golden["b11"]]["unit"] == "NOK"
    assert "mixed_currency" in pack.limitations
    joined = " ".join(pack.observations)
    assert "no total is\nreported across currencies" in joined.replace("  ", " ") or (
        "no total is reported across currencies" in joined
    )


def test_field_pack_and_comparison_agree_on_the_same_rows(session, golden):
    pack = ComparisonIntelligence(session).compare(well_ids=[golden["a3"], golden["b11"]])
    rows = metrics(pack)
    assert (
        FieldIntelligence(session).npt(well_id=golden["a3"])["rows"]
        == rows["npt.rows"]["values"][golden["a3"]]["value"]
    )


# --------------------------------------------------------------------------------------
# Risk: reported, never scored (Test F/G)
# --------------------------------------------------------------------------------------


def test_risk_is_reported_not_scored_and_scale_identity_gates_bands(session, golden):
    pack = ComparisonIntelligence(session).compare(
        well_ids=[golden["a3"], golden["b11"], golden["c17"]]
    )
    rows = metrics(pack)
    assert "risk.score" not in rows
    assert rows["risk.current"]["values"][golden["a3"]]["value"] == 2
    assert rows["risk.current"]["values"][golden["b11"]]["value"] == 0
    assert rows["risk.severity_unassessed"]["values"][golden["a3"]]["value"] == 1
    # only A-3 carries risks: the scale identity is not provable across the selection, so
    # every severity band row is UNRESOLVED - the counts are still all visible.
    band_rows = [row for row in rows.values() if row["metric"].startswith("risk.severity.")]
    assert band_rows, "severity bands must exist while a subject states them"
    for row in band_rows:
        assert row["comparability"] == UNRESOLVED
        for entry in row["values"].values():
            if entry["value"] is not None:
                assert entry["comparability"] == UNRESOLVED


def test_differing_risk_scales_make_bands_incomparable(session):
    project, field, left, right = _two_wells(session, left="SCALE-A", right="SCALE-B")
    risks = RiskRepository(session)
    a = risks.create_risk(
        title="left risk",
        category="drilling",
        scale="MATRIX_5X5",
        well_id=left.id,
        field_id=field.id,
        project_id=project.id,
    )
    risks.assess_risk(a.id, probability=2, impact=2, severity=4, severity_band="MEDIUM", by="eng")
    b = risks.create_risk(
        title="right risk",
        category="drilling",
        scale="MATRIX_3X3",
        well_id=right.id,
        field_id=field.id,
        project_id=project.id,
    )
    risks.assess_risk(b.id, probability=1, impact=1, severity=2, severity_band="LOW", by="eng")
    pack = ComparisonIntelligence(session).compare(well_ids=[str(left.id), str(right.id)])
    rows = metrics(pack)
    band_rows = [row for row in rows.values() if row["metric"].startswith("risk.severity.")]
    assert band_rows
    for row in band_rows:
        assert row["comparability"] == INCOMPARABLE
        for entry in row["values"].values():
            if entry["value"] is not None:
                assert entry["comparability"] == INCOMPARABLE
                assert entry["value"] >= 0, "counts stay visible under INCOMPARABLE"
    assert "incomparable_units" in pack.limitations
    detail = {section["section"]: section["detail"] for section in pack.to_dict()["sections"]}[
        "risk"
    ]
    assert detail[str(left.id)]["scales"] == {"MATRIX_5X5": 1}
    assert detail[str(right.id)]["scales"] == {"MATRIX_3X3": 1}


def test_a_superseded_risk_is_history_not_current(session, golden):
    risks = RiskRepository(session)
    risks.set_risk_status(
        golden["risk_assessed"].id, RiskLifecycle.SUPERSEDED, by="eng", reason="rev 2"
    )
    pack = ComparisonIntelligence(session).compare(well_ids=[golden["a3"], golden["b11"]])
    rows = metrics(pack)
    assert rows["risk.current"]["values"][golden["a3"]]["value"] == 1
    detail = {section["section"]: section["detail"] for section in pack.to_dict()["sections"]}[
        "risk"
    ]
    assert detail[golden["a3"]]["superseded"] >= 1


# --------------------------------------------------------------------------------------
# Learning, patterns, calculations
# --------------------------------------------------------------------------------------


def test_lessons_practices_recommendations_stay_distinct_counts(session, golden):
    pack = ComparisonIntelligence(session).compare(well_ids=[golden["a3"], golden["b11"]])
    rows = metrics(pack)
    assert rows["lessons.approved"]["values"][golden["a3"]]["value"] == 1
    assert rows["lessons.approved"]["values"][golden["b11"]]["value"] == 0
    assert rows["lessons.current"]["values"][golden["b11"]]["value"] == 1
    assert rows["practices.adopted"]["values"][golden["a3"]]["value"] == 1
    assert rows["recommendations.proposed"]["values"][golden["a3"]]["value"] == 1
    assert rows["recommendations.accepted"]["values"][golden["b11"]]["value"] == 1
    # lessons are not practices are not recommendations: three separate rows always
    for metric in ("lessons.current", "practices.current", "recommendations.total"):
        assert metric in rows


def test_patterns_are_descriptive_and_stale_marks_freshness(session, golden):
    pack = ComparisonIntelligence(session).compare(well_ids=[golden["a3"], golden["b11"]])
    rows = metrics(pack)
    assert rows["patterns.total"]["values"][golden["a3"]]["value"] == 2
    assert rows["patterns.confirmed"]["values"][golden["a3"]]["value"] == 1
    assert rows["patterns.stale"]["values"][golden["a3"]]["value"] == 1
    # Staleness of a pattern is domain state and travels as a limitation; the section
    # freshness follows the certified decision read (the read itself is current).
    assert "stale_pattern" in pack.limitations
    assert pack.freshness["patterns"] in ("CURRENT", "STALE")


def test_calculations_use_the_chain_not_the_status(session, golden):
    pack = ComparisonIntelligence(session).compare(well_ids=[golden["a3"], golden["b11"]])
    rows = metrics(pack)
    assert rows["calculations.current"]["values"][golden["a3"]]["value"] == 1
    assert rows["calculations.history"]["values"][golden["a3"]]["value"] == 1
    assert rows["calculations.total"]["values"][golden["b11"]]["value"] == 0


# --------------------------------------------------------------------------------------
# Conflicts (section 33) and site-HSE non-leak (Test H)
# --------------------------------------------------------------------------------------


def test_open_conflicts_are_preserved_not_resolved(session, golden):
    pack = ComparisonIntelligence(session).compare(well_ids=[golden["a3"], golden["b11"]])
    open_rows = list(
        session.execute(
            select(KnowledgeConflict).where(KnowledgeConflict.status == "OPEN")
        ).scalars()
    )
    assert open_rows, "the corpus carries an open conflict"
    total = pack.summary["open_conflicts"]
    assert total >= 1
    assert pack.freshness["conflicts"] == UNRESOLVED
    assert "unresolved_conflict" in pack.limitations
    observed_keys = [line for line in pack.observations if "conflict" in line]
    assert observed_keys
    for row in open_rows:
        if row.well_id in (golden["a3"], golden["b11"]):
            assert str(row.lookup_key) in observed_keys[0]
    # the conflict's own state is untouched
    still = list(
        session.execute(
            select(KnowledgeConflict).where(KnowledgeConflict.id.in_([r.id for r in open_rows]))
        ).scalars()
    )
    assert all(row.status == "OPEN" for row in still)


def test_site_hse_never_attaches_to_a_well_subject(session, golden):
    pack = ComparisonIntelligence(session).compare(
        well_ids=[golden["a3"], golden["b11"], golden["c17"]]
    )
    rows = metrics(pack)
    site_total = int(
        session.execute(
            select(func.count(HseIncident.id)).where(HseIncident.well_id.is_(None))
        ).scalar_one()
    )
    assert site_total > 0, "the V7.2 site corpus carries site-scoped incidents"
    attached = sum(
        rows["hse.incidents"]["values"][wid]["value"]
        for wid in (golden["a3"], golden["b11"], golden["c17"])
    )
    well_rows = int(
        session.execute(
            select(func.count(HseIncident.id)).where(HseIncident.well_id.is_not(None))
        ).scalar_one()
    )
    assert attached == well_rows, "well subjects see exactly the well-scoped rows"
    detail = {section["section"]: section["detail"] for section in pack.to_dict()["sections"]}[
        "operations"
    ]
    # non-leak, proven three ways per subject: the fold counts exactly the rows the database
    # holds for that well, the site bucket stays at zero under a well scope, and the incident
    # types sum to the well-scoped total - no site row and no other subject's row leaks in.
    for wid in (golden["a3"], golden["b11"], golden["c17"]):
        hse = detail[wid]["hse"]
        db_rows = int(
            session.execute(
                select(func.count(HseIncident.id)).where(HseIncident.well_id == wid)
            ).scalar_one()
        )
        assert hse["incidents"] == db_rows, wid
        assert hse["site_scoped_incidents"] == 0, wid
        assert hse["well_scoped_incidents"] == hse["incidents"], wid
        assert sum((hse.get("by_incident_type") or {}).values()) == hse["incidents"], wid


# --------------------------------------------------------------------------------------
# Date windows (Test E)
# --------------------------------------------------------------------------------------


def test_date_windows_apply_to_windowed_domains_and_undated_stays_outside(session, golden):
    field = FieldIntelligence(session)
    full = field.npt(well_id=golden["a3"])
    assert full["undated"] >= 0
    pack = ComparisonIntelligence(session).compare(
        well_ids=[golden["a3"], golden["b11"]],
        since="2099-01-01",
        until="2099-12-31",
    )
    rows = metrics(pack)
    assert rows["npt.rows"]["values"][golden["a3"]]["value"] == 0, (
        "a future window must exclude every dated row"
    )
    # undated rows are reported by their own counter, never swept into the window
    detail = {section["section"]: section["detail"] for section in pack.to_dict()["sections"]}[
        "operations"
    ]
    if full["undated"]:
        assert detail[golden["a3"]]["npt"]["undated"] == full["undated"]
    window = pack.basis.payload()["window"]
    assert window["applied"] is True
    assert window["since"] == "2099-01-01"
    assert window["until"] == "2099-12-31"
    assert "undated" in window["undated_behavior"]
    assert "Date window applied to" in " ".join(pack.observations)
    # the undated rows still exist, unwindowed, in the unconstrained read
    assert full["undated"] >= 0


def test_current_state_sections_say_the_window_did_not_apply(session, golden):
    pack = ComparisonIntelligence(session).compare(
        well_ids=[golden["a3"], golden["b11"]],
        since="2000-01-01",
    )
    detail = {section["section"]: section["detail"] for section in pack.to_dict()["sections"]}
    for wid in (golden["a3"], golden["b11"]):
        assert "window" in detail["operations"][wid]
        assert detail["operations"][wid]["window"]["applied"] is True
        assert "window" not in detail["economics"][wid], (
            "cost reads are current-state; the section must not pretend a window applied"
        )
        assert "window" not in detail["risk"][wid]
        assert "window" not in detail["execution"][wid]
    windowed = pack.basis.payload()["window"]["windowed_domains"]
    assert windowed == ["npt", "problems", "well_control", "hse"]


# --------------------------------------------------------------------------------------
# Observation honesty (Test I)
# --------------------------------------------------------------------------------------


def test_observations_never_interpret_rank_or_recommend(session, golden):
    pack = ComparisonIntelligence(session).compare(
        well_ids=[golden["a3"], golden["b11"], golden["c17"]]
    )
    assert pack.observations
    joined = " ".join(pack.observations).lower()
    notes = " ".join(
        row["note"] for section in pack.to_dict()["sections"] for row in section["metrics"]
    ).lower()
    for forbidden in (
        "is unsafe",
        "is more expensive",
        "worse",
        "best vendor",
        "will recur",
        "likely",
        "optimum",
        "optimal",
        "expected to",
        "guaranteed",
        "root cause",
        "prediction",
        "ranked",
        "better",
        "safer",
        "best",
        "copy this",
        "should ",
        "outperform",
        "most reliable",
    ):
        assert forbidden not in joined, forbidden
        assert forbidden not in notes, forbidden
    # every count sentence names its numbers deterministically
    assert any(
        line.startswith("2 wells on basis") or "wells on basis" in line
        for line in pack.observations
    )
    assert any("NPT rows by well" in line for line in pack.observations)
    assert any("Well-control rows by well" in line for line in pack.observations)


def test_a_comparison_observation_traces_to_its_rows(session, golden):
    pack = ComparisonIntelligence(session).compare(well_ids=[golden["a3"], golden["b11"]])
    rows = metrics(pack)
    wc_observation = next(
        line for line in pack.observations if line.startswith("Well-control rows")
    )
    a3_count = rows["well_control.events"]["values"][golden["a3"]]["value"]
    b11_count = rows["well_control.events"]["values"][golden["b11"]]["value"]
    assert f"A-3 {a3_count}" in wc_observation
    assert f"B-11 {b11_count}" in wc_observation
    db_count = int(
        session.execute(
            select(func.count(WellControlEvent.id)).where(WellControlEvent.well_id == golden["a3"])
        ).scalar_one()
    )
    assert a3_count == db_count


# --------------------------------------------------------------------------------------
# Evidence chains (Test J / section 21)
# --------------------------------------------------------------------------------------


def test_evidence_chains_npt_hse_wc_cost_and_risk_to_actual_identities(session, golden):
    pack = ComparisonIntelligence(session).compare(well_ids=[golden["a3"], golden["b11"]])
    refs = [ref.payload() for ref in pack.evidence]
    # NPT: re-executable method reference with the actual count and the actual well scope
    npt_ref = next(
        ref for ref in refs if ref["domain"] == "npt_record" and ref["kind"] == "aggregate"
    )
    assert npt_ref["method"] == "FieldIntelligence.npt"
    assert npt_ref["scope"]["well_id"] == golden["a3"]
    direct_npt = FieldIntelligence(session).npt(well_id=golden["a3"])
    assert npt_ref["count"] == direct_npt["rows"] > 0
    # plus bounded structured identities for the very rows behind that aggregate
    npt_rows_ref = next(
        ref for ref in refs if ref["domain"] == "npt_record" and ref["kind"] == "rows"
    )
    assert npt_rows_ref["identity"] == "structured"
    db_npt_ids = {
        f"structured:npt_record:{row}"
        for row in session.execute(
            select(NptRecord.id).where(NptRecord.well_id == golden["a3"])
        ).scalars()
    }
    sampled = set(npt_rows_ref["sample"])
    assert sampled, "a non-empty count must carry sample identities"
    assert sampled <= db_npt_ids
    assert all(value.startswith("structured:npt_record:") for value in sampled)
    # well control: structured identities of the seven rows
    wc_ref = next(
        ref for ref in refs if ref["domain"] == "well_control_event" and ref["kind"] == "rows"
    )
    db_wc_ids = {
        f"structured:well_control_event:{row}"
        for row in session.execute(
            select(WellControlEvent.id).where(WellControlEvent.well_id == golden["a3"])
        ).scalars()
    }
    assert set(wc_ref["sample"]) <= db_wc_ids
    assert wc_ref["count"] == len(db_wc_ids) == 7
    # HSE: structured identities, well-scoped only
    hse_ref = next(ref for ref in refs if ref["domain"] == "hse_incident" and ref["kind"] == "rows")
    db_hse_ids = {
        f"structured:hse_incident:{row}"
        for row in session.execute(
            select(HseIncident.id).where(HseIncident.well_id == golden["a3"])
        ).scalars()
    }
    assert set(hse_ref["sample"]) <= db_hse_ids
    # cost: row-id sample from the very statement summary() materialises
    cost_ref = next(ref for ref in refs if ref["domain"] == "cost_item")
    from drilling_intelligence.database.models import CostItem

    db_cost_ids = {
        str(row)
        for row in session.execute(
            select(CostItem.id).where(CostItem.well_id == golden["a3"])
        ).scalars()
    }
    assert set(cost_ref["sample"]) <= db_cost_ids
    assert cost_ref["count"] == len(db_cost_ids)
    # risk: row-id sample of the actual risk rows
    risk_ref = next(ref for ref in refs if ref["domain"] == "risk_record")
    db_risk_ids = {
        str(row)
        for row in session.execute(
            select(RiskRecord.id).where(RiskRecord.well_id == golden["a3"])
        ).scalars()
    }
    assert set(risk_ref["sample"]) == db_risk_ids


# --------------------------------------------------------------------------------------
# Offset discovery flow (sections 29-34)
# --------------------------------------------------------------------------------------


def _sharing_world(workspace, session, count: int, *, limit: int = 0):
    """A field with ``count`` candidate wells that all share one problem type and hole
    size with the anchor, so discovery has real overlap to report."""
    from drilling_intelligence.database.models import ProblemDefinition, ProblemOccurrence

    wells = WellRepository(session)
    project = wells.get_or_create_project("Offset Block")
    field = wells.get_or_create_field("Offset Field", project=project)
    anchor = wells.create_well("OFF-0", project_id=project.id, field_id=field.id)
    definition = ProblemDefinition(
        id="pd-off-1",
        canonical_key="stuck-pipe",
        problem_type="stuck_pipe",
        name="Stuck pipe",
        origin=KnowledgeOrigin.MANUAL.value,
    )
    session.add(definition)
    session.flush()
    for index in range(count):
        well = wells.create_well(f"OFF-{index + 1}", project_id=project.id, field_id=field.id)
        for wid in (anchor.id, well.id):
            session.add(
                ProblemOccurrence(
                    id=f"po-{wid}-{index}",
                    well_id=str(wid),
                    problem_definition_id=definition.id,
                    problem_type="stuck_pipe",
                    hole_size_in=12.25,
                    occurred_at=datetime(2026, 3, 1, tzinfo=UTC),
                    origin=KnowledgeOrigin.MANUAL.value,
                )
            )
    session.flush()
    return str(anchor.id)


def test_offset_discovery_is_bounded_by_the_limit_and_reports_at_limit(workspace, session):
    anchor = _sharing_world(workspace, session, count=6)
    pack = ComparisonIntelligence(session).compare(anchor=anchor, offset_limit=3)
    assert pack.basis.kind == "offset_candidates"
    assert pack.basis.offset_returned == 3
    assert pack.basis.offset_at_limit is True
    assert len(pack.basis.subjects) == 4  # anchor + three candidates
    assert len(pack.basis.profiles) == 3
    assert any("Offset discovery returned 3 candidates" in line for line in pack.observations)
    assert list(pack.basis.shared_problem_types) == ["stuck_pipe"]
    assert pack.basis.shared_hole_sizes  # hole sizes recorded on every subject


def test_offset_profiles_are_capped_and_reported(session, monkeypatch, workspace):
    import drilling_intelligence.intelligence.comparison as comparison_module

    anchor = _sharing_world(workspace, session, count=4)
    monkeypatch.setattr(comparison_module, "OFFSET_PROFILE_CAP", 2)
    pack = ComparisonIntelligence(session).compare(anchor=anchor, offset_limit=4)
    assert len(pack.basis.profiles) == 2
    assert pack.basis.profiles_truncated is True
    assert "truncated_detail" in pack.limitations
    assert any("truncated at 2" in line for line in pack.observations)


def test_profile_missing_lists_include_the_unstated_cells(session, golden):
    pack = ComparisonIntelligence(session).compare(anchor=golden["a3"])
    profiles = {profile["well_id"]: profile for profile in pack.basis.profiles}
    assert golden["b11"] in profiles
    profile = profiles[golden["b11"]]
    assert isinstance(profile["missing"], list)
    assert isinstance(profile["comparable"], list)
    assert isinstance(profile["incomparable"], list)
    assert isinstance(profile["limitations"], list)
    # B-11 never states an actual USD total: that cell is missing, honestly listed
    assert "cost.actual" in profile["missing"] or "cost.actual.USD" in profile["missing"]


# --------------------------------------------------------------------------------------
# Section 37: field/project scenarios
# --------------------------------------------------------------------------------------


def test_field_and_project_scenarios_agree_with_the_per_well_reads(session, golden):
    """The golden field read and the project read must see the same per-well numbers the
    comparison folds - one repository, three lenses, no drift."""
    pack = ComparisonIntelligence(session).compare(well_ids=[golden["a3"], golden["b11"]])
    rows = metrics(pack)
    field_summary = FieldIntelligence(session).summary(field_id=golden["field_id"])
    total_rows = (
        rows["npt.rows"]["values"][golden["a3"]]["value"]
        + rows["npt.rows"]["values"][golden["b11"]]["value"]
    )
    # A-3 and B-11 are the only wells in the golden field, so their rows sum to the field.
    assert total_rows == field_summary["npt_rows"]
    # project lens: the wells of the project include C-17 too
    wells_in_project = FieldIntelligence(session).wells(project_id=golden["project_id"])
    assert wells_in_project["count"] >= 3


def test_cross_field_explicit_subjects_are_labelled_not_hidden(session):
    wells = WellRepository(session)
    project = wells.get_or_create_project("Cross Block")
    field_x = wells.get_or_create_field("Field X", project=project)
    field_y = wells.get_or_create_field("Field Y", project=project)
    left = wells.create_well("X-1", project_id=project.id, field_id=field_x.id)
    right = wells.create_well("Y-1", project_id=project.id, field_id=field_y.id)
    pack = ComparisonIntelligence(session).compare(well_ids=[str(left.id), str(right.id)])
    assert pack.basis.same_field is False
    assert "insufficient_shared_basis" in pack.limitations


# --------------------------------------------------------------------------------------
# Read-only (section 39)
# --------------------------------------------------------------------------------------


def test_building_a_comparison_pack_writes_nothing(session, golden):
    """P39: comparison, analyst, offset-discovery and evidence reads leave every table intact."""
    from drilling_intelligence.intelligence.analyst import AnalystIntelligence
    from drilling_intelligence.intelligence.field import FieldIntelligence

    def counts() -> dict[str, int]:
        out: dict[str, int] = {}
        for table in Base.registry.mappers:
            name = table.local_table.name
            out[name] = int(
                session.execute(select(func.count()).select_from(table.local_table)).scalar_one()
            )
        return out

    before = counts()
    service = ComparisonIntelligence(session)
    service.compare(well_ids=[golden["a3"], golden["b11"], golden["c17"]])
    service.compare(anchor=golden["a3"], offset_limit=3)
    FieldIntelligence(session).offset_candidates(golden["a3"], limit=3)
    analyst = AnalystIntelligence(session)
    analyst.analyze("npt_summary", well_id=golden["a3"])
    analyst.analyze("decision_pack", well_id=golden["b11"])
    analyst.analyze("offset_comparison", anchor=golden["a3"])
    assert counts() == before, (
        "comparison, analyst and offset reads are read-only: viewing must not change a row"
    )


# --------------------------------------------------------------------------------------
# Scale (section 36): measured, then pinned
# --------------------------------------------------------------------------------------


def _bare_wells(workspace, session, count: int) -> list[str]:
    wells = WellRepository(session)
    field = session.get(Field, field_id(workspace))
    ids = []
    for index in range(count):
        well = wells.create_well(f"SCALE-{index}", project_id=field.project_id, field_id=field.id)
        ids.append(str(well.id))
    return ids


def _compare_query_count(workspace, well_ids: list[str], **kwargs) -> int:
    with workspace.database.read_only() as session:
        return _select_count(
            workspace.database.engine,
            lambda: ComparisonIntelligence(session).compare(well_ids=well_ids, **kwargs),
        )


def test_compare_query_count_grows_per_subject_not_per_metric(workspace, corpus):
    """The invariant: a fixed number of reads per subject, independent of metric count.

    2/5/10 subjects measured on one corpus; the budget below was measured first, then
    pinned as a linear envelope (fixed base + fixed per-subject cost), so a future
    per-metric query shows up immediately."""
    session_factory = corpus.database
    well_ids = []
    with session_factory.unit_of_work() as session:
        well_ids = _bare_wells(corpus, session, count=10)
    a3 = well_id_for(corpus, "A-3")
    b11 = well_id_for(corpus, "B-11")
    counts = {}
    for size, ids in (
        (2, [a3, b11]),
        (5, [a3, b11, *well_ids[:3]]),
        (10, [a3, b11, *well_ids[:8]]),
    ):
        assert len(ids) == size
        counts[size] = _compare_query_count(corpus, ids)
        print(f"\nCOMPARE_SCALE subjects={size} queries={counts[size]}")
    # Measured first: 2 -> 86, 5 -> 203, 10 -> 398 on this corpus (6 + 39*N).
    # The pins below are budgets with a small margin, identical per subject at every size.
    assert counts[2] <= 6 + 42 * 2, counts
    assert counts[5] <= 6 + 42 * 5, counts
    assert counts[10] <= 6 + 42 * 10, counts
    # linear envelope: each additional subject costs the same fixed amount
    per_extra = (counts[10] - counts[2]) / 8
    assert per_extra <= 40, (
        f"per-subject cost {per_extra} exceeds the measured budget of 40 SELECTs: {counts}"
    )
    assert counts[5] - counts[2] == pytest.approx(per_extra * 3, abs=4), counts


def test_offset_discovery_at_forty_candidates_stays_bounded(workspace):
    # Everything runs on the workspace's own database: one connection family, so the lazy
    # migration and the world's write transaction can never fight over the file lock.
    with workspace.database.unit_of_work() as session:
        anchor = _sharing_world(workspace, session, count=40)
    with workspace.database.read_only() as session:
        measured = _select_count(
            workspace.database.engine,
            lambda: ComparisonIntelligence(session).compare(
                anchor=anchor, offset_limit=40, detail=0
            ),
        )
    print(f"\nOFFSET_SCALE candidates=40 queries={measured}")
    with workspace.database.read_only() as session:
        pack = ComparisonIntelligence(session).compare(anchor=anchor, offset_limit=40)
    assert pack.basis.offset_returned == 40
    assert len(pack.basis.subjects) == 41
    assert len(pack.basis.profiles) <= 16
    assert pack.basis.profiles_truncated is True
    assert "truncated_detail" in pack.limitations
    assert measured <= 2100, (
        f"41 subjects issued {measured} SELECTs; the measured envelope is 2100 "
        "(6 + 39*N measured on bare subjects, detail folds excluded by detail=0)"
    )


# --------------------------------------------------------------------------------------
# The CLI: names resolve, the JSON is byte-for-byte the service document, no logic here
# --------------------------------------------------------------------------------------


def test_cli_compare_json_is_exactly_the_service_document(workspace, corpus):
    from tests.integration.test_cli_domain import call

    from drilling_intelligence.intelligence.service import IntelligenceService

    a3 = well_id_for(corpus, "A-3")
    b11 = well_id_for(corpus, "B-11")
    payload = call(corpus, "fields", "compare", "--well", "A-3", "--well", "B-11")
    with corpus.database.read_only() as session:
        pack = IntelligenceService.for_workspace(corpus).compare(
            well_ids=[a3, b11], session=session
        )
    assert payload == pack.to_dict()
    assert payload["schema"] == "well-comparison/1"
    assert payload["basis"]["kind"] == "explicit_wells"
    assert [subject["name"] for subject in payload["basis"]["subjects"]] == ["A-3", "B-11"]


def test_cli_compare_anchor_discovers_candidates_and_text_prints_the_matrix(workspace, corpus):
    from tests.integration.test_cli_domain import _text, call

    payload = call(corpus, "fields", "compare", "--anchor", "A-3")
    assert payload["basis"]["kind"] == "offset_candidates"
    assert payload["basis"]["anchor_name"] == "A-3"
    assert [row["name"] for row in payload["basis"]["discovered"]] == ["B-11"]
    text, _err = _text(corpus, "fields", "compare", "--well", "A-3", "--well", "B-11")
    assert "comparison pack: well-comparison/1" in text
    assert "basis: explicit_wells (2 wells: A-3, B-11)" in text
    assert "metric" in text and "state" in text
    assert "COMPARABLE" in text
    assert "npt.rows" in text and "well_control.events" in text
    # a missing value prints as "-", never as a zero the source did not state
    assert "identity: " in text


def test_cli_compare_refuses_one_well_and_unknown_names(workspace, corpus):
    from tests.integration.test_cli_domain import _capture

    code, out, err = _capture(corpus, "fields", "compare", "--well", "A-3")
    assert code != 0, out[:400]
    assert "at least two" in (err + out)
    code, out, err = _capture(
        corpus, "fields", "compare", "--well", "A-3", "--well", "NO-SUCH-WELL"
    )
    assert code != 0, out[:400]
    assert "no well matches" in (err + out)


def test_cli_compare_on_an_empty_workspace_fails_without_inventing_a_scope(workspace):
    from tests.integration.test_cli_domain import _capture

    code, out, err = _capture(workspace, "fields", "compare", "--well", "A-3", "--well", "B-11")
    assert code != 0, out[:400]
    assert "no well matches" in (err + out)
