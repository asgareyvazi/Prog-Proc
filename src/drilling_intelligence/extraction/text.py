"""Plain text / markdown / CSV extractor.

Small but load-bearing: procedures, standards excerpts, lessons learned and
exported DDR text all arrive this way, and CSV/TSV is how a lot of time and cost
data leaves a spreadsheet.  Line-based provenance (``Lines 42-58``) keeps every
hit verifiable against the file with nothing but a text editor.
"""

from __future__ import annotations

import csv
import io
import re

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

_MD_HEADING = re.compile(r"^(?P<hashes>#{1,6})\s+(?P<title>.+?)\s*$")
_NUMBERED_HEADING = re.compile(
    r"^\s*(?P<number>\d+(?:\.\d+){0,3})\.?\s+(?P<title>[A-Z][^\n]{1,120})$"
)
UNDERLINE_HEADING = re.compile(
    r"^(?P<title>[A-Za-z0-9 ()/\"'\-.,&]{3,120})\n(?P<underline>[=\-~^]{3,})$"
)


class TextExtractor:
    """Text/markdown/CSV reader with line-range provenance."""

    name = "text"
    version = EXTRACTION_ENGINE_VERSION
    description = "Plain text, markdown and CSV/TSV (UTF-8 with detection of CRLF/BOM)."

    def supports(self, context: ExtractionContext) -> tuple[bool, str]:
        extension = context.extension.lower()
        if extension in (".txt", ".md", ".markdown", ".log", ".csv", ".tsv"):
            return True, f"text-like file ({extension})"
        return False, f"extension {context.extension} is not text-like"

    def probe(self, context: ExtractionContext) -> DocumentComplexity:
        complexity = DocumentComplexity(has_text_layer=True)
        try:
            text = _read_text(context.path, 200_000)
            lines = text.splitlines()
            complexity.pages = max(1, len(lines) // 45)
            complexity.text_chars_per_page = len(text) / complexity.pages
            if context.extension.lower() in (".csv", ".tsv"):
                complexity.table_count = 1
                complexity.reasons.append(f"delimited data ({len(lines)} lines)")
            if any(_MD_HEADING.match(line) for line in lines[:2000]):
                complexity.reasons.append("markdown headings present")
        except Exception as exc:  # noqa: BLE001
            complexity.reasons.append(f"probe failed: {type(exc).__name__}: {exc}")
        return complexity

    def extract(
        self, context: ExtractionContext, provenance: ProvenanceBuilder
    ) -> NormalizedDocument:
        max_bytes = int(context.option("text_max_bytes", 8 * 1024 * 1024) or 8 * 1024 * 1024)
        try:
            text, truncated = _read_text_full(context.path, max_bytes)
        except Exception as exc:
            raise ExtractionError(
                f"Cannot read {context.filename}: {type(exc).__name__}: {exc}"
            ) from exc
        if truncated:
            text += "\n\n[truncated at configured text_max_bytes]"

        document = NormalizedDocument(
            metadata=ExtractionMetadata(
                filename=context.filename,
                path=str(context.path),
                sha256=context.sha256,
                extension=context.extension,
                mime_type=context.mime_type or "text/plain",
                size_bytes=context.size_bytes,
                engine="builtin text/csv reader",
            )
        )

        if context.extension.lower() in (".csv", ".tsv"):
            self._extract_delimited(document, text, context, provenance)
        else:
            self._extract_lines(document, text, provenance)

        document.text = clean_text(
            "\n\n".join(
                [p.text for p in document.paragraphs if p.text]
                + [t.text() for t in document.tables]
            )
            or text
        )
        document.metadata.page_count = len(document.pages) or 1
        return document

    # ------------------------------------------------------------------ lines
    def _extract_lines(
        self, document: NormalizedDocument, text: str, provenance: ProvenanceBuilder
    ) -> None:
        lines = text.splitlines()
        blocks: list[tuple[int, int, list[str]]] = []
        start = 1
        buffer: list[str] = []
        for line_number, line in enumerate(lines, start=1):
            if line.strip():
                buffer.append(line)
                continue
            if buffer:
                blocks.append((start, line_number - 1, buffer))
                buffer = []
            start = line_number + 1
        if buffer:
            blocks.append((start, len(lines), buffer))

        char_cursor = 0
        index = 0
        page_lines = 45
        current_section: Section | None = None
        paragraph_index_for_section: dict[int, list[int]] = {}
        for block_start, block_end, block_lines in blocks:
            raw = "\n".join(block_lines)
            heading = _block_heading(raw)
            page = (block_start - 1) // page_lines + 1
            while len(document.pages) < page:
                document.pages.append(
                    Page(
                        index=len(document.pages) + 1,
                        text="",
                        label=f"Lines {(len(document.pages)) * page_lines + 1}",
                    )
                )
            if heading is not None:
                paragraph = Paragraph(
                    index=index,
                    text=raw.strip(),
                    page=page,
                    block=block_start,
                    heading_level=heading[0],
                    style="heading",
                    char_start=char_cursor,
                    char_end=char_cursor + len(raw),
                    provenance=provenance.text(
                        line_start=block_start, line_end=block_end, excerpt=raw[:2000]
                    ),
                )
                current_section = Section(
                    heading=heading[1],
                    level=heading[0],
                    page=page,
                    number=heading[2],
                    char_start=char_cursor,
                )
                document.sections.append(current_section)
                document.paragraphs.append(paragraph)
                index += 1
                char_cursor += len(raw) + 2
                continue
            paragraph = Paragraph(
                index=index,
                text=clean_text(raw),
                page=page,
                block=block_start,
                section=current_section.label if current_section else "",
                char_start=char_cursor,
                char_end=char_cursor + len(raw),
                provenance=provenance.text(
                    line_start=block_start,
                    line_end=block_end,
                    section=(current_section.label if current_section else None),
                    excerpt=raw[:2000],
                ),
            )
            document.paragraphs.append(paragraph)
            if current_section is not None:
                current_section.paragraph_indices.append(index)
                current_section.char_end = char_cursor + len(raw)
                paragraph_index_for_section.setdefault(current_section.char_start, []).append(index)
            index += 1
            char_cursor += len(raw) + 2
        for page_obj in document.pages:
            first = (page_obj.index - 1) * page_lines
            page_obj.text = clean_text("\n\n".join(lines[first : first + page_lines]))
            page_obj.char_end = sum(len(line) + 1 for line in lines[first : first + page_lines])

    # --------------------------------------------------------------- delimited
    def _extract_delimited(
        self,
        document: NormalizedDocument,
        text: str,
        context: ExtractionContext,
        provenance: ProvenanceBuilder,
    ) -> None:
        delimiter = "\t" if context.extension.lower() == ".tsv" else ","
        sample = "\n".join(text.splitlines()[:5])
        try:
            dialect = csv.Sniffer().sniff(sample, delimiters=",;\t|")
            delimiter = dialect.delimiter
        except csv.Error:
            pass
        reader = csv.reader(io.StringIO(text), delimiter=delimiter)
        rows = [list(row) for row in reader]
        rows = [row for row in rows if any((cell or "").strip() for cell in row)]
        if not rows:
            document.diagnostics.append("delimited file contained no data rows")
            return
        document.pages.append(Page(index=1, text="", label="Table"))
        document.tables.append(
            Table(
                table_id="csv1",
                rows=rows,
                caption=f"{document.metadata.filename} data",
                page=1,
                anchor=f"rows 1-{len(rows)}",
                provenance=provenance.text(
                    line_start=1,
                    line_end=len(rows),
                    excerpt="\n".join("\t".join(r) for r in rows[:8])[:2000],
                ),
                extra={"delimiter": delimiter},
            )
        )


def _block_heading(raw: str) -> tuple[int, str, str] | None:
    """Markdown/numbered/underlined heading for a text block (single line only)."""
    lines = [line for line in raw.splitlines() if line.strip()]
    if len(lines) != 1:
        return None
    line = lines[0].strip()
    markdown = _MD_HEADING.match(line)
    if markdown:
        return len(markdown.group("hashes")), markdown.group("title").strip(), ""
    numbered = _NUMBERED_HEADING.match(line)
    if numbered:
        return (
            numbered.group("number").count(".") + 1,
            numbered.group("title").strip(),
            numbered.group("number"),
        )
    return None


def _read_text(path: object, limit: int) -> str:
    from pathlib import Path

    with Path(path).open("rb") as handle:
        payload = handle.read(limit)
    return _decode(payload)


def _read_text_full(path: object, limit: int) -> tuple[str, bool]:
    from pathlib import Path

    with Path(path).open("rb") as handle:
        payload = handle.read(limit + 1)
    truncated = len(payload) > limit
    return _decode(payload[:limit]), truncated


def _decode(payload: bytes) -> str:
    if payload.startswith(b"\xff\xfe") or payload.startswith(b"\xfe\xff"):
        return payload.decode("utf-16", errors="replace")
    try:
        return payload.decode("utf-8")
    except UnicodeDecodeError:
        # Drilling reports exported from Windows tooling are frequently cp1252.
        return payload.decode("cp1252", errors="replace")


__all__ = ["UNDERLINE_HEADING", "TextExtractor"]
