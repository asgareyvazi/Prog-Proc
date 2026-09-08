"""Evidence Query & Package: the deterministic, content-addressed answer to a set of topics.

Search discovers, retrieval verifies, and this layer composes the verified evidence into an
addressable package: one per question, with per-topic coverage, a content identity that the same
database state always earns for the same question, and a freshness check that re-runs the
package's own query rather than trusting a timestamp.  Nothing here discovers candidates or
manufactures evidence - every item is a
:class:`~drilling_intelligence.retrieval.contract.EvidenceItem` that retrieval verified against
the authoritative database (ADR-0013, ADR-0014).
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

__all__ = [
    "EvidencePackage",
    "EvidenceQuery",
    "EvidenceQueryService",
    "FreshnessReport",
    "PackageEvidence",
    "TopicCoverage",
]
