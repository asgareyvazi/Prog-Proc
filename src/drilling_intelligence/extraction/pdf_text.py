"""PDF text-layer extractor built on PyMuPDF.

PyMuPDF is the proven component here (docs/DEPENDENCIES.md): it is fast, has no
model download, and gives spans with bounding boxes - which is what makes
per-block provenance possible.  This extractor is *not* a MinerU replacement:
it does not OCR and does not understand layout, so the router hands it only
PDFs that have a usable text layer, and records a diagnostic when a page looks
scanned.
"""

from __future__ import annotations

import re
from typing import Any

from ..__init__ import EXTRACTION_ENGINE_VERSION
from ..core.errors import ExtractionError
from .interfaces import DocumentComplexity, ExtractionContext, ProvenanceBuilder
from .normalized import (
    ExtractionMetadata,
    NormalizedDocument,
    Page,
    Paragraph,
    Section,
    Table,
    clean_text,
)

#: Headings are inferred from font size: a span at least this much larger than the
#: body mode is a heading.  Deliberately conservative - a wrong heading is worse
#: than no heading because it changes section-level provenance.
_HEADING_SIZE_RATIO = 1.12
_SECTION_NUMBER = re.compile(r"^\s*(\d+(?:\.\d+){0,3})\.?\s+(?P<title>.{2,120})$")
_TABLE_CAPTION = re.compile(r"(?i)\b(table|tab\.?)\s*(\d+[a-z]?)\b[:.]?\s*(.{0,120})")


class PdfTextExtractor:
    """Text-layer PDF extractor with per-block provenance."""

    name = "pdf_text"
    version = EXTRACTION_ENGINE_VERSION
    description = "PyMuPDF text layer + table detection (no OCR)."

    # -- DocumentExtractor protocol ----------------------------------------
    def supports(self, context: ExtractionContext) -> tuple[bool, str]:
        if context.extension.lower() not in (".pdf",):
            return False, f"extension {context.extension} is not a PDF"
        return True, "PDF: text-layer extraction available"

    def probe(self, context: ExtractionContext) -> DocumentComplexity:
        complexity = DocumentComplexity()
        try:
            import pymupdf

            with pymupdf.open(context.path) as doc:
                complexity.pages = doc.page_count
                complexity.encrypted = bool(doc.is_encrypted)
                table_count = 0
                text_chars = 0
                scanned_pages = 0
                for page in doc:
                    page_text = page.get_text() or ""
                    text_chars += len(page_text)
                    try:
                        table_count += len(page.find_tables().tables)
                    except Exception:  # noqa: BLE001 - a broken table must not kill the probe
                        pass
                    if len(page_text.strip()) < 25:
                        scanned_pages += 1
                complexity.text_chars_per_page = text_chars / max(1, doc.page_count)
                complexity.table_count = table_count
                complexity.has_text_layer = text_chars > 0
                complexity.is_scanned = doc.page_count > 0 and scanned_pages >= max(1, int(doc.page_count * 0.6))
                if complexity.is_scanned:
                    complexity.reasons.append(f"{scanned_pages}/{doc.page_count} pages have no usable text layer")
                if table_count:
                    complexity.reasons.append(f"{table_count} table(s) detected")
                if complexity.text_chars_per_page < 400:
                    complexity.reasons.append("low text density (complex layout or images)")
        except Exception as exc:  # noqa: BLE001 - probing must never raise
            complexity.reasons.append(f"probe failed: {type(exc).__name__}: {exc}")
        return complexity

    def extract(self, context: ExtractionContext, provenance: ProvenanceBuilder) -> NormalizedDocument:
        try:
            import pymupdf
        except Exception as exc:  # pragma: no cover - dependency failure
            raise ExtractionError(f"PyMuPDF is not available: {exc}") from exc

        document = NormalizedDocument(
            metadata=ExtractionMetadata(
                filename=context.filename,
                path=str(context.path),
                sha256=context.sha256,
                extension=context.extension,
                mime_type=context.mime_type or "application/pdf",
                size_bytes=context.size_bytes,
                engine=f"PyMuPDF {getattr(pymupdf, '__version__', '?')}",
            )
        )
        max_pages = int(context.option("pdf_max_pages", 4000) or 4000)
        want_tables = bool(context.option("pdf_extract_tables", True))

        with pymupdf.open(context.path) as doc:
            document.metadata.page_count = doc.page_count
            document.metadata.extra["pdf_metadata"] = _pdf_metadata(doc)
            document.metadata.extra["toc_entries"] = len(doc.get_toc())
            if doc.is_encrypted:
                document.diagnostics.append("PDF is encrypted; text extraction may be incomplete")
            if doc.page_count > max_pages:
                document.diagnostics.append(f"page count {doc.page_count} truncated to configured max {max_pages}")

            char_cursor = 0
            paragraph_index = 0
            body_size = self._body_font_size(doc, max_pages)
            current_section: Section | None = None
            for page_number in range(min(doc.page_count, max_pages)):
                page = doc[page_number]
                page_text_parts: list[str] = []
                blocks = _ordered_blocks(page)
                document.pages.append(
                    Page(
                        index=page_number + 1,
                        text="",
                        char_start=char_cursor,
                        char_end=char_cursor,
                        width=float(page.rect.width),
                        height=float(page.rect.height),
                        block_count=len(blocks),
                    )
                )
                if not blocks:
                    document.diagnostics.append(f"page {page_number + 1}: no extractable text (image/scan?)")
                for block_index, block in enumerate(blocks):
                    text = clean_text(block["text"])
                    if not text:
                        continue
                    locator_bbox = tuple(block["bbox"]) if block.get("bbox") else None
                    paragraph = Paragraph(
                        index=paragraph_index,
                        text=text,
                        page=page_number + 1,
                        block=block_index,
                        char_start=char_cursor,
                        char_end=char_cursor + len(text),
                        provenance=provenance.pdf(
                            page=page_number + 1,
                            block=block_index,
                            paragraph=paragraph_index,
                            bbox=locator_bbox,  # type: ignore[arg-type]
                            excerpt=text[:2000],
                            confidence=block.get("confidence"),
                        ),
                    )
                    section = _heading_to_section(text, body_size, block.get("max_size", 0.0))
                    if section is not None:
                        paragraph.heading_level = section[0]
                        paragraph.style = "heading"
                        current_section = Section(heading=section[1], level=section[0], page=page_number + 1, char_start=char_cursor, number=section[2])
                        document.sections.append(current_section)
                    paragraph.section = current_section.label if current_section else ""
                    document.paragraphs.append(paragraph)
                    if current_section is not None:
                        current_section.paragraph_indices.append(paragraph_index)
                    paragraph_index += 1
                    page_text_parts.append(text)
                    char_cursor += len(text) + 2
                page_obj = document.pages[-1]
                page_obj.text = clean_text("\n\n".join(page_text_parts))
                page_obj.char_end = char_cursor
                if current_section is not None:
                    current_section.char_end = char_cursor

            if want_tables:
                self._extract_tables(doc, provenance, document, paragraph_index, page_limit=min(doc.page_count, max_pages))

        # Table content is part of the searchable/scannable text, exactly as in the
        # DOCX and XLSX readers: a DDR table is where the numbers live.
        document.text = clean_text("\n\n".join([p.text for p in document.paragraphs if p.text] + [t.text() for t in document.tables]))
        if not document.paragraphs:
            document.diagnostics.append("no text recovered: PDF appears to be a scan; MinerU/OCR required")
        return document

    # -- internals ----------------------------------------------------------
    @staticmethod
    def _body_font_size(doc: Any, max_pages: int) -> float:
        """Mode of span font sizes over the first pages = the body size."""
        try:
            sizes: list[float] = []
            # Document.pages(a, b) is a page *range*, not a number of pages: asking it
            # for "6 pages" samples nothing on a short PDF, which silently disables
            # every size-based heading decision.  Index the pages we want instead.
            sample = min(6, max_pages, int(getattr(doc, "page_count", 0) or 0))
            for index in range(sample):
                page = doc[index]
                data = page.get_text("dict") or {}
                for block in data.get("blocks", []):
                    for span in block.get("lines", [{}])[0].get("spans", []) if block.get("lines") else []:
                        if (span.get("text") or "").strip():
                            sizes.append(round(float(span.get("size", 0.0)), 1))
            if not sizes:
                return 0.0
            counts: dict[float, int] = {}
            for size in sizes:
                counts[size] = counts.get(size, 0) + 1
            return max(counts, key=lambda k: (counts[k], k))
        except Exception:  # noqa: BLE001 - heading detection is best-effort
            return 0.0

    def _extract_tables(
        self,
        doc: Any,
        provenance: ProvenanceBuilder,
        document: NormalizedDocument,
        start_index: int,
        *,
        page_limit: int | None = None,
    ) -> None:
        """Find and read ruled/whitespace tables, one paragraph per row for scanning.

        ``page_limit`` keeps a 900-page annex from costing more than the text pass;
        the truncated count is recorded as a diagnostic rather than hidden.
        """
        limit = doc.page_count if page_limit is None else min(doc.page_count, max(1, int(page_limit)))
        if limit < doc.page_count:
            document.diagnostics.append(f"table scan limited to the first {limit} of {doc.page_count} pages (pdf_max_pages)")
        table_index = 0
        for page_number in range(limit):
            page = doc[page_number]
            try:
                finder = page.find_tables()
            except Exception as exc:  # noqa: BLE001
                document.diagnostics.append(f"table detection failed on page {page_number + 1}: {type(exc).__name__}")
                continue
            for table_number, table in enumerate(finder.tables):
                try:
                    rows = [[None if cell is None else str(cell).strip() for cell in row] for row in table.extract()]
                except Exception as exc:  # noqa: BLE001
                    document.diagnostics.append(f"table extraction failed (page {page_number + 1}, table {table_number}): {exc}")
                    continue
                rows = [row for row in rows if any(cell not in (None, "") for cell in row)]
                if len(rows) < 2:
                    continue
                bbox = tuple(table.bbox) if table.bbox else None
                anchor = f"p{page_number + 1}t{table_number}"
                excerpt = "\n".join("\t".join("" if c is None else c for c in row) for row in rows[:6])
                document.tables.append(
                    Table(
                        table_id=f"p{page_number + 1}-table{table_number + 1}",
                        rows=rows,
                        caption=_table_caption(document, page_number + 1, table_number),
                        page=page_number + 1,
                        anchor=anchor,
                        provenance=provenance.pdf(page=page_number + 1, table=table_number, bbox=bbox, excerpt=excerpt[:2000]),
                        extra={"row_count": len(rows), "col_count": len(rows[0]) if rows else 0},
                    )
                )
                table_index += 1
        document.metadata.extra["table_count"] = table_index


# --------------------------------------------------------------------------- helpers
def _ordered_blocks(page: Any) -> list[dict[str, Any]]:
    """Text blocks in reading order (top-to-bottom, then left-to-right).

    PyMuPDF gives blocks in content-stream order, which for a two-column DDR is
    wrong.  Sorting by (y-band, x) restores a sane order for reports; column
    detection is MinerU's job, and the router records when it is needed.
    """
    raw = []
    data = page.get_text("dict") or {}
    for index, block in enumerate(data.get("blocks", [])):
        if block.get("type") != 0:
            continue
        text_parts: list[str] = []
        max_size = 0.0
        for line in block.get("lines", []):
            line_text = "".join(str(span.get("text", "")) for span in line.get("spans", []))
            text_parts.append(line_text)
            for span in line.get("spans", []):
                max_size = max(max_size, float(span.get("size", 0.0) or 0.0))
        bbox = block.get("bbox") or (0, 0, 0, 0)
        raw.append(
            {
                "index": index,
                "text": "\n".join(text_parts),
                "bbox": tuple(float(v) for v in bbox),
                "max_size": max_size,
            }
        )
    if not raw:
        # Fall back to the simple block API (covers unusual structures).
        for index, block in enumerate(page.get_text("blocks")):
            if int(block[6] or 0) != 0:
                continue
            raw.append({"index": index, "text": str(block[4]), "bbox": (block[0], block[1], block[2], block[3]), "max_size": 0.0})
    raw.sort(key=lambda item: (round(item["bbox"][1] / 4.0), item["bbox"][0]))
    return raw


def _heading_to_section(text: str, body_size: float, block_max_size: float) -> tuple[int, str, str] | None:
    """Detect a heading: numbered and/or larger than the body text."""
    stripped = text.strip()
    if not stripped or len(stripped) > 160 or "\n" in stripped:
        return None
    match = _SECTION_NUMBER.match(stripped)
    larger = bool(body_size) and block_max_size >= body_size * _HEADING_SIZE_RATIO
    if not match and not larger:
        return None
    number = (match.group(1) if match else "") or ""
    title = (match.group("title") if match else stripped).strip()
    level = (number.count(".") + 1) if number else (1 if larger else 2)
    return min(level, 6), title[:120], number


def _table_caption(document: NormalizedDocument, page: int, table_number: int) -> str:
    for paragraph in document.paragraphs:
        if paragraph.page == page and paragraph.index and _TABLE_CAPTION.search(paragraph.text):
            match = _TABLE_CAPTION.search(paragraph.text)
            if match:
                return f"{match.group(1).title()} {match.group(2)} {match.group(3)}".strip()
    return f"Table {page}-{table_number + 1}"


def _pdf_metadata(doc: Any) -> dict[str, str]:
    try:
        meta = dict(doc.metadata or {})
    except Exception:  # noqa: BLE001
        return {}
    return {str(k): str(v) for k, v in meta.items() if v not in (None, "")}


__all__ = ["PdfTextExtractor"]
