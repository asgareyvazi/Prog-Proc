"""Lesson revision chain and governed deletion (§5)."""

from __future__ import annotations

from pathlib import Path

import pytest
from sqlalchemy import select

from drilling_intelligence.core.errors import ValidationError
from drilling_intelligence.database.models import KnowledgeRelation, LessonLearned
from drilling_intelligence.lessons.repository import LessonRepository
from drilling_intelligence.wells.workspace import Workspace


@pytest.fixture
def ws(tmp_path: Path) -> Workspace:
    from drilling_intelligence.cli.app import main

    root = tmp_path / "ws"
    assert main(["workspace", "create", str(root)]) == 0
    w = Workspace.open(root)
    _ = w.database
    yield w
    w.close()


def _repo(ws: Workspace):
    sess = ws.database.session()
    return LessonRepository(sess), sess


def _chain(ws: Workspace, code: str = "LL-CHAIN"):
    repo, sess = _repo(ws)
    les = repo.capture(lesson="v1", code=code, title="v1")
    sess.commit()
    return les, repo, sess


def test_delete_only_revision(ws: Workspace) -> None:
    repo, sess = _repo(ws)
    les = repo.capture(lesson="only", code="LL-ONLY", title="t")
    sess.commit()
    # Attach evidence
    repo.attach_evidence(les.id, knowledge_item_ids=[])
    sess.commit()
    res = repo.delete_lesson(les.id, by="actor", reason="no longer needed")
    sess.commit()
    assert res["chain_length"] == 1 and res["position"] == 0
    assert sess.get(LessonLearned, les.id) is None
    # Audit exists
    from drilling_intelligence.database.audit import AuditLog

    assert AuditLog(sess).has_action("lesson_learned", les.id, "lesson.delete")
    sess.close()


def test_delete_earliest_of_two(ws: Workspace) -> None:
    repo, sess = _repo(ws)
    les = repo.capture(lesson="v1", code="LL-EARLY", title="v1")
    sess.commit()
    rev = repo.revise(les.id, by="a", changes={"lesson": "v2"})
    sess.commit()
    # Delete earliest (non-current)
    res = repo.delete_lesson(les.id, by="a", reason="earliest")
    sess.commit()
    assert res["successor_id"] == rev.id
    remaining = sess.get(LessonLearned, rev.id)
    assert remaining.supersedes_id is None
    assert remaining.is_current is True
    sess.close()


def test_delete_middle_of_three(ws: Workspace) -> None:
    repo, sess = _repo(ws)
    les = repo.capture(lesson="v1", code="LL-MID3", title="v1")
    sess.commit()
    r2 = repo.revise(les.id, by="a", changes={"lesson": "v2"})
    sess.commit()
    r3 = repo.revise(r2.id, by="a", changes={"lesson": "v3"})
    sess.commit()
    # Delete middle (r2)
    repo.delete_lesson(r2.id, by="a", reason="middle")
    sess.commit()
    r3_after = sess.get(LessonLearned, r3.id)
    assert r3_after.supersedes_id == les.id
    assert sess.get(LessonLearned, r2.id) is None
    sess.close()


def test_delete_latest_current_restores_predecessor(ws: Workspace) -> None:
    repo, sess = _repo(ws)
    les = repo.capture(lesson="v1", code="LL-LATEST", title="v1")
    sess.commit()
    # Approve first to test status restoration path
    # Use repository approve (needs evidence and different reviewer)
    # Instead just revise and delete latest to see predecessor restored
    r2 = repo.revise(les.id, by="a", changes={"lesson": "v2"})
    sess.commit()
    # Delete latest current (r2) — predecessor les should become current
    repo.delete_lesson(r2.id, by="a", reason="latest")
    sess.commit()
    les_after = sess.get(LessonLearned, les.id)
    assert les_after.is_current is True
    # Status should have been restored from SUPERSEDED to DRAFT (since not approved)
    assert les_after.status == "DRAFT"
    sess.close()


def test_delete_with_evidence_source_and_target(ws: Workspace) -> None:
    repo, sess = _repo(ws)
    les = repo.capture(lesson="v1", code="LL-EVI", title="t")
    sess.commit()
    # Create evidence edges manually

    # Insert dummy knowledge item
    with ws.database.engine.begin() as conn:
        from sqlalchemy import text

        conn.execute(
            text(
                "INSERT INTO knowledge_item (id, item_type, title, content, domain, lookup_key, value, unit, record_state, status, revision, origin, value_type, created_by, created_at, updated_at) "
                "VALUES ('ki-ev', 'FACT', 't', 'c', 'mud', 'k', 'v', '', 'ACTUAL', 'ACTIVE', 1, 'MANUAL', 'text', 'system', datetime('now'), datetime('now'))"
            )
        )
    repo.attach_evidence(les.id, knowledge_item_ids=["ki-ev"])
    sess.commit()
    # Verify edge exists
    with ws.database.read_only() as ro:
        edges = list(
            ro.execute(
                select(KnowledgeRelation).where(KnowledgeRelation.source_id == les.id)
            ).scalars()
        )
        assert len(edges) == 1
    # Delete lesson — edges should be removed
    repo2, sess2 = _repo(ws)
    # Need to get fresh les id
    les_id = les.id
    repo2.delete_lesson(les_id, by="a", reason="with evidence")
    sess2.commit()
    with ws.database.read_only() as ro:
        edges = list(
            ro.execute(
                select(KnowledgeRelation).where(KnowledgeRelation.source_id == les_id)
            ).scalars()
        )
        assert len(edges) == 0
        edges2 = list(
            ro.execute(
                select(KnowledgeRelation).where(KnowledgeRelation.target_id == les_id)
            ).scalars()
        )
        assert len(edges2) == 0
    sess2.close()


def test_bulk_delete_rollback_on_failure(ws: Workspace) -> None:
    repo, sess = _repo(ws)
    a = repo.capture(lesson="a", code="LL-BULK-A", title="t")
    b = repo.capture(lesson="b", code="LL-BULK-B", title="t")
    sess.commit()
    # Bulk delete where second id is invalid — should fail before mutating
    with pytest.raises(ValidationError, match="no lesson"):
        repo.delete_lessons([a.id, "no-such-id"], by="actor", reason="bulk")
    sess.commit()
    sess.rollback()
    # Both original rows must still exist
    assert sess.get(LessonLearned, a.id) is not None
    assert sess.get(LessonLearned, b.id) is not None
    # Audit for first must not have been left behind after rollback
    from drilling_intelligence.database.audit import AuditLog

    # After rollback, no audit for a should exist (since transaction rolled back)
    # Check in a new session
    sess.close()
    with ws.database.read_only() as ro:
        assert not AuditLog(ro).has_action("lesson_learned", a.id, "lesson.delete")
    # Now successful bulk delete
    repo2, sess2 = _repo(ws)
    res = repo2.delete_lessons([a.id, b.id], by="actor", reason="bulk ok")
    sess2.commit()
    assert len(res) == 2
    assert sess2.get(LessonLearned, a.id) is None
    assert sess2.get(LessonLearned, b.id) is None
    sess2.close()


@pytest.mark.parametrize(
    ("initial_state", "expected_state"),
    [
        ("DRAFT", "DRAFT"),
        ("REVIEW", "REVIEW"),
        ("APPROVED", "APPROVED"),
        ("REJECTED", "REJECTED"),
    ],
)
def test_revision_restores_exact_persisted_lifecycle_state(
    ws: Workspace, initial_state: str, expected_state: str
) -> None:
    repo, session = _repo(ws)
    lesson = repo.capture(
        lesson="stateful lesson",
        code=f"LL-STATE-{initial_state}",
        title="stateful",
        created_by="author",
        provenance=[{"source": "work-order-7", "locator": "row-3"}],
    )
    session.commit()
    if initial_state == "REVIEW":
        repo.submit_for_review(lesson.id, by="reviewer")
    elif initial_state == "APPROVED":
        repo.approve(lesson.id, by="reviewer", note="verified evidence")
    elif initial_state == "REJECTED":
        repo.reject(lesson.id, by="reviewer", reason="not supported by the source")
    session.commit()
    assert lesson.status == expected_state

    revised = repo.revise(lesson.id, by="author-2", changes={"lesson": "edited text"})
    session.commit()
    assert session.get(LessonLearned, lesson.id).prior_status == expected_state
    assert revised.provenance == []  # revision evidence must be explicitly re-attached

    repo.delete_lesson(revised.id, by="owner", reason="revert this revision")
    session.commit()
    restored = session.get(LessonLearned, lesson.id)
    assert restored is not None
    assert restored.is_current is True
    assert restored.status == expected_state
    assert restored.prior_status is None
    assert restored.provenance == [{"source": "work-order-7", "locator": "row-3"}]
    session.close()


def test_approved_lesson_delete_is_audited_withdrawal_and_search_excludes_it(
    ws: Workspace,
) -> None:
    from drilling_intelligence.database.audit import AuditLog

    repo, session = _repo(ws)
    approved = repo.capture(
        lesson="preserve approved content",
        code="LL-APPROVED-RETENTION",
        title="retained decision",
        created_by="author",
        provenance=[{"source": "DDR-77", "page": 4, "hash": "abc123"}],
    )
    session.commit()
    repo.approve(approved.id, by="independent-reviewer", note="evidence checked")
    session.commit()

    result = repo.delete_lesson(approved.id, by="owner", reason="decision withdrawn")
    session.commit()
    retained = session.get(LessonLearned, approved.id)
    assert result["disposition"] == "WITHDRAWN"
    assert retained is not None
    assert retained.lesson == "preserve approved content"
    assert retained.provenance == [{"source": "DDR-77", "page": 4, "hash": "abc123"}]
    assert retained.status == "WITHDRAWN" and retained.is_current is False
    assert repo.list_lessons(search="preserve approved content") == []
    assert repo.list_lessons(search="preserve approved content", include_withdrawn=True) == [
        retained
    ]

    audit = AuditLog(session).trail("lesson_learned", approved.id)
    event = next(item for item in audit if item.action == "lesson.withdraw")
    detail = event.detail
    assert event.actor == "owner"
    assert detail["reason"] == "decision withdrawn"
    assert detail["code_key"] == "ll-approved-retention"
    assert detail["original_lifecycle_state"] == "APPROVED"
    assert detail["original_current"] is True
    assert detail["final_disposition"] == "WITHDRAWN"
    assert detail["lesson_snapshot"]["lesson"] == "preserve approved content"
    assert detail["lesson_snapshot"]["provenance"] == retained.provenance
    assert "evidence_count" in detail and "evidence_edges" in detail
    session.close()


def test_superseded_approved_history_is_never_hard_deleted(ws: Workspace) -> None:
    repo, session = _repo(ws)
    lesson = repo.capture(
        lesson="approved historic decision",
        code="LL-APPROVED-HISTORY",
        title="history",
        created_by="author",
        provenance=[{"source": "report", "row": 9}],
    )
    session.commit()
    repo.approve(lesson.id, by="reviewer")
    session.commit()
    successor = repo.revise(lesson.id, by="author-2", changes={"lesson": "new wording"})
    session.commit()
    assert lesson.status == "SUPERSEDED" and lesson.prior_status == "APPROVED"

    result = repo.delete_lesson(lesson.id, by="owner", reason="withdraw old approved decision")
    session.commit()
    retained = session.get(LessonLearned, lesson.id)
    assert result["disposition"] == "WITHDRAWN"
    assert retained is not None and retained.status == "WITHDRAWN"
    assert retained.lesson == "approved historic decision"
    assert retained.supersedes_id is None
    assert session.get(LessonLearned, successor.id) is not None
    session.close()


@pytest.mark.parametrize(
    ("corruption", "diagnostic"),
    [
        ("cycle", "cycle"),
        ("branch", "branching successors"),
        ("dangling", "dangling predecessor"),
        ("revision_order", "revision order"),
        ("multiple_current", "multiple current"),
        ("bad_lifecycle", "current row has inactive lifecycle"),
    ],
)
def test_malformed_revision_graphs_refuse_destructive_operations(
    ws: Workspace, corruption: str, diagnostic: str
) -> None:
    import sqlite3

    from drilling_intelligence.core.errors import ValidationError

    repo, session = _repo(ws)
    root = repo.capture(lesson="root", code=None, title="root")
    session.commit()
    second = repo.revise(root.id, by="author", changes={"lesson": "second"})
    session.commit()
    if corruption == "cycle":
        root.supersedes_id = second.id
    elif corruption == "branch":
        branch = repo.capture(lesson="branch", code=None, title="branch")
        session.flush()
        branch.supersedes_id = root.id
    elif corruption == "revision_order":
        second.revision = root.revision
    elif corruption == "multiple_current":
        root.is_current = True
    elif corruption == "bad_lifecycle":
        second.status = "SUPERSEDED"
        second.prior_status = "DRAFT"
    session.commit()
    session.close()

    if corruption == "dangling":
        database_path = ws.database.engine.url.database
        connection = sqlite3.connect(database_path)
        try:
            connection.execute("PRAGMA foreign_keys=OFF")
            connection.execute(
                "UPDATE lesson_learned SET supersedes_id='missing-parent' WHERE id=?",
                (second.id,),
            )
            connection.commit()
        finally:
            connection.close()

    repo2, session2 = _repo(ws)
    with pytest.raises(ValidationError, match=diagnostic):
        repo2.delete_lesson(root.id, by="owner", reason="must refuse corruption")
    assert session2.get(LessonLearned, root.id) is not None
    session2.close()


def test_revision_gaps_are_valid_and_batch_order_uses_the_graph(ws: Workspace) -> None:
    from drilling_intelligence.database.audit import AuditEvent

    repo, session = _repo(ws)
    first = repo.capture(lesson="v1", code=None, title="v1")
    session.commit()
    second = repo.revise(first.id, by="author", changes={"lesson": "v2"})
    session.commit()
    third = repo.revise(second.id, by="author", changes={"lesson": "v3"})
    session.commit()
    fourth = repo.revise(third.id, by="author", changes={"lesson": "v4"})
    session.commit()
    first.revision, second.revision, third.revision, fourth.revision = 1, 4, 20, 90
    session.commit()

    results = repo.delete_lessons(
        [first.id, second.id, third.id, fourth.id, third.id],
        by="owner",
        reason="remove whole test chain",
    )
    session.commit()
    assert [item["lesson_id"] for item in results] == [
        fourth.id,
        third.id,
        second.id,
        first.id,
    ]
    assert all(
        session.get(LessonLearned, row_id) is None
        for row_id in (first.id, second.id, third.id, fourth.id)
    )
    events = list(
        session.execute(
            select(AuditEvent)
            .where(AuditEvent.action == "lesson.delete")
            .order_by(AuditEvent.at.asc())
        ).scalars()
    )
    relevant = [
        event.subject_id
        for event in events
        if event.subject_id in {first.id, second.id, third.id, fourth.id}
    ]
    assert relevant == [fourth.id, third.id, second.id, first.id]
    session.close()


def test_batch_delete_across_independent_chains_and_duplicate_ids(ws: Workspace) -> None:
    repo, session = _repo(ws)
    a = repo.capture(lesson="a1", code="LL-INDEP-A", title="a1")
    b = repo.capture(lesson="b1", code="LL-INDEP-B", title="b1")
    session.commit()
    a2 = repo.revise(a.id, by="author", changes={"lesson": "a2"})
    b2 = repo.revise(b.id, by="author", changes={"lesson": "b2"})
    session.commit()

    results = repo.delete_lessons([a.id, a2.id, b.id, b2.id, a.id], by="owner", reason="two chains")
    session.commit()
    assert len(results) == 4
    assert {item["lesson_id"] for item in results} == {a.id, a2.id, b.id, b2.id}
    assert all(session.get(LessonLearned, row_id) is None for row_id in (a.id, a2.id, b.id, b2.id))
    session.close()


def test_batch_savepoint_rolls_back_after_real_mid_operation_failure(
    ws: Workspace, monkeypatch
) -> None:
    from drilling_intelligence.database.audit import AuditLog

    repo, session = _repo(ws)
    rows = [
        repo.capture(lesson=f"lesson-{i}", code=f"LL-MIDFAIL-{i}", title=f"t{i}") for i in range(3)
    ]
    session.commit()
    original = repo._delete_validated_lesson
    called = 0

    def fail_after_two_mutations(lesson_id: str, *, actor: str, reason: str):
        nonlocal called
        result = original(lesson_id, actor=actor, reason=reason)
        called += 1
        if called == 2:
            raise RuntimeError("injected failure after two flushed deletions")
        return result

    monkeypatch.setattr(repo, "_delete_validated_lesson", fail_after_two_mutations)
    with pytest.raises(RuntimeError, match="after two flushed deletions"):
        repo.delete_lessons([row.id for row in rows], by="owner", reason="atomic fault injection")
    session.expire_all()
    assert called == 2
    assert all(session.get(LessonLearned, row.id) is not None for row in rows)
    assert all(
        not AuditLog(session).has_action("lesson_learned", row.id, "lesson.delete") for row in rows
    )
    assert all(session.get(LessonLearned, row.id).is_current for row in rows)
    session.close()


def test_restore_never_moves_revision_specific_evidence_edges(ws: Workspace) -> None:
    from sqlalchemy import text

    repo, session = _repo(ws)
    root = repo.capture(
        lesson="text tied to source evidence",
        code="LL-EVIDENCE-OWNERSHIP",
        title="source-bound",
        provenance=[{"source": "manual-report", "locator": "table-2"}],
    )
    session.commit()
    with ws.database.engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO knowledge_item (id, item_type, title, content, domain, lookup_key, value, unit, record_state, status, revision, origin, value_type, created_by, created_at, updated_at) "
                "VALUES ('ki-owner', 'FACT', 'evidence', 'source fact', 'lesson', 'evidence-key', 'fact', '', 'ACTUAL', 'ACTIVE', 1, 'MANUAL', 'text', 'system', datetime('now'), datetime('now'))"
            )
        )
    repo.attach_evidence(root.id, knowledge_item_ids=["ki-owner"])
    session.commit()
    edge_before = session.execute(
        select(KnowledgeRelation).where(
            KnowledgeRelation.source_type == "lesson", KnowledgeRelation.source_id == root.id
        )
    ).scalar_one()

    revised = repo.revise(root.id, by="author", changes={"lesson": "materially different wording"})
    session.commit()
    assert session.get(KnowledgeRelation, edge_before.id).source_id == root.id
    assert repo.evidence_count(revised.id) == 0

    repo.delete_lesson(revised.id, by="owner", reason="revert wording")
    session.commit()
    restored = session.get(LessonLearned, root.id)
    assert restored is not None and restored.is_current
    assert restored.provenance == [{"source": "manual-report", "locator": "table-2"}]
    assert session.get(KnowledgeRelation, edge_before.id).source_id == root.id
    session.close()


def test_approved_withdrawal_keeps_evidence_edges_and_records_them(ws: Workspace) -> None:
    from sqlalchemy import text

    from drilling_intelligence.database.audit import AuditLog

    repo, session = _repo(ws)
    lesson = repo.capture(
        lesson="approved with graph evidence",
        code="LL-WITHDRAW-EVIDENCE",
        title="evidence retained",
        created_by="author",
        provenance=[{"source": "report", "page": 2}],
    )
    session.commit()
    with ws.database.engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO knowledge_item (id, item_type, title, content, domain, lookup_key, value, unit, record_state, status, revision, origin, value_type, created_by, created_at, updated_at) "
                "VALUES ('ki-withdraw', 'FACT', 'evidence', 'source fact', 'lesson', 'withdraw-key', 'fact', '', 'ACTUAL', 'ACTIVE', 1, 'MANUAL', 'text', 'system', datetime('now'), datetime('now'))"
            )
        )
    repo.attach_evidence(lesson.id, knowledge_item_ids=["ki-withdraw"])
    session.commit()
    repo.approve(lesson.id, by="reviewer")
    session.commit()
    edge_ids = [edge.id for edge in repo._evidence_edges(lesson.id)]

    repo.delete_lesson(lesson.id, by="owner", reason="withdraw decision")
    session.commit()
    assert all(session.get(KnowledgeRelation, edge_id) is not None for edge_id in edge_ids)
    event = next(
        item
        for item in AuditLog(session).trail("lesson_learned", lesson.id)
        if item.action == "lesson.withdraw"
    )
    assert event.detail["evidence_count"] == len(edge_ids)
    assert len(event.detail["evidence_edges"]) == len(edge_ids)
    assert event.detail["evidence_edges"][0]["target_id"] == "ki-withdraw"
    session.close()


def test_unapproved_delete_audit_has_complete_chain_and_evidence_context(ws: Workspace) -> None:
    from drilling_intelligence.database.audit import AuditLog

    repo, session = _repo(ws)
    root = repo.capture(
        lesson="audit root", code=None, title="root", provenance=[{"source": "log", "row": 4}]
    )
    session.commit()
    child = repo.revise(root.id, by="author", changes={"lesson": "audit child"})
    session.commit()
    result = repo.delete_lesson(child.id, by="owner", reason="remove draft")
    session.commit()
    event = next(
        item
        for item in AuditLog(session).trail("lesson_learned", child.id)
        if item.action == "lesson.delete"
    )
    assert result["disposition"] == "DELETED"
    assert event.actor == "owner"
    assert event.at is not None
    assert event.detail["code_key"] is None
    assert event.detail["revision"] == child.revision
    assert event.detail["original_lifecycle_state"] == "DRAFT"
    assert event.detail["original_current"] is True
    assert event.detail["chain_position"] == 1
    assert event.detail["chain_length"] == 2
    assert event.detail["predecessor_id"] == root.id
    assert event.detail["successor_id"] is None
    assert event.detail["final_disposition"] == "DELETED"
    assert event.detail["lesson_snapshot"]["lesson"] == "audit child"
    assert event.detail["lesson_snapshot"]["supersedes_id"] == root.id
    session.close()


def test_code_can_be_reused_after_unapproved_singleton_is_deleted(ws: Workspace) -> None:
    repo, session = _repo(ws)
    first = repo.capture(lesson="first", code="LL-REUSE", title="first")
    session.commit()
    repo.delete_lesson(first.id, by="owner", reason="duplicate draft")
    session.commit()
    replacement = repo.capture(lesson="replacement", code=" ll-reuse ", title="replacement")
    session.commit()
    assert replacement.code_key == "ll-reuse"
    assert replacement.is_current
    session.close()


def test_dangling_bulk_request_is_atomic_and_does_not_audit_partial_work(ws: Workspace) -> None:
    from drilling_intelligence.database.audit import AuditLog

    repo, session = _repo(ws)
    first = repo.capture(lesson="first", code="LL-BULK-VALID", title="first")
    session.commit()
    from drilling_intelligence.core.errors import ValidationError

    with pytest.raises(ValidationError, match="no lesson"):
        repo.delete_lessons([first.id, "missing-lesson-id"], by="owner", reason="missing request")
    session.rollback()
    assert session.get(LessonLearned, first.id) is not None
    assert not AuditLog(session).has_action("lesson_learned", first.id, "lesson.delete")
    session.close()


def test_legacy_superseded_state_backfill_does_not_guess_review_or_rejection(ws: Workspace) -> None:
    """0023 carries only positive approval evidence; other legacy states remain UNKNOWN."""
    from sqlalchemy import text

    from drilling_intelligence.database.engine import open_sqlite
    from drilling_intelligence.database.migrations import upgrade

    path = ws.database.engine.url.database
    engine = open_sqlite(f"sqlite:///{path}")
    try:
        upgrade(engine, "0022", allow_downgrade=True)
        # Insert the two migration fixtures only after returning to the 0022 schema.
        with engine.begin() as connection:
            for lesson_id in ("les-legacy-approved", "les-legacy-unknown"):
                connection.execute(
                    text(
                        "INSERT INTO lesson_learned "
                        "(id, code, code_key, revision, is_current, status, title, context, "
                        "observation, root_cause_status, lesson, recommendation, depth_from_unit, "
                        "depth_to_unit, created_by, origin, created_at, updated_at) "
                        "VALUES (:id, NULL, NULL, 1, 0, 'SUPERSEDED', 'legacy', '', '', 'UNKNOWN', "
                        "'legacy text', '', 'm', 'm', 'author', 'MANUAL', CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)"
                    ),
                    {"id": lesson_id},
                )
            connection.execute(
                text(
                    "UPDATE lesson_learned SET approved_by='reviewer', reviewer='reviewer', "
                    "reviewed_at=CURRENT_TIMESTAMP WHERE id='les-legacy-approved'"
                )
            )
            connection.execute(
                text(
                    "UPDATE lesson_learned SET approved_by=NULL, reviewer='reviewer', "
                    "reviewed_at=CURRENT_TIMESTAMP WHERE id='les-legacy-unknown'"
                )
            )
        upgrade(engine, "head")
        with engine.connect() as connection:
            rows = dict(
                connection.execute(
                    text("SELECT id, prior_status FROM lesson_learned WHERE id LIKE 'les-legacy-%'")
                ).all()
            )
        assert rows == {
            "les-legacy-approved": "APPROVED",
            "les-legacy-unknown": "UNKNOWN",
        }
    finally:
        engine.dispose()


def test_source_target_duplicate_and_unrelated_evidence_edges_are_disposed_exactly(
    ws: Workspace,
) -> None:
    from sqlalchemy import text

    repo, session = _repo(ws)
    lesson = repo.capture(lesson="delete with two edge directions", code=None, title="edges")
    unrelated = repo.capture(lesson="unrelated edge owner", code=None, title="other")
    session.commit()
    with ws.database.engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO knowledge_item (id, item_type, title, content, domain, lookup_key, value, unit, record_state, status, revision, origin, value_type, created_by, created_at, updated_at) "
                "VALUES ('ki-two-way', 'FACT', 'evidence', 'source fact', 'lesson', 'two-way-key', 'fact', '', 'ACTUAL', 'ACTIVE', 1, 'MANUAL', 'text', 'system', datetime('now'), datetime('now'))"
            )
        )
    repo.attach_evidence(lesson.id, knowledge_item_ids=["ki-two-way"])
    session.commit()
    repo.attach_evidence(lesson.id, knowledge_item_ids=["ki-two-way"])
    session.commit()
    forward_edges = list(
        session.execute(
            select(KnowledgeRelation).where(
                KnowledgeRelation.source_type == "lesson",
                KnowledgeRelation.source_id == lesson.id,
                KnowledgeRelation.target_id == "ki-two-way",
            )
        ).scalars()
    )
    assert len(forward_edges) == 1, "reattaching an identical edge must not duplicate it"
    forward = session.execute(
        select(KnowledgeRelation).where(
            KnowledgeRelation.source_type == "lesson",
            KnowledgeRelation.source_id == lesson.id,
            KnowledgeRelation.target_id == "ki-two-way",
        )
    ).scalar_one()
    reverse = KnowledgeRelation(
        id="edge-knowledge-to-lesson",
        source_type="knowledge_item",
        source_id="ki-two-way",
        relation="LESSON_DERIVED_FROM_EVENT",
        target_type="lesson",
        target_id=lesson.id,
    )
    other = KnowledgeRelation(
        id="edge-unrelated-lesson",
        source_type="lesson",
        source_id=unrelated.id,
        relation="LESSON_CITES_EVIDENCE",
        target_type="knowledge_item",
        target_id="ki-two-way",
    )
    session.add_all([reverse, other])
    session.commit()

    repo.delete_lesson(lesson.id, by="owner", reason="remove unapproved lesson")
    session.commit()
    assert session.get(KnowledgeRelation, forward.id) is None
    assert session.get(KnowledgeRelation, reverse.id) is None
    assert session.get(KnowledgeRelation, other.id) is not None
    session.close()
