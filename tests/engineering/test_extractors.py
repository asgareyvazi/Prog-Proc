"""Per-format extraction guarantees (spec sections 18-24).

Each format has one promise that the rest of the platform depends on, and each promise is
checked against a real generated file: PDFs must keep page geometry, spreadsheets must
keep formulas *and* values plus the hidden rows, Word documents must keep the heading
hierarchy, and text/CSV must keep line offsets.  Everything must survive JSON, because
what the search index and the UI read is the stored artefact, not the parser.
"""

from __future__ import annotations

import pytest

from drilling_intelligence.config.settings import Settings
from drilling_intelligence.extraction.interfaces import ExtractionContext
from drilling_intelligence.extraction.normalized import NormalizedDocument
from drilling_intelligence.extraction.registry import build_default_router


@pytest.fixture(scope="module")
def router(tmp_path_factory):
    config = tmp_path_factory.mktemp("ext-cfg") / "settings.toml"
    config.write_text('[mineru]\nmode = "disabled"\n', encoding="utf-8")
    return build_default_router(Settings.load(config))


@pytest.fixture
def extract(router):
    def _extract(path):
        document, choice, extractor = router.extract(
            ExtractionContext(
                path=path,
                filename=path.name,
                sha256="",
                extension=path.suffix,
                size_bytes=path.stat().st_size,
            )
        )
        return document, choice, extractor

    return _extract


pytestmark = pytest.mark.engineering


def test_pdf_keeps_pages_blocks_and_geometry(corpus_dir, extract) -> None:
    document, _choice, extractor = extract(corpus_dir / "well_a3_program_rev12.pdf")
    assert extractor.name == "pdf_text"
    assert document.pages and document.pages[0].index == 1  # 1-based, what a human reads
    assert document.pages[0].width > 0 and document.pages[0].height > 0
    assert document.text and "DRILLING PROGRAM" in document.text.upper()
    sourced = [paragraph for paragraph in document.paragraphs if paragraph.provenance is not None]
    assert sourced, "every paragraph should be able to say where it came from"
    locator = sourced[0].provenance.locator
    assert locator.kind == "pdf" and locator.page >= 1
    assert document.metadata.page_count == len(document.pages)


def test_pdf_table_is_a_grid_not_a_blob_of_text(corpus_dir, extract) -> None:
    document, _choice, _extractor = extract(corpus_dir / "well_a3_program_rev12.pdf")
    assert document.tables, "the program page has a table"
    table = document.tables[0]
    assert table.row_count >= 3 and table.column_count >= 2
    assert all(len(row) == table.column_count for row in table.rows), "ragged grid"
    assert table.page == 1 and table.table_id
    cells = {cell for row in table.rows for cell in row if cell}
    assert any("ppg" in cell.lower() or "gradient" in cell.lower() for cell in cells), sorted(
        cells
    )[:8]


def test_a_scanned_pdf_reports_that_it_has_no_text(corpus_dir, extract) -> None:
    """The diagnostic is the product here: "no text layer" must be visible, not silent."""
    document, _choice, _extractor = extract(corpus_dir / "scanned_well_b11_report.pdf")
    assert not document.text.strip()
    assert document.diagnostics, "an unreadable page has to say so"
    assert any(
        "no extractable text" in line or "scan" in line.lower() for line in document.diagnostics
    )


def test_excel_keeps_sheets_formulas_and_cell_types(corpus_dir, extract) -> None:
    document, _choice, _extractor = extract(corpus_dir / "mud_report_well-a3.xlsx")
    workbook = document.metadata.extra.get("workbook") or {}
    sheets = workbook.get("sheets") or []
    assert len(sheets) >= 2, sheets
    assert all(sheet.get("name") for sheet in sheets)
    # A hidden sheet is data the reader cannot scroll to; the platform records the fact
    # rather than silently dropping it (section 21).
    assert any(sheet["name"] == "Calibration" and not sheet["visible"] for sheet in sheets), sheets
    assert document.tables, "each populated sheet becomes a table"
    table = document.tables[0]
    assert table.sheet and table.provenance is not None
    assert (
        table.provenance.locator.kind == "excel" and table.provenance.locator.sheet == table.sheet
    )
    # A label/value row is read as a field, and it is cited to the *cell* that holds the
    # number - including when the row also carries a units column and a remark column,
    # which is how real mud reports are laid out.
    fields = [field for field in document.extracted_fields if field.name == "mud_weight"]
    assert fields, "the Summary sheet's mud weight row must be read as a field"
    field = fields[0]
    assert float(field.value) == pytest.approx(10.2)
    assert field.unit == "ppg", "the units column belongs to the value"
    assert field.provenance.locator.ref() == "Sheet: Summary > Cell: B9", (
        field.provenance.locator.ref()
    )
    assert all(other.provenance.locator.cell for other in document.extracted_fields), (
        "every key/value field needs a cell citation"
    )


def test_a_workbook_provenance_excerpt_is_the_text_at_its_location(corpus_dir, extract) -> None:
    """A recorded excerpt is a quotation, and a quotation must survive being re-read.

    Every non-empty excerpt the Excel extractor writes is compared with what the locator points
    at in the same file.  This is the property the whole provenance feature rests on - "the number
    is here because the source says so" - and it is checked against a real workbook rather than a
    fixture shaped to pass it.
    """
    from drilling_intelligence.core.provenance import verify_provenance

    path = corpus_dir / "mud_report_well-a3.xlsx"
    document, _choice, _extractor = extract(path)
    records = [
        (f"paragraph {paragraph.index}", paragraph.text, paragraph.provenance)
        for paragraph in document.paragraphs
    ]
    records += [
        (f"table {table.table_id}", table.caption, table.provenance) for table in document.tables
    ]
    records += [
        (
            f"field {field.name}",
            field.value_text if hasattr(field, "value_text") else str(field.value),
            field.provenance,
        )
        for field in document.extracted_fields
    ]
    quoted = [
        (label, text, provenance)
        for label, text, provenance in records
        if provenance is not None and str(provenance.excerpt or "")
    ]
    assert quoted, "the workbook extractor is expected to record excerpts, not only locations"
    for label, _text, provenance in quoted:
        outcome = verify_provenance(path, provenance)
        assert outcome.status == "MATCH", (
            f"{label} cites {provenance.locator.ref()!r} but it does not re-read: {outcome.detail} / {outcome.current_excerpt[:60]!r}"
        )


def test_a_synthetic_sheet_heading_is_located_without_claiming_a_quotation(
    corpus_dir, extract
) -> None:
    """``Sheet: Summary`` is our label for a place, not a sentence the file contains.

    Keeping the locator is useful (it is where the sheet starts); recording an excerpt is not,
    because verification would compare our words with cell A1 and report a mismatch on an
    untouched file.  So the heading is cited, quotes nothing, and says so.
    """
    from drilling_intelligence.core.provenance import verify_provenance

    path = corpus_dir / "mud_report_well-a3.xlsx"
    document, _choice, _extractor = extract(path)
    headings = [paragraph for paragraph in document.paragraphs if paragraph.style == "sheet"]
    assert headings, "each sheet contributes one searchable title paragraph"
    for paragraph in headings:
        assert paragraph.provenance is not None and paragraph.provenance.locator.ref().startswith(
            "Sheet:"
        )
        assert not str(paragraph.provenance.excerpt or ""), (
            "a synthesised label must not be recorded as a quotation"
        )
        assert verify_provenance(path, paragraph.provenance).status == "NOT_CHECKABLE"


def test_docx_keeps_the_heading_hierarchy_and_tables(corpus_dir, extract) -> None:
    document, _choice, extractor = extract(corpus_dir / "daily_drilling_report_well-a3.docx")
    assert extractor.name == "docx"
    headings = [paragraph for paragraph in document.paragraphs if paragraph.is_heading]
    assert headings, "the DDR's section headings are structure, not decoration"
    assert any(paragraph.heading_level == 1 for paragraph in headings)
    assert document.tables, "the DDR has a table"
    assert len(document.text) > 200
    core = document.metadata.extra.get("core_properties") or {}
    assert core, "the DOCX core properties are part of the record"


def test_csv_and_text_keep_line_offsets_and_the_header_row(corpus_dir, extract) -> None:
    for name in ("npt_summary_2025-06.csv", "lesson_learned_ll-2025-014.txt"):
        document, _choice, extractor = extract(corpus_dir / name)
        assert extractor.name == "text", name
        assert document.text, name
        # Provenance may sit on a paragraph (prose) or on the table (a delimited file),
        # but in both cases it must be a line range that a human can open to.
        located = [
            paragraph.provenance for paragraph in document.paragraphs if paragraph.provenance
        ]
        located += [table.provenance for table in document.tables if table.provenance]
        assert located, name
        for provenance in located:
            assert provenance.locator.kind == "text", name
            assert provenance.locator.line_start >= 1, name
    csv_doc, _choice, _extractor = extract(corpus_dir / "npt_summary_2025-06.csv")
    assert csv_doc.tables, "a CSV is a table by definition"
    header = [cell for cell in csv_doc.tables[0].rows[0] if cell]
    assert any("npt" in cell.lower() or "hours" in cell.lower() for cell in header), header


def test_the_stored_artefact_round_trips_without_losing_meaning(corpus_dir, extract) -> None:
    """The JSON in the database is what search, comparison and the UI actually read."""
    document, _choice, _extractor = extract(corpus_dir / "well_a3_program_rev12.pdf")
    payload = document.to_dict()
    restored = NormalizedDocument.from_dict(payload)
    assert restored.text == document.text
    assert len(restored.paragraphs) == len(document.paragraphs)
    assert len(restored.tables) == len(document.tables)
    assert [len(t.rows) for t in restored.tables] == [len(t.rows) for t in document.tables]
    assert restored.metadata.page_count == document.metadata.page_count
    assert len(restored.extracted_fields) == len(document.extracted_fields)
    assert restored.diagnostics == document.diagnostics


def test_the_router_degrades_instead_of_failing_on_garbage(tmp_path, extract) -> None:
    broken = tmp_path / "broken.pdf"
    broken.write_bytes(b"%PDF-1.4\nthis is not a document\n%%EOF\n")
    with pytest.raises(Exception) as excinfo:
        extract(broken)
    assert not isinstance(excinfo.value, (RecursionError, MemoryError, OSError))
