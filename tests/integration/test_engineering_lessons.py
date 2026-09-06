"""Tests for the engineering and lessons layers: procedures, programs, risks, lessons, practices.

These run against a real SQLite database built from the models (``session`` fixture), with a real
hierarchy created through :class:`~drilling_intelligence.wells.repository.WellRepository`.  Nothing here
is mocked: a procedure revision, a risk score and a lesson approval are exactly the kind of thing that
breaks when a test substitutes an object that cannot disagree with itself.
"""

from __future__ import annotations

import pytest
from sqlalchemy.exc import IntegrityError

from drilling_intelligence.core.enums import (
    ProcedureLifecycle,
    ProgramLifecycle,
    RecommendationLifecycle,
    RiskLifecycle,
    SeverityLevel,
)
from drilling_intelligence.core.errors import ValidationError
from drilling_intelligence.database.integrity import check_knowledge_relations
from drilling_intelligence.database.models import ProcedureRecord
from drilling_intelligence.database.serialize import record_to_dict
from drilling_intelligence.engineering.repository import EngineeringRepository
from drilling_intelligence.engineering.risk import DEFAULT_SCALE, RiskRepository
from drilling_intelligence.lessons.repository import LessonRepository
from drilling_intelligence.wells.repository import WellRepository


@pytest.fixture
def hierarchy(session):
    """A field with two wells, one of which has a drilled section."""
    from datetime import UTC, datetime

    from drilling_intelligence.database.models import WellSection

    repository = WellRepository(session)
    project = repository.get_or_create_project("Cormorant Block")
    field = repository.get_or_create_field("North Cormorant", project=project)
    well_a = repository.create_well("A-3", project_id=project.id, field_id=field.id)
    well_b = repository.create_well("B-11", project_id=project.id, field_id=field.id)
    section = WellSection(
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
        notes="drilled as planned apart from two NPT runs",
    )
    other = WellSection(
        id="sec-2",
        well_id=well_b.id,
        sequence=1,
        name="12 1/4 in",
        hole_size_in=12.25,
        top_depth_value=1200.0,
        top_depth_unit="m",
    )
    section.created_at = datetime.now(UTC)
    session.add_all([section, other])
    session.flush()
    return {
        "project": project,
        "field": field,
        "well_a": well_a,
        "well_b": well_b,
        "section": section,
        "other_section": other,
        "session": session,
    }


def _provenance(sheet: str = "Program", cell: str = "B4") -> list[dict]:
    return [
        {
            "kind": "spreadsheet",
            "document": {"sheet": sheet, "cell": cell, "page": 1},
            "excerpt": "value read from the cell",
            "method": "extractor",
        }
    ]


# -- procedures and programs -------------------------------------------------
def test_a_revision_supersedes_rather_than_edits(session, hierarchy) -> None:
    repository = EngineeringRepository(session)
    procedure = repository.create_procedure(
        code="NCF-DRILL-01",
        title="Hole cleaning in the 8 1/2 in section",
        procedure_type="hole_cleaning",
        description="Two bottoms-up circulations before tripping.",
        field_id=hierarchy["field"].id,
        provenance=_provenance(),
        created_by="k.adeyemi",
    )
    assert procedure.revision == 1
    assert procedure.is_current is True
    assert procedure.status == str(ProcedureLifecycle.DRAFT)
    stored = record_to_dict(procedure)
    assert stored["provenance"], "a procedure written from a document must carry its provenance"

    revised = repository.revise_procedure(
        procedure.id,
        by="j.rivera",
        changes={"description": "Three bottoms-up circulations, and a sweep at the shoe."},
        revision_label="Rev B",
    )

    assert revised.revision == 2
    assert revised.is_current is True
    # Copied from rev 1, because a revision that changes one line is still the same procedure.
    assert revised.code == procedure.code
    assert revised.title == procedure.title
    assert revised.procedure_type == procedure.procedure_type
    assert revised.field_id == procedure.field_id
    assert revised.provenance == procedure.provenance
    assert revised.description != procedure.description
    # And rev 1 is still readable, which is the whole point of not editing in place.
    assert procedure.is_current is False
    assert procedure.description == "Two bottoms-up circulations before tripping."
    assert revised.status == str(ProcedureLifecycle.DRAFT), "a changed procedure is not approved"

    chain = repository.revision_chain(revised.id)
    assert [row.revision for row in chain] == [1, 2]
    assert repository.current_procedure("NCF-DRILL-01").id == revised.id
    assert [row.id for row in repository.list_procedures(field_id=hierarchy["field"].id)] == [
        revised.id
    ], "a superseded revision is out of the default list"
    both = repository.list_procedures(field_id=hierarchy["field"].id, include_superseded=True)
    assert [row.revision for row in both] == [2, 1]


def test_an_approved_procedure_that_is_revised_loses_its_approval_only_on_the_old_row(
    session, hierarchy
) -> None:
    repository = EngineeringRepository(session)
    procedure = repository.create_procedure(title="Casing running", code="NCF-CASE-03")
    repository.approve_procedure(procedure.id, by="d.okafor", note="as agreed at AAR")
    assert procedure.status == str(ProcedureLifecycle.APPROVED)
    assert procedure.approved_by == "d.okafor"
    assert procedure.approved_at is not None

    revised = repository.revise_procedure(
        procedure.id, by="d.okafor", changes={"title": "Casing running (revised)"}
    )
    assert revised.status == str(ProcedureLifecycle.DRAFT)
    assert revised.approved_by is None, "an approval of rev 1 says nothing about rev 2's text"
    assert procedure.status == str(ProcedureLifecycle.SUPERSEDED)


def test_an_approval_needs_a_person_and_a_legal_transition(session, hierarchy) -> None:
    repository = EngineeringRepository(session)
    procedure = repository.create_procedure(title="Stand-off, what it means")
    with pytest.raises(ValidationError, match="without an author"):
        repository.set_procedure_status(
            procedure.id, ProcedureLifecycle.APPROVED, by="", reason="because"
        )
    repository.set_procedure_status(procedure.id, ProcedureLifecycle.IN_REVIEW, by="a.novak")
    # Back to draft is legal - that is what sending something for rework means.
    repository.set_procedure_status(
        procedure.id, ProcedureLifecycle.DRAFT, by="a.novak", reason="rework"
    )
    repository.approve_procedure(procedure.id, by="d.okafor", note="ok")
    with pytest.raises(
        ValidationError, match="illegal procedure transition APPROVED -> DRAFT"
    ) as caught:
        repository.set_procedure_status(procedure.id, ProcedureLifecycle.DRAFT, by="a.novak")
    # The refusal says what *is* possible, which is the difference between an error and a dead end.
    assert caught.value.context["allowed"] == ["SUPERSEDED", "WITHDRAWN"]
    stored = repository.get_procedure(procedure.id)
    assert stored.status == str(ProcedureLifecycle.APPROVED)
    assert "DRAFT->IN_REVIEW" in str(stored.status_note)
    assert "IN_REVIEW->DRAFT" in str(stored.status_note)
    assert "APPROVED->DRAFT" not in str(stored.status_note), (
        "a refused transition never reaches the record"
    )


def test_a_procedure_can_only_cite_a_version_that_exists(session, hierarchy) -> None:
    from sqlalchemy import select

    from drilling_intelligence.database.models import Document, DocumentVersion, KnowledgeRelation

    repository = EngineeringRepository(session)
    procedure = repository.create_procedure(title="Bit handling", code="NCF-BIT-07")
    with pytest.raises(ValidationError, match="no document"):
        repository.reference_procedure(procedure.id, standard_document_ids=["doc-missing"])

    # A real document row with a real version, written the way the document registry writes them.
    document = Document(
        id="doc-api-59",
        identity_path="standards/api-rp-59.pdf",
        filename="api-rp-59.pdf",
        sha256="0" * 64,
    )
    version = DocumentVersion(
        id="ver-api-59",
        document_id=document.id,
        version_number=1,
        source_path="standards/api-rp-59.pdf",
        sha256="1" * 64,
        is_current=True,
    )
    session.add_all([document, version])
    document.current_version_id = version.id
    session.flush()
    bare = Document(
        id="doc-no-version",
        identity_path="standards/api-rp-13.pdf",
        filename="api-rp-13.pdf",
        sha256="2" * 64,
    )
    session.add(bare)
    session.flush()
    with pytest.raises(ValidationError, match="has no current version"):
        repository.reference_procedure(procedure.id, standard_document_ids=["doc-no-version"])
    counts = repository.reference_procedure(
        procedure.id,
        standard_document_ids=[document.id],
        offset_well_ids=[hierarchy["well_b"].id],
    )
    assert counts == {"PROCEDURE_CITES_STANDARD": 1, "PROCEDURE_OBSERVES_WELL": 1}
    edges = list(
        session.execute(
            select(KnowledgeRelation)
            .where(
                KnowledgeRelation.source_type == "procedure",
                KnowledgeRelation.source_id == procedure.id,
            )
            .order_by(KnowledgeRelation.relation)
        ).scalars()
    )
    assert [edge.target_type for edge in edges] == ["document_version", "well"]
    assert edges[0].target_id == version.id, (
        "an edge must point at the revision that was read, not at the file"
    )
    assert not check_knowledge_relations(session), "the edges must be resolvable"


def test_a_program_and_its_targets_carry_provenance_and_a_chain(session, hierarchy) -> None:
    repository = EngineeringRepository(session)
    program = repository.create_program(
        code="NCF-A3-PROG",
        title="A-3 12 1/4 in section programme",
        summary="TD 9850 m MD, mud 11.4 ppg, 12 days.",
        well_id=hierarchy["well_a"].id,
        provenance=_provenance(sheet="Program", cell="C2"),
    )
    assert program.status == str(ProgramLifecycle.DRAFT)
    assert program.revision == 1
    target = repository.add_target(
        program.id,
        name="8 1/2 in",
        section_id=hierarchy["section"].id,
        sequence=1,
        planned_depth_md_value=9850.0,
        planned_depth_md_unit="m",
        planned_duration_days=12.0,
        planned_mud_weight_value=11.4,
        planned_mud_weight_unit="ppg",
        planned_npt_hours=6.0,
        provenance=_provenance(sheet="Targets", cell="D7"),
    )
    assert target.provenance
    assert repository.list_targets(program.id) == [target]

    with pytest.raises(ValidationError, match="no field named planned_cost_hours"):
        repository.add_target(program.id, planned_cost_hours=3)
    with pytest.raises(ValidationError, match="belongs to another well"):
        repository.add_target(
            program.id, name="B-11 top hole", section_id=hierarchy["other_section"].id
        )

    revised = repository.revise_program(program.id, by="e.vaziri", revision_label="Rev 2")
    assert revised.revision == 2
    assert revised.status == str(ProgramLifecycle.DRAFT)
    assert program.status == str(ProgramLifecycle.SUPERSEDED)
    copies = repository.list_targets(revised.id)
    assert len(copies) == 1, "a revision carries its targets across"
    assert copies[0].id != target.id
    assert copies[0].planned_depth_md_value == 9850.0
    assert copies[0].provenance == target.provenance
    # Rev 1's own target is untouched, which is what "what did we plan at the time" means.
    assert repository.list_targets(program.id) == [target]
    assert repository.programs_for_well(hierarchy["well_a"].id) == [revised]


def test_a_well_sees_its_own_procedures_before_its_fields(session, hierarchy) -> None:
    repository = EngineeringRepository(session)
    field_wide = repository.create_procedure(
        title="Lost circulation material, how we spot it", field_id=hierarchy["field"].id
    )
    well_specific = repository.create_procedure(
        title="A-3 liner hanger sequence", well_id=hierarchy["well_a"].id
    )
    only_well = repository.procedures_for_well(hierarchy["well_a"].id, include_field=False)
    assert [row.id for row in only_well] == [well_specific.id]
    governing = repository.procedures_for_well(hierarchy["well_a"].id)
    assert {row.id for row in governing} == {well_specific.id, field_wide.id}
    assert repository.list_procedures(
        well_id=hierarchy["well_a"].id, field_id=hierarchy["field"].id
    )


# -- plan versus actual -------------------------------------------------------
def test_plan_versus_actual_never_invents_a_zero(session, hierarchy) -> None:
    repository = EngineeringRepository(session)
    program = repository.create_program(title="A-3 programme", well_id=hierarchy["well_a"].id)
    repository.add_target(
        program.id,
        name="8 1/2 in",
        section_id=hierarchy["section"].id,
        sequence=1,
        planned_depth_md_value=9850.0,
        planned_duration_days=14.5,  # the actual, so this metric is ON_PLAN
        planned_mud_weight_value=11.4,
        planned_mud_weight_unit="ppg",
        # no planned_npt_hours at all
    )
    rows = {
        row["metric"]: row for row in repository.plan_actual_summary(well_id=hierarchy["well_a"].id)
    }

    assert rows["depth_md"]["status"] == "ON_PLAN"
    assert rows["depth_md"]["variance"] == 0.0
    assert rows["depth_md"]["planned"] == 9850.0
    assert rows["duration_days"]["status"] == "ON_PLAN"
    assert rows["mud_weight"]["status"] == "VARIANCE"
    assert rows["mud_weight"]["planned"] == 11.4
    assert rows["mud_weight"]["actual"] == 11.9
    assert rows["mud_weight"]["variance"] == pytest.approx(0.5)
    assert rows["mud_weight"]["unit"] == "ppg", "the unit the plan was written in"
    npt = rows["npt_hours"]
    assert npt["status"] == "NO_PLAN", "a program that said nothing cannot be reported as on plan"
    assert npt["planned"] is None and npt["variance"] is None

    # B-11 has a section but no program, so every metric of it is unplanned - and the section's own
    # numbers are still reported rather than replaced with zeros.
    other = {
        row["metric"]: row for row in repository.plan_actual_summary(well_id=hierarchy["well_b"].id)
    }
    assert set(other) == {"depth_md", "duration_days", "mud_weight", "npt_hours"}
    assert {row["status"] for row in other.values()} == {"NO_TARGET"}
    assert all(row["planned"] is None and row["variance"] is None for row in other.values())
    assert other["depth_md"]["actual"] is None, "this section was never drilled to a recorded depth"
    with pytest.raises(ValidationError, match="needs a scope"):
        repository.plan_actual_summary()


def test_a_section_without_a_program_is_reported_as_unplanned(session, hierarchy) -> None:
    repository = EngineeringRepository(session)
    rows = repository.plan_actual_summary(well_id=hierarchy["well_a"].id)
    assert rows, "the section exists"
    assert {row["status"] for row in rows} == {"NO_TARGET"}, (
        "every metric of an unplanned section is unplanned, not zero"
    )
    assert all(row["planned"] is None for row in rows)
    assert all(row["program_id"] is None for row in rows)
    assert all(row["actual"] is not None for row in rows if row["metric"] != "npt_hours")


# -- risks -------------------------------------------------------------------
def test_an_assessment_is_stored_as_stated_and_nothing_is_invented(session, hierarchy) -> None:
    repository = RiskRepository(session)
    risk = repository.create_risk(
        title="Shallow gas below the 30 in conductor",
        code="NCF-R-01",
        category="shallow gas",
        field_id=hierarchy["field"].id,
        probability=4,
        impact=5,
        severity=20,
        severity_band="critical",
        causes=["unmapped sands charged from the shallower gas leg"],
        consequences=["flow check on the rig floor"],
        mitigation="Gap log before the conductor is driven; rig on standby for two hours.",
        owner="d.okafor",
        provenance=_provenance(),
    )
    assert risk.severity == 20
    assert risk.severity_band == str(SeverityLevel.CRITICAL)
    assert risk.scale == DEFAULT_SCALE, "the row says which grid its numbers are on"
    assert risk.status == str(RiskLifecycle.OPEN)
    assert risk.causes == ["unmapped sands charged from the shallower gas leg"]
    assert risk.consequences == ["flow check on the rig floor"]
    assert risk.mitigation.startswith("Gap log"), "mitigation is prose, kept as written"
    assert risk.description == ""

    # The two axes without a product: what a source that never gave one looks like.
    axes = repository.create_risk(title="Pump capacity", probability=3, impact=2)
    assert (axes.probability, axes.impact) == (3, 2)
    assert axes.severity is None, "no severity was stated, so none is claimed"
    assert axes.severity_band is None
    unassessed = repository.create_risk(title="Third-party rig move")
    assert (unassessed.probability, unassessed.impact, unassessed.severity) == (None, None, None)

    with pytest.raises(ValidationError, match="impact must be between 1 and 5"):
        repository.create_risk(title="Out of range", impact=9)
    with pytest.raises(ValidationError, match="probability must be a whole number"):
        repository.create_risk(title="Half a number", probability="high")
    with pytest.raises(ValidationError, match="belongs in causes/consequences"):
        repository.create_risk(title="Wrong shape", mitigation=["a gap log", "a standby rig"])
    with pytest.raises(ValidationError, match="revision belongs to the chain"):
        repository.update_risk(risk.id, revision=4)

    repository.assess_risk(risk.id, probability=2, by="d.okafor", note="seismic re-tie")
    session.refresh(risk)
    assert risk.probability == 2
    assert risk.impact == 5, "the axis nobody re-stated keeps its value"
    assert risk.severity == 20, "re-scoring one axis does not re-derive the other number"
    history = risk.attributes["assessments"]
    assert len(history) == 1
    assert history[0]["by"] == "d.okafor"
    assert history[0]["note"] == "seismic re-tie"
    assert history[0]["severity"] == 20

    with pytest.raises(ValidationError, match="needs an author"):
        repository.assess_risk(risk.id, impact=4)


def test_a_band_word_is_stored_and_an_unknown_one_is_refused(session, hierarchy) -> None:
    repository = RiskRepository(session)
    # "severe" is how a report says it; "HIGH" is how the register stores it.  One alias hop, no
    # arithmetic, and a word that means nothing is refused instead of filed under "we guessed".
    risk = repository.create_risk(title="Well control margin", severity_band="severe", severity=12)
    assert risk.severity_band == str(SeverityLevel.HIGH)
    assert risk.severity == 12
    with pytest.raises(ValidationError, match="cannot read a severity band out of"):
        repository.create_risk(title="Quite bad", severity_band="quite bad")
    with pytest.raises(ValidationError, match="cannot read a severity band out of"):
        repository.assess_risk(risk.id, severity_band="catastrophic-ish", by="d.okafor")
    unscored = repository.create_risk(title="No assessment yet", field_id=hierarchy["field"].id)
    listed = repository.list_risks(field_id=hierarchy["field"].id, unscored_only=True)
    assert [row.id for row in listed] == [unscored.id]


def test_the_register_counts_what_is_open_and_says_what_is_unscored(session, hierarchy) -> None:
    repository = RiskRepository(session)
    repository.create_risk(
        title="R-1",
        field_id=hierarchy["field"].id,
        probability=5,
        impact=5,
        severity=25,
        severity_band="critical",
    )
    repository.create_risk(
        title="R-2", field_id=hierarchy["field"].id, severity=2, severity_band="low"
    )
    repository.create_risk(title="R-3", field_id=hierarchy["field"].id)
    closed = repository.create_risk(
        title="R-4", field_id=hierarchy["field"].id, severity=20, severity_band="high"
    )
    repository.set_risk_status(
        closed.id, RiskLifecycle.MITIGATED, by="d.okafor", reason="in the programme"
    )
    repository.set_risk_status(
        closed.id, RiskLifecycle.CLOSED, by="d.okafor", reason="well completed"
    )
    open_risk = repository.create_risk(
        title="R-5", field_id=hierarchy["field"].id, probability=4, impact=4
    )
    with pytest.raises(ValidationError, match="needs a reason"):
        repository.set_risk_status(open_risk.id, RiskLifecycle.CLOSED, by="d.okafor")
    with pytest.raises(ValidationError, match="without an author"):
        repository.set_risk_status(
            open_risk.id, RiskLifecycle.CLOSED, reason="the well was completed"
        )

    register = repository.register(field_id=hierarchy["field"].id)
    assert register["open_count"] == 4
    assert register["by_band"] == {str(SeverityLevel.CRITICAL): 1, str(SeverityLevel.LOW): 1}
    assert register["unscored"] == 2, "an unscored risk is a gap in the work, not a low one"
    assert register["with_owner"] == 0
    assert register["highest"][0]["title"] == "R-1"
    assert register["highest"][0]["severity"] == 25
    assert register["by_category"]["uncategorised"] == 4
    # A closed risk is out of the open register but stays readable in the list.
    assert closed.id not in {row["id"] for row in register["highest"]}
    assert closed.id in {row.id for row in repository.list_risks(field_id=hierarchy["field"].id)}


def test_risk_evidence_and_control_edges_are_real(session, hierarchy) -> None:
    from datetime import UTC, datetime

    from sqlalchemy import select

    from drilling_intelligence.core.enums import CauseStatus, ConfirmationStatus
    from drilling_intelligence.database.models import (
        KnowledgeRelation,
        NptRecord,
        ProblemOccurrence,
    )

    repository = RiskRepository(session)
    risk = repository.create_risk(title="Stuck pipe after a long trip", category="stuck pipe")
    with pytest.raises(ValidationError, match="no npt record"):
        repository.cite_evidence(risk.id, npt_ids=["npt-missing"])
    with pytest.raises(ValidationError, match="no procedure"):
        repository.link_procedure(risk.id, "proc-missing")
    with pytest.raises(ValidationError, match="a realised risk needs"):
        repository.affects_activity(risk.id)

    # The evidence is a real pair of rows, shaped the way the promoter would have left them.
    now = datetime.now(UTC)
    npt = NptRecord(
        id="npt-evidence",
        well_id=hierarchy["well_a"].id,
        category="stuck_pipe",
        description="Back reaming to free the bit",
        started_at=now,
        duration_hours=6.5,
        duration_basis="STATED",
        root_cause_status=str(CauseStatus.UNKNOWN),
        immediate_cause_status=str(CauseStatus.KNOWN),
        status=str(ConfirmationStatus.CONFIRMED),
        origin="DERIVED",
        created_by="promoter",
    )
    problem = ProblemOccurrence(
        id="problem-evidence",
        well_id=hierarchy["well_a"].id,
        npt_id=npt.id,
        code="NPT-STUCK",
        problem_type="stuck_pipe",
        description="Bit held the formation after a long trip",
        occurred_at=now,
        immediate_cause="differential",
        immediate_cause_status=str(CauseStatus.KNOWN),
        root_cause_status=str(CauseStatus.UNKNOWN),
        status=str(ConfirmationStatus.CANDIDATE),
        origin="DERIVED",
        created_by="promoter",
    )
    session.add_all([npt, problem])
    session.flush()

    assert repository.cite_evidence(risk.id, npt_ids=[npt.id], problem_ids=[problem.id]) == 2
    repository.derive_from_problem(risk.id, problem.id)
    engineering = EngineeringRepository(session)
    procedure = engineering.create_procedure(title="Short trips in reactive shales")
    repository.link_procedure(risk.id, procedure.id, note="mitigated by rev 1")
    repository.affects_activity(risk.id, npt_id=npt.id, note="realised on A-3")

    edges = list(
        session.execute(
            select(KnowledgeRelation)
            .where(KnowledgeRelation.source_type == "risk", KnowledgeRelation.source_id == risk.id)
            .order_by(KnowledgeRelation.relation, KnowledgeRelation.target_id)
        ).scalars()
    )
    assert [(edge.relation, edge.target_type, edge.target_id) for edge in edges] == [
        ("RISK_AFFECTS_ACTIVITY", "npt_record", npt.id),
        ("RISK_CITES_EVIDENCE", "npt_record", npt.id),
        ("RISK_CITES_EVIDENCE", "problem_occurrence", problem.id),
        ("RISK_DERIVED_FROM_PROBLEM", "problem_occurrence", problem.id),
        ("RISK_MITIGATED_BY_PROCEDURE", "procedure", procedure.id),
    ]
    assert not check_knowledge_relations(session), "every end of every edge is a real row"


# -- lessons -----------------------------------------------------------------
def test_a_lesson_needs_evidence_and_a_reviewer_who_is_not_the_author(session, hierarchy) -> None:
    repository = LessonRepository(session)
    lesson = repository.capture(
        title="Washout below the motor",
        lesson="Trip for a motor replacement on any washout signature below 9000 m, not after.",
        observation="Two similar signs were read as bit balling on A-3.",
        problem_type="differential_sticking",
        field_id=hierarchy["field"].id,
        well_id=hierarchy["well_a"].id,
        root_cause="erosion under a partially plugged nozzle",
        created_by="k.adeyemi",
        applicable_operations=["tripping", "drilling"],
        hole_size_in=12.25,
        depth_from_value=9000.0,
        depth_from_unit="m",
    )
    assert lesson.status == "DRAFT"
    assert lesson.problem_type == "differential_sticking"
    assert lesson.applicable_operations == ["tripping", "drilling"]
    assert lesson.root_cause_status == "KNOWN", "a cause written down is a cause stated"
    assert lesson.title.startswith("Washout")

    with pytest.raises(ValidationError, match="no evidence"):
        repository.approve(lesson.id, by="d.okafor")
    with pytest.raises(ValidationError, match="cannot approve it"):
        repository.approve(lesson.id, by="k.adeyemi")

    repository.update_lesson(lesson.id, provenance=_provenance())
    approved = repository.approve(lesson.id, by="d.okafor", note="agreed at the AAR")
    assert approved.status == "APPROVED"
    assert approved.approved_by == "d.okafor"
    assert approved.approved_at is not None
    assert repository.evidence_count(lesson.id) == 1

    # An approved lesson is not edited in place; it is revised.
    with pytest.raises(ValidationError, match="new revision"):
        repository.update_lesson(lesson.id, lesson="Different advice entirely.")
    revised = repository.revise(
        lesson.id, by="k.adeyemi", changes={"lesson": "Trip on the first signature, full stop."}
    )
    assert revised.revision == 2
    assert revised.status == "DRAFT"
    assert revised.provenance == lesson.provenance, "the proof does not change with the wording"
    assert lesson.status == "SUPERSEDED"
    assert repository.list_lessons(field_id=hierarchy["field"].id) == [revised]


def test_evidence_edges_are_kept_apart_from_derivation(session, hierarchy) -> None:
    from sqlalchemy import select

    from drilling_intelligence.database.models import KnowledgeRelation

    repository = LessonRepository(session)
    lesson = repository.capture(
        lesson="Do not wait for the second sign.",
        field_id=hierarchy["field"].id,
        created_by="k.adeyemi",
    )
    counts = repository.attach_evidence(
        lesson.id,
        document_version_ids=[],
        well_ids=[hierarchy["well_a"].id, hierarchy["well_b"].id],
    )
    assert counts == {"cited": 0, "derived": 2}
    repository.attach_evidence(lesson.id, well_ids=[hierarchy["well_b"].id])
    edges = list(
        session.execute(
            select(KnowledgeRelation)
            .where(
                KnowledgeRelation.source_type == "lesson",
                KnowledgeRelation.source_id == lesson.id,
            )
            .order_by(KnowledgeRelation.target_id)
        ).scalars()
    )
    assert [edge.relation for edge in edges] == ["LESSON_DERIVED_FROM_WELL"] * 2, (
        "the same well twice is the same edge, not two"
    )
    assert {edge.target_id for edge in edges} == {hierarchy["well_a"].id, hierarchy["well_b"].id}
    assert repository.evidence(lesson.id)["wells"], (
        "the wells a lesson was learnt on are readable back"
    )
    assert repository.evidence_count(lesson.id) == 2
    with pytest.raises(ValidationError, match="no well"):
        repository.attach_evidence(lesson.id, well_ids=["well-nope"])


def test_rejecting_a_lesson_keeps_the_reason_and_reopening_it_is_possible(
    session, hierarchy
) -> None:
    repository = LessonRepository(session)
    lesson = repository.capture(
        lesson="Always run the top drive at 900 gpm.", created_by="k.adeyemi"
    )
    with pytest.raises(ValidationError, match="needs a reason"):
        repository.reject(lesson.id, by="d.okafor", reason="")
    repository.reject(lesson.id, by="d.okafor", reason="only true in the 12 1/4 in section")
    assert lesson.status == "REJECTED"
    assert "only true in the 12 1/4 in section" in str(lesson.status_note)
    repository.reopen(lesson.id, by="k.adeyemi", reason="narrowed the claim")
    assert lesson.status == "DRAFT"


def test_a_practice_comes_out_of_an_approved_lesson_and_carries_its_evidence(
    session, hierarchy
) -> None:
    repository = LessonRepository(session)
    lesson = repository.capture(
        lesson="Spot the washout before the second sign.",
        observation="Two signs were read as bit balling.",
        field_id=hierarchy["field"].id,
        created_by="k.adeyemi",
    )
    with pytest.raises(ValidationError, match="only an approved lesson"):
        repository.promote_to_best_practice(
            lesson.id, by="d.okafor", statement="Trip on the first washout signature."
        )
    repository.update_lesson(lesson.id, provenance=_provenance())
    repository.approve(lesson.id, by="d.okafor")

    practice = repository.promote_to_best_practice(
        lesson.id,
        by="d.okafor",
        statement="Trip on the first washout signature below 9000 m.",
        code="NCF-BP-02",
        practice_type="tripping",
        applicable_operations=["tripping"],
    )
    assert practice.status == str(ProcedureLifecycle.DRAFT), (
        "a practice still needs its own approval"
    )
    assert practice.revision == 1
    assert practice.well_id is None, "a practice is a field-level statement, not a well note"
    assert practice.field_id == lesson.field_id
    assert practice.provenance == lesson.provenance, "the evidence travels with the practice"
    assert practice.attributes["promoted_from_lesson"] == lesson.id
    assert practice.rationale == lesson.observation

    with pytest.raises(ValidationError, match="cannot be emptied"):
        repository.update_practice(practice.id, statement="   ")
    with pytest.raises(ValidationError, match="author of a practice cannot approve"):
        repository.approve_practice(practice.id, by="d.okafor")
    # The revision that follows carries no rationale, which is the thing that has to be checked.
    revised_practice = repository.revise_practice(
        practice.id, by="d.okafor", changes={"rationale": ""}
    )
    assert revised_practice.revision == 2
    assert revised_practice.rationale == ""
    assert practice.is_current is False
    with pytest.raises(ValidationError, match="needs a rationale"):
        repository.approve_practice(revised_practice.id, by="e.vaziri")
    repository.update_practice(
        revised_practice.id, rationale="Three washouts in this field started here."
    )
    approved = repository.approve_practice(
        revised_practice.id, by="e.vaziri", note="field standard"
    )
    assert approved.provenance == practice.provenance, "a revision keeps the evidence too"
    assert approved.status == str(ProcedureLifecycle.APPROVED)
    assert repository.practices_for_well(hierarchy["well_a"].id) == [approved]
    assert repository.practices_for_well(hierarchy["well_a"].id, hole_size_in=8.5) == [approved]
    assert repository.practices_for_well(hierarchy["well_b"].id) == [approved]


def test_a_practice_for_another_hole_size_is_not_shown(session, hierarchy) -> None:
    repository = LessonRepository(session)
    lesson = repository.capture(
        lesson="Use a lower torque limit in the 17 1/2 in hole.",
        field_id=hierarchy["field"].id,
        created_by="k.adeyemi",
        provenance=_provenance(),
    )
    repository.approve(lesson.id, by="d.okafor")
    practice = repository.promote_to_best_practice(
        lesson.id, by="d.okafor", statement="Torque limit 22k ft-lb.", rationale="two twists-off"
    )
    repository.update_practice(practice.id, hole_size_in=17.5)
    repository.approve_practice(practice.id, by="e.vaziri")
    assert repository.practices_for_well(hierarchy["well_a"].id) == [practice]
    assert repository.practices_for_well(hierarchy["well_a"].id, hole_size_in=8.5) == []
    assert repository.practices_for_well(hierarchy["well_a"].id, hole_size_in=17.5) == [practice]


# -- recommendations ---------------------------------------------------------
def test_a_recommendation_is_proposed_and_only_a_person_decides_it(session, hierarchy) -> None:
    repository = LessonRepository(session)
    lesson = repository.capture(
        lesson="Narrow the density window in the 8 1/2 in section.",
        field_id=hierarchy["field"].id,
        created_by="k.adeyemi",
        provenance=_provenance(),
    )
    repository.approve(lesson.id, by="d.okafor")
    recommendation = repository.propose_recommendation(
        statement="Set the 8 1/2 in mud weight target at 10.8 ppg, not 11.4 ppg.",
        reason="Two wells lost 20+ h to tight hole at the top of the window.",
        evidence=[
            {"kind": "pattern", "well_ids": [hierarchy["well_a"].id, hierarchy["well_b"].id]}
        ],
        query={"problem_type": "stuck_pipe", "hole_size_in": 8.5},
        confidence=0.7,
        lesson_id=lesson.id,
        field_id=hierarchy["field"].id,
    )
    assert recommendation.status == str(RecommendationLifecycle.PROPOSED)
    assert recommendation.signature and len(recommendation.signature) == 32
    assert recommendation.generated_by == "intelligence"
    assert recommendation.decided_by is None
    assert recommendation.query == {"problem_type": "stuck_pipe", "hole_size_in": 8.5}

    again = repository.propose_recommendation(
        statement="Set the 8 1/2 in mud weight target at 10.8 ppg, not 11.4 ppg.",
        field_id=hierarchy["field"].id,
        lesson_id=lesson.id,
    )
    assert again.id == recommendation.id, (
        "re-deriving advice finds the open row instead of duplicating it"
    )

    with pytest.raises(ValidationError, match="needs a person"):
        repository.decide_recommendation(recommendation.id, "ACCEPTED", by="")
    with pytest.raises(ValidationError, match="needs a reason"):
        repository.decide_recommendation(recommendation.id, "DECLINED", by="d.okafor")
    repository.decide_recommendation(
        recommendation.id, "DECLINED", by="d.okafor", reason="hole pressure tested"
    )
    assert recommendation.decline_reason == "hole pressure tested"
    assert recommendation.decided_by == "d.okafor"

    # A declined recommendation stays declined when the analysis runs again; it is not silently reset.
    third = repository.propose_recommendation(
        statement="Set the 8 1/2 in mud weight target at 10.8 ppg, not 11.4 ppg.",
        field_id=hierarchy["field"].id,
        lesson_id=lesson.id,
    )
    assert third.id == recommendation.id
    assert third.status == str(RecommendationLifecycle.DECLINED)

    reopened = repository.propose_recommendation(
        statement="Narrow the window in the top hole instead.",
        field_id=hierarchy["field"].id,
        lesson_id=lesson.id,
    )
    repository.decide_recommendation(reopened.id, "ACCEPTED", by="d.okafor", reason="agreed")
    engineering = EngineeringRepository(session)
    procedure = engineering.create_procedure(title="Mud weight targets by section")
    program = engineering.create_program(title="A-3 programme", well_id=hierarchy["well_a"].id)
    repository.decide_recommendation(reopened.id, "IMPLEMENTED", by="d.okafor")
    assert reopened.status == str(RecommendationLifecycle.IMPLEMENTED)
    assert reopened.procedure_id is None, (
        "implementing is a decision; which procedure it landed in is a separate, explicit link"
    )
    assert engineering.reference_procedure(
        procedure.id, lesson_ids=[lesson.id], program_id=program.id
    ) == {"PROCEDURE_BASED_ON_PROGRAM": 1, "PROCEDURE_ADDRESSES_LESSON": 1}
    assert not check_knowledge_relations(session)
    listing = repository.list_recommendations(field_id=hierarchy["field"].id, status="PROPOSED")
    assert listing == []
    counts = repository.counts(field_id=hierarchy["field"].id)
    assert counts["lessons"] == 1
    assert counts["without_evidence"] == 0, "the lesson was captured with a provenance entry"
    assert counts["practices"] == 0
    assert counts["recommendations_open"] == 0, "both recommendations were decided"


def test_two_current_revisions_cannot_be_written(db, session, hierarchy) -> None:
    """The partial unique index is what makes "the current revision" a single answer.

    Committed first, and attempted from a second session: a rejected write inside the fixture's own
    transaction would leave that session needing a rollback, and the assertion would then be about
    SQLAlchemy's error recovery rather than about the constraint.
    """
    from sqlalchemy.orm import Session as SqlSession

    repository = EngineeringRepository(session)
    procedure = repository.create_procedure(
        title="Cementing", code="NCF-CEM-01", field_id=hierarchy["field"].id
    )
    session.commit()

    with SqlSession(db.engine) as other:
        other.add(
            ProcedureRecord(
                id="proc-other-current",
                code="NCF-CEM-01",
                title="Cementing, written by hand",
                procedure_type="general",
                revision=2,
                is_current=True,
                status=str(ProcedureLifecycle.DRAFT),
                field_id=hierarchy["field"].id,
                origin="MANUAL",
                created_by="someone",
            )
        )
        with pytest.raises(IntegrityError):
            other.flush()
        other.rollback()

    assert repository.current_procedure("NCF-CEM-01").id == procedure.id
    assert repository.list_procedures(field_id=hierarchy["field"].id) == [procedure]
