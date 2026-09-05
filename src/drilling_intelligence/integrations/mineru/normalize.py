"""MinerU output -> :class:`NormalizedDocument`.

MinerU produces several artefacts per document; we prefer them in this order
because they carry very different amounts of provenance:

1.  ``{name}_middle.json`` - per-page blocks with bboxes.  Gives page + bbox
    provenance, so a fact can be pointed at "page 27, block 4".
2.  ``{name}_content_list.json`` - flat content list with ``page_idx`` and bbox.
    Less structure, still page-anchored.
3.  ``{name}.md`` - markdown.  Reading order and tables are preserved but page
    numbers are *not*; using it means degraded provenance and that is recorded
    in ``diagnostics``, not hidden.

MinerU also removes headers/footers/page numbers by design.  That is excellent for
retrieval and dangerous for citation, so the count of discarded blocks is kept in
the metadata and surfaced in the UI.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from html.parser import HTMLParser
from pathlib import Path
from typing import Any

from ...core.errors import ExtractionError
from ...core.provenance import Provenance
from ..base import parse_json_loose


@dataclass
class MinerURawOutput:
    """The artefacts MinerU wrote for one input document."""

    middle: dict[str, Any] | None = None
    content_list: Any = None
    markdown: str = ""
    layout_pdf: Path = Path()
    directory: Path = Path()

    @property
    def best(self) -> str:
        if self.middle:
            return "middle.json"
        if self.content_list:
            return "content_list.json"
        return "markdown"


_TABLE_ROW = re.compile(r"<tr[^>]*>(.*?)</tr>", re.IGNORECASE | re.DOTALL)
_TABLE_CELL = re.compile(r"<t[dh][^>]*>(.*?)</t[dh]>", re.IGNORECASE | re.DOTALL)
_TAG = re.compile(r"<[^>]+>")


class _CellText(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.parts: list[str] = []

    def handle_data(self, data: str) -> None:  # pragma: no cover - trivial
        self.parts.append(data)


def strip_html(fragment: str) -> str:
    text = _TAG.sub(" ", fragment or "")
    text = text.replace("&amp;", "&").replace("&lt;", "<").replace("&gt;", ">").replace("&nbsp;", " ")
    return re.sub(r"\s+", " ", text).strip()


def parse_table_html(html: str) -> list[list[str | None]]:
    """Extract a rectangular grid from MinerU's table HTML.

    Rowspan/colspan are expanded so the grid stays rectangular - an ragged table
    silently breaks every downstream column-oriented check.
    """
    if not html:
        return []
    rows: list[list[str | None]] = []
    pending: dict[int, tuple[int, str]] = {}  # column -> (remaining rows, text)
    for row_html in _TABLE_ROW.findall(html):
        row: list[str | None] = []
        cell_attrs: list[str] = []
        for match in re.finditer(r"<t[dh]([^>]*)>(.*?)</t[dh]>", row_html, re.IGNORECASE | re.DOTALL):
            cell_attrs.append(match.group(1))
            row.append(strip_html(match.group(2)))
        # Fill columns occupied by spans from previous rows.
        filled: list[str | None] = []
        index = 0
        while index in pending or row:
            if index in pending:
                remaining, text = pending[index]
                filled.append(text)
                if remaining <= 1:
                    del pending[index]
                else:
                    pending[index] = (remaining - 1, text)
            elif row:
                value = row.pop(0)
                attrs = cell_attrs[len(filled)] if len(cell_attrs) > len(filled) else ""
                rowspan = _attr(attrs, "rowspan")
                colspan = _attr(attrs, "colspan")
                filled.append(value)
                if rowspan and rowspan > 1:
                    pending[index] = (rowspan - 1, value)
                for _ in range(max(0, (colspan or 1) - 1)):
                    filled.append(value)
            index += 1
        rows.append(filled)
    width = max((len(r) for r in rows), default=0)
    return [row + [None] * (width - len(row)) for row in rows] if width else []


def _attr(attrs: str, name: str) -> int | None:
    match = re.search(rf'{name}\s*=\s*"?(\d+)"?', attrs or "", re.IGNORECASE)
    return int(match.group(1)) if match else None


def _spans_text(block: dict[str, Any]) -> tuple[str, list[tuple[float, float, float, float]]]:
    """Join all text spans of a block, collecting their bboxes."""
    parts: list[str] = []
    boxes: list[tuple[float, float, float, float]] = []

    def walk(node: Any) -> None:
        if isinstance(node, dict):
            if node.get("type") == "text" and "content" in node:
                parts.append(str(node.get("content", "")))
                bbox = node.get("bbox")
                if isinstance(bbox, (list, tuple)) and len(bbox) == 4:
                    boxes.append(tuple(float(v) for v in bbox))  # type: ignore[arg-type]
            for key in ("lines", "spans", "blocks", "table_body"):
                if key in node:
                    walk(node[key])
        elif isinstance(node, list):
            for item in node:
                walk(item)

    walk(block)
    text = "".join(parts).strip()
    if not text and isinstance(block.get("lines"), list):
        # Some MinerU versions put the text on line level only.
        for line in block["lines"]:
            if isinstance(line, dict) and line.get("content"):
                text += str(line["content"])
    return text, boxes


def _union(boxes: list[tuple[float, float, float, float]]) -> tuple[float, float, float, float] | None:
    if not boxes:
        return None
    x0 = min(b[0] for b in boxes)
    y0 = min(b[1] for b in boxes)
    x1 = max(b[2] for b in boxes)
    y1 = max(b[3] for b in boxes)
    return (x0, y0, x1, y1)


def normalize_middle_json(payload: dict[str, Any], *, filename: str, document_id: str, version_id: str, sha256: str) -> tuple[Any, list[str]]:
    """middle.json -> NormalizedDocument.  Returns ``(document, diagnostics)``."""
    from ...core.provenance import PdfLocator
    from ...extraction.normalized import (
        ExtractionMetadata,
        Figure,
        NormalizedDocument,
        Page,
        Paragraph,
        Section,
        Table,
        clean_text,
    )

    diagnostics: list[str] = []
    pages = payload.get("pdf_info")
    if not isinstance(pages, list):
        raise ExtractionError("MinerU middle.json has no 'pdf_info' page list", file=filename)

    provenance_parser = f"mineru/{payload.get('_version_name', '?')}"
    meta = ExtractionMetadata(
        filename=filename,
        sha256=sha256,
        mime_type="application/pdf",
        parser="mineru",
        parser_version=str(payload.get("_version_name") or ""),
        engine=f"MinerU {payload.get('_version_name', '?')} backend={payload.get('_backend', '?')}",
        page_count=len(pages),
        extra={"mineru_backend": payload.get("_backend"), "mineru_version": payload.get("_version_name")},
    )
    document = NormalizedDocument(metadata=meta)
    paragraph_index = 0
    char_cursor = 0
    current_section: Section | None = None

    def locate(page_number: int, block_index: int | None, bbox: Any, excerpt: str) -> Provenance:
        return Provenance(
            document_id=document_id,
            document_version_id=version_id or None,
            filename=filename,
            locator=PdfLocator(page=page_number, block=block_index, paragraph=paragraph_index, bbox=bbox),
            parser=provenance_parser,
            excerpt=excerpt[:2000],
            source_sha256=sha256,
            confidence=0.95,
        )

    for page_info in pages:
        if not isinstance(page_info, dict):
            continue
        page_index = int(page_info.get("page_idx", page_info.get("page_no", len(document.pages))) or 0)
        page_number = page_index + 1
        size = page_info.get("page_size") or [0, 0]
        blocks = page_info.get("para_blocks") or page_info.get("preproc_blocks") or []
        page_start = char_cursor
        page_texts: list[str] = []
        for block_index, block in enumerate(blocks):
            if not isinstance(block, dict):
                continue
            kind = str(block.get("type", "text"))
            if kind == "table":
                rows = _table_rows(block)
                if rows:
                    html = _table_html(block)
                    document.tables.append(
                        Table(
                            table_id=f"p{page_number}-table{block_index + 1}",
                            rows=rows,
                            caption=str(block.get("table_caption") or "")[:200],
                            page=page_number,
                            anchor=f"bbox={_union([tuple(b) for b in [block['bbox']]] if isinstance(block.get('bbox'), list) else [])}",
                            provenance=locate(page_number, block_index, _block_bbox(block), _table_text(rows)[:2000]),
                            extra={"source": "mineru", "html_available": bool(html)},
                        )
                    )
                    page_texts.append(_table_text(rows))
                    char_cursor += len(page_texts[-1]) + 2
                continue
            if kind in ("image", "chart"):
                document.figures.append(
                    Figure(
                        figure_id=f"p{page_number}-img{block_index + 1}",
                        page=page_number,
                        kind=kind,
                        caption=str(block.get("img_caption") or "")[:200],
                        bbox=_block_bbox(block),
                        provenance=locate(page_number, block_index, _block_bbox(block), _spans_text(block)[0][:200]),
                        text=_spans_text(block)[0][:4000],
                        extra={"image_path": block.get("image_path", "")},
                    )
                )
                continue
            if kind == "interline_equation":
                text = _spans_text(block)[0]
                document.paragraphs.append(
                    Paragraph(
                        index=paragraph_index,
                        text=f"$${text}$$",
                        page=page_number,
                        block=block_index,
                        style="equation",
                        char_start=char_cursor,
                        char_end=char_cursor + len(text),
                        provenance=locate(page_number, block_index, _block_bbox(block), text),
                    )
                )
                paragraph_index += 1
                char_cursor += len(text) + 2
                page_texts.append(text)
                continue
            text, _boxes = _spans_text(block)
            text = clean_text(text)
            if not text:
                continue
            level = None
            if kind == "title":
                raw_level = block.get("level")
                level = int(raw_level) if isinstance(raw_level, int) and raw_level > 0 else 1
            paragraph = Paragraph(
                index=paragraph_index,
                text=text,
                page=page_number,
                block=block_index,
                heading_level=level,
                style=kind,
                section=current_section.label if current_section else "",
                char_start=char_cursor,
                char_end=char_cursor + len(text),
                provenance=locate(page_number, block_index, _block_bbox(block), text),
            )
            if level:
                number_match = re.match(r"^\s*(\d+(?:\.\d+){0,3})\.?\s+(.{2,120})$", text)
                current_section = Section(
                    heading=text,
                    level=level,
                    page=page_number,
                    char_start=char_cursor,
                    number=number_match.group(1) if number_match else "",
                )
                document.sections.append(current_section)
            else:
                if current_section is not None:
                    current_section.paragraph_indices.append(paragraph_index)
                    current_section.char_end = char_cursor + len(text)
            document.paragraphs.append(paragraph)
            paragraph_index += 1
            char_cursor += len(text) + 2
            page_texts.append(text)

        discarded = page_info.get("discarded_blocks") or []
        if discarded:
            meta.extra.setdefault("discarded_blocks", 0)
            meta.extra["discarded_blocks"] = int(meta.extra.get("discarded_blocks", 0)) + len(discarded)
        document.pages.append(
            Page(
                index=page_number,
                text=clean_text("\n\n".join(page_texts)),
                char_start=page_start,
                char_end=char_cursor,
                width=float(size[0] or 0) if isinstance(size, (list, tuple)) and len(size) > 0 else 0.0,
                height=float(size[1] or 0) if isinstance(size, (list, tuple)) and len(size) > 1 else 0.0,
                block_count=len(blocks),
            )
        )

    if meta.extra.get("discarded_blocks"):
        diagnostics.append(
            f"MinerU discarded {meta.extra['discarded_blocks']} header/footer/page-number block(s); "
            "they are excluded from the text by design - cite the page image if page furniture matters"
        )
    document.text = clean_text("\n\n".join(p.text for p in document.pages))
    return document, diagnostics


def _block_bbox(block: dict[str, Any]) -> tuple[float, float, float, float] | None:
    bbox = block.get("bbox")
    if isinstance(bbox, (list, tuple)) and len(bbox) == 4:
        try:
            return (float(bbox[0]), float(bbox[1]), float(bbox[2]), float(bbox[3]))
        except (TypeError, ValueError):
            return None
    return None


def _table_html(block: dict[str, Any]) -> str:
    for candidate in _iter_dicts(block):
        for key in ("table_html", "html"):
            value = candidate.get(key)
            if isinstance(value, str) and "<" in value:
                return value
        body = candidate.get("table_body")
        if isinstance(body, str) and "<" in body:
            return body
    return ""


def _iter_dicts(node: Any):
    if isinstance(node, dict):
        yield node
        for value in node.values():
            yield from _iter_dicts(value)
    elif isinstance(node, list):
        for item in node:
            yield from _iter_dicts(item)


def _table_rows(block: dict[str, Any]) -> list[list[str | None]]:
    html = _table_html(block)
    if html:
        rows = parse_table_html(html)
        if rows:
            return rows
    # No HTML: rebuild rows from line/span geometry.  Rough, but visible as such.
    rows_by_y: dict[int, list[tuple[float, str]]] = {}
    for line in _iter_dicts(block):
        spans = line.get("spans")
        if not isinstance(spans, list):
            continue
        for span in spans:
            if not isinstance(span, dict) or span.get("type") not in (None, "text"):
                continue
            content = str(span.get("content", "")).strip()
            bbox = span.get("bbox")
            if not content or not isinstance(bbox, (list, tuple)) or len(bbox) != 4:
                continue
            rows_by_y.setdefault(int(float(bbox[1]) / 6.0), []).append((float(bbox[0]), content))
    grid: list[list[str | None]] = []
    for key in sorted(rows_by_y):
        items = sorted(rows_by_y[key], key=lambda item: item[0])
        grid.append([text for _x, text in items])
    return grid


def _table_text(rows: list[list[str | None]]) -> str:
    return "\n".join("\t".join("" if cell is None else str(cell) for cell in row) for row in rows)


def normalize_content_list(payload: Any, *, filename: str, document_id: str, version_id: str, sha256: str) -> tuple[Any, list[str]]:
    """content_list.json (flat) -> NormalizedDocument, page-anchored provenance."""
    from ...core.provenance import PdfLocator
    from ...extraction.normalized import (
        ExtractionMetadata,
        NormalizedDocument,
        Page,
        Paragraph,
        Table,
        clean_text,
    )

    items = payload if isinstance(payload, list) else []
    diagnostics = ["MinerU content_list.json used instead of middle.json (section structure unavailable)"]
    document = NormalizedDocument(
        metadata=ExtractionMetadata(
            filename=filename,
            sha256=sha256,
            parser="mineru",
            engine="MinerU content_list",
            page_count=max((int(item.get("page_idx", 0) or 0) for item in items if isinstance(item, dict)), default=0) + 1,
        )
    )
    pages: dict[int, list[str]] = {}
    index = 0
    for item in items:
        if not isinstance(item, dict):
            continue
        page = int(item.get("page_idx", 0) or 0) + 1
        kind = str(item.get("type", "text"))
        bbox = item.get("bbox")
        boxes = tuple(float(v) for v in bbox) if isinstance(bbox, (list, tuple)) and len(bbox) == 4 else None
        if kind == "table":
            rows = parse_table_html(str(item.get("table_body") or ""))
            if rows:
                document.tables.append(
                    Table(
                        table_id=f"p{page}-table{index + 1}",
                        rows=rows,
                        caption=str(item.get("table_caption") or "")[:200],
                        page=page,
                        provenance=Provenance(
                            document_id=document_id,
                            document_version_id=version_id or None,
                            filename=filename,
                            locator=PdfLocator(page=page, bbox=boxes),  # type: ignore[arg-type]
                            parser="mineru/content_list",
                            excerpt=_table_text(rows)[:2000],
                            source_sha256=sha256,
                        ),
                    )
                )
                pages.setdefault(page, []).append(_table_text(rows))
            index += 1
            continue
        text = clean_text(str(item.get("text") or item.get("caption") or ""))
        if not text:
            continue
        document.paragraphs.append(
            Paragraph(
                index=index,
                text=text,
                page=page,
                heading_level=int(item["text_level"]) if str(item.get("text_level") or "").isdigit() and int(item.get("text_level") or 0) > 0 else None,
                style=kind,
                provenance=Provenance(
                    document_id=document_id,
                    document_version_id=version_id or None,
                    filename=filename,
                    locator=PdfLocator(page=page, paragraph=index, bbox=boxes),  # type: ignore[arg-type]
                    parser="mineru/content_list",
                    excerpt=text[:2000],
                    source_sha256=sha256,
                ),
            )
        )
        pages.setdefault(page, []).append(text)
        index += 1
    for page_number in sorted(pages):
        document.pages.append(Page(index=page_number, text=clean_text("\n\n".join(pages[page_number])), label=f"page {page_number}"))
    document.text = clean_text("\n\n".join(p.text for p in document.pages))
    return document, diagnostics


def normalize_markdown(text: str, *, filename: str, document_id: str, version_id: str, sha256: str) -> tuple[Any, list[str]]:
    """Markdown output -> NormalizedDocument with *line* provenance only.

    No page numbers exist in markdown, so provenance degrades to line ranges.
    The degradation is recorded in diagnostics so a reader never mistakes a
    markdown-derived citation for a page-anchored one.
    """
    from ...core.provenance import TextLocator
    from ...extraction.normalized import (
        ExtractionMetadata,
        NormalizedDocument,
        Page,
        Paragraph,
        Section,
        Table,
        clean_text,
    )

    lines = text.splitlines()
    document = NormalizedDocument(
        metadata=ExtractionMetadata(
            filename=filename,
            sha256=sha256,
            parser="mineru",
            engine="MinerU markdown",
            extra={"provenance_quality": "line-level (no page anchors in markdown output)"},
        )
    )
    paragraph_index = 0
    char_cursor = 0
    block_start: int | None = None
    buffer: list[str] = []
    current_section: Section | None = None

    def flush(end_line: int) -> None:
        nonlocal paragraph_index, char_cursor, buffer, block_start, current_section
        raw = "\n".join(buffer).strip()
        buffer = []
        if not raw or block_start is None:
            block_start = None
            return
        heading = re.match(r"^(#{1,6})\s+(.*)$", raw)
        if heading:
            level = len(heading.group(1))
            document.paragraphs.append(
                Paragraph(
                    index=paragraph_index,
                    text=raw.lstrip("#").strip(),
                    page=1,
                    block=block_start,
                    heading_level=level,
                    style="heading",
                    char_start=char_cursor,
                    char_end=char_cursor + len(raw),
                    provenance=Provenance(
                        document_id=document_id,
                        document_version_id=version_id or None,
                        filename=filename,
                        locator=TextLocator(line_start=block_start, line_end=end_line),
                        parser="mineru/markdown",
                        excerpt=raw[:2000],
                        source_sha256=sha256,
                    ),
                )
            )
            current_section = Section(heading=raw.lstrip("#").strip(), level=level, page=1, char_start=char_cursor)
            document.sections.append(current_section)
            paragraph_index += 1
            char_cursor += len(raw) + 2
            block_start = None
            return
        if _looks_like_markdown_table(raw):
            rows = _markdown_table_rows(raw)
            document.tables.append(
                Table(
                    table_id=f"md-table{len(document.tables) + 1}",
                    rows=rows,
                    page=1,
                    anchor=f"lines {block_start}-{end_line}",
                    provenance=Provenance(
                        document_id=document_id,
                        document_version_id=version_id or None,
                        filename=filename,
                        locator=TextLocator(line_start=block_start, line_end=end_line),
                        parser="mineru/markdown",
                        excerpt=raw[:2000],
                        source_sha256=sha256,
                    ),
                )
            )
            block_start = None
            return
        document.paragraphs.append(
            Paragraph(
                index=paragraph_index,
                text=clean_text(raw),
                page=1,
                block=block_start,
                style="text",
                section=current_section.label if current_section else "",
                char_start=char_cursor,
                char_end=char_cursor + len(raw),
                provenance=Provenance(
                    document_id=document_id,
                    document_version_id=version_id or None,
                    filename=filename,
                    locator=TextLocator(line_start=block_start, line_end=end_line, section=(current_section.label if current_section else None)),
                    parser="mineru/markdown",
                    excerpt=raw[:2000],
                    source_sha256=sha256,
                ),
            )
        )
        if current_section is not None:
            current_section.paragraph_indices.append(paragraph_index)
        paragraph_index += 1
        char_cursor += len(raw) + 2
        block_start = None

    for line_number, line in enumerate(lines, start=1):
        if line.strip():
            if block_start is None:
                block_start = line_number
            buffer.append(line)
        elif block_start is not None:
            flush(line_number - 1)
    if buffer:
        flush(len(lines))

    document.pages.append(Page(index=1, text=clean_text(text), char_start=0, char_end=len(text), block_count=len(document.paragraphs)))
    document.metadata.page_count = 1
    document.text = clean_text("\n\n".join(p.text for p in document.paragraphs))
    diagnostics = [
        "MinerU markdown used: page-level provenance unavailable (line ranges only); prefer middle.json output when present"
    ]
    return document, diagnostics


def _looks_like_markdown_table(block: str) -> bool:
    rows = [line for line in block.splitlines() if line.strip()]
    return len(rows) >= 2 and rows[0].count("|") >= 2 and bool(re.match(r"^\s*\|?[\s:|-]+\|[\s:|-]*$", rows[1]))


def _markdown_table_rows(block: str) -> list[list[str | None]]:
    rows: list[list[str | None]] = []
    for raw_line in block.splitlines():
        line = raw_line.strip()
        if not line or set(line) <= set("-:| "):
            continue
        cells = [cell.strip() for cell in line.strip("|").split("|")]
        rows.append([strip_html(cell) for cell in cells])
    return rows


def load_mineru_outputs(directory: Path, stem: str) -> MinerURawOutput:
    """Collect the artefacts MinerU wrote for one document (version-tolerant)."""
    root = Path(directory)
    middle: dict[str, Any] | None = None
    content_list: Any = None
    markdown = ""
    layout_pdf = Path()
    for candidate in sorted(root.rglob("*")):
        if not candidate.is_file():
            continue
        name = candidate.name.lower()
        if name.endswith("_middle.json") or name == "middle.json":
            middle = _read_json(candidate)
        elif name.endswith("content_list.json"):
            content_list = _read_json(candidate)
        elif candidate.suffix == ".md" and (not stem or candidate.stem.lower().startswith(stem.lower()[:4])):
            markdown = candidate.read_text(encoding="utf-8", errors="replace")
        elif candidate.suffix == ".pdf" and "layout" in name:
            layout_pdf = candidate
    if middle is None and content_list is None and not markdown:
        raise ExtractionError(f"MinerU produced no readable output in {root} for {stem}", directory=str(root))
    return MinerURawOutput(middle=middle, content_list=content_list, markdown=markdown, layout_pdf=layout_pdf, directory=root)


def _read_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8", errors="replace"))
    except json.JSONDecodeError:
        parsed = parse_json_loose(path.read_text(encoding="utf-8", errors="replace"))
        if parsed is None:
            raise ExtractionError(f"MinerU output is not valid JSON: {path}") from None
        return parsed


__all__ = [
    "MinerURawOutput",
    "load_mineru_outputs",
    "normalize_content_list",
    "normalize_markdown",
    "normalize_middle_json",
    "parse_table_html",
    "strip_html",
]
