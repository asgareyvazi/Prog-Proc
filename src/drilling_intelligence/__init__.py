"""Drilling Intelligence & Knowledge Platform.

Well-centric document intelligence, structured knowledge and deterministic engineering for
drilling projects.  The layer contract, in one line:

    UI / CLI  ->  application services  ->  domain  ->  repositories  ->  database

Nothing above the domain layer may issue SQL, perform engineering arithmetic, parse documents,
call an LLM or touch a vector store directly.  The decisions behind that shape - and why the
alternatives were rejected - are recorded in ``docs/DECISIONS.md`` (one ADR per decision), which
is the file this package's docstrings point at.
"""

from __future__ import annotations

from importlib import metadata as _metadata

__version__ = "0.0.1a0"

# A version in two places is a version that drifts: ``pyproject.toml`` is what an installed
# distribution reports, so prefer it and keep the literal above as the source-tree fallback (a
# checkout run with ``PYTHONPATH=src`` has no installed metadata at all, and must not fail).
try:  # pragma: no cover - depends on how the package was made importable
    __version__ = _metadata.version("drilling-intelligence")
except _metadata.PackageNotFoundError:  # pragma: no cover - running from a source tree
    pass

#: Schema/behaviour version stamps.  Persisted with derived artefacts so that a change in a rule
#: set forces reprocessing instead of silently reusing stale results.  Bump the relevant
#: constant whenever the corresponding logic changes in a way that would alter output.
EXTRACTION_ENGINE_VERSION = "2026.09.phase0"
CLASSIFIER_VERSION = "2026.09.phase0"
INGESTION_POLICY_VERSION = "2026.09.phase0"

__all__ = [
    "CLASSIFIER_VERSION",
    "EXTRACTION_ENGINE_VERSION",
    "INGESTION_POLICY_VERSION",
    "__version__",
]
