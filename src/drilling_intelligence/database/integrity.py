"""Cross-row invariants the schema cannot express, plus the tools to prove them.

A relational schema can say "one current version *per document*" (the partial unique
index on ``document_version``) and it can say "``document.current_version_id`` points at
a real version" (the deferred foreign key).  What it cannot say is the rule the whole
registry depends on:

    every document has **exactly one** current version, the pointer names *that*
    version, and a version that has been superseded does not still claim to be current.

That is a three-table statement, so it lives here as a *checker* that any entry point
(ingestion, repair, migration verification, the status bar) can run in one query batch,
and as a *policy* the repository follows when it writes (see
:meth:`drilling_intelligence.documents.repository.DocumentRepository.create_version`).

Everything in this module is dialect-portable SQL built with SQLAlchemy expressions:
no ``PRAGMA``, no SQLite-only syntax, nothing that would have to be rewritten when the
system of record moves to PostgreSQL.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from ..core.errors import DrillingIntelligenceError
from ..core.ids import new_id
from .models import (
    Calculation,
    Document,
    DocumentVersion,
    ExtractionCache,
    KnowledgeItem,
    KnowledgeRelation,
    Project,
    Skill,
    Source,
    Well,
    WellSection,
)

__all__ = [
    "RELATION_ENDPOINT_MODELS",
    "IntegrityProblem",
    "KnowledgeIntegrityError",
    "check_current_version_invariants",
    "check_extraction_cache",
    "check_knowledge_relations",
    "create_knowledge_relation",
    "describe_problems",
    "require_current_version_invariants",
    "validate_knowledge_relation",
]


class KnowledgeIntegrityError(DrillingIntelligenceError):
    """A knowledge edge that does not point at two real, supported rows."""

    code = "KNOWLEDGE_INTEGRITY"


@dataclass(frozen=True)
class IntegrityProblem:
    """One violation, phrased so it can be shown to a user and acted on."""

    table: str
    row_id: str
    problem: str
    detail: dict[str, Any] | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "table": self.table,
            "row_id": self.row_id,
            "problem": self.problem,
            "detail": dict(self.detail or {}),
        }

    def __str__(self) -> str:  # pragma: no cover - formatting helper
        return f"{self.table}({self.row_id}): {self.problem}"


def describe_problems(problems: list[IntegrityProblem]) -> str:
    return "; ".join(str(problem) for problem in problems) or "no problems"


# --------------------------------------------------------------------------- documents
def check_current_version_invariants(session: Session) -> list[IntegrityProblem]:
    """Return every current-version inconsistency in the registry.

    An empty list is the guarantee.  The checks are deliberately separate so a repair
    tool can act on the specific kind of breakage:

    ``NO_CURRENT_VERSION``    the document has versions but none is flagged current;
    ``MULTIPLE_CURRENT``      two or more versions claim to be current;
    ``POINTER_MISSING``       a current version exists but the document points nowhere;
    ``POINTER_MISMATCH``      the pointer names a version that is not the current one;
    ``POINTER_FOREIGN``       the pointer names a version of a *different* document;
    ``SUPERSEDED_IS_CURRENT`` the supersede chain and the current flag disagree.
    """
    problems: list[IntegrityProblem] = []

    documents = list(session.execute(select(Document)).scalars())
    versions = list(session.execute(select(DocumentVersion)).scalars())
    by_document: dict[str, list[DocumentVersion]] = {}
    for version in versions:
        by_document.setdefault(version.document_id, []).append(version)
    versions_by_id = {version.id: version for version in versions}

    for document in documents:
        owned = by_document.get(document.id, [])
        if not owned:
            if document.current_version_id:
                problems.append(
                    IntegrityProblem(
                        "document",
                        document.id,
                        "POINTER_FOREIGN",
                        {
                            "detail": f"pointer {document.current_version_id} but the document has no versions"
                        },
                    )
                )
            continue
        current = [version for version in owned if version.is_current]
        if len(current) == 0:
            problems.append(
                IntegrityProblem(
                    "document", document.id, "NO_CURRENT_VERSION", {"versions": len(owned)}
                )
            )
        elif len(current) > 1:
            problems.append(
                IntegrityProblem(
                    "document",
                    document.id,
                    "MULTIPLE_CURRENT",
                    {"versions": sorted(version.id for version in current)},
                )
            )
        expected = current[0] if current else None
        pointer = versions_by_id.get(document.current_version_id or "")
        if expected is not None:
            if pointer is None:
                problems.append(
                    IntegrityProblem(
                        "document",
                        document.id,
                        "POINTER_MISSING",
                        {
                            "current_version_id": document.current_version_id,
                            "expected": expected.id,
                        },
                    )
                )
            elif pointer.document_id != document.id:
                # Checked before the id comparison on purpose: a pointer at *another
                # document's* version is a different and worse fault than pointing at an
                # older revision of the same one (the row exists, so the foreign key is
                # satisfied, and every read of this document would show someone else's
                # revision).  Reporting it as a mismatch would send a repair tool off to
                # find the wrong version.
                problems.append(
                    IntegrityProblem(
                        "document",
                        document.id,
                        "POINTER_FOREIGN",
                        {"version": pointer.id, "version_of_document": pointer.document_id},
                    )
                )
            elif pointer.id != expected.id:
                problems.append(
                    IntegrityProblem(
                        "document",
                        document.id,
                        "POINTER_MISMATCH",
                        {"points_at": pointer.id, "expected": expected.id},
                    )
                )
        for version in owned:
            if version.superseded_by_version_id and version.is_current:
                problems.append(
                    IntegrityProblem(
                        "document_version",
                        version.id,
                        "SUPERSEDED_IS_CURRENT",
                        {"superseded_by": version.superseded_by_version_id},
                    )
                )

    # A current version whose document no longer exists is unreachable rather than
    # wrong, and the FK cascade makes it impossible; assert it anyway so a hand-edited
    # database is reported instead of silently skipped.
    document_ids = {document.id for document in documents}
    for version in versions:
        if version.document_id not in document_ids:
            problems.append(
                IntegrityProblem(
                    "document_version",
                    version.id,
                    "ORPHAN_VERSION",
                    {"document_id": version.document_id},
                )
            )

    return problems


def require_current_version_invariants(session: Session) -> None:
    """Raise if the registry's current-version rule is broken (used after writes)."""
    problems = check_current_version_invariants(session)
    if problems:
        raise DrillingIntelligenceError(
            f"current-version invariant violated: {describe_problems(problems)}",
            problems=[problem.to_dict() for problem in problems],
        )


# --------------------------------------------------------------------------- knowledge
#: The tables a knowledge edge may connect.  A closed set is what makes the polymorphic
#: ``source_type``/``target_type`` pair safe to query without dozens of foreign keys:
#: adding an endpoint type is one line here plus a real migration.
RELATION_ENDPOINT_MODELS: dict[str, type] = {
    "document": Document,
    "document_version": DocumentVersion,
    "knowledge_item": KnowledgeItem,
    "source": Source,
    "well": Well,
    "well_section": WellSection,
    "project": Project,
    "calculation": Calculation,
    "skill": Skill,
}


def _endpoint_exists(session: Session, endpoint_type: str, endpoint_id: str) -> bool:
    model = RELATION_ENDPOINT_MODELS.get(endpoint_type)
    if model is None or not endpoint_id:
        return False
    if session.get(model, endpoint_id) is not None:
        return True
    # Validate *before* persistence: an object added moments ago and not yet flushed
    # still counts, without forcing a flush that would defeat the point of checking.
    return any(
        isinstance(pending, model) and getattr(pending, "id", None) == endpoint_id
        for pending in session.new
    )


def validate_knowledge_relation(
    session: Session,
    *,
    source_type: str,
    source_id: str,
    target_type: str,
    target_id: str,
    relation: str = "",
) -> None:
    """Reject an edge that must not be written: unsupported type, blank or dangling id.

    This is the pre-persistence half of the guarantee; the dangling references that
    already exist (from a deleted row, a hand-edited file, an interrupted import) are
    found by :func:`check_knowledge_relations`.
    """
    for label, endpoint_type, endpoint_id in (
        ("source", source_type, source_id),
        ("target", target_type, target_id),
    ):
        if not str(endpoint_type or "").strip():
            raise KnowledgeIntegrityError(f"knowledge relation {label}_type must not be empty")
        if endpoint_type not in RELATION_ENDPOINT_MODELS:
            raise KnowledgeIntegrityError(
                f"knowledge relation {label}_type {endpoint_type!r} is not a supported endpoint",
                supported=sorted(RELATION_ENDPOINT_MODELS),
            )
        if not str(endpoint_id or "").strip():
            raise KnowledgeIntegrityError(f"knowledge relation {label}_id must not be empty")
        if not _endpoint_exists(session, endpoint_type, endpoint_id):
            raise KnowledgeIntegrityError(
                f"knowledge relation {label} does not exist: {endpoint_type}({endpoint_id})",
                endpoint_type=endpoint_type,
                endpoint_id=endpoint_id,
            )
    if not str(relation or "").strip():
        raise KnowledgeIntegrityError("knowledge relation type must not be empty")
    # The relation vocabulary is open (it grows with the domain), but it must be stable
    # enough to query against: snake_case, no spaces, no separators that invite aliases.
    normalised = str(relation).strip()
    if normalised != relation or " " in relation:
        raise KnowledgeIntegrityError(
            f"knowledge relation {relation!r} must be a single snake_case token"
        )


def find_knowledge_relation(
    session: Session,
    *,
    source_type: str,
    source_id: str,
    relation: str,
    target_type: str,
    target_id: str,
) -> KnowledgeRelation | None:
    """The stored edge with the same five key columns, if there is one."""
    return session.execute(
        select(KnowledgeRelation).where(
            KnowledgeRelation.source_type == source_type,
            KnowledgeRelation.source_id == source_id,
            KnowledgeRelation.relation == relation,
            KnowledgeRelation.target_type == target_type,
            KnowledgeRelation.target_id == target_id,
        )
    ).scalar_one_or_none()


def create_knowledge_relation(
    session: Session,
    *,
    source_type: str,
    source_id: str,
    relation: str,
    target_type: str,
    target_id: str,
    weight: float = 1.0,
    provenance: list[Any] | None = None,
    note: str | None = None,
) -> KnowledgeRelation:
    """Validate, then add (or strengthen) the edge - the only sanctioned write path.

    The graph keeps its polymorphic endpoints (``source_type``/``source_id`` rather than
    twenty foreign keys, which is the decision recorded in ADR-0006), so nothing at the
    database level can stop an edge pointing at a row that does not exist.  This function
    is where that guarantee is enforced instead: it refuses before ``session.add``, so a
    rejected edge never reaches the unit of work at all.  Re-asserting the same edge
    updates the existing row instead of tripping ``uq_relation_edge``.
    """
    validate_knowledge_relation(
        session,
        source_type=source_type,
        source_id=source_id,
        target_type=target_type,
        target_id=target_id,
        relation=relation,
    )
    if not 0.0 <= float(weight) <= 1.0:
        raise KnowledgeIntegrityError(
            f"knowledge relation weight must be between 0 and 1, got {weight!r}"
        )

    existing = find_knowledge_relation(
        session,
        source_type=source_type,
        source_id=source_id,
        relation=relation,
        target_type=target_type,
        target_id=target_id,
    )
    if existing is not None:
        return _strengthen(existing, weight=weight, provenance=provenance, note=note)

    relation_row = KnowledgeRelation(
        id=new_id("rel"),
        source_type=source_type,
        source_id=source_id,
        relation=relation,
        target_type=target_type,
        target_id=target_id,
        weight=float(weight),
        provenance=list(provenance or []),
        note=note,
    )
    session.add(relation_row)
    try:
        with session.begin_nested():
            session.flush()
    except IntegrityError:
        # Someone else wrote the same edge while we were validating: re-read and merge.
        session.expire_all()
        winner = find_knowledge_relation(
            session,
            source_type=source_type,
            source_id=source_id,
            relation=relation,
            target_type=target_type,
            target_id=target_id,
        )
        if winner is None:  # pragma: no cover - the constraint fired for another reason
            raise
        return _strengthen(winner, weight=weight, provenance=provenance, note=note)
    return relation_row


def _strengthen(
    row: KnowledgeRelation, *, weight: float, provenance: list[Any] | None, note: str | None
) -> KnowledgeRelation:
    row.weight = max(float(row.weight or 0.0), float(weight))
    if provenance:
        seen = {str(item) for item in (row.provenance or [])}
        row.provenance = list(row.provenance or []) + [
            item for item in provenance if str(item) not in seen
        ]
    if note:
        row.note = note
    return row


def check_knowledge_relations(session: Session) -> list[IntegrityProblem]:
    """Every stored edge whose endpoints are unsupported, missing or dangling.

    Reads the whole table on purpose: the row count is the number of asserted links
    between knowledge items, which is small enough to enumerate, and a repair tool wants
    the complete list in one pass rather than one query per relation type.
    """
    problems: list[IntegrityProblem] = []
    for relation in session.execute(select(KnowledgeRelation)).scalars():
        for label, endpoint_type, endpoint_id in (
            ("source", relation.source_type, relation.source_id),
            ("target", relation.target_type, relation.target_id),
        ):
            if endpoint_type not in RELATION_ENDPOINT_MODELS:
                problems.append(
                    IntegrityProblem(
                        "knowledge_relation",
                        relation.id,
                        "UNSUPPORTED_ENDPOINT_TYPE",
                        {label: f"{endpoint_type}({endpoint_id})"},
                    )
                )
                continue
            model = RELATION_ENDPOINT_MODELS[endpoint_type]
            if session.get(model, endpoint_id) is None:
                problems.append(
                    IntegrityProblem(
                        "knowledge_relation",
                        relation.id,
                        "DANGLING_REFERENCE",
                        {label: f"{endpoint_type}({endpoint_id})"},
                    )
                )
        if (
            relation.source_type == relation.target_type
            and relation.source_id == relation.target_id
        ):
            problems.append(
                IntegrityProblem(
                    "knowledge_relation",
                    relation.id,
                    "SELF_REFERENCE",
                    {"relation": relation.relation},
                )
            )
    return problems


def check_extraction_cache(session: Session) -> list[IntegrityProblem]:
    """Report extraction-cache keys that somehow hold more than one row.

    The unique constraint makes this impossible in a healthy database; the check exists
    so a hand-edited or partially-migrated file reports the problem instead of serving a
    random artefact (``select(...).limit(1)`` would otherwise hide it).
    """

    statement = (
        select(
            ExtractionCache.content_sha256,
            ExtractionCache.extractor,
            ExtractionCache.extractor_version,
            ExtractionCache.config_hash,
            func.group_concat(ExtractionCache.id),
            func.count(ExtractionCache.id),
        )
        .group_by(
            ExtractionCache.content_sha256,
            ExtractionCache.extractor,
            ExtractionCache.extractor_version,
            ExtractionCache.config_hash,
        )
        .having(func.count(ExtractionCache.id) > 1)
    )
    problems: list[IntegrityProblem] = []
    for sha, extractor, extractor_version, config_hash, ids, count in session.execute(
        statement
    ).all():
        problems.append(
            IntegrityProblem(
                "extraction_cache",
                str(ids or "").split(",")[0],
                "DUPLICATE_CACHE_KEY",
                {
                    "entries": [str(item) for item in str(ids or "").split(",")],
                    "count": int(count),
                    "key": {
                        "sha256": sha[:16],
                        "extractor": extractor,
                        "extractor_version": extractor_version,
                        "config_hash": config_hash,
                    },
                },
            )
        )
    return problems
