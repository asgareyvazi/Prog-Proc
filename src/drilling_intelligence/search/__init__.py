"""Search foundation: index extracted text, keep provenance, rank deterministically.

The pipeline through here is deliberately short and one-directional:

``NormalizedDocument`` (:mod:`drilling_intelligence.extraction`)
    → :func:`drilling_intelligence.extraction.normalized.NormalizedDocument.search_units` -
    drilling-aware chunks, each carrying its own location
    → :mod:`drilling_intelligence.search.chunking` - ids, per-chunk term counts, denormalised
    registry metadata
    → :mod:`drilling_intelligence.search.index` - a disposable sidecar database (or a dictionary)
    → :mod:`drilling_intelligence.search.ranking` + :mod:`drilling_intelligence.search.tokenize`
    - BM25 over a fixed vocabulary, no extension required, no model
    → :class:`drilling_intelligence.search.service.SearchService` - the only API callers use

Two properties hold the package together, and everything else is detail:

**The index is derived, never authoritative.**  It is rebuilt from the registry in one call
(:meth:`SearchService.rebuild`) and may be deleted at any time; the document registry, its
version chain and its artefacts are the record of what was extracted from which file.

**A hit cites a location.**  Chunking preserves document → section → heading → table →
paragraph → sheet → row/range → page; a character-window splitter is used nowhere, because a
snippet that cuts a number away from its label is worthless in a directory of drilling
documents, and because the citation has to survive into retrieval.

Vector retrieval and RAG are *not* in here.  When they arrive they add a recall path and a
context builder around this same ranked, cited result set - not a replacement for it.
"""

from .chunking import (
    CHUNK_KINDS,
    MAX_INDEXED_CHARS,
    UNCITABLE_KINDS,
    ChunkSet,
    IndexChunk,
    IndexDocument,
    build_chunk_set,
    chunk_id_for,
    chunks_for_document,
    page_fallback_chunks,
    uncited_chunks,
)
from .index import (
    MAX_CANDIDATES,
    SCHEMA_VERSION,
    Hit,
    IndexStats,
    InMemorySearchIndex,
    SearchFilters,
    SearchIndex,
    SearchRequest,
    SqliteSearchIndex,
    chunk_set_for,
    index_document_for,
    score_candidates,
    search_metadata,
)
from .ranking import (
    DEFAULT_KIND_WEIGHTS,
    K1,
    B,
    IndexStatistics,
    MatchedChunk,
    candidate_matches,
    phrase_present,
    rank_chunks,
)
from .service import SearchResponse, SearchResult, SearchService, build_index
from .tokenize import STOPWORDS, highlight, parse_query, term_counts, tokenize, tokens_of_query

__all__ = [
    "CHUNK_KINDS",
    "DEFAULT_KIND_WEIGHTS",
    "K1",
    "MAX_CANDIDATES",
    "MAX_INDEXED_CHARS",
    "SCHEMA_VERSION",
    "STOPWORDS",
    "UNCITABLE_KINDS",
    "B",
    "ChunkSet",
    "Hit",
    "InMemorySearchIndex",
    "IndexChunk",
    "IndexDocument",
    "IndexStatistics",
    "IndexStats",
    "MatchedChunk",
    "SearchFilters",
    "SearchIndex",
    "SearchRequest",
    "SearchResponse",
    "SearchResult",
    "SearchService",
    "SqliteSearchIndex",
    "build_chunk_set",
    "build_index",
    "candidate_matches",
    "chunk_id_for",
    "chunk_set_for",
    "chunks_for_document",
    "highlight",
    "index_document_for",
    "page_fallback_chunks",
    "parse_query",
    "phrase_present",
    "rank_chunks",
    "score_candidates",
    "search_metadata",
    "term_counts",
    "tokenize",
    "tokens_of_query",
    "uncited_chunks",
]
