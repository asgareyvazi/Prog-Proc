"""Transactional tests for manual DDR drafts, submission, review, and revisions."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select, text
from sqlalchemy.exc import IntegrityError

from drilling_intelligence.core.errors import ValidationError
from drilling_intelligence.database.models import AuditEvent, DdrActivity, DdrReport, WellOperation
from drilling_intelligence.operations.ddr import DdrAuthoringService
from drilling_intelligence.operations.ddr_comparison import compare_ddr_revisions
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


def _independent_ddr_failing_save_process(
    database_url: str,
    report_id: str,
    expected_updated_at: str,
    result_queue: object,
) -> None:
    """Run one save in a fresh process so a DB trigger can prove transaction rollback."""
    from drilling_intelligence.config.settings import Settings
    from drilling_intelligence.database.session import Database

    database = Database.from_url(database_url, Settings())
    try:
        DdrAuthoringService(database).save_draft(
            report_id,
            expected_updated_at=expected_updated_at,
            report_date="2026-11-08",
            summary="Must roll back after injected insert failure",
            activities=[{"operation_type": "tripping", "label": "Must not persist"}],
            actor="declared-author",
            reason="Exercise spawned transactional rollback",
        )
        result_queue.put(("unexpected_success", "save unexpectedly committed"))  # type: ignore[attr-defined]
    except BaseException as exc:  # noqa: BLE001 - sent to the parent test assertion
        result_queue.put(("error", f"{type(exc).__name__}: {exc}"))  # type: ignore[attr-defined]
    finally:
        database.dispose()


def _independent_ddr_transition_process(
    database_url: str,
    report_id: str,
    expected_updated_at: str,
    action: str,
    barrier: object,
    result_queue: object,
    actor: str,
) -> None:
    """Race a lifecycle/review write using a process-local engine and service."""
    from types import SimpleNamespace

    from drilling_intelligence.config.settings import Settings
    from drilling_intelligence.database.session import Database

    database = Database.from_url(database_url, Settings())
    authoring = DdrAuthoringService(database)
    try:
        if action == "submit":
            assert_editable = authoring._assert_editable

            def wait_after_freshness(report, expected: str) -> None:
                assert_editable(report, expected)
                barrier.wait(timeout=20)  # type: ignore[attr-defined]

            authoring._assert_editable = wait_after_freshness  # type: ignore[method-assign]
            value = authoring.submit(
                report_id,
                expected_updated_at=expected_updated_at,
                actor=actor,
                reason=f"Independent process submission by {actor}",
            )
            result_queue.put(("success", value["authoring_state"]))  # type: ignore[attr-defined]
        elif action == "revision":
            claim_snapshot = authoring._claim_snapshot

            def wait_then_claim(session, report, expected: str) -> None:
                barrier.wait(timeout=20)  # type: ignore[attr-defined]
                claim_snapshot(session, report, expected)

            authoring._claim_snapshot = wait_then_claim  # type: ignore[method-assign]
            value = authoring.create_revision(
                report_id,
                expected_updated_at=expected_updated_at,
                actor=actor,
                reason=f"Independent process revision by {actor}",
            )
            result_queue.put(("success", value["report_id"]))  # type: ignore[attr-defined]
        elif action in {"confirm", "reject", "operation_confirm", "operation_reject"}:
            operation_action = action.startswith("operation_")
            review_action = action.removeprefix("operation_")
            review = ReviewActionService(workspace=SimpleNamespace(database=database))
            check = review._check_preconditions

            def wait_after_preconditions(session, row, request) -> None:
                check(session, row, request)
                barrier.wait(timeout=20)  # type: ignore[attr-defined]

            review._check_preconditions = wait_after_preconditions  # type: ignore[method-assign]
            value = review.execute(
                ReviewActionRequest(
                    record_type="well_operation" if operation_action else "ddr_report",
                    record_id=report_id,
                    action=review_action,
                    actor=actor,
                    expected_status="CANDIDATE",
                    expected_revision=None if operation_action else 1,
                    expected_current=True,
                    expected_updated_at=expected_updated_at,
                    reason=f"Independent process review by {actor}",
                )
            )
            result_queue.put(("success", value.status))  # type: ignore[attr-defined]
        else:
            raise AssertionError(f"unsupported test worker action: {action}")
    except (ValidationError, ReviewActionError) as exc:
        result_queue.put(("stale", str(exc)))  # type: ignore[attr-defined]
    except BaseException as exc:  # noqa: BLE001 - parent test reports worker failures
        result_queue.put(("error", f"{type(exc).__name__}: {exc}"))  # type: ignore[attr-defined]
    finally:
        database.dispose()


def _run_process_race(
    db,
    *,
    report_id: str,
    token: str,
    action: str,
    actors: tuple[str, str],
    actions: tuple[str, str] | None = None,
):
    """Race spawned workers against disposable file-backed SQLite.

    Actor values are declared provenance strings only; no authentication or human identity is
    exercised or implied by using different values here.
    """
    import multiprocessing
    from pathlib import Path

    from sqlalchemy.engine import make_url

    assert db.engine.dialect.name == "sqlite"
    database_path = make_url(db.url).database
    assert database_path not in {None, ":memory:"}
    assert Path(database_path).is_file(), "process races require disposable file-backed SQLite"
    worker_actions = actions or (action, action)
    context = multiprocessing.get_context("spawn")
    barrier = context.Barrier(2)
    results = context.Queue()
    workers = [
        context.Process(
            target=_independent_ddr_transition_process,
            args=(db.url, report_id, token, worker_action, barrier, results, actor),
        )
        for worker_action, actor in zip(worker_actions, actors, strict=True)
    ]
    outcomes = []
    try:
        for worker in workers:
            worker.start()
        outcomes = [results.get(timeout=45) for _ in workers]
        for worker in workers:
            worker.join(timeout=30)
        assert [worker.exitcode for worker in workers] == [0, 0]
        assert all(not worker.is_alive() for worker in workers)
    finally:
        for worker in workers:
            if worker.is_alive():
                worker.terminate()
                worker.join(timeout=5)
        results.close()
        results.join_thread()
    assert all(worker.exitcode is not None for worker in workers)
    successes = [value for state, value in outcomes if state == "success"]
    stale = [value for state, value in outcomes if state == "stale"]
    errors = [value for state, value in outcomes if state == "error"]
    assert errors == [], errors
    assert len(successes) == 1 and len(stale) == 1, outcomes
    return successes[0], stale[0]


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


def test_spawned_ddr_save_failure_rolls_back_rows_and_audit_on_file_backed_sqlite(
    db, committed_well_id
) -> None:
    import multiprocessing
    from pathlib import Path

    from sqlalchemy.engine import make_url

    assert db.engine.dialect.name == "sqlite"
    database_path = make_url(db.url).database
    assert database_path not in {None, ":memory:"}
    assert Path(database_path).is_file()
    service = DdrAuthoringService(db)
    draft = service.create_draft(
        well_id=committed_well_id,
        report_date="2026-11-08",
        summary="Persisted before the injected failure",
        activities=[{"operation_type": "drilling", "label": "Original persisted activity"}],
        actor="declared-author",
        reason="Create spawned rollback case",
    )
    original = service.get(draft["report_id"])
    with db.engine.begin() as connection:
        connection.exec_driver_sql(
            "CREATE TRIGGER reject_spawned_activity_insert BEFORE INSERT ON ddr_activity "
            "BEGIN SELECT RAISE(ABORT, 'injected spawned activity insert failure'); END"
        )

    context = multiprocessing.get_context("spawn")
    results = context.Queue()
    worker = context.Process(
        target=_independent_ddr_failing_save_process,
        args=(db.url, draft["report_id"], draft["updated_at"], results),
    )
    try:
        worker.start()
        outcome = results.get(timeout=45)
        worker.join(timeout=30)
        assert not worker.is_alive()
        assert worker.exitcode == 0
        assert outcome[0] == "error"
        assert "injected spawned activity insert failure" in outcome[1]
    finally:
        if worker.is_alive():
            worker.terminate()
            worker.join(timeout=5)
        results.close()
        results.join_thread()
        with db.engine.begin() as connection:
            connection.exec_driver_sql("DROP TRIGGER IF EXISTS reject_spawned_activity_insert")

    persisted = service.get(draft["report_id"])
    assert persisted["summary"] == original["summary"]
    assert persisted["activities"] == original["activities"]
    assert persisted["updated_at"] == original["updated_at"]
    with db.read_only() as session:
        events = list(
            session.scalars(
                select(AuditEvent).where(
                    AuditEvent.subject_type == "ddr_report",
                    AuditEvent.subject_id == draft["report_id"],
                )
            )
        )
    assert [event.action for event in events].count("ddr.draft_saved") == 0
    with db.engine.connect() as connection:
        assert list(connection.execute(text("PRAGMA foreign_key_check"))) == []
        assert connection.execute(text("PRAGMA integrity_check")).scalar_one() == "ok"


def test_same_ddr_submission_snapshot_is_claimed_once_across_independent_processes(
    db, committed_well_id
) -> None:
    """Two spawned writers cannot create duplicate operations from one DRAFT token."""
    service = DdrAuthoringService(db)
    draft = service.create_draft(
        well_id=committed_well_id,
        report_date="2026-11-08",
        activities=[{"operation_type": "drilling", "label": "One authored activity"}],
        actor="author-1",
        reason="Create cross-process submit case",
    )
    success, stale = _run_process_race(
        db,
        report_id=draft["report_id"],
        token=draft["updated_at"],
        action="submit",
        actors=("author-1", "author-1"),
    )
    assert success == "SUBMITTED"
    assert "changed since it was loaded" in stale
    persisted = service.get(draft["report_id"])
    assert persisted["authoring_state"] == "SUBMITTED"
    assert len(persisted["activities"]) == 1
    operation_id = persisted["activities"][0]["operation_id"]
    assert operation_id
    with db.read_only() as session:
        operations = list(
            session.scalars(
                select(WellOperation).where(WellOperation.report_id == draft["report_id"])
            )
        )
        events = list(
            session.scalars(
                select(AuditEvent).where(
                    AuditEvent.subject_type == "ddr_report",
                    AuditEvent.subject_id == draft["report_id"],
                )
            )
        )
    assert len(operations) == 1 and operations[0].id == operation_id
    assert operations[0].status == "CANDIDATE"
    assert [event.action for event in events].count("ddr.submitted") == 1
    with db.engine.connect() as connection:
        assert list(connection.execute(text("PRAGMA foreign_key_check"))) == []
        assert connection.execute(text("PRAGMA integrity_check")).scalar_one() == "ok"


def test_same_ddr_review_snapshot_is_claimed_once_across_independent_processes(
    db, committed_well_id
) -> None:
    """Conflicting process-level decisions yield one audit disposition and no operation promotion."""
    service = DdrAuthoringService(db)
    draft = service.create_draft(
        well_id=committed_well_id,
        report_date="2026-11-08",
        activities=[{"operation_type": "drilling", "label": "Candidate operation"}],
        actor="author-1",
        reason="Create cross-process review case",
    )
    submitted = service.submit(
        draft["report_id"],
        expected_updated_at=draft["updated_at"],
        actor="author-1",
        reason="Submit before concurrent independent review",
    )
    success, stale = _run_process_race(
        db,
        report_id=draft["report_id"],
        token=submitted["updated_at"],
        action="confirm",
        actors=("reviewer-1", "reviewer-2"),
    )
    assert success == "CONFIRMED"
    assert "stale" in stale
    persisted = service.get(draft["report_id"])
    assert persisted["status"] == "CONFIRMED"
    with db.read_only() as session:
        operations = list(
            session.scalars(
                select(WellOperation).where(WellOperation.report_id == draft["report_id"])
            )
        )
    assert len(operations) == 1 and operations[0].status == "CANDIDATE"
    decision_provenance = [
        item for item in persisted["provenance"] if item.get("source_kind") == "review_decision"
    ]
    assert len(decision_provenance) == 1
    assert decision_provenance[0]["actor"] in {"reviewer-1", "reviewer-2"}
    assert decision_provenance[0]["reason"].startswith("Independent process review by")
    with db.engine.connect() as connection:
        assert list(connection.execute(text("PRAGMA foreign_key_check"))) == []
        assert connection.execute(text("PRAGMA integrity_check")).scalar_one() == "ok"


def test_same_ddr_revision_token_is_claimed_once_across_independent_processes(
    db, committed_well_id
) -> None:
    service = DdrAuthoringService(db)
    draft = service.create_draft(
        well_id=committed_well_id,
        report_date="2026-11-08",
        activities=[{"operation_type": "drilling", "label": "Authored predecessor activity"}],
        actor="declared-author",
        reason="Create process revision race",
    )
    submitted = service.submit(
        draft["report_id"],
        expected_updated_at=draft["updated_at"],
        actor="declared-author",
        reason="Submit before process revision race",
    )
    successor_id, stale = _run_process_race(
        db,
        report_id=draft["report_id"],
        token=submitted["updated_at"],
        action="revision",
        actors=("declared-author", "declared-author"),
    )
    assert "changed since it was loaded" in stale
    history = service.list_reports(committed_well_id, include_history=True)
    assert len(history) == 2
    assert sum(bool(item["is_current"]) for item in history) == 1
    successor = service.get(successor_id)
    predecessor = service.get(draft["report_id"])
    assert successor["supersedes_report_id"] == predecessor["report_id"]
    assert successor["revision_number"] == 2
    assert successor["activities"][0]["label"] == "Authored predecessor activity"
    assert predecessor["is_current"] is False
    with db.read_only() as session:
        reports = list(
            session.scalars(
                select(DdrReport).where(DdrReport.revision_group_id == draft["revision_group_id"])
            )
        )
        activities = list(
            session.scalars(select(DdrActivity).where(DdrActivity.ddr_report_id == successor_id))
        )
        events = list(
            session.scalars(
                select(AuditEvent).where(
                    AuditEvent.subject_type == "ddr_report",
                    AuditEvent.subject_id == draft["report_id"],
                )
            )
        )
    assert len(reports) == 2
    assert len(activities) == 1
    assert [event.action for event in events].count("ddr.revision_superseded") == 1
    assert [event.action for event in events if event.action == "ddr.revision_superseded"] == [
        "ddr.revision_superseded"
    ]
    with db.engine.connect() as connection:
        assert list(connection.execute(text("PRAGMA foreign_key_check"))) == []
        assert connection.execute(text("PRAGMA integrity_check")).scalar_one() == "ok"


def test_process_level_confirm_and_reject_race_persists_exactly_one_report_decision(
    db, committed_well_id
) -> None:
    service = DdrAuthoringService(db)
    draft = service.create_draft(
        well_id=committed_well_id,
        report_date="2026-11-08",
        activities=[{"operation_type": "drilling", "label": "Reviewable authored activity"}],
        actor="declared-author",
        reason="Create spawned report review race",
    )
    submitted = service.submit(
        draft["report_id"],
        expected_updated_at=draft["updated_at"],
        actor="declared-author",
        reason="Submit for independent review",
    )
    status, stale = _run_process_race(
        db,
        report_id=draft["report_id"],
        token=submitted["updated_at"],
        action="confirm",
        actions=("confirm", "reject"),
        actors=("reviewer-one", "reviewer-two"),
    )
    assert status in {"CONFIRMED", "REJECTED"}
    assert "stale" in stale
    persisted = service.get(draft["report_id"])
    assert persisted["status"] == status
    decisions = [
        item for item in persisted["provenance"] if item.get("source_kind") == "review_decision"
    ]
    assert len(decisions) == 1
    assert decisions[0]["actor"] in {"reviewer-one", "reviewer-two"}
    assert decisions[0]["reason"].startswith("Independent process review by")
    with db.read_only() as session:
        operations = list(
            session.scalars(
                select(WellOperation).where(WellOperation.report_id == draft["report_id"])
            )
        )
        events = list(
            session.scalars(
                select(AuditEvent).where(
                    AuditEvent.subject_type == "ddr_report",
                    AuditEvent.subject_id == draft["report_id"],
                )
            )
        )
    assert len(operations) == 1 and operations[0].status == "CANDIDATE"
    assert [event.action for event in events].count("ddr.submitted") == 1
    with db.engine.connect() as connection:
        assert list(connection.execute(text("PRAGMA foreign_key_check"))) == []
        assert connection.execute(text("PRAGMA integrity_check")).scalar_one() == "ok"


def test_process_level_operation_review_race_does_not_confirm_its_parent_ddr(
    db, committed_well_id
) -> None:
    service = DdrAuthoringService(db)
    draft = service.create_draft(
        well_id=committed_well_id,
        report_date="2026-11-08",
        activities=[{"operation_type": "drilling", "label": "Candidate operation for race"}],
        actor="declared-author",
        reason="Create spawned operation review race",
    )
    submitted = service.submit(
        draft["report_id"],
        expected_updated_at=draft["updated_at"],
        actor="declared-author",
        reason="Create candidate operation",
    )
    operation_id = submitted["activities"][0]["operation_id"]
    with db.read_only() as session:
        operation = session.get(WellOperation, operation_id)
        assert operation is not None
        operation_token = operation.updated_at.isoformat()
    status, stale = _run_process_race(
        db,
        report_id=operation_id,
        token=operation_token,
        action="operation_confirm",
        actions=("operation_confirm", "operation_reject"),
        actors=("operation-reviewer-one", "operation-reviewer-two"),
    )
    assert status in {"CONFIRMED", "REJECTED"}
    assert "stale" in stale
    with db.read_only() as session:
        report = session.get(DdrReport, draft["report_id"])
        operation = session.get(WellOperation, operation_id)
        events = list(
            session.scalars(
                select(AuditEvent).where(
                    AuditEvent.subject_type == "well_operation",
                    AuditEvent.subject_id == operation_id,
                )
            )
        )
    assert report is not None and report.status == "CANDIDATE"
    assert report.authoring_state == "SUBMITTED"
    assert operation is not None and operation.status == status
    assert len(events) == 1 and events[0].action == "ddr.activity_submitted"
    status_history = operation.attributes["status_history"]
    assert len(status_history) == 1
    assert status_history[0]["by"] in {"operation-reviewer-one", "operation-reviewer-two"}
    assert status_history[0]["reason"].startswith("Independent process review by")
    with db.engine.connect() as connection:
        assert list(connection.execute(text("PRAGMA foreign_key_check"))) == []
        assert connection.execute(text("PRAGMA integrity_check")).scalar_one() == "ok"


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
        assert connection.execute(text("PRAGMA integrity_check")).scalar_one() == "ok"


def test_service_generated_revision_comparison_classifies_edit_insert_delete_and_reorder(
    db, committed_well_id
) -> None:
    service = DdrAuthoringService(db)
    before_rows = [
        {"operation_type": "drilling", "label": "Anchor A"},
        {"operation_type": "drilling", "label": "Old edit text", "description": "verbatim old"},
        {"operation_type": "tripping", "label": "Anchor B"},
        {"operation_type": "cementing", "label": "Removed authored row"},
        {"operation_type": "drilling", "label": "Anchor C"},
        {"operation_type": "tripping", "label": "Moved authored row"},
        {"operation_type": "drilling", "label": "Anchor E"},
    ]
    predecessor = service.create_draft(
        well_id=committed_well_id,
        report_date="2026-11-08",
        summary="Exact service-generated predecessor",
        activities=before_rows,
        actor="declared-author",
        reason="Create authored predecessor for comparison",
    )
    submitted = service.submit(
        predecessor["report_id"],
        expected_updated_at=predecessor["updated_at"],
        actor="declared-author",
        reason="Submit before creating the successor revision",
    )
    successor = service.create_revision(
        predecessor["report_id"],
        expected_updated_at=submitted["updated_at"],
        actor="declared-author",
        reason="Create service-generated successor revision",
    )
    successor = service.save_draft(
        successor["report_id"],
        expected_updated_at=successor["updated_at"],
        report_date="2026-11-08",
        summary="Exact service-generated predecessor",
        activities=[
            {"operation_type": "drilling", "label": "Anchor A"},
            {
                "operation_type": "drilling",
                "label": "Corrected edit text",
                "description": "verbatim corrected",
            },
            {"operation_type": "tripping", "label": "Anchor B"},
            {"operation_type": "tripping", "label": "Moved authored row"},
            {"operation_type": "drilling", "label": "Anchor C"},
            {"operation_type": "drilling", "label": "Anchor E"},
            {"operation_type": "cementing", "label": "Inserted authored row"},
        ],
        actor="declared-author",
        reason="Edit, reorder, remove and insert authored text",
    )

    before_compare = service.get(predecessor["report_id"])
    after_compare = service.get(successor["report_id"])
    result = compare_ddr_revisions(before_compare, after_compare)
    statuses = [item["status"] for item in result["activities"]["items"]]
    assert statuses.count("UNCHANGED") == 3
    assert statuses.count("MODIFIED") == 1
    assert statuses.count("REORDERED") == 2
    assert statuses.count("REMOVED") == 1
    assert statuses.count("ADDED") == 1
    assert result["status"] == "OK" and result["conclusive"] is True
    assert result["activities"]["order_changed"] is True
    modification = next(
        item for item in result["activities"]["items"] if item["status"] == "MODIFIED"
    )
    assert modification["previous"]["label"] == "Old edit text"
    assert modification["successor"]["label"] == "Corrected edit text"
    assert modification["changed_fields"] == ["label", "description"]
    before_ids = {row["id"] for row in before_compare["activities"]}
    after_ids = {row["id"] for row in after_compare["activities"]}
    assert before_ids.isdisjoint(after_ids), (
        "revision clone must not imply durable activity identity"
    )
    assert (
        before_compare["activities"][0]["sequence"]
        == after_compare["activities"][0]["sequence"]
        == 1
    )
    assert before_compare["activities"][-1]["sequence"] == 7
    assert after_compare["activities"][-1]["sequence"] == 7

    duplicate = service.create_draft(
        well_id=committed_well_id,
        report_date="2026-11-08",
        activities=[
            {"operation_type": "drilling", "label": "Repeated authored activity"},
            {"operation_type": "drilling", "label": "Repeated authored activity"},
        ],
        actor="declared-author",
        reason="Create repeated-content predecessor",
    )
    duplicate_submitted = service.submit(
        duplicate["report_id"],
        expected_updated_at=duplicate["updated_at"],
        actor="declared-author",
        reason="Submit repeated-content predecessor",
    )
    duplicate_successor = service.create_revision(
        duplicate["report_id"],
        expected_updated_at=duplicate_submitted["updated_at"],
        actor="declared-author",
        reason="Create repeated-content successor",
    )
    duplicate_result = compare_ddr_revisions(
        service.get(duplicate["report_id"]), service.get(duplicate_successor["report_id"])
    )
    assert duplicate_result["status"] == "AMBIGUOUS"
    assert duplicate_result["conclusive"] is False
    assert duplicate_result["has_changes"] is None
    assert duplicate_result["activities"]["items"][0]["reason"] == (
        "repeated_content_has_no_durable_identity"
    )
