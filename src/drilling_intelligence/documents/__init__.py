"""Document registry: identity, immutable versions, extraction storage, provenance.

Adding a revision never overwrites the previous one; every change is a new
``document_version`` row linked by ``supersedes`` plus an audit event.
"""

from .registry import DocumentRegistry, RegistrationResult
from .repository import DOCUMENT_LEVEL_FIELDS, DocumentRepository, identity_for
from .versioning import RevisionInfo, is_latest, parse_revision, revision_from_token, sort_key

__all__ = [
    "DOCUMENT_LEVEL_FIELDS",
    "DocumentRegistry",
    "DocumentRepository",
    "RegistrationResult",
    "RevisionInfo",
    "identity_for",
    "is_latest",
    "parse_revision",
    "revision_from_token",
    "sort_key",
]
