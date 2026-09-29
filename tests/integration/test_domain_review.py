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


def _clone_mud_reports(workspace, well_id: str, extra: int) -> tuple[int, int]:
    """Duplicate the corpus mud report so one well's child batch spans several parents.

    The review fetches a report's measurements as one batch bounded by ``limit * parents``.  With a
    single parent that bound equals ``limit``, so the corpus as generated cannot distinguish "this
    child batch was cut" from "this child batch simply has many rows".  Several parents can.
    """
    import uuid

    from drilling_intelligence.database.models import MudMeasurement, MudReport

    with workspace.database.unit_of_work() as session:
        original = session.execute(select(MudReport)).scalars().first()
        base = list(
            session.execute(
                select(MudMeasurement).where(MudMeasurement.mud_report_id == original.id)
            ).scalars()
        )
        for n in range(extra):
            clone = MudReport(
                id=str(uuid.uuid4()),
                well_id=well_id,
                section_id=original.section_id,
                identity_key=f"{original.identity_key}-clone-{n}",
                report_date=original.report_date,
                is_current=True,
            )
            session.add(clone)
            session.flush()
            for measurement in base:
                session.add(
                    MudMeasurement(
                        id=str(uuid.uuid4()),
                        mud_report_id=clone.id,
                        well_id=well_id,
                        section_id=measurement.section_id,
                        identity_key=f"{measurement.identity_key}-clone-{n}",
                        property_name=measurement.property_name,
                        sample_key=measurement.sample_key,
                        value=measurement.value,
                        unit=measurement.unit,
                    )
                )
        session.commit()
        reports = len(session.execute(select(MudReport)).scalars().all())
        measurements = len(session.execute(select(MudMeasurement)).scalars().all())
    return reports, measurements


class TestReviewTruncationTruthfulness:
    """A review must not claim truncation it did not perform.

    The child batches (mud measurements, BHA components, survey stations) are fetched at
    ``limit * number_of_parents`` so that no single report can starve the others.  Their sizes were
    then compared against ``limit`` - a bound belonging to a different population - so a well with
    several mud reports reported ``truncated=True`` while every measurement it had was returned.
    A reviewer told a complete answer was incomplete.
    """

    def test_a_complete_child_batch_is_not_reported_as_truncated(self, workspace) -> None:
        ingest(workspace)
        promote(workspace)
        well_id = well_id_for(workspace, "A-3")
        reports, measurements = _clone_mud_reports(workspace, well_id, extra=4)
        assert reports == 5 and measurements == 110, (
            "the probe corpus is not the one this test reasons about"
        )

        service = DomainReviewService.for_workspace(workspace)
        # limit=100 bounds the child batch at 100 * 5 = 500, and only 110 measurements exist, so
        # nothing is cut.  The review returned every row it has.
        review = service.review(DomainReviewRequest(well_id=well_id, limit=100))
        assert review.truncated is False, (
            f"a review that returned all {measurements} measurements claimed truncation"
        )

    def test_the_same_corpus_is_stable_across_generous_limits(self, workspace) -> None:
        """Raising the limit past the population must not change the answer or the flag."""
        ingest(workspace)
        promote(workspace)
        well_id = well_id_for(workspace, "A-3")
        _clone_mud_reports(workspace, well_id, extra=4)
        service = DomainReviewService.for_workspace(workspace)
        small = service.review(DomainReviewRequest(well_id=well_id, limit=100))
        large = service.review(DomainReviewRequest(well_id=well_id, limit=10_000))
        assert small.truncated is large.truncated is False
        assert len(small.records) == len(large.records)

    def test_a_limit_that_actually_cuts_still_reports_truncation(self, workspace) -> None:
        """Fixing the false positive must not silence the true one."""
        ingest(workspace)
        promote(workspace)
        well_id = well_id_for(workspace, "A-3")
        _clone_mud_reports(workspace, well_id, extra=4)
        service = DomainReviewService.for_workspace(workspace)
        uncapped = service.review(DomainReviewRequest(well_id=well_id, limit=10_000))
        cut = service.review(DomainReviewRequest(well_id=well_id, limit=10))
        assert len(cut.records) < len(uncapped.records), "limit=10 did not actually cut anything"
        assert cut.truncated is True, "a review that dropped rows said it was complete"

    def test_review_rows_are_not_duplicated_by_the_bounded_read(self, workspace) -> None:
        """The truncation bookkeeping must not feed the review's own row list twice."""
        ingest(workspace)
        promote(workspace)
        well_id = well_id_for(workspace, "A-3")
        service = DomainReviewService.for_workspace(workspace)
        review = service.review(DomainReviewRequest(well_id=well_id, limit=10_000))
        ids = [record.record_id for record in review.records]
        assert len(ids) == len(set(ids)), "the review returned the same record more than once"


def test_an_unscoped_record_is_not_treated_as_universal(workspace) -> None:
    """Ledger row 13: null scope must never become an accidental wildcard.

    ``_for_well`` keeps rows with no ``well_id`` on purpose - a field- or project-wide record is
    genuinely well-wide - but that allowance only ever sees rows a *positively* scoped query
    returned.  A record with no scope at all is fetched by nobody, so it belongs to no review; the
    alternative would be a record nobody filed against any well silently appearing in every well's
    review in the workspace.
    """
    ingest(workspace)
    promote(workspace)
    a_id = well_id_for(workspace, "A-3")
    b_id = well_id_for(workspace, "B-11")

    with workspace.database.unit_of_work() as session:
        repository = EngineeringRepository(session)
        unscoped = repository.create_program(title="no scope at all")
        a_well = session.get(Well, a_id)
        assert a_well is not None and a_well.field_id
        field_wide = repository.create_program(
            title="field template",
            field_id=str(a_well.field_id),
            project_id=str(a_well.project_id),
        )
        assert unscoped.well_id is None and unscoped.field_id is None
        assert unscoped.project_id is None, "a writable record with no scope at all"
        unscoped_id, field_wide_id = unscoped.id, field_wide.id

    service = DomainReviewService.for_workspace(workspace)
    ids: dict[str, set[str]] = {}
    for label, well_id in (("A-3", a_id), ("B-11", b_id)):
        review = service.review(DomainReviewRequest(well_id=well_id))
        ids[label] = {record.record_id for record in _records(review, "drilling_program")}

    assert unscoped_id not in ids["A-3"], "no scope means no well, not every well"
    assert unscoped_id not in ids["B-11"], "and not any other well either"
    assert field_wide_id in ids["A-3"], (
        "a positively field-scoped record really is well-wide, so the contrast is the point: "
        "inheritance comes from a scope that was asserted, not from one that was left empty"
    )


def test_the_currentness_matrix_is_table_keyed_not_one_rule_for_all(workspace) -> None:
    """Ledger row 14: each population's currentness rule is the one its own domain defines.

    Only ``document_version`` carries ``is_current``; every other table answers from status or from
    its parent's currentness.  Flattening these into one rule would be wrong in both directions -
    a REJECTED daily report is not historical, and a superseded calculation is.  So this pins each
    branch of ``_current_for`` separately, using real model instances rather than doubles, and
    asserts the *reason* the answer differs.
    """
    from drilling_intelligence.database.models import (
        Calculation,
        DdrReport,
        DocumentVersion,
        KnowledgeItem,
        ProgramTarget,
        Recommendation,
        RiskRecord,
    )
    from drilling_intelligence.review.service import _current_for

    def current(row, *, superseded=(), programs=()):
        return _current_for(
            row, superseded_calculations=set(superseded), current_programs=set(programs)
        )

    # program_target: currentness is inherited from the governing programme, never from itself.
    target = ProgramTarget(id="t-1", program_id="p-current")
    assert current(target, programs={"p-current"}) is True
    assert current(target, programs={"p-other"}) is False, (
        "a target of a superseded programme is historical even though the target row says nothing"
    )

    # document_version: the only table with an explicit currentness column.
    assert current(DocumentVersion(id="dv-1", is_current=True)) is True
    assert current(DocumentVersion(id="dv-2", is_current=False)) is False

    # calculation: superseded by lineage *or* by its own status - either is enough.
    assert current(Calculation(id="c-1", status="APPROVED")) is True
    assert current(Calculation(id="c-2", status="APPROVED"), superseded={"c-2"}) is False
    assert current(Calculation(id="c-3", status="SUPERSEDED")) is False

    # knowledge_item: SUPERSEDED and RETIRED are historical; anything else is current.
    assert current(KnowledgeItem(id="k-1", status="ACTIVE")) is True
    assert current(KnowledgeItem(id="k-2", status="SUPERSEDED")) is False
    assert current(KnowledgeItem(id="k-3", status="RETIRED")) is False

    # operational rows: only REJECTED is historical - a DRAFT or CANDIDATE report is still current.
    assert current(DdrReport(id="d-1", status="DRAFT")) is True
    assert current(DdrReport(id="d-2", status="REJECTED")) is False

    # risk_record / recommendation: SUPERSEDED only.
    assert current(RiskRecord(id="r-1", status="OPEN")) is True
    assert current(RiskRecord(id="r-2", status="SUPERSEDED")) is False
    assert current(Recommendation(id="rec-1", status="OPEN")) is True
    assert current(Recommendation(id="rec-2", status="SUPERSEDED")) is False

    # the fallback: a table with no rule of its own is current unless it says otherwise.
    class _Plain:
        __tablename__ = "something_else"

    assert current(_Plain()) is True
