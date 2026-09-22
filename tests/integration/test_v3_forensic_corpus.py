"""The V3 forensic corpus exercises admitted and explicitly denied classes."""

from __future__ import annotations

from sqlalchemy import select
from tests.fixtures.fieldops import register_wells, well_id_for
from tests.fixtures.generate import build_v3_forensic_corpus

from drilling_intelligence.database.models import Document
from drilling_intelligence.ingestion.pipeline import IngestionPipeline
from drilling_intelligence.operations.service import OperationalService

_EXPECTED = {
    "bha_report_well-a3.txt": "BHA_REPORT",
    "bit_record_well-a3.txt": "BIT_RECORD",
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


def test_twelve_source_shaped_cases_classify_without_expanding_writers(workspace) -> None:
    register_wells(workspace)
    corpus = workspace.root / "corpus"
    built = build_v3_forensic_corpus(corpus)
    assert len(built) == 12
    ingestion = IngestionPipeline(
        settings=workspace.settings,
        workspace_root=workspace.root,
        database=workspace.database,
    ).run(root=corpus, well_id=well_id_for(workspace, "A-3"))
    assert ingestion.ok, ingestion.error
    assert ingestion.counts["NEW"] == 12

    with workspace.database.read_only() as session:
        documents = {row.filename: row.classification for row in session.scalars(select(Document))}
    assert documents == _EXPECTED

    summary = OperationalService.for_workspace(workspace).promote_workspace(
        include_unsupported=True
    )
    assert summary["versions"] == 12
    assert summary["outcomes"]["promoted"] == 4
    assert summary["outcomes"]["unsupported"] == 8
    assert summary["counts"]["mud_report"]["created"] == 1
