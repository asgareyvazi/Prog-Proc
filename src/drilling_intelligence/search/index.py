"""The search index: a derived, disposable, rebuildable structure over extracted text.

Rules this module is built around, all of them load-bearing:

*   **The registry is the authority.**  Everything here is computed from
    ``extraction.document_json`` - no re-parsing, no re-hashing - so a rebuild after a crash, a
    corrupted sidecar or a move to another machine reproduces the same index from the same rows.
    Deleting ``index/search_index.db`` costs time, never information.
*   **Ranking is not a black box.**  FTS5 (when the SQLite build has it) is used only to obtain
    candidate chunk ids.  Matching, filtering, scoring and ordering all happen in
    :mod:`drilling_intelligence.search.ranking`, shared with :class:`InMemorySearchIndex`, so a
    query returns the same list with or without the optional extension.
*   **A result cites a location.**  Every chunk carries its locator text and the provenance the
    extractor recorded; the service refuses to present an uncited body-text hit as a fact.
*   **Superseded is not searchable, and removed files still are.**  :meth:`SearchIndex.prune_obsolete`
    drops chunks of versions that are no longer current - so a search answers "what should I act
    on" - while a document whose *file* disappeared keeps its chunks, because the record survives
    its folder by design.

Two backends are provided.  :class:`SqliteSearchIndex` is the product path: a sidecar database,
FTS5-accelerated when the extension is present.  :class:`InMemorySearchIndex` is the same
algorithm without a database - real search over real chunks - used by tests as the parity
reference for the SQLite path and by ``doctor`` when the data directory cannot be written to.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from typing import Any, Protocol

from sqlalchemy import (
    Column,
    Integer,
    MetaData,
    String,
    Table,
    Text,
    delete,
    func,
    insert,
    select,
    update,
)
from sqlalchemy import inspect as sa_inspect
from sqlalchemy import text as sa_text

from ..core.logging import get_logger
from ..documents.repository import DocumentRepository
from ..extraction.normalized import NormalizedDocument
from .chunking import ChunkSet, IndexChunk, IndexDocument, build_chunk_set
from .ranking import IndexStatistics, rank_chunks
from .tokenize import parse_query

log = get_logger("search.index")

__all__ = [
    "FTS_TABLE",
    "MAX_CANDIDATES",
    "RETRIEVAL_CAP",
    "SCHEMA_VERSION",
    "InMemorySearchIndex",
    "IndexStats",
    "SearchFilters",
    "SearchIndex",
    "SearchRequest",
    "SqliteSearchIndex",
    "chunk_set_for",
    "index_document_for",
    "score_candidates",
    "search_metadata",
]

#: How many candidate chunks one query may score.  Ranking is O(candidates), and a common word
#: in a large workspace would otherwise score the whole corpus; the cap bounds worst-case
#: latency, and ``truncated`` in the result metadata says when it bit.
MAX_CANDIDATES = 4000

FTS_TABLE = "search_chunk_fts"

#: How many candidate chunks one query fetches from the index before Python scores them.
RETRIEVAL_CAP = MAX_CANDIDATES * 4


#: Bumping this is how an index built by an older build is detected and rebuilt, rather than
#: queried with a schema the running code no longer understands.
SCHEMA_VERSION = 1

#: The sidecar schema.  Deliberately not under Alembic (ADR-0003): it is derived data with one
#: owner, and it can always be dropped and rebuilt from the registry.
search_metadata = MetaData()

search_document_table = Table(
    "search_document",
    search_metadata,
    Column("version_id", String(36), primary_key=True),
    Column("document_id", String(36), nullable=False, index=True),
    Column("version_number", Integer, nullable=False, default=0),
    Column("workspace_id", String(36), nullable=False, default="", index=True),
    Column("project_id", String(36), nullable=False, default="", index=True),
    Column("company_id", String(36), nullable=False, default="", index=True),
    Column("project_name", String(200), nullable=False, default=""),
    Column("company_name", String(200), nullable=False, default=""),
    Column("well_id", String(36), nullable=False, default="", index=True),
    Column("well_name", String(200), nullable=False, default=""),
    Column("document_type", String(40), nullable=False, default="", index=True),
    Column("title", String(400), nullable=False, default=""),
    Column("filename", String(512), nullable=False, default=""),
    Column("identity_path", String(1024), nullable=False, default=""),
    Column("source_relative_path", String(1024), nullable=False, default=""),
    Column("extension", String(16), nullable=False, default=""),
    Column("parser", String(64), nullable=False, default=""),
    Column("revision", String(64), nullable=False, default=""),
    Column("revision_key", Integer, nullable=False, default=0),
    Column("status", String(32), nullable=False, default=""),
    Column("processing_status", String(24), nullable=False, default=""),
    Column("source_authority", String(64), nullable=False, default=""),
    Column("document_date", String(32), nullable=False, default="", index=True),
    Column("imported_at", String(32), nullable=False, default=""),
    Column("page_count", Integer, nullable=False, default=0),
    Column("sheet_count", Integer, nullable=False, default=0),
    Column("word_count", Integer, nullable=False, default=0),
    Column("size_bytes", Integer, nullable=False, default=0),
    Column("sha256", String(64), nullable=False, default="", index=True),
    Column("is_current", Integer, nullable=False, default=1, index=True),
    Column("diagnostics", Text, nullable=False, default="[]"),
    Column("chunk_count", Integer, nullable=False, default=0),
)

search_chunk_table = Table(
    "search_chunk",
    search_metadata,
    Column("chunk_id", String(64), primary_key=True),
    Column("document_id", String(36), nullable=False, index=True),
    Column("version_id", String(36), nullable=False, index=True),
    Column("chunk_index", Integer, nullable=False, default=0),
    Column("kind", String(24), nullable=False, default="paragraph", index=True),
    Column("text", Text, nullable=False, default=""),
    Column("page", Integer, nullable=True),
    Column("sheet", String(200), nullable=False, default="", index=True),
    Column("locator_ref", String(400), nullable=False, default=""),
    Column("provenance_json", Text, nullable=True),
    Column("terms_json", Text, nullable=False, default="{}"),
    Column("length", Integer, nullable=False, default=0),
    Column("char_count", Integer, nullable=False, default=0),
    Column("source_sha256", String(64), nullable=False, default=""),
)

search_meta_table = Table(
    "search_meta",
    search_metadata,
    Column("key", String(64), primary_key=True),
    Column("value", Text, nullable=False, default=""),
)


# --------------------------------------------------------------------------- requests
@dataclass(frozen=True)
class SearchFilters:
    """Everything a query may be narrowed to.  ``None`` means "do not filter on this"."""

    workspace_id: str | None = None
    project_id: str | None = None
    company_id: str | None = None
    well_id: str | None = None
    document_type: str | None = None
    document_types: tuple[str, ...] | None = None
    revision: str | None = None
    status: str | None = None
    processing_status: str | None = None
    parser: str | None = None
    sheet: str | None = None
    page_from: int | None = None
    page_to: int | None = None
    date_from: str | None = None
    date_to: str | None = None
    kinds: tuple[str, ...] | None = None
    include_superseded: bool = False

    def applies_to(self, document: IndexDocument, chunk: IndexChunk | None = None) -> bool:
        """Python-side filtering, shared by both backends so they cannot disagree.

        Doing this in SQL in one backend and in Python in the other is how two index
        implementations quietly start answering different questions; the index is filtered after
        retrieval on purpose, and the retrieval step is only ever an accelerator.
        """
        if self.workspace_id and document.workspace_id != self.workspace_id:
            return False
        if self.project_id and document.project_id != self.project_id:
            return False
        if self.company_id and document.company_id != self.company_id:
            return False
        if self.well_id and document.well_id != self.well_id:
            return False
        if self.document_type and document.document_type != self.document_type:
            return False
        if self.document_types and document.document_type not in set(self.document_types):
            return False
        if self.revision and document.revision != self.revision:
            return False
        if self.status and document.status != self.status:
            return False
        if self.processing_status and document.processing_status != self.processing_status:
            return False
        if self.parser and document.parser != self.parser:
            return False
        if not self.include_superseded and not document.is_current:
            return False
        if self.date_from and document.document_date and document.document_date < self.date_from:
            return False
        if self.date_to and document.document_date and document.document_date > self.date_to:
            return False
        if self.kinds and (chunk is None or chunk.kind not in set(self.kinds)):
            return False
        if self.sheet and (chunk is None or chunk.sheet != self.sheet):
            return False
        if self.page_from is not None or self.page_to is not None:
            # A page filter needs a page: a chunk that does not know where it is cannot claim
            # to satisfy "pages 12-18", so it is excluded rather than guessed in.
            if chunk is None or chunk.page is None:
                return False
            if self.page_from is not None and chunk.page < self.page_from:
                return False
            if self.page_to is not None and chunk.page > self.page_to:
                return False
        return True

    def to_dict(self) -> dict[str, Any]:
        return {key: value for key, value in self.__dict__.items() if value not in (None, False, (), "")}


@dataclass
class SearchRequest:
    """One parsed query, ready to hand to a backend."""

    query: str
    filters: SearchFilters = field(default_factory=SearchFilters)
    limit: int = 20
    #: ``all`` requires every query term; ``any`` is the broadened fallback the service uses
    #: when ``all`` finds nothing (and reports that it did).
    mode: str = "all"

    @property
    def terms(self) -> list[str]:
        return parse_query(self.query)[0]

    @property
    def phrases(self) -> list[str]:
        return parse_query(self.query)[1]


@dataclass(frozen=True)
class Hit:
    """A backend's answer for one chunk: the row, the score, and why it scored."""

    chunk: IndexChunk
    document: IndexDocument
    score: float
    matched_terms: tuple[str, ...] = ()
    term_scores: dict[str, float] = field(default_factory=dict)


@dataclass
class IndexStats:
    """What the index holds, and whether it can be trusted to be current."""

    documents: int = 0
    versions: int = 0
    chunks: int = 0
    #: Indexed versions the registry no longer considers current (prunable).
    stale_versions: int = 0
    #: Indexed versions whose row has gone from the registry entirely.
    orphaned: int = 0
    #: Current registry versions with nothing indexed - the "the index was never built" case,
    #: and the reason a search can legitimately return nothing for a file that exists.
    missing_versions: int = 0
    fts_available: bool = False
    schema_version: int = SCHEMA_VERSION
    built_at: str = ""
    registry_revision: str = ""

    def to_dict(self) -> dict[str, Any]:
        return dict(self.__dict__)


class SearchIndex(Protocol):
    """The seam the ingestion pipeline, the CLI and the UI talk to.

    ``repository`` is accepted by the write and prune methods because the index reads its input
    from the registry, never from the file system.  A backend constructed with one (both of ours
    can be) may be called without it, which is what lets ingestion hand an index to a worker loop
    without threading a session through every call.
    """

    def upsert(self, document_id: str, version_id: str, *, repository: DocumentRepository | None = None) -> int: ...

    def store(self, chunk_set: ChunkSet) -> int: ...

    def remove_version(self, version_id: str) -> int: ...

    def remove_document(self, document_id: str) -> int: ...

    def prune_obsolete(self, *, repository: DocumentRepository | None = None) -> int:
        """Versions removed from the searchable state (not chunk rows - see ``remove_version``)."""


    def clear(self) -> int: ...

    def rebuild(self, *, repository: DocumentRepository | None = None) -> IndexStats: ...

    def search(self, request: SearchRequest) -> tuple[list[Hit], dict[str, Any]]: ...

    def stats(self, *, repository: DocumentRepository | None = None) -> IndexStats: ...

    def close(self) -> None: ...


# --------------------------------------------------------------------------- registry -> index
def index_document_for(document: Any, version: Any, extraction: Any, normalized: NormalizedDocument) -> IndexDocument:
    """Copy the registry's filterable facts into the index record for one version.

    Company, project and well are denormalised - names as well as ids - so a result can label
    itself without a join and so the index stays explainable while the registry is briefly
    unavailable.  ``document_date`` is the *authored* date when the extractor found one; the
    filesystem timestamps are never used here, because a copy operation must not move a
    document in time.
    """
    project = getattr(document, "project", None)
    company = getattr(project, "company", None)
    well = getattr(document, "well", None)
    return IndexDocument(
        document_id=document.id,
        version_id=version.id,
        version_number=int(version.version_number or 0),
        workspace_id=str(document.workspace_id or ""),
        project_id=str(document.project_id or ""),
        company_id=str(getattr(company, "id", "") or ""),
        project_name=str(getattr(project, "name", "") or ""),
        company_name=str(getattr(company, "name", "") or ""),
        well_id=str(document.well_id or ""),
        well_name=str(getattr(well, "name", "") or ""),
        document_type=str(document.classification or "OTHER"),
        title=str(document.title or document.filename or ""),
        filename=str(document.filename or ""),
        identity_path=str(document.identity_path or ""),
        source_relative_path=str(version.source_relative_path or ""),
        extension=str(document.extension or ""),
        parser=str(version.parser or ""),
        revision=str(version.revision or document.revision or ""),
        revision_key=int(version.revision_key or 0),
        status=str(document.status or ""),
        processing_status=str(document.processing_status or ""),
        source_authority=str(document.source_authority or ""),
        document_date=_iso(document.document_date or version.revision_date),
        imported_at=_iso(document.imported_at),
        page_count=int(version.page_count or len(normalized.pages) or 0),
        sheet_count=int(version.sheet_count or 0),
        word_count=int(version.word_count or normalized.word_count or 0),
        size_bytes=int(version.size_bytes or 0),
        sha256=str(version.sha256 or ""),
        is_current=bool(version.is_current),
        diagnostics=tuple(str(note) for note in (normalized.diagnostics or ())),
        chunk_count=0,
    )


def _iso(value: Any) -> str:
    """ISO-8601 text for anything date-like.

    Stored as text because lexicographic order on ISO-8601 *is* chronological order, which is
    what makes a date-range filter one string comparison in both backends.
    """
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    iso = getattr(value, "isoformat", None)
    return str(iso()) if callable(iso) else str(value)


def chunk_set_for(repository: DocumentRepository, document_id: str, version_id: str) -> ChunkSet | None:
    """Index records for one version, read only from registry rows.

    ``None`` - rather than an empty chunk set - means "there is nothing to index yet", so the
    pipeline can tell that apart from "an artefact with no citable structure" (a scan, say) and
    only the first is worth retrying.
    """
    document = repository.get(document_id)
    version = repository.version(version_id)
    if document is None or version is None or version.document_id != document.id:
        return None
    extraction = repository.extraction_for_version(version.id)
    if extraction is None or not extraction.document_json:
        return None
    normalized = NormalizedDocument.from_dict(dict(extraction.document_json))
    return build_chunk_set(
        document=index_document_for(document, version, extraction, normalized),
        normalized=normalized,
        version_id=version.id,
        source_sha256=version.sha256,
    )


def _current_pairs(repository: DocumentRepository) -> list[tuple[str, str]]:
    """``(document_id, version_id)`` for every current version: the rebuild work list.

    Superseded versions are left out, so an index rebuild reproduces "the answer now".  The
    history is still reachable - ``SearchFilters.include_superseded`` - but only for a
    workspace that chose to index those versions in the first place.
    """
    from ..database.models import DocumentVersion

    statement = (
        select(DocumentVersion.document_id, DocumentVersion.id)
        .where(DocumentVersion.is_current.is_(True))
        .order_by(DocumentVersion.document_id, DocumentVersion.version_number)
    )
    return [(str(document_id), str(version_id)) for document_id, version_id in repository.session.execute(statement).all()]


def _current_version_ids(repository: DocumentRepository) -> set[str]:
    from ..database.models import DocumentVersion

    return {
        str(row[0])
        for row in repository.session.execute(select(DocumentVersion.id).where(DocumentVersion.is_current.is_(True))).all()
    }


def _registry_revision(repository: DocumentRepository) -> str:
    """The Alembic revision the registry is on when the index was built.

    Recorded so ``doctor`` can say "this index was built against schema 0002" instead of
    guessing whether the sidecar predates a migration.
    """
    from ..database.migrations import current_revision

    bind = repository.session.bind
    if bind is None:  # pragma: no cover - a repository always has an engine in practice
        return ""
    try:
        return current_revision(bind)
    except Exception:  # noqa: BLE001 - informational only
        return ""


def _now_iso() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def _score(
    pairs: Sequence[tuple[IndexChunk, IndexDocument]],
    request: SearchRequest,
    *,
    statistics: IndexStatistics,
) -> tuple[list[Hit], bool, bool]:
    """Score, filter and cut down to the limit: ``(hits, truncated, matched_any)``.

    Retrieval is a superset and this is the decision: BM25 over the query's terms, the phrase
    requirements of a quoted query, the kind weighting, and the filter set - all from
    :mod:`drilling_intelligence.search.ranking`, on the same ``rows`` in both backends.
    """
    triples = [(chunk, chunk.terms, max(1, chunk.length), chunk.kind, chunk.text) for chunk, _ in pairs]
    matches = rank_chunks(
        triples,
        terms=request.terms,
        phrases=request.phrases if request.mode != "any" else (),
        statistics=statistics,
        require_all=request.mode != "any",
        limit=0,
    )
    by_id = {chunk.chunk_id: (chunk, document) for chunk, document in pairs}
    hits: list[Hit] = []
    for item in matches:
        pair = by_id.get(item.row.chunk_id)
        if pair is None:  # pragma: no cover - candidates came from this same map
            continue
        # Filters are applied *after* ranking on purpose, and the distinction matters for the
        # fallback below: "no chunk contains these words" and "the chunks that do belong to a
        # well you did not ask about" are different answers, and only the first one justifies
        # broadening the query.
        if not request.filters.applies_to(pair[1], item.row):
            continue
        hits.append(
            Hit(
                chunk=pair[0],
                document=pair[1],
                score=round(item.score, 6),
                matched_terms=item.matched_terms,
                term_scores=dict(item.term_scores),
            )
        )
    truncated = len(hits) > MAX_CANDIDATES
    if truncated:
        hits = hits[:MAX_CANDIDATES]
    limit = request.limit if request.limit > 0 else len(hits)
    return hits[:limit], truncated, bool(matches)


def score_candidates(
    request: SearchRequest,
    *,
    statistics: IndexStatistics,
    candidates_for: Callable[[str], Sequence[tuple[IndexChunk, IndexDocument]]],
    total_chunks: int,
    fts_used: bool,
    retrieval_truncated: Callable[[str], bool] | None = None,
) -> tuple[list[Hit], dict[str, Any]]:
    """Run one query, with the broadened reading as an explicit, reported fallback.

    The single place the "nothing satisfied every term, so try any term" decision is made, so
    the two backends cannot answer the same query differently.  ``candidates_for(mode)`` is
    asked for the pairs to score under each reading - a backend that can accelerate the strict
    reading still has to widen its retrieval for the fallback, which is why this is a callback
    rather than a pre-fetched list.
    """
    mode = request.mode if request.mode in ("all", "any") else "all"
    pairs = list(candidates_for(mode))
    hits, scoring_truncated, matched_any = _score(pairs, replace(request, mode=mode), statistics=statistics)
    truncated = scoring_truncated or bool(retrieval_truncated and retrieval_truncated(mode))
    candidates = len(pairs)
    if not hits and matched_any is False and mode == "all" and len(request.terms) > 1:
        # Reported, never silent: "nothing matched the whole query" and "here is what matched
        # any word of it" are different answers about a well, and a caller must be able to tell
        # which one it was given.
        mode = "any"
        pairs = list(candidates_for(mode))
        candidates = len(pairs)
        hits, scoring_truncated, _ = _score(pairs, replace(request, mode=mode), statistics=statistics)
        truncated = truncated or scoring_truncated
    return hits, {
        "mode": mode,
        "truncated": truncated,
        "candidates": candidates,
        "total_chunks": total_chunks,
        "fts_used": fts_used,
    }


# --------------------------------------------------------------------------- in-memory backend
class InMemorySearchIndex:
    """The same index without a database: dictionaries and the shared ranking code."""

    def __init__(self, *, repository: DocumentRepository | None = None) -> None:
        self._documents: dict[str, IndexDocument] = {}
        self._chunks: dict[str, IndexChunk] = {}
        self._by_version: dict[str, list[str]] = {}
        self._repository = repository
        self.built_at = ""
        self.registry_revision = ""

    # -- writes -------------------------------------------------------------
    def upsert(self, document_id: str, version_id: str, *, repository: DocumentRepository | None = None) -> int:
        chunk_set = chunk_set_for(_need_repository(repository, self._repository), document_id, version_id)
        if chunk_set is None:
            return 0
        self.remove_version(version_id)
        return self.store(chunk_set)

    def store(self, chunk_set: ChunkSet) -> int:
        self._documents[chunk_set.document.version_id] = chunk_set.document
        ids = [chunk.chunk_id for chunk in chunk_set.chunks]
        for chunk in chunk_set.chunks:
            self._chunks[chunk.chunk_id] = chunk
        self._by_version[chunk_set.document.version_id] = ids
        return len(ids)

    def remove_version(self, version_id: str) -> int:
        removed = 0
        for chunk_id in self._by_version.pop(version_id, []):
            if self._chunks.pop(chunk_id, None) is not None:
                removed += 1
        self._documents.pop(version_id, None)
        return removed

    def remove_document(self, document_id: str) -> int:
        removed = 0
        for version_id, document in list(self._documents.items()):
            if document.document_id == document_id:
                removed += self.remove_version(version_id)
        return removed

    def prune_obsolete(self, *, repository: DocumentRepository | None = None) -> int:
        obsolete = [version_id for version_id in list(self._documents) if version_id not in _current_version_ids(_need_repository(repository, self._repository))]
        for version_id in obsolete:
            self.remove_version(version_id)
        return len(obsolete)

    def clear(self) -> int:
        count = len(self._chunks)
        self._chunks.clear()
        self._documents.clear()
        self._by_version.clear()
        return count

    def rebuild(self, *, repository: DocumentRepository | None = None) -> IndexStats:
        repository = _need_repository(repository, self._repository)
        self.clear()
        for document_id, version_id in _current_pairs(repository):
            self.upsert(document_id, version_id, repository=repository)
        stats = self._tally(repository=repository)
        self.built_at = stats.built_at = _now_iso()
        self.registry_revision = stats.registry_revision = _registry_revision(repository)
        return stats

    # -- reads --------------------------------------------------------------
    def pairs(self) -> list[tuple[IndexChunk, IndexDocument]]:
        return [
            (chunk, self._documents[chunk.version_id])
            for chunk in sorted(self._chunks.values(), key=lambda item: item.chunk_id)
            if chunk.version_id in self._documents
        ]

    def search(self, request: SearchRequest) -> tuple[list[Hit], dict[str, Any]]:
        pairs = self.pairs()
        if not pairs:
            return [], {"mode": request.mode, "truncated": False, "candidates": 0, "total_chunks": 0, "fts_used": False}
        return score_candidates(
            request,
            statistics=self._statistics(request.terms),
            candidates_for=lambda mode: pairs,
            total_chunks=len(self._chunks),
            fts_used=False,
        )

    def _statistics(self, terms: Sequence[str]) -> IndexStatistics:
        total_length = sum(chunk.length for chunk in self._chunks.values())
        frequencies = {term: sum(1 for chunk in self._chunks.values() if term in chunk.terms) for term in terms}
        return IndexStatistics(total_chunks=len(self._chunks), total_length=total_length, document_frequency=frequencies)

    def stats(self, *, repository: DocumentRepository | None = None) -> IndexStats:
        return self._tally(repository=repository or self._repository)

    def _tally(self, *, repository: DocumentRepository | None) -> IndexStats:
        stats = IndexStats(
            documents=len({document.document_id for document in self._documents.values()}),
            versions=len(self._documents),
            chunks=len(self._chunks),
            fts_available=False,
        )
        if repository is not None:
            current = _current_version_ids(repository)
            for version_id in list(self._documents):
                if version_id not in current:
                    stats.stale_versions += 1
            stats.missing_versions = len(current - set(self._documents))
        return stats

    def close(self) -> None:
        return None


# --------------------------------------------------------------------------- SQLite backend
class SqliteSearchIndex:
    """The product index: a sidecar SQLite database, FTS5-accelerated where available.

    FTS5 is optional in the way every heavy dependency here is: on a minimal ``libsqlite3``
    without it, ``fts_available()`` reports False and candidate retrieval scans the chunk
    table.  Results are identical either way, because FTS5 never decides the ranking.

    Every statement here is built with SQLAlchemy Core against :data:`search_metadata`, not
    with concatenated SQL text, for two reasons: the sidecar must stay readable by a future
    PostgreSQL index, and a statement assembled from our own column names is a statement that
    cannot be turned by a chunk id containing a quote.  The exception is FTS5 itself, which
    Core cannot express (virtual tables, ``MATCH``); those are marked and use bound parameters
    for every value.
    """

    def __init__(self, database: Any, *, repository: DocumentRepository | None = None) -> None:
        self.database = database
        self._repository = repository
        self._fts_ready: bool | None = None
        self.ensure_schema()

    # -- schema -------------------------------------------------------------
    @property
    def engine(self) -> Any:
        return self.database.engine

    def fts_available(self) -> bool:
        """Can this SQLite build create an FTS5 table?  Probed once, then cached."""
        if self._fts_ready is None:
            self._fts_ready = self._probe_fts()
        return bool(self._fts_ready)

    def _probe_fts(self) -> bool:
        try:
            with self.engine.connect() as connection:
                options = {str(row).strip().upper() for row in connection.execute(sa_text("pragma compile_options")).scalars()}
            if "ENABLE_FTS5" in options:
                return True
            with self.engine.begin() as connection:
                connection.execute(sa_text("create temp table __fts_probe (a) using fts5(a)"))
                connection.execute(sa_text("drop table __fts_probe"))
            return True
        except Exception:  # noqa: BLE001 - no FTS5 simply means the scan path
            return False

    def ensure_schema(self) -> None:
        search_metadata.create_all(self.engine, checkfirst=True)
        if self.fts_available():
            statement = (
                f"create virtual table if not exists {FTS_TABLE} using fts5("
                "body, chunk_id UNINDEXED, tokenize = 'unicode61 remove_diacritics 2')"
            )
            with self.engine.begin() as connection:
                connection.execute(sa_text(statement))
        self._set_meta("schema_version", str(SCHEMA_VERSION))

    def schema_is_current(self) -> bool:
        return self._meta("schema_version", "0") == str(SCHEMA_VERSION)

    def missing_tables(self) -> list[str]:
        existing = set(sa_inspect(self.engine).get_table_names())
        return sorted(set(search_metadata.tables) - existing)

    def _meta(self, key: str, default: str = "") -> str:
        with self.engine.connect() as connection:
            row = connection.execute(select(search_meta_table.c.value).where(search_meta_table.c.key == key)).first()
        return str(row[0]) if row else default

    def _set_meta(self, key: str, value: str) -> None:
        with self.engine.begin() as connection:
            found = connection.execute(select(search_meta_table.c.key).where(search_meta_table.c.key == key)).first()
            if found is None:
                connection.execute(insert(search_meta_table).values(key=key, value=value))
            else:
                connection.execute(update(search_meta_table).where(search_meta_table.c.key == key).values(value=value))

    # -- writes -------------------------------------------------------------
    def upsert(self, document_id: str, version_id: str, *, repository: DocumentRepository | None = None) -> int:
        chunk_set = chunk_set_for(_need_repository(repository, self._repository), document_id, version_id)
        if chunk_set is None:
            return 0
        self.remove_version(version_id)
        return self.store(chunk_set)

    def store(self, chunk_set: ChunkSet) -> int:
        """Write one version's rows in a single transaction.

        A crash between the document row and its chunks would leave a searchable document with
        no text, or text with no metadata.  The index is allowed to be *behind* the registry -
        never half-built.
        """
        version_id = chunk_set.document.version_id
        with self.engine.begin() as connection:
            self._delete(connection, version_id)
            row = chunk_set.document.to_row()
            row["chunk_count"] = len(chunk_set.chunks)
            connection.execute(insert(search_document_table).values(**row))
            for chunk in chunk_set.chunks:
                connection.execute(insert(search_chunk_table).values(**chunk.to_row()))
                if self.fts_available():
                    connection.execute(
                        sa_text(
                            f"insert into {FTS_TABLE} (rowid, body, chunk_id)"  # noqa: S608 - FTS5 is not in Core; both values are bound
                            " values ((select rowid from search_chunk where chunk_id = :id), :body, :id)"
                        ),
                        {"id": chunk.chunk_id, "body": " ".join(chunk.terms)},
                    )
        return len(chunk_set.chunks)

    def _delete(self, connection: Any, version_id: str) -> int:
        if self.fts_available():
            connection.execute(
                sa_text(
                    f"delete from {FTS_TABLE} where chunk_id in (select chunk_id from search_chunk where version_id = :version_id)"  # noqa: S608 - FTS5 is not in Core; the id is bound
                ),
                {"version_id": version_id},
            )
        removed = connection.execute(delete(search_chunk_table).where(search_chunk_table.c.version_id == version_id)).rowcount
        connection.execute(delete(search_document_table).where(search_document_table.c.version_id == version_id))
        return int(removed or 0)

    def remove_version(self, version_id: str) -> int:
        """Chunk rows dropped for one version."""
        with self.engine.begin() as connection:
            return self._delete(connection, version_id)

    def remove_document(self, document_id: str) -> int:
        with self.engine.begin() as connection:
            versions = [str(row[0]) for row in connection.execute(select(search_document_table.c.version_id).where(search_document_table.c.document_id == document_id)).all()]
            return sum(self._delete(connection, version_id) for version_id in versions)

    def prune_obsolete(self, *, repository: DocumentRepository | None = None) -> int:
        """Drop indexed versions the registry no longer considers current (or no longer has).

        This is what keeps "searchable" equal to "up to date" without the index pretending to
        be the record: the registry says which version is current, everything else leaves the
        searchable state - and stays in the registry, cited and reachable by id.
        """
        current = _current_version_ids(_need_repository(repository, self._repository))
        with self.engine.connect() as connection:
            stored = [str(row[0]) for row in connection.execute(select(search_document_table.c.version_id)).all()]
        obsolete = [version_id for version_id in stored if version_id not in current]
        for version_id in obsolete:
            self.remove_version(version_id)
        return len(obsolete)

    def clear(self) -> int:
        with self.engine.begin() as connection:
            count = int(connection.execute(select(func.count()).select_from(search_chunk_table)).scalar_one() or 0)
            if self.fts_available():
                connection.execute(sa_text(f"delete from {FTS_TABLE}"))  # noqa: S608 - module constant
            connection.execute(delete(search_chunk_table))
            connection.execute(delete(search_document_table))
        return count

    def rebuild(self, *, repository: DocumentRepository | None = None) -> IndexStats:
        """Recompute the whole index from the registry.  Safe to run at any time, from anywhere."""
        repository = _need_repository(repository, self._repository)
        self.clear()
        for document_id, version_id in _current_pairs(repository):
            self.upsert(document_id, version_id, repository=repository)
        stats = self.stats(repository=repository)
        stats.built_at = _now_iso()
        stats.registry_revision = _registry_revision(repository)
        self._set_meta("built_at", stats.built_at)
        self._set_meta("registry_revision", stats.registry_revision)
        log.event(
            "search.rebuilt",
            documents=stats.documents,
            versions=stats.versions,
            chunks=stats.chunks,
            fts=stats.fts_available,
            schema=stats.schema_version,
        )
        return stats

    # -- reads --------------------------------------------------------------
    def search(self, request: SearchRequest) -> tuple[list[Hit], dict[str, Any]]:
        if not request.terms and not request.phrases:
            return [], {
                "mode": request.mode,
                "truncated": False,
                "candidates": 0,
                "total_chunks": self._counts()["chunks"],
                "fts_used": False,
                "empty_query": True,
            }
        terms = request.terms
        total = self._counts()["chunks"]
        statistics = self._statistics(terms)
        cache: dict[str, tuple[list[tuple[IndexChunk, IndexDocument]], bool]] = {}

        def candidates_for(mode: str) -> list[tuple[IndexChunk, IndexDocument]]:
            if mode not in cache:
                chunk_ids, retrieval_truncated = self._candidate_ids(terms, mode)
                if chunk_ids is None:
                    # The scan path takes the first RETRIEVAL_CAP chunks in id order, so it is
                    # truncated exactly when the corpus is bigger than that - and says so.
                    retrieval_truncated = total > RETRIEVAL_CAP
                cache[mode] = (self._rows(chunk_ids), retrieval_truncated)
            return cache[mode][0]

        def retrieval_truncated(mode: str) -> bool:
            candidates_for(mode)
            return cache[mode][1]

        return score_candidates(
            request,
            statistics=statistics,
            candidates_for=candidates_for,
            total_chunks=total,
            fts_used=bool(self.fts_available() and terms),
            retrieval_truncated=retrieval_truncated,
        )

    def _candidate_ids(self, terms: Sequence[str], mode: str) -> tuple[list[str] | None, bool]:
        """Ids to score, or ``None`` for "scan the table" (no FTS5, or no plain terms).

        Two properties make this safe to use as an accelerator rather than as the answer:

        *   the FTS body is **the chunk's own indexed vocabulary** (written by :meth:`store`),
            not its prose.  FTS5's ``unicode61`` tokenizer and this module's tokenizer then see
            the same strings, so a chunk the Python ranker would accept is always in the
            candidate set - FTS5 may only ever be wider, never narrower;
        *   a query expression is built by quoting each term as a phrase, so characters FTS5
            would otherwise read as operators (`10.2`, `500/300`, `12 1/4`) are tokenised on both
            sides by the same rules.  Only the double quote itself needs doubling.

        When the expression is rejected for any reason the scan is used instead - a query that
        runs slower on a machine without the extension must never be a query that returns a
        different list.
        """
        if not self.fts_available() or not terms:
            return None, False
        unique = list(dict.fromkeys(terms))
        joiner = " OR " if mode == "any" else " "
        match = joiner.join(f'"{term.replace(chr(34), chr(34) * 2)}"' for term in unique)
        statement = sa_text(
            f"select chunk_id from {FTS_TABLE} where {FTS_TABLE} match :match order by chunk_id limit :cap"  # noqa: S608 - FTS5 MATCH is not in Core; :match is bound
        )
        try:
            with self.engine.connect() as connection:
                rows = connection.execute(statement, {"match": match, "cap": RETRIEVAL_CAP + 1}).all()
        except Exception:  # noqa: BLE001 - a malformed expression means "use the scan"
            log.warning("search.fts_query_failed", match=match[:200], level=25)
            return None, False
        truncated = len(rows) > RETRIEVAL_CAP
        return [str(row[0]) for row in rows[:RETRIEVAL_CAP]], truncated

    def _rows(self, chunk_ids: Sequence[str] | None) -> list[tuple[IndexChunk, IndexDocument]]:
        """Chunk rows (in ``chunk_id`` order) paired with their document rows.

        Two queries rather than a join: the tables share column names, and pairing them in
        Python keeps both rows unambiguous - worth one extra round trip to avoid a class of
        "which table's document_id won?" bugs in the row mapping.
        """
        statement = select(search_chunk_table)
        if chunk_ids is not None:
            ordered = sorted(set(chunk_ids))
            rows: list[dict[str, Any]] = []
            with self.engine.connect() as connection:
                for batch in _batches(ordered):
                    rows.extend(
                        dict(row)
                        for row in connection.execute(statement.where(search_chunk_table.c.chunk_id.in_(batch)).order_by(search_chunk_table.c.chunk_id)).mappings()
                    )
        else:
            with self.engine.connect() as connection:
                rows = [
                    dict(row)
                    for row in connection.execute(statement.order_by(search_chunk_table.c.chunk_id).limit(RETRIEVAL_CAP)).mappings()
                ]
        if not rows:
            return []
        versions = sorted({str(row["version_id"]) for row in rows})
        documents: dict[str, IndexDocument] = {}
        with self.engine.connect() as connection:
            for batch in _batches(versions):
                for row in connection.execute(select(search_document_table).where(search_document_table.c.version_id.in_(batch))).mappings():
                    documents[str(row["version_id"])] = IndexDocument.from_row(dict(row))
        return [
            (IndexChunk.from_row(row), documents[str(row["version_id"])])
            for row in rows
            if str(row["version_id"]) in documents
        ]

    def _statistics(self, terms: Sequence[str]) -> IndexStatistics:
        with self.engine.connect() as connection:
            total_chunks, total_length = connection.execute(
                select(func.count(), func.coalesce(func.sum(search_chunk_table.c.length), 0)).select_from(search_chunk_table)
            ).one()
            frequencies: dict[str, int] = {}
            for term in terms:
                needle = f'"{term}":'
                count = connection.execute(
                    select(func.count()).select_from(search_chunk_table).where(sa_text("instr(terms_json, :needle) > 0").bindparams(needle=needle))
                ).scalar_one()
                frequencies[term] = int(count or 0)
        return IndexStatistics(
            total_chunks=int(total_chunks or 0),
            total_length=int(total_length or 0),
            document_frequency=frequencies,
        )

    def _counts(self) -> dict[str, int]:
        with self.engine.connect() as connection:
            documents = connection.execute(
                select(func.count(func.distinct(search_document_table.c.document_id)))
            ).scalar_one()
            versions = connection.execute(select(func.count()).select_from(search_document_table)).scalar_one()
            chunks = connection.execute(select(func.count()).select_from(search_chunk_table)).scalar_one()
        return {"documents": int(documents or 0), "versions": int(versions or 0), "chunks": int(chunks or 0)}

    def stats(self, *, repository: DocumentRepository | None = None) -> IndexStats:
        counts = self._counts()
        stats = IndexStats(
            documents=counts["documents"],
            versions=counts["versions"],
            chunks=counts["chunks"],
            fts_available=self.fts_available(),
            schema_version=_int(self._meta("schema_version", str(SCHEMA_VERSION))),
            built_at=self._meta("built_at"),
            registry_revision=self._meta("registry_revision"),
        )
        if repository is not None:
            # The registry says what is current; the sidecar says what is searchable.  Asking
            # both is how "the index is behind" becomes a number instead of a surprise.
            current = _current_version_ids(repository)
            known = {str(row[0]) for row in repository.session.execute(sa_text("select id from document_version")).all()}
            with self.engine.connect() as connection:
                stored = {str(row[0]) for row in connection.execute(select(search_document_table.c.version_id)).all()}
            for version_id in stored:
                if version_id not in known:
                    stats.orphaned += 1
                elif version_id not in current:
                    stats.stale_versions += 1
            stats.missing_versions = len(current - stored)
        return stats

    def close(self) -> None:
        # The engine belongs to the workspace's Database, which owns disposal.
        return None


def _need_repository(*candidates: DocumentRepository | None) -> DocumentRepository:
    """The first repository that is not ``None``, or an error that says what to do.

    Keeps ``upsert(document_id, version_id)`` valid for an index constructed with a repository -
    which is what the ingestion loop calls - while a completely unbound index fails with an
    instruction instead of an ``AttributeError`` on ``None``.
    """
    for candidate in candidates:
        if candidate is not None:
            return candidate
    raise ValueError("this index needs a repository: construct it with one or pass repository=...")


def _batches(values: Sequence[str], *, size: int = 500) -> Iterable[tuple[str, ...]]:
    """Split an ``IN`` list so no dialect's host-parameter limit is reached.

    Old SQLite builds refuse more than 999 parameters; batching costs nothing and keeps a
    candidate set of any size working on every platform the sidecar runs on.
    """
    for start in range(0, len(values), size):
        yield tuple(values[start : start + size])


def _int(value: Any) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):  # pragma: no cover - a corrupt meta row is not fatal
        return 0
