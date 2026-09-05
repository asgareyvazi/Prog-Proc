"""Extractor contract shared by every parser (section 7 and 16).

An extractor is a *pure function of bytes*: given an :class:`ExtractionContext`
it returns a :class:`NormalizedDocument`.  It does not write to the database,
does not classify, and does not know about wells - that keeps parsers
independently testable and interchangeable behind the router.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol, runtime_checkable

from ..core.hashing import iso_utc
from ..core.provenance import (
    DocxLocator,
    ExcelLocator,
    PdfLocator,
    Provenance,
    TextLocator,
)
from .normalized import NormalizedDocument


@dataclass
class DocumentComplexity:
    """Cheap structural facts used by the router to choose a parser.

    These come from a *probe* (fast metadata read), never from a full parse, so
    routing costs milliseconds even for a 600-page DDR compilation.
    """

    pages: int = 0
    has_text_layer: bool = True
    text_chars_per_page: float = 0.0
    table_count: int = 0
    multi_column: bool = False
    is_scanned: bool = False
    encrypted: bool = False
    sheet_count: int = 0
    #: Why the document looks complex, in plain words - surfaced in the UI.
    reasons: list[str] = field(default_factory=list)

    @property
    def looks_structured_pdf(self) -> bool:
        return self.is_scanned or not self.has_text_layer or self.multi_column or self.table_count >= 3


@dataclass
class ExtractionContext:
    """Everything an extractor needs, and nothing else."""

    path: Path
    filename: str
    sha256: str
    extension: str = ""
    size_bytes: int = 0
    mime_type: str = ""
    document_id: str = ""
    document_version_id: str = ""
    #: Optional hints (registry classification, well linkage, user override).
    hints: dict[str, object] = field(default_factory=dict)
    complexity: DocumentComplexity = field(default_factory=DocumentComplexity)
    #: Extraction options validated against each extractor's declared options.
    options: dict[str, object] = field(default_factory=dict)

    def option(self, name: str, default: object = None) -> object:
        return self.options.get(name, default)


class ProvenanceBuilder:
    """Creates provenance records stamped with the current document identity."""

    __slots__ = ("confidence_default", "document_id", "document_version_id", "filename", "parser", "sha256")

    def __init__(
        self,
        document_id: str,
        document_version_id: str,
        filename: str,
        sha256: str,
        parser: str,
        confidence_default: float | None = None,
    ) -> None:
        self.document_id = document_id
        self.document_version_id = document_version_id
        self.filename = filename
        self.sha256 = sha256
        self.parser = parser
        self.confidence_default = confidence_default

    def _make(self, locator: object, excerpt: str, confidence: float | None) -> Provenance:
        return Provenance(
            document_id=self.document_id,
            document_version_id=self.document_version_id or None,
            filename=self.filename,
            locator=locator,  # type: ignore[arg-type]
            parser=self.parser,
            excerpt=excerpt,
            source_sha256=self.sha256,
            confidence=self.confidence_default if confidence is None else confidence,
        )

    def pdf(self, *, page: int | None = None, block: int | None = None, paragraph: int | None = None,
             section: str | None = None, table: int | None = None, bbox: tuple[float, float, float, float] | None = None,
             excerpt: str = "", confidence: float | None = None) -> Provenance:
        return self._make(
            PdfLocator(page=page, block=block, paragraph=paragraph, section=section, table=table, bbox=bbox),
            excerpt,
            confidence,
        )

    def excel(self, *, sheet: str, cell: str | None = None, range_: str | None = None, read: str = "value",
              row: int | None = None, column: int | None = None, excerpt: str = "",
              confidence: float | None = None) -> Provenance:
        return self._make(
            ExcelLocator(sheet=sheet, cell=cell, range_=range_, read=read, row=row, column=column), excerpt, confidence
        )

    def docx(self, *, heading: str | None = None, paragraph: int | None = None, table: int | None = None,
             row: int | None = None, column: int | None = None, excerpt: str = "",
             confidence: float | None = None) -> Provenance:
        return self._make(
            DocxLocator(heading=heading, paragraph=paragraph, table=table, row=row, column=column), excerpt, confidence
        )

    def text(self, *, line_start: int | None = None, line_end: int | None = None, char_start: int | None = None,
             char_end: int | None = None, section: str | None = None, excerpt: str = "",
             confidence: float | None = None) -> Provenance:
        return self._make(
            TextLocator(
                line_start=line_start,
                line_end=line_end,
                char_start=char_start,
                char_end=char_end,
                section=section,
            ),
            excerpt,
            confidence,
        )


@runtime_checkable
class DocumentExtractor(Protocol):
    """The one interface every parser implements (including MinerU)."""

    #: Stable machine name persisted with every extraction.
    name: str
    #: Bump whenever the extractor's output changes, to invalidate caches.
    version: str
    #: Human description shown in the UI/CLI.
    description: str = ""

    def supports(self, context: ExtractionContext) -> tuple[bool, str]:
        """Return (can_handle, reason).  The reason ends up in the audit trail."""
        ...

    def extract(self, context: ExtractionContext, provenance: ProvenanceBuilder) -> NormalizedDocument:
        """Parse the file into the normalised model.  Raise ExtractionError on failure."""
        ...

    def probe(self, context: ExtractionContext) -> DocumentComplexity:
        """Cheap structural probe used for routing.  Must not raise."""
        ...


def new_provenance_builder(context: ExtractionContext, parser: str, version: str) -> ProvenanceBuilder:
    builder = ProvenanceBuilder(
        document_id=context.document_id,
        document_version_id=context.document_version_id,
        filename=context.filename,
        sha256=context.sha256,
        parser=f"{parser}/{version}",
    )
    return builder


def stamp_extraction(document: NormalizedDocument, parser: str, version: str, engine: str = "") -> NormalizedDocument:
    """Attach extraction identity to the document metadata (single place, no drift)."""
    document.metadata.parser = parser
    document.metadata.parser_version = version
    if engine:
        document.metadata.engine = engine
    if not document.metadata.extracted_at:
        document.metadata.extracted_at = iso_utc()
    return document


__all__ = [
    "DocumentComplexity",
    "DocumentExtractor",
    "ExtractionContext",
    "ProvenanceBuilder",
    "new_provenance_builder",
    "stamp_extraction",
]
