"""Explicit human action boundary for the read-only Domain Review.

This module is intentionally an adapter, not a workflow engine.  It has no action table and no
persistence of its own.  Capabilities are derived from the existing lifecycle machines and the
record's authoritative review values; execution re-reads the exact row, checks the review's
revision/status/scope preconditions, and delegates the write to the repository or service that owns
that domain rule.

The UI can therefore be a safe client of this boundary without learning how a lesson, a pattern, a
recommendation, a risk, or an operational record is stored.  A failed or stale request raises before
the owning method is called, and the database unit of work rolls the whole action back.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from types import MappingProxyType
from typing import Any

from sqlalchemy.orm import Session

from ..core.enums import RecommendationLifecycle, RiskLifecycle
from ..core.errors import ValidationError
from ..core.lifecycle import (
    CONFIRMATION_LIFECYCLE,
    LESSON_LIFECYCLE,
    PROCEDURE_LIFECYCLE,
    PROGRAM_LIFECYCLE,
    RECOMMENDATION_LIFECYCLE,
    RISK_LIFECYCLE,
    Lifecycle,
)
from ..database.models import (
    DdrReport,
    FieldPattern,
    KnowledgeConflict,
    NptRecord,
    ProblemOccurrence,
    WellEvent,
    WellOperation,
)
from ..engineering.costs import CostRepository
from ..engineering.repository import EngineeringRepository
from ..engineering.risk import RiskRepository
from ..intelligence.service import IntelligenceService
from ..knowledge.service import KnowledgeExtractionService
from ..lessons.repository import LessonRepository
from ..operations.repository import OperationsRepository
from ..review.contract import ReviewConflict, ReviewRecord

__all__ = [
    "ActionCapability",
    "ActionRequest",
    "ActionResult",
    "ReviewAction",
    "ReviewActionError",
    "ReviewActionRequest",
    "ReviewActionResult",
    "ReviewActionService",
]


class ReviewActionError(ValidationError):
    """A human action was not safe to execute from the displayed review state."""

    code = "REVIEW_ACTION"


# The review contract names are table names.  Short aliases make the headless API convenient while
# keeping the canonical value in every request/result and in the UI.
_RECORD_ALIASES = {
    "procedure": "procedure_record",
    "program": "drilling_program",
    "lesson": "lesson_learned",
    "practice": "best_practice",
    "pattern": "field_pattern",
    "risk": "risk_record",
    "recommendation": "recommendation",
    "cost": "cost_item",
    "conflict": "knowledge_conflict",
}

_OPERATIONAL_MODELS: dict[str, type] = {
    model.__tablename__: model
    for model in (DdrReport, WellOperation, WellEvent, NptRecord, ProblemOccurrence)
}

_LIFECYCLES: dict[str, Lifecycle] = {
    "procedure_record": PROCEDURE_LIFECYCLE,
    "drilling_program": PROGRAM_LIFECYCLE,
    "lesson_learned": LESSON_LIFECYCLE,
    "best_practice": PROCEDURE_LIFECYCLE,
    "recommendation": RECOMMENDATION_LIFECYCLE,
    "risk_record": RISK_LIFECYCLE,
    "field_pattern": CONFIRMATION_LIFECYCLE,
    "cost_item": CONFIRMATION_LIFECYCLE,
    **dict.fromkeys(_OPERATIONAL_MODELS, CONFIRMATION_LIFECYCLE),
}


@dataclass(frozen=True)
class ReviewAction:
    """One domain-derived action a reviewer may explicitly confirm."""

    action_id: str
    label: str
    target_status: str
    confirmation: str
    requires_actor: bool = True
    requires_reason: bool = False
    requires_evidence: bool = False
    requires_independent_actor: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "action_id": self.action_id,
            "label": self.label,
            "target_status": self.target_status,
            "confirmation": self.confirmation,
            "requires_actor": self.requires_actor,
            "requires_reason": self.requires_reason,
            "requires_evidence": self.requires_evidence,
            "requires_independent_actor": self.requires_independent_actor,
        }


# A descriptive alias for callers that prefer the vocabulary used in the requirement.
ActionCapability = ReviewAction


def _freeze(value: Mapping[str, Any] | None) -> Mapping[str, Any]:
    return MappingProxyType(dict(value or {}))


def _canonical_record_type(value: object) -> str:
    text = str(value or "").strip().lower()
    return _RECORD_ALIASES.get(text, text)


def _canonical_status(value: object) -> str:
    return str(getattr(value, "value", value) or "").strip().upper().replace("-", "_")


def _stamp(value: object) -> str:
    if isinstance(value, datetime):
        parsed = value if value.tzinfo else value.replace(tzinfo=UTC)
        return parsed.astimezone(UTC).isoformat()
    text = str(value or "").strip()
    if not text:
        return ""
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return text
    parsed = parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC).isoformat()


def _same_stamp(left: object, right: object) -> bool:
    return bool(_stamp(left)) and _stamp(left) == _stamp(right)


def _row_status(row: Any) -> str:
    return _canonical_status(getattr(row, "status", ""))


def _row_revision(row: Any) -> int | None:
    value = getattr(row, "revision", None)
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _row_current(row: Any) -> bool:
    if hasattr(row, "is_current") and not bool(row.is_current):
        return False
    return _row_status(row) not in {"SUPERSEDED", "RETIRED"}


def _row_scope(row: Any) -> dict[str, str]:
    return {
        key: str(getattr(row, key, "") or "")
        for key in ("well_id", "field_id", "project_id", "section_id")
    }


def _action(
    action_id: str,
    label: str,
    target: object,
    *,
    reason: bool = False,
    evidence: bool = False,
    independent: bool = False,
) -> ReviewAction:
    return ReviewAction(
        action_id=action_id,
        label=label,
        target_status=_canonical_status(target),
        confirmation=f"{label} this record",
        requires_reason=reason,
        requires_evidence=evidence,
        requires_independent_actor=independent,
    )


def _capabilities(
    record_type: str,
    status: str,
    *,
    current: bool = True,
    data: Mapping[str, Any] | None = None,
    has_evidence: bool | None = None,
) -> tuple[ReviewAction, ...]:
    """Map only existing legal domain edges to semantic actions.

    This is deliberately an allow-list around the lifecycle machines.  In particular, a procedure
    or program is superseded by its revision method, not by a generic status button, and a best
    practice has only the owning approval method exposed here.
    """
    record_type = _canonical_record_type(record_type)
    status = _canonical_status(status)
    data = data or {}
    if not current:
        return ()
    if record_type == "field_pattern" and data.get("stale_at"):
        # The snapshot is no longer the query result the reviewer would be confirming.  Refreshing
        # it is a separate existing intelligence operation, not a hidden write from this boundary.
        return ()
    lifecycle = _LIFECYCLES.get(record_type)
    if lifecycle is None:
        return ()
    try:
        targets = {_canonical_status(state) for state in lifecycle.allowed(status)}
    except ValidationError:
        return ()

    actions: list[ReviewAction] = []
    if record_type == "procedure_record":
        if "IN_REVIEW" in targets:
            actions.append(_action("submit_for_review", "Submit for review", "IN_REVIEW"))
        if "APPROVED" in targets:
            actions.append(_action("approve", "Approve", "APPROVED"))
        if "DRAFT" in targets:
            actions.append(_action("return_to_draft", "Return to draft", "DRAFT"))
        if "WITHDRAWN" in targets:
            actions.append(_action("withdraw", "Withdraw", "WITHDRAWN", reason=False))
    elif record_type == "drilling_program":
        if "IN_REVIEW" in targets:
            actions.append(_action("submit_for_review", "Submit for review", "IN_REVIEW"))
        if "APPROVED" in targets:
            actions.append(_action("approve", "Approve", "APPROVED"))
        if "DRAFT" in targets:
            actions.append(_action("return_to_draft", "Return to draft", "DRAFT"))
        if "ARCHIVED" in targets:
            actions.append(_action("archive", "Archive", "ARCHIVED"))
    elif record_type == "lesson_learned":
        if "REVIEW" in targets:
            actions.append(_action("submit_for_review", "Submit for review", "REVIEW"))
        if "APPROVED" in targets and (has_evidence is not False):
            actions.append(
                _action(
                    "approve",
                    "Approve",
                    "APPROVED",
                    evidence=True,
                    independent=True,
                )
            )
        if "REJECTED" in targets:
            actions.append(_action("reject", "Reject", "REJECTED", reason=True))
        if "DRAFT" in targets:
            actions.append(_action("reopen", "Reopen for revision", "DRAFT"))
    elif record_type == "best_practice":
        # There is intentionally no generic practice status writer: approval is the domain method.
        if "APPROVED" in targets and str(data.get("rationale") or "").strip():
            actions.append(_action("approve", "Approve", "APPROVED", independent=True))
    elif record_type == "recommendation":
        if "ACCEPTED" in targets:
            actions.append(_action("accept", "Accept", "ACCEPTED"))
        if "DECLINED" in targets:
            actions.append(_action("decline", "Decline", "DECLINED", reason=True))
        if "IMPLEMENTED" in targets:
            actions.append(_action("mark_implemented", "Mark implemented", "IMPLEMENTED"))
        if "PROPOSED" in targets:
            actions.append(_action("reopen", "Reopen recommendation", "PROPOSED"))
        if "SUPERSEDED" in targets:
            actions.append(_action("supersede", "Supersede", "SUPERSEDED"))
    elif record_type == "risk_record":
        if "MITIGATED" in targets:
            actions.append(_action("mitigate", "Mark mitigated", "MITIGATED", reason=True))
        if "CLOSED" in targets:
            actions.append(_action("close", "Close", "CLOSED", reason=True))
        if "OPEN" in targets:
            actions.append(_action("reopen", "Reopen risk", "OPEN"))
        if "SUPERSEDED" in targets:
            actions.append(_action("supersede", "Supersede", "SUPERSEDED"))
    elif record_type in {"field_pattern", "cost_item", *_OPERATIONAL_MODELS}:
        if "CONFIRMED" in targets:
            actions.append(_action("confirm", "Confirm", "CONFIRMED"))
        if "REJECTED" in targets:
            actions.append(_action("reject", "Reject", "REJECTED"))
        if "CANDIDATE" in targets:
            actions.append(_action("reopen", "Return to candidate", "CANDIDATE"))
    return tuple(actions)


@dataclass(frozen=True)
class ReviewActionRequest:
    """Immutable command carrying the displayed review preconditions.

    ``expected_status`` is mandatory at execution time.  Optional revision, current, scope and
    update-stamp values make a request stronger when the record family has those concepts; all are
    compared against a fresh authoritative read before the owning method is invoked.
    """

    record_type: str
    record_id: str
    action: str
    actor: str
    expected_status: str
    reason: str = ""
    note: str = ""
    well_id: str = ""
    expected_revision: int | None = None
    expected_current: bool | None = None
    expected_scope: Mapping[str, Any] = field(default_factory=dict)
    expected_updated_at: str = ""
    chosen_item_id: str = ""

    def __post_init__(self) -> None:
        record_type = _canonical_record_type(self.record_type)
        action = str(self.action or "").strip().lower().replace("-", "_")
        record_id = str(self.record_id or "").strip()
        expected_status = _canonical_status(self.expected_status)
        if not record_type or not record_id or not action:
            raise ValueError("a review action needs record_type, record_id and action")
        if not expected_status:
            raise ValueError("a review action needs expected_status for stale-state protection")
        object.__setattr__(self, "record_type", record_type)
        object.__setattr__(self, "record_id", record_id)
        object.__setattr__(self, "action", action)
        object.__setattr__(self, "actor", str(self.actor or "").strip())
        object.__setattr__(self, "expected_status", expected_status)
        object.__setattr__(self, "reason", str(self.reason or ""))
        object.__setattr__(self, "note", str(self.note or ""))
        object.__setattr__(self, "well_id", str(self.well_id or "").strip())
        object.__setattr__(self, "expected_scope", _freeze(self.expected_scope))
        object.__setattr__(self, "expected_updated_at", str(self.expected_updated_at or ""))
        object.__setattr__(self, "chosen_item_id", str(self.chosen_item_id or "").strip())

    @property
    def action_id(self) -> str:
        return self.action

    @property
    def explanation(self) -> str:
        return self.reason.strip() or self.note.strip()

    def to_dict(self) -> dict[str, Any]:
        return {
            "record_type": self.record_type,
            "record_id": self.record_id,
            "action": self.action,
            "actor": self.actor,
            "expected_status": self.expected_status,
            "reason": self.reason,
            "note": self.note,
            "well_id": self.well_id,
            "expected_revision": self.expected_revision,
            "expected_current": self.expected_current,
            "expected_scope": dict(self.expected_scope),
            "expected_updated_at": self.expected_updated_at,
            "chosen_item_id": self.chosen_item_id,
        }


@dataclass(frozen=True)
class ReviewActionResult:
    """Immutable result read from the committed domain row."""

    record_type: str
    record_id: str
    action: str
    actor: str
    previous_status: str
    status: str
    changed: bool
    at: str = ""
    revision: int | None = None
    current: bool = True
    scope: Mapping[str, Any] = field(default_factory=dict)
    detail: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "record_type", _canonical_record_type(self.record_type))
        object.__setattr__(self, "record_id", str(self.record_id))
        object.__setattr__(self, "action", str(self.action))
        object.__setattr__(self, "actor", str(self.actor))
        object.__setattr__(self, "previous_status", _canonical_status(self.previous_status))
        object.__setattr__(self, "status", _canonical_status(self.status))
        object.__setattr__(self, "at", str(self.at or ""))
        object.__setattr__(self, "scope", _freeze(self.scope))
        object.__setattr__(self, "detail", _freeze(self.detail))

    @property
    def action_id(self) -> str:
        return self.action

    @property
    def new_status(self) -> str:
        return self.status

    def to_dict(self) -> dict[str, Any]:
        return {
            "record_type": self.record_type,
            "record_id": self.record_id,
            "action": self.action,
            "actor": self.actor,
            "previous_status": self.previous_status,
            "status": self.status,
            "changed": self.changed,
            "at": self.at,
            "revision": self.revision,
            "current": self.current,
            "scope": dict(self.scope),
            "detail": dict(self.detail),
        }


# Short names are useful to headless callers while the explicit names remain the public documentation.
ActionRequest = ReviewActionRequest
ActionResult = ReviewActionResult


class ReviewActionService:
    """Route governed review actions to existing domain repositories and services."""

    def __init__(self, *, workspace: Any) -> None:
        if workspace is None:
            raise ReviewActionError("a review action needs a workspace")
        self._workspace = workspace

    @classmethod
    def for_workspace(cls, workspace: Any) -> ReviewActionService:
        return cls(workspace=workspace)

    def available_actions(
        self, record: ReviewRecord | ReviewConflict | Mapping[str, Any]
    ) -> tuple[ReviewAction, ...]:
        """Return actions from a read value only; this method never opens a write session."""
        if isinstance(record, ReviewConflict):
            if record.current and _canonical_status(record.status) == "OPEN":
                return (
                    ReviewAction(
                        action_id="resolve_conflict",
                        label="Resolve conflict",
                        target_status="RESOLVED_MANUALLY",
                        confirmation="Resolve this knowledge conflict",
                    ),
                )
            return ()
        if isinstance(record, ReviewRecord):
            return _capabilities(
                record.record_type,
                record.status,
                current=record.current,
                data=record.data,
                has_evidence=bool(record.provenance or record.evidence),
            )
        payload = record
        return _capabilities(
            str(payload.get("record_type") or ""),
            str(payload.get("status") or ""),
            current=bool(payload.get("current", True)),
            data=payload.get("data") if isinstance(payload.get("data"), Mapping) else payload,
            has_evidence=(
                bool(payload.get("provenance") or payload.get("evidence"))
                if "provenance" in payload or "evidence" in payload
                else None
            ),
        )

    def capabilities(
        self, record: ReviewRecord | ReviewConflict | Mapping[str, Any]
    ) -> tuple[ReviewAction, ...]:
        """Alias using the capability vocabulary used by action clients."""
        return self.available_actions(record)

    def actions_for(
        self, record: ReviewRecord | ReviewConflict | Mapping[str, Any]
    ) -> tuple[ReviewAction, ...]:
        """Stable read-side alias for callers building an action panel."""
        return self.available_actions(record)

    def execute(self, request: ReviewActionRequest | Mapping[str, Any]) -> ReviewActionResult:
        """Re-read, validate and execute one confirmed action in one committed transaction."""
        req = (
            request
            if isinstance(request, ReviewActionRequest)
            else ReviewActionRequest(**dict(request))
        )
        with self._workspace.database.unit_of_work() as session:
            if req.record_type == "knowledge_conflict":
                return self._execute_conflict(session, req)
            row = self._load_row(session, req.record_type, req.record_id)
            self._lock_row(session, row)
            self._check_preconditions(session, row, req)
            before = _row_status(row)
            capability = self._find_capability(session, row, req)
            self._validate_input(req, capability)
            detail: dict[str, Any] = {}
            self._route(session, row, req, capability)
            session.flush()
            # The row is still the committed unit of truth; result fields come from it after the
            # owning repository has flushed, never from a client-provided target or timestamp.
            after = _row_status(row)
            at = self._authoritative_at(row)
            return ReviewActionResult(
                record_type=req.record_type,
                record_id=req.record_id,
                action=req.action,
                actor=req.actor,
                previous_status=before,
                status=after,
                changed=before != after,
                at=at,
                revision=_row_revision(row),
                current=_row_current(row),
                scope=_row_scope(row),
                detail=detail,
            )

    def execute_action(
        self, request: ReviewActionRequest | Mapping[str, Any]
    ) -> ReviewActionResult:
        """Explicitly named alias for the write boundary used by UI/controllers."""
        return self.execute(request)

    def _execute_conflict(self, session: Session, req: ReviewActionRequest) -> ReviewActionResult:
        conflict = session.get(KnowledgeConflict, req.record_id)
        if conflict is None:
            raise ReviewActionError(f"no knowledge conflict {req.record_id!r}")
        self._lock_row(session, conflict)
        before = _canonical_status(conflict.status)
        if before != "OPEN":
            raise ReviewActionError(
                f"knowledge conflict {req.record_id!r} is {before}; reload before deciding",
                hint="a resolved conflict cannot be decided twice",
            )
        if req.expected_status != before:
            self._stale(req, before)
        if req.expected_current is False:
            raise ReviewActionError(
                f"historical knowledge conflict {req.record_id!r} cannot receive a decision",
                hint="act on the open conflict shown by the current review",
            )
        conflict_well_id = str(getattr(conflict, "well_id", "") or "")
        if req.well_id and conflict_well_id and conflict_well_id != req.well_id:
            raise ReviewActionError(
                f"knowledge conflict {req.record_id!r} is outside the selected well",
                hint="reload the review for the selected well",
                expected_well_id=req.well_id,
                actual_well_id=conflict_well_id,
            )
        if req.expected_updated_at and not _same_stamp(
            req.expected_updated_at, getattr(conflict, "updated_at", None)
        ):
            raise ReviewActionError(
                f"knowledge conflict {req.record_id!r} is stale: record changed",
                hint="reload the authoritative review before deciding",
            )
        if req.action != "resolve_conflict":
            raise ReviewActionError(
                f"action {req.action!r} is not available for a knowledge conflict"
            )
        if not req.chosen_item_id:
            raise ReviewActionError(
                "resolving a knowledge conflict needs a chosen candidate",
                hint="select one of the candidates shown in the review",
            )
        if not req.actor:
            raise ReviewActionError("a human action needs an explicit actor")
        # The existing knowledge application service owns candidate retirement, re-comparison and
        # the append-only audit event.  Passing this transaction keeps them atomic with the action.
        payload = KnowledgeExtractionService(
            database=self._workspace.database, refresh_index=False
        ).resolve(
            req.record_id,
            chosen_item_id=req.chosen_item_id,
            note=req.explanation,
            by=req.actor,
            session=session,
        )
        resolution = payload.get("resolution") or {}
        return ReviewActionResult(
            record_type=req.record_type,
            record_id=req.record_id,
            action=req.action,
            actor=req.actor,
            previous_status=before,
            status=_canonical_status(payload.get("status")),
            changed=True,
            at=str(resolution.get("at") or ""),
            current=True,
            scope={},
            detail={
                "chosen_item_id": req.chosen_item_id,
                "recheck": payload.get("recheck") or {},
            },
        )

    @staticmethod
    def _lock_row(session: Session, row: Any) -> None:
        """Take the database's row lock where the dialect supports it before revalidation.

        SQLite serialises the eventual writer and has no useful ``FOR UPDATE`` clause; PostgreSQL
        and other row-locking dialects use the same existing transaction without a second persistence
        mechanism.  The precondition is checked after this refresh, never against a stale identity-map
        copy.
        """
        session.refresh(row, with_for_update=True)

    @staticmethod
    def _load_row(session: Session, record_type: str, record_id: str) -> Any:
        if record_type == "procedure_record":
            return EngineeringRepository(session).get_procedure(record_id)
        if record_type == "drilling_program":
            return EngineeringRepository(session).get_program(record_id)
        if record_type == "lesson_learned":
            return LessonRepository(session).get_lesson(record_id)
        if record_type == "best_practice":
            return LessonRepository(session).get_practice(record_id)
        if record_type == "recommendation":
            return LessonRepository(session).get_recommendation(record_id)
        if record_type == "risk_record":
            return RiskRepository(session).get_risk(record_id)
        if record_type == "field_pattern":
            row = session.get(FieldPattern, record_id)
            if row is None:
                raise ReviewActionError(f"no field pattern {record_id!r}")
            return row
        if record_type == "cost_item":
            return CostRepository(session).get(record_id)
        if record_type in _OPERATIONAL_MODELS:
            return OperationsRepository(session).get_row(record_type, record_id)
        raise ReviewActionError(
            f"{record_type!r} is not an actionable review record",
            hint="the review only exposes actions owned by an existing domain method",
        )

    def _check_preconditions(self, session: Session, row: Any, req: ReviewActionRequest) -> None:
        actual = _row_status(row)
        if actual != req.expected_status:
            self._stale(req, actual)
        current = _row_current(row)
        if req.expected_current is not None and current != req.expected_current:
            raise ReviewActionError(
                f"review action for {req.record_id!r} is stale: current/history changed",
                hint="reload the authoritative review before acting",
                expected_current=req.expected_current,
                actual_current=current,
            )
        if req.expected_revision is not None and _row_revision(row) != req.expected_revision:
            raise ReviewActionError(
                f"review action for {req.record_id!r} is stale: revision changed",
                hint="reload the authoritative revision before acting",
                expected_revision=req.expected_revision,
                actual_revision=_row_revision(row),
            )
        if req.expected_updated_at and not _same_stamp(
            req.expected_updated_at, getattr(row, "updated_at", None)
        ):
            raise ReviewActionError(
                f"review action for {req.record_id!r} is stale: record changed",
                hint="reload the authoritative review before acting",
                expected_updated_at=req.expected_updated_at,
                actual_updated_at=_stamp(getattr(row, "updated_at", None)),
            )
        if not current:
            raise ReviewActionError(
                f"historical record {req.record_id!r} cannot receive a review action",
                hint="act on the current revision instead",
            )
        expected_scope = {
            str(key): str(value or "")
            for key, value in req.expected_scope.items()
            if str(value or "")
        }
        actual_scope = _row_scope(row)
        mismatches = {
            key: {"expected": value, "actual": actual_scope.get(key, "")}
            for key, value in expected_scope.items()
            if actual_scope.get(key, "") != value
        }
        if mismatches:
            raise ReviewActionError(
                f"review action for {req.record_id!r} has a scope mismatch",
                hint="the selected record is not the displayed well/field revision",
                mismatches=mismatches,
            )
        if req.well_id and actual_scope.get("well_id") and actual_scope["well_id"] != req.well_id:
            raise ReviewActionError(
                f"review action for {req.record_id!r} is outside the selected well",
                hint="reload the review for the selected well",
                expected_well_id=req.well_id,
                actual_well_id=actual_scope["well_id"],
            )
        if req.record_type == "field_pattern" and getattr(row, "stale_at", None) is not None:
            raise ReviewActionError(
                f"field pattern {req.record_id!r} is stale and cannot be acted on",
                hint="refresh the pattern snapshot, then review the new authoritative row",
            )

    @staticmethod
    def _stale(req: ReviewActionRequest, actual: str) -> None:
        raise ReviewActionError(
            f"review action for {req.record_id!r} is stale: expected {req.expected_status}, found {actual}",
            hint="reload the authoritative review before acting",
            expected_status=req.expected_status,
            actual_status=actual,
        )

    def _find_capability(
        self, session: Session, row: Any, req: ReviewActionRequest
    ) -> ReviewAction:
        data: Mapping[str, Any] = {}
        has_evidence: bool | None = None
        record_type = req.record_type
        if record_type == "lesson_learned":
            repository = LessonRepository(session)
            has_evidence = repository.evidence_count(str(row.id)) > 0
            data = {"rationale": ""}
        elif record_type == "best_practice":
            data = {"rationale": str(getattr(row, "rationale", "") or "")}
        elif record_type == "field_pattern" and getattr(row, "stale_at", None) is not None:
            raise ReviewActionError(
                f"field pattern {row.id!r} is stale and cannot be acted on",
                hint="refresh the pattern snapshot before deciding",
            )
        candidates = _capabilities(
            record_type,
            _row_status(row),
            current=_row_current(row),
            data=data,
            has_evidence=has_evidence,
        )
        for candidate in candidates:
            if candidate.action_id == req.action:
                return candidate
        raise ReviewActionError(
            f"action {req.action!r} is not available for {record_type} {row.id!r} in {_row_status(row)}",
            hint="choose one of the actions derived from the current lifecycle",
            available=[candidate.action_id for candidate in candidates],
        )

    @staticmethod
    def _validate_input(req: ReviewActionRequest, capability: ReviewAction) -> None:
        if capability.requires_actor and not req.actor:
            raise ReviewActionError(
                "a human action needs an explicit actor", hint="enter your name or user id"
            )
        if capability.requires_reason and not req.explanation.strip():
            raise ReviewActionError(
                f"{capability.label} needs a reason",
                hint="enter the reason that will remain with the domain decision",
            )

    def _route(
        self, session: Session, row: Any, req: ReviewActionRequest, capability: ReviewAction
    ) -> None:
        explanation = req.explanation
        record_type = req.record_type
        target = capability.target_status
        if record_type == "procedure_record":
            repository = EngineeringRepository(session)
            if req.action == "approve":
                repository.approve_procedure(str(row.id), by=req.actor, note=explanation)
            else:
                repository.set_procedure_status(
                    str(row.id), target, by=req.actor, reason=explanation
                )
            return
        if record_type == "drilling_program":
            repository = EngineeringRepository(session)
            if req.action == "approve":
                repository.approve_program(str(row.id), by=req.actor, note=explanation)
            else:
                repository.set_program_status(str(row.id), target, by=req.actor, reason=explanation)
            return
        if record_type == "lesson_learned":
            repository = LessonRepository(session)
            if req.action == "approve":
                repository.approve(str(row.id), by=req.actor, note=explanation)
            elif req.action == "reject":
                repository.reject(str(row.id), by=req.actor, reason=explanation)
            elif req.action == "submit_for_review":
                repository.submit_for_review(str(row.id), by=req.actor)
            elif req.action == "reopen":
                repository.reopen(str(row.id), by=req.actor, reason=explanation)
            else:
                raise ReviewActionError(f"unsupported lesson action {req.action!r}")
            return
        if record_type == "best_practice":
            if req.action != "approve":
                raise ReviewActionError(f"unsupported best practice action {req.action!r}")
            LessonRepository(session).approve_practice(str(row.id), by=req.actor, note=explanation)
            return
        if record_type == "recommendation":
            decision = {
                "accept": RecommendationLifecycle.ACCEPTED,
                "decline": RecommendationLifecycle.DECLINED,
                "mark_implemented": RecommendationLifecycle.IMPLEMENTED,
                "reopen": RecommendationLifecycle.PROPOSED,
                "supersede": RecommendationLifecycle.SUPERSEDED,
            }.get(req.action)
            if decision is None:
                raise ReviewActionError(f"unsupported recommendation action {req.action!r}")
            LessonRepository(session).decide_recommendation(
                str(row.id), decision, by=req.actor, reason=explanation
            )
            return
        if record_type == "risk_record":
            RiskRepository(session).set_risk_status(
                str(row.id), RiskLifecycle.parse(target) or target, by=req.actor, reason=explanation
            )
            return
        if record_type == "field_pattern":
            # Keep the intelligence service's existing session-borrowing path: it is the owner of
            # pattern confirmation and its status history, while this boundary owns only routing.
            IntelligenceService(database=self._workspace.database).confirm_pattern(
                str(row.id), target, by=req.actor, reason=explanation, session=session
            )
            return
        if record_type == "cost_item":
            CostRepository(session).set_status(
                str(row.id), target, by=req.actor, reason=explanation
            )
            return
        if record_type in _OPERATIONAL_MODELS:
            OperationsRepository(session).set_status(row, target, by=req.actor, reason=explanation)
            return
        raise ReviewActionError(f"no route for actionable record type {record_type!r}")

    @staticmethod
    def _authoritative_at(row: Any) -> str:
        for name in ("approved_at", "decided_at", "submitted_at"):
            value = getattr(row, name, None)
            if value is not None:
                return _stamp(value)
        attributes = getattr(row, "attributes", None) or {}
        history = attributes.get("status_history") if isinstance(attributes, Mapping) else None
        if history:
            last = history[-1] if isinstance(history[-1], Mapping) else {}
            if last.get("at"):
                return str(last["at"])
        updated = getattr(row, "updated_at", None)
        return _stamp(updated) if updated is not None else ""
