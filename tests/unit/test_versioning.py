"""Revision and status parsing (spec sections 14 and 29).

The rule the module obeys: derive a revision only from unambiguous evidence and record a
note when there is none.  A wrong ``Rev 4`` is worse than an empty field, because the UI
badge, the authority tier and the conflict detector all sort on it.
"""

from __future__ import annotations

from datetime import UTC, datetime

from drilling_intelligence.core.enums import DocumentStatus
from drilling_intelligence.documents.versioning import (
    RevisionInfo,
    is_latest,
    parse_revision,
    revision_from_token,
    sort_key,
)


def test_revision_markers_survive_every_naming_style() -> None:
    for filename in (
        "well_a3_program_rev12.pdf",
        "well-a3-program-rev-12.pdf",
        "Program Rev. 12.pdf",
    ):
        info = parse_revision(filename)
        assert info.revision == "Rev 12", filename
        assert info.revision_key == 1200, filename
        assert info.source == "filename", filename


def test_letter_revisions_sort_between_the_numbers() -> None:
    assert revision_from_token("0") == 0
    assert revision_from_token("3") == 300
    assert revision_from_token("C") == 350
    assert revision_from_token("iii") == 0  # no evidence rather than a guess
    assert parse_revision("program_revC.pdf").revision == "Rev C"


def test_filename_evidence_beats_the_document_body() -> None:
    info = parse_revision("well_a3_program_rev12.pdf", content="Revision 3\nApproved for drilling")
    assert info.revision == "Rev 12" and info.source == "filename"
    assert info.status is DocumentStatus.APPROVED  # status may still come from the body
    assert any("first pages" in note for note in info.notes)


def test_revision_read_from_the_body_is_flagged() -> None:
    info = parse_revision("program.pdf", content="Document Revision No. 14\nApproved for drilling")
    assert info.revision == "Rev 14" and info.source == "content" and info.approved
    assert any("not the filename" in note for note in info.notes)


def test_status_markers_are_recognised_whatever_the_separator() -> None:
    cases = {
        "IFR_well_a3.pdf": DocumentStatus.ISSUED_FOR_REVIEW,
        "as_built_well_b11.pdf": DocumentStatus.APPROVED,
        "as-built-report.pdf": DocumentStatus.APPROVED,
        "draft_program.pdf": DocumentStatus.DRAFT,
        "obsolete_program.pdf": DocumentStatus.SUPERSEDED,
        "daily_report.pdf": DocumentStatus.UNKNOWN,
    }
    for filename, expected in cases.items():
        assert parse_revision(filename).status is expected, filename


def test_document_date_is_never_the_filesystem_stamp() -> None:
    assert parse_revision("mudlog_2025-06-14.pdf").revision_date == datetime(
        2025, 6, 14, tzinfo=UTC
    )
    # mtime says nothing about the document's revision date, so it is not substituted.
    assert (
        parse_revision("mudlog.pdf", file_modified=datetime(2020, 1, 1, tzinfo=UTC)).revision_date
        is None
    )


def test_missing_evidence_is_recorded_not_invented() -> None:
    info = parse_revision("daily_report.pdf")
    assert info.revision == "" and info.revision_key == 0
    assert info.status is DocumentStatus.UNKNOWN and info.source == "none"
    assert any("no revision evidence" in note for note in info.notes)


def test_latest_version_ranks_by_revision_then_mtime() -> None:
    older = parse_revision("program_rev11.pdf")
    newer = parse_revision("program_rev12.pdf")
    unlabelled = parse_revision("program.pdf")
    now = datetime(2026, 1, 1, tzinfo=UTC)
    assert sort_key(newer, now) > sort_key(older, now)
    # Without a revision marker the mtime decides, but a marked revision still wins.
    assert sort_key(older, None) > sort_key(unlabelled, now)
    assert is_latest([(unlabelled, now), (older, now), (newer, now)]) == 2
    assert is_latest([]) == -1


def test_revision_info_serialises_for_the_database() -> None:
    info = RevisionInfo(
        revision="Rev 2", revision_key=200, status=DocumentStatus.APPROVED, source="filename"
    )
    payload = info.to_dict()
    assert payload["status"] == "APPROVED" and payload["revision_key"] == 200
    assert payload["revision_date"] is None and payload["notes"] == []
