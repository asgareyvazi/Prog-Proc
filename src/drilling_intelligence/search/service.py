"""The search API: a query in, cited results out.

:meth:`SearchService.search` is the one entry point the CLI, the UI and (later) the retrieval
layer use::

    results = service.search("mud weight 10.2 ppg", well_id=..., document_type="MUD_REPORT")

and every result carries five things: what matched (``snippet``), where it came from
(``provenance`` and ``locator_ref``), which revision of the document it is from (``version_id``
and ``version_number``), why it ranked where it did (``score``, ``matched_terms``,
``term_scores``), and the registry facts needed to label it (``document``).  A hit whose chunk
has no location is only ever returned as a *document-level* statement - a diagnostic about the
extraction - and is labelled as such, because "the extractor could not read this file" is a
fact about the file, not a quotation from page 3.

The index is treated as disposable everywhere in this class:

*   :meth:`rebuild` recomputes it from the registry, and is the recovery action for anything
    odd (the CLI calls it when the recorded schema version or registry revision disagrees);
*   :meth:`prune` drops superseded versions so search answers "now";
*   :meth:`search` with ``verify=True`` re-reads the source file behind each hit through the
    same :func:`~drilling_intelligence.core.provenance.verify_provenance` the UI uses, so a
    result can be presented as *checked* rather than merely *indexed*.
"""

from __future__ import annotations

import time
from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ..core.logging import get_logger
from ..core.provenance import Provenance, verify_provenance
from ..documents.repository import DocumentRepository
from .chunking import uncited_chunks
from .index import (
    SCHEMA_VERSION,
    Hit,
    InMemorySearchIndex,
    SearchFilters,
    SearchIndex,
    SearchRequest,
    SqliteSearchIndex,
    chunk_set_for,
)
from .tokenize import highlight

log = get_logger("search.service")

__all__ = ["SearchResponse", "SearchResult", "SearchService", "build_index"]


@dataclass(frozen=True)
class SearchResult:
    """One ranked hit, with its citation and the numbers behind its rank."""

    document_id: str
    version_id: str
    version_number: int
    chunk_id: str
    kind: str
    score: float
    snippet: str
    highlights: tuple[tuple[int, int], ...]
    text: str
    page: int | None
    sheet: str
    locator_ref: str
    provenance: dict[str, Any]
    citation: str
    #: True when a location (page, cell, lines) was recorded for this chunk at all.
    cited: bool
    #: True when the chunk text *is* what the extractor read at that location.  The two flags
    #: differ for a view - a table row rendered with its caption, a field rendered as
    #: ``name = value unit`` - and the difference decides what ``verify=True`` can honestly claim.
    verbatim: bool
    #: The registry facts a result is labelled with (filename, type, revision, status, well,
    #: project, company, dates, page/sheet/word counts, sha256, and the extraction's own
    #: diagnostics).  Called ``metadata`` because that is what it is: the search layer copies
    #: these out of the index row for convenience, and the registry stays the authority -
    #: ``verify=True`` is the call that re-reads it.
    metadata: dict[str, Any]
    matched_terms: tuple[str, ...] = ()
    term_scores: dict[str, float] = field(default_factory=dict)
    verification: dict[str, Any] | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "document_id": self.document_id,
            "version_id": self.version_id,
            "version_number": self.version_number,
            "chunk_id": self.chunk_id,
            "kind": self.kind,
            "score": self.score,
            "snippet": self.snippet,
            "highlights": [list(span) for span in self.highlights],
            "page": self.page,
            "sheet": self.sheet,
            "locator_ref": self.locator_ref,
            "provenance": dict(self.provenance),
            "citation": self.citation,
            "cited": self.cited,
            "verbatim": self.verbatim,
            "metadata": dict(self.metadata),
            "matched_terms": list(self.matched_terms),
            "term_scores": dict(self.term_scores),
            "verification": dict(self.verification) if self.verification else None,
        }


@dataclass(frozen=True)
class SearchResponse:
    """Results plus the honesty about how they were obtained."""

    query: str
    results: tuple[SearchResult, ...] = ()
    mode: str = "all"
    #: True when the candidate cap was reached: the ranking is over a bounded set, not the corpus.
    truncated: bool = False
    candidates: int = 0
    total_chunks: int = 0
    fts_used: bool = False
    took_ms: float = 0.0
    #: Filters that were applied, for display ("searched 4 mud reports for A-3").
    filters: dict[str, Any] = field(default_factory=dict)

    @property
    def broadened(self) -> bool:
        """Did the exact query find nothing, and we fell back to any-of-the-terms?"""
        return self.mode == "any"

    @property
    def ok(self) -> bool:
        return True

    def __len__(self) -> int:  # pragma: no cover - convenience for callers
        return len(self.results)

    def to_dict(self) -> dict[str, Any]:
        return {
            "query": self.query,
            "results": [item.to_dict() for item in self.results],
            "count": len(self.results),
            "mode": self.mode,
            "broadened": self.broadened,
            "truncated": self.truncated,
            "candidates": self.candidates,
            "total_chunks": self.total_chunks,
            "fts_used": self.fts_used,
            "took_ms": round(self.took_ms, 2),
            "filters": dict(self.filters),
        }


class SearchService:
    """Domain-facing search: it owns the index, the registry reads, and the citation rules."""

    def __init__(
        self,
        *,
        index: SearchIndex,
        repository: DocumentRepository | None = None,
        database: Any = None,
        default_limit: int = 20,
        snippet_context: int = 90,
    ) -> None:
        """Bind an index to the registry - by session (``repository``) or by ``database``.

        Prefer ``database`` for anything long-lived.  A repository is a session, and a session
        that only ever reads keeps the snapshot it first took: in a workspace where the UI ingests
        in a second session, a service holding one open session would answer "no results" for a
        file that was added a minute ago.  Reading through ``database`` opens a session per
        operation, sees every commit, and leaks nothing.
        """
        if repository is None and database is None:
            raise ValueError(
                "a search service needs a repository or a database to read the registry from"
            )
        self.index = index
        self._repository = repository
        self._database = database
        self.default_limit = max(1, int(default_limit))
        self.snippet_context = max(20, int(snippet_context))

    @contextmanager
    def _registry(self) -> Iterator[DocumentRepository]:
        """The repository to read this operation's rows through."""
        if self._repository is not None:
            yield self._repository
            return
        with self._database.session() as session:
            yield DocumentRepository(session)

    # -- construction -------------------------------------------------------
    @classmethod
    def for_workspace(cls, workspace: Any, *, in_memory: bool = False) -> SearchService:
        """Bind a service to an open workspace (the usual entry point).

        The sidecar database is created on first use and never migrated: it is derived data,
        so an old or damaged file is a thing to rebuild, not a thing to upgrade.
        """
        index: SearchIndex = (
            InMemorySearchIndex() if in_memory else SqliteSearchIndex(workspace.index_database)
        )
        settings = getattr(workspace, "settings", None)
        limit = int(getattr(getattr(settings, "search", None), "keyword_results", 40) or 40)
        return cls(
            index=index, database=workspace.database, default_limit=min(200, max(1, limit // 2))
        )

    # -- writes -------------------------------------------------------------
    def index_version(self, document_id: str, version_id: str) -> int:
        """Index (or re-index) one version from the registry, and report what was written."""
        with self._registry() as repository:
            chunk_set = chunk_set_for(repository, document_id, version_id)
        if chunk_set is None:
            log.warning(
                "search.index.no_artefact", document_id=document_id, version_id=version_id, level=25
            )
            return 0
        uncited = uncited_chunks(chunk_set.chunks)
        if uncited:
            # Surfaced, not repaired: a body chunk with no locator means provenance was lost
            # upstream, and quietly indexing it would let an untraceable number into a result.
            log.warning(
                "search.index.uncited_chunks",
                document_id=document_id,
                version_id=version_id,
                count=len(uncited),
                chunk_ids=[chunk.chunk_id for chunk in uncited][:10],
                level=25,
            )
        return int(self.index.store(chunk_set))

    def remove_document(self, document_id: str) -> int:
        return int(self.index.remove_document(document_id))

    def prune(self) -> int:
        """Drop searchable state for versions the registry no longer considers current."""
        with self._registry() as repository:
            return int(self.index.prune_obsolete(repository=repository))

    def rebuild(self) -> dict[str, Any]:
        """Recompute the whole index from the registry (the recovery action for anything odd)."""
        with self._registry() as repository:
            return self.index.rebuild(repository=repository).to_dict()

    def needs_rebuild(self) -> bool:
        """True when the searchable state disagrees with the registry.

        Four ways, all of them real and all of them observable from the two structures:

        *   the registry has current versions with nothing indexed (an index that was never
            built, or one whose file was deleted);
        *   the index holds versions the registry no longer considers current (a prune fixes it);
        *   the index holds versions the registry has lost entirely;
        *   the sidecar was written against a different Alembic revision of the registry.

        An index maintained incrementally by ingestion is *not* "in need of a rebuild" just
        because nobody has run one: ``built_at`` is a stamp, not a health flag.
        """
        stats = self.stats()
        if stats.get("missing_versions") or stats.get("stale_versions") or stats.get("orphaned"):
            return True
        from ..database.migrations import current_revision

        with self._registry() as repository:
            bind = repository.session.bind
        if bind is not None and stats.get("registry_revision"):
            return current_revision(bind) != stats["registry_revision"]
        return False

    def stats(self) -> dict[str, Any]:
        with self._registry() as repository:
            return self.index.stats(repository=repository).to_dict()

    # -- reads --------------------------------------------------------------
    def search(
        self,
        query: str,
        *,
        workspace_id: str | None = None,
        project_id: str | None = None,
        company_id: str | None = None,
        well_id: str | None = None,
        document_type: str | None = None,
        document_types: Sequence[str] | None = None,
        revision: str | None = None,
        status: str | None = None,
        processing_status: str | None = None,
        parser: str | None = None,
        sheet: str | None = None,
        page_from: int | None = None,
        page_to: int | None = None,
        date_from: str | None = None,
        date_to: str | None = None,
        kinds: Sequence[str] | None = None,
        include_superseded: bool = False,
        limit: int | None = None,
        verify: bool = False,
    ) -> SearchResponse:
        started = time.perf_counter()
        filters = SearchFilters(
            workspace_id=workspace_id,
            project_id=project_id,
            company_id=company_id,
            well_id=well_id,
            document_type=document_type,
            document_types=tuple(document_types) if document_types else None,
            revision=revision,
            status=status,
            processing_status=processing_status,
            parser=parser,
            sheet=sheet,
            page_from=page_from,
            page_to=page_to,
            date_from=_date(date_from),
            date_to=_date(date_to),
            kinds=tuple(kinds) if kinds else None,
            include_superseded=include_superseded,
        )
        request = SearchRequest(
            query=str(query or ""), filters=filters, limit=int(limit or self.default_limit)
        )
        hits, meta = self.index.search(request)
        # Verification reads files, and the registry row that names the file has to come from a
        # session - so the whole presentation step happens inside one registry read.
        with self._registry() as repository:
            results = [self._result(hit, meta, repository, verify=verify) for hit in hits]
        response = SearchResponse(
            query=request.query,
            results=tuple(results),
            mode=str(meta.get("mode") or request.mode),
            truncated=bool(meta.get("truncated")),
            candidates=int(meta.get("candidates") or 0),
            total_chunks=int(meta.get("total_chunks") or 0),
            fts_used=bool(meta.get("fts_used")),
            took_ms=(time.perf_counter() - started) * 1000.0,
            filters=filters.to_dict(),
        )
        log.event(
            "search.query",
            level=15,
            query=request.query[:120],
            results=len(response.results),
            mode=response.mode,
            candidates=response.candidates,
            fts=response.fts_used,
            ms=round(response.took_ms, 1),
        )
        return response

    # -- presentation of one hit -------------------------------------------
    def _result(
        self, hit: Hit, meta: Mapping[str, Any], repository: DocumentRepository, *, verify: bool
    ) -> SearchResult:
        chunk, document = hit.chunk, hit.document
        # Highlight what the query actually matched; on the broadened fallback that can be a
        # subset of the terms, which is exactly why the mode is reported alongside.
        snippet, spans = highlight(
            chunk.text, list(hit.matched_terms), context=self.snippet_context
        )
        provenance = dict(chunk.provenance or {})
        cited = bool(chunk.provenance)
        verbatim = _is_verbatim(chunk.text, str(provenance.get("excerpt") or ""))
        if not cited:
            # A chunk without a recorded location is reported against the document itself, and
            # says so, rather than being dressed up as a quotation.
            provenance = {
                "document_id": chunk.document_id,
                "document_version_id": chunk.version_id,
                "filename": document.filename,
                "locator": {"kind": "unknown", "locator_kind": "unknown", "note": chunk.kind},
                "excerpt": chunk.text[:200],
                "source_sha256": chunk.source_sha256 or document.sha256,
            }
        result = SearchResult(
            document_id=chunk.document_id,
            version_id=chunk.version_id,
            version_number=document.version_number,
            chunk_id=chunk.chunk_id,
            kind=chunk.kind,
            score=hit.score,
            snippet=snippet,
            highlights=tuple(spans),
            text=chunk.text,
            page=chunk.page,
            sheet=chunk.sheet,
            locator_ref=chunk.locator_ref or document.reference(),
            provenance=provenance,
            citation=_citation(chunk, document, cited=cited),
            cited=cited,
            verbatim=verbatim,
            metadata={
                "filename": document.filename,
                "identity_path": document.identity_path,
                "source_relative_path": document.source_relative_path,
                "title": document.title,
                "document_type": document.document_type,
                "revision": document.revision,
                "status": document.status,
                "processing_status": document.processing_status,
                "source_authority": document.source_authority,
                "document_date": document.document_date,
                "imported_at": document.imported_at,
                "well_id": document.well_id,
                "well_name": document.well_name,
                "project_id": document.project_id,
                "project_name": document.project_name,
                "company_id": document.company_id,
                "company_name": document.company_name,
                "page_count": document.page_count,
                "sheet_count": document.sheet_count,
                "word_count": document.word_count,
                "size_bytes": document.size_bytes,
                "sha256": document.sha256,
                "parser": document.parser,
                "is_current": document.is_current,
                "diagnostics": list(document.diagnostics),
            },
            matched_terms=tuple(hit.matched_terms),
            term_scores=dict(hit.term_scores),
        )
        if verify:
            return _with_verification(result, hit, repository=repository)
        return result


def _is_verbatim(text: str, excerpt: str) -> bool:
    """Can this chunk be read as a quotation of what the extractor recorded at its location?

    True when the chunk *contains* the excerpt of the cited region - a paragraph, or a whole table
    unit whose excerpt is the top of that table.  False when the chunk is a *view* carved out of a
    larger region (one row of a table, one field of a key/value pair), because there is then no
    text at that location that reads as this chunk, and an excerpt comparison would report a
    correct citation as broken.  Whitespace-insensitive, and deliberately one-directional: the
    excerpt has to appear in what we return, because that is what "quotation" means to a reader.
    """
    if not excerpt:
        return False
    left = " ".join(str(text or "").split()).casefold()
    right = " ".join(excerpt.split()).casefold()
    if not left or not right:
        return False
    return left == right or right in left


def _citation(chunk: Any, document: Any, *, cited: bool) -> str:
    """The one-line "where did this come from" a UI prints and a log can be searched for."""
    location = chunk.locator_ref or "whole document"
    label = document.source_relative_path or document.identity_path or document.filename
    revision = f" (revision {document.version_number})" if document.version_number else ""
    suffix = "" if cited else " [document-level: no location recorded]"
    return f"{label}{revision} > {location}{suffix}"


def _with_verification(
    result: SearchResult, hit: Hit, *, repository: DocumentRepository
) -> SearchResult:
    """Re-read the source and compare, through the same check the UI uses.

    Verification is opt-in per query because it opens files: on a workspace of a few thousand
    documents that is the difference between an instant result list and a slow one.  When it is
    asked for, a missing or changed file is reported in the result rather than hidden - the
    index said this text came from that file, and the reader deserves to know whether it still
    does.
    """
    from dataclasses import replace

    chunk = hit.chunk
    version = repository.version(chunk.version_id)
    if version is None:
        return replace(
            result,
            verification={
                "status": "NOT_CHECKABLE",
                "detail": f"version {chunk.version_id} is no longer in the registry",
            },
        )
    path = repository.resolve_source_path(version)
    if path is None:
        return replace(
            result,
            verification={
                "status": "UNREADABLE",
                "detail": f"source file not reachable from the workspace: {version.source_relative_path or version.source_path}",
            },
        )
    try:
        provenance = Provenance.from_dict(dict(chunk.provenance)) if chunk.provenance else None
    except Exception as exc:  # noqa: BLE001 - a malformed record is reported, not fatal
        return replace(
            result,
            verification={
                "status": "NOT_CHECKABLE",
                "detail": f"stored provenance unreadable: {exc}",
            },
        )
    if provenance is None:
        return replace(
            result,
            verification={
                "status": "NOT_CHECKABLE",
                "detail": "chunk has no recorded location to verify",
            },
        )
    if not result.verbatim:
        # A view of a larger region has nothing at its location that reads as this chunk, so the
        # excerpt comparison cannot apply to it.  What *can* be established - and what such a
        # chunk actually claims - is that the file behind the citation is still the file the
        # extraction was made from.  Weaker, and labelled as such rather than hidden.
        from ..core.hashing import sha256_file

        current_hash = sha256_file(Path(path))
        matches = bool(chunk.source_sha256) and current_hash == chunk.source_sha256
        return replace(
            result,
            verification={
                "status": "MATCH" if matches else "MISMATCH",
                "ok": matches,
                "check": "source",
                "detail": (
                    f"view of a larger cited region ({chunk.kind}): the excerpt recorded for the region is "
                    "not this chunk's text, so the source file's hash was compared with the hash this "
                    "version was indexed under"
                    if matches
                    else "source file changed since this version was extracted - re-extract and rebuild"
                ),
                "source": str(path),
                "expected_sha256": chunk.source_sha256,
                "actual_sha256": current_hash,
            },
        )
    outcome = verify_provenance(Path(path), provenance)
    return replace(
        result,
        verification={
            "status": outcome.status,
            "ok": bool(outcome.ok),
            "check": "excerpt",
            "detail": outcome.detail,
            "source": str(path),
            "current_excerpt": outcome.current_excerpt[:400] if outcome.current_excerpt else "",
        },
    )


def _date(value: Any) -> str | None:
    """Accept ``date``/``datetime``/ISO text for the range filters; everything else is an error."""
    if value is None:
        return None
    if isinstance(value, str):
        return value
    iso = getattr(value, "isoformat", None)
    if callable(iso):
        return str(iso())
    raise TypeError(f"date filter must be ISO text or a date, got {type(value).__name__}")


def build_index(database: Any, *, settings: Any = None) -> SqliteSearchIndex | InMemorySearchIndex:
    """Construct the index a workspace should use.

    The choice of backend is a capability question rather than a preference: a read-only or
    damaged data directory is discovered by opening it, and the answer is an in-memory index
    with a warning - search that works for this session - not a crash on startup.  A workspace
    that needs the index to survive a restart should be fixed, and ``doctor`` reports which
    backend is in use so this never silently becomes the permanent state.
    """
    if database is None:
        log.warning("search.index.in_memory", reason="no sidecar database configured")
        return InMemorySearchIndex()
    try:
        index = SqliteSearchIndex(database)
        index.ensure_schema()
    except Exception as exc:  # noqa: BLE001 - an unwritable data directory must not stop the app
        log.warning("search.index.in_memory", reason=f"{type(exc).__name__}: {exc}", level=25)
        return InMemorySearchIndex()
    if not index.schema_is_current():
        # Not fatal: rows written by a newer build still contain the text, and a rebuild fixes
        # the bookkeeping.  Saying so loudly is what keeps it from becoming a mystery later.
        log.warning("search.index.stale_schema", expected=SCHEMA_VERSION, level=25)
    return index
