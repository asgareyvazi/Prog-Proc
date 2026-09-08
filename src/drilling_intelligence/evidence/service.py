"""Evidence Query Service: compose verified evidence from a set of topics into an addressable package.

The one rule this module exists to keep: **evidence is only ever produced by retrieval.**  Every
topic of an :class:`~drilling_intelligence.evidence.contract.EvidenceQuery` is answered by a
:class:`~drilling_intelligence.retrieval.service.RetrievalService` call - search's candidates
re-read from the authoritative database, lifecycle and scope checked on the rows.  This layer adds
composition and addressing on top of that, nothing else:

*   **Composition** - the topics' verified answers are merged into one package, deduplicated by
    the item's authoritative identity, each item carrying the topics that found it.
*   **Addressing** - the package's content identity hashes the canonical request and the verified
    evidence (identity, source, status, current) plus per-topic coverage - never scores or display
    order - so the same database state and the same question always earn the same address.
*   **Freshness** - a package stores its own query; :meth:`EvidenceQueryService.check_freshness`
    re-runs that query and reports exactly what moved (added, removed, changed), so a snapshot is
    either still true or carries a named diff.

Read-only throughout: the service holds a retrieval service, and retrieval opens read-only (or
caller-borrowed) sessions and never commits.  Nothing here writes, and a query over unchanged
data produces the identical package, byte for byte.
"""

from __future__ import annotations

from typing import Any

from ..core.errors import ValidationError
from ..core.hashing import sha256_obj
from ..retrieval.contract import RetrievalRequest
from ..retrieval.service import RetrievalService
from .contract import (
    EvidencePackage,
    EvidenceQuery,
    FreshnessReport,
    PackageEvidence,
    TopicCoverage,
)

__all__ = ["EvidenceQueryService"]


class EvidenceQueryService:
    """Answers an :class:`EvidenceQuery` by asking :class:`RetrievalService` once per topic."""

    def __init__(self, *, retrieval: RetrievalService) -> None:
        if retrieval is None:
            raise ValidationError(
                "an evidence query runs through retrieval, not around it",
                hint="bind a workspace (EvidenceQueryService.for_workspace) so the search index "
                "and the authoritative database are both available",
            )
        self._retrieval = retrieval

    @classmethod
    def for_workspace(cls, workspace: Any) -> EvidenceQueryService:
        """Bind the service to an open workspace, reusing retrieval's search binding."""
        return cls(retrieval=RetrievalService.for_workspace(workspace))

    # -- the one entry point ---------------------------------------------------
    def query(self, request: EvidenceQuery | dict[str, Any]) -> EvidencePackage:
        """Compose the package: one retrieval per topic, merged, deduplicated, addressed.

        Display order is topic order, then the discovery rank within that topic; the package's
        content identity does not depend on either.
        """
        req = request if isinstance(request, EvidenceQuery) else EvidenceQuery(**dict(request))
        bundles = [
            self._retrieval.retrieve(
                RetrievalRequest(
                    query=topic,
                    well_id=req.well_id,
                    field_id=req.field_id,
                    project_id=req.project_id,
                    source_types=req.source_types,
                    lifecycle=req.lifecycle,
                    limit=req.limit,
                    date_from=req.date_from,
                    date_to=req.date_to,
                )
            )
            for topic in req.topics
        ]

        # Merge by the item's authoritative identity: the first occurrence keeps its position,
        # later topics only add themselves to found_by (in topic order, never duplicated).
        first_item: dict[str, Any] = {}
        found_by: dict[str, list[str]] = {}
        for topic, bundle in zip(req.topics, bundles, strict=True):
            for item in bundle.items:
                if item.identity not in first_item:
                    first_item[item.identity] = item
                topics = found_by.setdefault(item.identity, [])
                if topic not in topics:
                    topics.append(topic)
        items = tuple(
            PackageEvidence(item=first_item[identity], found_by=tuple(found_by[identity]))
            for identity in first_item
        )

        coverage = tuple(
            TopicCoverage(
                topic=topic,
                returned=len(bundle.items),
                dropped=len(bundle.dropped),
                broadened=bundle.discovery_broadened,
                drop_reasons=tuple(dict(entry) for entry in bundle.dropped),
            )
            for topic, bundle in zip(req.topics, bundles, strict=True)
        )
        return EvidencePackage(
            identity=self._content_identity(req, items, coverage),
            request=req.to_dict(),
            items=items,
            coverage=coverage,
            policy=req.lifecycle,
            scope=dict(bundles[0].scope) if bundles else {},
        )

    # -- addressing -------------------------------------------------------------
    @staticmethod
    def _content_identity(
        req: EvidenceQuery, items: tuple[PackageEvidence, ...], coverage: tuple[TopicCoverage, ...]
    ) -> str:
        """The package's address: the canonical request plus the verified evidence, sorted.

        Deliberately excluded: scores (ranking is a property of the disposable index, not of the
        evidence), display order and per-topic hit counts beyond coverage - what stays is what was
        asked, which authoritative records answered, in which state, and how each topic fared.
        """
        evidence = [
            {
                "identity": entry.item.identity,
                "source_type": entry.item.source_type,
                "record_type": entry.item.record_type,
                "status": entry.item.status,
                "current": entry.item.current,
            }
            for entry in sorted(items, key=lambda entry: entry.item.identity)
        ]
        payload = {
            "request": {
                "topics": sorted(set(req.topics)),
                "scope": {
                    "well_id": req.well_id,
                    "field_id": req.field_id,
                    "project_id": req.project_id,
                },
                "source_types": sorted(set(req.source_types)),
                "lifecycle": req.lifecycle,
                "limit": req.limit,
                "date_from": req.date_from,
                "date_to": req.date_to,
            },
            "evidence": evidence,
            "coverage": [
                {
                    "topic": entry.topic,
                    "returned": entry.returned,
                    "dropped": entry.dropped,
                    "broadened": entry.broadened,
                }
                for entry in sorted(coverage, key=lambda entry: entry.topic)
            ],
        }
        return "evpkg:" + sha256_obj(payload)

    # -- freshness ----------------------------------------------------------------
    def check_freshness(self, package: EvidencePackage) -> FreshnessReport:
        """Re-run the package's own query and diff what the authoritative database answers now.

        A package is a snapshot of a read, and staleness is not a timestamp on it: it is the
        difference between the snapshot and a fresh read.  ``fresh`` is the content identity
        comparison; the delta fields name the evidence that moved, so a stale package still says
        exactly what changed rather than merely "it is old".
        """
        current = self.query(EvidenceQuery.from_dict(package.request))
        stored = {entry.item.identity: entry.item for entry in package.items}
        now = {entry.item.identity: entry.item for entry in current.items}
        added = tuple(sorted(set(now) - set(stored)))
        removed = tuple(sorted(set(stored) - set(now)))
        changed = tuple(
            {
                "identity": identity,
                "status": {"from": stored[identity].status, "to": now[identity].status},
                "current": {"from": stored[identity].current, "to": now[identity].current},
            }
            for identity in sorted(set(stored) & set(now))
            if (stored[identity].status, stored[identity].current)
            != (now[identity].status, now[identity].current)
        )
        fresh = package.identity == current.identity and not (added or removed or changed)
        return FreshnessReport(
            fresh=fresh,
            stored_identity=package.identity,
            current_identity=current.identity,
            added=added,
            removed=removed,
            changed=changed,
        )
