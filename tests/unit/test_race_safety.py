"""Race-safety of the two get-or-create paths: sources and the extraction cache.

Both follow the same shape - "select, and if it is not there, insert" - and both are
covered by a unique constraint, because two ingestion runs (or an ingestion run and an
import) can decide at the same instant that a row does not exist.  A unique constraint
without a retry just moves the failure into the user's face: the run dies with
``IntegrityError`` halfway through indexing a workspace.

The tests below therefore make the constraint fire *on purpose* - by having the repository
look the other way for one call - and assert the outcome is a reuse rather than an error.
That is a deterministic stand-in for a second thread: what is under test is the
savepoint-and-re-read, not the scheduler.  ``get_or_create_source`` and
``remember_extraction_in_cache`` are the two writers, and the database-level constraints
are asserted separately so the fallback and the guarantee are both covered.
"""

from __future__ import annotations

import pytest
from sqlalchemy import func, select, text

from drilling_intelligence.core.enums import FileChangeKind
from drilling_intelligence.database.models import (
    Document,
    DocumentVersion,
    Extraction,
    ExtractionCache,
    Source,
)
from drilling_intelligence.documents.repository import DocumentRepository


class BlindOnceRepository(DocumentRepository):
    """A repository that cannot see a row on its first look (the concurrent-commit window)."""

    def __init__(self, session, *, blind: str = "source") -> None:
        super().__init__(session)
        self.blind_for = blind
        self.lookups = 0

    def find_source(self, *, kind: str, reference: str) -> Source | None:
        self.lookups += 1
        if self.blind_for == "source" and self.lookups == 1:
            return None
        return super().find_source(kind=kind, reference=reference)

    def cache_entry(self, **key) -> ExtractionCache | None:  # type: ignore[override]
        self.lookups += 1
        if self.blind_for == "cache" and self.lookups == 1:
            return None
        return super().cache_entry(**key)


@pytest.fixture
def document_and_version(session):
    repository = DocumentRepository(session)
    document = repository.create_document(
        workspace_id=None,
        identity_path="docs/race.pdf",
        filename="race.pdf",
        extension=".pdf",
        mime_type="",
        size_bytes=10,
        sha256="d" * 64,
    )
    version = repository.create_version(
        document,
        sha256="d" * 64,
        source_path="docs/race.pdf",
        size_bytes=10,
        parser="pdf_text",
        parser_version="1",
        extraction_version="1",
        origin=FileChangeKind.NEW,
    )
    return repository, document, version


# --------------------------------------------------------------------------- sources
def test_get_or_create_source_is_idempotent_and_keeps_the_unique_key(session) -> None:
    repository = DocumentRepository(session)
    first = repository.get_or_create_source(
        kind="document",
        reference="ver-1",
        label="Mud report Rev 1",
        authority_tier="current_operational_report",
    )
    again = repository.get_or_create_source(
        kind="document", reference="ver-1", label="ignored", authority_tier="historical_report"
    )
    assert first.id == again.id
    assert session.scalar(select(func.count()).select_from(Source)) == 1
    # Reuse also refreshes the fields that legitimately change (authority, revision),
    # so the second call is not a silent no-op.
    assert again.authority_tier == "historical_report"


def test_a_competing_source_insert_becomes_a_reuse(session) -> None:
    """The window: we looked, saw nothing, and the row was there anyway."""
    repository = DocumentRepository(session)
    winner = repository.get_or_create_source(
        kind="manual", reference="paper-42", label="Offset paper"
    )
    session.commit()

    blind = BlindOnceRepository(session, blind="source")
    result = blind.get_or_create_source(
        kind="manual", reference="paper-42", label="Offset paper (again)"
    )
    assert result.id == winner.id, "the loser reuses the winner's row"
    assert blind.lookups >= 2, "the re-read after the failed insert is what saves the run"
    assert session.scalar(select(func.count()).select_from(Source)) == 1
    session.commit()  # the transaction is still healthy after the savepoint rollback


def test_the_source_key_constraint_is_real(session) -> None:
    """The constraint the race handling relies on, asserted against the database itself."""
    DocumentRepository(session).get_or_create_source(kind="document", reference="dup", label="one")
    session.flush()
    raw = text(
        "insert into source (id, kind, reference, label, authority_tier, verified, created_at, updated_at)"
        " values (:id, :kind, :reference, :label, :tier, 0, '2026-01-01', '2026-01-01')"
    )
    with pytest.raises(
        Exception, match=r"UNIQUE constraint failed: source\.kind, source\.reference"
    ):
        session.execute(
            raw,
            {
                "id": "src-raw",
                "kind": "document",
                "reference": "dup",
                "label": "two",
                "tier": "general_knowledge",
            },
        )
    session.rollback()


def test_different_reference_or_kind_is_a_different_source(session) -> None:
    repository = DocumentRepository(session)
    repository.get_or_create_source(kind="document", reference="ver-1", label="a")
    repository.get_or_create_source(kind="manual", reference="ver-1", label="b")
    repository.get_or_create_source(kind="document", reference="ver-2", label="c")
    assert session.scalar(select(func.count()).select_from(Source)) == 3


# --------------------------------------------------------------------------- extraction cache
def raw_cache_insert(sha: str):
    """A raw insert with a duplicate cache key, written so no ORM identity map can help."""
    return text(
        "insert into extraction_cache (id, content_sha256, extractor, extractor_version, config_hash, extraction_id,"
        " hits, refreshed, created_at, updated_at)"
        " values ('extcache-raw', :sha, 'pdf_text', '1', 'cfg', NULL, 0, 0, '2026-01-01', '2026-01-01')"
    ).bindparams(sha=sha)


def store_artefact(
    repository: DocumentRepository,
    document: Document,
    version: DocumentVersion,
    *,
    extractor: str = "pdf_text",
    sha: str = "d" * 64,
) -> Extraction:
    return repository.save_extraction(
        document=document,
        version=version,
        extractor=extractor,
        extractor_version="1",
        content_sha256=sha,
        config_hash="cfg",
        document_json={"text": "hello", "extracted_fields": []},
        text="hello",
        stats={"words": 1},
        router_decision={"extractor": extractor},
        status="OK",
        cache=False,
    )


def test_one_cache_entry_per_key_pointing_at_a_real_artefact(session, document_and_version) -> None:
    repository, document, version = document_and_version
    artefact = store_artefact(repository, document, version)
    entry = repository.remember_extraction_in_cache(
        artefact,
        content_sha256="d" * 64,
        extractor="pdf_text",
        extractor_version="1",
        config_hash="cfg",
        document_version_id=version.id,
    )
    assert entry.extraction_id == artefact.id
    assert entry.produced_by_version_id == version.id
    assert (
        repository.find_cached_extraction(
            content_sha256="d" * 64, extractor="pdf_text", extractor_version="1", config_hash="cfg"
        ).id
        == artefact.id
    )
    assert repository.check_extraction_cache() == []


def test_a_competing_cache_insert_becomes_an_update_not_a_failure(
    session, document_and_version
) -> None:
    repository, document, version = document_and_version
    first = store_artefact(repository, document, version)
    repository.remember_extraction_in_cache(
        first,
        content_sha256="d" * 64,
        extractor="pdf_text",
        extractor_version="1",
        config_hash="cfg",
        document_version_id=version.id,
    )
    session.commit()

    # A second artefact row for the same content (a forced re-extraction, say).  The blind
    # lookup makes the cache upsert try to insert an entry for a key that already exists,
    # which is exactly what the loser of the race does.
    second = store_artefact(repository, document, version)

    blind = BlindOnceRepository(session, blind="cache")
    entry = blind.remember_extraction_in_cache(
        second,
        content_sha256="d" * 64,
        extractor="pdf_text",
        extractor_version="1",
        config_hash="cfg",
        document_version_id=version.id,
    )
    assert entry.extraction_id == second.id, "the freshest artefact is what the cache should serve"
    assert session.scalar(select(func.count()).select_from(ExtractionCache)) == 1, (
        "but still exactly one entry per key"
    )
    session.commit()
    assert blind.lookups >= 2


def test_a_second_artefact_for_the_same_key_is_refused_by_the_database(
    session, document_and_version
) -> None:
    repository, document, version = document_and_version
    artefact = store_artefact(repository, document, version)
    repository.remember_extraction_in_cache(
        artefact,
        content_sha256="d" * 64,
        extractor="pdf_text",
        extractor_version="1",
        config_hash="cfg",
        document_version_id=version.id,
    )
    session.flush()
    with pytest.raises(Exception, match=r"UNIQUE constraint failed: extraction_cache"):
        session.execute(raw_cache_insert("d" * 64))
    session.rollback()


def test_a_deleted_artefact_leaves_no_zombie_entry(session, document_and_version) -> None:
    """``ON DELETE CASCADE`` is what makes "cache hit" safe: never a pointer at nothing."""
    repository, document, version = document_and_version
    artefact = store_artefact(repository, document, version)
    repository.remember_extraction_in_cache(
        artefact,
        content_sha256="d" * 64,
        extractor="pdf_text",
        extractor_version="1",
        config_hash="cfg",
        document_version_id=version.id,
    )
    session.flush()
    assert session.scalar(select(func.count()).select_from(ExtractionCache)) == 1
    session.delete(artefact)
    session.flush()
    assert session.scalar(select(func.count()).select_from(ExtractionCache)) == 0
    assert (
        repository.find_cached_extraction(
            content_sha256="d" * 64, extractor="pdf_text", extractor_version="1", config_hash="cfg"
        )
        is None
    )


def test_stale_pointer_is_cleaned_up_instead_of_served(session, document_and_version) -> None:
    """A hand-edited file where the artefact row is gone: the lookup repairs itself."""
    repository, document, version = document_and_version
    artefact = store_artefact(repository, document, version)
    entry = repository.remember_extraction_in_cache(
        artefact,
        content_sha256="d" * 64,
        extractor="pdf_text",
        extractor_version="1",
        config_hash="cfg",
        document_version_id=version.id,
    )
    session.flush()
    session.execute(
        text("update extraction_cache set extraction_id = NULL where id = :id"), {"id": entry.id}
    )
    session.expire_all()
    assert (
        repository.find_cached_extraction(
            content_sha256="d" * 64, extractor="pdf_text", extractor_version="1", config_hash="cfg"
        )
        is None
    )
    assert session.scalar(select(func.count()).select_from(ExtractionCache)) == 0, (
        "the broken entry is removed on the spot"
    )
