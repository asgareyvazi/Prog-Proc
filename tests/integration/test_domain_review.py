"""Forensic guarantees of the read-only Domain Review boundary.

These tests use the real generated corpus and the real SQLite workspace.  The review is intentionally
not treated as another persistence feature: the strongest assertions compare the database before and
after a read, then repeat the read and compare the complete value object.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import inspect, select
from tests.fixtures.fieldops import add_casing_program, ingest, promote, well_id_for

from drilling_intelligence.database.models import DdrReport, Well
from drilling_intelligence.database.serialize import record_to_dict
from drilling_intelligence.engineering.repository import EngineeringRepository
from drilling_intelligence.lessons.repository import LessonRepository
from drilling_intelligence.review import DomainReviewRequest, DomainReviewService
from drilling_intelligence.wells.repository import WellRepository


def _database_snapshot(workspace) -> tuple[Any, ...]:
    """A SQLite row snapshot that includes lifecycle/evidence columns and migration state."""
    engine = workspace.database.engine
    names = sorted(inspect(engine).get_table_names())
    with engine.connect() as connection:
        return tuple(
            (
                name,
                tuple(
                    tuple(str(value) for value in row)
                    for row in connection.exec_driver_sql(
                        f'SELECT * FROM "{name}" ORDER BY rowid'  # noqa: S608
                    ).fetchall()
                ),
            )
            for name in names
        )


def _records(review, record_type: str) -> list:
    return [record for record in review.records if record.record_type == record_type]


def test_review_is_authoritative_repeatable_and_does_not_mutate(workspace) -> None:
    """A human review cannot be a write disguised as a read, and two reads must be identical."""
    ingest(workspace)
    promote(workspace)
    well_id = well_id_for(workspace, "A-3")
    service = DomainReviewService.for_workspace(workspace)

    before = _database_snapshot(workspace)
    first = service.review(DomainReviewRequest(well_id=well_id))
    between = _database_snapshot(workspace)
    second = service.review(DomainReviewRequest(well_id=well_id))
    after = _database_snapshot(workspace)

    assert before == between == after
    assert first.to_dict() == second.to_dict()
    assert first.observations["search_sidecar_used"] is False
    assert first.observations["open_conflicts"] == len(first.conflicts)
    assert first.record_count > 0
    assert _records(first, "ddr_report")
    assert _records(first, "document")
    assert _records(first, "document_version")
    assert _records(first, "knowledge_item")
    assert _records(first, "npt_record")
    assert _records(first, "well_operation")
    assert _records(first, "program_target")
    assert first.plan_actual
    assert all(row["well_id"] == well_id for row in first.plan_actual)
    assert all(record.record_id for record in first.records)

    cited = next(record for record in first.records if record.provenance)
    assert cited.evidence, "a review row must carry its existing evidence/provenance pointers"
    assert cited.verification.retrieval == "NOT_USED"
    assert all("score" not in record.data for record in first.records)

    limited = service.review(DomainReviewRequest(well_id=well_id, limit=1))
    assert len(limited.records) <= 1
    assert limited.truncated is True
    assert limited.observations["truncated"] is True


def test_review_keeps_program_history_and_plan_actual_lineage_visible(workspace) -> None:
    """The current answer follows the current programme; history keeps the copied old targets."""
    ingest(workspace)
    promote(workspace)
    add_casing_program(workspace)
    well_id = well_id_for(workspace, "A-3")

    other_well_id = well_id_for(workspace, "B-11")
    with workspace.database.unit_of_work() as session:
        repository = EngineeringRepository(session)
        other_program = repository.create_program(title="B-11 programme", well_id=other_well_id)
        current = repository.list_programs(well_id=well_id, include_superseded=False)
        assert current
        old_program = current[0]
        revised = repository.revise_program(old_program.id, by="review-test")
        old_targets = repository.list_targets(old_program.id)
        assert old_targets
        old_target_ids = {row.id for row in old_targets}
        session.flush()

    service = DomainReviewService.for_workspace(workspace)
    current_review = service.review(DomainReviewRequest(well_id=well_id, lifecycle="current"))
    history_review = service.review(DomainReviewRequest(well_id=well_id, lifecycle="history"))

    current_programs = {record.record_id for record in _records(current_review, "drilling_program")}
    history_programs = {record.record_id for record in _records(history_review, "drilling_program")}
    assert old_program.id not in current_programs
    assert revised.id in current_programs
    assert old_program.id in history_programs
    assert revised.id in history_programs
    assert other_program.id not in history_programs, (
        "another well's programme must not leak through field scope"
    )

    current_targets = {record.record_id for record in _records(current_review, "program_target")}
    history_targets = {
        record.record_id: record for record in _records(history_review, "program_target")
    }
    assert not old_target_ids & current_targets
    assert old_target_ids <= history_targets.keys()
    assert all(not history_targets[target_id].current for target_id in old_target_ids)
    current_plan_programs = {row["program_id"] for row in current_review.plan_actual}
    history_plan_programs = {row["program_id"] for row in history_review.plan_actual}
    assert revised.id in current_plan_programs
    assert old_program.id not in current_plan_programs
    assert old_program.id in history_plan_programs
    assert any(record.current for record in _records(history_review, "program_target"))


def test_review_intersects_inherited_program_targets_with_the_subject_well(workspace) -> None:
    """A field-scoped program may own targets for several wells, but a well review may not leak them."""
    ingest(workspace)
    promote(workspace)
    a_id = well_id_for(workspace, "A-3")
    b_id = well_id_for(workspace, "B-11")

    with workspace.database.unit_of_work() as session:
        b_well = session.get(Well, b_id)
        assert b_well is not None
        b_section = WellRepository(session).get_or_create_section(b_well, "B-only review section")
        a_well = session.get(Well, a_id)
        assert a_well is not None
        a_section = WellRepository(session).get_or_create_section(a_well, "A-only review section")
        repository = EngineeringRepository(session)
        inherited = repository.create_program(
            title="field template with A and B targets",
            field_id=str(a_well.field_id),
            project_id=str(a_well.project_id),
        )
        a_target = repository.add_target(
            inherited.id,
            name=a_section.name,
            section_id=a_section.id,
            planned_depth_md_value=9876.0,
        )
        b_target = repository.add_target(
            inherited.id,
            name=b_section.name,
            section_id=b_section.id,
            planned_depth_md_value=1234.0,
        )
        inherited_id, a_target_id, b_target_id = inherited.id, a_target.id, b_target.id

    service = DomainReviewService.for_workspace(workspace)
    for lifecycle in ("current", "history"):
        review = service.review(DomainReviewRequest(well_id=a_id, lifecycle=lifecycle))
        assert inherited_id in {record.record_id for record in _records(review, "drilling_program")}
        target_ids = {record.record_id for record in _records(review, "program_target")}
        assert a_target_id in target_ids
        assert b_target_id not in target_ids
        assert any(row["program_id"] == inherited_id for row in review.plan_actual)
        assert all(row["well_id"] == a_id for row in review.plan_actual)
        assert all(record.scope.get("well_id") != b_id for record in review.records)


def test_review_audits_file_citations_carried_in_record_evidence(workspace) -> None:
    """A row-level evidence citation is not merely displayed; the opt-in audit re-reads it."""
    ingest(workspace)
    promote(workspace)
    well_id = well_id_for(workspace, "A-3")
    with workspace.database.read_only() as session:
        source = session.scalar(select(DdrReport).where(DdrReport.well_id == well_id))
        assert source is not None and source.provenance
        citation = dict(source.provenance[0])

    with workspace.database.unit_of_work() as session:
        recommendation = LessonRepository(session).propose_recommendation(
            statement="Keep the cited review evidence attached",
            reason="domain review citation coverage",
            evidence=[citation],
            well_id=well_id,
        )
        recommendation_id = recommendation.id

    review = DomainReviewService.for_workspace(workspace).review(
        DomainReviewRequest(well_id=well_id, verify_citations=True)
    )
    record = next(
        row for row in _records(review, "recommendation") if row.record_id == recommendation_id
    )
    assert citation in record.evidence
    assert record.verification.citation_audit == "MATCH"
    assert review.citation_audit["counts"]["MATCH"] > 0


def test_review_exposes_calculation_contract_without_claiming_reproducibility(workspace) -> None:
    """Stored engineering claims are displayed with their inputs, not executed by review."""
    ingest(workspace)
    promote(workspace)
    well_id = well_id_for(workspace, "A-3")
    with workspace.database.unit_of_work() as session:
        row, created = EngineeringRepository(session).record_calculation(
            method_id="hydraulics.ecd",
            method_version="1.0",
            calculation_type="ecd",
            inputs={"mud_weight": {"value": 10.2, "unit": "ppg"}},
            outputs={"ecd": 11.4},
            assumptions=["stored result; no executor is registered"],
            validation={"checked_by": "review-test"},
            well_id=well_id,
        )
        assert created is True
        calculation_id = row.id

    review = DomainReviewService.for_workspace(workspace).review(
        DomainReviewRequest(well_id=well_id)
    )
    calculation = next(record for record in review.records if record.record_id == calculation_id)
    assert calculation.record_type == "calculation"
    assert calculation.data["indexed_inputs"]
    assert "calculation_not_executable_in_repository" in calculation.flags
    assert calculation.verification.reproducibility == "NOT_EXECUTABLE_IN_REPOSITORY"
    assert calculation.data["method_id"] == "hydraulics.ecd"
    assert calculation.data["outputs"] == {"ecd": 11.4}


def test_optional_citation_audit_uses_existing_file_verification(workspace) -> None:
    """Citation auditing is opt-in and reported as audit metadata, never as a subjective score."""
    ingest(workspace)
    promote(workspace)
    before = _database_snapshot(workspace)
    review = DomainReviewService.for_workspace(workspace).review(
        DomainReviewRequest(well_id=well_id_for(workspace, "A-3"), verify_citations=True)
    )
    after = _database_snapshot(workspace)
    assert before == after
    assert review.citation_audit is not None
    assert review.citation_audit["counts"]["MATCH"] > 0
    assert "all_verified" in review.citation_audit
    assert all(record.verification.citation_audit != "NOT_RUN" for record in review.records)
    assert "confidence" not in review.observations
    assert "quality_score" not in review.observations


def test_review_request_rejects_ambiguous_lifecycle_and_negative_limits() -> None:
    for payload in (
        {"well_id": "well-1", "lifecycle": "future"},
        {"well_id": "well-1", "limit": -1},
    ):
        try:
            DomainReviewRequest.from_dict(payload)
        except ValueError:
            pass
        else:  # pragma: no cover - the assertion makes the failure message clearer
            raise AssertionError(f"accepted invalid review request {payload!r}")


def test_review_context_uses_the_existing_serialized_domain_rows(workspace) -> None:
    """The projection is ``record_to_dict`` data, not a second lossy serializer."""
    ingest(workspace)
    promote(workspace)
    well_id = well_id_for(workspace, "A-3")
    review = DomainReviewService.for_workspace(workspace).review(
        DomainReviewRequest(well_id=well_id)
    )
    with workspace.database.read_only() as session:
        from drilling_intelligence.database.models import DdrReport

        source = session.scalar(select(DdrReport).where(DdrReport.well_id == well_id))
        assert source is not None
        expected = record_to_dict(source)
    actual = next(record.data for record in review.records if record.record_id == source.id)
    assert actual == expected
