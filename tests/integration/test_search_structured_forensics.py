"""P2 forensic verification of the structured search projection.

``test_search_structured.py`` proves the *feature* works on the real corpus.  This file is the
*forensic* half: it does not trust that feature's certification, and re-derives every claim the
brief makes from the authoritative ORM rows, the sidecar bytes and the lifecycle machines, then
closes the gaps the first pass left open.

The guarantees under test, one class each:

*   **provenance** - a promoted record's exact identity chain ``record_id -> document_id ->
    document_version_id -> locator/evidence``, a hand-written record's refusal to fabricate a
    document identity, and "many evidence entries is still one logical result";
*   **determinism** - insertion-order independence, rebuild idempotence, rebuild-after-mutation,
    and the structured tie-break by record identity;
*   **prune/status forensics** - REJECTED, orphaned (deleted row), a non-current lesson revision
    and a SUPERSEDED recommendation all leave the searchable state, and ``stats`` reports stale vs
    orphaned correctly;
*   **authority** - the authoritative registry is byte-for-byte unchanged by index/rebuild/prune/
    status, the sidecar upgrades *additively* from a legacy document+fact schema, and it is
    disposable (delete + rebuild reproduces it);
*   **retrieval** - one query returns document, fact and structured source types in one ranked
    list, every metadata filter applies to the structured half (not free-text), and both backends
    agree;
*   **lifecycle** - ``is_searchable`` is exactly the domain lifecycle, asserted for every state of
    every machine, not a sample.

No mocks anywhere: the corpus is generated and promoted through the real pipeline, and every
assertion reads the real rows and the real sidecar file.
"""

from __future__ import annotations

import hashlib
import io
import json
import sys
from datetime import datetime
from pathlib import Path

import pytest
from sqlalchemy import MetaData, event, insert, select
from tests.fixtures.fieldops import field_id, ingest, promote, well_id_for

from drilling_intelligence.config.settings import Settings
from drilling_intelligence.core.enums import (
    ConfirmationStatus,
    KnowledgeOrigin,
    RecommendationLifecycle,
)
from drilling_intelligence.core.lifecycle import (
    CONFIRMATION_LIFECYCLE,
    LESSON_LIFECYCLE,
    RECOMMENDATION_LIFECYCLE,
)
from drilling_intelligence.database.models import (
    DocumentVersion,
    Extraction,
    LessonLearned,
    NptRecord,
    ProblemDefinition,
    Recommendation,
)
from drilling_intelligence.database.session import Database
from drilling_intelligence.knowledge.service import KnowledgeExtractionService
from drilling_intelligence.lessons.repository import LessonRepository
from drilling_intelligence.search.index import (
    InMemorySearchIndex,
    SqliteSearchIndex,
    search_chunk_table,
    search_document_table,
    search_meta_table,
    search_structured_table,
)
from drilling_intelligence.search.service import SearchService
from drilling_intelligence.search.structured import (
    STRUCTURED_RECORD_TYPES,
    StructuredRecord,
    is_searchable,
    structured_record_id,
    structured_records,
)
from drilling_intelligence.wells.repository import WellRepository


@pytest.fixture
def promoted(workspace):
    """A workspace with the corpus ingested and promoted (the same starting point as the sibling file)."""
    ingest(workspace)
    promote(workspace)
    return workspace


@pytest.fixture
def rebuilt(promoted) -> SearchService:
    """A search service over the SQLite sidecar, rebuilt from documents + structured records."""
    service = SearchService.for_workspace(promoted)
    service.rebuild()
    return service


def _structured(hits) -> list:
    return [hit for hit in hits if hit.source_type == "structured"]


def _sidecar_structured_rows(workspace) -> list[tuple]:
    """Every ``search_structured`` row, in id order, with the payload columns that must survive."""
    with workspace.index_database.engine.connect() as connection:
        return [
            (
                row["record_id"],
                row["record_type"],
                row["text"],
                row["provenance_json"],
                row["terms_json"],
            )
            for row in connection.execute(
                select(search_structured_table).order_by(search_structured_table.c.record_id)
            ).mappings()
        ]


def _authoritative_fingerprint(workspace) -> str:
    """A content fingerprint of every table in the registry database (the system of record).

    The index, rebuild, prune and status paths must leave this byte-for-byte unchanged: the sidecar
    is derived data, and a single stray write to the registry would change the digest.
    """
    with workspace.database.engine.connect() as connection:
        tables = sorted(
            connection.exec_driver_sql(
                "select name from sqlite_master where type = 'table' and name not like 'sqlite_%'"
            ).scalars()
        )
        parts: list[str] = []
        for table in tables:
            # The table name comes from the registry's own sqlite_master, not from caller input.
            rows = [
                tuple(row)
                for row in connection.exec_driver_sql(f'select * from "{table}"').all()  # noqa: S608
            ]
            rows.sort()
            parts.append(f"{table}::{rows}")
    return hashlib.sha256(repr(parts).encode("utf-8")).hexdigest()


def _new_workspace(tmp_path: Path, settings: Settings, name: str):
    from drilling_intelligence.wells.workspace import Workspace

    root = tmp_path / name
    workspace = Workspace.create(root, settings, name=name)
    Path(workspace.database_url.replace("sqlite:///", "")).parent.mkdir(parents=True, exist_ok=True)
    return workspace


def _register_well(workspace, name: str = "A-3") -> str:
    with workspace.database.session() as session:
        repository = WellRepository(session)
        repository.get_or_create_workspace(str(workspace.root), name="Forensics")
        project = repository.get_or_create_project("Forensics")
        field = repository.get_or_create_field("Forensics", project=project)
        well = repository.create_well(name, project_id=project.id, field_id=field.id)
        session.commit()
        return str(well.id)


# --------------------------------------------------------------------------- provenance
class TestProvenanceIntegrity:
    def test_a_promoted_record_carries_the_exact_identity_chain(self, promoted) -> None:
        """``record_id -> document_id -> document_version_id -> extraction (locator/evidence)``.

        The projection must not only round-trip its own id: the promoted record's provenance has to
        preserve the two foreign keys that reach the artefact the record was promoted from, and the
        version must resolve to a stored extraction (whose ``document_json`` is where the locator and
        the evidence live).
        """
        with promoted.database.read_only() as session:
            records = structured_records(session)
            versions = {str(v.id): v for v in session.scalars(select(DocumentVersion))}
            extractions = {str(e.document_version_id) for e in session.scalars(select(Extraction))}
        derived = [r for r in records if r.provenance and r.provenance.get("document_id")]
        assert derived, "the promoted corpus must produce document-derived records"
        for record in derived:
            provenance = record.provenance
            # The id chain is exact: the record id is the source row id, and the provenance names
            # the document and version the row was promoted from.
            assert record.record_id == structured_record_id(
                record.record_type, provenance["record_id"]
            )
            assert provenance["document_id"] and provenance["document_version_id"]
            version = versions[provenance["document_version_id"]]
            assert version.document_id == provenance["document_id"], (
                "the provenance's version must belong to the provenance's document"
            )
            assert provenance["document_version_id"] in extractions, (
                "the version must have a stored artefact (locator + evidence) to follow"
            )

    def test_a_manual_record_fabricates_no_document_identity(self, promoted) -> None:
        """A hand-written lesson cites nothing and says so - it must not invent a document link."""
        fid = field_id(promoted)
        with promoted.database.session() as session:
            repository = LessonRepository(session)
            lesson = repository.capture(
                lesson="Run a wiper trip before pulling the BHA in the reactive shales.",
                title="Wiper trip before BHA pull",
                field_id=fid,
            )
            session.commit()
            lesson_id = lesson.id
        with promoted.database.read_only() as session:
            row = session.get(LessonLearned, lesson_id)
            assert row.origin == KnowledgeOrigin.MANUAL.value, "capture writes a MANUAL origin"
            projected = next(
                r
                for r in structured_records(session)
                if r.record_type == "lesson_learned" and r.source_id == lesson_id
            )
        provenance = projected.provenance or {}
        assert not provenance.get("document_id"), "a manual lesson has no document to cite"
        assert not provenance.get("document_version_id")
        # Its locator is its own identity, not a page/sheet/cell nobody recorded.
        assert projected.locator_ref == f"lesson {lesson_id}"

    def test_many_evidence_entries_are_still_one_logical_result(self, promoted) -> None:
        """Evidence multiplicity is not record multiplicity (docs/DOMAIN.md)."""
        evidence = [
            {"kind": "spreadsheet", "document": {"sheet": "Summary", "cell": "B9"}},
            {"kind": "spreadsheet", "document": {"sheet": "NPT", "cell": "C4"}},
            {"kind": "manual", "note": "confirmed by the drilling engineer"},
        ]
        with promoted.database.session() as session:
            row = session.scalar(select(NptRecord).order_by(NptRecord.id))
            assert row is not None
            row.provenance = evidence
            session.commit()
            npt_id = row.id
        with promoted.database.read_only() as session:
            matches = [
                r
                for r in structured_records(session)
                if r.record_type == "npt_record" and r.source_id == npt_id
            ]
        assert len(matches) == 1, "three evidence entries must not become three records"
        assert matches[0].provenance["evidence"] == evidence


# --------------------------------------------------------------------------- determinism
class TestDeterminism:
    def test_the_projection_is_insertion_order_independent(self, tmp_path, settings) -> None:
        """Two equivalent datasets built in different orders project to the identical sequence."""
        by_id = {
            "npt-1": "2025-06-10",
            "npt-2": "2025-06-11",
            "npt-3": "2025-06-12",
        }
        orders = {"forward": list(by_id), "reverse": list(reversed(by_id))}
        results: dict[str, list[str]] = {}
        for key, order in orders.items():
            workspace = _new_workspace(tmp_path, settings, key)
            well_id = _register_well(workspace)
            with workspace.database.session() as session:
                for npt_id in order:
                    session.add(
                        NptRecord(
                            id=npt_id,
                            well_id=well_id,
                            category="stuck_pipe",
                            description=f"stuck pipe on {by_id[npt_id]}",
                            started_at=datetime.fromisoformat(by_id[npt_id]),
                        )
                    )
                session.commit()
            with workspace.database.read_only() as session:
                results[key] = [r.record_id for r in structured_records(session)]
            workspace.close()
        assert results["forward"] == results["reverse"]
        # Ordering is by the record's own columns (started_at, then id), never by insertion.
        assert results["forward"] == [
            structured_record_id("npt_record", "npt-1"),
            structured_record_id("npt_record", "npt-2"),
            structured_record_id("npt_record", "npt-3"),
        ]

    def test_a_structured_rebuild_is_idempotent(self, rebuilt, promoted) -> None:
        rebuilt.rebuild()
        first = _sidecar_structured_rows(promoted)
        rebuilt.rebuild()
        second = _sidecar_structured_rows(promoted)
        assert first == second and first, "rebuilding must reproduce the same rows, ids included"

    def test_a_rebuild_after_mutation_tracks_the_registry(self, promoted) -> None:
        service = SearchService.for_workspace(promoted)
        service.rebuild()
        before = {
            hit.metadata["record_id"]
            for hit in _structured(service.search("stuck", limit=1000).results)
        }
        with promoted.database.session() as session:
            repository = LessonRepository(session)
            repository.capture(
                lesson="A brand new wiper-trip lesson that did not exist before.",
                title="Brand new lesson",
                problem_type="stuck_pipe",
                field_id=field_id(promoted),
            )
            session.commit()
        service.rebuild()
        after = {
            hit.metadata["record_id"]
            for hit in _structured(service.search("stuck", limit=1000).results)
        }
        assert before <= after
        assert len(after - before) >= 1, "the new lesson must appear once it is rebuilt"

    def test_structured_ties_break_by_record_identity(self, promoted) -> None:
        """Two records with identical text score identically and order by their record id."""
        with promoted.database.session() as session:
            well_id = well_id_for(promoted, "A-3")
            for npt_id in ("npt-aaa", "npt-bbb"):
                session.add(
                    NptRecord(
                        id=npt_id,
                        well_id=well_id,
                        category="stuck_pipe",
                        description="identical stuck pipe wording",
                    )
                )
            session.commit()
        service = SearchService.for_workspace(promoted)
        service.rebuild()
        hits = [
            hit
            for hit in _structured(
                service.search("stuck", record_types=("npt_record",), limit=1000).results
            )
            if hit.metadata["source_id"] in {"npt-aaa", "npt-bbb"}
        ]
        assert len(hits) == 2, hits
        by_id = {hit.metadata["source_id"]: hit for hit in hits}
        assert by_id["npt-aaa"].score == by_id["npt-bbb"].score, "identical text scores identically"
        assert hits[0].metadata["source_id"] == "npt-aaa", (
            "the tie must break by the lowest record identity, not by insertion"
        )


# --------------------------------------------------------------------------- prune / status
class TestPruneAndStatus:
    def test_a_deleted_row_is_orphaned_then_pruned(self, rebuilt, promoted) -> None:
        from drilling_intelligence.database.models import ProblemOccurrence

        with promoted.database.session() as session:
            row = session.scalar(select(ProblemOccurrence).order_by(ProblemOccurrence.id))
            assert row is not None
            removed_id = structured_record_id("problem_occurrence", row.id)
            session.delete(row)
            session.commit()
        stats = rebuilt.stats()
        assert stats["structured_orphaned"] == 1, (
            "the row is gone, but the projection still holds it"
        )
        removed = rebuilt.prune()
        assert removed >= 1
        assert all(
            hit.metadata["record_id"] != removed_id
            for hit in _structured(rebuilt.search("stuck", limit=1000).results)
        )

    def test_a_superseded_recommendation_is_stale_then_pruned(self, rebuilt, promoted) -> None:
        with promoted.database.session() as session:
            repository = LessonRepository(session)
            recommendation = repository.propose_recommendation(
                statement="Use a wiper trip on every pre-pull checklist.",
                reason="from the stuck-pipe records",
                field_id=field_id(promoted),
            )
            session.commit()
            rec_id = recommendation.id
        rec_record_id = structured_record_id("recommendation", rec_id)
        rebuilt.rebuild()
        assert any(
            hit.metadata["record_id"] == rec_record_id
            for hit in _structured(rebuilt.search("wiper trip", limit=1000).results)
        )
        with promoted.database.session() as session:
            repository = LessonRepository(session)
            repository.decide_recommendation(
                rec_id, RecommendationLifecycle.SUPERSEDED, by="drilling-engineer"
            )
            session.commit()
        assert rebuilt.stats()["structured_stale"] >= 1
        removed = rebuilt.prune()
        assert removed >= 1
        assert all(
            hit.metadata["record_id"] != rec_record_id
            for hit in _structured(rebuilt.search("wiper trip", limit=1000).results)
        )

    def test_a_noncurrent_lesson_revision_is_stale_then_pruned(self, rebuilt, promoted) -> None:
        fid = field_id(promoted)
        with promoted.database.session() as session:
            repository = LessonRepository(session)
            first = repository.capture(lesson="Keep the first revision.", field_id=fid)
            session.commit()
            lesson_id = first.id
        rebuilt.rebuild()
        old_record_id = structured_record_id("lesson_learned", lesson_id)
        assert any(
            hit.metadata["record_id"] == old_record_id
            for hit in _structured(rebuilt.search("first revision", limit=1000).results)
        )
        with promoted.database.session() as session:
            repository = LessonRepository(session)
            repository.revise(
                lesson_id, by="drilling-engineer", changes={"lesson": "Keep the revised version."}
            )
            session.commit()
        # The old revision is still an authoritative row, but no longer the current one: stale, not gone.
        stats = rebuilt.stats()
        assert stats["structured_stale"] >= 1
        removed = rebuilt.prune()
        assert removed >= 1
        assert all(
            hit.metadata["record_id"] != old_record_id
            for hit in _structured(rebuilt.search("first revision", limit=1000).results)
        )


# --------------------------------------------------------------------------- authority
class TestAuthority:
    def test_the_registry_is_byte_for_byte_unchanged_by_index_operations(self, promoted) -> None:
        service = SearchService.for_workspace(promoted)
        baseline = _authoritative_fingerprint(promoted)
        service.rebuild()
        assert _authoritative_fingerprint(promoted) == baseline, "rebuild wrote to the registry"
        service.prune()
        assert _authoritative_fingerprint(promoted) == baseline, "prune wrote to the registry"
        service.stats()
        service.needs_rebuild()
        service.search("stuck", limit=20)
        assert _authoritative_fingerprint(promoted) == baseline, (
            "stats/status/search wrote to the registry"
        )

    def test_a_legacy_document_fact_sidecar_upgrades_additively(self, tmp_path, settings) -> None:
        """Opening an old sidecar (documents + facts only) must add the structured tables and keep the rows."""
        database = Database.from_url(f"sqlite:///{tmp_path / 'legacy_sidecar.db'}", settings)
        legacy = MetaData()
        search_document_table.to_metadata(legacy)
        search_chunk_table.to_metadata(legacy)
        search_meta_table.to_metadata(legacy)
        legacy.create_all(database.engine)
        with database.engine.begin() as connection:
            connection.execute(
                insert(search_document_table).values(version_id="ver-1", document_id="doc-1")
            )
            connection.execute(
                insert(search_chunk_table).values(
                    chunk_id="chk-1", document_id="doc-1", version_id="ver-1", kind="paragraph"
                )
            )
        try:
            index = SqliteSearchIndex(database)
            assert index.missing_tables() == [], "ensure_schema must add the structured table"
            with database.engine.connect() as connection:
                names = set(
                    connection.exec_driver_sql(
                        "select name from sqlite_master where type = 'table'"
                    ).scalars()
                )
                document_rows = connection.execute(select(search_document_table)).mappings().all()
                chunk_rows = connection.execute(select(search_chunk_table)).mappings().all()
            assert "search_structured" in names
            assert [row["version_id"] for row in document_rows] == ["ver-1"], (
                "the legacy document rows must survive the upgrade"
            )
            assert [row["chunk_id"] for row in chunk_rows] == ["chk-1"]
        finally:
            database.dispose()

    def test_the_structured_projection_is_disposable_end_to_end(self, promoted) -> None:
        """Delete the sidecar file, reopen, rebuild: the structured answers are unchanged."""
        service = SearchService.for_workspace(promoted)
        service.rebuild()
        first = sorted(
            hit.metadata["record_id"]
            for hit in _structured(service.search("stuck", limit=1000).results)
        )
        assert first
        path = promoted.index_database_path
        service.index.close()
        promoted.close()
        path.unlink()
        assert not path.exists()

        from drilling_intelligence.wells.workspace import Workspace

        reopened = Workspace.open(promoted.root, promoted.settings)
        try:
            fresh = SearchService.for_workspace(reopened)
            fresh.rebuild()
            second = sorted(
                hit.metadata["record_id"]
                for hit in _structured(fresh.search("stuck", limit=1000).results)
            )
            assert second == first
        finally:
            reopened.close()


# --------------------------------------------------------------------------- retrieval
class TestRetrieval:
    def test_one_query_returns_document_fact_and_structured_source_types(self, promoted) -> None:
        KnowledgeExtractionService.for_workspace(promoted).rebuild(workspace_id="", well_id="")
        service = SearchService.for_workspace(promoted)
        service.rebuild()
        response = service.search("npt", limit=500)
        assert response.results
        kinds = {(hit.source_type, hit.kind) for hit in response.results}
        assert ("structured", "structured") in kinds, kinds
        assert ("document", "knowledge_fact") in kinds, kinds
        assert any(
            source == "document" and kind in {"paragraph", "field", "table_row", "heading", "page"}
            for source, kind in kinds
        ), kinds

    def test_both_backends_agree_on_a_mixed_query(self, promoted) -> None:
        KnowledgeExtractionService.for_workspace(promoted).rebuild(workspace_id="", well_id="")
        sqlite = SearchService.for_workspace(promoted)
        memory = SearchService.for_workspace(promoted, in_memory=True)
        sqlite.rebuild()
        memory.rebuild()
        for query in ("npt", "stuck", "mud weight"):
            expected = [
                (hit.chunk_id, hit.source_type, round(hit.score, 6))
                for hit in memory.search(query, limit=200).results
            ]
            got = [
                (hit.chunk_id, hit.source_type, round(hit.score, 6))
                for hit in sqlite.search(query, limit=200).results
            ]
            assert got == expected, query

    def test_structured_metadata_filters_narrow_to_matching_rows(self, rebuilt, promoted) -> None:
        a3 = well_id_for(promoted, "A-3")
        with promoted.database.read_only() as session:
            reference = structured_records(session)
            project_id = next(r.project_id for r in reference if r.project_id)

        # well_id
        hits = _structured(rebuilt.search("stuck", well_id=a3, limit=1000).results)
        assert hits and all(hit.metadata["well_id"] == a3 for hit in hits)

        # project_id
        hits = _structured(rebuilt.search("stuck", project_id=project_id, limit=1000).results)
        assert hits and all(hit.metadata["project_id"] == project_id for hit in hits)

        # status (metadata, not free text: CANDIDATE appears in no indexed wording)
        hits = _structured(rebuilt.search("stuck", status="CANDIDATE", limit=1000).results)
        assert hits and all(hit.metadata["status"] == "CANDIDATE" for hit in hits)

        # date range: empty dates pass, but nothing dated before the window does
        hits = _structured(rebuilt.search("stuck", date_from="2025-06-01", limit=1000).results)
        assert hits
        assert all(
            not hit.metadata["record_date"] or hit.metadata["record_date"] >= "2025-06-01"
            for hit in hits
        )

        # a combination of three independent filters at once
        hits = _structured(
            rebuilt.search(
                "stuck",
                well_id=a3,
                record_types=("npt_record",),
                category="stuck_pipe",
                limit=1000,
            ).results
        )
        assert hits
        assert all(
            hit.metadata["well_id"] == a3
            and hit.metadata["record_type"] == "npt_record"
            and hit.metadata["category"] == "stuck_pipe"
            for hit in hits
        )

    def test_a_company_filter_applies_even_when_the_corpus_has_no_company(self, rebuilt) -> None:
        # The corpus registers no company, so a company filter must match nothing rather than leak.
        hits = _structured(rebuilt.search("stuck", company_id="co-nobody", limit=1000).results)
        assert hits == []


# --------------------------------------------------------------------------- edge cases
class TestEdgeCases:
    def test_an_empty_registry_projects_zero_structured_records(self, workspace) -> None:
        with workspace.database.read_only() as session:
            assert structured_records(session) == []
        service = SearchService.for_workspace(workspace)
        service.rebuild()
        assert service.stats()["structured_records"] == 0
        assert _structured(service.search("stuck", limit=100).results) == []

    def test_non_ascii_and_null_fields_round_trip_through_the_sidecar(self, promoted) -> None:
        """Long, non-ASCII wording and null columns survive the projection + sidecar round trip."""
        lesson_text = (
            "Re-run the µ-calibration and log the Ø4.5 in hole size — then ream to bottom. "
            "No depth, no hole size and no applicable formation were recorded."
        )
        with promoted.database.session() as session:
            repository = LessonRepository(session)
            lesson = repository.capture(
                lesson=lesson_text, title="µ Ø — round trip", field_id=field_id(promoted)
            )
            session.commit()
            lesson_id = lesson.id
        service = SearchService.for_workspace(promoted)
        service.rebuild()
        with promoted.database.read_only() as session:
            projected = next(
                r
                for r in structured_records(session)
                if r.record_type == "lesson_learned" and r.source_id == lesson_id
            )
        # The wording is kept verbatim, including the non-ASCII characters ...
        assert "µ" in projected.text and "Ø" in projected.text and "—" in projected.text
        # ... and null columns contribute no "None" lines rather than fabricated values.
        assert "None" not in projected.text
        # The same text survives the sidecar round trip and is still searchable.
        response = service.search("calibration", limit=100)
        hits = [
            hit
            for hit in _structured(response.results)
            if hit.metadata["record_id"] == structured_record_id("lesson_learned", lesson_id)
        ]
        assert hits and "µ-calibration" in hits[0].text


# --------------------------------------------------------------------------- lifecycle
class TestLifecycleDerivation:
    def test_searchability_is_exactly_the_domain_lifecycle_for_every_state(self) -> None:
        """Every state of every machine, not a sample: ``is_searchable`` is the projection's
        one interpretation of the lifecycle, so every transition has to be checked."""
        for state in CONFIRMATION_LIFECYCLE.states:
            row = NptRecord(
                id=f"npt-{state.value}",
                well_id="well-1",
                category="c",
                description="d",
                status=state.value,
            )
            assert is_searchable(row) == (state != ConfirmationStatus.REJECTED), state
        for state in RECOMMENDATION_LIFECYCLE.states:
            row = Recommendation(
                id=f"rec-{state.value}",
                signature=f"sig-{state.value}",
                statement="s",
                reason="r",
                status=state.value,
            )
            assert is_searchable(row) == (state != RecommendationLifecycle.SUPERSEDED), state
        # A lesson's approval state never hides it; only supersession (is_current) does.
        for state in LESSON_LIFECYCLE.states:
            current = LessonLearned(
                id=f"les-{state.value}-c",
                title="t",
                lesson="l",
                status=state.value,
                is_current=True,
            )
            superseded = LessonLearned(
                id=f"les-{state.value}-s",
                title="t",
                lesson="l",
                status=state.value,
                is_current=False,
            )
            assert is_searchable(current) is True, state
            assert is_searchable(superseded) is False, state
        # A canonical problem definition has no reject/supersede state: always searchable.
        for status in ("ACTIVE", "DRAFT", "REJECTED", "SUPERSEDED", ""):
            row = ProblemDefinition(
                id=f"pdef-{status or 'empty'}",
                canonical_key="k",
                problem_type="p",
                name="n",
                description="d",
                status=status,
            )
            assert is_searchable(row) is True, status


# --------------------------------------------------------------------------- N+1 / scope
class TestProjectionCost:
    def test_scope_resolution_does_not_grow_with_the_row_count(self, promoted) -> None:
        """The ``_Scope`` preload means the projection issues a constant number of queries."""

        def count_selects() -> int:
            count = {"value": 0}

            def before(conn, cursor, statement, params, context, executemany):
                if str(statement).lstrip().upper().startswith("SELECT"):
                    count["value"] += 1

            event.listen(promoted.database.engine, "before_cursor_execute", before)
            try:
                with promoted.database.read_only() as session:
                    structured_records(session)
            finally:
                event.remove(promoted.database.engine, "before_cursor_execute", before)
            return count["value"]

        baseline = count_selects()
        assert 0 < baseline <= 12, baseline
        with promoted.database.session() as session:
            well_id = well_id_for(promoted, "A-3")
            for index in range(30):
                session.add(
                    NptRecord(
                        id=f"npt-extra-{index}",
                        well_id=well_id,
                        category="stuck_pipe",
                        description=f"extra stuck {index}",
                    )
                )
            session.commit()
        assert count_selects() == baseline, (
            "the projection must not issue one query per record (N+1)"
        )


# --------------------------------------------------------------------------- protocol shape
def test_the_projection_only_ever_emits_declared_types(promoted) -> None:
    with promoted.database.read_only() as session:
        records = structured_records(session)
    assert {record.record_type for record in records} <= set(STRUCTURED_RECORD_TYPES)
    assert records
    for record in records:
        assert isinstance(record, StructuredRecord)


def test_both_backends_expose_the_full_structured_protocol() -> None:
    for name in ("store_structured", "remove_structured", "rebuild", "prune_obsolete", "stats"):
        assert hasattr(SqliteSearchIndex, name), name
        assert hasattr(InMemorySearchIndex, name), name


def test_the_sidecar_schema_is_not_under_alembic() -> None:
    """The structured table is derived data, so no Alembic revision may claim it (README/ADR-0003)."""
    for path in Path("migrations/versions").glob("*.py"):
        source = path.read_text(encoding="utf-8")
        assert "search_structured" not in source, path


# --------------------------------------------------------------------------- CLI smoke
def _run_cli(argv: list[str]) -> tuple[int, str, str]:
    """Run ``drillintel`` with a captured stdout (the ``--json`` payload channel)."""
    from drilling_intelligence.cli.app import main

    out, err = io.StringIO(), io.StringIO()
    saved = sys.stdout, sys.stderr
    sys.stdout, sys.stderr = out, err
    try:
        code = main(list(argv))
    finally:
        sys.stdout, sys.stderr = saved
    return code, out.getvalue(), err.getvalue()


def test_cli_rebuilds_and_searches_structured_records(tmp_path, settings) -> None:
    """The one command a person runs: ingest+promote, then ``index rebuild`` and ``search``.

    ``command_search`` has a dedicated structured branch; it must be exercised by the CLI, not only
    by the service layer, and the index counters a person reads must include the structured half.
    """
    from drilling_intelligence.wells.workspace import Workspace

    root = tmp_path / "workspace"
    workspace = Workspace.create(root, settings, name="CLI structured")
    try:
        ingest(workspace)
        promote(workspace)
    finally:
        workspace.close()
    config = settings.source_path

    code, out, err = _run_cli(
        ["index", "rebuild", "--workspace", str(root), "--config", str(config), "--json"]
    )
    assert code == 0, err
    stats = json.loads(out)["stats"]
    assert stats["structured_records"] > 0, stats
    assert stats["documents"] > 0

    code, out, err = _run_cli(
        ["search", "stuck", "--workspace", str(root), "--config", str(config), "--json"]
    )
    assert code == 0, err
    results = json.loads(out)["results"]
    structured = [hit for hit in results if hit["source_type"] == "structured"]
    assert structured, "the CLI must list the promoted structured records"
    assert structured[0]["metadata"]["record_id"].startswith("structured:")
    assert structured[0]["metadata"]["record_type"] in STRUCTURED_RECORD_TYPES
