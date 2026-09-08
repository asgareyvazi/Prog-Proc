"""The Retrieval & Evidence contract: the value objects a retrieval answer is made of.

Search discovers *candidates* from a disposable index.  Retrieval re-reads each candidate from the
authoritative database, validates its lifecycle and scope, and returns it as an
:class:`EvidenceItem` - or drops it, with a reason, when it no longer holds.  What this module
defines is the *shape* of that answer, and the two rules that make it trustworthy:

*   **An identity is the record's own, never a retrieval invention.**  A structured row is
    ``structured:<type>:<row id>`` (the domain's primary key), a knowledge fact is
    ``knowledge:<item id>``, a document citation is ``document:<doc>:<version>:<chunk>``.  The same
    authoritative record retrieved twice - different query, different insertion order, different
    ranking - carries the same identity, because the identity is built from the authoritative row,
    not from content, a timestamp or a position in a result list.
*   **Provenance is carried, never fabricated.**  Every field a record does not hold is an empty
    string or ``None``.  A record with no document link says ``document_id=""``; one with no recorded
    location says ``locator_ref=""``.  An answer that invents a page number or a source is the exact
    thing this layer exists to prevent.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

__all__ = [
    "LIFECYCLE_CURRENT",
    "LIFECYCLE_HISTORY",
    "SOURCE_DOCUMENT",
    "SOURCE_KNOWLEDGE",
    "SOURCE_STRUCTURED",
    "SOURCE_TYPES",
    "EvidenceBundle",
    "EvidenceItem",
    "RetrievalRequest",
]

#: The three source types an evidence item may come from.
SOURCE_STRUCTURED = "structured"
SOURCE_DOCUMENT = "document"
SOURCE_KNOWLEDGE = "knowledge"
SOURCE_TYPES: tuple[str, ...] = (SOURCE_STRUCTURED, SOURCE_DOCUMENT, SOURCE_KNOWLEDGE)

#: Lifecycle policies.  ``current`` returns only the rows the domain considers authoritative now;
#: ``history`` returns those plus the superseded/retired/rejected rows, each labelled with its state
#: so a caller can see it is historical rather than treating it as the answer.
LIFECYCLE_CURRENT = "current"
LIFECYCLE_HISTORY = "history"
_LIFECYCLES: tuple[str, ...] = (LIFECYCLE_CURRENT, LIFECYCLE_HISTORY)


def _check_iso_date(value: str, name: str) -> None:
    """A date bound must be a real ISO date - the search layer compares them as strings, so a
    malformed one would not error but silently widen or narrow the query, and that must not be
    allowed to reach the index."""
    from datetime import datetime

    text = str(value).strip()
    try:
        datetime.fromisoformat(text.replace("Z", "+00:00"))
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{name} must be an ISO date (e.g. 2025-06-14), got {value!r}") from exc


@dataclass(frozen=True)
class RetrievalRequest:
    """One explicit retrieval question.  Nothing here is optional-by-ambiguity.

    The scope follows the platform's one precedence rule (a named well is the whole scope, never a
    union with a field or project); ``source_types`` narrows which source kinds are verified; and
    ``lifecycle`` decides whether superseded state is returned at all.  ``limit`` of zero means
    "no cap", the same convention the search and intelligence layers use.
    """

    query: str
    well_id: str = ""
    field_id: str = ""
    project_id: str = ""
    #: A subset of :data:`SOURCE_TYPES`; empty means every source kind the index can surface.
    source_types: tuple[str, ...] = ()
    lifecycle: str = LIFECYCLE_CURRENT
    limit: int = 20
    date_from: str | None = None
    date_to: str | None = None

    def __post_init__(self) -> None:
        lifecycle = str(self.lifecycle or LIFECYCLE_CURRENT).strip().lower()
        if lifecycle not in _LIFECYCLES:
            raise ValueError(
                f"lifecycle must be one of {list(_LIFECYCLES)}, got {self.lifecycle!r}"
            )
        object.__setattr__(self, "lifecycle", lifecycle)
        wanted = tuple(str(t) for t in (self.source_types or ()))
        unknown = sorted(set(wanted) - set(SOURCE_TYPES))
        if unknown:
            raise ValueError(
                f"unknown source type(s) {unknown}; expected a subset of {list(SOURCE_TYPES)}"
            )
        object.__setattr__(self, "source_types", wanted)
        if self.limit < 0:
            raise ValueError(f"limit must be zero (no cap) or positive, got {self.limit}")
        for name, value in (("date_from", self.date_from), ("date_to", self.date_to)):
            if value is None:
                continue
            _check_iso_date(value, name)

    @property
    def scope_level(self) -> str:
        """``well`` | ``field`` | ``project`` | ``all`` - the precedence that decides the scope."""
        if self.well_id:
            return "well"
        if self.field_id:
            return "field"
        if self.project_id:
            return "project"
        return "all"

    def to_dict(self) -> dict[str, Any]:
        return {
            "query": self.query,
            "well_id": self.well_id,
            "field_id": self.field_id,
            "project_id": self.project_id,
            "source_types": list(self.source_types),
            "lifecycle": self.lifecycle,
            "limit": self.limit,
            "date_from": self.date_from,
            "date_to": self.date_to,
            "scope_level": self.scope_level,
        }


@dataclass(frozen=True)
class EvidenceItem:
    """One authoritative record, re-read and verified - not a sidecar row.

    ``verified`` is always True for a returned item (the whole point is the re-read); what varies is
    ``current`` - whether the record is the one the domain answers "now" under the requested
    lifecycle.  ``score`` and ``matched_terms`` are the *discovery* ranking, carried so a caller can
    see why this was surfaced; they are not a claim about truth, which the re-read is.
    """

    identity: str
    source_type: str
    record_type: str
    source_id: str
    #: Scope, read from the authoritative row (never the sidecar copy).
    well_id: str = ""
    field_id: str = ""
    project_id: str = ""
    well_name: str = ""
    field_name: str = ""
    project_name: str = ""
    #: The row's own lifecycle state, and whether it is the current answer under the policy.
    status: str = ""
    current: bool = True
    #: Provenance, carried from the row - empty where the row holds none, never invented.
    #: A document or knowledge citation carries the extraction's recorded locator (a mapping); a
    #: structured record carries its own evidence list, since its citation is the row itself.
    document_id: str = ""
    document_version_id: str = ""
    locator_ref: str = ""
    provenance: dict[str, Any] | list[Any] = field(default_factory=dict)
    #: Display content, read from the authoritative row.
    title: str = ""
    text: str = ""
    record_date: str = ""
    #: The discovery ranking that surfaced this candidate (not a claim of authority).
    score: float = 0.0
    matched_terms: tuple[str, ...] = ()
    #: True when the identity resolves to an existing authoritative row at retrieval time.
    verified: bool = True

    def to_dict(self) -> dict[str, Any]:
        return {
            "identity": self.identity,
            "source_type": self.source_type,
            "record_type": self.record_type,
            "source_id": self.source_id,
            "well_id": self.well_id,
            "field_id": self.field_id,
            "project_id": self.project_id,
            "well_name": self.well_name,
            "field_name": self.field_name,
            "project_name": self.project_name,
            "status": self.status,
            "current": self.current,
            "document_id": self.document_id,
            "document_version_id": self.document_version_id,
            "locator_ref": self.locator_ref,
            "provenance": (
                dict(self.provenance)
                if isinstance(self.provenance, Mapping)
                else list(self.provenance)
            ),
            "title": self.title,
            "text": self.text,
            "record_date": self.record_date,
            "score": self.score,
            "matched_terms": list(self.matched_terms),
            "verified": self.verified,
        }


@dataclass(frozen=True)
class EvidenceBundle:
    """The deterministic answer to a :class:`RetrievalRequest`.

    ``items`` are the verified evidence, in discovery-rank order with a stable identity tie-break,
    and ``dropped`` are the candidates the authoritative re-read rejected, each with the reason - so
    a stale sidecar row is not silently absorbed but reported as "not authoritative".  Nothing
    volatile (a timestamp of the call, a memory address, a random id) is part of the bundle, so the
    same request over unchanged data produces the same answer.
    """

    request: dict[str, Any]
    items: tuple[EvidenceItem, ...] = ()
    dropped: tuple[dict[str, Any], ...] = ()
    policy: str = LIFECYCLE_CURRENT
    scope: dict[str, Any] = field(default_factory=dict)
    #: True when discovery relaxed the query: the exact all-terms AND matched nothing and the
    #: search layer fell back to any-of-the-terms.  Retrieval never broadens on its own, but it
    #: refuses to let a reader mistake broadened discovery for an exact match either.
    discovery_broadened: bool = False

    @property
    def count(self) -> int:
        return len(self.items)

    def to_dict(self) -> dict[str, Any]:
        return {
            "request": dict(self.request),
            "policy": self.policy,
            "scope": dict(self.scope),
            "count": len(self.items),
            "discovery_broadened": self.discovery_broadened,
            "items": [item.to_dict() for item in self.items],
            "dropped": [dict(entry) for entry in self.dropped],
        }
