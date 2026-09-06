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
    BestPractice,
    Calculation,
    Company,
    CostItem,
    DdrReport,
    Document,
    DocumentVersion,
    DrillingProgram,
    ExtractionCache,
    Field,
    FieldPattern,
    KnowledgeItem,
    KnowledgeRelation,
    LessonLearned,
    NptRecord,
    ProblemOccurrence,
    ProcedureRecord,
    Project,
    Recommendation,
    RiskRecord,
    ServiceCompany,
    Skill,
    Source,
    Well,
    WellEvent,
    WellOperation,
    WellSection,
)

__all__ = [
    "RELATION_ENDPOINT_MODELS",
    "IntegrityProblem",
    "KnowledgeIntegrityError",
    "check_cross_well_links",
    "check_current_version_invariants",
    "check_extraction_cache",
    "check_knowledge_relations",
    "check_operational_integrity",
    "check_promoted_evidence",
    "check_revision_chains",
    "check_well_hierarchy",
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
#: Every table an evidence-graph edge is allowed to point at, keyed by the name the API uses.
#:
#: The hierarchy, the evidence and the derived knowledge were here first; the operational and
#: engineering records join it rather than getting a graph of their own (:doc:`docs/DECISIONS.md`
#: ADR-0011), so ``RISK_MITIGATES`` and ``REPORT_CONTAINS_EVENT`` are checked by the same code as
#: ``SUPPORTS_FACT`` and inherit its rules: both ends must be real rows of a registered table, the
#: source and target types must be ones the relation accepts, and an edge is still only as strong as
#: the provenance carried with it.  A record table missing from this map is not an unsupported
#: relationship - it is a rejected write, which is the point: a typo in an endpoint name would
#: otherwise become a dangling edge that every later query has to be defensive about.
RELATION_ENDPOINT_MODELS: dict[str, type] = {
    # context
    "company": Company,
    "project": Project,
    "field": Field,
    "well": Well,
    "well_section": WellSection,
    # evidence
    "document": Document,
    "document_version": DocumentVersion,
    "source": Source,
    "knowledge_item": KnowledgeItem,
    # operational history
    "ddr_report": DdrReport,
    "well_operation": WellOperation,
    "well_event": WellEvent,
    "npt_record": NptRecord,
    "problem_occurrence": ProblemOccurrence,
    # engineering records
    "procedure": ProcedureRecord,
    "program": DrillingProgram,
    "risk": RiskRecord,
    "lesson": LessonLearned,
    # A best practice and a recommendation are records with owners and decisions of their own, so an
    # edge is allowed to point at them: ``LESSON_BEST_PRACTICE`` is how a lesson's evidence travels with
    # the practice it became, and a recommendation's links are its own columns.
    "best_practice": BestPractice,
    "recommendation": Recommendation,
    "pattern": FieldPattern,
    "service_company": ServiceCompany,
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


# --------------------------------------------------------------------------- operations
#: Which link columns point from one well's record to another record that must belong to the same well.
#:
#: A derived foreign key is the schema's promise that the row exists; it says nothing about whether the two
#: rows describe the same hole.  A stuck-pipe problem filed against well A-3 that links to an NPT record on
#: B-11 satisfies every constraint in the database and produces a number that is simply wrong - and it is
#: the kind of wrong that a promoter writing rows from a spreadsheet with a mistyped well column will produce
#: first, which is why the pair is checked here rather than trusted.
_SAME_WELL_LINKS: tuple[tuple[type, str, type], ...] = (
    (NptRecord, "event_id", WellEvent),
    (NptRecord, "report_id", DdrReport),
    (WellOperation, "report_id", DdrReport),
    (WellEvent, "report_id", DdrReport),
    (WellEvent, "operation_id", WellOperation),
    (NptRecord, "operation_id", WellOperation),
    (ProblemOccurrence, "npt_id", NptRecord),
    (ProblemOccurrence, "event_id", WellEvent),
    (ProblemOccurrence, "operation_id", WellOperation),
    (CostItem, "npt_id", NptRecord),
)

#: Section-scoped rows: the section must be a section *of the well the row is filed under*.
_SECTION_OWNERS: tuple[type, ...] = (
    WellOperation,
    WellEvent,
    NptRecord,
    ProblemOccurrence,
    RiskRecord,
    LessonLearned,
)

#: The tables whose revisions form a chain, and whether "current" is a column they carry.
_REVISION_CHAINS: tuple[tuple[type, bool], ...] = (
    (ProcedureRecord, True),
    (DrillingProgram, True),
    (LessonLearned, True),
    (BestPractice, True),
    (RiskRecord, False),
)

#: Rows a document could have produced, and so rows that must be able to show the document.
_PROMOTED_MODELS: tuple[type, ...] = (
    DdrReport,
    WellOperation,
    WellEvent,
    NptRecord,
    ProblemOccurrence,
    CostItem,
    ProcedureRecord,
    DrillingProgram,
)


def check_well_hierarchy(session: Session) -> list[IntegrityProblem]:
    """Every well, section and scoped row agrees about who contains whom.

    The containment chain (project → field → well → section → operation/event/NPT/problem) is spread
    across five tables, so nothing in the schema can state it.  This checks the two ways it actually
    breaks: a well filed under a field of another project, and a section whose own numbers do not make
    sense - because a section whose bottom is above its top turns every depth query that uses it into an
    empty answer that looks like an honest one.
    """
    problems: list[IntegrityProblem] = []
    for well in session.execute(select(Well).order_by(Well.name, Well.id)).scalars():
        if well.field_id:
            field = session.get(Field, str(well.field_id))
            if field is None:
                problems.append(
                    IntegrityProblem(
                        "well",
                        well.id,
                        "cites a field that does not exist",
                        {"field_id": str(well.field_id)},
                    )
                )
            elif well.project_id and field.project_id and field.project_id != well.project_id:
                problems.append(
                    IntegrityProblem(
                        "well",
                        well.id,
                        "is in a field that belongs to another project",
                        {
                            "well_project_id": str(well.project_id),
                            "field_project_id": str(field.project_id),
                            "field_id": str(well.field_id),
                        },
                    )
                )
        if well.project_id and session.get(Project, str(well.project_id)) is None:
            problems.append(
                IntegrityProblem("well", well.id, "cites a project that does not exist")
            )

    by_well: dict[str, list[Any]] = {}
    for section in session.execute(
        select(WellSection).order_by(WellSection.well_id, WellSection.sequence, WellSection.id)
    ).scalars():
        by_well.setdefault(str(section.well_id), []).append(section)
        if (
            section.top_depth_value is not None
            and section.bottom_depth_value is not None
            and float(section.bottom_depth_value) <= float(section.top_depth_value)
        ):
            problems.append(
                IntegrityProblem(
                    "well_section",
                    section.id,
                    "bottom depth is not below its top",
                    {
                        "top_depth_value": section.top_depth_value,
                        "bottom_depth_value": section.bottom_depth_value,
                        "unit": section.bottom_depth_unit or section.top_depth_unit,
                    },
                )
            )
        if (
            section.planned_duration_days is not None and float(section.planned_duration_days) < 0
        ) or (section.actual_duration_days is not None and float(section.actual_duration_days) < 0):
            problems.append(
                IntegrityProblem(
                    "well_section",
                    section.id,
                    "states a negative duration",
                    {
                        "planned_duration_days": section.planned_duration_days,
                        "actual_duration_days": section.actual_duration_days,
                    },
                )
            )
    for well_id, sections in by_well.items():
        seen: dict[int, str] = {}
        for section in sections:
            owner = seen.get(int(section.sequence or 0))
            if owner is not None:
                problems.append(
                    IntegrityProblem(
                        "well_section",
                        section.id,
                        "shares a sequence with another section of the same well",
                        {
                            "well_id": well_id,
                            "sequence": section.sequence,
                            "other_section_id": owner,
                        },
                    )
                )
            seen[int(section.sequence or 0)] = str(section.id)
    return problems


def check_revision_chains(session: Session) -> list[IntegrityProblem]:
    """No revision chain dangles, forks, or comes round again.

    A superseding row is a new row, so the chain is a linked list the database cannot constrain: the
    earlier revision has to exist, only one revision of a code may be current, and a row that another row
    supersedes must not still claim to be current.  The cycle check is not paranoia - a script that
    revised ``A`` from ``B`` and then ``B`` from ``A`` leaves every individual row perfectly valid and
    makes "which procedure are we drilling to?" unanswerable.
    """
    problems: list[IntegrityProblem] = []
    for model, has_current in _REVISION_CHAINS:
        rows = list(session.execute(select(model).order_by(model.id)).scalars())
        known = {str(row.id) for row in rows}
        superseded: set[str] = set()
        reported: set[frozenset[str]] = set()
        for row in rows:
            wanted = str(getattr(row, "supersedes_id", None) or "")
            if not wanted:
                continue
            if wanted not in known:
                problems.append(
                    IntegrityProblem(
                        model.__tablename__,
                        row.id,
                        "cites a revision that does not exist",
                        {"supersedes_id": wanted},
                    )
                )
                continue
            superseded.add(wanted)
            seen = {str(row.id)}
            walker = session.get(model, wanted)
            while walker is not None:
                seen.add(str(walker.id))
                nxt = str(getattr(walker, "supersedes_id", None) or "")
                if not nxt:
                    break
                if nxt in seen:
                    # One report per cycle, not one per row in it: two rows pointing at each other is one
                    # problem, and a doctor that printed it twice would read as two.
                    shape = frozenset(seen)
                    if shape not in reported:
                        reported.add(shape)
                        problems.append(
                            IntegrityProblem(
                                model.__tablename__,
                                row.id,
                                "revision chain is a cycle",
                                {"cycle": sorted(seen)},
                            )
                        )
                    break
                walker = session.get(model, nxt)
        if not has_current:
            continue
        for row in rows:
            if str(row.id) in superseded and bool(getattr(row, "is_current", False)):
                problems.append(
                    IntegrityProblem(
                        model.__tablename__,
                        row.id,
                        "is superseded and still marked current",
                    )
                )
        currents: dict[str, list[str]] = {}
        for row in rows:
            if not bool(getattr(row, "is_current", False)):
                continue
            code = str(getattr(row, "code", "") or "")
            if code:
                currents.setdefault(code, []).append(str(row.id))
        for code, ids in sorted(currents.items()):
            if len(ids) > 1:
                problems.append(
                    IntegrityProblem(
                        model.__tablename__,
                        ids[0],
                        "two revisions are current for one code",
                        {"code": code, "ids": ids},
                    )
                )
    return problems


def check_cross_well_links(session: Session) -> list[IntegrityProblem]:
    """A link between two records of two different wells, or a link to a row that is not there.

    Reported separately from the schema's own foreign keys on purpose: SQLite does not enforce them by
    default, so a dangling ``npt_id`` is possible in exactly the deployments this platform runs on.
    """
    problems: list[IntegrityProblem] = []
    for model, column, target in _SAME_WELL_LINKS:
        for row in session.execute(select(model).order_by(model.id)).scalars():
            wanted = str(getattr(row, column, None) or "")
            if not wanted:
                continue
            other = session.get(target, wanted)
            if other is None:
                problems.append(
                    IntegrityProblem(
                        model.__tablename__,
                        row.id,
                        f"links a {target.__tablename__} that does not exist",
                        {column: wanted},
                    )
                )
                continue
            mine, theirs = getattr(row, "well_id", None), getattr(other, "well_id", None)
            if mine and theirs and str(mine) != str(theirs):
                problems.append(
                    IntegrityProblem(
                        model.__tablename__,
                        row.id,
                        f"links a {target.__tablename__} on another well",
                        {column: wanted, "well_id": str(mine), "other_well_id": str(theirs)},
                    )
                )
    sections = {
        str(row.id): str(row.well_id)
        for row in session.execute(select(WellSection.id, WellSection.well_id)).all()
    }
    for model in _SECTION_OWNERS:
        for row in session.execute(select(model).order_by(model.id)).scalars():
            section_id = str(getattr(row, "section_id", None) or "")
            if not section_id:
                continue
            owner = sections.get(section_id)
            if owner is None:
                problems.append(
                    IntegrityProblem(
                        model.__tablename__,
                        row.id,
                        "cites a section that does not exist",
                        {"section_id": section_id},
                    )
                )
            elif getattr(row, "well_id", None) and str(row.well_id) != owner:
                problems.append(
                    IntegrityProblem(
                        model.__tablename__,
                        row.id,
                        "is filed under a section of another well",
                        {"section_id": section_id, "section_well_id": owner},
                    )
                )
    for model in _PROMOTED_MODELS:
        columns = {column.name for column in model.__table__.columns}
        if "document_id" not in columns or "document_version_id" not in columns:
            continue
        for row in session.execute(select(model).order_by(model.id)).scalars():
            version_id = str(row.document_version_id or "")
            if not version_id:
                continue
            version = session.get(DocumentVersion, version_id)
            if version is None:
                problems.append(
                    IntegrityProblem(
                        model.__tablename__,
                        row.id,
                        "cites a document version that does not exist",
                        {"document_version_id": version_id},
                    )
                )
            elif row.document_id and str(version.document_id) != str(row.document_id):
                problems.append(
                    IntegrityProblem(
                        model.__tablename__,
                        row.id,
                        "names a document that is not its version's document",
                        {
                            "document_id": str(row.document_id),
                            "version_document_id": str(version.document_id),
                        },
                    )
                )
    return problems


def check_promoted_evidence(session: Session) -> list[IntegrityProblem]:
    """A row that came from a document can still show the document.

    ``origin`` says where a row came from, and provenance is the promise that goes with it.  A derived row
    with no evidence is the failure mode this platform exists to prevent: somebody quotes a number from
    the database, and there is nowhere to go back to.  The repair is not to invent a source - it is to
    re-promote from the file, or to mark the row as something a person asserted.
    """
    from ..core.enums import KnowledgeOrigin

    problems: list[IntegrityProblem] = []
    for model in _PROMOTED_MODELS:
        columns = {column.name for column in model.__table__.columns}
        if "origin" not in columns or "provenance" not in columns:
            continue
        statement = select(model).where(model.origin == KnowledgeOrigin.DERIVED.value)
        for row in session.execute(statement.order_by(model.id)).scalars():
            evidence = list(row.provenance or [])
            if not evidence:
                problems.append(
                    IntegrityProblem(
                        model.__tablename__,
                        row.id,
                        "is derived from a document and cites no evidence",
                        {"document_version_id": str(getattr(row, "document_version_id", "") or "")},
                    )
                )
    return problems


def check_operational_integrity(session: Session) -> list[IntegrityProblem]:
    """All four operational checks in one call, which is what ``doctor`` and a test both want.

    Grouped because they answer one question - can these rows be read as a field's history? - and because
    a partial answer would be misread as a clean bill of health.
    """
    return (
        check_well_hierarchy(session)
        + check_revision_chains(session)
        + check_cross_well_links(session)
        + check_promoted_evidence(session)
    )
