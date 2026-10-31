"""Lesson revision chain and governed deletion (§5)."""

from __future__ import annotations

from pathlib import Path

import pytest
from sqlalchemy import select

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
    with pytest.raises(Exception):  # noqa: B017 - any validation failure is acceptable here
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
