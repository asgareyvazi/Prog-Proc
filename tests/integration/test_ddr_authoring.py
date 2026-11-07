"""Transactional tests for manual DDR drafts, submission, review, and revisions."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select, text
from sqlalchemy.exc import IntegrityError

from drilling_intelligence.core.errors import ValidationError
from drilling_intelligence.database.models import AuditEvent, DdrActivity, DdrReport, WellOperation
from drilling_intelligence.operations.ddr import DdrAuthoringService
from drilling_intelligence.review import ReviewActionRequest, ReviewActionService
from drilling_intelligence.review.actions import ReviewActionError


def _independent_ddr_save_process(
    database_url: str,
    report_id: str,
    expected_updated_at: str,
    summary: str,
    barrier: object,
    result_queue: object,
) -> None:
    """Compete from a fresh process, engine, session factory, and service instance."""
    from drilling_intelligence.config.settings import Settings
    from drilling_intelligence.database.session import Database

    database = Database.from_url(database_url, Settings())
    service = DdrAuthoringService(database)
    assert_editable = service._assert_editable

    def wait_after_snapshot(report, expected: str) -> None:
        assert_editable(report, expected)
        barrier.wait(timeout=20)  # type: ignore[attr-defined]

    service._assert_editable = wait_after_snapshot  # type: ignore[method-assign]
    try:
        saved = service.save_draft(
            report_id,
            expected_updated_at=expected_updated_at,
            report_date="2026-10-31",
            summary=summary,
            activities=[{"operation_type": "drilling", "label": summary}],
            actor="parallel-author",
            reason=f"Independent process save: {summary}",
        )
        result_queue.put(("success", saved["summary"]))  # type: ignore[attr-defined]
    except ValidationError as exc:
        result_queue.put(("stale", str(exc)))  # type: ignore[attr-defined]
    except BaseException as exc:  # noqa: BLE001 - surfaced to the parent assertion
        result_queue.put(("error", f"{type(exc).__name__}: {exc}"))  # type: ignore[attr-defined]
    finally:
        database.dispose()


def _activities() -> list[dict[str, str]]:
    return [
        {
            "operation_type": "drilling",
            "label": "Drilled 12 1/4 in. section",
            "description": "Rotary drilling continued; wording entered by the operator.",
            "period_text": "06:00-18:00",
        },
        {
            "operation_type": "tripping",
            "label": "POOH for bit inspection",
            "description": "No duration or depth is inferred.",
            "period_text": "18:00-20:00",
        },
    ]


def test_ddr_draft_submit_review_and_revision_are_explicit(
    db, workspace, committed_well_id
) -> None:
    service = DdrAuthoringService(db)
    draft = service.create_draft(
        well_id=committed_well_id,
        report_date="2026-10-31",
        report_number="DDR-31",
        shift="Day",
        summary="Operator-entered daily summary.",
        activities=_activities(),
        actor="author-1",
        reason="Create shift report",
    )
    assert draft["status"] == "CANDIDATE"
    assert draft["authoring_state"] == "DRAFT"
    assert draft["report_date_text"] == "2026-10-31"
    assert len(draft["activities"]) == 2
    assert all(not item["operation_id"] for item in draft["activities"])

    action_service = ReviewActionService.for_workspace(workspace)
    assert (
        action_service.available_actions(
            {
                "record_type": "ddr_report",
                "status": "CANDIDATE",
                "current": True,
                "data": {"authoring_state": "DRAFT"},
            }
        )
        == ()
    )

    saved = service.save_draft(
        draft["report_id"],
        expected_updated_at=draft["updated_at"],
        report_date="2026-10-31",
        report_number="DDR-31",
        shift="Day",
        summary="Corrected operator-entered daily summary.",
        activities=_activities(),
        actor="author-1",
        reason="Correct a typo in the summary",
    )
    assert saved["summary"] == "Corrected operator-entered daily summary."
    assert len(saved["activities"]) == 2
    assert datetime.fromisoformat(saved["updated_at"]) > datetime.fromisoformat(draft["updated_at"])
    with pytest.raises(ValidationError, match="changed since it was loaded"):
        service.save_draft(
            draft["report_id"],
            expected_updated_at=draft["updated_at"],
            report_date="2026-10-31",
            actor="author-1",
            reason="A stale editor must not overwrite the saved version",
        )

    submitted = service.submit(
        draft["report_id"],
        expected_updated_at=saved["updated_at"],
        actor="author-1",
        reason="Ready for an independent review",
    )
    assert submitted["authoring_state"] == "SUBMITTED"
    assert submitted["status"] == "CANDIDATE"
    assert all(item["operation_id"] for item in submitted["activities"])
    with db.read_only() as session:
        operations = list(
            session.scalars(
                select(WellOperation).where(WellOperation.report_id == draft["report_id"])
            )
        )
        activities = list(
            session.scalars(
                select(DdrActivity).where(DdrActivity.ddr_report_id == draft["report_id"])
            )
        )
        audit = list(
            session.scalars(
                select(AuditEvent).where(
                    AuditEvent.subject_type == "ddr_report",
                    AuditEvent.subject_id == draft["report_id"],
                )
            )
        )
    assert len(operations) == len(activities) == 2
    assert {row.status for row in operations} == {"CANDIDATE"}
    assert {row.origin for row in operations} == {"MANUAL"}
    assert all(row.is_current for row in operations)
    assert {item.operation_id for item in activities} == {row.id for row in operations}
    assert {row.action for row in audit} == {
        "ddr.draft_created",
        "ddr.draft_saved",
        "ddr.submitted",
    }
    assert all(row.detail["content_sha256"] for row in audit if "content_sha256" in row.detail)

    actions = action_service.available_actions(
        {
            "record_type": "ddr_report",
            "status": "CANDIDATE",
            "current": True,
            "data": {"authoring_state": "SUBMITTED"},
        }
    )
    assert {item.action_id for item in actions} == {"confirm", "reject"}
    assert all(item.requires_reason and item.requires_independent_actor for item in actions)
    operation_actions = action_service.available_actions(
        {
            "record_type": "well_operation",
            "status": "CANDIDATE",
            "current": True,
            "data": {
                "created_by": operations[0].created_by,
                "attributes": operations[0].attributes,
            },
        }
    )
    assert {item.action_id for item in operation_actions} == {"confirm", "reject"}
    assert all(
        item.requires_reason and item.requires_independent_actor for item in operation_actions
    )
    request = ReviewActionRequest(
        record_type="ddr_report",
        record_id=draft["report_id"],
        action="confirm",
        actor="author-1",
        expected_status="CANDIDATE",
        expected_current=True,
        expected_updated_at=submitted["updated_at"],
        expected_scope={"well_id": committed_well_id},
        reason="The report is accurate",
    )
    with pytest.raises(ValidationError, match="different from its author"):
        action_service.execute(request)
    with pytest.raises(ValidationError, match="needs a reason"):
        action_service.execute(
            ReviewActionRequest(
                record_type="ddr_report",
                record_id=draft["report_id"],
                action="confirm",
                actor="reviewer-2",
                expected_status="CANDIDATE",
                expected_current=True,
                expected_updated_at=submitted["updated_at"],
                expected_scope={"well_id": committed_well_id},
            )
        )
    confirmed = action_service.execute(
        ReviewActionRequest(
            record_type="ddr_report",
            record_id=draft["report_id"],
            action="confirm",
            actor="reviewer-2",
            expected_status="CANDIDATE",
            expected_current=True,
            expected_updated_at=submitted["updated_at"],
            expected_scope={"well_id": committed_well_id},
            reason="Reviewed against the authored report and approved",
        )
    )
    assert confirmed.status == "CONFIRMED"
    assert confirmed.at
    reviewed = service.get(draft["report_id"])
    assert any(
        item.get("source_kind") == "review_decision"
        and item.get("actor") == "reviewer-2"
        and item.get("reason") == "Reviewed against the authored report and approved"
        for item in reviewed["provenance"]
    )
    revision = service.create_revision(
        reviewed["report_id"],
        expected_updated_at=reviewed["updated_at"],
        actor="author-1",
        reason="Corrected shift narrative after review",
    )
    assert revision["revision_group_id"] == draft["revision_group_id"]
    assert revision["revision_number"] == 2
    assert revision["supersedes_report_id"] == draft["report_id"]
    assert revision["authoring_state"] == "DRAFT"
    assert revision["status"] == "CANDIDATE"
    assert revision["is_current"] is True
    assert len(revision["activities"]) == 2
    with db.read_only() as session:
        previous = session.get(DdrReport, draft["report_id"])
        old_operations = list(
            session.scalars(
                select(WellOperation).where(WellOperation.report_id == draft["report_id"])
            )
        )
    assert previous is not None and previous.status == "CONFIRMED" and not previous.is_current
    assert old_operations and all(not row.is_current for row in old_operations)
    historical = service.get(draft["report_id"])
    with pytest.raises(ValidationError, match="historical DDR revision is read-only"):
        service.save_draft(
            draft["report_id"],
            expected_updated_at=historical["updated_at"],
            report_date="2026-10-31",
            actor="author-1",
            reason="late edit",
        )
    from drilling_intelligence.database.migrations import upgrade

    with pytest.raises(RuntimeError, match="cannot downgrade 0026 while authored DDR revisions"):
        upgrade(db.engine, "0025", allow_downgrade=True)


def test_ddr_reject_action_requires_a_distinct_declared_actor_and_records_provenance(
    db, workspace, committed_well_id
) -> None:
    service = DdrAuthoringService(db)
    draft = service.create_draft(
        well_id=committed_well_id,
        report_date="2026-10-31",
        summary="Operator-entered report that must remain subject to review.",
        activities=[],
        actor="author-1",
        reason="Create report for rejection case",
    )
    submitted = service.submit(
        draft["report_id"],
        expected_updated_at=draft["updated_at"],
        actor="author-1",
        reason="Submit report for review",
    )
    actions = ReviewActionService.for_workspace(workspace)
    rejected = actions.execute(
        ReviewActionRequest(
            record_type="ddr_report",
            record_id=draft["report_id"],
            action="reject",
            actor="reviewer-2",
            expected_status="CANDIDATE",
            expected_current=True,
            expected_updated_at=submitted["updated_at"],
            expected_scope={"well_id": committed_well_id},
            reason="Needs correction before confirmation",
        )
    )
    assert rejected.status == "REJECTED"
    assert rejected.actor == "reviewer-2"
    assert (
        actions.available_actions(
            {
                "record_type": "ddr_report",
                "status": "REJECTED",
                "current": False,
                "data": {"authoring_state": "SUBMITTED"},
            }
        )
        == ()
    )
    final = service.get(draft["report_id"])
    assert final["is_current"]
    assert any(
        item.get("source_kind") == "review_decision"
        and item.get("actor") == "reviewer-2"
        and item.get("reason") == "Needs correction before confirmation"
        for item in final["provenance"]
    )


def test_ddr_activity_replacement_failure_rolls_back_report_activities_and_audit(
    db, committed_well_id
) -> None:
    service = DdrAuthoringService(db)
    draft = service.create_draft(
        well_id=committed_well_id,
        report_date="2026-10-31",
        summary="Original summary",
        activities=[{"operation_type": "drilling", "label": "Original activity"}],
        actor="author-1",
        reason="Create original draft",
    )
    before = service.get(draft["report_id"])

    # Force a database failure after the old activities have been deleted and flushed.
    # The service unit-of-work must restore both those rows and any preceding report edits.
    with db.engine.begin() as connection:
        connection.execute(
            text(
                """CREATE TRIGGER reject_ddr_activity_replacement
                BEFORE INSERT ON ddr_activity
                WHEN NEW.label = 'Reject replacement'
                BEGIN SELECT RAISE(ABORT, 'injected DDR activity failure'); END"""
            )
        )
    try:
        with pytest.raises(IntegrityError, match="injected DDR activity failure"):
            service.save_draft(
                draft["report_id"],
                expected_updated_at=before["updated_at"],
                report_date="2026-10-31",
                summary="This update must roll back",
                activities=[{"operation_type": "tripping", "label": "Reject replacement"}],
                actor="author-2",
                reason="Attempt replacement with injected failure",
            )
    finally:
        with db.engine.begin() as connection:
            connection.execute(text("DROP TRIGGER reject_ddr_activity_replacement"))

    after = service.get(draft["report_id"])
    assert after["summary"] == before["summary"]
    assert after["updated_at"] == before["updated_at"]
    assert after["activities"] == before["activities"]
    with db.read_only() as session:
        audit = list(
            session.scalars(
                select(AuditEvent).where(
                    AuditEvent.subject_type == "ddr_report",
                    AuditEvent.subject_id == draft["report_id"],
                )
            )
        )
    assert [event.action for event in audit] == ["ddr.draft_created"]


def test_ddr_rejects_unknown_operation_type_and_ambiguous_date(db, committed_well_id) -> None:
    service = DdrAuthoringService(db)
    with pytest.raises(ValidationError, match="registered operation vocabulary"):
        service.create_draft(
            well_id=committed_well_id,
            report_date="2026-10-31",
            activities=[{"operation_type": "guess", "label": "Unclassified work"}],
            actor="author",
            reason="test refusal",
        )
    with pytest.raises(ValidationError, match="YYYY-MM-DD"):
        service.create_draft(
            well_id=committed_well_id,
            report_date="October 31, 2026",
            summary="A report with no canonical day is refused.",
            actor="author",
            reason="test refusal",
        )
    with db.read_only() as session:
        assert session.scalars(select(DdrReport)).all() == []


def test_ddr_concurrency_timestamp_advances_when_clock_repeats(monkeypatch) -> None:
    from drilling_intelligence.operations import ddr

    current = datetime(2026, 11, 4, 12, 0, 0, tzinfo=UTC)
    monkeypatch.setattr(ddr, "_utc_now", lambda: current)

    assert ddr._next_updated_at(current) == current + timedelta(microseconds=1)
    assert ddr._next_updated_at(current.replace(tzinfo=None)) == current + timedelta(microseconds=1)


def test_parallel_ddr_saves_with_one_freshness_token_do_not_lose_an_update(
    db, committed_well_id, monkeypatch
) -> None:
    """SQLite's competing snapshot writer must fail rather than silently overwrite a save."""
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier

    assert db.engine.dialect.name == "sqlite"
    service = DdrAuthoringService(db)
    draft = service.create_draft(
        well_id=committed_well_id,
        report_date="2026-10-31",
        summary="Original operator-entered summary",
        actor="author-1",
        reason="Create a concurrency test draft",
    )
    barrier = Barrier(2)
    assert_editable = service._assert_editable

    def wait_after_freshness_check(report, expected_updated_at: str) -> None:
        assert_editable(report, expected_updated_at)
        barrier.wait(timeout=10)

    monkeypatch.setattr(service, "_assert_editable", wait_after_freshness_check)

    def save(summary: str) -> dict:
        return service.save_draft(
            draft["report_id"],
            expected_updated_at=draft["updated_at"],
            report_date="2026-10-31",
            summary=summary,
            actor="author-1",
            reason=f"Concurrent replacement: {summary}",
        )

    summaries = ("Concurrent update A", "Concurrent update B")
    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(save, summary) for summary in summaries]
        successes = []
        failures = []
        for future in futures:
            try:
                successes.append(future.result(timeout=15))
            except ValidationError as exc:
                failures.append(exc)

    assert len(successes) == 1
    assert len(failures) == 1
    assert "changed since it was loaded" in str(failures[0])
    persisted = service.get(draft["report_id"])
    assert persisted["summary"] in summaries
    assert persisted["updated_at"] > draft["updated_at"]
    with db.read_only() as session:
        events = list(
            session.scalars(
                select(AuditEvent).where(
                    AuditEvent.subject_type == "ddr_report",
                    AuditEvent.subject_id == draft["report_id"],
                )
            )
        )
    assert [event.action for event in events].count("ddr.draft_saved") == 1


def test_parallel_ddr_submissions_create_only_one_candidate_operation(
    db, committed_well_id, monkeypatch
) -> None:
    """One report snapshot cannot create duplicate operation rows under concurrent submit."""
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier

    service = DdrAuthoringService(db)
    draft = service.create_draft(
        well_id=committed_well_id,
        report_date="2026-10-31",
        activities=[{"operation_type": "drilling", "label": "One explicit activity"}],
        actor="author-1",
        reason="Create concurrent submit case",
    )
    barrier = Barrier(2)
    assert_editable = service._assert_editable

    def wait_after_freshness_check(report, expected_updated_at: str) -> None:
        assert_editable(report, expected_updated_at)
        barrier.wait(timeout=10)

    monkeypatch.setattr(service, "_assert_editable", wait_after_freshness_check)

    def submit():
        return service.submit(
            draft["report_id"],
            expected_updated_at=draft["updated_at"],
            actor="author-1",
            reason="Submit the same snapshot",
        )

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(submit), pool.submit(submit)]
        successes = []
        failures = []
        for future in futures:
            try:
                successes.append(future.result(timeout=15))
            except ValidationError as exc:
                failures.append(exc)

    assert len(successes) == 1
    assert len(failures) == 1
    assert "changed since it was loaded" in str(failures[0])
    with db.read_only() as session:
        operations = list(
            session.scalars(
                select(WellOperation).where(WellOperation.report_id == draft["report_id"])
            )
        )
        activities = list(
            session.scalars(
                select(DdrActivity).where(DdrActivity.ddr_report_id == draft["report_id"])
            )
        )
    assert len(operations) == len(activities) == 1
    assert activities[0].operation_id == operations[0].id
    assert operations[0].status == "CANDIDATE"


def test_parallel_ddr_revisions_create_only_one_current_successor(
    db, committed_well_id, monkeypatch
) -> None:
    """Two revision requests from one token cannot create duplicate successor reports."""
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier

    service = DdrAuthoringService(db)
    draft = service.create_draft(
        well_id=committed_well_id,
        report_date="2026-10-31",
        summary="Submitted report to revise concurrently.",
        actor="author-1",
        reason="Create concurrent revision case",
    )
    submitted = service.submit(
        draft["report_id"],
        expected_updated_at=draft["updated_at"],
        actor="author-1",
        reason="Submit report before revision race",
    )
    barrier = Barrier(2)
    claim_snapshot = service._claim_snapshot

    def wait_then_claim(session, report, expected_updated_at: str) -> None:
        barrier.wait(timeout=10)
        claim_snapshot(session, report, expected_updated_at)

    monkeypatch.setattr(service, "_claim_snapshot", wait_then_claim)

    def revise():
        return service.create_revision(
            draft["report_id"],
            expected_updated_at=submitted["updated_at"],
            actor="author-1",
            reason="Create a successor revision",
        )

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(revise), pool.submit(revise)]
        successes = []
        failures = []
        for future in futures:
            try:
                successes.append(future.result(timeout=15))
            except ValidationError as exc:
                failures.append(exc)

    assert len(successes) == 1
    assert len(failures) == 1
    assert "changed since it was loaded" in str(failures[0])
    history = service.list_reports(committed_well_id, include_history=True)
    assert len(history) == 2
    assert {record["revision_number"] for record in history} == {1, 2}
    assert sum(bool(record["is_current"]) for record in history) == 1


def test_concurrent_ddr_review_actions_are_serialized_by_the_claimed_snapshot(
    db, workspace, committed_well_id, monkeypatch
) -> None:
    """Two reviewers cannot confirm and reject the same displayed report concurrently."""
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier

    service = DdrAuthoringService(db)
    draft = service.create_draft(
        well_id=committed_well_id,
        report_date="2026-10-31",
        summary="A submitted report for competing review actions.",
        actor="author-1",
        reason="Create review concurrency case",
    )
    submitted = service.submit(
        draft["report_id"],
        expected_updated_at=draft["updated_at"],
        actor="author-1",
        reason="Submit the concurrency case",
    )
    # Materialize the workspace's lazy engine before either worker starts.
    _ = workspace.database
    actions_a = ReviewActionService.for_workspace(workspace)
    actions_b = ReviewActionService.for_workspace(workspace)
    barrier = Barrier(2)
    check_preconditions = actions_a._check_preconditions

    def wait_after_preconditions(session, row, request) -> None:
        check_preconditions(session, row, request)
        barrier.wait(timeout=10)

    monkeypatch.setattr(actions_a, "_check_preconditions", wait_after_preconditions)
    monkeypatch.setattr(actions_b, "_check_preconditions", wait_after_preconditions)

    def decide(service, action: str, actor: str):
        return service.execute(
            ReviewActionRequest(
                record_type="ddr_report",
                record_id=draft["report_id"],
                action=action,
                actor=actor,
                expected_status="CANDIDATE",
                expected_current=True,
                expected_updated_at=submitted["updated_at"],
                expected_scope={"well_id": committed_well_id},
                reason=f"Concurrent independent review: {action}",
            )
        )

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [
            pool.submit(decide, actions_a, "confirm", "reviewer-1"),
            pool.submit(decide, actions_b, "reject", "reviewer-2"),
        ]
        successes = []
        failures = []
        for future in futures:
            try:
                successes.append(future.result(timeout=15))
            except ReviewActionError as exc:
                failures.append(exc)

    assert len(successes) == 1
    assert len(failures) == 1
    assert "stale" in str(failures[0])
    final = service.get(draft["report_id"])
    assert final["status"] in {"CONFIRMED", "REJECTED"}
    history = [item for item in final["provenance"] if item.get("source_kind") == "review_decision"]
    assert len(history) == 1


def test_ddr_operation_review_uses_the_same_atomic_claim_without_confirming_its_report(
    db, workspace, committed_well_id
) -> None:
    service = DdrAuthoringService(db)
    draft = service.create_draft(
        well_id=committed_well_id,
        report_date="2026-10-31",
        activities=[{"operation_type": "drilling", "label": "Operator-entered operation"}],
        actor="author-1",
        reason="Create operation review case",
    )
    submitted = service.submit(
        draft["report_id"],
        expected_updated_at=draft["updated_at"],
        actor="author-1",
        reason="Submit one candidate operation",
    )
    operation_id = submitted["activities"][0]["operation_id"]
    with db.read_only() as session:
        operation = session.get(WellOperation, operation_id)
        assert operation is not None
        operation_updated_at = operation.updated_at

    result = ReviewActionService.for_workspace(workspace).execute(
        ReviewActionRequest(
            record_type="well_operation",
            record_id=operation_id,
            action="confirm",
            actor="reviewer-1",
            expected_status="CANDIDATE",
            expected_current=True,
            expected_updated_at=operation_updated_at.isoformat(),
            expected_scope={"well_id": committed_well_id},
            reason="Independently confirmed the authored operation",
        )
    )
    assert result.status == "CONFIRMED"
    with db.read_only() as session:
        report = session.get(DdrReport, draft["report_id"])
        operation = session.get(WellOperation, operation_id)
        assert report is not None and report.status == "CANDIDATE"
        assert operation is not None and operation.status == "CONFIRMED"


def test_same_ddr_snapshot_is_claimed_once_across_independent_sqlite_processes(
    db, committed_well_id
) -> None:
    """File-backed SQLite must reject a stale writer across engines and processes.

    Both workers load and validate the same pre-write token before the process barrier releases
    either one. The conditional snapshot claim is then exercised against separate SQLAlchemy
    engines/session factories, not shared Python service state.
    """
    import multiprocessing

    service = DdrAuthoringService(db)
    draft = service.create_draft(
        well_id=committed_well_id,
        report_date="2026-10-31",
        summary="Original process-race snapshot.",
        activities=[{"operation_type": "drilling", "label": "Original activity"}],
        actor="author-1",
        reason="Create process-level concurrency case",
    )
    context = multiprocessing.get_context("spawn")
    barrier = context.Barrier(2)
    results = context.Queue()
    summaries = ("Independent process A", "Independent process B")
    workers = [
        context.Process(
            target=_independent_ddr_save_process,
            args=(db.url, draft["report_id"], draft["updated_at"], summary, barrier, results),
        )
        for summary in summaries
    ]
    try:
        for worker in workers:
            worker.start()
        outcomes = [results.get(timeout=45) for _ in workers]
        for worker in workers:
            worker.join(timeout=45)
        assert all(not worker.is_alive() for worker in workers), [
            worker.exitcode for worker in workers
        ]
        assert [worker.exitcode for worker in workers] == [0, 0]
    finally:
        for worker in workers:
            if worker.is_alive():
                worker.terminate()
                worker.join(timeout=5)
        results.close()
        results.join_thread()

    successes = [value for state, value in outcomes if state == "success"]
    stale = [value for state, value in outcomes if state == "stale"]
    errors = [value for state, value in outcomes if state == "error"]
    assert errors == [], errors
    assert len(successes) == len(stale) == 1, outcomes
    assert "changed since it was loaded" in stale[0]

    persisted = service.get(draft["report_id"])
    assert persisted["summary"] == successes[0]
    assert persisted["summary"] in summaries
    assert persisted["updated_at"] > draft["updated_at"]
    assert len(persisted["activities"]) == 1
    assert persisted["activities"][0]["label"] == persisted["summary"]
    with db.read_only() as session:
        events = list(
            session.scalars(
                select(AuditEvent).where(
                    AuditEvent.subject_type == "ddr_report",
                    AuditEvent.subject_id == draft["report_id"],
                )
            )
        )
    assert [event.action for event in events].count("ddr.draft_saved") == 1
    with db.engine.connect() as connection:
        assert list(connection.execute(text("PRAGMA foreign_key_check"))) == []
