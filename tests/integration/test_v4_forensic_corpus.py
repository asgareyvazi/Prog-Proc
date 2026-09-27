"""The V4 forensic corpus exercises admitted writers and explicitly denied source shapes.

Fourteen deterministic source files, each with a stated expectation.  The value of the corpus is that
it holds both sides of the boundary at once: ``bha_tally_well-a3.xlsx`` is promoted *because* it is a
component tally, and ``bha_report_well-a3.txt`` is not promoted even though it carries the same
classification, because a BHA narrative is not a tally.  A future change that admits the prose file, or
that stops admitting the tally, fails here rather than in somebody's workspace.
"""

from __future__ import annotations

from sqlalchemy import select
from tests.fixtures.fieldops import register_wells, well_id_for
from tests.fixtures.generate import build_v4_forensic_corpus

from drilling_intelligence.database.models import (
    BhaComponent,
    BhaReport,
    BitRecord,
    Document,
    SurveyRun,
    SurveyStation,
)
from drilling_intelligence.ingestion.pipeline import IngestionPipeline
from drilling_intelligence.operations.service import OperationalService

_EXPECTED = {
    "bha_report_well-a3.txt": "BHA_REPORT",
    "bha_tally_well-a3.xlsx": "BHA_REPORT",
    "bit_record_well-a3.txt": "BIT_RECORD",
    "bit_tally_well-a3.xlsx": "BIT_RECORD",
    "casing_report_well-a3.txt": "CASING_REPORT",
    "daily_drilling_report_well-a3.docx": "DDR",
    "directional_survey_well-a3.csv": "DIRECTIONAL_SURVEY",
    "lesson_learned_ll-2025-014.txt": "LESSON_LEARNED",
    "mud_report_well-a3.xlsx": "MUD_REPORT",
    "npt_summary_2025-06.csv": "NPT",
    "scanned_well_b11_report.pdf": "OTHER",
    "service_report_well-a3.txt": "SERVICE_REPORT",
    "well_a3_program_rev12.pdf": "DRILLING_PROGRAM",
    "well_control_kill_sheet_well-a3.txt": "WELL_CONTROL",
}

#: The five files whose classification has a registered writer but whose *shape* is not one the writer
#: accepts.  Each of these is a successful refusal, not an error: the artefact, its evidence and its
#: knowledge facts all remain, and no authoritative row is written.
_EXPECTED_SHAPE_REFUSALS = {
    # Same classification as ``bha_tally_well-a3.xlsx`` / ``bit_tally_well-a3.xlsx``, different shape.
    "bha_report_well-a3.txt",
    "bit_record_well-a3.txt",
    # No registered writer at all.
    "casing_report_well-a3.txt",
    "lesson_learned_ll-2025-014.txt",
    "scanned_well_b11_report.pdf",
    "service_report_well-a3.txt",
    "well_control_kill_sheet_well-a3.txt",
}


def _ingest(workspace):
    register_wells(workspace)
    corpus = workspace.root / "corpus"
    built = build_v4_forensic_corpus(corpus)
    assert len(built) == 14
    result = IngestionPipeline(
        settings=workspace.settings,
        workspace_root=workspace.root,
        database=workspace.database,
    ).run(root=corpus, well_id=well_id_for(workspace, "A-3"))
    assert result.ok, result.error
    assert result.counts["NEW"] == 14
    return corpus


def test_fourteen_source_shaped_cases_classify_and_promote_exactly_as_documented(workspace) -> None:
    _ingest(workspace)

    with workspace.database.read_only() as session:
        documents = {row.filename: row.classification for row in session.scalars(select(Document))}
    assert documents == _EXPECTED

    summary = OperationalService.for_workspace(workspace).promote_workspace(
        include_unsupported=True
    )
    assert summary["versions"] == 14
    assert summary["outcomes"]["promoted"] == 7
    assert summary["outcomes"]["unsupported"] == 7
    assert summary["outcomes"]["error"] == 0
    assert summary["outcomes"]["ambiguous"] == 0
    assert summary["outcomes"]["missing_provenance"] == 0
    assert summary["counts"]["bha_report"]["created"] == 1
    assert summary["counts"]["bha_component"]["created"] == 6
    assert summary["counts"]["bit_record"]["created"] == 2
    assert summary["counts"]["survey_run"]["created"] == 1
    assert summary["counts"]["survey_station"]["created"] == 5
    assert summary["counts"]["mud_report"]["created"] == 1

    # The two prose files are refused by shape, and the refusal says why rather than reporting a
    # generic "unsupported classification" for a class that does have a writer.
    shape_refusals = [
        item
        for item in summary["skipped_details"]
        if item["reason"] == "NO_RECOGNISED_TABLE"
    ]
    assert len(shape_refusals) == 2
    assert {"bottom hole assembly" in item["detail"] or "bit record" in item["detail"] for item in shape_refusals} == {True}


def test_no_row_is_written_for_a_refused_source_shape(workspace) -> None:
    _ingest(workspace)
    OperationalService.for_workspace(workspace).promote_workspace(include_unsupported=True)

    with workspace.database.read_only() as session:
        prose_versions = [
            str(row.current_version_id)
            for row in session.scalars(select(Document))
            if row.filename in {"bha_report_well-a3.txt", "bit_record_well-a3.txt"}
        ]
    assert len(prose_versions) == 2
    for model in (BhaReport, BhaComponent, BitRecord, SurveyRun, SurveyStation):
        with workspace.database.read_only() as session:
            rows = [
                row
                for row in session.scalars(select(model))
                if str(row.document_version_id) in prose_versions
            ]
        assert rows == [], f"{model.__tablename__} was written from a prose source"


def test_the_v4_writers_read_the_well_the_document_is_linked_to(workspace) -> None:
    _ingest(workspace)
    OperationalService.for_workspace(workspace).promote_workspace()

    a3 = well_id_for(workspace, "A-3")
    for model in (BhaReport, BhaComponent, BitRecord, SurveyRun, SurveyStation):
        with workspace.database.read_only() as session:
            wells = {str(row.well_id) for row in session.scalars(select(model))}
        assert wells == {a3}, f"{model.__tablename__} crossed a well boundary: {wells}"
