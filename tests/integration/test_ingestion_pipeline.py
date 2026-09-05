"""End-to-end ingestion over a real workspace and a real SQLite database.

This is the test that the whole phase-0 promise rests on: the first run reads every file,
the second run does nothing, a changed file becomes a new version that supersedes the old
one without losing its links, a copy is recognised as a duplicate and reuses the cached
extraction, and a file that disappears is reported as removed rather than deleted.
Migrations are exercised because ``workspace.database`` runs them (no ``create_all``).
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest
from sqlalchemy import select, text
from tests.fixtures.generate import build_corpus

from drilling_intelligence.database.models import (
    AuditEvent,
    Document,
    DocumentVersion,
    Extraction,
    IngestionRun,
)
from drilling_intelligence.ingestion.pipeline import IngestionPipeline
from drilling_intelligence.wells.repository import WellRepository

EXPECTED = {
    "daily_drilling_report_well-a3.docx": ("docx", "DDR"),
    "lesson_learned_ll-2025-014.txt": ("text", "LESSON_LEARNED"),
    "mud_report_well-a3.xlsx": ("excel", "MUD_REPORT"),
    "npt_summary_2025-06.csv": ("text", "NPT"),
    "scanned_well_b11_report.pdf": ("pdf_text", "OTHER"),
    "well_a3_program_rev12.pdf": ("pdf_text", "DRILLING_PROGRAM"),
}


@pytest.fixture
def corpus(workspace) -> Path:
    root = workspace.root / "corpus"
    build_corpus(root)
    return root


@pytest.fixture
def ingested(workspace, corpus: Path):
    """One pipeline plus the ids the runs need, so each test drives real code."""
    with workspace.database.session() as session:
        repo = WellRepository(session)
        ws_row = repo.get_or_create_workspace(str(workspace.root), name="North Cormorant")
        project = repo.get_or_create_project("North Cormorant")
        well = repo.create_well("A-3", project_id=project.id)
        session.commit()
        ids = (ws_row.id, well.id)
    pipeline = IngestionPipeline(settings=workspace.settings, workspace_root=workspace.root, database=workspace.database)
    return pipeline, corpus, ids


def run(pipeline, corpus: Path, ids) -> object:
    workspace_id, well_id = ids
    return pipeline.run(root=corpus, workspace_id=workspace_id, well_id=well_id)


def test_first_run_registers_and_extracts_every_file(ingested, workspace) -> None:
    pipeline, corpus, ids = ingested
    result = run(pipeline, corpus, ids)
    assert result.ok, result.error
    assert result.failures == 0, [item.error for item in result.failures_report()]
    assert result.counts["NEW"] == len(EXPECTED)
    assert result.counts["PROCESSED"] == len(EXPECTED)
    assert result.files_registered == len(EXPECTED)
    by_name = {item.filename: item for item in result.results}
    assert set(by_name) == set(EXPECTED)
    for name, (extractor, classification) in EXPECTED.items():
        item = by_name[name]
        assert item.ok, (name, item.error)
        assert item.extractor == extractor, (name, item.extractor)
        assert item.classification == classification, (name, item.classification, item.warnings)
        assert item.sha256 and len(item.sha256) == 64
        # The scanned PDF genuinely has no text layer: it must be reported as unreadable
        # rather than classified from a guess.
        assert (item.fields > 0) is (name != "scanned_well_b11_report.pdf"), name
    with workspace.database.read_only() as session:
        documents = list(session.scalars(select(Document)))
        assert len(documents) == len(EXPECTED)
        for document in documents:
            assert document.current_version_id, document.filename
            assert document.identity_path == f"corpus/{document.filename.lower()}", document.identity_path
            version = session.get(DocumentVersion, document.current_version_id)
            extraction = session.scalar(select(Extraction).where(Extraction.document_version_id == version.id))
            assert extraction is not None and extraction.status in {"OK", "CACHE_HIT"}, document.filename
            if document.filename == "scanned_well_b11_report.pdf":
                # A scan really has no text layer: the run must succeed *and* say so,
                # rather than either failing the file or inventing content for it.
                assert not extraction.text_blob.strip()
                payload = extraction.document_json or {}
                assert any("no extractable text" in line for line in payload.get("diagnostics") or []), payload
            else:
                assert extraction.text_blob.strip(), document.filename


def test_second_run_does_no_work_at_all(ingested) -> None:
    pipeline, corpus, ids = ingested
    run(pipeline, corpus, ids)
    again = run(pipeline, corpus, ids)
    # Idempotency is the whole point of the content-hash planner: nothing changed, so
    # nothing is re-parsed, no version is added and no document is re-registered.
    assert again.counts["UNCHANGED"] == len(EXPECTED)
    assert again.counts["TO_PROCESS"] == 0
    assert again.counts["PROCESSED"] == 0
    assert again.files_registered == 0
    assert not [item for item in again.results if item.change.value != "UNCHANGED"]


def test_changed_file_becomes_a_new_version_and_keeps_its_links(ingested, workspace) -> None:
    from openpyxl import load_workbook

    pipeline, corpus, ids = ingested
    _, well_id = ids
    run(pipeline, corpus, ids)
    workbook_path = corpus / "mud_report_well-a3.xlsx"
    book = load_workbook(workbook_path)
    book["Summary"]["B9"] = 10.6
    book.save(workbook_path)
    result = run(pipeline, corpus, ids)
    assert result.counts["MODIFIED"] == 1 and result.counts["PROCESSED"] == 1
    changed = next(item for item in result.results if item.filename == "mud_report_well-a3.xlsx")
    assert changed.change.value == "MODIFIED"

    with workspace.database.read_only() as session:
        document = session.scalar(select(Document).where(Document.filename == "mud_report_well-a3.xlsx"))
        versions = list(
            session.scalars(select(DocumentVersion).where(DocumentVersion.document_id == document.id).order_by(DocumentVersion.version_number))
        )
        assert len(versions) == 2
        first, second = versions
        assert first.is_current is False and first.superseded_by_version_id == second.id
        assert second.is_current is True and second.origin == "MODIFIED"
        assert document.current_version_id == second.id
        assert document.sha256 == second.sha256 != first.sha256
        # Nothing is deleted and nothing is lost: the old version is still readable.
        old_extraction = session.scalar(select(Extraction).where(Extraction.document_version_id == first.id))
        assert old_extraction is not None, "history must stay queryable"
        assert document.well_id == well_id, "carry-forward must keep the well link"


def test_identical_copy_is_a_duplicate_reusing_the_cache(ingested, workspace) -> None:
    pipeline, corpus, ids = ingested
    run(pipeline, corpus, ids)
    source = corpus / "lesson_learned_ll-2025-014.txt"
    copy = corpus / "copy_of_lesson.txt"
    shutil.copy2(source, copy)
    result = run(pipeline, corpus, ids)
    assert result.counts["DUPLICATE"] == 1
    duplicate = next(item for item in result.results if item.filename == copy.name)
    assert duplicate.change.value == "DUPLICATE" and duplicate.from_cache
    assert duplicate.fields > 0, "a cached extraction still has to yield the fields"
    with workspace.database.read_only() as session:
        document = session.scalar(select(Document).where(Document.filename == copy.name))
        version = session.get(DocumentVersion, document.current_version_id)
        assert version.duplicate_of_version_id, "the duplicate must point at what it duplicates"
        assert version.origin == "DUPLICATE"


def test_the_better_cited_field_survives_into_storage(ingested, workspace) -> None:
    """The extractor's cell citation must not be replaced by a looser paragraph match.

    A mud report states ``Mud weight (ppg) | 10.2 | ppg | remark``; that is one number
    with two possible readings, and the registry has to keep the one that can be shown
    to a drilling engineer as a cell reference.
    """
    pipeline, corpus, ids = ingested
    run(pipeline, corpus, ids)
    with workspace.database.read_only() as session:
        document = session.scalar(select(Document).where(Document.filename == "mud_report_well-a3.xlsx"))
        version = session.get(DocumentVersion, document.current_version_id)
        extraction = session.scalar(select(Extraction).where(Extraction.document_version_id == version.id))
        fields = [
            field
            for field in (extraction.document_json or {}).get("extracted_fields") or []
            if field.get("name") == "mud_weight"
        ]
        assert fields, "the mud weight row should be stored as a field"
        mud_weight = fields[0]
        assert float(mud_weight["value"]) == pytest.approx(10.2)
        assert mud_weight["unit"] == "ppg"
        assert mud_weight["provenance"]["locator"]["cell"] == "B9", mud_weight["provenance"]["locator"]
        assert len(fields) == 1, "the same number must not be stored twice under one name"


def test_a_limited_run_does_not_report_the_rest_of_the_folder_as_missing(ingested) -> None:
    """``limit`` bounds the work of one run; it says nothing about what exists.

    Removal detection compares the registry against the *scan*.  Compare it against the truncated
    work list instead - which is what this used to do - and a capped run over a folder of 400
    files reports 399 documents as missing.
    """
    pipeline, corpus, (workspace_id, _well_id) = ingested
    pipeline.run(root=corpus, workspace_id=workspace_id)

    capped = pipeline.run(root=corpus, workspace_id=workspace_id, limit=2)
    assert capped.counts["TO_PROCESS"] <= 2, capped.counts
    assert capped.removed == [], f"a capped run must not invent missing files: {capped.removed}"

    # And the reconciliation still works: a file that really is gone is reported even when the
    # run only touches one of the remaining five.
    (corpus / "npt_summary_2025-06.csv").unlink()
    after = pipeline.run(root=corpus, workspace_id=workspace_id, limit=1)
    assert [item["filename"] for item in after.removed] == ["npt_summary_2025-06.csv"], after.removed


def test_removed_file_is_reported_but_never_deleted(ingested, workspace) -> None:
    pipeline, corpus, ids = ingested
    run(pipeline, corpus, ids)
    (corpus / "npt_summary_2025-06.csv").unlink()
    result = run(pipeline, corpus, ids)
    assert [(item["filename"], item["change"]) for item in result.removed] == [
        ("npt_summary_2025-06.csv", "REMOVED")
    ]
    # Ingestion never destroys the record: the file is gone from the folder, the
    # document and its provenance stay in the registry.
    with workspace.database.read_only() as session:
        assert session.scalar(select(Document).where(Document.filename == "npt_summary_2025-06.csv")) is not None
        assert result.counts["PROCESSED"] == 0


def test_provenance_locator_matches_the_format(ingested, workspace) -> None:
    pipeline, corpus, ids = ingested
    run(pipeline, corpus, ids)
    expected_kind = {
        "well_a3_program_rev12.pdf": "pdf",
        "mud_report_well-a3.xlsx": "excel",
        "daily_drilling_report_well-a3.docx": "docx",
        "npt_summary_2025-06.csv": "text",
    }
    with workspace.database.read_only() as session:
        for name, kind in expected_kind.items():
            document = session.scalar(select(Document).where(Document.filename == name))
            version = session.get(DocumentVersion, document.current_version_id)
            extraction = session.scalar(select(Extraction).where(Extraction.document_version_id == version.id))
            fields = (extraction.document_json or {}).get("extracted_fields") or []
            assert fields, name
            locators = [field["provenance"]["locator"] for field in fields if field.get("provenance")]
            assert locators, f"{name}: fields stored without provenance"
            assert all(locator["locator_kind"] == kind for locator in locators), (name, locators[0])
            if kind == "pdf":
                assert all(locator.get("page", 0) >= 1 for locator in locators)
            elif kind == "excel":
                assert all(locator.get("sheet") for locator in locators)
            elif kind == "text":
                assert all(locator.get("line_start", 0) >= 1 for locator in locators)


def test_runs_and_audit_trail_are_persisted(ingested, workspace) -> None:
    from collections import Counter

    pipeline, corpus, ids = ingested
    run(pipeline, corpus, ids)
    run(pipeline, corpus, ids)
    with workspace.database.read_only() as session:
        runs = list(session.scalars(select(IngestionRun)))
        assert len(runs) == 2
        assert all(run.counts for run in runs), "each run must store its own counts"
        actions = Counter(event.action for event in session.scalars(select(AuditEvent)))
        assert actions["document.registered"] == len(EXPECTED)
        assert actions["document.classified"] >= len(EXPECTED)
        assert actions["ingestion.run"] == 2


def test_a_run_reports_a_registry_it_cannot_trust(ingested, workspace) -> None:
    """Post-run consistency is part of the run report, not an act of faith (P0-3).

    The repository writes under the current-version invariants, so a breakage means damage
    from elsewhere - an interrupted run, a hand-edited file.  The pipeline runs the checker
    over the whole registry once per pass and reports what it finds: the run is still
    successful (the files are committed and correct), but nobody has to discover the
    inconsistency through a wrong search result.
    """
    pipeline, corpus, ids = ingested
    clean = run(pipeline, corpus, ids)
    assert clean.ok and clean.invariant_problems == [], clean.to_dict()

    with workspace.database.session() as session:
        document_id = session.execute(text("select id from document order by identity_path limit 1")).scalar_one()
        session.execute(text("update document set current_version_id = null where id = :id"), {"id": document_id})
        session.commit()

    result = run(pipeline, corpus, ids)
    assert result.ok, result.error
    assert [problem["problem"] for problem in result.invariant_problems] == ["POINTER_MISSING"], result.invariant_problems
    assert result.invariant_problems[0]["row_id"] == document_id
    assert any("registry invariant broken" in warning for warning in result.warnings), result.warnings

    # The same statement is what the UI's status bar and the repair tool read.
    with workspace.database.session() as session:
        raw = session.execute(text("select report from ingestion_run order by started_at desc limit 1")).scalar_one()
        stored = raw if isinstance(raw, dict) else json.loads(raw)
        assert [problem["problem"] for problem in stored["invariant_problems"]] == ["POINTER_MISSING"], stored
        # ...and repairing the row makes the next run clean again.
        session.execute(
            text(
                "update document set current_version_id ="
                " (select id from document_version v where v.document_id = document.id and v.is_current)"
                " where id = :id"
            ),
            {"id": document_id},
        )
        session.commit()
    assert run(pipeline, corpus, ids).invariant_problems == []


def test_the_invariant_check_is_opt_out_for_a_repair_pass(ingested, workspace) -> None:
    """A repair tool fixes rows one at a time and checks at the end; it must not re-scan."""
    _first, corpus, ids = ingested
    pipeline = IngestionPipeline(
        settings=workspace.settings,
        workspace_root=workspace.root,
        database=workspace.database,
        verify_invariants=False,
    )
    with workspace.database.session() as session:
        session.execute(text("update document set current_version_id = null"))
        session.commit()
    result = run(pipeline, corpus, ids)
    assert result.ok, result.error
    assert result.invariant_problems == [], "silence here is the configured behaviour, not a bug"
    assert not any("invariant" in warning for warning in result.warnings)
