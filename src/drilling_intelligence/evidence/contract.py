"""Evidence Query & Package: the deterministic, content-addressed answer to a set of topics.

This is the layer a reader - a person at the terminal, a script, or a later AI surface - points
at when it needs *the verified evidence* for a question, not a search result page.  It sits
strictly above the certified chain::

    Search -> candidate discovery
    Retrieval -> authoritative re-read (the only way evidence is produced)
    Evidence -> this package: composed, deduplicated, addressable, checkable

The vocabulary:

*   :class:`EvidenceQuery` - one structured, scope-safe question with one or more topics.  Each
    topic is answered through :class:`~drilling_intelligence.retrieval.service.RetrievalService`;
    this layer never discovers candidates itself.
*   :class:`TopicCoverage` - what one topic produced: how many verified items it answered with,
    how many candidates the authoritative re-read rejected (and why), and whether discovery was
    broadened.  An empty answer is data, not an error.
*   :class:`PackageEvidence` - one verified item plus the topics that found it.  An item matching
    two topics appears once, with both recorded.
*   :class:`EvidencePackage` - the composed answer with a **content identity**: a hash over the
    request and the verified evidence, independent of ranking and display order.  The same
    database state and the same question always produce the same address, which is what makes a
    package diffable, snapshot-able and consumable by later layers.
*   :class:`FreshnessReport` - whether a package still matches what the authoritative database
    answers today.  A package is a snapshot of a read; staleness is decided by re-running its own
    query, never by a timestamp.

Everything here is a frozen value object of strings, numbers, booleans and plain mappings -
JSON-serialisable end to end, with no ORM rows, no sessions and nothing volatile (no call
timestamps, no memory addresses, no random ids).
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

from ..retrieval.contract import (
    LIFECYCLE_CURRENT,
    LIFECYCLE_HISTORY,
    SOURCE_TYPES,
    EvidenceItem,
)

__all__ = [
    "EvidencePackage",
    "EvidenceQuery",
    "FreshnessReport",
    "PackageEvidence",
    "TopicCoverage",
]

_LIFECYCLES = (LIFECYCLE_CURRENT, LIFECYCLE_HISTORY)


def _check_iso_date(value: str, name: str) -> None:
    """A date bound must be a real ISO date - the search layer compares them as strings, so a
    malformed one would not error but silently widen or narrow the query, and that must not be
    allowed to reach the index (the same rule :mod:`drilling_intelligence.retrieval` enforces)."""
    from datetime import datetime

    text = str(value).strip()
    try:
        datetime.fromisoformat(text.replace("Z", "+00:00"))
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{name} must be an ISO date (e.g. 2025-06-14), got {value!r}") from exc


@dataclass(frozen=True)
class EvidenceQuery:
    """One structured question with one or more topics, asked of the verified evidence layer.

    The scope follows the platform's one precedence rule (a named well is the whole scope, never a
    union with a field or project) and is handed to retrieval unchanged.  ``limit`` of zero means
    "no cap", the convention the search and intelligence layers use.  Topics are the questions; the
    package is the union of their verified answers.
    """

    topics: tuple[str, ...]
    well_id: str = ""
    field_id: str = ""
    project_id: str = ""
    #: A subset of :data:`SOURCE_TYPES`; empty means every source kind retrieval verifies.
    source_types: tuple[str, ...] = ()
    lifecycle: str = LIFECYCLE_CURRENT
    limit: int = 0
    date_from: str | None = None
    date_to: str | None = None

    def __post_init__(self) -> None:
        topics = tuple(str(t).strip() for t in (self.topics or ()))
        if not topics or not any(topics):
            raise ValueError(
                "an evidence query needs at least one topic - a package with no question has no address"
            )
        object.__setattr__(self, "topics", topics)
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
        if not isinstance(self.limit, int) or isinstance(self.limit, bool) or self.limit < 0:
            raise ValueError(
                f"limit must be a non-negative integer (0 = no cap), got {self.limit!r}"
            )
        if self.date_from is not None:
            _check_iso_date(str(self.date_from), "date_from")
        if self.date_to is not None:
            _check_iso_date(str(self.date_to), "date_to")

    def to_dict(self) -> dict[str, Any]:
        """The canonical question - what the package stores as its own query, and what a
        freshness check re-runs."""
        return {
            "topics": list(self.topics),
            "scope": {
                "well_id": self.well_id,
                "field_id": self.field_id,
                "project_id": self.project_id,
            },
            "source_types": list(self.source_types),
            "lifecycle": self.lifecycle,
            "limit": self.limit,
            "date_from": self.date_from,
            "date_to": self.date_to,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> EvidenceQuery:
        """The inverse of :meth:`to_dict` - how a stored package re-asks its own question.

        Validation runs again, so a hand-edited stored query fails exactly like a fresh one.
        """
        scope = dict(data.get("scope") or {})
        return cls(
            topics=tuple(data.get("topics") or ()),
            well_id=str(scope.get("well_id") or ""),
            field_id=str(scope.get("field_id") or ""),
            project_id=str(scope.get("project_id") or ""),
            source_types=tuple(data.get("source_types") or ()),
            lifecycle=str(data.get("lifecycle") or LIFECYCLE_CURRENT),
            limit=int(data.get("limit", 0)),
            date_from=data.get("date_from"),
            date_to=data.get("date_to"),
        )


@dataclass(frozen=True)
class TopicCoverage:
    """What one topic produced, so an empty answer is a statement rather than a silence.

    ``returned`` counts the verified items the topic answered with; ``dropped`` counts the
    candidates the authoritative re-read rejected (each entry carries the reason); ``broadened``
    says whether search had to relax the exact query.  All three are read off the retrieval bundle
    for that topic, never re-derived.
    """

    topic: str
    returned: int
    dropped: int
    broadened: bool
    drop_reasons: tuple[dict[str, Any], ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "topic": self.topic,
            "returned": self.returned,
            "dropped": self.dropped,
            "broadened": self.broadened,
            "drop_reasons": [dict(entry) for entry in self.drop_reasons],
        }


@dataclass(frozen=True)
class PackageEvidence:
    """One verified evidence item, plus every topic that found it.

    An item that two topics surface is one item in the package, listed once, with both topics in
    ``found_by`` (in topic order) - evidence multiplicity is not record multiplicity.
    """

    item: EvidenceItem
    found_by: tuple[str, ...]

    def to_dict(self) -> dict[str, Any]:
        payload = self.item.to_dict()
        payload["found_by"] = list(self.found_by)
        return payload


@dataclass(frozen=True)
class EvidencePackage:
    """The composed, deduplicated, content-addressed answer to an :class:`EvidenceQuery`.

    ``identity`` addresses the *verified evidence*, not its presentation: it is computed over the
    canonical request and each item's authoritative state (identity, source, status, current)
    plus per-topic coverage - never over scores or display order, so two renderings of the same
    evidence share an address.  ``items`` are ordered by topic order and, within a topic, by the
    discovery rank; that order is presentation, and the identity does not depend on it.

    The package stores its own query (``request``): a freshness check re-runs that query and
    compares what the authoritative database answers now.
    """

    identity: str
    request: dict[str, Any]
    items: tuple[PackageEvidence, ...] = ()
    coverage: tuple[TopicCoverage, ...] = ()
    policy: str = LIFECYCLE_CURRENT
    scope: dict[str, Any] = field(default_factory=dict)

    @property
    def count(self) -> int:
        return len(self.items)

    def to_dict(self) -> dict[str, Any]:
        return {
            "identity": self.identity,
            "request": dict(self.request),
            "policy": self.policy,
            "scope": dict(self.scope),
            "count": len(self.items),
            "items": [entry.to_dict() for entry in self.items],
            "coverage": [entry.to_dict() for entry in self.coverage],
        }


@dataclass(frozen=True)
class FreshnessReport:
    """Whether a stored package still matches the authoritative database's current answer.

    ``fresh`` is decided by the content identity (re-run the package's own query, compare).
    ``added``/``removed``/``changed`` explain the item-level delta when it is not fresh: an
    identity that now answers the query, one that no longer does, and one whose authoritative
    state (status, current) moved.  A package is never "silent-live": it is either a snapshot
    that still holds, or a snapshot with a named diff.
    """

    fresh: bool
    stored_identity: str
    current_identity: str
    added: tuple[str, ...] = ()
    removed: tuple[str, ...] = ()
    changed: tuple[dict[str, Any], ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "fresh": self.fresh,
            "stored_identity": self.stored_identity,
            "current_identity": self.current_identity,
            "added": list(self.added),
            "removed": list(self.removed),
            "changed": [dict(entry) for entry in self.changed],
        }
