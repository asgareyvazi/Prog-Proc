"""Shared fixtures for the V7.6 integration suites: the golden comparison world.

The V7.2 golden corpus (A-3 drilled, B-11 partial, site HSE that belongs to no well, an open
knowledge conflict) enriched with costs in two currencies, risks, learning, patterns,
recommendations, calculations, and a third bare well C-17 - the A-3/B-11/C-17 scenario both
the comparison and analyst suites run against.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from tests.fixtures.fieldops import (
    add_casing_program,
    field_id,
    ingest_v72,
    promote,
    well_id_for,
)

from drilling_intelligence.core.enums import (
    KnowledgeOrigin,
    LessonLifecycle,
    ProcedureLifecycle,
    RecommendationLifecycle,
)
from drilling_intelligence.database.models import (
    BestPractice,
    Calculation,
    CalculationInput,
    Field,
    FieldPattern,
    Recommendation,
)
from drilling_intelligence.engineering.costs import CostRepository
from drilling_intelligence.engineering.risk import RiskRepository
from drilling_intelligence.lessons.repository import LessonRepository
from drilling_intelligence.wells.repository import WellRepository


@pytest.fixture
def corpus(workspace):
    """The V7.2 golden corpus: A-3 drilled with NPT/problems/well-control/HSE, B-11 partial,
    site HSE rows that belong to no well, an open knowledge conflict, and a plan/actual
    programme on A-3."""
    ingest_v72(workspace)
    promote(workspace)
    add_casing_program(workspace)
    return workspace


@pytest.fixture
def golden(corpus, session):
    """The corpus plus costs in two currencies, risks, learning, patterns, recommendations,
    calculations and a third bare well C-17 - the A-3/B-11/C-17 comparison scenario."""
    workspace = corpus
    wells = WellRepository(session)
    fid = field_id(workspace)
    field_row = session.get(Field, fid)
    a3 = well_id_for(workspace, "A-3")
    b11 = well_id_for(workspace, "B-11")
    project_id = field_row.project_id
    c17 = wells.create_well("C-17", project_id=project_id, field_id=fid)

    # -- economics: A-3 USD planned+actual and NOK planned-only; B-11 NOK planned+actual --
    costs = CostRepository(session)
    usd, _ = costs.record_item(
        description="mud motor rental",
        planned_value=100_000.0,
        planned_unit="USD",
        actual_value=120_000.0,
        actual_unit="USD",
        category="equipment",
        well_id=a3,
        field_id=fid,
        project_id=project_id,
        provenance=[{"sheet": "Costs", "cell": "C4"}],
    )
    nok_a3, _ = costs.record_item(
        description="platform day rate",
        planned_value=500_000.0,
        planned_unit="NOK",
        actual_value=None,
        actual_unit="NOK",
        category="services",
        well_id=a3,
        field_id=fid,
        project_id=project_id,
        provenance=[{"sheet": "Costs", "cell": "C5"}],
    )
    nok_b11, _ = costs.record_item(
        description="casing haulage",
        planned_value=75_000.0,
        planned_unit="NOK",
        actual_value=80_000.0,
        actual_unit="NOK",
        category="services",
        well_id=b11,
        field_id=fid,
        project_id=project_id,
        provenance=[{"sheet": "Costs", "cell": "C6"}],
    )

    # -- risk: two on A-3 (one assessed, one unassessed); nothing on B-11 or C-17 --------
    risks = RiskRepository(session)
    r1 = risks.create_risk(
        title="Shallow gas while drilling the 12 1/4 in",
        category="well_integrity",
        well_id=a3,
        field_id=fid,
        project_id=project_id,
    )
    risks.assess_risk(r1.id, probability=3, impact=4, severity=7, severity_band="HIGH", by="eng")
    r2 = risks.create_risk(
        title="Stuck pipe in the 8 1/2 in",
        category="drilling",
        well_id=a3,
        field_id=fid,
        project_id=project_id,
    )

    # -- learning: well-scoped rows so well subjects can hold them ------------------------
    lessons = LessonRepository(session)
    approved = lessons.capture(
        lesson="Tag the casing shoe before entering the reservoir.",
        title="Shoe tagging",
        well_id=a3,
        field_id=fid,
        project_id=project_id,
    )
    approved.status = str(LessonLifecycle.APPROVED)
    draft = lessons.capture(
        lesson="Rotate early in the sandstone to avoid sticking.",
        title="Rotation practice",
        well_id=b11,
        field_id=fid,
        project_id=project_id,
    )
    practice = BestPractice(
        id="bp-cmp-1",
        title="Pre-job BHA review",
        practice_type="review",
        statement="Run a documented BHA review before every section.",
        rationale="Two wells lost time to an avoidable BHA clash.",
        revision=1,
        is_current=True,
        status=str(ProcedureLifecycle.APPROVED),
        well_id=a3,
        field_id=fid,
        project_id=project_id,
        origin=KnowledgeOrigin.MANUAL.value,
        created_by="eng",
    )
    session.add(practice)

    # -- patterns: one confirmed and stale (field-scoped, both A-3 and B-11 read it) ------
    session.add_all(
        [
            FieldPattern(
                id="pat-cmp-1",
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
                field_id=fid,
                project_id=project_id,
                evidence=[{"id": "structured:problem_occurrence:po-1"}],
                well_ids=[a3],
                detected_by="test",
                computed_at=datetime(2026, 5, 1, tzinfo=UTC),
            ),
            FieldPattern(
                id="pat-cmp-2",
                signature="washout|12.25",
                problem_type="washout",
                depth_from_unit="m",
                depth_to_unit="m",
                occurrence_count=2,
                event_count=2,
                well_count=1,
                status="CANDIDATE",
                field_id=fid,
                project_id=project_id,
                detected_by="test",
                computed_at=datetime(2026, 5, 1, tzinfo=UTC),
            ),
        ]
    )

    # -- recommendations: one well-scoped each way ---------------------------------------
    session.add_all(
        [
            Recommendation(
                id="rec-cmp-1",
                signature="add-logging-while-drilling",
                statement="Add LWD while drilling the next exploration well.",
                reason="The last two wells needed a wireline run that cost a trip.",
                status=str(RecommendationLifecycle.PROPOSED),
                generated_by="pattern-snapshot",
                well_id=a3,
                field_id=fid,
                project_id=project_id,
                pattern_id="pat-cmp-1",
                lesson_id=approved.id,
                risk_id=r1.id,
            ),
            Recommendation(
                id="rec-cmp-2",
                signature="pre-job-bha-review",
                statement="Adopt the pre-job BHA review checklist.",
                reason="Two wells lost time to an avoidable BHA clash.",
                status=str(RecommendationLifecycle.ACCEPTED),
                generated_by="pattern-snapshot",
                decided_by="ops-manager",
                well_id=b11,
                field_id=fid,
                project_id=project_id,
            ),
        ]
    )

    # -- calculations: the revision chain on A-3 ------------------------------------------
    c1 = Calculation(
        id="calc-cmp-1",
        method_id="torque_drag",
        method_version="1.0",
        calculation_type="mechanics",
        record_state="CURRENT",
        well_id=a3,
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
        id="calc-cmp-2",
        method_id="torque_drag",
        method_version="1.1",
        calculation_type="mechanics",
        record_state="CURRENT",
        well_id=a3,
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
    from drilling_intelligence.core.ids import subject_key

    resolved_key = subject_key(well_id=a3, property_name="mud_weight", record_state="ACTUAL")
    session.add(
        CalculationInput(
            id="cin-cmp-1",
            calculation_id=c2.id,
            name="mud_weight",
            value=11.9,
            unit="ppg",
            dimension="density",
            source_kind="well",
            subject_key=resolved_key,
            subject_kind="well",
            subject_id=str(a3),
        )
    )
    session.flush()

    return {
        "workspace": workspace,
        "field_id": fid,
        "project_id": project_id,
        "a3": a3,
        "b11": b11,
        "c17": str(c17.id),
        "usd": usd,
        "nok_a3": nok_a3,
        "nok_b11": nok_b11,
        "risk_assessed": r1,
        "risk_unassessed": r2,
        "lesson_approved": approved,
        "lesson_draft": draft,
        "practice": practice,
    }
