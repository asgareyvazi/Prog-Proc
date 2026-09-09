"""Retrieval & Evidence: the authoritative verification boundary.

The layering this module enforces::

    Search     -> candidate discovery (a disposable index, may be stale)
    Retrieval  -> authoritative verification (re-read the database, never the sidecar)
    Evidence   -> a verified, provenance-carrying, deterministically-identified answer

Search is asked only to *locate* candidates.  Every candidate it returns is then re-read from the
authoritative database by its own identity; a row that is gone, outside the requested scope, or
not current under the requested policy is dropped and reported in :attr:`EvidenceBundle.dropped`,
never silently absorbed.  That is the whole guarantee: **a stale sidecar row cannot become
authoritative evidence**, because nothing here trusts the sidecar past the point of discovery.

The rules it keeps, each of which a forensic test pins down:

*   **The re-read is the authority.**  Status, scope and names are read from the database rows,
    not copied from the index row that surfaced the candidate.
*   **Scope follows the platform's one precedence** (a named well is the whole scope, never a union
    with a field or project), and is re-checked against the authoritative row.
*   **Current and history are explicit.**  ``current`` returns only the rows the domain answers
    "now" (the domain's own lifecycle rules, per source type); ``history`` returns those plus the
    superseded/retired/rejected rows, each labelled with its state.  Nothing is "latest by
    timestamp".
*   **Read-only, caller's transaction is the caller's.**  A retrieval opens a read-only session (or
    borrows the caller's) and never commits, so it cannot persist a caller's in-flight change.
*   **Bounded reads.**  Candidates are re-read in batches by identity - one ``id IN (...)`` per
    structured type actually present, plus a few for versions, documents and the scope names - so a
    result of one, ten or a hundred rows costs a bounded number of queries, not one per row.
*   **Broadened discovery is labelled.**  Retrieval never broadens a query itself, but when the
    exact all-terms AND finds nothing the search layer falls back to any-of-the-terms, and the
    bundle carries that as ``discovery_broadened`` so a broadened answer is never read as an exact
    one.
"""

from __future__ import annotations

from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any

from sqlalchemy import select

from ..core.enums import (
    ConfirmationStatus,
    KnowledgeStatus,
    RecommendationLifecycle,
)
from ..core.errors import ValidationError
from ..database.models import (
    Document,
    DocumentVersion,
    Field,
    KnowledgeItem,
    LessonLearned,
    NptRecord,
    ProblemDefinition,
    ProblemOccurrence,
    Project,
    Recommendation,
    Well,
    WellEvent,
)
from .contract import (
    LIFECYCLE_CURRENT,
    LIFECYCLE_HISTORY,
    SOURCE_DOCUMENT,
    SOURCE_KNOWLEDGE,
    SOURCE_STRUCTURED,
    EvidenceBundle,
    EvidenceItem,
    RetrievalRequest,
)

__all__ = ["RetrievalService"]

#: The authoritative model for each structured record type.
_STRUCTURED_MODELS: dict[str, type] = {
    "problem_definition": ProblemDefinition,
    "problem_occurrence": ProblemOccurrence,
    "npt_record": NptRecord,
    "well_event": WellEvent,
    "lesson_learned": LessonLearned,
    "recommendation": Recommendation,
}

#: The ``kind`` a knowledge-fact chunk carries (a fact is surfaced as a document-shaped chunk).
_KIND_KNOWLEDGE = "knowledge_fact"

#: The search candidate cap retrieval asks for when the caller wants no cap of its own.
_DISCOVERY_CAP = 200


def _iso(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    iso = getattr(value, "isoformat", None)
    return str(iso()) if callable(iso) else str(value)


def _row_provenance(row: Any) -> dict[str, Any] | list[Any]:
    """A structured row's own provenance, carried as stored - a mapping or its evidence list.

    The column holds the record's evidence entries (a list), so it is returned as a plain list; an
    empty or missing value is an empty list, never an invented locator.
    """
    value = getattr(row, "provenance", None)
    if value is None:
        return []
    if isinstance(value, Mapping):
        return dict(value)
    return [dict(item) if isinstance(item, Mapping) else item for item in value]


@dataclass(frozen=True)
class _Scope:
    """The resolved request scope, with the set of wells it covers."""

    level: str  # well | field | project | all
    well_id: str = ""
    field_id: str = ""
    project_id: str = ""
    well_ids: frozenset[str] = frozenset()

    def contains(self, well_id: str, field_id: str, project_id: str) -> bool:
        """Whether a record's authoritative scope columns fall inside this scope.

        A record with no scope of its own (a canonical definition) is in scope only when no scope
        was requested - the same behaviour search's structured filter already has, so retrieval and
        discovery cannot disagree about what a scope means.
        """
        if self.level == "well":
            return well_id == self.well_id
        if self.level == "field":
            return field_id == self.field_id or (well_id and well_id in self.well_ids)
        if self.level == "project":
            return project_id == self.project_id or (well_id and well_id in self.well_ids)
        return True


@dataclass(frozen=True)
class _Names:
    """Authoritative id -> display name maps, loaded once per retrieval."""

    wells: Mapping[str, Any]
    fields: Mapping[str, Any]
    projects: Mapping[str, Any]

    def well_name(self, well_id: str) -> str:
        well = self.wells.get(well_id)
        return str(well.name) if well is not None else ""

    def field_name(self, field_id: str) -> str:
        field = self.fields.get(field_id)
        return str(field.name) if field is not None else ""

    def project_name(self, project_id: str) -> str:
        project = self.projects.get(project_id)
        return str(project.name) if project is not None else ""

    def field_for_well(self, well_id: str) -> str:
        well = self.wells.get(well_id)
        return str(getattr(well, "field_id", "") or "") if well is not None else ""

    def project_for_well(self, well_id: str) -> str:
        well = self.wells.get(well_id)
        return str(getattr(well, "project_id", "") or "") if well is not None else ""


class RetrievalService:
    """Re-reads search candidates from the authoritative database and returns verified evidence."""

    def __init__(self, *, database: Any, search_service: Any | None = None) -> None:
        if database is None:
            raise ValidationError("a retrieval service needs the authoritative database")
        self.database = database
        self._search = search_service

    @classmethod
    def for_workspace(cls, workspace: Any, *, in_memory: bool = False) -> RetrievalService:
        """Bind retrieval to an open workspace, reusing its search service for discovery."""
        from ..search.service import SearchService

        return cls(
            database=workspace.database,
            search_service=SearchService.for_workspace(workspace, in_memory=in_memory),
        )

    # -- session ownership ----------------------------------------------------
    @contextmanager
    def _authority(self, session: Any | None) -> Iterator[Any]:
        """The session every authoritative read goes through.

        A caller may pass its own session (the retrieval then reads inside the caller's
        transaction and never commits it); otherwise a read-only session is opened for the call and
        closed after it.  Either way, this method does not commit.
        """
        if session is not None:
            yield session
            return
        with self.database.read_only() as active:
            yield active

    # -- the one entry point --------------------------------------------------
    def retrieve(self, request: RetrievalRequest, *, session: Any | None = None) -> EvidenceBundle:
        """Answer one retrieval request: discover candidates, verify each against the database."""
        req = (
            request if isinstance(request, RetrievalRequest) else RetrievalRequest(**dict(request))
        )
        with self._authority(session) as active:
            scope = self._resolve_scope(active, req)
            candidates, broadened = self._discover(req)
            return self._verify(active, req, scope, candidates, broadened)

    # -- discovery: search is the only candidate source ------------------------
    def _discover(self, req: RetrievalRequest) -> tuple[list[Any], bool]:
        """The ranked candidates from the search layer, plus whether discovery was broadened.

        An empty query has no terms to match, so there is nothing to verify and the answer is
        empty - an empty bundle, not an error, because "no question" is a valid, deterministic
        answer (and a search over no terms is already defined to match nothing).

        The second element is search's own ``broadened`` flag: retrieval never broadens a query on
        its own, but when the exact all-terms AND finds nothing the search layer falls back to
        any-of-the-terms, and the bundle must say so - broadened discovery is not an exact match.
        """
        if not str(req.query or "").strip():
            return [], False
        if self._search is None:
            raise ValidationError(
                "no search service is bound; retrieval discovers candidates through search",
                hint="bind a workspace so the disposable index is available",
            )
        cap = req.limit if req.limit > 0 else _DISCOVERY_CAP
        # The scope is a single level decided by the platform's precedence (well beats field beats
        # project).  Only that level is passed to search: handing it well_id AND field_id would
        # make search AND them (the empty intersection), and ORing them is exactly the union the
        # precedence exists to forbid.  The authoritative re-check in _verify then re-applies the
        # same single level to the database row.
        if req.well_id:
            scope_kwargs = {"well_id": req.well_id}
        elif req.field_id:
            scope_kwargs = {"field_id": req.field_id}
        elif req.project_id:
            scope_kwargs = {"project_id": req.project_id}
        else:
            scope_kwargs = {}
        response = self._search.search(
            req.query,
            date_from=req.date_from,
            date_to=req.date_to,
            limit=cap,
            # A history answer must be able to *discover* superseded document versions; a current
            # answer must not.  (Historical structured rows are a different matter: the search
            # projection stores only the domain's current structured rows, so their history is what
            # the index still holds - retrieval verifies it but never invents it.)
            include_superseded=req.lifecycle == LIFECYCLE_HISTORY,
            **scope_kwargs,
        )
        return list(response.results), response.broadened

    # -- scope ----------------------------------------------------------------
    def _resolve_scope(self, active: Any, req: RetrievalRequest) -> _Scope:
        """Resolve the requested scope to the wells it covers, refusing ids that do not exist.

        A scope that names a row the database does not have is a caller error, not an empty answer -
        answering "nothing" to "well X that does not exist" would hide the typo.
        """
        if req.well_id:
            well = active.get(Well, req.well_id)
            if well is None:
                raise ValidationError(f"no well {req.well_id!r}")
            return _Scope(
                level="well",
                well_id=well.id,
                field_id=str(well.field_id or ""),
                project_id=str(well.project_id or ""),
                well_ids=frozenset({well.id}),
            )
        if req.field_id:
            field = active.get(Field, req.field_id)
            if field is None:
                raise ValidationError(f"no field {req.field_id!r}")
            well_ids = {
                str(w)
                for w in active.execute(select(Well.id).where(Well.field_id == field.id)).scalars()
            }
            return _Scope(level="field", field_id=field.id, well_ids=frozenset(well_ids))
        if req.project_id:
            project = active.get(Project, req.project_id)
            if project is None:
                raise ValidationError(f"no project {req.project_id!r}")
            well_ids = {
                str(w)
                for w in active.execute(
                    select(Well.id).where(Well.project_id == project.id)
                ).scalars()
            }
            return _Scope(level="project", project_id=project.id, well_ids=frozenset(well_ids))
        return _Scope(level="all")

    # -- verification ---------------------------------------------------------
    def _verify(
        self,
        active: Any,
        req: RetrievalRequest,
        scope: _Scope,
        candidates: Sequence[Any],
        broadened: bool,
    ) -> EvidenceBundle:
        wanted = set(req.source_types) or {SOURCE_STRUCTURED, SOURCE_DOCUMENT, SOURCE_KNOWLEDGE}
        # Partition the candidates by what must be re-read, so each authoritative table is queried
        # a bounded number of times rather than once per candidate.
        structured_ids: dict[str, list[str]] = {}
        version_ids: set[str] = set()
        document_ids: set[str] = set()
        plan: list[tuple[Any, str, str, str]] = []  # (result, source, identity, version_id)
        for result in candidates:
            source, identity, source_id, version_id = self._candidate_key(result)
            if source not in wanted:
                continue
            if source == SOURCE_STRUCTURED:
                structured_ids.setdefault(identity.split(":")[1], []).append(source_id)
            else:
                if version_id:
                    version_ids.add(version_id)
                document_id = str(result.document_id or "")
                if document_id:
                    document_ids.add(document_id)
            plan.append((result, source, identity, version_id))

        rows = self._batch_read(
            active, structured_ids, version_ids, document_ids, {v for *_, v in plan if v}
        )
        names = self._load_names(active, rows)
        items: list[EvidenceItem] = []
        dropped: list[dict[str, Any]] = []
        for result, source, identity, version_id in plan:
            outcome = self._one(req, scope, result, source, identity, version_id, rows, names)
            if isinstance(outcome, EvidenceItem):
                items.append(outcome)
            else:
                dropped.append({"identity": identity, "source_type": source, "reason": outcome})
        items = items[: req.limit] if req.limit > 0 else items
        scope_dict = {
            "level": scope.level,
            "well_id": scope.well_id,
            "field_id": scope.field_id,
            "project_id": scope.project_id,
        }
        return EvidenceBundle(
            request=req.to_dict(),
            items=tuple(items),
            dropped=tuple(dropped),
            policy=req.lifecycle,
            scope=scope_dict,
            discovery_broadened=broadened,
        )

    def _candidate_key(self, result: Any) -> tuple[str, str, str, str]:
        """The (source type, discovery identity, authoritative row id, version id) of a candidate."""
        if result.source_type == "structured":
            record_type = str(result.metadata.get("record_type") or "")
            source_id = str(result.metadata.get("source_id") or result.chunk_id or "")
            return (SOURCE_STRUCTURED, f"structured:{record_type}:{source_id}", source_id, "")
        version_id = str(result.version_id or "")
        document_id = str(result.document_id or "")
        source = (
            SOURCE_KNOWLEDGE if getattr(result, "kind", "") == _KIND_KNOWLEDGE else SOURCE_DOCUMENT
        )
        return (source, f"document:{document_id}:{version_id}:{result.chunk_id}", "", version_id)

    def _batch_read(
        self,
        active: Any,
        structured_ids: Mapping[str, Sequence[str]],
        version_ids: set[str],
        document_ids: set[str],
        knowledge_versions: set[str],
    ) -> dict[str, Any]:
        """Every authoritative row the plan needs, in a bounded number of queries.

        One ``id IN (...)`` per structured type actually present, one for versions, one for
        documents, one for the knowledge items behind the fact chunks - never a query per candidate.
        """
        rows: dict[str, Any] = {"structured": {}, "versions": {}, "documents": {}, "knowledge": {}}
        for record_type, ids in structured_ids.items():
            model = _STRUCTURED_MODELS.get(record_type)
            if model is None or not ids:
                continue
            found = (
                active.execute(select(model).where(model.id.in_(sorted(set(ids))))).scalars().all()
            )
            for row in found:
                rows["structured"][(record_type, str(row.id))] = row
        if version_ids:
            found = (
                active.execute(
                    select(DocumentVersion).where(DocumentVersion.id.in_(sorted(version_ids)))
                )
                .scalars()
                .all()
            )
            rows["versions"] = {str(v.id): v for v in found}
        if document_ids:
            found = (
                active.execute(select(Document).where(Document.id.in_(sorted(document_ids))))
                .scalars()
                .all()
            )
            rows["documents"] = {str(d.id): d for d in found}
        if knowledge_versions:
            found = (
                active.execute(
                    select(KnowledgeItem).where(
                        KnowledgeItem.document_version_id.in_(sorted(knowledge_versions))
                    )
                )
                .scalars()
                .all()
            )
            by_version: dict[str, list] = {}
            for item in found:
                by_version.setdefault(str(item.document_version_id or ""), []).append(item)
            rows["knowledge"] = by_version
        return rows

    def _load_names(self, active: Any, rows: Mapping[str, Any]) -> _Names:
        """The authoritative names for every scope id the plan touches, in three bounded queries."""
        well_ids: set[str] = set()
        field_ids: set[str] = set()
        project_ids: set[str] = set()
        for row in rows["structured"].values():
            well_ids.add(str(getattr(row, "well_id", "") or ""))
            field_ids.add(str(getattr(row, "field_id", "") or ""))
            project_ids.add(str(getattr(row, "project_id", "") or ""))
        for document in rows["documents"].values():
            well_ids.add(str(document.well_id or ""))
            project_ids.add(str(document.project_id or ""))
        for item in (rows["knowledge"] or {}).values():
            for entry in item:
                well_ids.add(str(entry.well_id or ""))
                project_ids.add(str(entry.project_id or ""))
        well_ids.discard("")
        field_ids.discard("")
        project_ids.discard("")
        wells = self._by_id(active, Well, well_ids)
        fields = self._by_id(active, Field, field_ids)
        projects = self._by_id(active, Project, project_ids)
        return _Names(wells=wells, fields=fields, projects=projects)

    @staticmethod
    def _by_id(active: Any, model: type, ids: set[str]) -> dict[str, Any]:
        if not ids:
            return {}
        found = active.execute(select(model).where(model.id.in_(sorted(ids)))).scalars().all()
        return {str(row.id): row for row in found}

    # -- one candidate -> one evidence item (or a drop reason) ----------------
    def _one(
        self,
        req: RetrievalRequest,
        scope: _Scope,
        result: Any,
        source: str,
        identity: str,
        version_id: str,
        rows: Mapping[str, Any],
        names: _Names,
    ) -> EvidenceItem | str:
        if source == SOURCE_STRUCTURED:
            record_type = str(result.metadata.get("record_type") or "")
            source_id = str(result.metadata.get("source_id") or result.chunk_id or "")
            row = rows["structured"].get((record_type, source_id))
            if row is None:
                return "no longer in the authoritative database"
            return self._structured_item(req, scope, result, record_type, row, names)
        if source == SOURCE_DOCUMENT:
            return self._document_item(req, scope, result, version_id, rows, names)
        if source == SOURCE_KNOWLEDGE:
            return self._knowledge_item(req, scope, result, version_id, rows, names)
        return "unknown source type"

    def _structured_item(
        self,
        req: RetrievalRequest,
        scope: _Scope,
        result: Any,
        record_type: str,
        row: Any,
        names: _Names,
    ) -> EvidenceItem | str:
        # Some record types carry only a well (occurrences, NPT, events); the field and project
        # for those come from the well's own scope, read from the authoritative well row.
        well_id = str(getattr(row, "well_id", "") or "")
        field_id = str(getattr(row, "field_id", "") or "") or names.field_for_well(well_id)
        project_id = str(getattr(row, "project_id", "") or "") or names.project_for_well(well_id)
        if not scope.contains(well_id, field_id, project_id):
            return "outside the requested scope"
        status = str(getattr(row, "status", "") or "")
        current = self._structured_current(record_type, row, status)
        if req.lifecycle == LIFECYCLE_CURRENT and not current:
            return f"not current (status {status or 'unknown'})"
        return EvidenceItem(
            identity=f"structured:{record_type}:{row.id}",
            source_type=SOURCE_STRUCTURED,
            record_type=record_type,
            source_id=str(row.id),
            well_id=well_id,
            field_id=field_id,
            project_id=project_id,
            well_name=names.well_name(well_id),
            field_name=names.field_name(field_id),
            project_name=names.project_name(project_id),
            status=status,
            current=current,
            document_id=str(getattr(row, "document_id", "") or ""),
            document_version_id=str(getattr(row, "document_version_id", "") or ""),
            locator_ref="",
            provenance=_row_provenance(row),
            title=self._structured_title(record_type, row),
            text=self._structured_text(record_type, row),
            record_date=self._structured_date(record_type, row),
            score=float(result.score),
            matched_terms=tuple(result.matched_terms),
            verified=True,
        )

    @staticmethod
    def _structured_current(record_type: str, row: Any, status: str) -> bool:
        """The domain's own "is this the answer now" rule, per record type."""
        if record_type == "problem_definition":
            return True
        if record_type in {"problem_occurrence", "npt_record", "well_event"}:
            return status != ConfirmationStatus.REJECTED.value
        if record_type == "lesson_learned":
            return bool(getattr(row, "is_current", True))
        if record_type == "recommendation":
            return status != RecommendationLifecycle.SUPERSEDED.value
        return True

    @staticmethod
    def _structured_title(record_type: str, row: Any) -> str:
        if record_type == "problem_definition":
            return str(row.name or "")
        if record_type == "problem_occurrence":
            return f"Problem - {row.problem_type}"
        if record_type == "npt_record":
            return f"NPT - {row.category}"
        if record_type == "lesson_learned":
            return str(row.title or "")
        if record_type == "recommendation":
            return str((row.statement or "").split(".")[0][:120])
        return str(getattr(row, "label", "") or f"Event - {getattr(row, 'event_type', '')}")

    @staticmethod
    def _structured_text(record_type: str, row: Any) -> str:
        """The record's own narrative, read from the authoritative row (never the sidecar's copy)."""
        if record_type == "lesson_learned":
            return str(row.lesson or "")
        if record_type == "recommendation":
            return str(row.statement or "")
        return str(getattr(row, "description", "") or "")

    @staticmethod
    def _structured_date(record_type: str, row: Any) -> str:
        if record_type == "npt_record":
            return _iso(getattr(row, "started_at", None))
        if record_type == "lesson_learned":
            return _iso(getattr(row, "approved_at", None))
        if record_type == "recommendation":
            return _iso(getattr(row, "decided_at", None))
        return _iso(getattr(row, "occurred_at", None))

    def _document_item(
        self,
        req: RetrievalRequest,
        scope: _Scope,
        result: Any,
        version_id: str,
        rows: Mapping[str, Any],
        names: _Names,
    ) -> EvidenceItem | str:
        version = rows["versions"].get(version_id)
        if version is None:
            return "no longer in the authoritative database"
        document = rows["documents"].get(str(result.document_id or ""))
        if document is None:
            return "no longer in the authoritative database"
        if str(version.document_id or "") != str(document.id):
            # The sidecar pairs a version with a document that no longer match: stale projection.
            return "no longer in the authoritative database"
        if not result.cited:
            # A diagnostic or page chunk has no recorded location: it is an extraction note, not a
            # citation.  Returning it as evidence would be manufacturing a source.
            return "not citable (no recorded location)"
        # The scope columns live on the document; a version only points at it.
        well_id = str(document.well_id or "")
        project_id = str(document.project_id or "")
        field_id = names.field_for_well(well_id)
        if not scope.contains(well_id, field_id, project_id):
            return "outside the requested scope"
        current = bool(version.is_current)
        if req.lifecycle == LIFECYCLE_CURRENT and not current:
            return "not current (superseded version)"
        return EvidenceItem(
            identity=f"document:{result.document_id}:{version_id}:{result.chunk_id}",
            source_type=SOURCE_DOCUMENT,
            record_type=str(result.kind or ""),
            source_id=str(result.chunk_id),
            well_id=well_id,
            field_id=field_id,
            project_id=project_id,
            well_name=names.well_name(well_id),
            field_name=names.field_name(field_id),
            project_name=names.project_name(project_id),
            status=str(document.status or ""),
            current=current,
            document_id=str(result.document_id or ""),
            document_version_id=version_id,
            locator_ref=str(result.locator_ref or ""),
            provenance=dict(result.provenance or {}),
            # Computed by search against the full chunk text: a citation check later can only be
            # an excerpt comparison when the item actually reads as a quotation of its region.
            verbatim=bool(result.verbatim),
            title=str(document.title or document.filename or ""),
            text=result.snippet or result.text or "",
            record_date=_iso(document.document_date),
            score=float(result.score),
            matched_terms=tuple(result.matched_terms),
            verified=True,
        )

    def _knowledge_item(
        self,
        req: RetrievalRequest,
        scope: _Scope,
        result: Any,
        version_id: str,
        rows: Mapping[str, Any],
        names: _Names,
    ) -> EvidenceItem | str:
        version = rows["versions"].get(version_id)
        if version is None:
            return "no longer in the authoritative database"
        item = self._match_knowledge_item(rows["knowledge"].get(version_id, []), result)
        if item is None:
            # The sidecar still lists a fact the authoritative table no longer holds (or whose
            # content changed): it is a stale projection, not evidence.
            return "no longer in the authoritative database"
        well_id = str(item.well_id or "")
        project_id = str(item.project_id or "")
        field_id = names.field_for_well(well_id)
        if not scope.contains(well_id, field_id, project_id):
            return "outside the requested scope"
        status = str(item.status or "")
        current = status not in {
            KnowledgeStatus.SUPERSEDED.value,
            KnowledgeStatus.RETIRED.value,
        }
        if req.lifecycle == LIFECYCLE_CURRENT and not current:
            return f"not current (status {status})"
        return EvidenceItem(
            identity=f"knowledge:{item.id}",
            source_type=SOURCE_KNOWLEDGE,
            record_type="knowledge_item",
            source_id=str(item.id),
            well_id=well_id,
            field_id=field_id,
            project_id=project_id,
            well_name=names.well_name(well_id),
            field_name=names.field_name(field_id),
            project_name=names.project_name(project_id),
            status=status,
            current=current,
            document_id=str(item.document_id or ""),
            document_version_id=str(item.document_version_id or version_id),
            locator_ref=str(result.locator_ref or ""),
            provenance=dict(result.provenance or {}),
            # A fact is a rendering of the source cell, not a quotation of it - its citation is
            # checkable by the source file's hash, and by excerpt only when the chunk does quote it.
            verbatim=bool(result.verbatim),
            title=str(item.predicate or ""),
            text=result.snippet or result.text or "",
            record_date=_iso(item.valid_from),
            score=float(result.score),
            matched_terms=tuple(result.matched_terms),
            verified=True,
        )

    @staticmethod
    def _match_knowledge_item(items: Sequence[Any], result: Any) -> Any | None:
        """The authoritative item a fact chunk was projected from.

        Matched by re-building the chunk's deterministic lines (``predicate: ...`` and
        ``as written: ...``) from each candidate item and requiring them to appear verbatim in the
        chunk text - an exact, parse-free comparison that a stale or reshaped sidecar row cannot
        fake.  When several items would match (two sections stating the same value), the first by
        id is returned, which keeps the answer deterministic.
        """
        if not items:
            return None
        text_lines = {line.strip() for line in str(result.text or "").splitlines() if line.strip()}
        for item in sorted(items, key=lambda row: str(row.id or "")):
            lines: set[str] = set()
            predicate = str(item.predicate or "")
            if predicate:
                lines.add(f"predicate: {predicate.replace('_', ' ')}")
            written = f"{item.original_value or ''} {item.original_unit or ''}".strip()
            if written:
                lines.add(f"as written: {written}")
            if lines and lines <= text_lines:
                return item
        return None
