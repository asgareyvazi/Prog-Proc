"""Append-only audit trail: the policy, enforced by code (master spec section 85).

Two things are easy to get wrong here, so both are made impossible:

*   *updating* an audit row "to fix a typo" - history is what an engineer can rely on
    months later, so it is written once and never rewritten;
*   *deleting* audit rows during cleanup, deduplication or a document re-import.

The rule is enforced at the ORM boundary: :func:`install_append_only_policy` registers
``before_update``/``before_delete`` listeners on :class:`AuditEvent`, so *any* session in
the process - repository, service, migration script, future UI action - fails loudly with
:class:`AuditPolicyError` instead of quietly losing evidence.  Correction is additive:
write a new event (e.g. ``audit.corrected``) that references the earlier one.

:meth:`AuditLog.record` is the only write path this module offers, which is what
"append-only repository policy" means in practice: there is simply no update or delete
method to call, and the guards catch anyone who reaches for the session directly.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import event, select
from sqlalchemy.orm import Session

from ..core.errors import DrillingIntelligenceError
from ..core.hashing import utc_now
from ..core.ids import new_id
from .models import AuditEvent

__all__ = ["AuditEvent", "AuditLog", "AuditPolicyError", "install_append_only_policy"]


class AuditPolicyError(DrillingIntelligenceError):
    """Raised when something tries to change or remove audit history."""

    code = "AUDIT_IMMUTABLE"

    def __init__(self, operation: str, *, subject_id: str = "") -> None:
        super().__init__(
            f"audit_event is append-only: {operation} is not permitted"
            + (f" (subject {subject_id})" if subject_id else "")
            + ". Append a correcting event instead.",
            operation=operation,
            subject_id=subject_id,
        )


#: Mutable flag rather than a ``global`` rebind, so the function stays side-effect-only.
_INSTALLED = [False]


def install_append_only_policy() -> bool:
    """Register the guards.  Idempotent; returns True when newly installed."""
    if _INSTALLED[0]:
        return False

    @event.listens_for(AuditEvent, "before_update", propagate=True)
    def _refuse_update(mapper: Any, connection: Any, target: AuditEvent) -> None:
        raise AuditPolicyError("update", subject_id=target.subject_id)

    @event.listens_for(AuditEvent, "before_delete", propagate=True)
    def _refuse_delete(mapper: Any, connection: Any, target: AuditEvent) -> None:
        raise AuditPolicyError("delete", subject_id=target.subject_id)

    _INSTALLED[0] = True
    return True


class AuditLog:
    """The audit trail as an append-only store.

    Deliberately small: ``record`` (append) and the read-side queries.  Anything that
    smells like editing history belongs in a data migration with a written reason, not
    in application code.
    """

    def __init__(self, session: Session) -> None:
        self.session = session

    def record(
        self,
        *,
        action: str,
        subject_type: str,
        subject_id: str,
        detail: dict[str, Any] | None = None,
        actor: str = "system",
        at: Any | None = None,
    ) -> AuditEvent:
        """Append one event.  ``detail`` must be JSON-serialisable and self-explanatory."""
        event_row = AuditEvent(
            id=new_id("aud"),
            at=at or utc_now(),
            actor=actor,
            action=action,
            subject_type=subject_type,
            subject_id=subject_id,
            detail=detail or {},
        )
        self.session.add(event_row)
        return event_row

    def trail(self, subject_type: str, subject_id: str, limit: int = 50) -> list[AuditEvent]:
        return list(
            self.session.execute(
                select(AuditEvent)
                .where(AuditEvent.subject_type == subject_type, AuditEvent.subject_id == subject_id)
                .order_by(AuditEvent.at.desc())
                .limit(limit)
            ).scalars()
        )

    def has_action(self, subject_type: str, subject_id: str, action: str) -> bool:
        """Was this action recorded for this subject?  Used by tests and repair tools."""
        row = self.session.execute(
            select(AuditEvent.id)
            .where(AuditEvent.subject_type == subject_type, AuditEvent.subject_id == subject_id, AuditEvent.action == action)
            .limit(1)
        ).first()
        return row is not None


#: Imported for its side effect: as soon as the persistence layer is loaded, the
#: append-only rule is active for every session in the process.
install_append_only_policy()
