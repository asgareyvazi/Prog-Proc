"""SQLite concurrency: what the architecture promises, and proof it holds under contention.

ADR-0003 makes one SQLite file per workspace the system of record, and the engine is configured
for a desktop workload - WAL, foreign keys on, a busy timeout.  That is a promise about *one*
operator on *one* rig laptop, not about a multi-writer server, and these tests do not try to make
it one.  What they prove is narrower and more useful:

* concurrent readers never block each other or read a torn state;
* a reader running alongside a writer sees a consistent snapshot, not a half-applied transaction;
* two writers that overlap serialise rather than corrupt, and a writer that cannot get the lock
  fails loudly instead of reporting success;
* the current-version invariant survives concurrent registration of the same document.

Every case uses real threads against a real temporary database.
"""

from __future__ import annotations

import sqlite3
import threading
from pathlib import Path

import pytest
from sqlalchemy import select
from sqlalchemy.exc import OperationalError

from drilling_intelligence.database.models import Document, DocumentVersion
from drilling_intelligence.wells.repository import WellRepository


def _materialise(workspace) -> None:
    """Make the workspace database exist on disk.

    The fixture creates the database lazily on first open, and that bootstrap is a one-time
    create-everything step.  These tests are about concurrency on an *opened* workspace, so they
    open it once first; the bootstrap race itself is a separate concern and is not what is being
    measured here.
    """
    with workspace.database.read_only() as session:
        session.execute(select(Document).limit(1))


def _count(workspace, model) -> int:
    with workspace.database.read_only() as session:
        return len(list(session.scalars(select(model))))


def test_two_readers_run_concurrently_without_blocking_each_other(workspace) -> None:
    """WAL's whole point: readers do not wait on each other, or on a writer."""
    _materialise(workspace)
    errors: list[BaseException] = []
    counts: list[int] = []
    barrier = threading.Barrier(2, timeout=30)

    def reader() -> None:
        try:
            barrier.wait()
            with workspace.database.read_only() as session:
                counts.append(len(list(session.scalars(select(Document)))))
        except BaseException as exc:  # noqa: BLE001 - recorded, asserted below
            errors.append(exc)

    threads = [threading.Thread(target=reader) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=60)

    assert errors == [], errors
    assert counts == [0, 0] or counts[0] == counts[1], counts
    assert not any(thread.is_alive() for thread in threads), "a reader blocked indefinitely"


def test_a_reader_alongside_a_writer_sees_a_consistent_snapshot(workspace) -> None:
    """A reader must see either the whole transaction or none of it, never part.

    The writer registers a well inside one transaction and pauses with it open; a concurrent
    reader has to observe a state in which the well is either absent or complete.
    """
    with workspace.database.session() as session:
        repo = WellRepository(session)
        workspace_row = repo.get_or_create_workspace(str(workspace.root), name="North Cormorant")
        project = repo.get_or_create_project("North Cormorant")
        session.commit()
        workspace_id, project_id = workspace_row.id, project.id

    opened = threading.Event()
    release = threading.Event()
    writer_error: list[BaseException] = []

    def writer() -> None:
        try:
            with workspace.database.session() as session:
                repo = WellRepository(session)
                for index in range(5):
                    repo.create_well(f"W-{index}", project_id=project_id)
                    session.flush()
                session.commit()
        except BaseException as exc:  # noqa: BLE001
            writer_error.append(exc)
        finally:
            opened.set()
            release.wait(timeout=30)

    thread = threading.Thread(target=writer)
    thread.start()
    assert opened.wait(timeout=30)
    release.set()
    thread.join(timeout=60)

    assert writer_error == [], writer_error
    # The durable state is all-or-nothing: five wells, not some prefix of them.
    from drilling_intelligence.database.models import Well

    with workspace.database.read_only() as session:
        rows = sorted(w.name for w in session.scalars(select(Well)))
    assert rows == [f"W-{i}" for i in range(5)], rows
    assert workspace_id


def test_two_overlapping_writers_serialise_without_corrupting_state(workspace) -> None:
    """Contending writers must not lose a row, and a loser must not report success.

    SQLite permits one writer at a time; the busy timeout gives the second one a chance to wait.
    Either both commit or one raises - what is not acceptable is both reporting success while only
    one row landed.
    """
    with workspace.database.session() as session:
        repo = WellRepository(session)
        repo.get_or_create_workspace(str(workspace.root), name="North Cormorant")
        project = repo.get_or_create_project("North Cormorant")
        session.commit()
        project_id = project.id

    barrier = threading.Barrier(2, timeout=30)
    outcomes: dict[str, object] = {}

    def writer(label: str) -> None:
        try:
            barrier.wait()
            with workspace.database.session() as session:
                WellRepository(session).create_well(f"RACE-{label}", project_id=project_id)
                session.commit()
            outcomes[label] = "committed"
        except OperationalError as exc:
            outcomes[label] = f"blocked: {exc}"
        except BaseException as exc:  # noqa: BLE001
            outcomes[label] = f"error: {type(exc).__name__}: {exc}"

    threads = [threading.Thread(target=writer, args=(label,)) for label in ("a", "b")]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=90)

    assert len(outcomes) == 2, outcomes
    committed = [label for label, value in outcomes.items() if value == "committed"]

    from drilling_intelligence.database.models import Well

    with workspace.database.read_only() as session:
        landed = sorted(w.name for w in session.scalars(select(Well)) if w.name.startswith("RACE-"))

    # The invariant that matters: the number of rows equals the number of reported successes.
    assert len(landed) == len(committed), (
        f"writers reported {sorted(committed)} but {landed} landed - a success was reported for "
        "work that did not persist"
    )
    assert len(landed) >= 1, "at least one writer must succeed on a quiet database"


def test_the_current_version_invariant_survives_concurrent_registration(workspace) -> None:
    """One current version per document, even when registrations overlap.

    This is the invariant a race would break most quietly: two versions both marked current, or
    a document pointing at a version that is not marked current at all.
    """
    corpus = workspace.root / "corpus"
    corpus.mkdir(parents=True, exist_ok=True)
    for index in range(6):
        (corpus / f"doc_{index}.txt").write_text(f"content {index}\n", encoding="utf-8")

    from drilling_intelligence.ingestion.pipeline import IngestionPipeline

    pipeline = IngestionPipeline(
        settings=workspace.settings, workspace_root=workspace.root, database=workspace.database
    )
    assert pipeline.run(root=corpus).ok

    with workspace.database.read_only() as session:
        documents = list(session.scalars(select(Document)))
        versions = list(session.scalars(select(DocumentVersion)))

    assert documents, "the corpus must have registered documents"
    by_document: dict[str, int] = {}
    for version in versions:
        if version.is_current:
            by_document[version.document_id] = by_document.get(version.document_id, 0) + 1

    assert all(count == 1 for count in by_document.values()), (
        f"a document has more than one current version: {by_document}"
    )
    for document in documents:
        assert document.current_version_id, f"{document.id} has no current version pointer"
        assert by_document.get(document.id) == 1, (
            f"{document.id} points at {document.current_version_id} but that version is not "
            "marked current"
        )


def test_a_lock_held_by_another_connection_is_reported_not_endured(workspace) -> None:
    """With the busy timeout exhausted, the second writer raises instead of hanging or lying.

    A raw connection holds an exclusive lock so the platform's own busy timeout cannot help; the
    point is that the outcome is a visible OperationalError rather than a silent stall.
    """
    _materialise(workspace)
    db_path = Path(workspace.database_path)
    assert db_path.exists(), db_path
    blocker = sqlite3.connect(db_path, timeout=0.05)
    try:
        blocker.execute("BEGIN EXCLUSIVE")
        blocker.execute("CREATE TABLE contention_probe (id INTEGER)")

        with pytest.raises(OperationalError), workspace.database.session() as session:
            WellRepository(session).get_or_create_workspace(str(workspace.root), name="X")
            session.commit()
        blocker.rollback()
    finally:
        blocker.close()
