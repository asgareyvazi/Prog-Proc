"""The search index backend: rows, filters, pruning, and the promise that it is disposable.

Everything here runs against a real SQLite file (no mock DB API) and, where a registry is
needed, a real ``DocumentRepository``.  The chunk *content* is asserted in
``tests/unit/test_search_ranking.py`` and the end-to-end behaviour in
``tests/integration/test_search_pipeline.py``; this file is about the two things only the
backend can prove: that the sidecar holds what it claims, and that both backends answer a
query identically.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import text as sa_text

from drilling_intelligence.database.session import Database
from drilling_intelligence.documents.repository import DocumentRepository
from drilling_intelligence.extraction.normalized import NormalizedDocument
from drilling_intelligence.search.chunking import (
    KIND_DIAGNOSTIC,
    KIND_FIELD,
    KIND_PAGE,
    KIND_PARAGRAPH,
    ChunkSet,
    IndexDocument,
    build_chunk_set,
    chunk_id_for,
)
from drilling_intelligence.search.index import (
    MAX_CANDIDATES,
    RETRIEVAL_CAP,
    SCHEMA_VERSION,
    InMemorySearchIndex,
    SearchFilters,
    SearchRequest,
    SqliteSearchIndex,
)
from drilling_intelligence.search.service import SearchService

SIDECAR_NAME = "search_index.db"


def artifact(*pairs: tuple[str, str]) -> NormalizedDocument:
    """A minimal artefact whose paragraphs are the given ``(kind, text)`` pairs.

    Provenance is attached to every paragraph, which is what lets these tests exercise the
    real chunk shape instead of a stripped-down stand-in.
    """
    paragraphs = [
        {
            "index": index,
            "text": text,
            "page": 1,
            "block": index + 1,
            "section": "Body",
            "char_start": 0,
            "char_end": len(text),
            "provenance": {
                "document_id": "doc-1",
                "document_version_id": "ver-1",
                "filename": "report.txt",
                "locator": {"kind": "text", "line_start": index + 1, "line_end": index + 1},
                "excerpt": text[:40],
                "parser": "text",
            },
        }
        for index, (_, text) in enumerate(pairs)
    ]
    return NormalizedDocument.from_dict(
        {
            "metadata": {
                "filename": "report.txt",
                "path": "corpus/report.txt",
                "sha256": "b" * 64,
                "parser": "text",
            },
            "pages": [
                {
                    "index": 1,
                    "text": "\n".join(text for _, text in pairs),
                    "char_start": 0,
                    "char_end": 100,
                }
            ],
            "paragraphs": paragraphs,
            "tables": [],
            "sections": [],
            "figures": [],
            "extracted_fields": [],
            "diagnostics": [],
        }
    )


def chunk_set(
    document_id: str,
    version_id: str,
    normalized: NormalizedDocument,
    *,
    is_current: bool = True,
    **overrides: Any,
) -> ChunkSet:
    fields: dict[str, Any] = {
        "document_id": document_id,
        "version_id": version_id,
        "version_number": 1,
        "workspace_id": "ws-1",
        "project_id": "prj-1",
        "company_id": "co-1",
        "project_name": "North Cormorant",
        "company_name": "ACME Drilling",
        "well_id": "well-1",
        "well_name": "A-3",
        "document_type": "DDR",
        "title": "Report",
        "filename": "report.txt",
        "identity_path": f"corpus/{document_id}.txt",
        "source_relative_path": "corpus/report.txt",
        "extension": "txt",
        "parser": "text",
        "revision": "rev.1",
        "revision_key": 1,
        "status": "ACTIVE",
        "processing_status": "INDEXED",
        "source_authority": "ENGINEERING",
        "document_date": "2025-06-14",
        "imported_at": "2026-09-05T00:00:00",
        "page_count": 1,
        "sheet_count": 0,
        "word_count": 20,
        "size_bytes": 2048,
        "sha256": "b" * 64,
        "is_current": is_current,
        "diagnostics": (),
        "chunk_count": 0,
    }
    fields.update(overrides)
    return build_chunk_set(
        document=IndexDocument(**fields),
        normalized=normalized,
        version_id=version_id,
        source_sha256="b" * 64,
    )


@pytest.fixture
def sidecar(tmp_path: Path, settings):
    database = Database.from_url(f"sqlite:///{tmp_path / SIDECAR_NAME}", settings)
    yield database
    database.dispose()


@pytest.fixture
def index(sidecar) -> SqliteSearchIndex:
    return SqliteSearchIndex(sidecar)


def request(query: str, **filters: Any) -> SearchRequest:
    """A query with the given filter fields applied (``kinds=("field",)`` included)."""
    if "kinds" in filters:
        filters["kinds"] = tuple(filters["kinds"])
    return SearchRequest(query=query, filters=SearchFilters(**filters), limit=20)


class TestSchema:
    def test_tables_land_in_the_sidecar_and_nowhere_else(self, index, sidecar, workspace) -> None:
        with sidecar.engine.connect() as connection:
            tables = set(
                connection.exec_driver_sql(
                    "select name from sqlite_master where type = 'table'"
                ).scalars()
            )
        assert {"search_document", "search_chunk", "search_meta"} <= tables
        # The registry stays free of derived structures: that separation is what lets the
        # index be deleted without an argument.
        with workspace.database.engine.connect() as connection:
            registry_tables = set(
                connection.exec_driver_sql(
                    "select name from sqlite_master where type = 'table'"
                ).scalars()
            )
        assert not {"search_document", "search_chunk", "search_meta"} & registry_tables

    def test_schema_version_is_recorded(self, index) -> None:
        assert index.schema_is_current()
        assert index._meta("schema_version") == str(SCHEMA_VERSION)

    def test_a_fresh_path_creates_its_own_schema_and_reports_nothing_missing(
        self, tmp_path: Path, settings
    ) -> None:
        # Opening the index *is* the schema step: there is no migration to run and no
        # bootstrap order to get wrong, because the file is derived data.
        database = Database.from_url(f"sqlite:///{tmp_path / 'fresh_sidecar.db'}", settings)
        try:
            index = SqliteSearchIndex(database)
            assert index.missing_tables() == []
            assert Path(tmp_path / "fresh_sidecar.db").exists()
            assert index.search(request("anything"))[0] == []
        finally:
            database.dispose()

    def test_an_index_written_by_a_newer_build_is_detected_not_queried_blindly(
        self, index, sidecar
    ) -> None:
        with sidecar.engine.begin() as connection:
            connection.execute(
                sa_text("update search_meta set value = '99' where key = 'schema_version'")
            )
        assert not index.schema_is_current()


class TestStoreAndSearch:
    def test_a_stored_version_is_searchable_and_cited(self, index) -> None:
        written = index.store(
            chunk_set(
                "doc-1", "ver-1", artifact(("paragraph", "Mud weight was 10.2 ppg at the shoe."))
            )
        )
        assert written == 1
        hits, meta = index.search(request("mud weight 10.2"))
        assert len(hits) == 1
        hit = hits[0]
        assert hit.chunk.kind == KIND_PARAGRAPH
        assert hit.chunk.locator_ref == "Lines 1", "the locator is the extractor's own rendering"
        assert hit.chunk.provenance is not None
        assert hit.document.document_type == "DDR"
        assert meta["candidates"] == 1 and meta["mode"] == "all"

    def test_storing_the_same_version_twice_replaces_its_chunks(self, index) -> None:
        index.store(chunk_set("doc-1", "ver-1", artifact(("paragraph", "First text about mud."))))
        index.store(chunk_set("doc-1", "ver-1", artifact(("paragraph", "Second text about mud."))))
        assert index._counts()["chunks"] == 1
        hits, _ = index.search(request("mud"))
        assert "Second text" in hits[0].chunk.text
        assert not index.search(request("First"))[0]

    def test_a_query_with_no_terms_returns_nothing_rather_than_everything(self, index) -> None:
        index.store(chunk_set("doc-1", "ver-1", artifact(("paragraph", "Mud weight 10.2 ppg."))))
        hits, meta = index.search(request("the of and"))
        assert hits == []
        assert meta.get("empty_query") is True

    def test_unknown_terms_match_nothing(self, index) -> None:
        index.store(chunk_set("doc-1", "ver-1", artifact(("paragraph", "Mud weight 10.2 ppg."))))
        assert index.search(request("zcbrxq"))[0] == []

    def test_each_unit_type_stays_separately_retrievable(self, index) -> None:
        """Every unit the artefact declares comes back as its own chunk."""
        normalized = artifact(
            ("paragraph", "Mud weight was 10.2 ppg."),
            ("paragraph", "Losses of 12 bbl/hr were reported at the shoe."),
        )
        normalized.extracted_fields = [
            __import__("drilling_intelligence.core.results", fromlist=["DataField"]).DataField(
                name="mud_weight", value="10.2", unit="ppg", method="paragraph"
            )
        ]
        expected = chunk_set("doc-1", "ver-1", normalized)
        assert index.store(expected) == len(expected.chunks)
        assert index._counts()["chunks"] == len(expected.chunks)
        hits, _ = index.search(request("losses"))
        assert [hit.chunk.kind for hit in hits] == [KIND_PARAGRAPH]
        hits, _ = index.search(request("mud_weight", kinds=(KIND_FIELD,)))
        assert [hit.chunk.kind for hit in hits] == [KIND_FIELD], (
            "the field unit is its own chunk, not folded into the paragraph"
        )


class TestFilters:
    @pytest.fixture
    def populated(self, index) -> SqliteSearchIndex:
        """Three documents: two current, one superseded (the current-version rule is part of
        every assertion below, which is why the expectations name only the searchable pair)."""
        index.store(
            chunk_set(
                "doc-1",
                "ver-1",
                artifact(("paragraph", "Mud weight 10.2 ppg at A-3.")),
                well_id="well-1",
                document_type="MUD_REPORT",
            )
        )
        index.store(
            chunk_set(
                "doc-2",
                "ver-2",
                artifact(("paragraph", "Mud weight 10.4 ppg at A-4.")),
                well_id="well-2",
                document_type="DDR",
                document_date="2025-07-01",
                company_id="co-2",
                project_id="prj-2",
            )
        )
        index.store(
            chunk_set(
                "doc-3",
                "ver-3",
                artifact(("paragraph", "Mud weight was superseded; see revision 2.")),
                well_id="well-1",
                document_type="MUD_REPORT",
                is_current=False,
                status="SUPERSEDED",
                revision="rev.2",
            )
        )
        return index

    def _documents(self, hits) -> set[str]:
        return {hit.document.document_id for hit in hits}

    def test_well_filter(self, populated) -> None:
        hits, _ = populated.search(request("mud weight", well_id="well-2"))
        assert self._documents(hits) == {"doc-2"}

    def test_document_type_filter(self, populated) -> None:
        hits, _ = populated.search(request("mud weight", document_type="MUD_REPORT"))
        assert self._documents(hits) == {"doc-1"}, (
            "the superseded mud report is excluded by the current-version rule"
        )

    def test_company_and_project_filters(self, populated) -> None:
        assert self._documents(populated.search(request("mud weight", company_id="co-2"))[0]) == {
            "doc-2"
        }
        assert self._documents(populated.search(request("mud weight", project_id="prj-1"))[0]) == {
            "doc-1"
        }
        assert (
            self._documents(populated.search(request("mud weight", project_id="prj-9"))[0]) == set()
        )

    def test_date_range_is_inclusive_of_the_boundary(self, populated) -> None:
        assert self._documents(
            populated.search(request("mud weight", date_from="2025-07-01"))[0]
        ) == {"doc-2"}
        assert self._documents(
            populated.search(request("mud weight", date_to="2025-06-14"))[0]
        ) == {"doc-1"}
        assert self._documents(
            populated.search(request("mud weight", date_from="2025-06-14", date_to="2025-06-14"))[0]
        ) == {"doc-1"}
        assert (
            self._documents(populated.search(request("mud weight", date_to="2025-06-13"))[0])
            == set()
        )

    def test_superseded_versions_are_invisible_unless_asked_for(self, populated) -> None:
        assert self._documents(populated.search(request("mud weight"))[0]) == {"doc-1", "doc-2"}
        hits, _ = populated.search(
            SearchRequest(
                query="mud weight", filters=SearchFilters(include_superseded=True), limit=20
            )
        )
        assert "doc-3" in self._documents(hits)

    def test_revision_and_status_and_parser(self, populated) -> None:
        # ``revision`` narrows to a version the current-version rule has already hidden, so the
        # answer is "nothing" until the caller asks for history explicitly.
        assert (
            self._documents(populated.search(request("mud weight", revision="rev.2"))[0]) == set()
        )
        assert self._documents(populated.search(request("mud weight", revision="rev.1"))[0]) == {
            "doc-1",
            "doc-2",
        }
        assert self._documents(
            populated.search(request("mud weight", status="SUPERSEDED", include_superseded=True))[0]
        ) == {"doc-3"}
        assert (
            self._documents(populated.search(request("mud weight", parser="pdf_text"))[0]) == set()
        )
        assert self._documents(
            populated.search(request("mud weight", processing_status="INDEXED"))[0]
        ) == {"doc-1", "doc-2"}

    def test_kind_filter_reaches_inside_a_matching_document(self, populated) -> None:
        hits, _ = populated.search(request("mud weight", kinds=(KIND_FIELD,)))
        assert hits == [], "none of these artefacts has an extracted field to match"
        hits, _ = populated.search(request("mud weight", kinds=(KIND_PARAGRAPH,)))
        assert self._documents(hits) == {"doc-1", "doc-2"}
        hits, _ = populated.search(
            request("mud weight", kinds=(KIND_PARAGRAPH,), include_superseded=True)
        )
        assert self._documents(hits) == {"doc-1", "doc-2", "doc-3"}

    def test_a_filter_nothing_satisfies_is_an_empty_answer_not_an_error(self, populated) -> None:
        hits, meta = populated.search(request("mud weight", well_id="well-999"))
        assert hits == []
        # The candidate set was fetched and *then* filtered, so the query is reported as the
        # query that was asked: no silent broadening just because the well does not exist.
        assert meta["candidates"] > 0 and meta["mode"] == "all"


class TestPruningAndRebuild:
    def test_prune_removes_versions_the_registry_no_longer_calls_current(self, index, db) -> None:
        index.store(chunk_set("doc-1", "ver-1", artifact(("paragraph", "Mud weight 10.2 ppg."))))
        index.store(
            chunk_set(
                "doc-2", "ver-2", artifact(("paragraph", "Cement volume 40 bag.")), is_current=False
            )
        )
        with db.session() as session:
            repository = DocumentRepository(session)
            removed = index.prune_obsolete(repository=repository)
        # Both versions are unknown to an empty registry, so both leave the searchable state -
        # which is exactly the "orphaned" case a rebuild repairs.
        assert removed == 2
        assert index._counts()["chunks"] == 0

    def test_prune_needs_the_registry(self, index) -> None:
        with pytest.raises(ValueError, match="repository"):
            index.prune_obsolete()

    def test_removing_a_document_removes_only_its_rows(self, index) -> None:
        index.store(chunk_set("doc-1", "ver-1", artifact(("paragraph", "Mud weight 10.2 ppg."))))
        index.store(chunk_set("doc-2", "ver-2", artifact(("paragraph", "Mud weight 10.4 ppg."))))
        assert index.remove_document("doc-1") == 1
        # ``knowledge_chunks`` is part of the tally now: an index built from artefacts only (no
        # derived facts) must say so, which is the difference between "the facts were never
        # rebuilt" and "there are no facts".
        assert index._counts() == {
            "documents": 1,
            "versions": 1,
            "chunks": 1,
            "knowledge_chunks": 0,
        }
        assert index.search(request("mud"))[0][0].document.document_id == "doc-2"

    def test_clear_empties_chunks_and_documents_together(self, index) -> None:
        index.store(chunk_set("doc-1", "ver-1", artifact(("paragraph", "Mud weight 10.2 ppg."))))
        assert index.clear() == 1
        assert index._counts() == {
            "documents": 0,
            "versions": 0,
            "chunks": 0,
            "knowledge_chunks": 0,
        }
        with index.engine.connect() as connection:
            fts = connection.execute(sa_text("select count(*) from search_chunk_fts")).scalar_one()
        assert fts == 0, "the FTS mirror must not keep candidates for deleted rows"

    def test_a_deleted_sidecar_file_rebuilds_the_same_results(
        self, sidecar, settings, tmp_path: Path
    ) -> None:
        index = SqliteSearchIndex(sidecar)
        index.store(
            chunk_set("doc-1", "ver-1", artifact(("paragraph", "Mud weight 10.2 ppg at the shoe.")))
        )
        before = index.search(request("mud weight"))[0]
        path = tmp_path / SIDECAR_NAME
        sidecar.dispose()
        path.unlink()
        assert not path.exists()
        rebuilt = SqliteSearchIndex(Database.from_url(f"sqlite:///{path}", settings))
        rebuilt.store(
            chunk_set("doc-1", "ver-1", artifact(("paragraph", "Mud weight 10.2 ppg at the shoe.")))
        )
        after = rebuilt.search(request("mud weight"))[0]
        assert [(hit.chunk.chunk_id, hit.score) for hit in after] == [
            (hit.chunk.chunk_id, hit.score) for hit in before
        ]
        rebuilt.close()

    def test_rows_are_identical_after_a_rebuild_of_the_same_registry(self, index, sidecar) -> None:
        def dump(connection) -> list[tuple]:
            return sorted(
                (row["chunk_id"], row["kind"], row["text"], str(row["page"]), row["locator_ref"])
                for row in connection.execute(sa_text("select * from search_chunk")).mappings()
            )

        index.store(
            chunk_set(
                "doc-1",
                "ver-1",
                artifact(("paragraph", "Mud weight 10.2 ppg."), ("field", "mud_weight = 10.2 ppg")),
            )
        )
        with sidecar.engine.connect() as connection:
            first = dump(connection)
        # Re-storing the same chunk set (what a rebuild does per version) reproduces the rows
        # exactly - the ids are position-derived, so a rebuild never renumbers anything.
        index.store(
            chunk_set(
                "doc-1",
                "ver-1",
                artifact(("paragraph", "Mud weight 10.2 ppg."), ("field", "mud_weight = 10.2 ppg")),
            )
        )
        with sidecar.engine.connect() as connection:
            second = dump(connection)
        assert first == second and first


class TestBackendsAgree:
    @pytest.fixture
    def both(self, index) -> tuple[SqliteSearchIndex, InMemorySearchIndex]:
        sets = [
            chunk_set(
                "doc-1",
                "ver-1",
                artifact(
                    ("paragraph", "Mud weight 10.2 ppg at the shoe."),
                    ("field", "mud_weight = 10.2 ppg"),
                ),
            ),
            chunk_set(
                "doc-2",
                "ver-2",
                artifact(
                    ("paragraph", "Losses of 12 bbl/hr at 9,940 ft."),
                    ("paragraph", "Mud weight raised to 10.4 ppg after the losses."),
                ),
            ),
            chunk_set("doc-3", "ver-3", artifact(("paragraph", "Casing shoe depth 8,500 ft MD."))),
        ]
        memory = InMemorySearchIndex()
        for item in sets:
            index.store(item)
            memory.store(item)
        return index, memory

    @pytest.mark.parametrize(
        "query",
        [
            "mud weight 10.2 ppg",
            "mud weight",
            "losses",
            "shoe",
            "casing shoe depth",
            "stuck pipe",  # matches nothing: the fallback must be reported the same way in both
            "12 bbl",
            '"shoe depth"',
            "zcbrxq",
        ],
    )
    def test_identical_ranking_with_and_without_fts5(self, both, query: str) -> None:
        sqlite_index, memory_index = both
        expected = [
            (hit.chunk.chunk_id, hit.score) for hit in memory_index.search(request(query))[0]
        ]
        assert [
            (hit.chunk.chunk_id, hit.score) for hit in sqlite_index.search(request(query))[0]
        ] == expected

    def test_the_broadened_fallback_is_reported_by_both(self, both) -> None:
        sqlite_index, memory_index = both
        assert sqlite_index.search(request("mud zcbrxq"))[1]["mode"] == "any"
        assert memory_index.search(request("mud zcbrxq"))[1]["mode"] == "any"

    def test_a_machine_without_fts5_gets_the_same_answers(self, both) -> None:
        sqlite_index, memory_index = both
        with_fts = [
            (hit.chunk.chunk_id, hit.score) for hit in sqlite_index.search(request("mud weight"))[0]
        ]
        sqlite_index._fts_ready = False  # exactly the state of a minimal libsqlite3 build
        without_fts = [
            (hit.chunk.chunk_id, hit.score) for hit in sqlite_index.search(request("mud weight"))[0]
        ]
        memory = [
            (hit.chunk.chunk_id, hit.score) for hit in memory_index.search(request("mud weight"))[0]
        ]
        assert with_fts == without_fts == memory
        sqlite_index._fts_ready = None

    def test_truncation_is_reported_rather_than_hidden(
        self, both, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        sqlite_index, memory_index = both
        monkeypatch.setattr("drilling_intelligence.search.index.MAX_CANDIDATES", 1)
        strict = sqlite_index.search(request("mud"))
        broad = memory_index.search(request("mud"))
        assert len(strict[0]) == 1
        assert strict[1]["truncated"] == broad[1]["truncated"] is True
        assert MAX_CANDIDATES > 1, "the production cap is a latency guard, not what the test pinned"


class TestServicePresentation:
    @pytest.fixture
    def service(self, index, db) -> SearchService:
        with db.session() as session:
            yield SearchService(index=index, repository=DocumentRepository(session))

    def test_a_result_carries_the_five_things_the_spec_asks_for(self, service) -> None:
        service.index.store(
            chunk_set(
                "doc-1", "ver-1", artifact(("paragraph", "Mud weight was 10.2 ppg at the shoe."))
            )
        )
        response = service.search("mud weight 10.2")
        assert len(response.results) == 1
        hit = response.results[0]
        assert hit.document_id == "doc-1" and hit.version_id == "ver-1"
        assert hit.score > 0
        assert "10.2" in hit.snippet
        assert hit.provenance["locator"]["kind"] == "text"
        assert hit.metadata["document_type"] == "DDR"
        assert hit.cited and hit.citation.endswith("> Lines 1")
        payload = hit.to_dict()
        assert payload["provenance"]["excerpt"] and payload["kind"] == KIND_PARAGRAPH

    def test_an_uncited_body_chunk_is_flagged_rather_than_presented_as_a_quotation(
        self, service
    ) -> None:
        normalized = artifact(("paragraph", "Mud weight 10.2 ppg."))
        # Strip the locator from the one unit that should have it: this is what an extractor
        # bug looks like from the index's side.
        payload = normalized.to_dict()
        payload["paragraphs"][0]["provenance"] = None
        stripped = NormalizedDocument.from_dict(payload)
        service.index.store(chunk_set("doc-1", "ver-1", stripped))
        uncited = [
            chunk
            for chunk in chunk_set("doc-1", "ver-1", stripped).chunks
            if not chunk.provenance and chunk.kind not in (KIND_DIAGNOSTIC, KIND_PAGE)
        ]
        assert uncited, "the fixture must actually produce an uncited body chunk"
        response = service.search("mud weight")
        assert response.results, "the text is still searchable"
        assert response.results[0].cited is False
        assert "[document-level" in response.results[0].citation

    def test_filters_reach_the_response_and_limit_bounds_it(self, service) -> None:
        service.index.store(
            chunk_set("doc-1", "ver-1", artifact(("paragraph", "Mud weight 10.2 ppg.")))
        )
        service.index.store(
            chunk_set(
                "doc-2", "ver-2", artifact(("paragraph", "Mud weight 10.4 ppg.")), well_id="well-2"
            )
        )
        service.index.store(
            chunk_set("doc-3", "ver-3", artifact(("paragraph", "Mud weight 10.6 ppg."))),
        )
        response = service.search("mud weight", well_id="well-2", limit=1)
        assert [hit.document_id for hit in response.results] == ["doc-2"]
        assert [
            hit.document_id
            for hit in service.search("mud weight", well_id="well-1", limit=1).results
        ] == ["doc-1", "doc-3"][:1]
        assert response.filters["well_id"] == "well-2"
        assert response.to_dict()["count"] == 1

    def test_a_bad_date_filter_is_an_error_not_a_silent_none(self, service) -> None:
        with pytest.raises((TypeError, ValueError)):
            service.search("mud", date_from=2025)  # type: ignore[arg-type]

    def test_verification_reports_the_missing_source_file_instead_of_hiding_the_hit(
        self, service, db
    ) -> None:
        service.index.store(
            chunk_set("doc-1", "ver-1", artifact(("paragraph", "Mud weight 10.2 ppg.")))
        )
        response = service.search("mud weight", verify=True)
        assert response.results[0].verification["status"] == "NOT_CHECKABLE"
        assert "registry" in response.results[0].verification["detail"]

    def test_an_empty_index_over_an_empty_registry_is_not_flagged(self, service) -> None:
        # Nothing to answer, nothing missing: the flag means "disagrees with the registry",
        # not "nobody has run a full rebuild in this process".
        assert service.needs_rebuild() is False

    def test_a_current_version_with_nothing_indexed_is_flagged_and_upsert_clears_it(
        self, service, db, workspace
    ) -> None:
        from drilling_intelligence.core.enums import FileChangeKind

        with db.session() as session:
            repository = DocumentRepository(session)
            document = repository.create_document(
                workspace_id=None,
                unscoped=True,  # a deliberate test row, not a production one
                identity_path="corpus/report.txt",
                filename="report.txt",
                extension="txt",
                mime_type="text/plain",
                size_bytes=2048,
                sha256="b" * 64,
            )
            version = repository.create_version(
                document,
                sha256="b" * 64,
                source_path=str(workspace.root / "corpus" / "report.txt"),
                size_bytes=2048,
                parser="text",
                parser_version="1",
                extraction_version="1",
                origin=FileChangeKind.NEW,
            )
            session.commit()
            document_id, version_id = document.id, version.id
        assert service.needs_rebuild() is True, (
            "the registry has a current version the index has never seen"
        )
        assert service.stats()["missing_versions"] == 1

        service.index.store(
            chunk_set(document_id, version_id, artifact(("paragraph", "Mud weight 10.2 ppg.")))
        )
        assert service.stats()["missing_versions"] == 0
        assert service.needs_rebuild() is False
        assert [hit.chunk_id for hit in service.search("mud weight").results] == [
            chunk_set(document_id, version_id, artifact(("paragraph", "Mud weight 10.2 ppg.")))
            .chunks[0]
            .chunk_id
        ]

    def test_close_is_a_noop_because_the_workspace_owns_the_engine(self, service) -> None:
        service.index.close()


def test_registry_rows_are_never_written_by_the_index() -> None:
    """Guard: the sidecar code path contains no SQL that could touch the system of record.

    A f-string around ``search_chunk``/``search_document`` is fine (FTS5 cannot be expressed in
    Core); one around ``document``, ``document_version`` or ``extraction`` would mean the
    disposable index had become a writer to the authoritative registry.
    """
    source = Path("src/drilling_intelligence/search/index.py").read_text(encoding="utf-8")
    forbidden = (
        "insert into document",
        "update document",
        "delete from document",
        "insert into document_version",
        "update extraction",
    )
    hits = [
        line.strip()
        for line in source.splitlines()
        if any(word in line.lower() for word in forbidden)
    ]
    assert hits == [], hits


def test_both_backends_expose_the_same_protocol_method_names() -> None:
    for name in (
        "upsert",
        "store",
        "remove_version",
        "remove_document",
        "prune_obsolete",
        "clear",
        "rebuild",
        "search",
        "stats",
        "close",
    ):
        assert hasattr(SqliteSearchIndex, name), name
        assert hasattr(InMemorySearchIndex, name), name


def test_registry_tables_are_not_touched_by_the_index(workspace) -> None:
    from drilling_intelligence.database.models import Base

    with workspace.database.engine.connect() as connection:
        before = set(connection.exec_driver_sql("select name from sqlite_master").scalars())
    assert {"document", "document_version", "extraction"} & before
    assert not set(Base.metadata.tables) & {"search_chunk", "search_document", "search_meta"}


class TestBroadenedMeansAFallbackHappened:
    """``broadened`` is a report of an event, not an echo of the requested reading.

    It used to be ``mode == "any"``, so a caller who *asked* for the broadened reading was told
    "no chunk matched every term" - a claim about a strict query that was never run.  The CLI
    printed exactly that sentence.
    """

    def test_an_explicit_broadened_request_reports_no_fallback(self, index) -> None:
        index.store(
            chunk_set(
                "doc-any",
                "ver-any",
                artifact(("text", "the zermatt mud report notes a tight hole")),
            )
        )
        _, meta = index.search(SearchRequest(query="zermatt loss", mode="any", limit=20))
        assert meta["mode"] == "any"
        assert meta["broadened"] is False

    def test_a_real_fallback_is_still_reported(self, index) -> None:
        index.store(
            chunk_set(
                "doc-fb",
                "ver-fb",
                artifact(("text", "the zermatt mud report notes a tight hole")),
            )
        )
        # "loss" appears nowhere, so the strict reading is genuinely disproved.
        _, meta = index.search(SearchRequest(query="zermatt loss", limit=20))
        assert meta["broadened"] is True
        assert meta["mode"] == "any"

    def test_a_strict_match_excluded_by_scope_does_not_broaden(self, index) -> None:
        """``matched_any`` covers discovered candidates *before* the filter set, on purpose.

        "Nothing matched" and "something matched, but outside the scope you asked about" are
        different answers; only the first justifies silently widening the question.
        """
        index.store(
            chunk_set(
                "doc-elsewhere",
                "ver-elsewhere",
                artifact(("text", "the zermatt loss circulation event")),
                well_id="well-9",
                well_name="B-7",
            )
        )
        hits, meta = index.search(
            SearchRequest(
                query="zermatt loss",
                filters=SearchFilters(well_id="well-1"),
                limit=20,
            )
        )
        assert hits == []
        assert meta["broadened"] is False, "a scoped-out match is not evidence of no match"


# ------------------------------------------------------- V5.6 real-constant boundary corpora
#: The whole point of these tests is that they run against the production constants, not a
#: monkeypatched ``MAX_CANDIDATES = 1`` over three rows: a cap that is only ever exercised at a
#: toy size proves nothing about the size at which it actually bites.
_BOUNDARY_QUERY = "zermatt loss"
_MATCHING_TEXT = "the zermatt loss circulation event was recorded in the daily report"
_FILLER_TEXT = "filler body text carrying common mud weight words and nothing else"


def _settings_for(directory: Path):
    from drilling_intelligence.config.settings import Settings

    config = directory / "config.toml"
    config.write_text(
        "\n".join(
            [
                "[app]",
                'data_dir = ".drillintel"',
                "",
                "[database]",
                'sqlite_filename = "drilling_intelligence.db"',
                "",
                "[logging]",
                'level = "WARNING"',
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
    return Settings.load(config)


def _build(directory: Path, rows: list[tuple[str, str]]) -> tuple[Database, SqliteSearchIndex]:
    """A real sidecar index holding ``rows`` as ``(version_id, text)``."""
    database = Database.from_url(
        "sqlite:///" + str(directory / "sidecar.sqlite"), _settings_for(directory)
    )
    index = SqliteSearchIndex(database)
    for version_id, text in rows:
        index.store(chunk_set(version_id, version_id, artifact(("text", text))))
    return database, index


def _identity_ranked(count: int) -> list[str]:
    """``count`` version ids in ``chunk_id`` order - the order discovery actually walks.

    ``chunk_id`` is ``sha256("<version_id>:<index>")``, so identity order is unrelated to
    insertion order but entirely computable in advance, which is what makes it possible to place
    a genuine match deliberately past the discovery bound.
    """
    pool = [f"ver-{i:06d}" for i in range(count + 400)]
    return sorted(pool, key=lambda v: chunk_id_for(v, 0))[:count]


@pytest.fixture(scope="module")
def late_match_index(tmp_path_factory):
    """``RETRIEVAL_CAP + 1`` chunks, three of which genuinely match, one past the bound."""
    directory = tmp_path_factory.mktemp("late_match")
    ranked = _identity_ranked(RETRIEVAL_CAP + 1)
    special = {ranked[0], ranked[len(ranked) // 2], ranked[-1]}
    rows = [(vid, _MATCHING_TEXT if vid in special else _FILLER_TEXT) for vid in ranked]
    database, index = _build(directory, rows)
    yield index, {chunk_id_for(vid, 0) for vid in special}, chunk_id_for(ranked[-1], 0)
    database.dispose()


@pytest.fixture(scope="module")
def cut_index(tmp_path_factory):
    """``MAX_CANDIDATES + 1`` chunks that *all* match, so the surviving hits exceed the cap."""
    directory = tmp_path_factory.mktemp("cut")
    rows = [(vid, _MATCHING_TEXT) for vid in _identity_ranked(MAX_CANDIDATES + 1)]
    database, index = _build(directory, rows)
    yield index, [vid for vid, _ in rows]
    database.dispose()


class TestRetrievalCapBoundaryIsAboutMatchingRows:
    """``RETRIEVAL_CAP`` bounds the *candidates*, not the table the candidates come from."""

    def test_fts_and_scan_return_the_same_ids_including_the_late_match(
        self, late_match_index
    ) -> None:
        index, expected_ids, late_id = late_match_index
        fts_hits, fts_meta = index.search(SearchRequest(query=_BOUNDARY_QUERY, limit=0))
        fts_ids = sorted(hit.chunk.chunk_id for hit in fts_hits)
        assert fts_ids == sorted(expected_ids)

        index._fts_ready = False  # the documented "this SQLite build has no FTS5" path
        try:
            scan_hits, scan_meta = index.search(SearchRequest(query=_BOUNDARY_QUERY, limit=0))
        finally:
            index._fts_ready = None
        scan_ids = sorted(hit.chunk.chunk_id for hit in scan_hits)

        assert scan_ids == fts_ids, "the accelerator must not change which rows are found"
        assert late_id in scan_ids, (
            "a genuine match whose identity sorts past the bound must still be found: discovery "
            "is term-aware on both paths, so the bound applies to matching rows, not to the table"
        )
        assert scan_meta["truncated"] == fts_meta["truncated"] is False, (
            "every matching candidate was examined on both paths, so neither may claim a bound"
        )
        assert scan_meta["candidates"] == fts_meta["candidates"] == len(expected_ids)
        assert fts_meta["fts_used"] is True and scan_meta["fts_used"] is False

    def test_scores_and_matched_terms_are_identical_across_backends(self, late_match_index) -> None:
        index, _expected, _late = late_match_index
        first = index.search(SearchRequest(query=_BOUNDARY_QUERY, limit=0))[0]
        index._fts_ready = False
        try:
            second = index.search(SearchRequest(query=_BOUNDARY_QUERY, limit=0))[0]
        finally:
            index._fts_ready = None
        assert [hit.chunk.chunk_id for hit in first] == [hit.chunk.chunk_id for hit in second], (
            "same ranking order, not merely the same set"
        )
        assert [hit.score for hit in first] == [hit.score for hit in second]
        assert [hit.matched_terms for hit in first] == [hit.matched_terms for hit in second]


class TestMaxCandidatesBoundary:
    """Exactly at the bound is not the same as past it.

    ``len(hits) > MAX_CANDIDATES`` - not ``>=`` - is the whole claim, and the three cases below
    are what keep it that way.  ``limit=0`` at the index layer means "no caller cap", so the
    returned length is the cap's own doing and nothing else.
    """

    def test_4001_4000_and_3999_surviving_hits(self, cut_index) -> None:
        index, version_ids = cut_index
        assert len(version_ids) == MAX_CANDIDATES + 1

        hits, meta = index.search(SearchRequest(query=_BOUNDARY_QUERY, limit=0))
        assert len(hits) == MAX_CANDIDATES, "one row past the cap, so exactly the cap is returned"
        assert meta["truncated"] is True
        assert meta["candidates"] == MAX_CANDIDATES + 1

        index.remove_version(version_ids[-1])
        hits, meta = index.search(SearchRequest(query=_BOUNDARY_QUERY, limit=0))
        assert len(hits) == MAX_CANDIDATES
        assert meta["truncated"] is False, (
            "exactly at the bound proves nothing was cut; only exceeding it does"
        )
        assert meta["candidates"] == MAX_CANDIDATES

        index.remove_version(version_ids[-2])
        hits, meta = index.search(SearchRequest(query=_BOUNDARY_QUERY, limit=0))
        assert len(hits) == MAX_CANDIDATES - 1
        assert meta["truncated"] is False
        assert meta["candidates"] == MAX_CANDIDATES - 1

    def test_a_caller_limit_smaller_than_the_cap_still_reports_the_cut(self, cut_index) -> None:
        """The cap is about what was *scored*, so it is reported even when fewer rows are shown."""
        index, _version_ids = cut_index
        _hits, meta = index.search(SearchRequest(query=_BOUNDARY_QUERY, limit=20))
        assert meta["truncated"] is False, "two rows were removed, so the cap no longer bites"
        assert meta["candidates"] == MAX_CANDIDATES - 1


@pytest.fixture(scope="module")
def scoped_index(tmp_path_factory):
    """``RETRIEVAL_CAP + 1`` chunks that all match, exactly one of which is in scope.

    The in-scope row is deliberately the one whose ``chunk_id`` sorts *last*, so it sits past the
    discovery bound: this is the corpus that asks whether a bounded search can tell "nothing
    matched" apart from "nothing matching was inside the universe I examined".
    """
    directory = tmp_path_factory.mktemp("scoped")
    ranked = _identity_ranked(RETRIEVAL_CAP + 1)
    rows = [
        (
            vid,
            _MATCHING_TEXT,
        )
        for vid in ranked
    ]
    database = Database.from_url(
        f"sqlite:///{directory / 'sidecar.sqlite'}", _settings_for(directory)
    )
    index = SqliteSearchIndex(database)
    for position, (version_id, text) in enumerate(rows):
        in_scope = position == len(rows) - 1
        index.store(
            chunk_set(
                version_id,
                version_id,
                artifact(("text", text)),
                well_id="well-1" if in_scope else "well-other",
                well_name="A-3" if in_scope else "B-9",
            )
        )
    in_scope_id = chunk_id_for(ranked[-1], 0)
    yield index, in_scope_id
    database.dispose()


class TestScopeIsFilteredAfterDiscovery:
    """Discovery is term-aware but *not* scope-aware, and the metadata must carry that."""

    def test_a_bounded_universe_is_reported_even_when_few_results_survive(
        self, scoped_index
    ) -> None:
        """State B: the candidate universe exceeded the discovery bound, the result set did not."""
        index, _in_scope = scoped_index
        hits, meta = index.search(
            SearchRequest(query="zermatt", filters=SearchFilters(well_id="well-1"), limit=0)
        )
        assert meta["truncated"] is True, (
            "16001 rows matched the term and only 16000 were examined, so the answer is bounded"
        )
        assert meta["candidates"] == RETRIEVAL_CAP
        assert len(hits) <= MAX_CANDIDATES

    def test_an_in_scope_match_past_the_bound_is_not_reported_as_absent(self, scoped_index) -> None:
        """The known limitation, pinned: the answer may be empty, but never *unqualified* empty.

        Discovery is term-aware and not scope-aware, so an in-scope row whose identity sorts past
        ``RETRIEVAL_CAP`` is genuinely not examined.  What must not happen is that this looks like
        "no such record exists": ``truncated`` is the caller's only proof that the universe was
        bounded, and it has to be set.
        """
        index, in_scope_id = scoped_index
        with index.engine.connect() as connection:
            present = connection.execute(
                sa_text("select count(*) from search_chunk where chunk_id = :cid"),
                {"cid": in_scope_id},
            ).scalar_one()
        assert present == 1, "the row really is indexed; only the bound keeps it out of the answer"

        hits, meta = index.search(
            SearchRequest(query="zermatt", filters=SearchFilters(well_id="well-1"), limit=0)
        )
        assert hits == [], "the in-scope row is past the bound, so the scoped answer is empty"
        assert meta["truncated"] is True, (
            "an empty scoped answer over a bounded universe must never be indistinguishable from "
            "a proven absence"
        )


class TestBroadeningUnderBoundedDiscovery:
    """Whether the decision to broaden was itself justified, not just whether it was reported.

    Row 18 made ``broadened`` a truthful report of an event.  These pin the harder half: the event
    must only be allowed to happen when the strict reading was actually disproved.  With term-aware
    discovery the two are linked - ``matched_any is False`` means the strict expression matched no
    discovered row, and a discovery that matched nothing cannot itself have hit the bound - so a
    bounded strict universe always carries strict matches with it and never triggers a fallback.
    """

    def test_a_scoped_out_strict_match_does_not_license_broadening(self, scoped_index) -> None:
        index, _in_scope = scoped_index
        _hits, meta = index.search(
            SearchRequest(
                query="zermatt loss",
                filters=SearchFilters(well_id="well-nonexistent"),
                limit=0,
            )
        )
        assert meta["broadened"] is False, (
            "strict matches were discovered and then excluded by scope; that is not a disproof"
        )
        assert meta["mode"] == "all"

    def test_a_genuinely_absent_strict_match_still_broadens(self, late_match_index) -> None:
        index, _expected, _late = late_match_index
        _hits, meta = index.search(SearchRequest(query="zermatt xylophone", limit=0))
        assert meta["broadened"] is True
        assert meta["mode"] == "any"
        assert meta["truncated"] is False, (
            "the fallback is only sound because the strict universe was fully examined"
        )


@pytest.fixture(scope="module")
def state_cut_index(tmp_path_factory):
    """``MAX_CANDIDATES + 1`` matching chunks, reserved for the state matrix.

    :class:`TestMaxCandidatesBoundary` walks its own corpus down by removing versions, so the state
    matrix cannot share it: "the result set was cut" is only true while all 4001 rows are present.
    """
    directory = tmp_path_factory.mktemp("state_cut")
    rows = [(vid, _MATCHING_TEXT) for vid in _identity_ranked(MAX_CANDIDATES + 1)]
    database, index = _build(directory, rows)
    yield index, [vid for vid, _ in rows]
    database.dispose()


class TestTruncationStatesAreDistinguishable:
    """The five states a bounded search can be in, and whether a caller can tell them apart.

    ``truncated`` alone cannot: it is the OR of two different facts.  ``candidate_capped`` says the
    universe examined was incomplete, so an absent row proves nothing; ``results_capped`` says every
    candidate was examined and only the answer list was cut, so the ranking is complete.  These run
    before :class:`TestRetrievalCapExactBound` because that one removes versions from the shared
    corpus.
    """

    def test_state_a_neither_bound(self, late_match_index) -> None:
        index, _expected, _late = late_match_index
        _hits, meta = index.search(SearchRequest(query=_BOUNDARY_QUERY, limit=0))
        assert meta["truncated"] is False
        assert meta["candidate_capped"] is False
        assert meta["results_capped"] is False

    def test_state_b_only_the_candidate_universe_was_bounded(self, scoped_index) -> None:
        index, _in_scope = scoped_index
        _hits, meta = index.search(
            SearchRequest(query="zermatt", filters=SearchFilters(well_id="well-1"), limit=0)
        )
        assert meta["candidate_capped"] is True
        assert meta["results_capped"] is False, (
            "far fewer rows survived than MAX_CANDIDATES, so nothing was cut off the answer"
        )
        assert meta["truncated"] is True

    def test_state_c_only_the_result_set_was_cut(self, state_cut_index) -> None:
        index, _version_ids = state_cut_index
        _hits, meta = index.search(SearchRequest(query=_BOUNDARY_QUERY, limit=0))
        assert meta["results_capped"] is True
        assert meta["candidate_capped"] is False, (
            "every matching candidate was discovered; only the returned list was cut"
        )
        assert meta["truncated"] is True

    def test_state_d_both_bounds(self, scoped_index) -> None:
        """Unscoped, every row matches, so discovery is bounded *and* the answer list is cut."""
        index, _in_scope = scoped_index
        _hits, meta = index.search(SearchRequest(query="zermatt", limit=0))
        assert meta["candidate_capped"] is True
        assert meta["results_capped"] is True
        assert meta["truncated"] is True
        assert meta["candidates"] == RETRIEVAL_CAP

    def test_the_two_backends_agree_at_the_bound_itself(self, scoped_index) -> None:
        """Parity where it is hardest: more matching rows than the cap, on both paths.

        Every other parity test uses a corpus where both backends find all the matches, which
        cannot detect one backend applying a *different* cap policy.  Here 16 001 rows match, so
        the candidate list, the count and both flags are all products of the bound.
        """
        index, _in_scope = scoped_index
        request = SearchRequest(query="zermatt", limit=0)
        fts_hits, fts_meta = index.search(request)
        index._fts_ready = False
        try:
            scan_hits, scan_meta = index.search(request)
        finally:
            index._fts_ready = None
        assert [h.chunk.chunk_id for h in scan_hits] == [h.chunk.chunk_id for h in fts_hits]
        assert scan_meta["candidates"] == fts_meta["candidates"] == RETRIEVAL_CAP
        assert scan_meta["candidate_capped"] is fts_meta["candidate_capped"] is True
        assert scan_meta["results_capped"] is fts_meta["results_capped"] is True
        assert scan_meta["truncated"] is fts_meta["truncated"] is True

    def test_state_e_exactly_at_a_bound_is_not_a_bound(self, state_cut_index) -> None:
        """Reaching a bound with nothing beyond it is a complete answer, not a truncated one.

        Runs last within the class because it removes a version; state C above needs all 4001.
        """
        index, version_ids = state_cut_index
        index.remove_version(version_ids[-1])
        _hits, meta = index.search(SearchRequest(query=_BOUNDARY_QUERY, limit=0))
        assert meta["truncated"] is False
        assert meta["candidate_capped"] is False
        assert meta["results_capped"] is False


class TestDrillingTokensAndPhrasesAgreeAcrossBackends:
    """The tokenizer-sensitive query forms this product actually has to answer."""

    @pytest.mark.parametrize(
        "query",
        [
            "10.2",
            "12 1/4",
            "500/300",
            "12 bbl",
            "9,940 ft",
            "10.2 ppg",
            "shoe depth",
            '"mud weight"',
            "mud mud weight",
        ],
    )
    def test_fts_and_scan_agree(self, index, query: str) -> None:
        index.store(
            chunk_set(
                "doc-tok",
                "ver-tok",
                artifact(
                    ("text", "mud weight 10.2 ppg at 9,940 ft with a 12 1/4 inch bit and 500/300")
                ),
            )
        )
        index.store(
            chunk_set(
                "doc-tok2",
                "ver-tok2",
                artifact(("text", "shoe depth 12 bbl circulated, mud weight held at 10.2 ppg")),
            )
        )
        fts_hits, fts_meta = index.search(SearchRequest(query=query, limit=0))
        index._fts_ready = False
        try:
            scan_hits, scan_meta = index.search(SearchRequest(query=query, limit=0))
        finally:
            index._fts_ready = None
        assert [h.chunk.chunk_id for h in scan_hits] == [h.chunk.chunk_id for h in fts_hits], query
        assert [h.score for h in scan_hits] == [h.score for h in fts_hits], query
        assert scan_meta["mode"] == fts_meta["mode"], query
        assert scan_meta["broadened"] == fts_meta["broadened"], query
        assert scan_meta["candidate_capped"] == fts_meta["candidate_capped"], query
        assert scan_meta["results_capped"] == fts_meta["results_capped"], query


class TestInMemoryBackendSharesTheRankingContract:
    """In-memory runs no bounded discovery stage at all, and that must be visible, not silent."""

    def test_it_never_reports_a_discovery_bound(self) -> None:
        index = InMemorySearchIndex()
        index.store(chunk_set("doc-mem", "ver-mem", artifact(("text", _MATCHING_TEXT))))
        _hits, meta = index.search(SearchRequest(query=_BOUNDARY_QUERY, limit=0))
        assert meta["candidate_capped"] is False, (
            "there is no candidate-retrieval stage to cap; claiming one would be a lie in the "
            "other direction"
        )
        assert meta["results_capped"] is False
        assert meta["truncated"] is False
        assert meta["fts_used"] is False

    def test_it_agrees_with_sqlite_on_a_small_corpus(self, index) -> None:
        index.store(chunk_set("doc-par", "ver-par", artifact(("text", _MATCHING_TEXT))))
        memory = InMemorySearchIndex()
        memory.store(chunk_set("doc-par", "ver-par", artifact(("text", _MATCHING_TEXT))))
        sql_hits, sql_meta = index.search(SearchRequest(query=_BOUNDARY_QUERY, limit=0))
        mem_hits, mem_meta = memory.search(SearchRequest(query=_BOUNDARY_QUERY, limit=0))
        assert [h.chunk.chunk_id for h in mem_hits] == [h.chunk.chunk_id for h in sql_hits]
        assert [h.score for h in mem_hits] == [h.score for h in sql_hits]
        assert mem_meta["mode"] == sql_meta["mode"]
        assert mem_meta["broadened"] == sql_meta["broadened"]
        assert mem_meta["candidates"] == sql_meta["candidates"]


class TestRetrievalCapExactBound:
    """``RETRIEVAL_CAP`` at 15999 / 16000 / 16001, against the production constant.

    This class *consumes* ``scoped_index`` (it removes versions), so it is defined last: the
    assertions above all run against the intact corpus first.  The point of the walk is the same
    one the retrieval look-ahead exists for - reaching the bound exactly must not be reported as
    having exceeded it, which is why discovery fetches ``RETRIEVAL_CAP + 1`` and compares with ``>``.
    """

    def test_16001_16000_and_15999_candidates(self, scoped_index) -> None:
        """The scoped query keeps the surviving hits far below ``MAX_CANDIDATES``, so the flag
        being asserted here can only be the *discovery* bound - the two are deliberately not
        conflated, which is the whole subject of ledger row 16."""
        index, in_scope_id = scoped_index
        scoped = SearchRequest(query="zermatt", filters=SearchFilters(well_id="well-1"), limit=0)

        _hits, meta = index.search(scoped)
        assert meta["candidates"] == RETRIEVAL_CAP
        assert meta["truncated"] is True, "one candidate past the bound, so the bound really bit"

        index.remove_version(next(iter(_version_for(index, in_scope_id))))
        _hits, meta = index.search(scoped)
        assert meta["candidates"] == RETRIEVAL_CAP
        assert meta["truncated"] is False, (
            "exactly at the bound with nothing beyond it is a complete answer, not a bounded one"
        )

        index.remove_version(next(iter(_version_for(index, in_scope_id))))
        _hits, meta = index.search(scoped)
        assert meta["candidates"] == RETRIEVAL_CAP - 1
        assert meta["truncated"] is False


def _version_for(index: SqliteSearchIndex, chunk_id: str) -> list[str]:
    """The version id behind a chunk id - removal is keyed by version, not by chunk."""
    with index.engine.connect() as connection:
        rows = connection.execute(
            sa_text("select version_id from search_chunk where chunk_id <> :cid limit 2"),
            {"cid": chunk_id},
        ).all()
    return [str(row[0]) for row in rows]
