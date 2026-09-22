"""Value objects returned by the domain review read boundary.

The objects in this module deliberately contain only plain values.  They carry the domain row's
state, evidence and provenance without becoming another persistence model.  A caller can render
them in a terminal today or pass ``to_dict()`` to a future UI without importing SQLAlchemy.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

REVIEW_CURRENT = "current"
REVIEW_HISTORY = "history"
_REVIEW_POLICIES = (REVIEW_CURRENT, REVIEW_HISTORY)


@dataclass(frozen=True)
class DomainReviewRequest:
    """One explicit, well-scoped review read.

    ``current`` follows each record family's existing current/history contract.  ``history`` adds
    the preserved superseded, rejected and replaced rows and labels them through ``current`` and
    the raw lifecycle status; it never rewrites a historical row as the current answer.
    ``verify_citations`` opts into the existing citation auditor and performs file reads only.
    """

    well_id: str
    lifecycle: str = REVIEW_CURRENT
    verify_citations: bool = False
    #: Zero means no application-level cap.  Repository calls still use a bounded safety limit so a
    #: corrupt database cannot make a screen allocate without bound; the result reports truncation.
    limit: int = 0

    def __post_init__(self) -> None:
        well_id = str(self.well_id or "").strip()
        if not well_id:
            raise ValueError("a domain review needs a well_id")
        object.__setattr__(self, "well_id", well_id)
        lifecycle = str(self.lifecycle or REVIEW_CURRENT).strip().lower()
        if lifecycle not in _REVIEW_POLICIES:
            raise ValueError(
                f"lifecycle must be one of {list(_REVIEW_POLICIES)}, got {self.lifecycle!r}"
            )
        object.__setattr__(self, "lifecycle", lifecycle)
        if not isinstance(self.limit, int) or isinstance(self.limit, bool) or self.limit < 0:
            raise ValueError(f"limit must be a non-negative integer, got {self.limit!r}")

    def to_dict(self) -> dict[str, Any]:
        return {
            "well_id": self.well_id,
            "lifecycle": self.lifecycle,
            "verify_citations": bool(self.verify_citations),
            "limit": self.limit,
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> DomainReviewRequest:
        return cls(
            well_id=str(payload.get("well_id") or ""),
            lifecycle=str(payload.get("lifecycle") or REVIEW_CURRENT),
            verify_citations=bool(payload.get("verify_citations", False)),
            limit=int(payload.get("limit", 0)),
        )


@dataclass(frozen=True)
class ReviewVerification:
    """The verification boundary for one returned record.

    ``row_authority`` is the authoritative SQLite row read by this service.  ``retrieval`` is
    explicitly ``NOT_USED`` because this subject read does not ask the disposable search sidecar to
    decide which records exist.  ``citation_audit`` is ``NOT_RUN`` unless the caller opts into the
    existing file re-read auditor.  These values prevent a UI from presenting a direct database read
    as a search hit or as a source-file audit that never happened.
    """

    row_authority: str = "AUTHORITATIVE"
    discovery: str = "authoritative_scope"
    retrieval: str = "NOT_USED"
    citation_audit: str = "NOT_RUN"
    reproducibility: str = "NOT_ASSESSED"

    def to_dict(self) -> dict[str, str]:
        return {
            "row_authority": self.row_authority,
            "discovery": self.discovery,
            "retrieval": self.retrieval,
            "citation_audit": self.citation_audit,
            "reproducibility": self.reproducibility,
        }


@dataclass(frozen=True)
class ReviewRecord:
    """One existing domain row projected for inspection, never persisted by review."""

    record_type: str
    record_id: str
    status: str = ""
    record_state: str = ""
    current: bool = True
    scope: Mapping[str, str] = field(default_factory=dict)
    data: Mapping[str, Any] = field(default_factory=dict)
    provenance: tuple[Mapping[str, Any], ...] = ()
    evidence: tuple[Mapping[str, Any], ...] = ()
    conflict_ids: tuple[str, ...] = ()
    flags: tuple[str, ...] = ()
    verification: ReviewVerification = field(default_factory=ReviewVerification)

    def to_dict(self) -> dict[str, Any]:
        return {
            "record_type": self.record_type,
            "record_id": self.record_id,
            "status": self.status,
            "record_state": self.record_state,
            "current": self.current,
            "scope": dict(self.scope),
            "data": dict(self.data),
            "provenance": [dict(entry) for entry in self.provenance],
            "evidence": [dict(entry) for entry in self.evidence],
            "conflict_ids": list(self.conflict_ids),
            "flags": list(self.flags),
            "verification": self.verification.to_dict(),
        }


@dataclass(frozen=True)
class ReviewConflict:
    """A knowledge conflict carried without selecting a winning candidate."""

    conflict_id: str
    lookup_key: str
    property_name: str
    status: str
    record_state: str
    current: bool
    candidates: tuple[Mapping[str, Any], ...] = ()
    resolution: Mapping[str, Any] = field(default_factory=dict)
    data: Mapping[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "conflict_id": self.conflict_id,
            "lookup_key": self.lookup_key,
            "property_name": self.property_name,
            "status": self.status,
            "record_state": self.record_state,
            "current": self.current,
            "candidates": [dict(entry) for entry in self.candidates],
            "resolution": dict(self.resolution),
            "data": dict(self.data),
        }


@dataclass(frozen=True)
class DomainReview:
    """The complete deterministic read for one well."""

    request: Mapping[str, Any]
    subject: Mapping[str, Any]
    sections: tuple[Mapping[str, Any], ...] = ()
    records: tuple[ReviewRecord, ...] = ()
    conflicts: tuple[ReviewConflict, ...] = ()
    relations: tuple[Mapping[str, Any], ...] = ()
    plan_actual: tuple[Mapping[str, Any], ...] = ()
    observations: Mapping[str, Any] = field(default_factory=dict)
    citation_audit: Mapping[str, Any] | None = None
    truncated: bool = False

    @property
    def record_count(self) -> int:
        return len(self.records)

    def to_dict(self) -> dict[str, Any]:
        return {
            "request": dict(self.request),
            "subject": dict(self.subject),
            "sections": [dict(section) for section in self.sections],
            "records": [record.to_dict() for record in self.records],
            "conflicts": [conflict.to_dict() for conflict in self.conflicts],
            "relations": [dict(relation) for relation in self.relations],
            "plan_actual": [dict(row) for row in self.plan_actual],
            "observations": dict(self.observations),
            "citation_audit": dict(self.citation_audit) if self.citation_audit else None,
            "truncated": self.truncated,
            "record_count": len(self.records),
        }


__all__ = [
    "REVIEW_CURRENT",
    "REVIEW_HISTORY",
    "DomainReview",
    "DomainReviewRequest",
    "ReviewConflict",
    "ReviewRecord",
    "ReviewVerification",
]
