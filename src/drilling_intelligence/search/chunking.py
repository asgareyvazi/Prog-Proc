"""Index records: what a chunk becomes in the search index, and what a document row holds.

The chunk selection itself lives with the document model
(:meth:`drilling_intelligence.extraction.normalized.NormalizedDocument.search_units`) so
there is exactly one chunker in the product.  This module does the two things an *index*
needs on top of it:

*   stable identities and statistics - each chunk gets a deterministic id derived from the
    version and its position, plus term counts and a token length, so ranking never has to
    re-read the source file;
*   the filterable metadata copy - company, project, well, document type, revision, status,
    dates.  These are denormalised into the index on purpose: the index is disposable and
    rebuildable, and a search that had to join back into the system of record for every
    candidate could not answer "the last three mud reports for A-3" quickly.  The registry
    remains the authority - ``SearchService.verify`` re-reads it (and the source file) before
    a result is presented as checked.

Locator text is copied out of the provenance record rather than reconstructed, so the index
says exactly what the extraction said: ``Summary!B9`` for a cell, ``page 12`` for a PDF page,
``lines 4-11`` for a note.  A chunk with no provenance is allowed only for the ``diagnostic``
kind - a statement *about* the extraction ("no text layer"), not a fact read from a location.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable, Mapping
from dataclasses import asdict, dataclass, field, replace
from typing import Any

from ..core.provenance import Provenance, SourceLocator
from ..extraction.normalized import NormalizedDocument, SearchUnit
from .tokenize import term_counts

__all__ = [
    "CHUNK_KINDS",
    "KIND_DIAGNOSTIC",
    "KIND_FIELD",
    "KIND_HEADING",
    "KIND_KNOWLEDGE",
    "KIND_PAGE",
    "KIND_PARAGRAPH",
    "KIND_TABLE_ROW",
    "MAX_INDEXED_CHARS",
    "UNCITABLE_KINDS",
    "ChunkSet",
    "IndexChunk",
    "IndexDocument",
    "build_chunk_set",
    "chunk_id_for",
    "chunks_for_document",
    "fact_chunks",
    "page_fallback_chunks",
    "uncited_chunks",
]

# The chunk vocabulary.  These are the ``unit_type`` strings
# :meth:`drilling_intelligence.extraction.normalized.NormalizedDocument.search_units` emits,
# plus ``page`` for the fallback chunks this module builds; ranking weights and index filters
# are keyed by the same names, so they are spelled once here and a test asserts the three
# vocabularies agree (a weight for a kind nothing ever produces is dead code that looks alive).
KIND_HEADING = "heading"
KIND_PARAGRAPH = "paragraph"
KIND_FIELD = "field"
KIND_TABLE_ROW = "table_row"
KIND_PAGE = "page"
KIND_DIAGNOSTIC = "diagnostic"
KIND_KNOWLEDGE = "knowledge_fact"

#: Kinds in document order, for display and for ``SearchFilters.kinds``.
#:
#: ``knowledge_fact`` is listed first because it is the most condensed form of the same
#: information: a fact chunk answers "what does the corpus *assert*", where the others answer
#: "what does the corpus *say*".  Listing it here is also what makes
#: ``SearchFilters.kinds=["knowledge_fact"]`` a real query instead of a filter that can match
#: nothing, and it stays citable - a fact hit answers with the page/sheet/cell its value came
#: from, exactly like a field chunk does.
CHUNK_KINDS: tuple[str, ...] = (
    KIND_KNOWLEDGE,
    KIND_HEADING,
    KIND_PARAGRAPH,
    KIND_FIELD,
    KIND_TABLE_ROW,
    KIND_PAGE,
    KIND_DIAGNOSTIC,
)

#: Kinds that may exist without a provenance record: a diagnostic describes the extraction, and
#: a page-fallback chunk is the whole page because nothing citable was found on it.  Any other
#: uncited chunk is a bug, and :func:`build_chunk_set` says so rather than indexing it quietly.
UNCITABLE_KINDS: frozenset[str] = frozenset({KIND_DIAGNOSTIC, KIND_PAGE})

#: One chunk is never larger than this many characters of stored text.  The document chunker
#: already respects ``max_chars``; this is the outer guard for anything handed to us raw.
MAX_INDEXED_CHARS = 8000


@dataclass(frozen=True)
class IndexChunk:
    """One searchable unit, with everything needed to rank, display and cite it."""

    chunk_id: str
    document_id: str
    version_id: str
    chunk_index: int
    kind: str
    text: str
    page: int | None
    sheet: str
    locator_ref: str
    provenance: Mapping[str, Any] | None
    terms: Mapping[str, int]
    length: int
    char_count: int
    #: sha256 of the source file the excerpt was taken from (verification, not retrieval).
    source_sha256: str

    @property
    def is_cited(self) -> bool:
        return self.provenance is not None

    def to_row(self) -> dict[str, Any]:
        """The sidecar row for this chunk (JSON columns are encoded here, once)."""
        return {
            "chunk_id": self.chunk_id,
            "document_id": self.document_id,
            "version_id": self.version_id,
            "chunk_index": self.chunk_index,
            "kind": self.kind,
            "text": self.text,
            "page": self.page,
            "sheet": self.sheet,
            "locator_ref": self.locator_ref,
            "provenance_json": json.dumps(self.provenance, sort_keys=True, ensure_ascii=False)
            if self.provenance
            else None,
            "terms_json": json.dumps(dict(self.terms), sort_keys=True),
            "length": self.length,
            "char_count": self.char_count,
            "source_sha256": self.source_sha256,
        }

    @classmethod
    def from_row(cls, row: Mapping[str, Any]) -> IndexChunk:
        provenance = row.get("provenance_json")
        terms = row.get("terms_json") or "{}"
        return cls(
            chunk_id=str(row["chunk_id"]),
            document_id=str(row["document_id"]),
            version_id=str(row["version_id"]),
            chunk_index=int(row.get("chunk_index") or 0),
            kind=str(row.get("kind") or KIND_PARAGRAPH),
            text=str(row.get("text") or ""),
            page=row.get("page"),
            sheet=str(row.get("sheet") or ""),
            locator_ref=str(row.get("locator_ref") or ""),
            provenance=json.loads(provenance) if provenance else None,
            terms=json.loads(terms),
            length=int(row.get("length") or 0),
            char_count=int(row.get("char_count") or 0),
            source_sha256=str(row.get("source_sha256") or ""),
        )


@dataclass(frozen=True)
class IndexDocument:
    """The filterable copy of a registry entry (one row per indexed version)."""

    document_id: str
    version_id: str
    version_number: int
    workspace_id: str = ""
    project_id: str = ""
    company_id: str = ""
    project_name: str = ""
    company_name: str = ""
    well_id: str = ""
    well_name: str = ""
    document_type: str = "OTHER"
    title: str = ""
    filename: str = ""
    identity_path: str = ""
    source_relative_path: str = ""
    extension: str = ""
    parser: str = ""
    revision: str = ""
    revision_key: int = 0
    status: str = ""
    processing_status: str = ""
    source_authority: str = ""
    document_date: str = ""
    imported_at: str = ""
    page_count: int = 0
    sheet_count: int = 0
    word_count: int = 0
    size_bytes: int = 0
    sha256: str = ""
    is_current: bool = True
    #: Extraction diagnostics (e.g. ``EXTRACTION_TRUNCATED: max_cells=...``, "no text layer"):
    #: they belong to the document as a whole, not to one chunk.
    diagnostics: tuple[str, ...] = ()
    chunk_count: int = 0

    def to_row(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["diagnostics"] = json.dumps(list(self.diagnostics), ensure_ascii=False)
        payload["is_current"] = bool(self.is_current)
        return payload

    @classmethod
    def from_row(cls, row: Mapping[str, Any]) -> IndexDocument:
        values = dict(row)
        raw = values.get("diagnostics")
        if isinstance(raw, str):
            values["diagnostics"] = tuple(json.loads(raw or "[]"))
        elif raw is None:
            values["diagnostics"] = ()
        for key in (
            "version_number",
            "revision_key",
            "page_count",
            "sheet_count",
            "word_count",
            "size_bytes",
            "chunk_count",
        ):
            values[key] = int(values.get(key) or 0)
        for key in (
            "workspace_id",
            "project_id",
            "company_id",
            "project_name",
            "company_name",
            "well_id",
            "well_name",
            "document_type",
            "title",
            "filename",
            "identity_path",
            "source_relative_path",
            "extension",
            "parser",
            "revision",
            "status",
            "processing_status",
            "source_authority",
            "document_date",
            "imported_at",
            "sha256",
        ):
            values[key] = str(values.get(key) or "")
        values["is_current"] = bool(values.get("is_current", True))
        return cls(**{key: values.get(key) for key in cls.__dataclass_fields__ if key in values})

    def reference(self) -> str:
        """How a result names its source: the durable path, not an absolute one."""
        return self.source_relative_path or self.identity_path or self.filename


@dataclass
class ChunkSet:
    """Chunks plus the document row they belong to (what an index upsert takes)."""

    document: IndexDocument
    chunks: list[IndexChunk] = field(default_factory=list)

    @property
    def kinds(self) -> dict[str, int]:
        tally: dict[str, int] = {}
        for chunk in self.chunks:
            tally[chunk.kind] = tally.get(chunk.kind, 0) + 1
        return tally


def chunk_id_for(version_id: str, chunk_index: int) -> str:
    """A deterministic id: re-indexing the same version produces the same identifiers.

    Content would be a better key than position, but position is what a user sees first
    ("third paragraph on page 4") and it keeps a rebuild byte-for-byte reproducible even when
    a chunk's text changes by a space.
    """
    digest = hashlib.sha256(f"{version_id}:{chunk_index}".encode()).hexdigest()
    return f"chk-{digest[:24]}"


def _locator_of(provenance: Provenance | None) -> tuple[str, str, int | None]:
    """``(sheet, locator_ref, page)`` straight out of the recorded locator.

    Nothing is invented here: an Excel chunk says which sheet and range, a PDF chunk says
    which page, a text chunk says which lines, and a chunk whose locator has no page simply
    reports ``None`` rather than a plausible zero.
    """
    if provenance is None:
        return "", "", None
    locator = provenance.locator
    sheet = str(getattr(locator, "sheet", "") or "")
    page = getattr(locator, "page", None)
    try:
        page_value = int(page) if page is not None else None
    except (TypeError, ValueError):  # pragma: no cover - defensive against hand-edited JSON
        page_value = None
    return sheet, locator.ref(), page_value


def chunks_for_document(
    *,
    document_id: str,
    version_id: str,
    normalized: NormalizedDocument,
    source_sha256: str = "",
    max_chars: int = 1200,
) -> list[IndexChunk]:
    """Index-ready chunks for one extracted artefact, in document order."""
    units: list[SearchUnit] = list(normalized.search_units(max_chars=max_chars))
    chunks: list[IndexChunk] = []
    for position, unit in enumerate(units):
        text = (unit.text or "").strip()
        if not text:
            continue
        text = text[:MAX_INDEXED_CHARS]
        provenance = unit.provenance.to_dict() if unit.provenance else None
        sheet, locator_ref, page = _locator_of(unit.provenance)
        counts = term_counts(text)
        chunks.append(
            IndexChunk(
                chunk_id=chunk_id_for(version_id, position),
                document_id=document_id,
                version_id=version_id,
                chunk_index=position,
                kind=str(unit.unit_type or KIND_PARAGRAPH),
                text=text,
                page=page,
                sheet=sheet,
                locator_ref=locator_ref,
                provenance=provenance,
                terms=counts,
                length=sum(counts.values()),
                char_count=len(text),
                source_sha256=source_sha256,
            )
        )
    return chunks


def page_fallback_chunks(
    *,
    document_id: str,
    version_id: str,
    normalized: NormalizedDocument,
    source_sha256: str = "",
    start_index: int = 0,
    max_chars: int = MAX_INDEXED_CHARS,
) -> list[IndexChunk]:
    """Page-level chunks for artefacts whose structure produced none.

    A document can legitimately come out of an extractor with text but no paragraphs - a
    table-only PDF, a workbook where every sheet was one wide row set.  Dropping it from the
    index entirely would be the worst outcome (the file exists, is searchable in principle,
    and is now invisible), so each page becomes one chunk and the locator says so plainly:
    "page 7", not a fake paragraph number.
    """
    if start_index or not normalized.pages:
        return []
    if not any((page.text or "").strip() for page in normalized.pages):
        return []
    chunks: list[IndexChunk] = []
    index = start_index
    for page in normalized.pages:
        text = (page.text or "").strip()[:max_chars]
        if not text:
            continue
        counts = term_counts(text)
        chunks.append(
            IndexChunk(
                chunk_id=chunk_id_for(version_id, index),
                document_id=document_id,
                version_id=version_id,
                chunk_index=index,
                kind=KIND_PAGE,
                text=text,
                page=page.index,
                sheet=page.label or "",
                locator_ref=f"Page: {page.index}"
                + (f" (sheet {page.label})" if page.label else ""),
                provenance=None,
                terms=counts,
                length=sum(counts.values()),
                char_count=len(text),
                source_sha256=source_sha256,
            )
        )
        index += 1
    return chunks


def build_chunk_set(
    *,
    document: IndexDocument,
    normalized: NormalizedDocument,
    version_id: str,
    source_sha256: str = "",
    max_chars: int = 1200,
    extra_chunks: Iterable[IndexChunk] = (),
) -> ChunkSet:
    """Structure chunks, plus page fallback, with the document row's count filled in.

    ``extra_chunks`` is how derived knowledge joins the same index: the facts are numbered after
    the document's own chunks, so one version's chunk set stays a single deterministic sequence
    whether it came from the artefact or from the knowledge repository.
    """
    chunks = chunks_for_document(
        document_id=document.document_id,
        version_id=version_id,
        normalized=normalized,
        source_sha256=source_sha256,
        max_chars=max_chars,
    )
    if not chunks:
        chunks = page_fallback_chunks(
            document_id=document.document_id,
            version_id=version_id,
            normalized=normalized,
            source_sha256=source_sha256,
        )
    extra = list(extra_chunks)
    if extra:
        # Numbered after the document's own chunks, then re-identified, because ``chunk_id_for``
        # is keyed by position: reusing it keeps one deterministic sequence per version instead of
        # inventing a second identifier scheme for derived knowledge.
        renumbered = [
            replace(
                chunk,
                chunk_index=len(chunks) + position,
                chunk_id=chunk_id_for(str(version_id), len(chunks) + position),
            )
            for position, chunk in enumerate(extra)
        ]
        chunks = chunks + renumbered
    return ChunkSet(document=_with_counts(document, chunks), chunks=chunks)


def fact_chunks(
    version_id: str,
    document_id: str,
    facts: Iterable[Any],
    *,
    source_sha256: str = "",
    start_index: int = 0,
) -> list[IndexChunk]:
    """Index chunks for the *facts* stored about one version (architecture section 19).

    Search has to find knowledge, not only text.  "What mud weight did we record for A-3?" is a
    question about a value, and a value reachable only by re-reading a table row is a value
    nobody will trust enough to ask about.  Each chunk carries the fact's predicate, both units,
    its status and its provenance, so a hit can be read, cited and argued with in one step.

    The facts come from the knowledge repository - the authoritative copy - and this function only
    formats, so the disposable index never becomes a second store of anything.  A fact with no
    provenance is indexed and reported as uncited rather than hidden: it exists, and an index that
    quietly dropped the uncomfortable rows would make the knowledge base look cleaner than it is.

    ``start_index`` continues the version's own chunk numbering, which is what keeps chunk ids
    unique without inventing a second identifier scheme (see :func:`chunk_id_for`) - and because
    ids derive from position, the *order* is part of this function's contract: facts are sorted by
    content (version, predicate, source wording, row id) rather than by the row's timestamp, so a
    rebuild after a week produces the same identifiers as the pass that ran at ingest time - which
    is also why the lifecycle status is formatted out of the text rather than into it (see
    :func:`_fact_text`).
    """
    ordered = sorted(
        facts,
        key=lambda fact: (
            str(getattr(fact, "document_version_id", "") or ""),
            str(getattr(fact, "predicate", "") or ""),
            str(getattr(fact, "original_value", "") or ""),
            str(getattr(fact, "item_id", "") or ""),
        ),
    )
    chunks: list[IndexChunk] = []
    for offset, fact in enumerate(ordered):
        content = fact.to_dict() if hasattr(fact, "to_dict") else dict(fact)
        citation = str(fact.citation()) if hasattr(fact, "citation") else _fact_citation(content)
        provenance = content.get("provenance")
        provenance = dict(provenance) if isinstance(provenance, Mapping) else None
        text = _fact_text(content, citation)[:MAX_INDEXED_CHARS]
        sheet, page, locator_ref = _fact_locator(provenance)
        counts = term_counts(text)
        chunks.append(
            IndexChunk(
                chunk_id=chunk_id_for(str(version_id), start_index + offset),
                document_id=str(document_id or (provenance or {}).get("document_id") or ""),
                version_id=str(version_id),
                chunk_index=start_index + offset,
                kind=KIND_KNOWLEDGE,
                text=text,
                page=page,
                sheet=sheet,
                locator_ref=locator_ref or citation,
                provenance=provenance,
                terms=counts,
                length=sum(counts.values()),
                char_count=len(text),
                source_sha256=str((provenance or {}).get("source_sha256") or source_sha256 or ""),
            )
        )
    return chunks


def _fact_locator(provenance: Mapping[str, Any] | None) -> tuple[str, int | None, str]:
    """``(sheet, page, locator_ref)`` of a fact's source cell, read from the recorded locator only.

    ``locator_ref`` goes through the same :meth:`SourceLocator.ref` the document chunks use, so a
    fact and the field it came from are displayed identically.  A second renderer for the same
    locator would be a second opinion about where a value came from.
    """
    if not provenance:
        return "", None, ""
    raw = provenance.get("locator")
    locator = dict(raw) if isinstance(raw, Mapping) else {}
    page = locator.get("page")
    try:
        page_value = int(page) if page is not None else None
    except (TypeError, ValueError):  # pragma: no cover - defensive against hand-edited JSON
        page_value = None
    try:
        ref = SourceLocator.from_dict(locator).ref()
    except Exception:  # noqa: BLE001 - a malformed locator degrades to no reference, never a wrong one
        ref = ""
    return str(locator.get("sheet") or ""), page_value, str(ref or "")


def _fact_text(content: Mapping[str, Any], citation: str) -> str:
    """The indexed body of a fact: the claim, both units, and where it came from.

    Both the source's own wording and the normalised value are in here on purpose: someone
    searching "10.2 ppg" and someone searching "1.06 sg" are asking about the same fact written by
    two people, and both must find it.

    What is deliberately *not* here is the fact's lifecycle status (``CONFLICTED``, ``RETIRED``,
    ``SUPERSEDED``).  Those are states the registry changes after the fact was written - a marking
    pass over another document can move them - and this text is stored in a disposable sidecar that
    is not rewritten by such a pass.  Indexing the status would therefore bake a lie into the index
    the moment a conflict was recorded, and would make "a rebuild reproduces the same index" false.
    Status is read from the registry by every listing that shows it: :meth:`search the knowledge
    commands <drilling_intelligence.knowledge.service.KnowledgeExtractionService.facts>` prints it,
    and a search hit carries the row it came from.
    """
    label = str(content.get("predicate_label") or "").strip()
    predicate = str(content.get("predicate") or "").replace("_", " ").strip()
    value = content.get("original_value")
    unit = content.get("original_unit") or ""
    claim = f"{label or predicate} = {value} {unit}".strip().rstrip("=").strip()
    parts = [
        str(content.get("title") or "").strip() or claim,
        f"predicate: {predicate}" if predicate else "",
        f"as written: {value} {unit}".strip() if value else "",
        f"normalised: {content.get('normalized_value')} {content.get('normalized_unit') or ''}".strip()
        if content.get("normalized_value") not in (None, "")
        else "",
        f"record state: {content.get('record_state')}" if content.get("record_state") else "",
        f"valid from {content.get('valid_from')}" if content.get("valid_from") else "",
        f"valid until {content.get('valid_to')}" if content.get("valid_to") else "",
        f"confidence: {content.get('confidence')}" if content.get("confidence") is not None else "",
        f"note: {content.get('note')}" if content.get("note") else "",
        f"source: {citation}" if citation else "source: no provenance recorded",
    ]
    return "\n".join(part for part in parts if part)


def _fact_citation(content: Mapping[str, Any]) -> str:
    """Best-effort citation string from a fact *dictionary* (the object path uses ``citation()``)."""
    provenance = content.get("provenance")
    provenance = dict(provenance) if isinstance(provenance, Mapping) else {}
    filename = str(provenance.get("filename") or "")
    locator = provenance.get("locator")
    locator = dict(locator) if isinstance(locator, Mapping) else {}
    ref = str(locator.get("ref") or "").strip()
    if not ref:
        pieces = [
            f"Sheet: {locator.get('sheet')}" if locator.get("sheet") else "",
            f"Cell: {locator.get('cell')}" if locator.get("cell") else "",
            f"Range: {locator.get('range_')}" if locator.get("range_") else "",
            f"Page: {locator.get('page')}" if locator.get("page") is not None else "",
        ]
        ref = " > ".join(piece for piece in pieces if piece)
    return f"{filename} > {ref}" if filename and ref else (filename or ref)


def uncited_chunks(chunks: Iterable[IndexChunk]) -> list[IndexChunk]:
    """Body chunks that carry no provenance - the ones that must not be presented as quotations.

    A ``paragraph`` chunk without a location means the extractor lost the trail somewhere
    between the file and the artefact: the text may be right, but nobody can prove it, and in a
    workspace where every engineering value is supposed to cite its source that is a defect
    worth a warning rather than a detail.  ``diagnostic`` and ``page`` chunks are exempt by
    design (see :data:`UNCITABLE_KINDS`).
    """
    return [chunk for chunk in chunks if not chunk.provenance and chunk.kind not in UNCITABLE_KINDS]


def _with_counts(document: IndexDocument, chunks: Iterable[IndexChunk]) -> IndexDocument:
    counted = list(chunks)
    if document.chunk_count == len(counted):
        return document
    return IndexDocument(**{**asdict(document), "chunk_count": len(counted)})
