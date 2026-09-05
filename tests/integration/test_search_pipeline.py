"""Search end-to-end: ingest real files, index them, query, and follow the citation back to the file.

This is the test that decides whether the search foundation is real.  It runs the production
pipeline over the generated corpus (genuine PDF/XLSX/DOCX/CSV/TXT), indexes what it extracted,
and then asserts the promises the brief makes:

*   a query returns ``document_id``/``version_id``/score/snippet/metadata/provenance;
*   a hit on an Excel value cites the sheet and cell, a hit on a PDF paragraph cites the page;
*   the citation is *true* - re-reading the source file at that location reproduces the text;
*   a duplicate uses the extraction cache and is still searchable;
*   a modified file's new version answers, and the superseded one stops answering;
*   a file removed from disk keeps its record searchable, and verification says so plainly;
*   a truncated extraction is findable as a diagnostic rather than presented as complete;
*   deleting the sidecar and rebuilding produces the same index - the database is authoritative,
    the index is disposable;
*   concurrent registration and concurrent index writing leave both structures consistent.

No mocks, no fake parsers, no stubbed index.
"""

from __future__ import annotations

import json
import shutil
import sqlite3
import threading
from pathlib import Path

import pytest
from sqlalchemy import select
from sqlalchemy import text as sa_text
from tests.fixtures.generate import build_corpus, build_mud_report_xlsx

from drilling_intelligence.config.settings import Settings
from drilling_intelligence.core.provenance import Provenance, verify_provenance
from drilling_intelligence.database.models import DocumentVersion
from drilling_intelligence.database.session import Database
from drilling_intelligence.documents.repository import DocumentRepository
from drilling_intelligence.extraction.normalized import NormalizedDocument
from drilling_intelligence.ingestion.pipeline import IngestionPipeline
from drilling_intelligence.search.chunking import (
    KIND_DIAGNOSTIC,
    KIND_FIELD,
    KIND_PARAGRAPH,
    KIND_TABLE_ROW,
)
from drilling_intelligence.search.index import InMemorySearchIndex, SqliteSearchIndex
from drilling_intelligence.search.service import SearchService
from drilling_intelligence.wells.repository import WellRepository

CORPUS_FILES = {
    "daily_drilling_report_well-a3.docx",
    "lesson_learned_ll-2025-014.txt",
    "mud_report_well-a3.xlsx",
    "npt_summary_2025-06.csv",
    "scanned_well_b11_report.pdf",
    "well_a3_program_rev12.pdf",
}


@pytest.fixture
def corpus_root(workspace) -> Path:
    root = workspace.root / "corpus"
    build_corpus(root)
    return root


@pytest.fixture
def ids(workspace, db):
    """Workspace, project and well rows, committed before any indexing happens."""
    with db.session() as session:
        repo = WellRepository(session)
        workspace_row = repo.get_or_create_workspace(str(workspace.root), name="North Cormorant")
        project = repo.get_or_create_project("North Cormorant")
        well = repo.create_well("A-3", project_id=project.id)
        session.commit()
        return workspace_row.id, project.id, well.id


@pytest.fixture
def pipeline(workspace):
    index = SqliteSearchIndex(workspace.index_database)
    return IngestionPipeline(settings=workspace.settings, workspace_root=workspace.root, database=workspace.database, index=index)


@pytest.fixture
def searched(workspace, corpus_root: Path, pipeline, ids):
    """One real ingestion run, then a service reading the sidecar it just wrote."""
    workspace_id, _project_id, well_id = ids
    result = pipeline.run(root=corpus_root, workspace_id=workspace_id, well_id=well_id)
    assert result.failures == 0, [item.error for item in result.failures_report()]
    return SearchService.for_workspace(workspace), corpus_root, result


class TestIngestThenSearch:
    def test_every_extracted_document_becomes_searchable(self, searched, db) -> None:
        service, _corpus, result = searched
        stats = service.stats()
        assert stats["documents"] == len(CORPUS_FILES)
        assert stats["chunks"] > 100
        assert stats["chunks"] == result.indexed_chunks
        assert stats["stale_versions"] == 0 and stats["orphaned"] == 0
        with db.session() as session:
            versions = len(list(session.scalars(select(DocumentVersion))))
        assert versions == stats["versions"], "one searchable version per registered version"

    def test_the_briefs_query_shape_works(self, searched) -> None:
        """``search(query=..., document_type=..., limit=20)`` returns a cited answer."""
        service, _corpus, _result = searched
        response = service.search(query="mud weight 10.2 ppg", document_type="MUD_REPORT", limit=20)
        assert response.ok
        assert response.results, "the mud report must answer"
        hit = response.results[0]
        assert hit.document_id and hit.version_id
        assert hit.score > 0
        assert hit.snippet
        assert hit.metadata["document_type"] == "MUD_REPORT"
        assert hit.metadata["well_name"] == "A-3"
        assert hit.metadata["sha256"] and len(hit.metadata["sha256"]) == 64
        assert hit.provenance
        payload = response.to_dict()
        assert payload["count"] == len(response.results)
        assert payload["filters"]["document_type"] == "MUD_REPORT"

    def test_a_number_the_corpus_lacks_is_never_claimed_as_a_match(self, searched) -> None:
        """The brief's example value is not in this corpus; the answer must not pretend otherwise.

        A query whose terms cannot all be satisfied in one chunk falls back to "any of them",
        and that fallback is *reported* - so a caller can tell a strict answer from a widened
        one, and no result ever lists a term it did not contain.
        """
        service, _corpus, _result = searched
        widened = service.search("mud weight 99.9 ppg")
        assert widened.broadened is True and widened.mode == "any"
        assert widened.results
        for hit in widened.results:
            assert "99.9" not in hit.matched_terms, hit
            assert "99.9" not in hit.snippet

        strict = service.search("mud weight 10.2 ppg")
        assert strict.broadened is False, "the real values satisfy the strict reading outright"
        assert "99.9" not in strict.results[0].matched_terms
        assert set(strict.results[0].matched_terms) == {"mud", "weight", "10.2", "ppg"}

        # One unmatched term alone has nothing to broaden to: it is simply no answer.
        assert service.search("99.9").results == ()

    def test_an_excel_hit_cites_the_sheet_and_cell(self, searched) -> None:
        service, _corpus, _result = searched
        response = service.search("mud weight 10.2 ppg", document_type="MUD_REPORT", limit=10)
        hit = response.results[0]
        assert hit.kind in {KIND_FIELD, KIND_TABLE_ROW}
        assert hit.sheet == "Summary"
        assert "Sheet: Summary" in hit.locator_ref
        assert "Cell" in hit.locator_ref or "Range" in hit.locator_ref
        assert hit.provenance["locator"]["kind"] == "excel"
        assert hit.provenance["filename"] == "mud_report_well-a3.xlsx"
        assert hit.page is None, "an Excel locator names a cell, not a page"

    def test_a_pdf_hit_cites_the_page(self, searched) -> None:
        service, _corpus, _result = searched
        response = service.search("casing shoe test", document_type="DRILLING_PROGRAM", limit=10)
        assert response.results
        hit = response.results[0]
        assert hit.kind == KIND_PARAGRAPH
        assert hit.page == 1
        assert hit.locator_ref.startswith("Page 1")
        assert hit.provenance["locator"]["kind"] == "pdf"
        assert hit.provenance["locator"]["page"] == 1

    def test_the_citation_is_true_for_every_result_of_a_query(self, searched, db) -> None:
        """Read the source file at the recorded location: that is the whole point of provenance."""
        service, _corpus, _result = searched
        response = service.search("mud", limit=20)
        assert len(response.results) >= 5
        with db.session() as session:
            repository = DocumentRepository(session)
            checked = 0
            for hit in response.results:
                if not hit.cited:
                    continue
                version = repository.version(hit.version_id)
                assert version is not None
                path = repository.resolve_source_path(version)
                assert path is not None, hit.citation
                outcome = verify_provenance(Path(path), Provenance.from_dict(hit.provenance))
                assert outcome.status == "MATCH", (hit.citation, outcome.status, outcome.detail)
                checked += 1
        assert checked >= 4, checked

    def test_verify_true_reports_the_check_on_the_result(self, searched) -> None:
        service, _corpus, _result = searched
        response = service.search("mud weight 10.2 ppg", limit=3, verify=True)
        assert response.results
        for hit in response.results:
            assert hit.verification is not None
            assert hit.verification["source"].endswith(hit.metadata["filename"])
            if hit.cited:
                assert hit.verification["status"] == "MATCH", hit.verification
                assert hit.verification["ok"] is True
            else:
                assert hit.verification["status"] == "NOT_CHECKABLE"

    def test_scanned_pdf_is_findable_as_a_diagnostic_and_says_it_is_uncited(self, searched) -> None:
        service, _corpus, _result = searched
        response = service.search("no text recovered", document_type="OTHER", limit=10)
        assert response.results, "an unreadable file must still be findable - that is how you fix it"
        hit = response.results[0]
        assert hit.kind == KIND_DIAGNOSTIC
        assert hit.cited is False
        assert "[document-level" in hit.citation
        assert hit.metadata["filename"] == "scanned_well_b11_report.pdf"
        assert any("no text recovered" in note.lower() for note in hit.metadata["diagnostics"])

    def test_a_diagnostic_is_damped_below_the_real_answers(self, searched) -> None:
        service, _corpus, _result = searched
        response = service.search("well report", limit=20)
        kinds = [hit.kind for hit in response.results]
        if KIND_DIAGNOSTIC in kinds:
            assert kinds.index(KIND_DIAGNOSTIC) > 0, "a note about extraction must not outrank extracted text"

    def test_indexed_text_is_the_artefacts_own_text(self, searched, db) -> None:
        """Guard: chunks are carved out of the stored artefact, not re-derived from the file."""
        service, _corpus, _result = searched
        hit = service.search("mud weight", document_type="MUD_REPORT", kinds=(KIND_PARAGRAPH,), limit=1).results[0]
        with db.session() as session:
            row = session.execute(
                sa_text("select document_json from extraction where document_version_id = :v"), {"v": hit.version_id}
            ).scalar()
        assert row is not None
        normalized = NormalizedDocument.from_dict(json.loads(row) if isinstance(row, str) else row)
        artefact = normalized.text
        # Verbatim, line by line: the chunk is text the extractor recorded, not text the search
        # layer assembled out of neighbouring structures.
        lines = [line for line in hit.text.splitlines() if line.strip()]
        assert lines
        missing = [line for line in lines if line not in artefact]
        assert not missing, missing


class TestIncrementalBehaviour:
    def test_a_second_run_is_idempotent_and_reindexes_nothing(self, searched, pipeline, corpus_root: Path, ids) -> None:
        service, _corpus, _result = searched
        workspace_id, _project, well_id = ids
        before = service.stats()
        again = pipeline.run(root=corpus_root, workspace_id=workspace_id, well_id=well_id)
        assert again.counts["UNCHANGED"] == len(CORPUS_FILES)
        assert again.counts["TO_PROCESS"] == 0
        assert again.indexed == 0
        assert again.index_removed == 0
        assert service.stats()["chunks"] == before["chunks"]

    def test_a_duplicate_uses_the_cache_without_parsing_and_is_searchable(self, searched, pipeline, corpus_root: Path, ids, db) -> None:
        service, _corpus, _result = searched
        workspace_id, _project, well_id = ids
        shutil.copy2(corpus_root / "lesson_learned_ll-2025-014.txt", corpus_root / "copy_of_lesson.txt")
        result = pipeline.run(root=corpus_root, workspace_id=workspace_id, well_id=well_id)
        by_name = {item.filename: item for item in result.results}
        assert "copy_of_lesson.txt" in by_name
        assert by_name["copy_of_lesson.txt"].from_cache is True, "the copy must reuse the cached artefact"
        assert result.from_cache >= 1
        assert result.indexed == 1 and result.indexed_chunks > 0
        # Two documents, two chunk sets: the copy is searchable under its own identity, and the
        # cache only removed the *parsing*, never the record.
        response = service.search("reaming", limit=10)
        filenames = {hit.metadata["filename"] for hit in response.results}
        assert {"lesson_learned_ll-2025-014.txt", "copy_of_lesson.txt"} <= filenames
        with db.session() as session:
            extractions = len(list(session.execute(sa_text("select id from extraction")).all()))
            versions = len(list(session.execute(sa_text("select id from document_version")).all()))
            distinct_artefacts = session.execute(
                sa_text(
                    "select count(distinct document_json) from extraction"
                    " where document_id in (select id from document where filename in"
                    " ('lesson_learned_ll-2025-014.txt', 'copy_of_lesson.txt'))"
                )
            ).scalar_one()
            sha = session.execute(
                sa_text("select sha256 from document_version where document_id = (select id from document where filename = 'copy_of_lesson.txt')")
            ).scalar_one()
            cache_rows = session.execute(
                sa_text("select count(*) from extraction_cache where content_sha256 = :sha"), {"sha": sha}
            ).scalar_one()
        assert extractions == versions, "one artefact row per version - history is never deduplicated away"
        assert distinct_artefacts == 1, "the copy's artefact is the cached one, byte for byte"
        assert cache_rows == 1, "the cache key is content-addressed, so two versions share one entry"

    def test_a_modified_file_answers_from_the_new_version_and_the_old_one_goes_quiet(
        self, searched, pipeline, corpus_root: Path, ids, db, workspace
    ) -> None:
        service, _corpus, _result = searched
        workspace_id, _project, well_id = ids
        target = corpus_root / "lesson_learned_ll-2025-014.txt"
        original = target.read_text(encoding="utf-8")
        marker = "Follow-up: the crew re-primed the pump and recovered in 40 minutes."
        target.write_text(original + "\n" + marker + "\n", encoding="utf-8")
        result = pipeline.run(root=corpus_root, workspace_id=workspace_id, well_id=well_id)
        assert result.counts["MODIFIED"] == 1
        assert result.indexed == 1
        assert result.index_removed == 1, "the superseded version leaves the searchable state in the same run"

        new_phrase = service.search("re-primed", limit=5)
        assert [hit.metadata["filename"] for hit in new_phrase.results] == ["lesson_learned_ll-2025-014.txt"]
        assert new_phrase.results[0].version_number == 2
        assert new_phrase.results[0].cited

        with db.session() as session:
            repository = DocumentRepository(session)
            document = repository.by_identity(workspace_id, f"corpus/{target.name}")
            assert document is not None, document
            versions = repository.versions_for(document.id)
            assert len(versions) == 2
            assert versions[1].is_current and not versions[0].is_current
            stale_version_id, new_version_id = versions[0].id, versions[1].id

        counts = service.stats()
        assert counts["versions"] == len(CORPUS_FILES), counts
        assert counts["stale_versions"] == 0, "the run pruned it itself; nothing is left to reconcile"
        # The history is still the registry's, with its artefact and provenance intact.
        with db.session() as session:
            repository = DocumentRepository(session)
            old = repository.version(stale_version_id)
            assert old is not None
            assert repository.extraction_for_version(old.id) is not None
        # Asking for history cannot resurrect it: pruning removed the old version's chunks, which
        # is the difference between "not current" and "not searchable".
        with workspace.index_database.engine.connect() as connection:
            stale_rows = connection.execute(
                sa_text("select count(*) from search_chunk where version_id = :v"), {"v": stale_version_id}
            ).scalar_one()
        assert stale_rows == 0
        assert {hit.version_id for hit in service.search("re-primed", include_superseded=True, limit=10).results} == {
            new_version_id
        }

    def test_a_removed_file_stays_searchable_and_verification_reports_the_source_gone(
        self, searched, pipeline, corpus_root: Path, ids
    ) -> None:
        service, _corpus, _result = searched
        workspace_id, _project, well_id = ids
        target = corpus_root / "npt_summary_2025-06.csv"
        before = service.search("back reaming", document_type="NPT", limit=5)
        assert before.results
        target.unlink()
        result = pipeline.run(root=corpus_root, workspace_id=workspace_id, well_id=well_id)
        assert [(item["filename"], item["change"]) for item in result.removed] == [("npt_summary_2025-06.csv", "REMOVED")]
        # Removal from disk is not removal from the record: the index keeps answering from the
        # artefact, and says which version it came from.
        after = service.search("back reaming", document_type="NPT", limit=5)
        assert [hit.metadata["filename"] for hit in after.results] == [hit.metadata["filename"] for hit in before.results]
        checked = service.search("back reaming", document_type="NPT", limit=5, verify=True)
        assert checked.results
        for hit in checked.results:
            assert hit.metadata["filename"] == "npt_summary_2025-06.csv"
            # Every cited hit here points into a file that no longer exists, and the result says
            # so - the index is not allowed to imply a source it cannot reach.
            assert hit.verification["status"] == "UNREADABLE", hit.verification
            assert "not reachable" in hit.verification["detail"]

    def test_a_rebuild_produces_the_same_index_as_the_incremental_path(self, searched, workspace) -> None:
        service, _corpus, _result = searched
        sidecar = workspace.index_database

        def dump() -> list[tuple]:
            with sidecar.engine.connect() as connection:
                return [
                    (row["chunk_id"], row["kind"], row["text"], str(row["page"]), row["locator_ref"], row["terms_json"])
                    for row in connection.execute(sa_text("select * from search_chunk order by chunk_id")).mappings()
                ]

        incremental = dump()
        assert incremental
        service.index.clear()
        assert service.stats()["chunks"] == 0
        stats = service.rebuild()
        assert stats["chunks"] == len(incremental)
        assert dump() == incremental, "a rebuild must reproduce the rows exactly, ids included"
        assert service.needs_rebuild() is False

    def test_the_sidecar_is_disposable_end_to_end(self, searched, workspace) -> None:
        """Delete the file, reopen the workspace, rebuild: the answers are unchanged."""
        service, _corpus, _result = searched
        query = "mud weight 10.2 ppg"
        first = [(hit.chunk_id, round(hit.score, 6), hit.locator_ref) for hit in service.search(query, limit=10).results]
        assert first
        path = workspace.index_database_path
        service.index.close()
        workspace.close()
        path.unlink()
        assert not path.exists()

        from drilling_intelligence.wells.workspace import Workspace

        reopened = Workspace.open(workspace.root, workspace.settings)
        try:
            fresh = SearchService.for_workspace(reopened)
            assert fresh.stats()["chunks"] == 0
            assert fresh.stats()["missing_versions"] == len(CORPUS_FILES)
            assert fresh.needs_rebuild() is True, "an empty index over a full registry is a rebuild away"
            fresh.rebuild()
            second = [(hit.chunk_id, round(hit.score, 6), hit.locator_ref) for hit in fresh.search(query, limit=10).results]
            assert second == first
        finally:
            reopened.close()

    def test_registry_drift_is_detected_and_repair_clears_the_flag(self, searched, workspace) -> None:
        service, _corpus, _result = searched
        assert service.needs_rebuild() is False
        with workspace.index_database.engine.connect() as connection:
            version_id = connection.execute(sa_text("select version_id from search_chunk limit 1")).scalar()
        assert version_id
        with workspace.index_database.engine.begin() as connection:
            connection.execute(sa_text("update search_document set is_current = 0 where version_id = :v"), {"v": version_id})
        with workspace.database.session() as session:
            session.execute(sa_text("update document_version set is_current = 0 where id = :v"), {"v": version_id})
            session.commit()
        assert service.needs_rebuild() is True
        stats = service.rebuild()
        assert stats["stale_versions"] == 0 and stats["orphaned"] == 0
        assert service.needs_rebuild() is False


class TestTruncation:
    def test_a_truncated_extraction_is_findable_as_a_diagnostic(self, tmp_path: Path) -> None:
        """A workbook over the cell limit is reported as partial - and search finds the report."""
        config = tmp_path / "tight.toml"
        config.write_text(
            "\n".join(
                [
                    "[app]",
                    'data_dir = ".drillintel"',
                    "",
                    "[extraction]",
                    "excel_max_cells = 40",
                    "",
                    "[ai]",
                    "enabled = false",
                    "require_ai = false",
                    "",
                    "[mineru]",
                    'mode = "disabled"',
                    "",
                ]
            ),
            encoding="utf-8",
        )
        settings = Settings.load(config)
        from drilling_intelligence.wells.workspace import Workspace

        workspace = Workspace.create(tmp_path / "tight-workspace", settings, name="Tight")
        try:
            corpus = workspace.root / "corpus"
            corpus.mkdir(parents=True, exist_ok=True)
            build_mud_report_xlsx(corpus / "wide_mud_report.xlsx")
            with workspace.database.session() as session:
                repo = WellRepository(session)
                workspace_row = repo.get_or_create_workspace(str(workspace.root), name="Tight")
                project = repo.get_or_create_project("Tight")
                well = repo.create_well("A-3", project_id=project.id)
                session.commit()
                ids = (workspace_row.id, well.id)
            pipeline = IngestionPipeline(
                settings=workspace.settings,
                workspace_root=workspace.root,
                database=workspace.database,
                index=SqliteSearchIndex(workspace.index_database),
            )
            result = pipeline.run(root=corpus, workspace_id=ids[0], well_id=ids[1])
            assert result.counts["PROCESSED"] == 1
            service = SearchService.for_workspace(workspace)
            response = service.search("EXTRACTION_TRUNCATED", limit=5)
            assert response.results, "a partial extraction must be findable, not hidden"
            hit = response.results[0]
            assert hit.kind == KIND_DIAGNOSTIC
            assert "max_cells=40" in hit.text
            assert any("EXTRACTION_TRUNCATED" in note for note in hit.metadata["diagnostics"])
            assert hit.cited is False
            # The partial extraction is still searchable for what it did read.
            assert service.search("mud weight", document_type="MUD_REPORT", limit=5).results
        finally:
            workspace.close()


class TestConcurrency:
    """Concurrent registration, version creation and index writing, over real sessions.

    Asserted after the joins, not during: the point is the resulting state, not the schedule.
    """

    @staticmethod
    def _stage(corpus_root: Path, staging: Path, count: int) -> list[Path]:
        text = (corpus_root / "lesson_learned_ll-2025-014.txt").read_text(encoding="utf-8")
        paths = []
        for index in range(count):
            path = staging / f"variant_{index}" / f"concurrent_note_{index}.txt"
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text + f"\nVariant {index}: the crew re-primed the pump and recovered.\n", encoding="utf-8")
            paths.append(path)
        return paths

    def test_parallel_runs_register_and_index_without_losing_either(
        self, searched, workspace, corpus_root: Path, ids, db
    ) -> None:
        service, _corpus, _result = searched
        workspace_id, _project, well_id = ids
        paths = self._stage(corpus_root, workspace.root / "concurrent", 4)
        errors: list[str] = []

        def worker(path: Path) -> None:
            try:
                database = Database.for_workspace(workspace.root, workspace.settings)
                try:
                    run_pipeline = IngestionPipeline(
                        settings=workspace.settings,
                        workspace_root=workspace.root,
                        database=database,
                        index=SqliteSearchIndex(workspace.index_database),
                    )
                    outcome = run_pipeline.run(root=path.parent, workspace_id=workspace_id, well_id=well_id)
                    assert outcome.failures == 0, [item.error for item in outcome.failures_report()]
                    assert outcome.counts["PROCESSED"] == 1, outcome.counts
                    assert outcome.indexed == 1, outcome.to_dict()["warnings"]
                finally:
                    database.dispose()
            except Exception as exc:  # noqa: BLE001 - collected and reported on the main thread
                errors.append(f"{path.name}: {type(exc).__name__}: {exc}")

        threads = [threading.Thread(target=worker, args=(path,)) for path in paths]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=120)
        assert not errors, errors
        assert all(not thread.is_alive() for thread in threads)

        response = service.search("re-primed", limit=20)
        assert len(response.results) == len(paths), [hit.citation for hit in response.results]
        assert len({hit.document_id for hit in response.results}) == len(paths)
        assert {hit.metadata["filename"] for hit in response.results} == {path.name for path in paths}
        with db.session() as session:
            problems = DocumentRepository(session).check_current_version_invariants()
        assert problems == [], problems

    def test_a_concurrent_rebuild_leaves_no_half_written_version(self, searched, workspace, db) -> None:
        service, _corpus, _result = searched
        with db.session() as session:
            pairs = [
                (str(row[0]), str(row[1]))
                for row in session.execute(sa_text("select id, current_version_id from document")).all()
            ]
        assert len(pairs) >= 2
        errors: list[str] = []

        def upsert(pair: tuple[str, str]) -> None:
            try:
                with workspace.database.session() as session:
                    SqliteSearchIndex(workspace.index_database).upsert(pair[0], pair[1], repository=DocumentRepository(session))
            except Exception as exc:  # noqa: BLE001
                errors.append(f"upsert {pair}: {type(exc).__name__}: {exc}")

        def rebuild() -> None:
            try:
                with workspace.database.session() as session:
                    SqliteSearchIndex(workspace.index_database).rebuild(repository=DocumentRepository(session))
            except Exception as exc:  # noqa: BLE001
                errors.append(f"rebuild: {type(exc).__name__}: {exc}")

        threads = [
            threading.Thread(target=upsert, args=(pairs[0],)),
            threading.Thread(target=rebuild, args=()),
            threading.Thread(target=upsert, args=(pairs[1],)),
            threading.Thread(target=rebuild, args=()),
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=120)
        assert not errors, errors
        assert all(not thread.is_alive() for thread in threads)

        with workspace.index_database.engine.connect() as connection:
            orphans = connection.execute(
                sa_text(
                    "select count(*) from search_chunk c left join search_document d on d.version_id = c.version_id"
                    " where d.version_id is null"
                )
            ).scalar_one()
            mismatched = connection.execute(
                sa_text(
                    "select count(*) from search_document d where d.chunk_count <>"
                    " (select count(*) from search_chunk c where c.version_id = d.version_id)"
                )
            ).scalar_one()
            rows = connection.execute(sa_text("select count(*) from search_chunk")).scalar_one()
        assert orphans == 0, "a chunk may never outlive the document row it was written with"
        assert mismatched == 0, "chunk_count is written in the same transaction as the chunks"
        assert rows == service.stats()["chunks"]

    def test_the_sidecar_holds_nothing_but_derived_tables(self, searched, workspace) -> None:
        """Guard: deleting the index can never lose a record, because it holds no records."""
        _service, _corpus, _result = searched
        path = workspace.index_database_path
        assert path.exists()
        with sqlite3.connect(path) as connection:
            names = {row[0] for row in connection.execute("select name from sqlite_master where type = 'table'")}
        assert {"search_chunk", "search_document", "search_meta"} <= names
        assert not names & {"document", "document_version", "extraction", "extraction_cache"}


class TestBackendsOnRealData:
    def test_the_in_memory_backend_answers_exactly_like_the_sqlite_one(self, searched, workspace) -> None:
        service, _corpus, _result = searched
        memory = SearchService(index=InMemorySearchIndex(), database=workspace.database)
        memory.rebuild()
        for query in (
            "mud weight 10.2 ppg",
            "stuck pipe",
            "casing shoe",
            "losses exceed",
            '"mud weight"',
            "rig up",
        ):
            expected = [(hit.chunk_id, round(hit.score, 6)) for hit in service.search(query, limit=10).results]
            actual = [(hit.chunk_id, round(hit.score, 6)) for hit in memory.search(query, limit=10).results]
            assert actual == expected, query

    def test_the_same_query_on_a_machine_without_fts5(self, searched) -> None:
        service, _corpus, _result = searched
        backend = service.index
        assert backend.fts_available(), "the fixture machine has FTS5; the parity below is the point"
        with_fts = [(hit.chunk_id, round(hit.score, 6)) for hit in service.search("mud weight 10.2 ppg", limit=10).results]
        backend._fts_ready = False
        try:
            without = [(hit.chunk_id, round(hit.score, 6)) for hit in service.search("mud weight 10.2 ppg", limit=10).results]
        finally:
            backend._fts_ready = None
        assert without == with_fts and without
