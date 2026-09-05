"""Current-version invariants and version-number allocation.

The registry's most load-bearing statement is "a document has exactly one current
version, and ``document.current_version_id`` names it".  Two versions marked current, or a
pointer at a superseded row, means the UI, the conflict detector and the citation text
disagree about which revision is live - and nothing downstream can tell.

Enforcement is layered, and each layer is tested separately here:

*   a **partial unique index** refuses a second ``is_current`` row per document;
*   a **deferred foreign key** refuses a pointer at a version that does not exist, while
    still letting the delete that nulls it happen in the same transaction;
*   the **repository** writes in an order that satisfies both, allocates version numbers
    under the unique constraint (retry, not hope), and
*   :mod:`drilling_intelligence.database.integrity` checks the three-table rule the
    schema cannot express, so a repair tool or a status page can prove it holds.
"""

from __future__ import annotations

import pytest
from sqlalchemy import func, select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from drilling_intelligence.core.enums import FileChangeKind
from drilling_intelligence.database.integrity import (
    check_current_version_invariants,
    require_current_version_invariants,
)
from drilling_intelligence.database.models import Document, DocumentVersion
from drilling_intelligence.documents.repository import (
    MAX_VERSION_NUMBER_ATTEMPTS,
    DocumentRepository,
)


def make_document(session: Session, name: str = "program.pdf") -> Document:
    repository = DocumentRepository(session)
    return repository.create_document(
        workspace_id=None,
        identity_path=f"docs/{name}".lower(),
        filename=name,
        extension=".pdf",
        mime_type="application/pdf",
        size_bytes=1024,
        sha256="a" * 64,
    )


def add_version(session: Session, document: Document, *, sha: str, number: int | None = None, current: bool = True) -> DocumentVersion:
    """Insert a version row directly, bypassing the repository.

    The tests below need to *create* inconsistent states on purpose, which the repository
    refuses to do - so this helper writes the rows raw and the assertions are about the
    constraints and the checker, not about ``create_version``.
    """
    version = DocumentVersion(
        id=f"ver-{sha[:8]}-{number or 0}",
        document_id=document.id,
        version_number=number or _next(session, document.id),
        sha256=sha,
        source_path=f"docs/{document.filename}",
        size_bytes=1024,
        parser="pdf_text",
        parser_version="1",
        extraction_version="1",
        origin="NEW",
        is_current=current,
    )
    session.add(version)
    session.flush()
    return version


def _next(session: Session, document_id: str) -> int:
    return int(session.scalar(select(func.max(DocumentVersion.version_number)).where(DocumentVersion.document_id == document_id)) or 0) + 1


# --------------------------------------------------------------------------- schema level
def test_a_second_current_version_is_refused_by_the_database(session) -> None:
    """The partial unique index, not application politeness, is what makes this true."""
    document = make_document(session)
    add_version(session, document, sha="b" * 64, number=1)
    with pytest.raises(IntegrityError) as caught:
        add_version(session, document, sha="c" * 64, number=2, current=True)
        session.flush()
    assert "uq_document_version_one_current" in str(caught.value) or "UNIQUE constraint" in str(caught.value)
    session.rollback()


def test_zero_current_versions_are_allowed_but_the_checker_says_so(session) -> None:
    """"No current version" is a state a document can legitimately be in (all superseded).

    The index cannot require "at least one", so the checker does - and the repository
    never creates that state.
    """
    document = make_document(session)
    version = add_version(session, document, sha="b" * 64, number=1)
    session.execute(text("update document_version set is_current = 0 where id = :id"), {"id": version.id})
    session.flush()
    # Raw SQL bypasses the identity map; expire so the checker reads the row, not the
    # object the session still remembers.
    session.expire_all()
    problems = check_current_version_invariants(session)
    assert [problem.problem for problem in problems] == ["NO_CURRENT_VERSION"], problems
    with pytest.raises(Exception, match="NO_CURRENT_VERSION"):
        require_current_version_invariants(session)


def test_the_pointer_is_a_real_foreign_key(session) -> None:
    """A pointer at a version that does not exist is rejected at commit.

    Deferred because document and document_version reference each other; the check still
    happens, just at the end of the transaction, which is what lets the registry insert a
    version and then point at it.
    """
    document = make_document(session)
    add_version(session, document, sha="b" * 64, number=1)
    session.commit()
    document.current_version_id = "ver-does-not-exist"
    with pytest.raises(IntegrityError):
        session.commit()
    session.rollback()


def test_deleting_the_current_version_nulls_the_pointer(session) -> None:
    """No dangling reference and no accidental document deletion: SET NULL is the point."""
    document = make_document(session)
    version = add_version(session, document, sha="b" * 64, number=1)
    document.current_version_id = version.id
    session.flush()
    session.delete(version)
    session.commit()
    # Read from the row, not from the identity map: the ORM has no idea the database nulled
    # the column underneath it, which is exactly why a stale in-memory pointer must never
    # be treated as the registry's answer.
    session.expire_all()
    assert session.get(Document, document.id).current_version_id is None
    assert session.scalar(text("select current_version_id from document where id = :id"), {"id": document.id}) is None


# --------------------------------------------------------------------------- numbering
def test_version_numbers_stay_sequential_across_the_repository(session) -> None:
    document = make_document(session)
    repository = DocumentRepository(session)
    numbers = []
    for index in range(4):
        version = repository.create_version(
            document,
            sha256=f"{index + 1:064x}",
            source_path="docs/x.pdf",
            size_bytes=10,
            parser="pdf_text",
            parser_version="1",
            extraction_version="1",
            origin=FileChangeKind.NEW if index == 0 else FileChangeKind.MODIFIED,
        )
        numbers.append(version.version_number)
    assert numbers == [1, 2, 3, 4]
    assert document.change_count == 4
    assert check_current_version_invariants(session) == []
    rows = session.execute(select(DocumentVersion).where(DocumentVersion.document_id == document.id)).scalars().all()
    assert sorted(row.version_number for row in rows) == [1, 2, 3, 4]
    assert [row.is_current for row in sorted(rows, key=lambda item: item.version_number)] == [False, False, False, True]
    # The supersede chain points forward one step at a time.
    by_number = {row.version_number: row for row in rows}
    for number in (1, 2, 3):
        assert by_number[number].superseded_by_version_id == by_number[number + 1].id
    assert by_number[4].superseded_by_version_id is None
    assert by_number[4].supersedes_version_id == by_number[3].id


class _RacingRepository(DocumentRepository):
    """Repository that lets a competing writer commit the number we just chose.

    The competitor writes through the same session (a second SQLite connection inside one
    transaction would just block on the write lock), which is enough to reproduce the
    failure mode: a row exists at INSERT time that the ``max()`` read did not see.  What is
    being tested is the response - savepoint, re-read, retry - not the operating system's
    locking.
    """

    def __init__(self, session: Session) -> None:
        super().__init__(session)
        self.fired = False
        self.attempts = 0

    def next_version_number(self, document_id: str) -> int:
        number = super().next_version_number(document_id)
        self.attempts += 1
        if not self.fired:
            self.fired = True
            self.session.add(
                DocumentVersion(
                    id="ver-rival",
                    document_id=document_id,
                    version_number=number,
                    sha256="r" * 64,
                    source_path="docs/rival.pdf",
                    size_bytes=1,
                    parser="pdf_text",
                    parser_version="1",
                    extraction_version="1",
                    origin="NEW",
                    is_current=False,
                )
            )
            self.session.flush()
        return number


def test_a_colliding_version_number_is_retried_not_raised(session) -> None:
    document = make_document(session)
    repository = _RacingRepository(session)
    version = repository.create_version(
        document,
        sha256="b" * 64,
        source_path="docs/x.pdf",
        size_bytes=10,
        parser="pdf_text",
        parser_version="1",
        extraction_version="1",
        origin=FileChangeKind.NEW,
    )
    rival = session.get(DocumentVersion, "ver-rival")
    assert sorted([rival.version_number, version.version_number]) == [1, 2], "numbers stay unique and dense"
    assert repository.attempts == 2, "exactly one retry: the loop must not spin"
    assert version.is_current and not rival.is_current
    assert document.current_version_id == version.id
    assert check_current_version_invariants(session) == []
    # The savepoint rollback discarded *our* attempt only: the rival row survives, and
    # the transaction is still usable (this is what "do not poison the run" means).
    session.commit()
    assert session.get(DocumentVersion, "ver-rival") is not None


class _StuckRepository(DocumentRepository):
    """Always hands out a number that is already taken - the retry loop must give up loudly."""

    def __init__(self, session: Session, *, stuck_at: int) -> None:
        super().__init__(session)
        self.stuck_at = stuck_at
        self.attempts = 0

    def next_version_number(self, document_id: str) -> int:
        self.attempts += 1
        return self.stuck_at


def test_exhausted_retries_raise_a_clear_error(session) -> None:
    document = make_document(session)
    DocumentRepository(session).create_version(
        document,
        sha256="1" * 64,
        source_path="docs/x.pdf",
        size_bytes=10,
        parser="pdf_text",
        parser_version="1",
        extraction_version="1",
        origin=FileChangeKind.NEW,
    )
    repository = _StuckRepository(session, stuck_at=1)
    with pytest.raises(Exception, match="could not allocate a version number"):
        repository.create_version(
            document,
            sha256="2" * 64,
            source_path="docs/x.pdf",
            size_bytes=10,
            parser="pdf_text",
            parser_version="1",
            extraction_version="1",
            origin=FileChangeKind.MODIFIED,
        )
    assert repository.attempts == MAX_VERSION_NUMBER_ATTEMPTS, "bounded retry, then a real error rather than a hang"
    # The failed allocation left the healthy state exactly as it was.
    session.expire_all()
    versions = list(session.scalars(select(DocumentVersion).where(DocumentVersion.document_id == document.id)))
    assert [row.version_number for row in versions] == [1]
    assert document.current_version_id == versions[0].id
    assert check_current_version_invariants(session) == []


def test_the_retry_loop_is_bounded_by_configuration() -> None:
    """Not a test of behaviour so much as a guard against an unbounded retry."""
    assert 1 < MAX_VERSION_NUMBER_ATTEMPTS <= 10, MAX_VERSION_NUMBER_ATTEMPTS


def test_a_second_current_version_is_refused_at_the_database_level_too(session) -> None:
    """Belt and braces: even a direct ORM write cannot create two current versions.

    ``create_version`` is the polite path; this is the one that matters for a hand-edited
    file, an import from another workspace or a bug in code that has not been written yet.
    """
    document = make_document(session)
    first = add_version(session, document, sha="1" * 64, number=1)
    second = add_version(session, document, sha="2" * 64, number=2, current=False)
    session.commit()
    second.is_current = True
    with pytest.raises(IntegrityError):
        session.flush()
    session.rollback()
    session.expire_all()
    assert session.get(DocumentVersion, first.id).is_current is True
    assert session.get(DocumentVersion, second.id).is_current is False


# --------------------------------------------------------------------------- the checker's codes
#: Every code :func:`check_current_version_invariants` can emit is produced on purpose below.
#: A code no test reaches is a code that is either dead or wrong, and both are worse than no
#: checker at all: the point of the report is that a repair tool can act on the *named*
#: failure.  Several of these states cannot be created through SQL while the partial unique
#: index exists - which is the guarantee working - so those tests drop the index first,
#: simulating exactly the case the checker is for: a database where the constraint was never
#: created (an older file, or a hand-edited one).
def _codes(session: Session) -> list[str]:
    session.flush()
    session.expire_all()
    return [problem.problem for problem in check_current_version_invariants(session)]


def _without_one_current_index(session: Session):
    session.execute(text("drop index uq_document_version_one_current"))
    session.flush()


def test_the_checker_names_two_current_versions_and_says_the_pointer_disagrees(session) -> None:
    document = make_document(session)
    _without_one_current_index(session)
    add_version(session, document, sha="1" * 64, number=1)
    second = add_version(session, document, sha="2" * 64, number=2)
    session.flush()
    document.current_version_id = second.id  # the pointer picks the other of the two "current" rows
    session.commit()

    codes = _codes(session)
    assert codes[0] == "MULTIPLE_CURRENT", codes
    assert codes[1] == "POINTER_MISMATCH", codes
    with pytest.raises(Exception, match="current-version invariant violated"):
        require_current_version_invariants(session)

    # The finding is actionable: fix the rows and the constraint can be rebuilt.
    session.execute(
        text(
            "update document_version set is_current = (version_number ="
            " (select max(version_number) from document_version where document_id = :id)) where document_id = :id"
        ),
        {"id": document.id},
    )
    session.execute(
        text(
            "update document set current_version_id = (select id from document_version where document_id = document.id"
            " and is_current = 1) where id = :id"
        ),
        {"id": document.id},
    )
    session.execute(text("create unique index uq_document_version_one_current on document_version (document_id) where is_current = 1"))
    session.commit()
    assert _codes(session) == []
    # ...and the rebuilt index refuses a second current row again.
    with pytest.raises(IntegrityError, match=r"UNIQUE constraint failed: document_version\.document_id"):
        session.execute(
            text(
                "insert into document_version (id, document_id, version_number, revision_key, status, source_path,"
                " sha256, size_bytes, mime_type, parser, parser_version, extraction_version, origin, is_current,"
                " created_at, updated_at) values ('ver-x', :doc, 9, 'r', 'DRAFT', 'docs/x', '3', 1, '', 'pdf_text',"
                " '1', '1', 'NEW', 1, '2026-01-01', '2026-01-01')"
            ),
            {"doc": document.id},
        )
    session.rollback()


def test_the_checker_distinguishes_a_stale_pointer_from_a_foreign_one(session) -> None:
    document = make_document(session)
    other = make_document(session, name="other.pdf")
    mine = add_version(session, document, sha="1" * 64, number=1)
    theirs = add_version(session, other, sha="2" * 64, number=1)
    other.current_version_id = theirs.id
    session.flush()

    document.current_version_id = None
    assert _codes(session) == ["POINTER_MISSING"], "a current version with nothing pointing at it is its own failure"

    document.current_version_id = theirs.id
    session.flush()
    codes = _codes(session)
    assert codes == ["POINTER_FOREIGN"], codes
    problem = check_current_version_invariants(session)[0]
    assert problem.detail["version_of_document"] == other.id
    assert "POINTER_MISMATCH" not in codes, "the sharper label must win: this row belongs to someone else"

    # A pointer at a superseded revision of the *right* document is the other failure.
    stale = add_version(session, document, sha="9" * 64, number=2, current=False)
    mine.is_current = False
    stale.is_current = True
    session.flush()
    document.current_version_id = mine.id
    codes = _codes(session)
    assert codes == ["POINTER_MISMATCH"], codes
    detail = check_current_version_invariants(session)[0].detail
    assert detail == {"points_at": mine.id, "expected": stale.id}, detail
    with pytest.raises(Exception, match="current-version invariant violated") as caught:
        require_current_version_invariants(session)
    assert [item["problem"] for item in caught.value.context["problems"]] == ["POINTER_MISMATCH"]


def test_a_current_version_that_claims_to_be_superseded_is_reported(session) -> None:
    document = make_document(session)
    _without_one_current_index(session)
    first = add_version(session, document, sha="1" * 64, number=1)
    add_version(session, document, sha="2" * 64, number=2)
    session.commit()
    # Both claims at once on the older row: current *and* replaced.
    session.execute(
        text("update document_version set is_current = 1, superseded_by_version_id = :winner where id = :id"),
        {"id": first.id, "winner": "ver-22222222-2"},
    )
    codes = _codes(session)
    assert "SUPERSEDED_IS_CURRENT" in codes, codes
    problem = next(item for item in check_current_version_invariants(session) if item.problem == "SUPERSEDED_IS_CURRENT")
    assert problem.row_id == first.id and problem.detail == {"superseded_by": "ver-22222222-2"}, problem.to_dict()


def test_an_orphan_version_is_reported_rather_than_skipped(session) -> None:
    """``ON DELETE CASCADE`` normally prevents this; a hand-edited file must still be caught.

    The breakage is created with a plain ``sqlite3`` connection, which is precisely how it
    happens in real life: someone edits the workspace file with a tool that has no foreign
    keys switched on, the document row goes, the versions stay - and every later read of the
    registry would skip them in silence if the checker did not look.
    """
    document = make_document(session)
    orphan = add_version(session, document, sha="1" * 64, number=1)
    session.commit()
    bind = session.get_bind()
    if bind.dialect.name != "sqlite":  # pragma: no cover - the suite runs on SQLite
        pytest.skip("the hand-edited-file scenario is a SQLite-file scenario")
    import sqlite3
    from pathlib import Path

    with sqlite3.connect(Path(str(bind.url.database))) as raw:
        raw.execute("delete from document where id = ?", (document.id,))
    # The document row is gone from under the session: drop it from the identity map rather
    # than expiring it, or the next flush fails on an object that no longer exists.
    session.expunge_all()
    assert session.get(DocumentVersion, orphan.id) is not None, "the cascade did not run: the row is orphaned"
    codes = _codes(session)
    assert codes == ["ORPHAN_VERSION"], codes
    assert check_current_version_invariants(session)[0].detail == {"document_id": document.id}
