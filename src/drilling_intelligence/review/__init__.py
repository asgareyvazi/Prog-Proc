"""The read-only domain review boundary.

A domain review is a deterministic inspection of existing authoritative records for one well.  It
is a read model, not a new table, evidence store, decision engine or approval workflow.  The service
keeps the database and the existing evidence/citation paths authoritative while giving a CLI or a
future UI one stable, JSON-serialisable answer to inspect.
"""

from .contract import (
    REVIEW_CURRENT,
    REVIEW_HISTORY,
    DomainReview,
    DomainReviewRequest,
    ReviewConflict,
    ReviewRecord,
    ReviewVerification,
)
from .service import DomainReviewService

__all__ = [
    "REVIEW_CURRENT",
    "REVIEW_HISTORY",
    "DomainReview",
    "DomainReviewRequest",
    "DomainReviewService",
    "ReviewConflict",
    "ReviewRecord",
    "ReviewVerification",
]
