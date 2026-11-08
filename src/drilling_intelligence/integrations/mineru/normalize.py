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
import math
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
    text = (
        text.replace("&amp;", "&").replace("&lt;", "<").replace("&gt;", ">").replace("&nbsp;", " ")
    )
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
        for match in re.finditer(
            r"<t[dh]([^>]*)>(.*?)</t[dh]>", row_html, re.IGNORECASE | re.DOTALL
        ):
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
            if node.get("type") == "text" and isinstance(node.get("content"), str):
                parts.append(node["content"])
                bbox = _safe_bbox(node.get("bbox"))
                if bbox is not None:
                    boxes.append(bbox)
            for key in ("lines", "spans", "blocks", "table_body", "content"):
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


def _union(
    boxes: list[tuple[float, float, float, float]],
) -> tuple[float, float, float, float] | None:
    if not boxes:
        return None
    x0 = min(b[0] for b in boxes)
    y0 = min(b[1] for b in boxes)
    x1 = max(b[2] for b in boxes)
    y1 = max(b[3] for b in boxes)
    return (x0, y0, x1, y1)


def normalize_middle_json(
    payload: dict[str, Any], *, filename: str, document_id: str, version_id: str, sha256: str
) -> tuple[Any, list[str]]:
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

    if not isinstance(payload, dict):
        raise ExtractionError("MinerU middle.json root must be a JSON object", file=filename)
    if payload.get("schema") == "docvortex.middle":
        return _normalize_docvortex_middle_json(
            payload,
            filename=filename,
            document_id=document_id,
            version_id=version_id,
            sha256=sha256,
        )

    diagnostics: list[str] = []
    pages = payload.get("pdf_info")
    if not isinstance(pages, list):
        raise ExtractionError(
            "MinerU middle.json has neither the legacy 'pdf_info' nor docvortex.middle page list",
            file=filename,
        )

    provenance_parser = f"mineru/{payload.get('_version_name', '?')}"
    meta = ExtractionMetadata(
        filename=filename,
        sha256=sha256,
        mime_type="application/pdf",
        parser="mineru",
        parser_version=str(payload.get("_version_name") or ""),
        engine=f"MinerU {payload.get('_version_name', '?')} backend={payload.get('_backend', '?')}",
        page_count=len(pages),
        extra={
            "mineru_backend": payload.get("_backend"),
            "mineru_version": payload.get("_version_name"),
        },
    )
    document = NormalizedDocument(metadata=meta)
    paragraph_index = 0
    char_cursor = 0
    current_section: Section | None = None
    invalid_bbox_count = 0

    def locate(page_number: int, block_index: int | None, bbox: Any, excerpt: str) -> Provenance:
        return Provenance(
            document_id=document_id,
            document_version_id=version_id or None,
            filename=filename,
            locator=PdfLocator(
                page=page_number, block=block_index, paragraph=paragraph_index, bbox=bbox
            ),
            parser=provenance_parser,
            excerpt=excerpt[:2000],
            source_sha256=sha256,
        )

    for page_position, page_info in enumerate(pages):
        if not isinstance(page_info, dict):
            diagnostics.append(
                f"MinerU middle.json page at index {page_position} was malformed and omitted"
            )
            continue
        page_index = _safe_index(
            page_info.get("page_idx", page_info.get("page_no", len(document.pages))),
            len(document.pages),
            filename=filename,
        )
        page_number = page_index + 1
        size = page_info.get("page_size") or [0, 0]
        blocks = page_info.get("para_blocks") or page_info.get("preproc_blocks") or []
        if not isinstance(blocks, list):
            diagnostics.append(
                f"MinerU page {page_number} had a malformed block list; blocks were omitted"
            )
            blocks = []
        page_start = char_cursor
        page_texts: list[str] = []
        for block_index, block in enumerate(blocks):
            if not isinstance(block, dict):
                diagnostics.append(
                    f"MinerU page {page_number} block {block_index + 1} was malformed and omitted"
                )
                continue
            kind = str(block.get("type", "text"))
            if block.get("bbox") is not None and _block_bbox(block) is None:
                invalid_bbox_count += 1
            if kind == "table":
                rows = _table_rows(block)
                if rows:
                    html = _table_html(block)
                    bbox = _block_bbox(block)
                    document.tables.append(
                        Table(
                            table_id=f"p{page_number}-table{block_index + 1}",
                            rows=rows,
                            caption=str(block.get("table_caption") or "")[:200],
                            page=page_number,
                            anchor=f"bbox={bbox}" if bbox else "",
                            provenance=locate(
                                page_number,
                                block_index,
                                bbox,
                                _table_text(rows)[:2000],
                            ),
                            extra={"source": "mineru", "html_available": bool(html)},
                        )
                    )
                    page_texts.append(_table_text(rows))
                    char_cursor += len(page_texts[-1]) + 2
                else:
                    text, _boxes = _spans_text(block)
                    text = clean_text(text)
                    if text:
                        document.paragraphs.append(
                            Paragraph(
                                index=paragraph_index,
                                text=text,
                                page=page_number,
                                block=block_index,
                                style="table_text_unstructured",
                                char_start=char_cursor,
                                char_end=char_cursor + len(text),
                                provenance=locate(
                                    page_number, block_index, _block_bbox(block), text
                                ),
                            )
                        )
                        paragraph_index += 1
                        page_texts.append(text)
                        char_cursor += len(text) + 2
                    diagnostics.append(
                        f"table block {block_index + 1} on page {page_number} lacked a structured grid; "
                        "available text was retained without inferred rows"
                    )
                continue
            if kind in ("image", "chart"):
                document.figures.append(
                    Figure(
                        figure_id=f"p{page_number}-img{block_index + 1}",
                        page=page_number,
                        kind=kind,
                        caption=str(block.get("img_caption") or "")[:200],
                        bbox=_block_bbox(block),
                        provenance=locate(
                            page_number,
                            block_index,
                            _block_bbox(block),
                            _spans_text(block)[0][:200],
                        ),
                        text=_spans_text(block)[0][:4000],
                        # MinerU image paths point into its transient output tree. Retain only
                        # whether one existed; never publish a path that cleanup will invalidate.
                        extra={"image_available": bool(block.get("image_path"))},
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
        if isinstance(discarded, (list, tuple)):
            if discarded:
                meta.extra.setdefault("discarded_blocks", 0)
                meta.extra["discarded_blocks"] = int(meta.extra.get("discarded_blocks", 0)) + len(
                    discarded
                )
        else:
            diagnostics.append(
                f"MinerU page {page_number} had malformed discarded-block metadata; it was ignored"
            )
        document.pages.append(
            Page(
                index=page_number,
                text=clean_text("\n\n".join(page_texts)),
                char_start=page_start,
                char_end=char_cursor,
                width=_finite_dimension(size[0])
                if isinstance(size, (list, tuple)) and size
                else 0.0,
                height=_finite_dimension(size[1])
                if isinstance(size, (list, tuple)) and len(size) > 1
                else 0.0,
                block_count=len(blocks),
            )
        )

    if invalid_bbox_count:
        diagnostics.append(
            f"MinerU supplied {invalid_bbox_count} invalid bounding box(es); those locators were omitted"
        )
    if meta.extra.get("discarded_blocks"):
        diagnostics.append(
            f"MinerU discarded {meta.extra['discarded_blocks']} header/footer/page-number block(s); "
            "they are excluded from the text by design - cite the page image if page furniture matters"
        )
    document.text = clean_text("\n\n".join(p.text for p in document.pages))
    return document, diagnostics


def _normalize_docvortex_middle_json(
    payload: dict[str, Any], *, filename: str, document_id: str, version_id: str, sha256: str
) -> tuple[Any, list[str]]:
    """MinerU 4.x ``docvortex.middle`` -> the shared, page-anchored normalizer.

    MinerU 4.x changed the outer shape from ``pdf_info/para_blocks`` to
    ``pages/blocks`` and uses normalized fractional bounding boxes. Adapt only
    those structural differences; retain the source text, page/block IDs, and
    exact bbox values rather than inventing legacy coordinate units.
    """
    pages = payload.get("pages")
    if not isinstance(pages, list):
        raise ExtractionError("MinerU docvortex.middle has no 'pages' list", file=filename)

    diagnostics: list[str] = []
    extensions = payload.get("extensions")
    extensions = extensions if isinstance(extensions, dict) else {}
    layout = extensions.get("docvortex_layout")
    layout_pages = layout.get("pages") if isinstance(layout, dict) else []
    layout_by_index: dict[int, dict[str, Any]] = {}
    if isinstance(layout_pages, list):
        for index, item in enumerate(layout_pages):
            if isinstance(item, dict):
                page_index = _safe_index(item.get("page_idx", index), index, filename=filename)
                layout_by_index[page_index] = item

    legacy_pages: list[dict[str, Any]] = []
    invalid_bbox_count = 0
    for page_number, page in enumerate(pages):
        if not isinstance(page, dict):
            diagnostics.append(
                f"MinerU docvortex.middle page at index {page_number} was malformed and omitted"
            )
            continue
        page_index = _safe_index(page.get("page_idx", page_number), page_number, filename=filename)
        layout_page = layout_by_index.get(page_index, {})
        blocks = page.get("blocks")
        if not isinstance(blocks, list):
            diagnostics.append(
                f"MinerU docvortex.middle page {page_index} had a malformed block list; blocks were omitted"
            )
            blocks = []
        normalized_blocks: list[dict[str, Any]] = []
        for block_index, block in enumerate(blocks):
            if not isinstance(block, dict):
                diagnostics.append(
                    f"MinerU docvortex.middle page {page_index} block {block_index + 1} was malformed and omitted"
                )
                continue
            adapted = dict(block)
            if block.get("bbox") is not None:
                bbox = _block_bbox(block, normalized=True)
                if bbox is None:
                    invalid_bbox_count += 1
                    adapted.pop("bbox", None)
                else:
                    adapted["bbox"] = list(bbox)
            if str(block.get("type", "")).lower() == "table":
                html = _table_html(block)
                if html:
                    adapted["table_body"] = html
            normalized_blocks.append(adapted)
        legacy_pages.append(
            {
                "page_idx": page_index,
                "page_size": [layout_page.get("width_pt", 0), layout_page.get("height_pt", 0)],
                "para_blocks": normalized_blocks,
            }
        )

    metadata = payload.get("metadata")
    metadata = metadata if isinstance(metadata, dict) else {}
    producer = metadata.get("producer")
    producer = producer if isinstance(producer, dict) else {}
    mineru_extension = extensions.get("mineru")
    mineru_extension = mineru_extension if isinstance(mineru_extension, dict) else {}
    adapted_payload = {
        "pdf_info": legacy_pages,
        "_version_name": producer.get("version", ""),
        "_backend": mineru_extension.get("tier", ""),
    }
    document, legacy_diagnostics = normalize_middle_json(
        adapted_payload,
        filename=filename,
        document_id=document_id,
        version_id=version_id,
        sha256=sha256,
    )
    diagnostics.extend(legacy_diagnostics)
    document.metadata.extra["mineru_schema"] = str(payload.get("schema_version") or "2.0")
    document.metadata.extra["mineru_tier"] = mineru_extension.get("tier", "")
    document.metadata.extra["mineru_bbox_units"] = "normalized fractions (0-1), source schema"
    diagnostics.append(
        "MinerU 4.x docvortex.middle output used; source bounding-box units are normalized fractions"
    )
    if invalid_bbox_count:
        diagnostics.append(
            f"MinerU supplied {invalid_bbox_count} invalid normalized bounding box(es); those locators were omitted"
        )
    return document, diagnostics


def _safe_index(value: Any, default: int, *, filename: str) -> int:
    if value is None:
        return default
    if isinstance(value, bool):
        raise ExtractionError("MinerU page index must be a non-negative integer", file=filename)
    try:
        parsed = int(value)
    except (TypeError, ValueError, OverflowError):
        raise ExtractionError(
            "MinerU page index must be a non-negative integer", file=filename
        ) from None
    if parsed < 0 or str(value).strip() not in {str(parsed), f"{parsed}.0"}:
        raise ExtractionError("MinerU page index must be a non-negative integer", file=filename)
    return parsed


def _safe_bbox(value: Any, *, normalized: bool = False) -> tuple[float, float, float, float] | None:
    if (
        not isinstance(value, (list, tuple))
        or len(value) != 4
        or any(isinstance(item, bool) for item in value)
    ):
        return None
    try:
        values = tuple(float(item) for item in value)
    except (TypeError, ValueError, OverflowError):
        return None
    if not all(math.isfinite(item) for item in values):
        return None
    x0, y0, x1, y1 = values
    if x1 < x0 or y1 < y0:
        return None
    if normalized and not all(0.0 <= item <= 1.0 for item in values):
        return None
    return values  # type: ignore[return-value]


def _block_bbox(
    block: dict[str, Any], *, normalized: bool = False
) -> tuple[float, float, float, float] | None:
    return _safe_bbox(block.get("bbox"), normalized=normalized)


def _finite_dimension(value: Any) -> float:
    try:
        parsed = float(value or 0)
    except (TypeError, ValueError, OverflowError):
        return 0.0
    return parsed if math.isfinite(parsed) and parsed >= 0 else 0.0


def _table_html(block: dict[str, Any]) -> str:
    for candidate in _iter_dicts(block):
        for key in ("table_html", "html"):
            value = candidate.get(key)
            if isinstance(value, str) and "<" in value:
                return value
        body = candidate.get("table_body")
        if isinstance(body, str) and "<" in body:
            return body
        if candidate.get("type") == "table_body":
            content = candidate.get("content")
            if isinstance(content, str) and "<" in content:
                return content
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
    """Return a structured grid only when MinerU supplied a table representation.

    Grouping spans by an invented coordinate scale can reorder or merge operator
    values. If no HTML/grid is supplied, the caller retains available text as an
    uninterpreted block and emits a diagnostic instead of fabricating rows.
    """
    html = _table_html(block)
    return parse_table_html(html) if html else []


def _table_text(rows: list[list[str | None]]) -> str:
    return "\n".join("\t".join("" if cell is None else str(cell) for cell in row) for row in rows)


def normalize_content_list(
    payload: Any, *, filename: str, document_id: str, version_id: str, sha256: str
) -> tuple[Any, list[str]]:
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

    if isinstance(payload, list):
        items = payload
        diagnostics = [
            "MinerU content_list.json used instead of middle.json (section structure unavailable)"
        ]
    else:
        items = []
        diagnostics = ["MinerU content_list.json root was not an array; no items were retained"]
    page_indices = [
        _safe_index(item.get("page_idx", 0), 0, filename=filename)
        for item in items
        if isinstance(item, dict)
    ]
    document = NormalizedDocument(
        metadata=ExtractionMetadata(
            filename=filename,
            sha256=sha256,
            parser="mineru",
            engine="MinerU content_list",
            page_count=max(page_indices, default=0) + 1,
        )
    )
    pages: dict[int, list[str]] = {}
    index = 0
    for item_index, item in enumerate(items):
        if not isinstance(item, dict):
            diagnostics.append(
                f"MinerU content_list item {item_index + 1} was malformed and omitted"
            )
            continue
        page = _safe_index(item.get("page_idx", 0), 0, filename=filename) + 1
        kind = str(item.get("type", "text"))
        boxes = _safe_bbox(item.get("bbox"))
        if kind == "table":
            raw_table = item.get("table_body")
            rows = parse_table_html(raw_table if isinstance(raw_table, str) else "")
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
            else:
                raw_text = item.get("text")
                text = clean_text(raw_text) if isinstance(raw_text, str) else ""
                if not text and isinstance(raw_table, str):
                    text = clean_text(strip_html(raw_table))
                if not text and isinstance(item.get("table_caption"), str):
                    text = clean_text(item["table_caption"])
                if text:
                    document.paragraphs.append(
                        Paragraph(
                            index=index,
                            text=text,
                            page=page,
                            block=index,
                            style="table_text_unstructured",
                            provenance=Provenance(
                                document_id=document_id,
                                document_version_id=version_id or None,
                                filename=filename,
                                locator=PdfLocator(page=page, paragraph=index, bbox=boxes),
                                parser="mineru/content_list",
                                excerpt=text[:2000],
                                source_sha256=sha256,
                            ),
                        )
                    )
                    pages.setdefault(page, []).append(text)
                    diagnostics.append(
                        f"MinerU content_list table item {index + 1} had no parseable grid; "
                        "available text was retained without inferred rows"
                    )
                else:
                    diagnostics.append(
                        f"MinerU content_list table item {index + 1} was empty/unreadable and omitted"
                    )
            index += 1
            continue
        raw_text = item.get("text")
        if not isinstance(raw_text, str):
            raw_text = item.get("caption")
        text = clean_text(raw_text) if isinstance(raw_text, str) else ""
        if not text:
            continue
        document.paragraphs.append(
            Paragraph(
                index=index,
                text=text,
                page=page,
                heading_level=int(item["text_level"])
                if str(item.get("text_level") or "").isdigit()
                and int(item.get("text_level") or 0) > 0
                else None,
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
        document.pages.append(
            Page(
                index=page_number,
                text=clean_text("\n\n".join(pages[page_number])),
                label=f"page {page_number}",
            )
        )
    document.text = clean_text("\n\n".join(p.text for p in document.pages))
    return document, diagnostics


def normalize_markdown(
    text: str, *, filename: str, document_id: str, version_id: str, sha256: str
) -> tuple[Any, list[str]]:
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
            current_section = Section(
                heading=raw.lstrip("#").strip(), level=level, page=1, char_start=char_cursor
            )
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
                    locator=TextLocator(
                        line_start=block_start,
                        line_end=end_line,
                        section=(current_section.label if current_section else None),
                    ),
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

    document.pages.append(
        Page(
            index=1,
            text=clean_text(text),
            char_start=0,
            char_end=len(text),
            block_count=len(document.paragraphs),
        )
    )
    document.metadata.page_count = 1
    document.text = clean_text("\n\n".join(p.text for p in document.paragraphs))
    diagnostics = [
        "MinerU markdown used: page-level provenance unavailable (line ranges only); prefer middle.json output when present"
    ]
    return document, diagnostics


def _looks_like_markdown_table(block: str) -> bool:
    rows = [line for line in block.splitlines() if line.strip()]
    return (
        len(rows) >= 2
        and rows[0].count("|") >= 2
        and bool(re.match(r"^\s*\|?[\s:|-]+\|[\s:|-]*$", rows[1]))
    )


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
    for candidate in sorted(root.rglob("*")):
        if not candidate.is_file():
            continue
        name = candidate.name.lower()
        if name.endswith("_middle.json") or name == "middle.json":
            middle = _read_json(candidate)
        elif name.endswith("content_list.json"):
            content_list = _read_json(candidate)
        elif candidate.suffix == ".md" and (
            not stem or candidate.stem.lower().startswith(stem.lower()[:4])
        ):
            markdown = candidate.read_text(encoding="utf-8", errors="replace")
    if middle is None and content_list is None and not markdown:
        raise ExtractionError(
            f"MinerU produced no readable output in {root} for {stem}", directory=str(root)
        )
    return MinerURawOutput(
        middle=middle,
        content_list=content_list,
        markdown=markdown,
    )


def _read_json(path: Path) -> Any:
    raw = path.read_text(encoding="utf-8", errors="replace")
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        parsed = parse_json_loose(raw)
        if parsed is None:
            raise ExtractionError(f"MinerU output is not valid JSON: {path}") from None
        # The loose parser salvages JSON out of noise.  What it must not do is
        # change the shape of the payload: a content list that becomes an
        # object is a document that lost most of its text without an error.
        if raw.lstrip().startswith("[") and not isinstance(parsed, list):
            raise ExtractionError(
                f"MinerU output is not a valid JSON array and cannot be salvaged "
                f"without losing items: {path}"
            ) from None
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
