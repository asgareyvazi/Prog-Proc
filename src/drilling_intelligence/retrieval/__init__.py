"""Retrieval & Evidence: the authoritative verification boundary above search.

Search discovers candidates from a disposable index; retrieval re-reads each candidate from the
authoritative database, validates its lifecycle and scope, and returns verified evidence - or
reports, with a reason, why a candidate is no longer authoritative.  Nothing here manufactures
engineering truth: it locates, verifies and reports.
"""

from __future__ import annotations

from .contract import (
    LIFECYCLE_CURRENT,
    LIFECYCLE_HISTORY,
    SOURCE_DOCUMENT,
    SOURCE_KNOWLEDGE,
    SOURCE_STRUCTURED,
    SOURCE_TYPES,
    EvidenceBundle,
    EvidenceItem,
    RetrievalRequest,
)
from .service import RetrievalService

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
    "RetrievalService",
]
