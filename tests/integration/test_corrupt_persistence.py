"""Ledger row 28: reads must not silently repair, reinterpret or hide authoritative corruption.

Every case here writes a deliberately malformed value into a real SQLite workspace with raw SQL -
the ORM is bypassed on purpose, because the point is persisted state that no writer would have
produced - and then runs the *real* read path.  The contract is not "every malformed optional field
is fatal"; it is that a read either fails explicitly, or names the row as unreadable/degraded, and
never turns corruption into a valid-looking default while reporting a complete answer.

Two invariants apply to every case:

* the database is unchanged after the read (a read that repairs is a write in disguise);
* corruption is never converted into a plausible value.

The expectations below are the *measured* behaviour, not an aspiration: each was observed first and
then pinned, so a future change that starts swallowing the error has to be a deliberate one.
"""

from __future__ import annotations

import json
from typing import Any

import pytest
from sqlalchemy import inspect as sa_inspect
from sqlalchemy import text
from tests.fixtures.fieldops import ingest, promote, well_id_for

from drilling_intelligence.core.enums import WellLifecycleStatus
from drilling_intelligence.review import DomainReviewRequest, DomainReviewService


def _fingerprint(workspace) -> tuple[Any, ...]:
    """Whole-database row snapshot, so any hidden write during a read is visible."""
    engine = workspace.database.engine
    names = sorted(sa_inspect(engine).get_table_names())
    with engine.connect() as connection:
        return tuple(
            (
                name,
                tuple(
                    tuple(str(value) for value in row)
                    for row in connection.exec_driver_sql(
                        f'SELECT * FROM "{name}" ORDER BY rowid'  # noqa: S608
                    ).fetchall()
                ),
            )
            for name in names
        )


def _corrupt(workspace, statement: str, **params: Any) -> int:
    with workspace.database.engine.begin() as connection:
        return connection.execute(text(statement), params).rowcount


@pytest.fixture
def populated(workspace):
    ingest(workspace)
    promote(workspace)
    return workspace


class TestCorruptPersistenceIsNeverSilentlyRepaired:
    def test_a_read_leaves_the_database_exactly_as_it_found_it(self, populated) -> None:
        """Baseline for the fingerprint argument: an ordinary read writes nothing."""
        well_id = well_id_for(populated, "A-3")
        service = DomainReviewService.for_workspace(populated)
        before = _fingerprint(populated)
        service.review(DomainReviewRequest(well_id=well_id))
        assert _fingerprint(populated) == before

    def test_malformed_provenance_json_fails_loudly_and_writes_nothing(self, populated) -> None:
        """The specific silent-repair this row exists to prevent.

        ``provenance`` is the evidence that a number came from somewhere.  A row whose provenance
        cannot be parsed must not be presented as a row with *no* provenance - those are different
        claims, and only the second is an answer a reader can act on.  Measured behaviour: the read
        raises ``json.JSONDecodeError`` naming the parse position, and the database is untouched.
        Failing the whole read is the safe direction here - the opposite failure, dropping the row
        and reporting a complete-looking corpus, is the one the contract forbids.
        """
        well_id = well_id_for(populated, "A-3")
        with populated.database.engine.connect() as connection:
            row = connection.execute(
                text(
                    "SELECT id FROM well_operation WHERE well_id = :w "
                    "AND provenance IS NOT NULL LIMIT 1"
                ),
                {"w": well_id},
            ).fetchone()
        assert row is not None, "the corpus must contain an operation with provenance to corrupt"
        assert (
            _corrupt(
                populated,
                "UPDATE well_operation SET provenance = :p WHERE id = :i",
                p="{not json at all",
                i=str(row[0]),
            )
            == 1
        )

        service = DomainReviewService.for_workspace(populated)
        before = _fingerprint(populated)
        with pytest.raises(json.JSONDecodeError):
            service.review(DomainReviewRequest(well_id=well_id))
        assert _fingerprint(populated) == before, "a failed read must not have written or repaired"

    def test_an_invalid_enum_value_is_not_coerced_to_a_member(self) -> None:
        """``parse`` answers ``None`` for a value the enum does not have - never a default member.

        This is the boundary between "not a valid value" and "some other valid value".  Returning a
        default would let a corrupt status column masquerade as a real lifecycle state.
        """
        assert WellLifecycleStatus.parse("DRILLING") is WellLifecycleStatus.DRILLING
        assert WellLifecycleStatus.parse("NOT_A_REAL_STATE") is None
        assert WellLifecycleStatus.parse("") is None
        assert WellLifecycleStatus.parse(None) is None
        assert WellLifecycleStatus.parse("active") is None, (
            "case is part of the stored contract; silently lowercasing would reinterpret the value"
        )

    def test_a_corrupt_row_does_not_leave_the_review_claiming_a_complete_corpus(
        self, populated
    ) -> None:
        """One unreadable row must not be replaced by a plausible-looking answer.

        Asserted through the same corpus read twice: once intact, once with a corrupt row.  The two
        reads must differ - if they were identical, the corrupt row had been silently normalised.
        """
        well_id = well_id_for(populated, "A-3")
        service = DomainReviewService.for_workspace(populated)
        intact = service.review(DomainReviewRequest(well_id=well_id)).to_dict()

        with populated.database.engine.connect() as connection:
            row = connection.execute(
                text("SELECT id FROM well_operation WHERE well_id = :w LIMIT 1"), {"w": well_id}
            ).fetchone()
        assert row is not None
        _corrupt(
            populated,
            "UPDATE well_operation SET provenance = :p WHERE id = :i",
            p="[]",  # structurally valid JSON, but not the provenance the writer stored
            i=str(row[0]),
        )
        degraded = service.review(DomainReviewRequest(well_id=well_id)).to_dict()
        assert degraded != intact, (
            "changing a row's recorded provenance must change the answer; if it does not, the read "
            "is not actually carrying provenance through"
        )
