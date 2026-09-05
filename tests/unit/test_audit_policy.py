"""The audit trail is append-only, and that is a *policy* the code enforces (section 85).

Two failure modes are made impossible here rather than discouraged:

*   "fixing" an audit row in place - a month later, an engineer reads this trail to decide
    who told them what and when, and a corrected-by-hand history is worse than no history;
*   deleting audit rows during cleanup or a re-import, which silently removes the evidence
    that the platform did the right thing.

:meth:`AuditLog.record` is the only write the module offers, so there is no update or delete
method to reach for; and because a repository can always be bypassed with
``session.delete(row)``, :func:`install_append_only_policy` also registers ORM-level guards
that refuse an update or delete of any :class:`AuditEvent` in any session.

What is deliberately *not* claimed: a raw SQL statement issued through the engine
(``execute(text("update audit_event ..."))``) does not pass through the ORM and so is not
intercepted.  That is the reach of an ORM-boundary policy, and the repair-tool path that
would need it is a data migration with a written reason, not application code.
"""

from __future__ import annotations

import inspect

import pytest
from sqlalchemy import func, select, text

from drilling_intelligence.core.enums import FileChangeKind
from drilling_intelligence.database.audit import (
    AuditLog,
    AuditPolicyError,
    install_append_only_policy,
)
from drilling_intelligence.database.models import AuditEvent, Document
from drilling_intelligence.documents.repository import DocumentRepository


@pytest.fixture
def trail(session) -> tuple[DocumentRepository, str]:
    repository = DocumentRepository(session)
    document = repository.create_document(
        workspace_id=None,
        identity_path="docs/audit.txt",
        filename="audit.txt",
        extension=".txt",
        mime_type="text/plain",
        size_bytes=4,
        sha256="a" * 64,
    )
    repository.create_version(
        document,
        sha256="a" * 64,
        source_path="docs/audit.txt",
        size_bytes=4,
        parser="text",
        parser_version="1",
        extraction_version="1",
        origin=FileChangeKind.NEW,
    )
    session.flush()
    return repository, document.id


# --------------------------------------------------------------------------- the write path
def test_events_are_appended_and_read_back(session, trail) -> None:
    repository, document_id = trail
    first = repository.audit(action="document.registered", subject_type="document", subject_id=document_id, detail={"sha256": "a" * 64})
    session.flush()
    assert first.id and first.at is not None
    assert repository.audit_trail("document", document_id)[0].action == "document.registered"
    assert repository.audit_log.has_action("document", document_id, "document.registered")
    assert not repository.audit_log.has_action("document", document_id, "document.deleted")


def test_detail_is_stored_verbatim_and_defaulted(session, trail) -> None:
    repository, document_id = trail
    row = repository.audit(action="document.classified", subject_type="document", subject_id=document_id, detail={"kind": "MUD_REPORT", "confidence": 0.91})
    session.flush()
    stored = session.get(AuditEvent, row.id)
    assert stored.detail == {"kind": "MUD_REPORT", "confidence": 0.91}
    assert stored.actor == "system"
    empty = repository.audit(action="document.touched", subject_type="document", subject_id=document_id)
    session.flush()
    assert session.get(AuditEvent, empty.id).detail == {}


# --------------------------------------------------------------------------- no update path
def test_an_edited_audit_row_is_refused(session, trail) -> None:
    repository, document_id = trail
    row = repository.audit(action="document.registered", subject_type="document", subject_id=document_id, detail={"sha256": "a" * 64})
    session.commit()  # written and durable: what follows is an attempt to rewrite history

    row.action = "document.registered_typo_fixed"
    with pytest.raises(AuditPolicyError, match="append-only: update is not permitted") as caught:
        session.flush()
    assert caught.value.code == "AUDIT_IMMUTABLE"
    assert caught.value.context["subject_id"] == document_id
    session.rollback()
    session.expire_all()
    assert session.get(AuditEvent, row.id).action == "document.registered", "the row is untouched, not half-written"


def test_a_deleted_audit_row_is_refused(session, trail) -> None:
    repository, document_id = trail
    row = repository.audit(action="document.version_added", subject_type="document", subject_id=document_id, detail={"version": 1})
    session.commit()
    session.delete(row)
    with pytest.raises(AuditPolicyError, match="delete is not permitted"):
        session.flush()
    session.rollback()
    session.expire_all()
    assert session.get(AuditEvent, row.id) is not None
    assert session.scalar(select(func.count()).select_from(AuditEvent)) == 1, "the refused delete removed nothing"


def test_correction_is_additive(session, trail) -> None:
    """The documented way to fix history: append an event that references the earlier one."""
    repository, document_id = trail
    wrong = repository.audit(action="document.classified", subject_type="document", subject_id=document_id, detail={"kind": "DDR"})
    session.flush()
    right = repository.audit(
        action="audit.corrected",
        subject_type="document",
        subject_id=document_id,
        detail={"replaces": wrong.id, "kind": "MUD_REPORT", "reason": "the label row is a mud report"},
    )
    session.flush()
    actions = [event.action for event in repository.audit_trail("document", document_id, limit=10)]
    assert actions == ["audit.corrected", "document.classified"], "both events are on the trail"
    assert right.detail["replaces"] == wrong.id
    assert session.get(AuditEvent, wrong.id).detail["kind"] == "DDR", "the original claim survives as written"


# --------------------------------------------------------------------------- the contract itself
def test_the_log_offers_no_way_to_rewrite_history() -> None:
    """The public surface is record + reads; nothing named like an edit or a purge exists."""
    names = {name for name, _ in inspect.getmembers(AuditLog, predicate=inspect.isfunction) if not name.startswith("_")}
    assert names == {"record", "trail", "has_action"}, names
    forbidden = [name for name in names if any(token in name.lower() for token in ("update", "delete", "remove", "purge", "edit", "clear", "set"))]
    assert forbidden == []


def test_the_repository_exposes_audit_only_as_an_append(session) -> None:
    """``DocumentRepository`` must not grow a way to rewrite the trail either."""
    repository = DocumentRepository(session)
    exposed = {name for name in dir(repository) if "audit" in name.lower()}
    assert exposed == {"audit", "audit_log", "audit_trail"}, exposed
    # The two audit methods must be an append and a read - nothing that rewrites or drops.
    # (The repository does delete rows elsewhere: stale cache entries.  That is why this
    # check is scoped to the audit methods instead of grepping the whole module.)
    for name in ("audit", "audit_trail"):
        body = inspect.getsource(getattr(DocumentRepository, name))
        forbidden = [token for token in ("session.delete", "session.execute", "update audit_event", "record.update") if token in body]
        assert forbidden == [], f"DocumentRepository.{name} must not {forbidden}: {body}"


def test_the_policy_is_installed_once_and_is_idempotent() -> None:
    """Re-import must not stack listeners (each would raise once per flush, harmlessly but noisily)."""
    assert install_append_only_policy() is False, "the guard is installed on import of the persistence layer"


def test_the_guard_covers_any_session_in_the_process(db, trail) -> None:
    """Not just the fixture's session: the listener is on the mapper, process-wide."""
    repository, document_id = trail
    row = repository.audit(action="document.registered", subject_type="document", subject_id=document_id)
    repository.session.commit()
    with db.session() as other:
        found = other.get(AuditEvent, row.id)
        assert found is not None
        found.detail = {"tampered": True}
        with pytest.raises(AuditPolicyError):
            other.flush()
        other.rollback()


def test_an_ordinary_document_update_is_not_collateral_damage(session, trail) -> None:
    """The guard is scoped to AuditEvent: the registry still writes to its own tables."""
    repository, document_id = trail
    document = session.get(Document, document_id)
    document.filename = "renamed.txt"
    repository.audit(action="document.renamed", subject_type="document", subject_id=document_id, detail={"to": "renamed.txt"})
    session.flush()
    session.commit()
    assert session.get(Document, document_id).filename == "renamed.txt"
    assert repository.audit_log.has_action("document", document_id, "document.renamed")


# --------------------------------------------------------------------------- what is not claimed
def test_raw_sql_is_outside_the_reach_of_an_orm_policy(session, trail) -> None:
    """Honest boundary: this is how a migration repairs data, and it must never touch audit.

    Asserting the limit of the guarantee is the point - a reader who assumes the database
    itself refuses the write would be wrong, and only the discipline of "no audit writes in
    raw SQL" protects the trail.
    """
    repository, document_id = trail
    repository.audit(action="document.registered", subject_type="document", subject_id=document_id)
    session.commit()
    session.execute(text("update audit_event set actor = 'someone-else' where actor = 'system'"))
    session.expire_all()
    row = session.execute(select(AuditEvent).limit(1)).scalar_one()
    assert row.actor == "someone-else", "the ORM guard cannot see this; the codebase must not do it"
