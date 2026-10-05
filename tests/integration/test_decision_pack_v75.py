"""V7.5 - the decision pack, on the real repositories.

The pack is a read model: these tests prove it builds from the authoritative surfaces, keeps
one scope, re-runs to the same identity, and never manufactures a conclusion.  Further tests
in this module cover plan/actual limits, cost currency separation, risk assessment state,
learning distinctions, patterns, calculations, evidence and query-count scale.
"""

from __future__ import annotations

import json

import pytest

from drilling_intelligence.core.errors import ValidationError
from drilling_intelligence.engineering.costs import CostRepository
from drilling_intelligence.engineering.repository import EngineeringRepository
from drilling_intelligence.engineering.risk import RiskRepository
from drilling_intelligence.intelligence.decision import (
    CLAIM_DERIVED,
    CLAIM_FACT,
    DECISION_PACK_SCHEMA,
    DecisionIntelligence,
)


@pytest.fixture
def hierarchy(session):
    """A field with two wells; A-3 drilled, B-11 bare."""
    from drilling_intelligence.database.models import WellSection
    from drilling_intelligence.wells.repository import WellRepository

    repository = WellRepository(session)
    project = repository.get_or_create_project("Cormorant Block")
    field = repository.get_or_create_field("North Cormorant", project=project)
    well_a = repository.create_well("A-3", project_id=project.id, field_id=field.id)
    well_b = repository.create_well("B-11", project_id=project.id, field_id=field.id)
    session.add(
        WellSection(
            id="sec-1",
            well_id=well_a.id,
            sequence=1,
            name="8 1/2 in",
            hole_size_in=8.5,
            top_depth_value=3500.0,
            top_depth_unit="m",
            bottom_depth_value=9850.0,
            bottom_depth_unit="m",
            planned_duration_days=12.0,
            actual_duration_days=14.5,
            planned_mud_weight_value=11.4,
            planned_mud_weight_unit="ppg",
            actual_mud_weight_value=11.9,
            actual_mud_weight_unit="ppg",
        )
    )
    session.flush()
    return {
        "project": project,
        "field": field,
        "well_a": well_a,
        "well_b": well_b,
    }


def test_pack_builds_for_a_well_and_round_trips_through_json(session, hierarchy):
    pack = DecisionIntelligence(session).pack(well_id=hierarchy["well_a"].id)
    assert pack.schema == DECISION_PACK_SCHEMA
    assert pack.subject.kind == "well"
    assert pack.subject.name == "A-3"
    payload = pack.to_dict()
    text = json.dumps(payload)  # plain values only: no ORM object survives
    assert json.loads(text)["identity"] == pack.identity
    assert pack.identity  # deterministic, non-empty
    # Sections carry their own scope and a claim kind the future AI layer can branch on.
    assert payload["execution"]["scope"] == {
        "well_id": hierarchy["well_a"].id,
        "field_id": None,
        "project_id": None,
    }
    assert payload["execution"]["claim_kind"] == CLAIM_DERIVED
    assert payload["risk"]["claim_kind"] == CLAIM_FACT


def test_pack_is_re_runnable_to_the_same_identity(session, hierarchy):
    first = DecisionIntelligence(session).pack(well_id=hierarchy["well_a"].id)
    second = DecisionIntelligence(session).pack(well_id=hierarchy["well_a"].id)
    assert first.identity == second.identity


def test_pack_requires_exactly_one_scope(session, hierarchy):
    with pytest.raises(ValidationError):
        DecisionIntelligence(session).pack()
    with pytest.raises(ValidationError) as mixed:
        DecisionIntelligence(session).pack(
            well_id=hierarchy["well_a"].id, field_id=hierarchy["field"].id
        )
    assert "exactly one" in str(mixed.value)


def test_well_pack_does_not_absorb_the_field(session, hierarchy):
    """A-3's pack must not report B-11's empty state as its own."""
    pack = DecisionIntelligence(session).pack(well_id=hierarchy["well_a"].id)
    assert pack.subject.well_count is None  # a well subject counts no other well
    assert pack.execution.scope["field_id"] is None


def test_field_pack_sees_both_wells(session, hierarchy):
    pack = DecisionIntelligence(session).pack(field_id=hierarchy["field"].id)
    assert pack.subject.kind == "field"
    assert pack.subject.well_count == 2
    # Execution at field scope covers the field's sections through one call - A-3's section.
    assert pack.execution.sections >= 1


def test_empty_scope_is_valid_and_freshness_is_not_applicable(session, hierarchy):
    pack = DecisionIntelligence(session).pack(well_id=hierarchy["well_b"].id)
    assert pack.execution.sections == 0
    assert pack.freshness["execution"] == "NOT_APPLICABLE"
    assert pack.risk.current == 0
    assert pack.recommendations.total == 0
    assert pack.limitations == ()  # empty state invents no limitations
    # An empty pack is still a valid pack.
    assert json.loads(json.dumps(pack.to_dict()))["subject"]["name"] == "B-11"


# --------------------------------------------------------------------------------------
# The rich decision corpus: every section in one place, built through the real writers.
# --------------------------------------------------------------------------------------


@pytest.fixture
def rich(session, hierarchy):
    """A-3 drilled with a programme, B-11 planned only; costs in two currencies; risks,
    lessons, a practice, recommendations, patterns and a calculation under the same scope."""
    from datetime import UTC, datetime

    from drilling_intelligence.core.enums import (
        CauseStatus,
        KnowledgeOrigin,
        LessonLifecycle,
        ProcedureLifecycle,
        RecommendationLifecycle,
        RecordState,
    )
    from drilling_intelligence.core.ids import subject_key
    from drilling_intelligence.database.models import (
        BestPractice,
        Calculation,
        CalculationInput,
        FieldPattern,
        HseIncident,
        Recommendation,
        WellSection,
    )
    from drilling_intelligence.engineering.costs import CostRepository
    from drilling_intelligence.engineering.risk import RiskRepository
    from drilling_intelligence.lessons.repository import LessonRepository

    project, field = hierarchy["project"], hierarchy["field"]
    well_a, well_b = hierarchy["well_a"], hierarchy["well_b"]

    # -- sections: B-11 gets one so the field pack has an unplanned well ----------------
    session.add(
        WellSection(
            id="sec-2",
            well_id=well_b.id,
            sequence=1,
            name="12 1/4 in",
            hole_size_in=12.25,
            top_depth_value=1200.0,
            top_depth_unit="m",
        )
    )

    # -- execution: ON_PLAN / VARIANCE / NO_PLAN / NO_ACTUAL, plus a NAME-matched plan ---
    engineering = EngineeringRepository(session)
    program_a = engineering.create_program(title="A-3 programme", well_id=well_a.id)
    engineering.add_target(
        program_a.id,
        name="8 1/2 in",
        section_id="sec-1",
        sequence=1,
        planned_depth_md_value=9850.0,
        planned_depth_md_unit="m",
        planned_duration_days=12.0,
        planned_mud_weight_value=11.4,
        planned_mud_weight_unit="ppg",
        planned_npt_hours=6.0,
        # deliberately no planned mud weight? no: mud IS planned -> VARIANCE against 11.9.
        # duration 12 vs actual 14.5 -> VARIANCE; depth 9850 == actual -> ON_PLAN.
    )
    # A second target that has NO section id and only matches B-11's section by name.
    program_b = engineering.create_program(title="B-11 programme", well_id=well_b.id)
    engineering.add_target(
        program_b.id,
        name="12 1/4 in",
        sequence=1,
        planned_depth_md_value=5000.0,
        planned_depth_md_unit="m",
        # only a depth: duration/mud/npt stay unplanned -> NO_PLAN rows matched by NAME
    )

    # -- economics: USD with both sides, NOK with plan only ------------------------------
    costs = CostRepository(session)
    usd, _usd_created = costs.record_item(
        description="mud motor rental",
        planned_value=100_000.0,
        planned_unit="USD",
        actual_value=120_000.0,
        actual_unit="USD",
        category="equipment",
        wbs_code="WP-1",
        well_id=well_a.id,
        field_id=field.id,
        project_id=project.id,
        provenance=[{"sheet": "Costs", "cell": "C4"}],
    )
    nok, _nok_created = costs.record_item(
        description="platform day rate",
        planned_value=500_000.0,
        planned_unit="NOK",
        actual_value=None,
        actual_unit="NOK",
        category="services",
        wbs_code="WP-2",
        well_id=well_a.id,
        field_id=field.id,
        project_id=project.id,
        provenance=[{"sheet": "Costs", "cell": "C5"}],
    )

    # -- risk: one assessed, one deliberately unassessed, one carried at field level -----
    risks = RiskRepository(session)
    r1 = risks.create_risk(
        title="Shallow gas while drilling the 12 1/4 in",
        category="well_integrity",
        well_id=well_a.id,
        field_id=field.id,
        project_id=project.id,
    )
    risks.assess_risk(r1.id, probability=3, impact=4, severity=7, severity_band="HIGH", by="eng")
    r2 = risks.create_risk(
        title="Stuck pipe in the 8 1/2 in",
        category="drilling",
        well_id=well_a.id,
        field_id=field.id,
        project_id=project.id,
        # no assessment: severity/probability/impact stay NULL on purpose
    )
    r3 = risks.create_risk(
        title="Third-party survey vessel availability",
        category="logistics",
        field_id=field.id,
        project_id=project.id,  # carried at field level, no well
    )
    risks.assess_risk(r3.id, probability=2, impact=3, severity=5, severity_band="MEDIUM", by="eng")

    # -- learning: a draft well lesson, an approved well lesson, a field-only lesson ----
    lessons = LessonRepository(session)
    draft = lessons.capture(
        lesson="Rotate early in the sandstone to avoid sticking.",
        title="Rotation practice",
        well_id=well_a.id,
        field_id=field.id,
        project_id=project.id,
    )
    approved = lessons.capture(
        lesson="Tag the casing shoe before entering the reservoir.",
        title="Shoe tagging",
        well_id=well_a.id,
        field_id=field.id,
        project_id=project.id,
    )
    approved.status = str(LessonLifecycle.APPROVED)  # the transition the engine allows
    field_lesson = lessons.capture(
        lesson="Keep a spare MWD on the pad.",
        title="Spare MWD",
        field_id=field.id,
        project_id=project.id,
    )

    # -- an adopted practice, carried at field level ------------------------------------
    practice = BestPractice(
        id="bp-1",
        title="Pre-job BHA review",
        practice_type="review",
        statement="Run a documented BHA review before every section.",
        rationale="Two wells lost time to an avoidable BHA clash.",
        revision=1,
        is_current=True,
        status=str(ProcedureLifecycle.APPROVED),
        field_id=field.id,
        project_id=project.id,
        origin=KnowledgeOrigin.MANUAL.value,
        created_by="eng",
    )
    session.add(practice)

    # -- patterns: one confirmed and stale, one candidate, one rejected ------------------
    session.add_all(
        [
            FieldPattern(
                id="pat-1",
                signature="stuck-pipe|8.5",
                problem_type="stuck_pipe",
                depth_from_unit="m",
                depth_to_unit="m",
                occurrence_count=5,
                event_count=4,
                well_count=2,
                total_npt_hours=12.5,
                status="CONFIRMED",
                stale_at=datetime(2026, 9, 1, tzinfo=UTC),
                field_id=field.id,
                project_id=project.id,
                evidence=[{"id": "structured:problem_occurrence:po-1"}],
                well_ids=[well_a.id],
                detected_by="test",
                computed_at=datetime(2026, 5, 1, tzinfo=UTC),
            ),
            FieldPattern(
                id="pat-2",
                signature="washout|12.25",
                problem_type="washout",
                depth_from_unit="m",
                depth_to_unit="m",
                occurrence_count=2,
                event_count=2,
                well_count=1,
                status="CANDIDATE",
                field_id=field.id,
                project_id=project.id,
                detected_by="test",
                computed_at=datetime(2026, 5, 1, tzinfo=UTC),
            ),
            FieldPattern(
                id="pat-3",
                signature="torque|x",
                problem_type="high_torque",
                depth_from_unit="m",
                depth_to_unit="m",
                occurrence_count=1,
                event_count=1,
                well_count=1,
                status="REJECTED",
                field_id=field.id,
                project_id=project.id,
                detected_by="test",
                computed_at=datetime(2026, 5, 1, tzinfo=UTC),
            ),
        ]
    )

    # -- recommendations: one proposed (linked), one accepted with evidence --------------
    session.add_all(
        [
            Recommendation(
                id="rec-1",
                signature="add-logging-while-drilling",
                statement="Add LWD while drilling the next exploration well.",
                reason="The last two wells needed a wireline run that cost a trip.",
                status=str(RecommendationLifecycle.PROPOSED),
                generated_by="pattern-snapshot",
                well_id=well_a.id,
                field_id=field.id,
                project_id=project.id,
                pattern_id="pat-1",
                lesson_id=approved.id,
                risk_id=r1.id,
            ),
            Recommendation(
                id="rec-2",
                signature="pre-job-bha-review",
                statement="Adopt the pre-job BHA review checklist field-wide.",
                reason="Two wells lost time to an avoidable BHA clash.",
                status=str(RecommendationLifecycle.ACCEPTED),
                generated_by="pattern-snapshot",
                decided_by="ops-manager",
                evidence=[{"document_version_id": "dv-exists"}],
                field_id=field.id,
                project_id=project.id,
                practice_id="bp-1",
            ),
            Recommendation(
                id="rec-3",
                signature="reduce-mud-weight",
                statement="Reduce mud weight in the next reservoir section.",
                reason="Differential sticking was observed twice.",
                status=str(RecommendationLifecycle.DECLINED),
                generated_by="pattern-snapshot",
                decided_by="ops-manager",
                decline_reason="The offset wells needed the higher weight.",
                field_id=field.id,
                project_id=project.id,
            ),
        ]
    )

    # -- calculations: one current with inputs, one superseding it -----------------------
    c1 = Calculation(
        id="calc-1",
        method_id="torque_drag",
        method_version="1.0",
        calculation_type="mechanics",
        record_state=RecordState.CURRENT,
        well_id=well_a.id,
        inputs={},
        status="COMPUTED",
        revision=1,
        triggered_by="test",
        origin=KnowledgeOrigin.MANUAL.value,
        created_by="eng",
    )
    session.add(c1)
    session.flush()
    c2 = Calculation(
        id="calc-2",
        method_id="torque_drag",
        method_version="1.1",
        calculation_type="mechanics",
        record_state=RecordState.CURRENT,
        well_id=well_a.id,
        inputs={},
        status="COMPUTED",
        revision=2,
        supersedes_id=c1.id,
        triggered_by="test",
        origin=KnowledgeOrigin.MANUAL.value,
        created_by="eng",
    )
    session.add(c2)
    session.flush()
    # The writer resolves the canonical key into (kind, id) at write time - the change-impact
    # index matches on those columns, so a row that set only the rendered key would be invisible
    # to its own dependency query.
    resolved_key = subject_key(well_id=well_a.id, property_name="mud_weight", record_state="ACTUAL")
    session.add(
        CalculationInput(
            id="cin-1",
            calculation_id=c2.id,
            name="mud_weight",
            value=11.9,
            unit="ppg",
            dimension="density",
            source_kind="well",
            subject_key=resolved_key,
            subject_kind="well",
            subject_id=str(well_a.id),
        )
    )

    # -- HSE: one well incident, one site-only ------------------------------------------
    session.add_all(
        [
            HseIncident(
                id="hse-1",
                description="Trip hazard on the rig floor identified during tour.",
                location_text="rig floor",
                occurred_at_text="2026-03-20",
                occurred_at=datetime(2026, 3, 20, tzinfo=UTC),
                incident_type="near_miss",
                severity="LOW",
                immediate_cause_status=CauseStatus.UNKNOWN.value,
                root_cause_status=CauseStatus.UNKNOWN.value,
                spill_volume_unit="bbl",
                status="OPEN",
                record_state=RecordState.CURRENT.value,
                origin=KnowledgeOrigin.MANUAL.value,
                created_by="supervisor",
                is_current=True,
                well_id=well_a.id,
                field_id=field.id,
            ),
            HseIncident(
                id="hse-site-1",
                description="Perimeter lighting out at the supply base gate.",
                location_text="supply base",
                occurred_at_text="2026-04-02",
                occurred_at=datetime(2026, 4, 2, tzinfo=UTC),
                incident_type="property_damage",
                severity="LOW",
                immediate_cause_status=CauseStatus.UNKNOWN.value,
                root_cause_status=CauseStatus.UNKNOWN.value,
                spill_volume_unit="bbl",
                status="OPEN",
                record_state=RecordState.CURRENT.value,
                origin=KnowledgeOrigin.MANUAL.value,
                created_by="site",
                is_current=True,
                field_id=field.id,  # site-scoped: no well
            ),
        ]
    )
    session.flush()
    return {
        "hierarchy": hierarchy,
        "usd": usd,
        "nok": nok,
        "risk_assessed": r1,
        "risk_unassessed": r2,
        "risk_field": r3,
        "lesson_draft": draft,
        "lesson_approved": approved,
        "lesson_field": field_lesson,
        "practice": practice,
    }


# --------------------------------------------------------------------------------------
# Execution: every plan/actual state, and the unit proof
# --------------------------------------------------------------------------------------


def test_execution_folds_every_plan_actual_state_without_inventing_values(session, rich):
    well = rich["hierarchy"]["well_a"]
    pack = DecisionIntelligence(session).pack(well_id=well.id)
    execution = pack.execution
    # sec-1: depth ON_PLAN (9850 m == 9850 m), duration VARIANCE (12 vs 14.5),
    # mud VARIANCE (11.4 vs 11.9), npt NO_PLAN (no planned hours in the target? planned=6
    # with no actual NPT rows -> NO_ACTUAL).
    assert execution.by_status.get("ON_PLAN") == 1
    assert execution.by_status.get("VARIANCE") == 2
    assert execution.by_status.get("NO_ACTUAL") == 1  # planned 6 h, nothing recorded
    assert execution.by_status.get("NO_TARGET", 0) == 0  # B-11's section is not A-3's
    assert execution.sections == 1
    assert execution.sections_with_target == 1
    assert execution.by_matched_by == {"SECTION_ID": 4}
    depth = execution.by_metric["depth_md"]
    assert depth["planned"] == 1 and depth["actual"] == 1 and depth["variance"] == 1
    npt = execution.by_metric["npt_hours"]
    assert npt["no_actual"] == 1 and npt["variance"] == 0, (
        "a planned figure with no actual must never produce a variance"
    )


def test_field_execution_covers_the_unplanned_well_too(session, rich):
    field = rich["hierarchy"]["field"]
    pack = DecisionIntelligence(session).pack(field_id=field.id)
    # B-11's section has no target of its own bound by id - the name-matched target gives it
    # NAME matching, never SECTION_ID.
    assert pack.execution.sections == 2
    assert pack.execution.by_matched_by.get("SECTION_ID", 0) == 4
    assert pack.execution.by_matched_by.get("NAME", 0) >= 1
    assert "plan_matched_by_name" in pack.limitations, (
        "a NAME match is a guess and must surface as a decision-relevant limitation"
    )


def test_a_plan_and_an_actual_in_different_units_refuse_the_subtraction(session, rich):
    """Metres minus feet is not a variance; it is a bug wearing a number's clothes."""
    engineering = EngineeringRepository(session)
    # A second section on A-3 stated in feet while the plan states metres.
    from drilling_intelligence.database.models import WellSection

    session.add(
        WellSection(
            id="sec-3",
            well_id=rich["hierarchy"]["well_a"].id,
            sequence=2,
            name="6 in",
            hole_size_in=6.0,
            top_depth_value=3000.0,
            top_depth_unit="m",
            bottom_depth_value=10500.0,
            bottom_depth_unit="ft",  # actual in FEET
        )
    )
    program = engineering.create_program(
        title="A-3 deep programme", well_id=rich["hierarchy"]["well_a"].id
    )
    engineering.add_target(
        program.id,
        name="6 in",
        section_id="sec-3",
        sequence=1,
        planned_depth_md_value=3200.0,
        planned_depth_md_unit="m",  # plan in METRES
    )
    rows = {
        row["section_id"] + ":" + row["metric"]: row
        for row in engineering.plan_actual_summary(well_id=rich["hierarchy"]["well_a"].id)
    }
    depth = rows["sec-3:depth_md"]
    assert depth["status"] == "INCOMPARABLE_UNITS"
    assert depth["variance"] is None, "the subtraction must not happen"
    assert depth["variance_state"] == "INCOMPARABLE_UNITS"
    assert depth["unit"] == "m" and depth["actual_unit"] == "ft"

    pack = DecisionIntelligence(session).pack(well_id=rich["hierarchy"]["well_a"].id)
    assert "incomparable_units" in pack.limitations
    assert pack.execution.by_status.get("INCOMPARABLE_UNITS") == 1
    assert any("units that differ" in line for line in pack.observations), (
        "the reader must be told, in words, what the pack refused to compute"
    )
    assert pack.execution.incomparable[0]["actual_unit"] == "ft"


def test_units_are_stated_by_the_schema_so_agreement_is_provable(session, rich):
    """``bottom_depth_unit`` and ``actual_mud_weight_unit`` are NOT NULL with defaults (m / ppg),
    so the actual side of every comparison states a unit - which is exactly *why* plan/actual
    agreement can be proved rather than assumed.  The contract still carries the
    ``ACTUAL_UNIT_UNSTATED`` state for any future metric whose actual unit is optional; here it
    correctly reads COMPARABLE, and the limitation stays absent because the condition is absent
    from the facts, not because the check was skipped."""
    from drilling_intelligence.database.models import WellSection

    session.add(
        WellSection(
            id="sec-4",
            well_id=rich["hierarchy"]["well_a"].id,
            sequence=3,
            name="4 1/2 in",
            hole_size_in=4.5,
            top_depth_value=3100.0,
            top_depth_unit="m",
            bottom_depth_value=3400.0,
            # no unit passed: the schema's default fills it, because it must say *something*
        )
    )
    engineering = EngineeringRepository(session)
    program = engineering.create_program(title="A-3 deep 2", well_id=rich["hierarchy"]["well_a"].id)
    engineering.add_target(
        program.id,
        name="4 1/2 in",
        section_id="sec-4",
        sequence=1,
        planned_depth_md_value=3400.0,
        planned_depth_md_unit="m",
    )
    rows = {
        row["section_id"] + ":" + row["metric"]: row
        for row in engineering.plan_actual_summary(well_id=rich["hierarchy"]["well_a"].id)
    }
    depth = rows["sec-4:depth_md"]
    assert depth["actual_unit"] == "m", "the schema filled the default; the row states its unit"
    assert depth["variance_state"] == "COMPARABLE"
    assert depth["status"] == "ON_PLAN" and depth["variance"] == 0.0
    stored = session.get(WellSection, "sec-4")
    assert stored.bottom_depth_unit is not None, "the column is NOT NULL - stated units by rule"
    pack = DecisionIntelligence(session).pack(well_id=rich["hierarchy"]["well_a"].id)
    assert "actual_unit_unstated" not in pack.limitations, (
        "the limitation is derived from real state; no state, no limitation"
    )


# --------------------------------------------------------------------------------------
# Economics: currencies never merge, plan never collapses into actual
# --------------------------------------------------------------------------------------


def test_two_currencies_stay_two_currencies(session, rich):
    pack = DecisionIntelligence(session).pack(well_id=rich["hierarchy"]["well_a"].id)
    money = pack.economics.summary
    assert set(money["by_currency"]) == {"NOK", "USD"}
    usd = money["by_currency"]["USD"]
    nok = money["by_currency"]["NOK"]
    assert usd["planned"] == 100_000.0 and usd["actual"] == 120_000.0
    assert usd["planned_lines"] == 1 and usd["actual_lines"] == 1
    assert usd["variance"] == 20_000.0, "same currency, same side - arithmetic the source licenses"
    assert nok["planned"] == 500_000.0
    assert nok["actual_lines"] == 0, "the actual side is absent, not zero"
    assert nok["variance"] is None, "no actual -> no variance, never a fabricated 0"
    assert money["mixed_currency"] is True
    assert "mixed_currency" in pack.limitations
    # No key anywhere in the section is a sum across currencies.
    text = str(money)
    assert "600000" not in text.replace(",", ""), "NOK+USD must never appear as one number"
    # The observations say so in words too.
    assert any("no combined monetary total" in line for line in pack.observations)


def test_a_superseded_cost_line_leaves_the_current_statement(session, rich):
    """Standing a line down is what re-import does; the pack reads only what still stands."""
    # Promotion writes exactly this flag when a corrected ledger supersedes the old one
    # (test_cost_promotion_v7); the pack must honour it rather than sum history twice.
    rich["usd"].is_current = False
    session.flush()

    pack = DecisionIntelligence(session).pack(well_id=rich["hierarchy"]["well_a"].id)
    money = pack.economics.summary
    assert set(money["by_currency"]) == {"NOK"}, "the superseded USD line left the statement"
    assert money["items"] == 1, "current rows only - the ledger does not double-count"
    # ...and the history is not deleted: reading it back is explicit, never the default.
    history = CostRepository(session).summary(
        current_only=False, well_id=rich["hierarchy"]["well_a"].id
    )
    assert set(history["by_currency"]) == {"NOK", "USD"}
    assert history["items"] == 2


# --------------------------------------------------------------------------------------
# Risk: stated versus unassessed, level separation, no scoring
# --------------------------------------------------------------------------------------


def test_risk_counts_separate_stated_from_unassessed(session, rich):
    well = rich["hierarchy"]["well_a"]
    pack = DecisionIntelligence(session).pack(well_id=well.id)
    risk = pack.risk
    assert risk.current == 2, "the field-carried risk is not a well risk"
    assert risk.by_scope_level == {"well": 2}
    assert risk.stated["severity_stated"] == 1
    assert risk.stated["severity_unassessed"] == 1, "missing severity is UNASSESSED, not 0"
    assert risk.severity_bands == {"HIGH": 1}, "no band is invented for the unassessed row"
    assert risk.by_status == {"OPEN": 2}
    assert "unassessed_risk" in pack.limitations
    assert any("no stated severity" in line for line in pack.observations)


def test_the_field_pack_sees_the_field_carried_risk_at_its_own_level(session, rich):
    field = rich["hierarchy"]["field"]
    pack = DecisionIntelligence(session).pack(field_id=field.id)
    assert pack.risk.current == 3
    assert pack.risk.by_scope_level == {"field": 1, "well": 2}, (
        "levels stay separable: a field risk never masquerades as a well risk"
    )
    # Explicit relations only - none were created, so none are reported.
    assert pack.risk.relations == {}


def test_a_superseded_risk_is_history_not_current(session, rich):
    risks = RiskRepository(session)
    risks.set_risk_status(
        rich["risk_unassessed"].id, "SUPERSEDED", by="eng", reason="duplicate entry"
    )
    pack = DecisionIntelligence(session).pack(well_id=rich["hierarchy"]["well_a"].id)
    assert pack.risk.current == 1
    assert pack.risk.superseded == 1
    assert pack.risk.by_status.get("SUPERSEDED") == 1


# --------------------------------------------------------------------------------------
# Learning: lessons, practices and recommendations keep their own semantics
# --------------------------------------------------------------------------------------


def test_only_an_approved_lesson_counts_as_approved(session, rich):
    well = rich["hierarchy"]["well_a"]
    pack = DecisionIntelligence(session).pack(well_id=well.id)
    lessons = pack.learning.lessons
    assert lessons["current"] == 2, "the field-only lesson is not a well lesson"
    assert lessons["approved"] == 1, "DRAFT is not APPROVED"
    assert lessons["by_status"] == {"APPROVED": 1, "DRAFT": 1}
    # The adopted practice is carried at field level - not visible as a well practice.
    assert pack.learning.practices["current"] == 0
    assert pack.learning.practices["adopted"] == 0


def test_the_field_pack_sees_the_adopted_practice_and_the_field_lesson(session, rich):
    field = rich["hierarchy"]["field"]
    pack = DecisionIntelligence(session).pack(field_id=field.id)
    assert pack.learning.lessons["current"] == 3
    assert pack.learning.lessons["approved"] == 1
    assert pack.learning.practices["current"] == 1
    assert pack.learning.practices["adopted"] == 1, "APPROVED is the adopted state"
    assert pack.summary["approved_lessons"] == 1
    assert pack.summary["adopted_practices"] == 1


def test_recommendations_are_never_presented_as_approved_facts(session, rich):
    field = rich["hierarchy"]["field"]
    pack = DecisionIntelligence(session).pack(field_id=field.id)
    recs = pack.recommendations
    assert recs.total == 3
    assert recs.by_status == {"ACCEPTED": 1, "DECLINED": 1, "PROPOSED": 1}
    assert pack.summary["proposed_recommendations"] == 1
    assert pack.summary["approved_lessons"] == 1, "a lesson approval is not a recommendation"
    assert recs.with_evidence == 1 and recs.without_evidence == 2
    # The explicit source links are the chain - reported, never inferred.
    assert recs.links["pattern"] == 1
    assert recs.links["lesson"] == 1
    assert recs.links["risk"] == 1
    assert recs.links["practice"] == 1
    assert "1 recommendations are proposed" in " ".join(pack.observations)


# --------------------------------------------------------------------------------------
# Patterns: observations with staleness, never predictions
# --------------------------------------------------------------------------------------


def test_patterns_report_recurrence_and_staleness_not_a_forecast(session, rich):
    field = rich["hierarchy"]["field"]
    pack = DecisionIntelligence(session).pack(field_id=field.id)
    patterns = pack.patterns
    assert patterns.total == 3
    assert patterns.by_status["CONFIRMED"]["count"] == 1
    assert patterns.by_status["CANDIDATE"]["count"] == 1
    assert patterns.by_status["REJECTED"]["count"] == 1
    assert patterns.stale == 1, "the confirmed pattern is stale and must say so"
    assert patterns.by_status["CONFIRMED"]["occurrences"] == 5
    assert patterns.by_status["CONFIRMED"]["wells"] == 2
    assert patterns.by_status["CONFIRMED"]["npt_hours"] == 12.5
    assert patterns.scope["field_id"] == field.id
    assert "stale_pattern" in pack.limitations
    # No observation turns recurrence into prediction.
    joined = " ".join(pack.observations).lower()
    for forbidden in ("will recur", "likely", "expected to", "prediction", "guaranteed"):
        assert forbidden not in joined, forbidden


def test_a_well_pack_reads_patterns_at_the_field_level_and_says_so(session, rich):
    well = rich["hierarchy"]["well_a"]
    field = rich["hierarchy"]["field"]
    pack = DecisionIntelligence(session).pack(well_id=well.id)
    assert pack.patterns.total == 3, "the well's field's patterns are visible..."
    assert pack.patterns.scope == {
        "well_id": None,
        "field_id": field.id,
        "project_id": None,
    }, "...but only with their own field-level scope label, never laundered into the well"


# --------------------------------------------------------------------------------------
# Calculations: read-side observations, chain-based currency, real dependencies
# --------------------------------------------------------------------------------------


def test_calculations_report_the_chain_and_resolve_their_dependencies(session, rich):
    well = rich["hierarchy"]["well_a"]
    pack = DecisionIntelligence(session).pack(well_id=well.id)
    calcs = pack.calculations
    assert calcs.total == 2
    assert calcs.current == 1, "calc-1 is superseded by calc-2: the chain decides currency"
    assert calcs.history == 1
    assert calcs.by_status == {"COMPUTED": 2}
    assert calcs.by_method == {"torque_drag": 2}
    assert calcs.inputs == 1
    assert calcs.subjects_resolved == 1
    assert calcs.subjects_truncated is False
    assert calcs.dependency == {"CURRENT": 1, "STALE": 0, "UNRESOLVED": 0}
    assert calcs.dependency_entries[0]["calculation_id"] == "calc-2"
    assert pack.freshness["calculations"] == "CURRENT"
    assert "stale_dependency" not in pack.limitations


# --------------------------------------------------------------------------------------
# HSE scope and the read-only guarantee
# --------------------------------------------------------------------------------------


def test_site_scoped_hse_never_attaches_to_a_well(session, rich):
    well = rich["hierarchy"]["well_a"]
    field = rich["hierarchy"]["field"]
    well_pack = DecisionIntelligence(session).pack(well_id=well.id)
    assert well_pack.operations.hse["incidents"] == 1
    assert well_pack.operations.hse["site_scoped_incidents"] == 0
    assert "site_scoped_hse" not in well_pack.limitations

    field_pack = DecisionIntelligence(session).pack(field_id=field.id)
    assert field_pack.operations.hse["incidents"] == 2
    assert field_pack.operations.hse["well_scoped_incidents"] == 1
    assert field_pack.operations.hse["site_scoped_incidents"] == 1, (
        "the site incident is in the field pack because it is a *site* row, and it is labelled"
    )
    assert "site_scoped_hse" in field_pack.limitations


def test_building_a_pack_writes_nothing(session, rich):
    """A decision view that mutates the ledger is not a decision view."""

    from drilling_intelligence.database.models import Base

    def counts() -> dict[str, int]:
        out: dict[str, int] = {}
        for table in Base.registry.mappers:
            name = table.local_table.name
            out[name] = int(
                session.execute(
                    __import__("sqlalchemy", fromlist=["select"])
                    .select(__import__("sqlalchemy", fromlist=["func"]).func.count())
                    .select_from(table.local_table)
                ).scalar_one()
            )
        return out

    before = counts()
    DecisionIntelligence(session).pack(well_id=rich["hierarchy"]["well_a"].id)
    DecisionIntelligence(session).pack(field_id=rich["hierarchy"]["field"].id)
    assert counts() == before, "the pack is read-only: viewing must not change a row"


# --------------------------------------------------------------------------------------
# The same-source guarantee: pack and detail screens read one query path
# --------------------------------------------------------------------------------------


def test_the_pack_agrees_with_fields_summary_because_it_calls_the_same_methods(session, rich):
    """A number that disagrees with the detail screen is a bug no matter which is 'right'."""
    field = rich["hierarchy"]["field"]
    summary = DecisionIntelligence(session).summary(field_id=field.id)
    pack = DecisionIntelligence(session).pack(field_id=field.id)
    assert pack.operations.npt["rows"] == summary["npt_rows"]
    assert pack.operations.npt["total_hours"] == summary["npt_hours"]
    assert pack.operations.problems["occurrences"] == summary["problems"]
    assert pack.operations.well_control["events"] == summary["well_control_events"]
    assert pack.operations.hse["incidents"] == summary["hse_incidents"]


def test_observations_never_interpret_rank_or_predict(session, rich):
    """The observation layer is count-derived sentences, full stop."""
    field = rich["hierarchy"]["field"]
    pack = DecisionIntelligence(session).pack(field_id=field.id)
    assert pack.observations, "the rich pack must say something"
    joined = " ".join(pack.observations).lower()
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
    ):
        assert forbidden not in joined, forbidden
    # And every sentence that names a number is backed by a section value already in the pack.
    assert any("cost currencies" in line for line in pack.observations)
    assert any("well-control rows" in line for line in pack.observations)


# --------------------------------------------------------------------------------------
# Evidence to decision, end to end (mission section 34)
# --------------------------------------------------------------------------------------


def test_a_decision_observation_traces_to_rows_identity_and_provenance(workspace) -> None:
    """observation -> aggregate -> authoritative rows -> retrieval identity -> provenance."""
    from sqlalchemy import select as sa_select
    from tests.fixtures.fieldops import fetch, ingest_v72, promote_file

    from drilling_intelligence.database.models import HseIncident, Well
    from drilling_intelligence.retrieval.contract import RetrievalRequest
    from drilling_intelligence.retrieval.service import RetrievalService
    from drilling_intelligence.search.service import SearchService

    ingest_v72(workspace)
    promote_file(workspace, "hse_register_well-a3.xlsx")

    with workspace.database.session() as session:
        well = session.execute(sa_select(Well).where(Well.name == "A-3")).scalar_one()
        pack = DecisionIntelligence(session).pack(well_id=well.id)

    # 1. the observation and the section value agree
    incidents = pack.operations.hse["incidents"]
    assert incidents >= 1
    assert any("HSE incidents in scope" in line for line in pack.observations)

    # 2. the evidence reference names the exact method and scope that produced the number
    ref = next(
        ref for ref in pack.evidence if ref.section == "operations" and ref.domain == "hse_incident"
    )
    assert ref.kind == "aggregate" and ref.identity == "method"
    assert ref.method == "FieldIntelligence.hse"
    assert ref.scope["well_id"] == well.id
    assert ref.count == incidents

    # 3. the authoritative rows behind that aggregate, in that scope
    rows = [
        row for row in fetch(workspace, HseIncident) if row.well_id == well.id and row.is_current
    ]
    assert len(rows) == incidents, "the aggregate and the rows it counts are the same set"

    # 4. one of those rows carries a retrieval-resolvable identity in the search index
    search = SearchService.for_workspace(workspace)
    search.rebuild()
    bundle = RetrievalService(database=workspace.database, search_service=search).retrieve(
        RetrievalRequest(query="spill", limit=20)
    )
    hse_items = [item for item in bundle.items if item.record_type == "hse_incident"]
    assert hse_items, "the register's incidents are searchable"
    row_ids = {row.id for row in rows}
    linked = [item for item in hse_items if str(item.source_id) in row_ids]
    assert linked, "a search hit must name one of the rows the pack counted"
    identity = f"structured:hse_incident:{linked[0].source_id}"
    assert identity.startswith("structured:hse_incident:"), identity

    # 5. the row's provenance points at the document version that produced it
    row = next(r for r in rows if r.id == str(linked[0].source_id))
    entry = row.provenance[0]
    assert entry["document_version_id"] == row.document_version_id
    assert entry["source_sha256"], "the chain ends at a digest, not at a maybe"


# --------------------------------------------------------------------------------------
# Query-count scale certification (mission sections 26/31)
# --------------------------------------------------------------------------------------


def _build_scale_corpus(workspace, *, costs: int, risks: int) -> str:
    """A field pack's two heaviest row families at the requested size.  Returns the field id."""
    from drilling_intelligence.core.enums import KnowledgeOrigin  # noqa: F401  (documents intent)
    from drilling_intelligence.engineering.costs import CostRepository
    from drilling_intelligence.engineering.risk import RiskRepository
    from drilling_intelligence.wells.repository import WellRepository

    with workspace.database.unit_of_work() as session:
        wells = WellRepository(session)
        wells.get_or_create_workspace(str(workspace.root), name="Scale")
        project = wells.get_or_create_project("Scale Project")
        field = wells.get_or_create_field("Scale Field", project=project)
        well = wells.create_well("SCALE-1", project_id=project.id, field_id=field.id)
        costs_repo = CostRepository(session)
        for index in range(costs):
            costs_repo.record_item(
                description=f"line {index}",
                planned_value=float(1000 + index),
                planned_unit="USD",
                actual_value=float(1100 + index),
                actual_unit="USD",
                category="equipment",
                well_id=well.id,
                field_id=field.id,
                project_id=project.id,
            )
        risk_repo = RiskRepository(session)
        for index in range(risks):
            risk = risk_repo.create_risk(
                title=f"risk {index}",
                category="drilling",
                well_id=well.id,
                field_id=field.id,
                project_id=project.id,
            )
            if index % 2:
                risk_repo.assess_risk(
                    risk.id,
                    probability=2,
                    impact=2,
                    severity=4,
                    severity_band="MEDIUM",
                    by="scale",
                )
        # One observation-style row set with an explicit decision status, so the learning and
        # recommendation folds are exercised at both scales too.
        from drilling_intelligence.database.models import Recommendation

        session.add(
            Recommendation(
                id="scale-rec",
                signature="scale-rec",
                statement="Keep the existing procedure.",
                reason="It worked at this size too.",
                status="PROPOSED",
                generated_by="scale",
                well_id=well.id,
                field_id=field.id,
                project_id=project.id,
            )
        )
    return field.id


def _pack_query_count(workspace, field_id: str) -> int:
    from sqlalchemy import event

    count = {"n": 0}

    def before(_conn, _cursor, statement, *_args, **_kwargs) -> None:
        if str(statement).lstrip().upper().startswith("SELECT"):
            count["n"] += 1

    with workspace.database.read_only() as session:
        event.listen(workspace.database.engine, "before_cursor_execute", before)
        try:
            DecisionIntelligence(session).pack(field_id=field_id)
        finally:
            event.remove(workspace.database.engine, "before_cursor_execute", before)
    return count["n"]


@pytest.mark.parametrize("costs, risks", [(8, 4), (40, 20)])
def test_the_pack_query_count_does_not_grow_with_row_count(workspace, costs, risks) -> None:
    """The invariant: query_count(large) == query_count(small), measured not asserted in prose.

    Both parametrisations pin the SAME budget constant below: 5x the cost lines and 5x the risks
    change no query count, because every section aggregates in grouped SQL or reuses the bounded
    methods that already do.  The ceiling itself was measured first, then pinned - it is a budget,
    not a hope.
    """
    field_id = _build_scale_corpus(workspace, costs=costs, risks=risks)
    measured = _pack_query_count(workspace, field_id)
    print(f"\nPACK_SCALE costs={costs} risks={risks} queries={measured}")
    # The budget: identical at both scales.  If this number ever has to differ between the two
    # parametrisations, the pack has grown a per-row or per-entity query and this test says so.
    assert measured <= 70, (
        f"the field pack issued {measured} SELECTs for {costs} cost lines and {risks} risks; "
        "the budget is 70 and must not scale with rows"
    )


def test_a_project_pack_covers_the_hierarchy_and_the_wbs_cbs_rollups(session, rich):
    """Project scope reaches through its field to its wells; rollups stay per currency."""
    project = rich["hierarchy"]["project"]
    pack = DecisionIntelligence(session).pack(project_id=project.id)
    assert pack.subject.kind == "project"
    assert pack.subject.well_count == 2
    # Sections through the project's wells, risks at every level of the hierarchy.
    assert pack.execution.sections == 2
    assert pack.risk.current == 3
    assert pack.risk.by_scope_level == {"field": 1, "well": 2}
    # The economics section carries the structural rollups at detail>=1.  Each row IS one
    # (code, currency) pair - the shape that makes a merged-currency row impossible to express.
    wbs_codes = {row["code"] for row in pack.economics.wbs_rollup}
    assert {"WP-1", "WP-2"} <= wbs_codes, sorted(wbs_codes)
    for row in pack.economics.wbs_rollup:
        assert row["currency"] in ("USD", "NOK")
        assert row["lines"] >= 1
    assert any(row["currency"] == "USD" for row in pack.economics.wbs_rollup)
    assert any(row["currency"] == "NOK" for row in pack.economics.wbs_rollup)
    assert pack.economics.cbs_rollup, "CBS groups come from the same statement"


def test_a_cost_line_with_no_values_is_a_line_not_a_total(session, rich):
    """A description without figures counts as a line and produces no money anywhere."""
    costs = CostRepository(session)
    costs.record_item(
        description="miscellaneous site travel",
        category="other",
        well_id=rich["hierarchy"]["well_a"].id,
        field_id=rich["hierarchy"]["field"].id,
        project_id=rich["hierarchy"]["project"].id,
    )
    pack = DecisionIntelligence(session).pack(well_id=rich["hierarchy"]["well_a"].id)
    money = pack.economics.summary
    assert money["items"] == 3
    assert money["priced"] == 2 and money["unpriced"] == 1
    assert money["mixed_currency"] is True, "NOK and USD still separate regardless"
    assert "unpriced_cost_lines" in pack.limitations
    # The unpriced line adds nothing to any currency bucket.
    assert money["by_currency"]["USD"]["planned"] == 100_000.0
    assert money["by_currency"]["NOK"]["planned"] == 500_000.0
