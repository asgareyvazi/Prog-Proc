"""Evidence Query & Package: the deterministic, content-addressed answer to a set of topics.

Search discovers, retrieval verifies, and this layer composes the verified evidence into an
addressable package: one per question, with per-topic coverage, a content identity that the same
database state always earns for the same question, and a freshness check that re-runs the
package's own query rather than trusting a timestamp.  Nothing here discovers candidates or
manufactures evidence - every item is a
:class:`~drilling_intelligence.retrieval.contract.EvidenceItem` that retrieval verified against
the authoritative database (ADR-0013, ADR-0014).  The read-only :class:`CitationAuditor` (ADR-0015)
re-checks the third thing a reader relies on - that each item's recorded citation still holds in
its source file - by re-reading the file, not by trusting the row.
"""

from __future__ import annotations

from .contract import (
    EvidencePackage,
    EvidenceQuery,
    FreshnessReport,
    PackageEvidence,
    TopicCoverage,
)
from .service import EvidenceQueryService
from .verify import (
    STATUS_MATCH,
    STATUS_MISMATCH,
    STATUS_NOT_CHECKABLE,
    STATUS_UNREADABLE,
    CitationAuditor,
    CitationAuditReport,
    CitationCheck,
)

__all__ = [
    "STATUS_MATCH",
    "STATUS_MISMATCH",
    "STATUS_NOT_CHECKABLE",
    "STATUS_UNREADABLE",
    "CitationAuditReport",
    "CitationAuditor",
    "CitationCheck",
    "EvidencePackage",
    "EvidenceQuery",
    "EvidenceQueryService",
    "FreshnessReport",
    "PackageEvidence",
    "TopicCoverage",
]
