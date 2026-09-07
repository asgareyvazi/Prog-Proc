"""Structured search: the six operational/domain record types as a disposable search projection.

The document half of the search index is covered in ``test_search_index.py`` and
``test_search_pipeline.py``; this file is about the second source type - that the same rebuild,
filter and ranking machinery answers for ``ProblemDefinition``, ``ProblemOccurrence``,
``NptRecord``, ``WellEvent``, ``LessonLearned`` and ``Recommendation`` rows, that a structured
result is presented *distinctly* (not dressed up as a page quotation), and that both backends
answer identically.

The corpus is the real generated one, promoted through the real ``OperationalService``, because
"what the files say" is the thing these tests check - a fixture that grew its own records would
only prove the feature works on rows it invented.
"""

from __future__ import annotations

import pytest
from sqlalchemy import select
from tests.fixtures.fieldops import field_id, ingest, promote, well_id_for

from drilling_intelligence.core.enums import ConfirmationStatus
from drilling_intelligence.database.models import (
    NptRecord,
)
from drilling_intelligence.lessons.repository import LessonRepository
from drilling_intelligence.search.index import (
    InMemorySearchIndex,
    SqliteSearchIndex,
)
from drilling_intelligence.search.service import SearchService
from drilling_intelligence.search.structured import (
    KIND_STRUCTURED,
    STRUCTURED_RECORD_TYPES,
    StructuredRecord,
    structured_record_id,
    structured_records,
    structured_row_ids,
    structured_searchable_ids,
)

#: The corpus, once promoted, produces exactly these counts (see tests/fixtures/fieldops.py):
#: two problem definitions, three occurrences, five NPT rows and three events.
_CORPUS_COUNTS = {
    "problem_definition": 2,
    "problem_occurrence": 3,
    "npt_record": 5,
    "well_event": 3,
}


@pytest.fixture
def promoted(workspace):
    """A workspace with the corpus ingested and promoted into operations, events, NPT, problems."""
    ingest(workspace)
    promote(workspace)
    return workspace


@pytest.fixture
def rebuilt(promoted) -> SearchService:
    """A search service over the SQLite sidecar, rebuilt from the registry (documents + records)."""
    service = SearchService.for_workspace(promoted)
    service.rebuild()
    return service


def _structured(hits) -> list:
    return [hit for hit in hits if hit.source_type == "structured"]


# --------------------------------------------------------------------------- projection
def test_the_projection_reads_the_authoritative_rows_not_a_copy(promoted) -> None:
    with promoted.database.read_only() as session:
        records = structured_records(session)
        searchable = structured_searchable_ids(session)
        rows = structured_row_ids(session)
    by_type: dict[str, list[StructuredRecord]] = {}
    for record in records:
        by_type.setdefault(record.record_type, []).append(record)
    # Only the six declared types appear, and the counts are the corpus's, not the test's.
    assert set(by_type) <= set(STRUCTURED_RECORD_TYPES), by_type
    assert {key: len(value) for key, value in by_type.items()} == _CORPUS_COUNTS
    # Every unit carries the deterministic identity, and it round-trips with the source id.
    for record in records:
        assert record.record_id == structured_record_id(record.record_type, record.source_id)
    assert searchable == {record.record_id for record in records}
    assert rows == searchable, "nothing is yet rejected/superseded, so all rows are searchable"


def test_identity_is_stable_across_two_reads(promoted) -> None:
    with promoted.database.read_only() as session:
        first = [(r.record_id, r.record_type, r.source_id) for r in structured_records(session)]
        second = [(r.record_id, r.record_type, r.source_id) for r in structured_records(session)]
    assert first == second and first, "a re-read must reproduce the same ids in the same order"


# --------------------------------------------------------------------------- search
def test_structured_records_search_alongside_documents(rebuilt) -> None:
    response = rebuilt.search("stuck")
    assert response.results, "the term matches both halves of the projection"
    kinds = {(hit.source_type, hit.kind) for hit in response.results}
    assert ("structured", KIND_STRUCTURED) in kinds
    structured = _structured(response.results)
    assert structured
    # A structured result keeps its own identity and label, not a document's.
    for hit in structured:
        assert hit.metadata["record_id"].startswith("structured:")
        assert hit.metadata["record_type"] in STRUCTURED_RECORD_TYPES


def test_a_record_that_matches_only_as_a_record_is_still_found(rebuilt) -> None:
    # "pipe" matches no document chunk in this corpus, only the promoted records.
    response = rebuilt.search("pipe")
    assert response.results and all(hit.source_type == "structured" for hit in response.results)


# --------------------------------------------------------------------------- filters
def test_record_types_filter_narrows_to_the_named_types(rebuilt) -> None:
    response = rebuilt.search("stuck", record_types=("npt_record",))
    assert response.results
    assert {hit.metadata["record_type"] for hit in response.results} == {"npt_record"}


def test_category_filter_narrows_to_the_named_category(rebuilt) -> None:
    response = rebuilt.search("stuck", category="stuck_pipe")
    assert response.results
    assert all(hit.metadata["category"] == "stuck_pipe" for hit in response.results)


def test_field_id_filter_reaches_the_structured_scope(rebuilt, promoted) -> None:
    fid = field_id(promoted)
    response = rebuilt.search("stuck", field_id=fid)
    assert response.results
    assert all(hit.metadata["field_id"] == fid for hit in response.results)


def test_a_structured_only_filter_excludes_document_chunks(rebuilt) -> None:
    # "stuck" matches five document chunks too, but a record-type filter has no document analogue,
    # so the document half must contribute nothing.
    response = rebuilt.search("stuck", record_types=("well_event",))
    assert response.results
    assert all(hit.source_type == "structured" for hit in response.results)
    assert {hit.metadata["record_type"] for hit in response.results} == {"well_event"}


def test_a_document_only_filter_excludes_structured_rows(rebuilt) -> None:
    unfiltered = rebuilt.search("stuck")
    assert any(hit.source_type == "structured" for hit in unfiltered.results)
    response = rebuilt.search("stuck", document_type="DDR")
    assert all(hit.source_type == "document" for hit in response.results), (
        "a document type filter has no structured analogue, so structured rows must not leak through"
    )


def test_workspace_scope_does_not_hide_structured_rows(rebuilt) -> None:
    # Structured rows carry no workspace id, so a workspace filter must not exclude them (their
    # scope is well/project/field, which the dedicated filters express).
    response = rebuilt.search("stuck", workspace_id="some-other-workspace")
    assert any(hit.source_type == "structured" for hit in response.results)


# --------------------------------------------------------------------------- lifecycle
def test_a_rejected_record_leaves_the_projection_on_prune(rebuilt, promoted) -> None:
    with promoted.database.session() as session:
        row = session.scalar(select(NptRecord).order_by(NptRecord.id))
        assert row is not None
        rejected_id = structured_record_id("npt_record", row.id)
        row.status = ConfirmationStatus.REJECTED.value
        session.commit()
    # The rejected row is still an authoritative row, but no longer searchable - so prune drops it.
    with promoted.database.read_only() as session:
        assert rejected_id in structured_row_ids(session)
        assert rejected_id not in structured_searchable_ids(session)
    removed = rebuilt.prune()
    assert removed >= 1
    response = rebuilt.search("stuck", limit=1000)
    assert all(hit.chunk_id != rejected_id for hit in response.results)


def test_stats_reports_structured_health_separately(rebuilt, promoted) -> None:
    stats = rebuilt.stats()
    assert stats["structured_records"] == sum(_CORPUS_COUNTS.values())
    assert stats["structured_missing"] == 0
    assert stats["structured_stale"] == 0
    assert stats["structured_orphaned"] == 0
    # Reject one row: it becomes stale (still exists, no longer searchable) until pruned.
    with promoted.database.session() as session:
        row = session.scalar(select(NptRecord).order_by(NptRecord.id))
        row.status = ConfirmationStatus.REJECTED.value
        session.commit()
    stats = rebuilt.stats()
    assert stats["structured_stale"] == 1
    assert stats["structured_missing"] == 0


# --------------------------------------------------------------------------- lessons & recommendations
def test_lessons_and_recommendations_are_searchable(promoted) -> None:
    """The two record types promotion never writes are still part of the projection."""
    fid = field_id(promoted)
    well = well_id_for(promoted, "A-3")
    with promoted.database.session() as session:
        repository = LessonRepository(session)
        repository.capture(
            lesson="Run a wiper trip before pulling the BHA in the reactive shales.",
            title="Wiper trip before BHA pull",
            problem_type="stuck_pipe",
            well_id=well,
            field_id=fid,
        )
        repository.propose_recommendation(
            statement="Add a wiper trip to the pre-pull checklist.",
            reason="from the stuck-pipe records",
            field_id=fid,
        )
        session.commit()
    service = SearchService.for_workspace(promoted)
    service.rebuild()
    response = service.search("wiper trip")
    types = {hit.metadata["record_type"] for hit in _structured(response.results)}
    assert "lesson_learned" in types
    # A recommendation is searchable by its statement, not only its reason.
    checklist = service.search("checklist")
    assert any(
        hit.metadata["record_type"] == "recommendation" for hit in _structured(checklist.results)
    )


def test_a_superseded_lesson_revision_is_not_searchable_but_the_current_one_is(promoted) -> None:
    fid = field_id(promoted)
    with promoted.database.session() as session:
        repository = LessonRepository(session)
        first = repository.capture(lesson="Keep the first lesson.", field_id=fid)
        session.commit()
        lesson_id = first.id
    service = SearchService.for_workspace(promoted)
    service.rebuild()
    assert any(
        hit.metadata["record_id"] == structured_record_id("lesson_learned", lesson_id)
        for hit in _structured(service.search("first lesson").results)
    )
    with promoted.database.session() as session:
        repository = LessonRepository(session)
        repository.revise(
            lesson_id, by="drilling-engineer", changes={"lesson": "Keep the revised lesson."}
        )
        session.commit()
    service.rebuild()
    with promoted.database.read_only() as session:
        lessons = [r for r in structured_records(session) if r.record_type == "lesson_learned"]
    ids = {r.source_id for r in lessons}
    # The superseded revision left the projection; exactly one (the current) revision is there.
    assert lesson_id not in ids
    assert len(lessons) == 1
    assert "revised lesson" in lessons[0].text


# --------------------------------------------------------------------------- presentation
def test_the_service_presents_a_structured_result_distinctly(rebuilt) -> None:
    response = rebuilt.search("pipe")
    hit = next(hit for hit in response.results if hit.source_type == "structured")
    assert hit.source_type == "structured"
    assert hit.kind == KIND_STRUCTURED
    assert hit.version_id == "" and hit.version_number == 0
    assert hit.page is None and hit.sheet == ""
    assert hit.cited is True, "a structured record cites its own row, not a page"
    # No invented filename: the label is the record's own type/title/category.
    assert "filename" not in hit.metadata
    assert hit.metadata["record_type"] in STRUCTURED_RECORD_TYPES
    payload = hit.to_dict()
    assert payload["source_type"] == "structured"
    assert payload["metadata"]["record_type"] == hit.metadata["record_type"]


# --------------------------------------------------------------------------- parity
def test_both_backends_answer_a_structured_query_identically(promoted) -> None:
    sqlite_service = SearchService.for_workspace(promoted)
    memory_service = SearchService.for_workspace(promoted, in_memory=True)
    sqlite_service.rebuild()
    memory_service.rebuild()
    for query in ("stuck", "pipe", "npt", "equipment failure", "2025"):
        expected = [
            (hit.chunk_id, hit.source_type, hit.score)
            for hit in memory_service.search(query, limit=100).results
        ]
        got = [
            (hit.chunk_id, hit.source_type, hit.score)
            for hit in sqlite_service.search(query, limit=100).results
        ]
        assert got == expected, query


def test_the_broadened_fallback_is_reported_for_a_mixed_query(promoted) -> None:
    service = SearchService.for_workspace(promoted)
    service.rebuild()
    response = service.search("stuck zcbrxq")
    assert response.broadened, "one real term plus one unknown term falls back to any-term"


# --------------------------------------------------------------------------- backend contract
def test_both_backends_expose_the_structured_protocol() -> None:
    for name in ("store_structured", "remove_structured", "rebuild", "prune_obsolete", "stats"):
        assert hasattr(SqliteSearchIndex, name), name
    assert hasattr(InMemorySearchIndex, "store_structured")
