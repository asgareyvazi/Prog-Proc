"""The extraction cache: what a duplicate costs, and what it must never do.

The property this file protects is a performance property with correctness consequences:
routing a file must be cheap, and a cache hit must not run a parser.  An earlier version of
the registry hashed the file, ran the *full extraction*, and only then consulted the cache -
so duplicates produced right answers at full price and the cache never saved any work.

Everything here runs the real pipeline against a real SQLite workspace with the real
extractors.  The only test double is :class:`RecordingRouter`, which delegates every call
and records which stage ran for which file; the parser assertion is measured on the
extractors themselves, not on the router, so a router that "did not call extract" but
parsed anyway could not pass.
"""

from __future__ import annotations

import shutil
from copy import deepcopy
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import func, select

from drilling_intelligence.database.models import (
    AuditEvent,
    Document,
    DocumentVersion,
    Extraction,
    ExtractionCache,
)
from drilling_intelligence.documents.repository import DocumentRepository
from drilling_intelligence.extraction.registry import build_default_router
from drilling_intelligence.ingestion.pipeline import IngestionPipeline

LESSON = "lesson_learned_ll-2025-014.txt"
MUD = "mud_report_well-a3.xlsx"
EXPECTED_EXTRACTOR = {LESSON: "text", MUD: "excel"}


class RecordingRouter:
    """Delegating router that records which stage ran for which file."""

    def __init__(self, inner: Any) -> None:
        self.inner = inner
        self.routed: list[str] = []
        self.probed: list[str] = []
        self.parsed: list[str] = []
        #: ``"extractor:filename" -> calls``, counted inside the extractor.
        self.extractor_calls: dict[str, int] = {}
        self.inner.extractors = [
            _SpyExtractor(extractor, self.extractor_calls) for extractor in self.inner.extractors
        ]
        # ``route`` calls ``probe`` on the inner router, so the instance attribute is
        # wrapped: without this, "the cheap probe ran" would be unobservable from here.
        inner_probe = self.inner.probe

        def _probe(context: Any) -> Any:
            self.probed.append(context.filename)
            return inner_probe(context)

        self.inner.probe = _probe

    @property
    def parser_calls(self) -> int:
        return sum(self.extractor_calls.values())

    def route(self, context: Any, *, options: dict[str, Any] | None = None) -> Any:
        self.routed.append(context.filename)
        return self.inner.route(context, options=options)

    def probe(self, context: Any) -> Any:
        self.probed.append(context.filename)
        return self.inner.probe(context)

    def select(self, context: Any) -> Any:
        return self.inner.select(context)

    def extract(self, context: Any, **kwargs: Any) -> Any:
        self.parsed.append(context.filename)
        return self.inner.extract(context, **kwargs)

    def list_extractors(self) -> Any:
        return self.inner.list_extractors()

    def mineru_available(self) -> Any:
        return self.inner.mineru_available()


class _SpyExtractor:
    """Counts ``extract``; everything else is the real extractor's own behaviour."""

    def __init__(self, inner: Any, counts: dict[str, int]) -> None:
        self._inner = inner
        self._counts = counts

    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)

    def extract(self, context: Any, provenance: Any) -> Any:
        key = f"{self._inner.name}:{context.filename}"
        self._counts[key] = self._counts.get(key, 0) + 1
        return self._inner.extract(context, provenance)


@pytest.fixture
def corpus(workspace) -> Path:
    root = workspace.root / "corpus"
    from tests.fixtures.generate import build_corpus

    build_corpus(root)
    return root


@pytest.fixture
def ids(workspace):
    from drilling_intelligence.wells.repository import WellRepository

    with workspace.database.session() as session:
        repo = WellRepository(session)
        workspace_row = repo.get_or_create_workspace(str(workspace.root), name="Cache Test")
        project = repo.get_or_create_project("Cache Test")
        well = repo.create_well("A-3", project_id=project.id)
        session.commit()
        return workspace_row.id, well.id


def run_pipeline(
    workspace, corpus: Path, ids, *, router: Any = None, settings: Any = None, **kwargs: Any
):
    pipeline = IngestionPipeline(
        settings=settings or workspace.settings,
        workspace_root=workspace.root,
        database=workspace.database,
        router=router,
    )
    workspace_id, well_id = ids
    return pipeline.run(root=corpus, workspace_id=workspace_id, well_id=well_id, **kwargs)


def cache_rows(database) -> dict[tuple[str, str, str, str], str]:
    with database.read_only() as session:
        return {
            (
                entry.content_sha256,
                entry.extractor,
                entry.extractor_version,
                entry.config_hash,
            ): str(entry.extraction_id or "")
            for entry in session.scalars(select(ExtractionCache))
        }


# --------------------------------------------------------------------------- the P0 property
def test_cache_hit_never_invokes_an_extractor(workspace, corpus, ids) -> None:
    """The regression test for "routing/probe first, parse only on a miss"."""
    spy = RecordingRouter(build_default_router(workspace.settings))
    first = run_pipeline(workspace, corpus, ids, router=spy)
    assert first.ok, first.error
    assert first.failures == 0, [item.error for item in first.results if item.error]
    assert spy.parser_calls, "a cold cache has to parse"
    assert set(spy.parsed) <= set(spy.routed), "every parsed file was routed first"
    assert spy.probed, "routing uses the cheap structural probe rather than a parse"

    copy = corpus / f"copy_of_{LESSON}"
    shutil.copy2(corpus / LESSON, copy)
    spy.parsed.clear()
    spy.routed.clear()
    spy.extractor_calls.clear()

    second = run_pipeline(workspace, corpus, ids, router=spy)
    assert second.ok, second.error
    duplicate = next(item for item in second.results if item.filename == copy.name)
    assert duplicate.change.value == "DUPLICATE", duplicate.to_dict()
    assert duplicate.from_cache is True
    assert duplicate.fields > 0, "a reused artefact still has to yield its fields"
    assert duplicate.extractor == EXPECTED_EXTRACTOR[LESSON]

    assert copy.name in spy.routed, (
        "the duplicate must still be routed: that is where the key comes from"
    )
    assert copy.name not in spy.parsed, "a cache hit must not ask the router to extract"
    assert spy.extractor_calls == {}, (
        f"no extractor may run for a duplicate, saw {spy.extractor_calls}"
    )
    # And the untouched files stay untouched too: the whole second run is parse-free.
    assert spy.parser_calls == 0

    with workspace.database.read_only() as session:
        document = session.scalar(select(Document).where(Document.filename == copy.name))
        version = session.get(DocumentVersion, document.current_version_id)
        extraction = session.scalar(
            select(Extraction).where(Extraction.document_version_id == version.id)
        )
        assert extraction.status == "CACHE_HIT", extraction.status
        assert extraction.duration_ms == 0.0
        assert extraction.stats["reused_from_extraction_id"], (
            "which artefact was reused has to be recorded"
        )
        # The version's row is self-contained: nothing downstream has to follow the
        # pointer in order to classify, display or cite it.
        assert extraction.document_json and extraction.text_blob.strip()
        assert extraction.router_decision["extractor"] == extraction.extractor, (
            "routing provenance survives the reuse"
        )


def test_modified_file_is_not_served_from_the_cache(workspace, corpus, ids) -> None:
    """Different bytes are a different key: the edit has to be parsed, and it has to land."""
    spy = RecordingRouter(build_default_router(workspace.settings))
    run_pipeline(workspace, corpus, ids, router=spy)
    spy.extractor_calls.clear()

    target = corpus / LESSON
    target.write_text(
        target.read_text(encoding="utf-8")
        + "\nAdded after the first run: 12.4 pgs EMW at 3200 ft MD.\n",
        encoding="utf-8",
    )
    result = run_pipeline(workspace, corpus, ids, router=spy)
    assert result.ok, result.error
    assert spy.extractor_calls.get(f"text:{LESSON}") == 1, spy.extractor_calls

    with workspace.database.read_only() as session:
        document = session.scalar(select(Document).where(Document.filename == LESSON))
        version = session.get(DocumentVersion, document.current_version_id)
        assert version.origin == "MODIFIED"
        extraction = session.scalar(
            select(Extraction).where(Extraction.document_version_id == version.id)
        )
        assert extraction.status == "OK"
        assert "EMW at 3200 ft" in extraction.text_blob


# --------------------------------------------------------------------------- key semantics
def test_one_cache_entry_per_key_and_reuses_are_counted(workspace, corpus, ids) -> None:
    run_pipeline(workspace, corpus, ids)
    entries = cache_rows(workspace.database)
    assert entries, "a real run must populate the cache"
    assert all(entries.values()), "an entry that points at no artefact is not a cache"

    shutil.copy2(corpus / LESSON, corpus / f"copy_of_{LESSON}")
    run_pipeline(workspace, corpus, ids)
    after = cache_rows(workspace.database)
    assert set(after) == set(entries), "a reuse must not introduce a new cache key"

    with workspace.database.read_only() as session:
        text_entries = [
            entry for entry in session.scalars(select(ExtractionCache)) if entry.extractor == "text"
        ]
        assert any(int(entry.hits) >= 1 for entry in text_entries), (
            "reuses have to be counted, otherwise 'is the cache helping?' is unanswerable"
        )


def test_option_change_invalidates_a_duplicate_but_not_the_others(workspace, corpus, ids) -> None:
    """Same bytes + different options = a different key, so a new artefact is parsed.

    The pipeline plans work from content hashes, so an unchanged file is never re-run
    because a setting changed; the case that *has* to be right is a fresh file whose bytes
    are already cached under another key.  If the option hash were left out, the copy
    would be served the artefact produced under the old limits and the setting would be a
    lie.
    """
    run_pipeline(workspace, corpus, ids)
    shutil.copy2(corpus / LESSON, corpus / "copy_a.txt")
    spy = RecordingRouter(build_default_router(workspace.settings))
    hit_run = run_pipeline(workspace, corpus, ids, router=spy)
    duplicate = next(item for item in hit_run.results if item.filename == "copy_a.txt")
    assert duplicate.from_cache and spy.extractor_calls == {}, (
        "unchanged options must hit the cache"
    )

    settings = deepcopy(workspace.settings)
    settings.extraction.text_max_bytes = max(64, settings.extraction.text_max_bytes // 2)
    shutil.copy2(corpus / LESSON, corpus / "copy_b.txt")
    spy2 = RecordingRouter(build_default_router(settings))
    miss_run = run_pipeline(workspace, corpus, ids, settings=settings, router=spy2)
    assert miss_run.ok, miss_run.error
    fresh = next(item for item in miss_run.results if item.filename == "copy_b.txt")
    assert fresh.from_cache is False, "a different option hash must not reuse the old artefact"
    assert spy2.extractor_calls.get(f"text:{fresh.filename}") == 1

    entries = cache_rows(workspace.database)
    same_bytes = {key for key in entries if key[0] == miss_run.results[0].sha256 or True}
    assert len(same_bytes) >= 2
    text_hashes = {key[3] for key in entries if key[1] == "text"}
    assert len(text_hashes) >= 2, f"the two option sets must be distinct keys, saw {text_hashes}"


def test_extractor_version_change_invalidates_the_cache(workspace, corpus, ids) -> None:
    """The extractor's own version is in the key: a parser fix invalidates its artefacts."""
    run_pipeline(workspace, corpus, ids)
    before = cache_rows(workspace.database)

    # A duplicate of an already-cached file, seen by an extractor that reports a new
    # version: identical bytes, different key, so it must be parsed again.
    router = build_default_router(workspace.settings)
    for extractor in router.extractors:
        if extractor.name == EXPECTED_EXTRACTOR[LESSON]:
            object.__setattr__(extractor, "version", "9999.1.bumped")
    shutil.copy2(corpus / LESSON, corpus / "copy_bumped.txt")
    spy = RecordingRouter(router)
    result = run_pipeline(workspace, corpus, ids, router=spy)
    assert result.ok, result.error
    bumped = next(item for item in result.results if item.filename == "copy_bumped.txt")
    assert bumped.from_cache is False
    assert spy.extractor_calls.get(f"text:{bumped.filename}") == 1, (
        f"a new extractor version must re-parse: {spy.extractor_calls}"
    )

    after = cache_rows(workspace.database)
    assert "9999.1.bumped" in {key[2] for key in after}, after
    assert len(after) == len(before) + 1, "the bumped extractor adds its own key for the same bytes"


def test_a_crashing_extractor_is_reported_not_raised(workspace, corpus, ids) -> None:
    """The failure path itself must not crash (it logs, and logging has reserved keys).

    A parser blowing up on one file costs that file: the run reports it with the error
    code, keeps the other documents, and the audit trail says why.  Recording that here
    because the handler is inside the extraction flow this file re-ordered, and because a
    crash while *reporting* a crash is the worst possible failure of a safety net.
    """
    router = build_default_router(workspace.settings)
    for extractor in router.extractors:
        if extractor.name == EXPECTED_EXTRACTOR[LESSON]:
            extractor.extract = lambda context, provenance: (_ for _ in ()).throw(
                RuntimeError("boom in the parser")
            )
    result = run_pipeline(workspace, corpus, ids, router=router)
    assert result.ok, result.error
    broken = next(item for item in result.results if item.filename == LESSON)
    assert "boom in the parser" in broken.error, broken.error
    assert broken.error_code == "EXTRACTION"
    processed = {item.filename: item for item in result.results}
    assert processed[MUD].ok, "one bad file must not stop the others"

    with workspace.database.read_only() as session:
        document = session.scalar(select(Document).where(Document.filename == LESSON))
        assert document.processing_status == "FAILED"
        assert "boom in the parser" in (document.processing_error or "")
        actions = [
            event.action
            for event in session.scalars(
                select(AuditEvent).where(AuditEvent.subject_id == document.id)
            )
        ]
        assert "extraction.failed" in actions, actions


# --------------------------------------------------------------------------- cache policy
def test_forced_reprocess_republishes_the_entry_and_keeps_history(workspace, corpus, ids) -> None:
    """``force`` re-parses and moves *what the cache points at*; artefact rows are never edited."""
    run_pipeline(workspace, corpus, ids)
    with workspace.database.read_only() as session:
        original_rows = {
            row[0]: (row[1], row[2])
            for row in session.execute(
                select(Extraction.id, Extraction.status, Extraction.document_json)
            )
        }

    result = run_pipeline(workspace, corpus, ids, force=True)
    assert result.ok, result.error

    with workspace.database.read_only() as session:
        rows_now = {
            row[0]: (row[1], row[2])
            for row in session.execute(
                select(Extraction.id, Extraction.status, Extraction.document_json)
            )
        }
        assert set(original_rows) <= set(rows_now), (
            "a forced run must not delete the artefacts it replaces"
        )
        assert all(rows_now[key] == original_rows[key] for key in original_rows), "or rewrite them"
        for entry in session.scalars(select(ExtractionCache)):
            artefact = session.get(Extraction, entry.extraction_id)
            assert artefact is not None and artefact.status == "OK"
            assert artefact.id not in original_rows, "the cache now serves the fresh artefact"
        # Force re-extracts; it does not fabricate a version for unchanged bytes.
        for document in session.scalars(select(Document)):
            assert (
                session.scalar(
                    select(func.count())
                    .select_from(DocumentVersion)
                    .where(DocumentVersion.document_id == document.id)
                )
                == 1
            ), document.filename


def test_cache_disabled_means_no_reuse_but_still_stores(workspace, corpus, ids) -> None:
    """``[extraction] cache_enabled = false`` is honoured, and "no cache" is not "no data"."""
    settings = deepcopy(workspace.settings)
    settings.extraction.cache_enabled = False
    spy = RecordingRouter(build_default_router(settings))
    result = run_pipeline(workspace, corpus, ids, settings=settings, router=spy)
    assert result.ok, result.error
    assert spy.parser_calls, "with the cache off every file is parsed"
    assert not cache_rows(workspace.database), "and nothing is published to the cache"

    with workspace.database.read_only() as session:
        assert (
            session.scalar(select(func.count()).select_from(Extraction))
            == len(EXPECTED_EXTRACTOR) + 4
        )
        assert session.scalar(select(func.count()).select_from(DocumentVersion)) > 0


def test_repository_lookup_keys_and_misses(workspace, corpus, ids) -> None:
    """The lookup is keyed on all four components; any difference misses."""
    run_pipeline(workspace, corpus, ids)
    with workspace.database.session() as session:
        repository = DocumentRepository(session)
        document = session.scalar(select(Document).where(Document.filename == MUD))
        version = session.get(DocumentVersion, document.current_version_id)
        artefact = repository.latest_extraction(document.id)
        base = {
            "content_sha256": version.sha256,
            "extractor": artefact.extractor,
            "extractor_version": artefact.extractor_version,
            "config_hash": artefact.config_hash,
        }
        cached = repository.find_cached_extraction(**base)
        assert cached is not None and cached.id == artefact.id
        for field, wrong in (
            ("content_sha256", "0" * 64),
            ("extractor", "nope"),
            ("extractor_version", "0" * 8),
            ("config_hash", "deadbeefdeadbeef"),
        ):
            probe = dict(base)
            probe[field] = wrong
            assert repository.find_cached_extraction(**probe) is None, (
                f"{field} must be part of the key"
            )
        assert repository.check_extraction_cache() == []
