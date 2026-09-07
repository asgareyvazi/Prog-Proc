"""The knowledge layer: entities, typed facts, relational links and open conflicts.

Four modules, one purpose - turn what the extractors read into things a well can be asked about:

:mod:`~drilling_intelligence.knowledge.entities`
    What a fact can be *about*.  Wells, sections, BHAs, bits, mud programs, casing, events, lessons
    and the generic engineering facts a document asserts, using the vocabulary the rest of the
    platform already has as its single source of truth.

:mod:`~drilling_intelligence.knowledge.facts`
    The fact model - subject, predicate, value as written, value normalised into one comparable
    unit, validity window, status, confidence, provenance - and the predicate registry that maps
    extractor field names onto it.

:mod:`~drilling_intelligence.knowledge.repository`
    Version-aware storage in ``knowledge_item`` (the authoritative copy), the edges in
    ``knowledge_relation``, and the conflict records.

:mod:`~drilling_intelligence.knowledge.conflicts`
    Detection and ranking.  It reports a ranking; it never picks a winner silently.

:mod:`~drilling_intelligence.knowledge.service`
    The orchestration the CLI and the pipeline call: derive facts from stored artefacts, link them,
    compare them, refresh the disposable search index.

The constraints these modules keep - provenance as a storage invariant, subjects looked up and never
invented, conflicts recorded rather than resolved - are recorded in ``docs/DECISIONS.md`` (ADR-0008).
"""

from __future__ import annotations

from typing import Any

from .conflicts import (
    ABS_TOLERANCE,
    AUTHORITY_RANK,
    REL_TOLERANCE,
    ConflictCandidate,
    ConflictReport,
    detect_conflicts,
    resolve_conflict,
    values_agree,
)
from .entities import (
    ENTITY_ALIASES,
    ENTITY_TYPES,
    EntityRef,
    EntitySpec,
    KnowledgeError,
    ensure_placeholder,
    entity_spec,
    find_well_ref,
    normalise_entity_type,
    placeholder_id,
    ref_for_row,
    require,
    resolve,
    subject_type_for_classification,
)
from .facts import (
    PREDICATE_BY_FIELD,
    PREDICATES,
    VALUE_TYPES,
    KnowledgeFact,
    PredicateSpec,
    predicate_for_field,
    render_value,
)
from .repository import KnowledgeRepository, fact_id_for
from .service import KnowledgeExtractionService, SyncResult

__all__ = [
    "ABS_TOLERANCE",
    "AUTHORITY_RANK",
    "ENTITY_ALIASES",
    "ENTITY_TYPES",
    "PREDICATES",
    "PREDICATE_BY_FIELD",
    "REL_TOLERANCE",
    "VALUE_TYPES",
    "ConflictCandidate",
    "ConflictReport",
    "EntityRef",
    "EntitySpec",
    "KnowledgeError",
    "KnowledgeExtractionService",
    "KnowledgeFact",
    "KnowledgeRepository",
    "PredicateSpec",
    "SyncResult",
    "detect_conflicts",
    "ensure_placeholder",
    "entity_spec",
    "fact_id_for",
    "find_well_ref",
    "normalise_entity_type",
    "placeholder_id",
    "predicate_for_field",
    "ref_for_row",
    "render_value",
    "require",
    "resolve",
    "resolve_conflict",
    "subject_type_for_classification",
    "values_agree",
]


def __getattr__(name: str) -> Any:
    """Keep a renamed symbol's absence explainable rather than mysterious."""
    if name == "FactStore":  # pragma: no cover - migration aid only
        raise AttributeError(
            "there is no separate FactStore: facts live in knowledge_item behind "
            "drilling_intelligence.knowledge.repository.KnowledgeRepository, which is the single "
            "place the authoritative copy is read and written"
        )
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
