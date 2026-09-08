"""P5 forensic verification of the calculation & engineering evidence foundation.

``test_engineering_lessons.py`` proves the *write path* works (a method is named, evidence is
required from anything not hand-entered, re-running is a no-op, superseding keeps the old row).
This file is the *forensic* half: it re-derives the contract from the authoritative rows and pins
the properties a future deterministic calculation engine will depend on:

*   **identity** - content-addressed, never a timestamp; method *and* method version distinguish a
    record, and the result's quality (``uncertainty``, ``confidence``) plus how it was triggered
    belong to the identity too (ADR-0012);
*   **units** - an input's value/unit/dimension survive storage; a unit is never silently dropped,
    and an unparseable value is kept as "no value" rather than guessed;
*   **provenance** - a MANUAL record fabricates no ``document_id``/``document_version_id``/evidence,
    while a DOCUMENT-DERIVED one preserves the document chain, and a domain input keeps its subject
    key and value;
*   **immutability** - an input is a snapshot, not a live view: mutating the source later does not
    rewrite the record, and read operations never touch the registry;
*   **determinism / cost** - identity is insertion-order independent, retrieval is name-ordered, and
    listing issues a constant number of queries.

No mocks: every record is written through :class:`EngineeringRepository` and every assertion reads
the real database.
"""

from __future__ import annotations

import hashlib

import pytest
from sqlalchemy import event, select

from drilling_intelligence.core.enums import CalculationStatus, KnowledgeOrigin
from drilling_intelligence.core.errors import ValidationError
from drilling_intelligence.database.integrity import check_promoted_evidence
from drilling_intelligence.database.models import (
    Calculation,
    CalculationInput,
    Document,
    DocumentVersion,
)
from drilling_intelligence.engineering.repository import EngineeringRepository


def _record(repository: EngineeringRepository, **overrides) -> tuple[Calculation, bool]:
    values = {
        "method_id": "hydraulics.ecd",
        "method_version": "1.0",
        "inputs": {"mw": {"value": "10.2 ppg"}, "tvd": {"value": 9850.0, "unit": "ft"}},
        "outputs": {"ecd_ppg": 11.4},
    }
    values.update(overrides)
    return repository.record_calculation(**values)


def _select_count(engine, fn) -> int:
    count = {"value": 0}

    def before(conn, cursor, statement, params, context, executemany):
        if str(statement).lstrip().upper().startswith("SELECT"):
            count["value"] += 1

    event.listen(engine, "before_cursor_execute", before)
    try:
        fn()
    finally:
        event.remove(engine, "before_cursor_execute", before)
    return count["value"]


def _fingerprint(session) -> str:
    """A content fingerprint of the two calculation tables (the authoritative record)."""
    parts: list[str] = []
    for model in (Calculation, CalculationInput):
        rows = [
            tuple(
                sorted(
                    (str(k), str(v)) for k, v in row.__dict__.items() if k != "_sa_instance_state"
                )
            )
            for row in session.execute(select(model).order_by(model.id)).scalars()
        ]
        parts.append(f"{model.__tablename__}::{rows}")
    return hashlib.sha256(repr(parts).encode("utf-8")).hexdigest()


# --------------------------------------------------------------------------- schema / identity
class TestCalculationIdentity:
    def test_the_schema_carries_the_evidence_contract(self, session) -> None:
        """The columns a reproducible engineering record needs all exist on ``calculation``."""
        columns = {c.name for c in Calculation.__table__.columns}
        for name in (
            "method_id",
            "method_version",
            "calculation_type",
            "inputs",
            "outputs",
            "assumptions",
            "validation",
            "uncertainty",
            "confidence",
            "provenance",
            "origin",
            "created_by",
            "identity_key",
            "document_id",
            "document_version_id",
            "supersedes_id",
            "revision",
            "status",
        ):
            assert name in columns, name
        input_columns = {c.name for c in CalculationInput.__table__.columns}
        for name in (
            "name",
            "value",
            "unit",
            "dimension",
            "source_kind",
            "subject_key",
            "provenance",
        ):
            assert name in input_columns, name

    def test_identity_is_content_addressed_not_a_timestamp(self, session) -> None:
        """The same content twice is the same row, decided by content, not by when it was written."""
        repository = EngineeringRepository(session)
        first, created = _record(repository)
        session.flush()
        again, created_again = _record(repository)
        session.flush()
        assert created and not created_again and again.id == first.id
        assert first.identity_key and first.identity_key.startswith("calc:")
        assert len(first.identity_key) == 37, "32 hex characters, 'calc:' prefix"

    def test_method_version_is_part_of_identity(self, session) -> None:
        """Same inputs, different method version: a distinct record, not a collision."""
        repository = EngineeringRepository(session)
        one, _ = _record(repository, method_version="1.0")
        two, _ = _record(repository, method_version="2.0")
        session.flush()
        assert one.id != two.id
        assert one.method_version == "1.0" and two.method_version == "2.0"

    def test_the_results_quality_is_part_of_identity(self, session) -> None:
        """``uncertainty``/``confidence``/``triggered_by`` distinguish a record (ADR-0012).

        Two runs that agree on the numbers but disagree on the confidence, the uncertainty bounds or
        how they were triggered are *different claims*; collapsing them would silently drop the newer
        assessment.  This is the regression for the identity-hash omission.
        """
        repository = EngineeringRepository(session)
        first, created_first = _record(
            repository, confidence=0.7, uncertainty={"ecd": 0.1}, triggered_by="cli"
        )
        second, created_second = _record(
            repository, confidence=0.9, uncertainty={"ecd": 0.3}, triggered_by="ui"
        )
        session.flush()
        assert created_first and created_second, "different quality is different content"
        assert first.id != second.id
        assert second.confidence == 0.9 and second.uncertainty == {"ecd": 0.3}
        assert second.triggered_by == "ui"


# --------------------------------------------------------------------------- inputs / units
class TestInputContract:
    def test_an_input_keeps_its_value_unit_and_dimension(self, session) -> None:
        repository = EngineeringRepository(session)
        row, _ = _record(repository)
        session.flush()
        by_name = {item.name: item for item in repository.calculation_inputs(row.id)}
        assert by_name["mw"].value == pytest.approx(10.2)
        assert by_name["mw"].unit == "ppg" and by_name["mw"].dimension == "MUD_WEIGHT"
        assert by_name["tvd"].value == pytest.approx(9850.0)
        assert by_name["tvd"].unit == "ft" and by_name["tvd"].dimension == "LENGTH"

    def test_a_unit_is_never_silently_dropped(self, session) -> None:
        """``12.5 ppg`` must not silently become a bare ``12.5`` with the unit lost."""
        repository = EngineeringRepository(session)
        row, _ = _record(repository, inputs={"mw": {"value": "12.5 ppg"}})
        session.flush()
        [item] = repository.calculation_inputs(row.id)
        assert item.value == pytest.approx(12.5) and item.unit == "ppg"
        # The authoritative JSON payload keeps the exact source wording too.
        assert row.inputs["mw"]["value"] == "12.5 ppg"

    def test_an_unparseable_or_invalid_unit_is_preserved_not_crashed(self, session) -> None:
        """A value the unit registry cannot read keeps no number and no invented dimension."""
        repository = EngineeringRepository(session)
        row, _ = _record(
            repository,
            inputs={
                "grip": {"value": "unparseable"},
                "weird": {"value": 1.0, "unit": "not-a-real-unit"},
            },
        )
        session.flush()
        by_name = {item.name: item for item in repository.calculation_inputs(row.id)}
        assert by_name["grip"].value is None and by_name["grip"].unit == ""
        assert by_name["grip"].dimension == ""
        # The unknown unit spelling is kept verbatim so nothing is silently normalised away.
        assert by_name["weird"].unit == "not-a-real-unit" and by_name["weird"].dimension == ""

    def test_null_zero_and_negative_inputs_are_preserved(self, session) -> None:
        """No domain restriction is invented: a null, a zero and a negative all survive as stated."""
        repository = EngineeringRepository(session)
        row, _ = _record(
            repository,
            inputs={"null": None, "zero": {"value": 0.0}, "negative": {"value": -5.0}},
        )
        session.flush()
        assert row.inputs["null"] is None
        assert row.inputs["zero"] == {"value": 0.0}
        assert row.inputs["negative"] == {"value": -5.0}

    def test_inputs_are_snapshots_not_live_views(self, session) -> None:
        """Mutating the source after the record is written must not rewrite the calculation."""
        repository = EngineeringRepository(session)
        source = {"mw": {"value": "10.2 ppg", "subject_key": "well:A|mud_weight"}}
        row, _ = _record(repository, inputs=source)
        session.commit()
        source["mw"]["value"] = "99.9 ppg"
        session.refresh(row)
        assert row.inputs["mw"]["value"] == "10.2 ppg", (
            "the record keeps the value it was computed from"
        )
        [item] = repository.calculation_inputs(row.id)
        assert item.value == pytest.approx(10.2) and item.subject_key == "well:A|mud_weight"


# --------------------------------------------------------------------------- provenance
class TestProvenanceContract:
    def test_a_manual_calculation_fabricates_no_document_evidence(self, session) -> None:
        repository = EngineeringRepository(session)
        row, _ = _record(repository)  # origin defaults to MANUAL, no provenance
        session.flush()
        assert row.origin == KnowledgeOrigin.MANUAL.value
        assert row.document_id is None and row.document_version_id is None
        assert row.provenance == [], "nothing is invented for a hand-typed number"

    def test_a_document_derived_calculation_preserves_the_document_chain(self, session) -> None:
        """A derived record keeps its document, version and evidence, and a missing one is refused."""
        document = Document(
            id="doc-calc", identity_path="sheets/ecd.xlsx", filename="ecd.xlsx", sha256="0" * 64
        )
        version = DocumentVersion(
            id="ver-calc",
            document_id=document.id,
            version_number=1,
            source_path="sheets/ecd.xlsx",
            sha256="1" * 64,
            is_current=True,
        )
        session.add_all([document, version])
        session.flush()
        provenance = [
            {
                "kind": "spreadsheet",
                "document": {"sheet": "Summary", "cell": "B9"},
                "method": "extractor",
            }
        ]
        repository = EngineeringRepository(session)
        row, created = _record(
            repository,
            origin=KnowledgeOrigin.DERIVED.value,
            provenance=provenance,
            document_id=document.id,
            document_version_id=version.id,
        )
        session.flush()
        assert created and row.origin == KnowledgeOrigin.DERIVED.value
        assert row.document_id == document.id and row.document_version_id == version.id
        assert row.provenance == provenance, "the evidence chain survives storage"

    def test_a_derived_calculation_without_evidence_is_refused(self, session) -> None:
        repository = EngineeringRepository(session)
        with pytest.raises(ValidationError, match="has to cite its evidence"):
            _record(repository, origin=KnowledgeOrigin.EXTRACTED.value)

    def test_an_orphan_derived_calculation_is_detected(self, session) -> None:
        """A DERIVED row written around the repository and citing nothing is reported, not hidden."""
        session.add(
            Calculation(
                id="calc-orphan",
                method_id="some.method",
                origin=KnowledgeOrigin.DERIVED.value,
                provenance=[],
            )
        )
        session.flush()
        problems = check_promoted_evidence(session)
        assert any(p.table == "calculation" and p.row_id == "calc-orphan" for p in problems)


# --------------------------------------------------------------------------- history / determinism
class TestHistoryAndDeterminism:
    def test_superseding_preserves_the_historical_record(self, session) -> None:
        """The old number stays exactly what a decision was made on; the new one is a separate row."""
        repository = EngineeringRepository(session)
        old, _ = _record(repository, outputs={"ecd_ppg": 11.4})
        session.flush()
        new, _ = _record(repository, outputs={"ecd_ppg": 11.1}, supersedes_id=old.id)
        session.flush()
        assert new.supersedes_id == old.id and new.revision == old.revision + 1
        assert str(old.status) == str(CalculationStatus.SUPERSEDED)
        assert old.outputs == {"ecd_ppg": 11.4}, "superseding never rewrites the older result"
        assert new.outputs == {"ecd_ppg": 11.1}
        assert repository.calculations_for(current_only=True) == [new]

    def test_identity_is_insertion_order_independent(self, session) -> None:
        """Two equivalent input sets in different key orders are one record, not two."""
        repository = EngineeringRepository(session)
        first, _ = _record(repository, method_id="m.order", inputs={"z": 1.0, "a": 2.0})
        session.flush()
        again, created_again = _record(repository, method_id="m.order", inputs={"a": 2.0, "z": 1.0})
        session.flush()
        assert not created_again and again.id == first.id
        # Retrieval is ordered by name, so the representation is stable regardless of input order.
        assert [item.name for item in repository.calculation_inputs(first.id)] == ["a", "z"]

    def test_listing_issues_a_constant_number_of_queries(self, db, session) -> None:
        """``calculations_for`` is a single scan, not one query per record."""
        repository = EngineeringRepository(session)
        for index in range(3):
            _record(repository, method_id=f"m.n{index}")
        session.flush()
        baseline = _select_count(db.engine, repository.calculations_for)
        assert 0 < baseline <= 3, baseline
        for index in range(3, 30):
            _record(repository, method_id=f"m.n{index}")
        session.flush()
        assert _select_count(db.engine, repository.calculations_for) == baseline, (
            "listing must not issue one query per record (N+1)"
        )

    def test_read_operations_leave_the_registry_unchanged(self, db, session) -> None:
        repository = EngineeringRepository(session)
        row, _ = _record(repository)
        session.flush()
        baseline = _fingerprint(session)
        repository.calculations_for()
        repository.calculation_inputs(row.id)
        repository.calculations_using("well:A|mud_weight")
        assert _fingerprint(session) == baseline, "reads must not write to the calculation tables"

    def test_a_duplicate_identity_is_rejected_by_the_database(self, db, session) -> None:
        """The unique index, not just the repository, holds the one-row-per-content promise."""
        repository = EngineeringRepository(session)
        row, _ = _record(repository)
        session.commit()
        from sqlalchemy.exc import IntegrityError

        session.add(
            Calculation(
                id="calc-twin",
                method_id="other.method",
                inputs={},
                identity_key=row.identity_key,
            )
        )
        with pytest.raises(IntegrityError):
            session.flush()
        session.rollback()
