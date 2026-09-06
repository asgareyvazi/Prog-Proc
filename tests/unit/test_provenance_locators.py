"""Provenance: every extracted fact points at an exact source location and can be
re-read from that file instead of being taken on trust (spec sections 17 and 95)."""

from __future__ import annotations

from drilling_intelligence.core.hashing import (
    filename_identity,
    identity_slug,
    is_sha256,
    sha256_file,
    sha256_text,
)
from drilling_intelligence.core.provenance import (
    DocxLocator,
    ExcelLocator,
    PdfLocator,
    Provenance,
    SourceLocator,
    TextLocator,
    UnknownLocator,
    verify_provenance,
)


def test_locator_references_are_human_readable() -> None:
    assert PdfLocator(page=7, block=2).ref() == "Page 7 > Block 2"
    assert ExcelLocator(sheet="Mud Log", cell="B14").ref() == "Sheet: Mud Log > Cell: B14"
    assert (
        ExcelLocator(sheet="S", range_="A3:G9", read="formula").ref()
        == "Sheet: S > Range: A3:G9 > formula"
    )
    assert DocxLocator(heading="NPT", paragraph=4).ref() == "Heading: NPT > Paragraph 4"
    assert TextLocator(line_start=10, line_end=12).ref() == "Lines 10-12"
    assert UnknownLocator(note="artifact lost").ref() == "Location unknown (artifact lost)"


def test_locator_round_trips_through_json() -> None:
    for locator in (
        PdfLocator(page=1, block=2, paragraph=3, bbox=(1.0, 2.0, 3.0, 4.0)),
        ExcelLocator(sheet="S", range_="A1:B2", read="formula"),
        DocxLocator(table=1, row=2, column=3),
        TextLocator(char_start=5, char_end=90),
    ):
        payload = locator.to_dict()
        assert payload["locator_kind"] == locator.kind
        assert SourceLocator.from_dict(payload) == locator, (
            f"{type(locator).__name__} lost data in JSON"
        )


def test_verify_provenance_re_reads_the_original_file(corpus_dir) -> None:
    source = corpus_dir / "lesson_learned_ll-2025-014.txt"
    lines = source.read_text(encoding="utf-8").splitlines()
    index = next(n for n, line in enumerate(lines, start=1) if len(line) > 25)

    def record(excerpt: str, *, sha: str = "") -> Provenance:
        return Provenance(
            document_id="doc-1",
            filename=source.name,
            locator=TextLocator(line_start=index, line_end=index),
            excerpt=excerpt,
            source_sha256=sha or sha256_file(source),
        )

    assert verify_provenance(source, record(lines[index - 1])).status == "MATCH"
    # The excerpt is not what the file says there.
    assert (
        verify_provenance(source, record("a sentence that is not in this file")).status
        == "MISMATCH"
    )
    # Right excerpt, different file on disk: the recorded hash is the tamper check.
    assert (
        verify_provenance(source, record(lines[index - 1], sha=sha256_text("other"))).status
        == "MISMATCH"
    )


def test_unreadable_source_is_reported_not_silently_ok(tmp_path) -> None:
    provenance = Provenance(
        document_id="doc-1",
        filename="gone.txt",
        locator=TextLocator(line_start=1, line_end=1),
        excerpt="anything",
    )
    result = verify_provenance(tmp_path / "gone.txt", provenance)
    assert result.status == "UNREADABLE" and not result.ok


def test_hashes_and_identity(tmp_path) -> None:
    file = tmp_path / "a.txt"
    file.write_text("12345\n", encoding="utf-8")
    assert sha256_file(file) == sha256_text("12345\n")
    assert is_sha256(sha256_file(file)) and not is_sha256("nope")
    assert identity_slug("North Cormorant  Well-A3!") == "north-cormorant-well-a3"

    root = tmp_path / "ws"
    (root / "documents").mkdir(parents=True)
    same = root / "documents" / "DDR.docx"
    assert filename_identity(same, root) == "documents/ddr.docx"
    assert filename_identity(same, root) == filename_identity(root / "Documents" / "ddr.DOCX", root)
    # A file outside the workspace still gets a stable key instead of ".." noise.
    assert ".." not in filename_identity(tmp_path / "elsewhere.docx", root)
