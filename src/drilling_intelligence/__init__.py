"""Drilling Intelligence & Knowledge Platform.

Well-centric document intelligence, structured knowledge and deterministic
engineering for drilling projects.  See ``docs/ARCHITECTURE.md`` for the layer
contract:

    PySide6 UI  ->  application services  ->  domain  ->  repositories  ->  DB

Nothing above the domain layer may issue SQL, perform engineering arithmetic,
parse documents, call an LLM or touch a vector store directly.
"""

from __future__ import annotations

__version__ = "0.0.1a0"

#: Schema/behaviour version stamps.  Persisted with derived artefacts so that a
#: change in a rule set forces reprocessing instead of silently reusing stale
#: results.  Bump the relevant constant whenever the corresponding logic changes
#: in a way that would alter output.
EXTRACTION_ENGINE_VERSION = "2026.09.phase0"
CLASSIFIER_VERSION = "2026.09.phase0"
INGESTION_POLICY_VERSION = "2026.09.phase0"

__all__ = [
    "CLASSIFIER_VERSION",
    "EXTRACTION_ENGINE_VERSION",
    "INGESTION_POLICY_VERSION",
    "__version__",
]
