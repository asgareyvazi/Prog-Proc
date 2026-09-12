"""The engineering write path, exercised from real documents rather than from a test's imagination.

Every existing caller of ``record_calculation`` is a test that hands the repository a dictionary it made
up.  That proves the persistence rules and proves nothing about whether the platform can *produce* an
engineering number, which is the question this file exists to answer: a generated corpus of genuine
DOCX/CSV files is parsed, extracted, promoted into NPT records, and only then summed by
:class:`~drilling_intelligence.engineering.service.EngineeringService` - no fixture writes a calculation,
and no test supplies an input value.

The scenarios are the ones that decide whether the record is trustworthy afterwards:

*   the **full path**, asserting the stored total is the corpus's own hours;
*   **idempotence**, because a roll-up that runs twice must not double-count;
*   **provenance**, per input, resolving to the document versions the rows came from;
*   **revision**, where a superseding document version leaves the old result intact and reconstructable;
*   **change impact**, which must report the stale dependency without recomputing anything;
*   **deterministic failure**, where a missing side is refused rather than filled in with a zero.
"""

from __future__ import annotations

import json
import math
from typing import ClassVar
from unittest import mock

import pytest
from sqlalchemy import select
from tests.fixtures.fieldops import (
    DDR_NPT_LINES,
    STATED,
    TOTAL_NPT_HOURS,
    ingest,
    promote,
    well_id_for,
)

from drilling_intelligence.core.enums import (
    CalculationStatus,
    FileChangeKind,
    KnowledgeOrigin,
    RecordState,
)
from drilling_intelligence.core.errors import ValidationError
from drilling_intelligence.core.ids import is_canonical_subject
from drilling_intelligence.database.models import (
    Calculation,
    CalculationInput,
    Document,
    DocumentVersion,
    NptRecord,
    Well,
)
from drilling_intelligence.documents.repository import DocumentRepository
from drilling_intelligence.engineering.repository import EngineeringRepository
from drilling_intelligence.engineering.service import (
    NPT_ROLLUP_METHOD_ID,
    NPT_ROLLUP_METHOD_VERSION,
    NPT_ROLLUP_PROPERTY,
    EngineeringService,
)
from drilling_intelligence.operations.repository import OperationsRepository

#: The corpus states lost time on A-3 in two documents; B-11's single line lives in the CSV only.
WELL = "A-3"

#: What A-3 lost, derived from the corpus's own constants rather than restated: the daily report's two
#: NPT activity lines plus the summary CSV's lines for the same well.  Deriving it is the point - if the
#: generator changes what the files say, this expectation moves with it instead of going quietly wrong.
WELL_HOURS = sum(DDR_NPT_LINES) + sum(hours for name, _, hours, _ in STATED if name == WELL)


@pytest.fixture
def promoted(workspace):
    """A workspace with the real corpus ingested and promoted - the production path, start to finish."""
    ingest(workspace)
    promote(workspace)
    return workspace


def _rollup(workspace, *, well_id: str = "", **kwargs):
    service = EngineeringService.for_workspace(workspace)
    return service.record_npt_rollup(well_id=well_id or well_id_for(workspace, WELL), **kwargs)


def test_a_real_document_flow_produces_an_engineering_calculation(promoted) -> None:
    """The architectural question: can the platform produce a calculation with no test-only caller?

    Nothing in this test names a number.  The hours come from files the corpus generator wrote, through
    the parser, the extractor and the promoter, and the assertion is against the corpus's own constant.
    """
    calculation, created = _rollup(promoted)

    assert created is True
    assert calculation.method_id == NPT_ROLLUP_METHOD_ID
    assert calculation.method_version == NPT_ROLLUP_METHOD_VERSION
    assert calculation.origin == KnowledgeOrigin.DERIVED.value
    assert calculation.status == str(CalculationStatus.COMPUTED)
    assert calculation.record_state == RecordState.ACTUAL.value

    output = calculation.outputs[NPT_ROLLUP_PROPERTY]
    assert output["value"] == pytest.approx(WELL_HOURS)
    assert (output["unit"], output["dimension"]) == ("h", "TIME")
    assert calculation.validation["records_summed"] == calculation.outputs["records"]

    # ...and the total is the promoted rows' own sum, not a number this file decided on.
    with promoted.database.read_only() as session:
        rows = list(
            session.scalars(select(NptRecord).where(NptRecord.well_id == calculation.well_id))
        )
    assert output["value"] == pytest.approx(
        sum(float(row.duration_hours) for row in rows if row.duration_hours is not None)
    )
    assert calculation.outputs["records"] == len(
        [row for row in rows if row.duration_hours is not None]
    )

    # And the well's share is a share: the rest of the corpus's lost time belongs to the other well, so
    # a roll-up that had quietly summed the whole workspace would fail here.
    other = sum(hours for name, _, hours, _ in STATED if name != WELL)
    assert output["value"] + other == pytest.approx(TOTAL_NPT_HOURS)


def test_every_summed_row_becomes_an_indexed_input_with_its_own_citation(promoted) -> None:
    """One input per NPT row, each carrying the document version *that row* came from.

    Per-row citation is the point: the corpus states A-3's lost time in two different documents, so a
    single ``document_version_id`` on the calculation would misattribute one of them.
    """
    calculation, _ = _rollup(promoted)

    with promoted.database.read_only() as session:
        inputs = EngineeringRepository(session).calculation_inputs(calculation.id)
        current = {
            str(row_id)
            for (row_id,) in session.execute(
                select(DocumentVersion.id).where(DocumentVersion.is_current.is_(True))
            )
        }

    assert len(inputs) == calculation.outputs["records"]
    cited: set[str] = set()
    for item in inputs:
        assert (item.unit, item.dimension) == ("h", "TIME")
        assert item.value is not None and item.value >= 0.0
        assert item.source_kind == "npt_record"
        # The dependency edge resolves to the durable subject...
        assert item.subject_kind == "well"
        assert item.subject_id == calculation.well_id
        assert is_canonical_subject(item.subject_key)
        # ...while the evidence is the version, recorded separately from the subject.
        assert item.provenance["npt_record_id"]
        version = item.provenance["document_version_id"]
        assert version in current
        cited.add(version)

    assert len(cited) >= 2, "A-3's lost time is stated by more than one document in this corpus"
    # The record's own provenance lists each contributing version once, deterministically ordered.
    listed = [entry["document_version_id"] for entry in calculation.provenance]
    assert listed == sorted(cited)


def test_the_dependency_chain_reaches_the_source_document(promoted) -> None:
    """Calculation -> input -> NPT row -> document version -> document, walked for real.

    This is the chain change-impact analysis depends on, so it is asserted link by link rather than
    trusted: an input that cites a version nobody can resolve to a file is not evidence.
    """
    calculation, _ = _rollup(promoted)

    with promoted.database.read_only() as session:
        inputs = EngineeringRepository(session).calculation_inputs(calculation.id)
        for item in inputs:
            record = session.get(NptRecord, item.provenance["npt_record_id"])
            assert record is not None, "the input must name an NPT row that exists"
            assert record.well_id == calculation.well_id
            assert float(record.duration_hours) == pytest.approx(item.value)

            version = session.get(DocumentVersion, item.provenance["document_version_id"])
            assert version is not None, "the cited version must resolve"
            assert version.id == record.document_version_id

            document = session.get(Document, version.document_id)
            assert document is not None, "the version must belong to a document"
            # The digest ties the citation to the exact bytes that were parsed.
            assert item.provenance["source_sha256"] == version.sha256


def test_the_subject_is_the_durable_well_not_the_evidence_version(promoted) -> None:
    """Phase 8's split, asserted directly: the dependency edge points at the thing that outlives sources."""
    calculation, _ = _rollup(promoted)
    service = EngineeringService.for_workspace(promoted)
    subject = service.npt_rollup_subject(calculation.well_id)

    assert subject == f"well:{calculation.well_id}|property:npt_hours|state:ACTUAL"
    assert is_canonical_subject(subject)

    with promoted.database.read_only() as session:
        inputs = EngineeringRepository(session).calculation_inputs(calculation.id)
        # A well is not a document version, and superseding a report does not delete the well.
        assert session.get(Well, calculation.well_id) is not None

    assert {item.subject_key for item in inputs} == {subject}
    # The subject carries no version, so it stays the same key after the evidence is replaced.
    assert "document_version" not in subject


def test_running_the_rollup_twice_returns_the_stored_record(promoted) -> None:
    """Idempotence, which is what makes re-running safe rather than double-counting."""
    first, created_first = _rollup(promoted)
    second, created_second = _rollup(promoted)

    assert created_first is True
    assert created_second is False
    assert first.id == second.id
    assert first.identity_key == second.identity_key

    with promoted.database.read_only() as session:
        assert len(session.scalars(select(Calculation)).all()) == 1
        # The input index is rebuilt from the payload, so it cannot have been duplicated either.
        assert len(
            session.scalars(
                select(CalculationInput).where(CalculationInput.calculation_id == first.id)
            ).all()
        ) == len(first.inputs)
        # Re-running must not disturb the record's own lifecycle columns.
        stored = session.get(Calculation, first.id)
        assert stored.status == str(CalculationStatus.COMPUTED)
        assert stored.revision == 1
        assert stored.supersedes_id is None


def test_identity_contains_no_clock_and_no_random_id(promoted) -> None:
    """The same evidence must hash the same way in a different transaction, or identity means nothing."""
    first, _ = _rollup(promoted)

    with promoted.database.session() as session:
        # A second, independent transaction recomputes the identity from the same stored rows.
        again, created = EngineeringService.for_workspace(promoted).record_npt_rollup(
            well_id=first.well_id, session=session
        )
        assert created is False
        assert again.identity_key == first.identity_key
        session.rollback()

    assert first.identity_key.startswith("calc:")
    payload = f"{first.inputs}{first.outputs}{first.provenance}"
    assert first.created_at.isoformat() not in payload
    assert first.id not in payload


def test_a_superseded_source_leaves_the_old_record_intact_and_marks_it_stale(promoted) -> None:
    """The revision scenario, run through the *existing* supersession model rather than a new one.

    A revised file produces a new ``document_version`` and the registry moves ``is_current`` to it.  The
    stored calculation is not touched: its inputs still cite version A, which is exactly what makes the
    old result reconstructable - and exactly what change impact must now report as stale.
    """
    original, _ = _rollup(promoted)
    subject = EngineeringService.for_workspace(promoted).npt_rollup_subject(original.well_id)

    with promoted.database.read_only() as session:
        before = EngineeringRepository(session).calculation_impact(subject)
    assert before["resolved"] is True
    assert {entry["dependency"] for entry in before["entries"]} == {"CURRENT"}

    # Revise one of the cited documents through the registry that owns versioning, so the supersession
    # is the platform's own - this test does not get to invent what "superseded" means.
    stale_version = original.provenance[0]["document_version_id"]
    with promoted.database.session() as session:
        repository = DocumentRepository(session)
        version = session.get(DocumentVersion, stale_version)
        assert version.is_current is True
        document = session.get(Document, version.document_id)
        replacement = repository.create_version(
            document,
            sha256="f" * 64,
            source_path=str(version.source_path or "revised"),
            size_bytes=int(version.size_bytes or 1) + 1,
            parser=str(version.parser or "text"),
            parser_version=str(version.parser_version or "1"),
            extraction_version=str(version.extraction_version or "1"),
            origin=FileChangeKind.MODIFIED,
        )
        session.commit()
        replacement_id = replacement.id

    with promoted.database.read_only() as session:
        # The registry moved the current flag and linked the chain, both ways.
        assert session.get(DocumentVersion, replacement_id).is_current is True
        superseded = session.get(DocumentVersion, stale_version)
        assert superseded.is_current is False
        assert session.get(DocumentVersion, replacement_id).supersedes_version_id == stale_version

        stored = session.get(Calculation, original.id)
        # Nothing was recomputed, rewritten or invalidated...
        assert stored.status == str(CalculationStatus.COMPUTED)
        assert stored.revision == 1
        assert stored.outputs == original.outputs
        assert stored.inputs == original.inputs
        # ...and version A is still named, so the historical result can be reconstructed.
        assert stale_version in {entry["document_version_id"] for entry in stored.provenance}
        assert session.get(DocumentVersion, stale_version) is not None

        after = EngineeringRepository(session).calculation_impact(subject)

    states = {entry["dependency"] for entry in after["entries"]}
    assert "STALE" in states, "the superseded evidence has to surface as a stale dependency"
    stale_entries = [entry for entry in after["entries"] if entry["dependency"] == "STALE"]
    assert all(entry["document_version_id"] == stale_version for entry in stale_entries)
    assert all(entry["calculation_id"] == original.id for entry in stale_entries)
    # The rows cited by the untouched document are still current: staleness is per input, not per record.
    assert "CURRENT" in states


def test_recording_again_after_a_revision_is_an_explicit_act_that_keeps_the_old_row(
    promoted,
) -> None:
    """Re-running is a decision, so the new row points back and the old one is marked, never deleted."""
    original, _ = _rollup(promoted)

    # A person decides the roll-up should be redone and says which record it replaces.
    with promoted.database.session() as session:
        well = session.get(Well, original.well_id)
        session.add(
            NptRecord(
                id="npt-extra-0001",
                well_id=well.id,
                identity_key="promote:manual-extra",
                category="NPT-OTHER",
                duration_hours=3.0,
                duration_basis="STATED",
                document_id=original.provenance[0].get("document_id"),
                document_version_id=original.provenance[0]["document_version_id"],
                provenance=[dict(original.provenance[0])],
                origin=KnowledgeOrigin.EXTRACTED.value,
            )
        )
        session.commit()

    revised, created = _rollup(promoted, supersedes_id=original.id)
    assert created is True
    assert revised.id != original.id
    assert revised.supersedes_id == original.id
    assert revised.revision == 2
    assert revised.outputs[NPT_ROLLUP_PROPERTY]["value"] == pytest.approx(WELL_HOURS + 3.0)

    with promoted.database.read_only() as session:
        previous = session.get(Calculation, original.id)
        assert previous.status == str(CalculationStatus.SUPERSEDED)
        # The superseded result keeps its own numbers and citations: it is what a decision was made on.
        assert previous.outputs[NPT_ROLLUP_PROPERTY]["value"] == pytest.approx(WELL_HOURS)
        assert previous.inputs == original.inputs
        assert len(session.scalars(select(Calculation)).all()) == 2


def test_a_well_with_no_quantified_lost_time_is_refused_not_rolled_up_as_zero(promoted) -> None:
    """The rule the rest of the platform already follows: absence is not zero.

    B-11's hours are deleted here so the well has rows that state no duration.  Storing 0.0 h would be a
    claim the sources never made, so the service refuses and says why.
    """
    well_id = well_id_for(promoted, "B-11")
    with promoted.database.session() as session:
        for row in session.scalars(select(NptRecord).where(NptRecord.well_id == well_id)):
            row.duration_hours = None
        session.commit()

    with pytest.raises(ValidationError) as error:
        _rollup(promoted, well_id=well_id)
    assert "no NPT row that states a duration" in str(error.value)

    with promoted.database.read_only() as session:
        assert session.scalars(select(Calculation)).all() == []


def test_an_unresolved_subject_is_refused_before_anything_is_written(promoted) -> None:
    """A calculation about a well that does not exist has no subject, so it is not a calculation."""
    with pytest.raises(ValidationError) as error:
        _rollup(promoted, well_id="well-does-not-exist")
    assert "no such well" in str(error.value)

    with pytest.raises(ValidationError):
        _rollup(promoted, well_id="   ")

    with promoted.database.read_only() as session:
        assert session.scalars(select(Calculation)).all() == []


def test_a_non_finite_duration_never_reaches_the_total(promoted) -> None:
    """A NaN compares false against everything, so a total containing one would look like a number
    and behave like nothing.

    Two layers stop that, and this test pins down which one actually does the work.  SQLite does not
    have a NaN float: writing one stores ``NULL``, so by the time the roll-up reads the row it is an
    unquantified row and is counted rather than summed - the storage layer, not the service, is what
    makes this safe on the default backend.  The service still carries its own ``math.isfinite``
    guard because that behaviour is the *storage engine's*, not a promise of the domain contract, and
    a backend that does round-trip NaN must not be able to poison a stored engineering number.

    Either way the observable rule is the same: the total is finite, and the row is visible as one
    that stated no usable duration.
    """
    well_id = well_id_for(promoted, WELL)
    with promoted.database.session() as session:
        row = session.scalars(select(NptRecord).where(NptRecord.well_id == well_id).limit(1)).one()
        row.duration_hours = float("nan")
        session.commit()

    with promoted.database.read_only() as session:
        stored = [
            record.duration_hours
            for record in session.scalars(select(NptRecord).where(NptRecord.well_id == well_id))
        ]
    assert None in stored, "this backend is expected to normalise NaN to NULL"
    assert not any(value is not None and math.isnan(value) for value in stored)

    calculation, _ = _rollup(promoted, well_id=well_id)
    total = calculation.outputs[NPT_ROLLUP_PROPERTY]["value"]
    assert math.isfinite(total)
    # The row that lost its number is reported, not silently treated as zero hours.
    assert calculation.validation["records_without_duration"] == 1
    assert calculation.validation["records_summed"] == len(stored) - 1


def test_the_service_refuses_a_non_finite_duration_it_is_actually_handed(promoted) -> None:
    """The guard itself, exercised directly, because the SQLite NULL coercion hides it above.

    A backend that does round-trip NaN would otherwise reach ``sum`` and store an engineering result
    that is not a number, so the refusal is asserted against the service rather than the database.
    """
    well_id = well_id_for(promoted, WELL)

    class _NanRow:
        """The smallest thing that looks like the NPT row the service reads."""

        id = "npt-nan-probe"
        identity_key = "promote:nan-probe"
        duration_hours = float("nan")
        document_id = "doc-x"
        document_version_id = "ver-x"
        provenance: ClassVar[list[dict[str, str]]] = [
            {"document_version_id": "ver-x", "source_sha256": "a" * 64}
        ]

    service = EngineeringService.for_workspace(promoted)
    with (
        mock.patch.object(
            OperationsRepository, "list_npt", autospec=True, return_value=[_NanRow()]
        ),
        pytest.raises(ValidationError) as error,
    ):
        service.record_npt_rollup(well_id=well_id)
    assert "not a finite number" in str(error.value)

    with promoted.database.read_only() as session:
        assert session.scalars(select(Calculation)).all() == []


def test_rows_that_state_no_duration_are_reported_rather_than_silently_dropped(promoted) -> None:
    """A total is only as complete as what it counted, so what it skipped is stored beside it."""
    well_id = well_id_for(promoted, WELL)
    with promoted.database.session() as session:
        session.add(
            NptRecord(
                id="npt-unquantified-1",
                well_id=well_id,
                identity_key="promote:unquantified",
                category="NPT-OTHER",
                duration_hours=None,
                duration_basis="STATED",
                origin=KnowledgeOrigin.EXTRACTED.value,
            )
        )
        session.commit()

    calculation, _ = _rollup(promoted, well_id=well_id)

    assert calculation.validation["records_without_duration"] == 1
    assert calculation.validation["records_considered"] == (
        calculation.validation["records_summed"] + 1
    )
    # ...and the unquantified row changed the total by nothing at all.
    assert calculation.outputs[NPT_ROLLUP_PROPERTY]["value"] == pytest.approx(WELL_HOURS)


def test_the_calculation_is_reachable_from_the_impact_query_by_subject(promoted) -> None:
    """The retrieval half of the contract: a subject key finds what depends on it."""
    calculation, _ = _rollup(promoted)
    subject = EngineeringService.for_workspace(promoted).npt_rollup_subject(calculation.well_id)

    with promoted.database.read_only() as session:
        repository = EngineeringRepository(session)
        using = repository.calculations_using(subject)
        report = repository.calculation_impact(subject)

    assert [row.id for row in using] == [calculation.id]
    assert report["resolved"] is True
    assert report["subject_kind"] == "well"
    assert report["subject_id"] == calculation.well_id
    assert {entry["method_id"] for entry in report["entries"]} == {NPT_ROLLUP_METHOD_ID}


def test_the_service_borrows_a_caller_transaction_and_never_commits_it(promoted) -> None:
    """Phase 9: the repository writes, the caller's transaction decides.  A rollback must lose the row."""
    well_id = well_id_for(promoted, WELL)
    with promoted.database.session() as session:
        calculation, created = EngineeringService.for_workspace(promoted).record_npt_rollup(
            well_id=well_id, session=session
        )
        assert created is True
        identifier = calculation.id
        session.rollback()

    with promoted.database.read_only() as session:
        assert session.get(Calculation, identifier) is None
        assert session.scalars(select(Calculation)).all() == []


def test_no_document_is_needed_twice_and_nothing_is_written_to_the_document_tables(
    promoted,
) -> None:
    """The roll-up reads promoted rows; it must not touch documents, versions or NPT records."""
    with promoted.database.read_only() as session:
        before = (
            session.scalars(select(Document)).all(),
            session.scalars(select(DocumentVersion)).all(),
            session.scalars(select(NptRecord)).all(),
        )
        counts_before = tuple(len(rows) for rows in before)
        npt_before = {row.id: (row.duration_hours, row.document_version_id) for row in before[2]}

    _rollup(promoted)

    with promoted.database.read_only() as session:
        counts_after = (
            len(session.scalars(select(Document)).all()),
            len(session.scalars(select(DocumentVersion)).all()),
            len(session.scalars(select(NptRecord)).all()),
        )
        npt_after = {
            row.id: (row.duration_hours, row.document_version_id)
            for row in session.scalars(select(NptRecord))
        }

    assert counts_after == counts_before
    assert npt_after == npt_before


def test_the_command_line_reaches_the_same_production_path(promoted, capsys) -> None:
    """The path a person actually uses, including the JSON a script would read.

    Exercised end to end because a service nobody can invoke is not a production path: ``records rollup``
    writes the record, ``records impact`` finds it again by the subject the writer filed it under, and the
    second roll-up reports that nothing was written.
    """
    from drilling_intelligence.cli.app import main

    root = str(promoted.root)

    assert main(["records", "rollup", "--workspace", root, "--well", WELL]) == 0
    first = capsys.readouterr().out
    assert f"{WELL_HOURS} h lost" in first
    assert "stored as calc-" in first

    # Running it again writes nothing and says so, rather than quietly appending a twin.
    assert main(["records", "rollup", "--workspace", root, "--well", WELL]) == 0
    second = capsys.readouterr().out
    assert "already recorded as calc-" in second
    assert "nothing was written" in second

    assert main(["records", "rollup", "--workspace", root, "--well", WELL, "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["created"] is False
    assert payload["value"] == pytest.approx(WELL_HOURS)
    assert payload["unit"] == "h"
    assert payload["method_id"] == NPT_ROLLUP_METHOD_ID
    assert len(payload["document_versions"]) >= 2

    # The subject the command printed is the one the impact query answers on.
    assert main(["records", "impact", "--workspace", root, payload["subject"]]) == 0
    impact = capsys.readouterr().out
    assert "1 calculation(s) depend on" in impact
    assert NPT_ROLLUP_METHOD_ID in impact
    assert "STALE 0" in impact


def test_the_command_refuses_a_scope_that_is_not_one_well(promoted, capsys) -> None:
    """A field is a scope but not a subject: rolling several wells into one total would misfile it."""
    from drilling_intelligence.cli.app import main

    root = str(promoted.root)

    assert main(["records", "rollup", "--workspace", root]) == 1
    assert "needs a scope" in capsys.readouterr().err

    assert main(["records", "rollup", "--workspace", root, "--field", "North Cormorant"]) == 1
    assert "about one well" in capsys.readouterr().err

    with promoted.database.read_only() as session:
        assert session.scalars(select(Calculation)).all() == []
