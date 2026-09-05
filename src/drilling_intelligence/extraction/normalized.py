"""The normalised document model every extractor must produce (section 16).

Keeping this representation parser-agnostic is what lets MinerU, PyMuPDF,
openpyxl, python-docx and a future dedicated DDR reader feed the same
knowledge/classification/retrieval pipeline.  It is also the artefact that is
persisted, so ``from_json(to_json())`` round-tripping is a tested contract.

Structure:

    NormalizedDocument
     ├── metadata      (what file, which parser, which version, when)
     ├── pages         (text + char offsets + geometry)
     ├── sections      (heading tree, each with page/char range)
     ├── paragraphs    (ordered blocks, each carrying its own provenance)
     ├── tables        (rectangular cell grids with sheet/range or page provenance)
     ├── figures       (image objects with page provenance)
     ├── extracted_fields (DataField: value+unit+provenance+quality)
     ├── provenance    (flat index of all provenance records)
     └── diagnostics   (parser warnings, fallbacks, limitations - never hidden)
"""

from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass, field
from typing import Any

from ..core.hashing import sha256_text
from ..core.provenance import Provenance
from ..core.results import DataField

_WS = re.compile(r"[ \t]+")


def clean_text(text: str) -> str:
    """Normalise line endings and horizontal whitespace, preserving paragraph breaks."""
    text = (text or "").replace("\r\n", "\n").replace("\r", "\n").replace("\u00a0", " ")
    text = "\n".join(_WS.sub(" ", line).strip() for line in text.split("\n"))
    while "\n\n\n" in text:
        text = text.replace("\n\n\n", "\n\n")
    return text.strip()


@dataclass
class ExtractionMetadata:
    filename: str = ""
    path: str = ""
    sha256: str = ""
    extension: str = ""
    mime_type: str = ""
    size_bytes: int = 0
    #: Which extractor produced this, and its version - the auditability keys.
    parser: str = ""
    parser_version: str = ""
    #: Upstream tool that actually read the bytes when a wrapper is used
    #: (e.g. parser="mineru" with engine="MinerU 3.4.5 / pipeline backend").
    engine: str = ""
    extracted_at: str = ""
    #: Document date as authored (from metadata or detected content), not file mtime.
    document_date: str = ""
    page_count: int = 0
    extra: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class Page:
    index: int  # 1-based, what a human reads
    text: str = ""
    char_start: int = 0
    char_end: int = 0
    width: float = 0.0
    height: float = 0.0
    #: Sheets for Excel-like documents, so "page" is meaningful to a user.
    label: str = ""
    block_count: int = 0
    extra: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> Page:
        return cls(
            index=int(payload.get("index", 1)),
            text=payload.get("text", "") or "",
            char_start=int(payload.get("char_start", 0)),
            char_end=int(payload.get("char_end", 0)),
            width=float(payload.get("width", 0.0) or 0.0),
            height=float(payload.get("height", 0.0) or 0.0),
            label=payload.get("label", "") or "",
            block_count=int(payload.get("block_count", 0) or 0),
            extra=dict(payload.get("extra") or {}),
        )


@dataclass
class Paragraph:
    index: int
    text: str
    page: int | None = None
    block: int | None = None
    #: Heading level when this paragraph is a heading (1 = top level).
    heading_level: int | None = None
    style: str = ""
    section: str = ""
    char_start: int = 0
    char_end: int = 0
    provenance: Provenance | None = None

    @property
    def is_heading(self) -> bool:
        return self.heading_level is not None

    def to_dict(self) -> dict[str, Any]:
        return {
            "index": self.index,
            "text": self.text,
            "page": self.page,
            "block": self.block,
            "heading_level": self.heading_level,
            "style": self.style,
            "section": self.section,
            "char_start": self.char_start,
            "char_end": self.char_end,
            "provenance": self.provenance.to_dict() if self.provenance else None,
        }

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> Paragraph:
        provenance = payload.get("provenance")
        return cls(
            index=int(payload.get("index", 0)),
            text=payload.get("text", "") or "",
            page=payload.get("page"),
            block=payload.get("block"),
            heading_level=payload.get("heading_level"),
            style=payload.get("style", "") or "",
            section=payload.get("section", "") or "",
            char_start=int(payload.get("char_start", 0) or 0),
            char_end=int(payload.get("char_end", 0) or 0),
            provenance=Provenance.from_dict(provenance) if provenance else None,
        )


@dataclass
class Section:
    heading: str
    level: int = 1
    page: int | None = None
    char_start: int = 0
    char_end: int = 0
    paragraph_indices: list[int] = field(default_factory=list)
    #: Numbered section reference as authored, e.g. "4.2".
    number: str = ""

    @property
    def label(self) -> str:
        return f"{self.number} {self.heading}".strip() if self.number else self.heading

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> Section:
        return cls(
            heading=payload.get("heading", "") or "",
            level=int(payload.get("level", 1) or 1),
            page=payload.get("page"),
            char_start=int(payload.get("char_start", 0) or 0),
            char_end=int(payload.get("char_end", 0) or 0),
            paragraph_indices=list(payload.get("paragraph_indices") or []),
            number=payload.get("number", "") or "",
        )


@dataclass
class Table:
    table_id: str
    rows: list[list[str | None]] = field(default_factory=list)
    caption: str = ""
    page: int | None = None
    sheet: str = ""
    #: Excel range (A1:F18) or PDF table index, for display and verification.
    anchor: str = ""
    provenance: Provenance | None = None
    #: Header row detected by the extractor (first row by convention).
    has_header: bool = True
    extra: dict[str, Any] = field(default_factory=dict)

    @property
    def row_count(self) -> int:
        return len(self.rows)

    @property
    def column_count(self) -> int:
        return max((len(r) for r in self.rows), default=0)

    @property
    def header(self) -> list[str]:
        if not self.rows:
            return []
        return [("" if c is None else str(c)).strip() for c in self.rows[0]]

    def cell(self, row: int, col: int) -> str:
        try:
            value = self.rows[row][col]
        except IndexError:
            return ""
        return "" if value is None else str(value)

    def iter_data_rows(self) -> list[tuple[int, list[str]]]:
        start = 1 if (self.has_header and self.rows) else 0
        out: list[tuple[int, list[str]]] = []
        for offset, row in enumerate(self.rows[start:], start=start):
            out.append((offset, ["" if c is None else str(c).strip() for c in row]))
        return out

    def to_dicts(self) -> list[dict[str, str]]:
        headers = self.header
        if not headers:
            return []
        records: list[dict[str, str]] = []
        for _, row in self.iter_data_rows():
            record: dict[str, str] = {}
            for index, key in enumerate(headers):
                name = key or f"column_{index + 1}"
                record[name] = row[index] if index < len(row) else ""
            records.append(record)
        return records

    def text(self) -> str:
        lines = ["\t".join(self.cell(r, c) for c in range(self.column_count)) for r in range(self.row_count)]
        return "\n".join(lines)

    def to_dict(self) -> dict[str, Any]:
        return {
            "table_id": self.table_id,
            "rows": self.rows,
            "caption": self.caption,
            "page": self.page,
            "sheet": self.sheet,
            "anchor": self.anchor,
            "has_header": self.has_header,
            "provenance": self.provenance.to_dict() if self.provenance else None,
            "extra": self.extra,
        }

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> Table:
        provenance = payload.get("provenance")
        return cls(
            table_id=payload.get("table_id", "table"),
            rows=[list(r) for r in (payload.get("rows") or [])],
            caption=payload.get("caption", "") or "",
            page=payload.get("page"),
            sheet=payload.get("sheet", "") or "",
            anchor=payload.get("anchor", "") or "",
            provenance=Provenance.from_dict(provenance) if provenance else None,
            has_header=bool(payload.get("has_header", True)),
            extra=dict(payload.get("extra") or {}),
        )


@dataclass
class Figure:
    figure_id: str
    page: int | None = None
    kind: str = "image"  # image | chart | stamp | signature
    caption: str = ""
    #: Pixel/point bounding box for verification, where the parser provides it.
    bbox: tuple[float, float, float, float] | None = None
    provenance: Provenance | None = None
    #: Text recovered by OCR/VLM, if any (never a guess).
    text: str = ""
    extra: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "figure_id": self.figure_id,
            "page": self.page,
            "kind": self.kind,
            "caption": self.caption,
            "bbox": list(self.bbox) if self.bbox else None,
            "text": self.text,
            "provenance": self.provenance.to_dict() if self.provenance else None,
            "extra": self.extra,
        }

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> Figure:
        bbox = payload.get("bbox")
        provenance = payload.get("provenance")
        return cls(
            figure_id=payload.get("figure_id", "figure"),
            page=payload.get("page"),
            kind=payload.get("kind", "image") or "image",
            caption=payload.get("caption", "") or "",
            bbox=tuple(bbox) if bbox else None,  # type: ignore[arg-type]
            provenance=Provenance.from_dict(provenance) if provenance else None,
            text=payload.get("text", "") or "",
            extra=dict(payload.get("extra") or {}),
        )


@dataclass
class SearchUnit:
    """A retrievable chunk with exactly one provenance reference."""

    text: str
    unit_type: str  # paragraph | table_row | field | heading
    page: int | None = None
    section: str = ""
    index: int = 0
    provenance: Provenance | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "text": self.text,
            "unit_type": self.unit_type,
            "page": self.page,
            "section": self.section,
            "index": self.index,
            "provenance": self.provenance.to_dict() if self.provenance else None,
        }


@dataclass
class NormalizedDocument:
    metadata: ExtractionMetadata = field(default_factory=ExtractionMetadata)
    pages: list[Page] = field(default_factory=list)
    sections: list[Section] = field(default_factory=list)
    paragraphs: list[Paragraph] = field(default_factory=list)
    tables: list[Table] = field(default_factory=list)
    figures: list[Figure] = field(default_factory=list)
    extracted_fields: list[DataField] = field(default_factory=list)
    #: Diagnostics are part of the artefact, not a log line: the UI shows them
    #: and the QA checks read them ("scanned PDF, no text layer" must be visible).
    diagnostics: list[str] = field(default_factory=list)
    #: Full normalised text, kept so that offsets, hashing and search agree.
    text: str = ""

    # -- derived ------------------------------------------------------------
    @property
    def provenance(self) -> list[Provenance]:
        seen: dict[str, Provenance] = {}
        for paragraph in self.paragraphs:
            if paragraph.provenance:
                seen[paragraph.provenance.ref] = paragraph.provenance
        for table in self.tables:
            if table.provenance:
                seen[table.provenance.ref] = table.provenance
        for item in self.extracted_fields:
            if item.provenance:
                seen[item.provenance.ref] = item.provenance
        return list(seen.values())

    @property
    def char_count(self) -> int:
        return len(self.text)

    @property
    def word_count(self) -> int:
        return len(self.text.split())

    @property
    def content_digest(self) -> str:
        """Digest of the *extracted* content (independent of file byte order)."""
        return sha256_text(self.text)

    @property
    def text_chars_per_page(self) -> float:
        pages = max(1, self.metadata.page_count or len(self.pages))
        return self.char_count / pages

    def paragraph_at(self, index: int) -> Paragraph | None:
        for paragraph in self.paragraphs:
            if paragraph.index == index:
                return paragraph
        return None

    def field(self, name: str) -> DataField | None:
        for item in self.extracted_fields:
            if item.name == name:
                return item
        return None

    def fields_named(self, name: str) -> list[DataField]:
        return [item for item in self.extracted_fields if item.name == name]

    def section_for_paragraph(self, index: int) -> Section | None:
        for section in self.sections:
            if index in section.paragraph_indices:
                return section
        return None

    def search_units(self, *, include_tables: bool = True, max_chars: int = 1200) -> list[SearchUnit]:
        """Chunk the document for keyword indexing, each chunk provenance-anchored.

        Chunking deliberately follows the document's own structure (paragraph,
        table row) rather than a fixed character window, so a search hit can
        always be shown as "page 27, section 4.2, paragraph 3".
        """
        units: list[SearchUnit] = []
        buffer: list[str] = []
        for paragraph in self.paragraphs:
            if not paragraph.text.strip():
                continue
            buffer.append(paragraph.text)
            joined = "\n".join(buffer)
            if len(joined) >= max_chars or paragraph.is_heading:
                units.append(
                    SearchUnit(
                        text=joined,
                        unit_type="paragraph",
                        page=paragraph.page,
                        section=paragraph.section,
                        index=paragraph.index,
                        provenance=paragraph.provenance,
                    )
                )
                buffer = []
        if buffer:
            last = self.paragraphs[-1] if self.paragraphs else None
            units.append(
                SearchUnit(
                    text="\n".join(buffer),
                    unit_type="paragraph",
                    page=last.page if last else None,
                    section=last.section if last else "",
                    index=last.index if last else 0,
                    provenance=last.provenance if last else None,
                )
            )
        if include_tables:
            for table in self.tables:
                for row_index, row in table.iter_data_rows():
                    rendered = " | ".join(cell for cell in row if cell)
                    if not rendered.strip():
                        continue
                    header = " | ".join(h for h in table.header if h)
                    units.append(
                        SearchUnit(
                            text=f"{table.caption or table.table_id}: {header}\n{rendered}"[: max_chars * 2],
                            unit_type="table_row",
                            page=table.page,
                            section=table.sheet or (table.caption or ""),
                            index=row_index,
                            provenance=table.provenance,
                        )
                    )
        return units

    def preview(self, max_chars: int = 4000) -> str:
        return self.text[:max_chars]

    # -- serialisation ------------------------------------------------------
    def to_dict(self) -> dict[str, Any]:
        return {
            "metadata": self.metadata.to_dict(),
            "pages": [p.to_dict() for p in self.pages],
            "sections": [s.to_dict() for s in self.sections],
            "paragraphs": [p.to_dict() for p in self.paragraphs],
            "tables": [t.to_dict() for t in self.tables],
            "figures": [f.to_dict() for f in self.figures],
            "extracted_fields": [f.to_dict() for f in self.extracted_fields],
            "diagnostics": list(self.diagnostics),
            "text": self.text,
        }

    def to_json(self, *, indent: int | None = None) -> str:
        return json.dumps(self.to_dict(), ensure_ascii=False, sort_keys=True, indent=indent, default=str)

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> NormalizedDocument:
        raw_meta = dict(payload.get("metadata") or {})
        known = set(ExtractionMetadata.__dataclass_fields__)
        extra = {k: v for k, v in raw_meta.items() if k not in known}
        extra.update(dict(raw_meta.get("extra") or {}))
        kwargs = {k: v for k, v in raw_meta.items() if k in known and k != "extra"}
        metadata = ExtractionMetadata(**kwargs)
        metadata.extra = extra
        return cls(
            metadata=metadata,
            pages=[Page.from_dict(p) for p in payload.get("pages") or []],
            sections=[Section.from_dict(s) for s in payload.get("sections") or []],
            paragraphs=[Paragraph.from_dict(p) for p in payload.get("paragraphs") or []],
            tables=[Table.from_dict(t) for t in payload.get("tables") or []],
            figures=[Figure.from_dict(f) for f in payload.get("figures") or []],
            extracted_fields=[DataField.from_dict(f) for f in payload.get("extracted_fields") or []],
            diagnostics=list(payload.get("diagnostics") or []),
            text=payload.get("text", "") or "",
        )

    @classmethod
    def from_json(cls, text: str) -> NormalizedDocument:
        return cls.from_dict(json.loads(text))


def metadata_extra(payload: dict[str, Any]) -> dict[str, Any]:
    meta = payload.get("metadata") or {}
    return dict(meta.get("extra") or {})


def structure_digest(document: NormalizedDocument) -> str:
    """Digest over structure only - used to detect "same text, different parsing"."""
    payload = {
        "pages": len(document.pages),
        "paragraphs": len(document.paragraphs),
        "tables": [(t.table_id, t.row_count, t.column_count) for t in document.tables],
        "fields": sorted({f.name for f in document.extracted_fields}),
    }
    return sha256_text(json.dumps(payload, sort_keys=True))


__all__ = [
    "ExtractionMetadata",
    "Figure",
    "NormalizedDocument",
    "Page",
    "Paragraph",
    "SearchUnit",
    "Section",
    "Table",
    "clean_text",
    "structure_digest",
]
