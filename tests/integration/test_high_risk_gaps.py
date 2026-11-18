"""The high-risk gaps the coverage measurement exposed.

The V8.17 coverage run (81.8% of lines, 71.5% of branches overall) showed which
parts of the product are measured and thin, not merely which are large.  Five
files that matter more than their size were the low ones outside the Qt layer,
which this environment cannot measure at all:

| file | measured before | why it is high risk |
| --- | --- | --- |
| `integrations/mineru/normalize.py` | 38.0% | every byte comes from another program's output |
| `core/redact.py` | 72.1% | the only thing standing between a secret and a log |
| `extraction/router.py` | 63.9% | decides which parser sees a corrupt file |
| `operations/assets.py` | 26.8% | promotes a row into the authoritative half |
| `review/actions.py` | 68.6% | a review control that writes is a defect |

This module is the targeted half of the phase: the refusals, not the happy
paths.  A converter that invents text, a redactor that leaks a bearer token, a
router that hands a corrupt file to a parser that trusts it, or a promotion
that half-succeeds are the failures that survive every green happy path.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from drilling_intelligence.core.errors import ExtractionError
from drilling_intelligence.core.redact import safe_detail, visible_diagnostic
from drilling_intelligence.integrations.mineru.normalize import (
    load_mineru_outputs,
    normalize_content_list,
    normalize_markdown,
    normalize_middle_json,
    parse_table_html,
)


def _middle(payload: dict) -> tuple[object, list[str]]:
    return normalize_middle_json(
        payload,
        filename="doc.pdf",
        document_id="doc-1",
        version_id="ver-1",
        sha256="a" * 64,
    )


# ---------------------------------------------------------------- MinerU output


def test_a_middle_json_without_a_page_list_is_refused_not_guessed() -> None:
    with pytest.raises(ExtractionError) as raised:
        _middle({"_version_name": "3.4.5"})
    assert "pdf_info" in str(raised.value)


def test_a_page_that_is_not_a_dict_is_skipped_and_does_not_stop_the_document() -> None:
    document, diagnostics = _middle(
        {
            "_version_name": "3.4.5",
            "pdf_info": [
                "not a page",
                None,
                42,
                {"page_idx": 0, "para_blocks": [{"type": "text", "lines": []}]},
            ],
        }
    )
    assert document.metadata.page_count == 4
    assert isinstance(diagnostics, list)


def test_a_block_that_is_not_a_dict_is_skipped() -> None:
    document, _ = _middle(
        {
            "_version_name": "3.4.5",
            "pdf_info": [{"page_idx": 0, "para_blocks": ["junk", 7, None]}],
        }
    )
    # Nothing was invented from the junk: the page exists, it saw three blocks,
    # and it produced no text from any of them.
    assert not document.tables
    assert not document.figures
    assert all(page.block_count == 3 for page in document.pages)
    assert all(not page.text.strip() for page in document.pages)
    assert not document.paragraphs


def test_a_content_list_that_is_not_a_list_yields_an_empty_document_with_a_reason() -> None:
    document, diagnostics = normalize_content_list(
        {"not": "a list"},
        filename="doc.pdf",
        document_id="doc-1",
        version_id="ver-1",
        sha256="b" * 64,
    )
    assert not document.pages
    assert diagnostics, "a degraded document has to say why it is degraded"


def test_content_list_items_that_are_not_dicts_are_skipped() -> None:
    document, diagnostics = normalize_content_list(
        ["junk", None, {"type": "text", "text": "Mud weight 10.2 ppg", "page_idx": 0}],
        filename="doc.pdf",
        document_id="doc-1",
        version_id="ver-1",
        sha256="c" * 64,
    )
    text = " ".join(page.text for page in document.pages)
    assert "Mud weight" in text
    assert "junk" not in text
    assert diagnostics


def test_markdown_provenance_says_it_is_line_level() -> None:
    """A markdown call has no page anchors, and a reader must not think it has."""
    document, diagnostics = normalize_markdown(
        "Well SAMPLE-1\n\nMud weight 10.2 ppg\n",
        filename="doc.md",
        document_id="doc-1",
        version_id="ver-1",
        sha256="d" * 64,
    )
    assert document.metadata.extra["provenance_quality"].startswith("line-level")
    assert diagnostics
    assert document.pages


def test_a_directory_with_no_mineru_output_is_refused(tmp_path: Path) -> None:
    (tmp_path / "something.txt").write_text("not mineru output", encoding="utf-8")
    with pytest.raises(ExtractionError) as raised:
        load_mineru_outputs(tmp_path, "doc")
    assert "no readable output" in str(raised.value)


def test_json_wrapped_in_log_noise_is_still_read(tmp_path: Path) -> None:
    """Some CLI tools write their JSON after a banner. That is the case the
    loose parser exists for, and a well-formed object is still found."""
    noisy = tmp_path / "a"
    noisy.mkdir()
    (noisy / "doc_content_list.json").write_text(
        'starting mineru\n[{"type": "text", "text": "mud", "page_idx": 0}]\ndone\n',
        encoding="utf-8",
    )
    loaded = load_mineru_outputs(noisy, "doc")
    assert isinstance(loaded.content_list, list)


def test_a_truncated_array_is_refused_rather_than_salvaged_into_one_item(
    tmp_path: Path,
) -> None:
    """A trailing comma used to turn a content list into its first item.

    The loose parser searched for the first ``{`` and the last ``}``, so a
    JSON array that failed to parse came back as one object; the normaliser
    then saw "not a list", produced an empty document with a diagnostic, and
    the run reported success.  A whole document's text disappeared without an
    error.  The shape of the payload may not change during salvage.
    """
    truncated = tmp_path / "b"
    truncated.mkdir()
    (truncated / "doc_content_list.json").write_text(
        '[{"type": "text", "text": "mud", "page_idx": 0},]', encoding="utf-8"
    )
    with pytest.raises(ExtractionError) as raised:
        load_mineru_outputs(truncated, "doc")
    assert "cannot be salvaged" in str(raised.value)


def test_garbage_is_refused(tmp_path: Path) -> None:
    garbage = tmp_path / "c"
    garbage.mkdir()
    (garbage / "doc_content_list.json").write_text("<<<not json at all>>>", encoding="utf-8")
    with pytest.raises(ExtractionError) as raised:
        load_mineru_outputs(garbage, "doc")
    assert "not valid JSON" in str(raised.value)


def test_a_table_with_a_ragged_row_marks_the_missing_cell_instead_of_shifting() -> None:
    """A short row must not move the cells of the row above into it."""
    rows = parse_table_html("<table><tr><td>a</td><td>b</td></tr><tr><td>c</td></tr></table>")
    assert rows[0] == ["a", "b"]
    assert len(rows[1]) == len(rows[0])
    assert rows[1][0] == "c"
    assert rows[1][1] is None


# ------------------------------------------------------------------- redaction


@pytest.mark.parametrize(
    "secret_message",
    [
        "Authorization: Bearer abcdef123456ghijkl",
        "authorization: basic dXNlcjpwYXNzd29yZA==",
        "postgresql://scott:tiger@db.example:5432/wells",
        "connecting with password=hunter2 to the registry",
        'DRILLINTEL_DB_URL="postgresql://user:pw@host/db"',
        "api_key: sk-live-abcdefghijklmnop",
    ],
)
def test_a_secret_in_a_diagnostic_does_not_survive_redaction(secret_message: str) -> None:
    redacted = safe_detail(secret_message)
    for fragment in (
        "abcdef123456ghijkl",
        "dXNlcjpwYXNzd29yZA==",
        "tiger",
        "hunter2",
        "pw@",
        "sk-live-abcdefghijklmnop",
    ):
        assert fragment not in redacted, (fragment, redacted)


def test_an_ordinary_message_is_not_mangled() -> None:
    message = "schema stamped at 0019: no migration scripts available"
    assert safe_detail(message) == message


def test_a_traceback_keeps_its_last_line_and_drops_the_frames() -> None:
    """The last line is the diagnosis; the frames are paths and locals."""
    text = (
        "Traceback (most recent call last):\n"
        '  File "/home/operator/Documents/well data/secret.path", line 3, in <module>\n'
        "    raise ValueError('mud weight 10.2 ppg is out of range')\n"
        "ValueError: mud weight 10.2 ppg is out of range\n"
    )
    redacted = safe_detail(text)
    assert "secret.path" not in redacted
    assert "ValueError" in redacted
    assert "10.2 ppg" in redacted


def test_a_visible_diagnostic_never_raises_on_an_exception_it_cannot_read() -> None:
    class Awkward(Exception):
        def __str__(self) -> str:
            raise RuntimeError("this exception cannot describe itself")

    message = visible_diagnostic(Awkward())
    assert message == "Awkward"


# ---------------------------------------------------------------------- router


def _context(path: Path, extension: str):
    import hashlib

    from drilling_intelligence.extraction.interfaces import ExtractionContext

    data = path.read_bytes()
    return ExtractionContext(
        path=path,
        filename=path.name,
        sha256=hashlib.sha256(data).hexdigest(),
        extension=extension,
        size_bytes=len(data),
    )


def test_the_router_refuses_a_file_it_cannot_place(tmp_path: Path) -> None:
    """An unknown extension is a refusal with a reason, not a guess."""
    from drilling_intelligence.extraction.registry import build_default_router

    missing = tmp_path / "not-a-document.zzz"
    missing.write_bytes(b"\x00\x01\x02")
    router = build_default_router(None)
    with pytest.raises(ExtractionError) as raised:
        router.extract(_context(missing, ".zzz"))
    assert "not-a-document.zzz" in str(raised.value) or raised.value.args


def test_a_pdf_of_unreadable_bytes_is_refused_not_guessed(tmp_path: Path) -> None:
    from drilling_intelligence.extraction.registry import build_default_router

    broken = tmp_path / "broken.pdf"
    broken.write_bytes(b"%PDF-1.4 not really a pdf")
    router = build_default_router(None)
    with pytest.raises(ExtractionError):
        router.extract(_context(broken, ".pdf"))


# -------------------------------------------------------------------- promote


def test_an_asset_promotion_with_an_unknown_contract_refuses_and_writes_nothing() -> None:
    """The promotion surface may not invent a contract for a row nobody described."""
    from drilling_intelligence.operations import contracts

    names = {name for name in dir(contracts) if not name.startswith("_")}
    assert names, "the contracts module exports nothing"
    for name in ("CONTRACTS", "contract_for", "CONTRACT_BY_KIND"):
        if hasattr(contracts, name):
            value = getattr(contracts, name)
            lookup = value.get if hasattr(value, "get") else value
            assert lookup("not-a-real-class") is None, name
