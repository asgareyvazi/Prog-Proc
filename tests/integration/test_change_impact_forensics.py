"""P12 forensic verification of the change-impact foundation (ADR-0016).

``test_calculation_forensics.py`` pins what a calculation *record* guarantees - content identity,
units, provenance, immutability.  This file is the forensic half of the question that record
exists to answer: **an engineering input changed; which numbers have to be re-run?**

Before 0008 that question was a free-form string compared with ``=``, and it returned the wrong
answer in six reproducible ways.  Each of those is a test here, phrased as the failure it prevents:

*   **Case A** - a canonical subject finds the records that consumed it;
*   **Case B** - representations of *one* subject (case, separators, component order, digest form)
    resolve to one canonical key and one answer, so an engineer who spells it differently is not
    told "nothing is affected";
*   **Case C** - a component containing ``|`` or ``:`` does not collide with a different subject,
    and genuinely distinct subjects never do;
*   **Case D** - a subject too long for the column is never silently truncated: a canonical one is
    stored as a deterministic digest and stays findable, a legacy one is refused outright;
*   **Case E** - revising the source document leaves the historical record reconstructable *and*
    reports the dependency as STALE, which is the whole point of the foundation.

Plus the properties the query itself must keep: de-duplication, deterministic ordering, lifecycle
and scope filtering, the unresolved-vs-not-affected distinction, input-level provenance, idempotent
re-recording, identity-key stability, read-only-ness and bounded reads.

No mocks: a real workspace, a real SQLite database, a real corpus, real repositories.
"""

from __future__ import annotations

import hashlib
from pathlib import Path

import pytest
from sqlalchemy import event, select

from drilling_intelligence.core.enums import KnowledgeOrigin
from drilling_intelligence.core.errors import ValidationError
from drilling_intelligence.core.ids import (
    ANCHOR_KINDS,
    DIGEST_PREFIX,
    LEGACY_KIND,
    MAX_RENDERED_LENGTH,
    SubjectKey,
    is_canonical_subject,
    subject_key,
)
from drilling_intelligence.database.integrity import check_calculation_dependencies
from drilling_intelligence.database.models import (
    Calculation,
    CalculationInput,
    Document,
    DocumentVersion,
)
from drilling_intelligence.engineering.repository import (
    DEPENDENCY_CURRENT,
    DEPENDENCY_STALE,
    DEPENDENCY_UNRESOLVED,
    EngineeringRepository,
    resolve_input_subject,
)


def _record(repository: EngineeringRepository, **overrides):
    values = {
        "method_id": "hydraulics.ecd",
        "method_version": "1.0",
        "inputs": {"mw": {"value": "10.2 ppg"}},
        "outputs": {"ecd_ppg": 11.4},
    }
    values.update(overrides)
    return repository.record_calculation(**values)


def _fingerprint(session) -> str:
    """A content fingerprint of both calculation tables - so "read-only" is a comparison."""
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


# --------------------------------------------------------------------------- the key itself
class TestCanonicalSubjectKey:
    def test_rendering_is_deterministic_and_component_order_is_fixed(self) -> None:
        """The same components always render the same bytes, whatever order they were supplied in."""
        first = SubjectKey(well_id="w-1", property_name="mud_weight", record_state="ACTUAL")
        second = SubjectKey(record_state="ACTUAL", property_name="mud_weight", well_id="w-1")
        assert first.render() == second.render() == "well:w-1|property:mud_weight|state:ACTUAL"
        assert [first.render() for _ in range(5)] == [first.render()] * 5

    def test_the_classic_rendering_is_unchanged(self) -> None:
        """ADR-0016 must not rewrite the keys the knowledge layer has already written.

        Every component here is free of delimiters, which is the case for every real predicate and
        every opaque id, so escaping is a no-op and the bytes are what they always were.
        """
        assert (
            subject_key(well_id="w-1", section_id="s-2", property_name="p", record_state="ACTUAL")
            == "well:w-1|section:s-2|property:p|state:ACTUAL"
        )
        assert subject_key() == "unscoped"

    def test_parsing_round_trips_including_escaped_delimiters(self) -> None:
        for subject in (
            SubjectKey(well_id="w-1", property_name="mud_weight", record_state="ACTUAL"),
            SubjectKey(well_id="w|1", property_name="a:b", record_state="ACTUAL"),
            SubjectKey(well_id="w-1", property_name="x|state:PLANNED"),
            SubjectKey(entity_type="document_version", entity_id="ver-1", property_name="p"),
        ):
            rendered = subject.render()
            assert SubjectKey.parse(rendered).render() == rendered
            assert SubjectKey.parse(rendered).property_name == subject.property_name

    def test_normalization_of_a_property_name_is_documented_and_applied(self) -> None:
        """``Mud Weight``, ``MUD_WEIGHT`` and ``mud-weight`` are one property (ADR-0016)."""
        keys = {
            SubjectKey(well_id="w", property_name=name, record_state=state).canonical_key()
            for name, state in (
                ("mud_weight", "ACTUAL"),
                ("MUD_WEIGHT", "ACTUAL"),
                ("Mud Weight", "actual"),
                ("mud-weight", " Actual "),
                ("  mud   weight  ", "ACTUAL"),
            )
        }
        assert keys == {"well:w|property:mud_weight|state:ACTUAL"}

    def test_an_id_keeps_its_case_because_it_is_an_opaque_token(self) -> None:
        assert (
            SubjectKey(well_id="Well-A", property_name="p").canonical_key()
            == "well:Well-A|property:p"
        )

    def test_is_canonical_separates_structure_from_free_text(self) -> None:
        assert is_canonical_subject("well:w-1|property:mud_weight|state:ACTUAL")
        assert is_canonical_subject("document_version:ver-1|property:revision|state:ACTUAL")
        # Free text that happens to contain a colon is not an identity.
        assert not is_canonical_subject("well:A-3|mud_weight")
        assert not is_canonical_subject("mud_report.xlsx!Summary!B9")
        assert not is_canonical_subject("property:mud_weight|state:ACTUAL")  # no anchor
        assert not is_canonical_subject("unscoped")
        assert not is_canonical_subject("")


# --------------------------------------------------------------------------- Case A
class TestCaseACanonicalLookup:
    def test_a_canonical_subject_finds_every_record_that_consumed_it(self, session, well) -> None:
        repository = EngineeringRepository(session)
        key = subject_key(well_id=well.id, property_name="mud_weight", record_state="ACTUAL")
        first, _ = _record(
            repository,
            method_id="hydraulics.ecd",
            well_id=well.id,
            inputs={"mw": {"value": "10.2 ppg", "subject_key": key}},
        )
        second, _ = _record(
            repository,
            method_id="casing.burst",
            well_id=well.id,
            inputs={"mw": {"value": "10.2 ppg", "subject_key": key}},
        )
        session.flush()
        assert {row.id for row in repository.calculations_using(key)} == {first.id, second.id}

    def test_the_resolved_reference_is_stored_beside_the_canonical_key(self, session, well) -> None:
        repository = EngineeringRepository(session)
        key = subject_key(well_id=well.id, property_name="mud_weight", record_state="ACTUAL")
        row, _ = _record(
            repository, well_id=well.id, inputs={"mw": {"value": "10.2 ppg", "subject_key": key}}
        )
        session.flush()
        [item] = repository.calculation_inputs(row.id)
        assert item.subject_key == key
        assert item.subject_kind == "well" and item.subject_id == well.id

    def test_an_input_that_names_no_subject_is_not_a_dependency(self, session, well) -> None:
        repository = EngineeringRepository(session)
        row, _ = _record(
            repository,
            well_id=well.id,
            inputs={"bare": 42.0, "blank": {"value": 1.0, "subject_key": "   "}},
        )
        session.flush()
        for item in repository.calculation_inputs(row.id):
            assert item.subject_key is None
            assert item.subject_kind is None and item.subject_id is None
            assert item.source_kind == "user"


# --------------------------------------------------------------------------- Case B
class TestCaseBEquivalentRepresentations:
    def test_every_spelling_of_one_subject_yields_one_key_and_one_answer(
        self, session, well
    ) -> None:
        """The reproduced defect: six of nine representations used to return nothing."""
        repository = EngineeringRepository(session)
        canonical = subject_key(well_id=well.id, property_name="mud_weight", record_state="ACTUAL")
        row, _ = _record(
            repository,
            well_id=well.id,
            inputs={"mw": {"value": "10.2 ppg", "subject_key": canonical}},
        )
        session.flush()
        variants = (
            canonical,
            canonical + "  ",
            "  " + canonical,
            canonical.replace("mud_weight", "MUD_WEIGHT"),
            canonical.replace("mud_weight", "mud-weight"),
            canonical.replace("mud_weight", "Mud Weight"),
            canonical.replace("state:ACTUAL", "state:actual"),
        )
        for variant in variants:
            found = repository.calculations_using(variant)
            assert [item.id for item in found] == [row.id], (
                f"{variant!r} must find the same record as the canonical key"
            )

    def test_a_record_written_with_a_variant_is_found_by_the_canonical_key(
        self, session, well
    ) -> None:
        """The dangerous direction: the *write* used a different spelling."""
        repository = EngineeringRepository(session)
        canonical = subject_key(well_id=well.id, property_name="mud_weight", record_state="ACTUAL")
        row, _ = _record(
            repository,
            method_id="torque_drag.hookload",
            well_id=well.id,
            inputs={
                "mw": {
                    "value": "10.2 ppg",
                    "subject_key": canonical.replace("mud_weight", "MUD WEIGHT"),
                }
            },
        )
        session.flush()
        assert [item.id for item in repository.calculations_using(canonical)] == [row.id]

    def test_the_stored_key_is_the_canonical_form_not_the_callers_spelling(
        self, session, well
    ) -> None:
        repository = EngineeringRepository(session)
        row, _ = _record(
            repository,
            well_id=well.id,
            inputs={
                "mw": {
                    "value": "10.2 ppg",
                    "subject_key": f"well:{well.id}|property:MUD WEIGHT|state:actual",
                }
            },
        )
        session.flush()
        [item] = repository.calculation_inputs(row.id)
        assert item.subject_key == f"well:{well.id}|property:mud_weight|state:ACTUAL"


# --------------------------------------------------------------------------- Case C
class TestCaseCCollisions:
    def test_a_delimiter_inside_a_component_does_not_forge_another_subject(self) -> None:
        """The reproduced collision: these two were byte-identical before ADR-0016."""
        sneaky = SubjectKey(well_id="A", property_name="x|state:PLANNED")
        real = SubjectKey(well_id="A", property_name="x", record_state="PLANNED")
        assert sneaky.render() != real.render()
        assert sneaky.canonical_key() != real.canonical_key()
        assert SubjectKey.parse(sneaky.render()).property_name == "x|state:PLANNED"
        assert SubjectKey.parse(real.render()).record_state == "PLANNED"

    def test_a_colon_inside_an_id_does_not_forge_another_anchor(self) -> None:
        first = SubjectKey(well_id="w|section:s", property_name="p")
        second = SubjectKey(well_id="w", section_id="s", property_name="p")
        assert first.render() != second.render()
        assert first.anchor() == ("well", "w|section:s")
        assert second.anchor() == ("well", "w")

    def test_distinct_subjects_never_contaminate_each_other(self, session, well) -> None:
        repository = EngineeringRepository(session)
        mud = subject_key(well_id=well.id, property_name="mud_weight", record_state="ACTUAL")
        pore = subject_key(well_id=well.id, property_name="pore_pressure", record_state="ACTUAL")
        first, _ = _record(
            repository, well_id=well.id, inputs={"mw": {"value": 10.2, "subject_key": mud}}
        )
        second, _ = _record(
            repository,
            method_id="geomech.pp",
            well_id=well.id,
            inputs={"pp": {"value": 12.1, "subject_key": pore}},
        )
        session.flush()
        assert [row.id for row in repository.calculations_using(mud)] == [first.id]
        assert [row.id for row in repository.calculations_using(pore)] == [second.id]

    def test_a_different_record_state_is_a_different_subject(self, session, well) -> None:
        """PLANNED and ACTUAL must never be mixed silently (core.enums.RecordState)."""
        repository = EngineeringRepository(session)
        planned = subject_key(well_id=well.id, property_name="hole_depth", record_state="PLANNED")
        actual = subject_key(well_id=well.id, property_name="hole_depth", record_state="ACTUAL")
        row, _ = _record(
            repository, well_id=well.id, inputs={"d": {"value": 8500.0, "subject_key": planned}}
        )
        session.flush()
        assert [item.id for item in repository.calculations_using(planned)] == [row.id]
        assert repository.calculations_using(actual) == []


# --------------------------------------------------------------------------- Case D
class TestCaseDLongSubjects:
    def test_a_long_canonical_subject_is_digested_never_truncated(self, session, well) -> None:
        """The reproduced defect: a 452-character key was stored at 300 and lost to its author."""
        repository = EngineeringRepository(session)
        long_key = subject_key(well_id=well.id, property_name="x" * 400)
        assert len(long_key) > MAX_RENDERED_LENGTH
        row, _ = _record(
            repository, well_id=well.id, inputs={"v": {"value": 1.0, "subject_key": long_key}}
        )
        session.flush()
        [item] = repository.calculation_inputs(row.id)
        assert item.subject_key.startswith(DIGEST_PREFIX)
        assert len(item.subject_key) <= MAX_RENDERED_LENGTH
        assert item.subject_kind == "well" and item.subject_id == well.id
        # Findable by the key its author actually used - the property a truncating write loses.
        assert [found.id for found in repository.calculations_using(long_key)] == [row.id]

    def test_the_digest_is_deterministic_and_specific(self, well) -> None:
        first = SubjectKey(well_id=well.id, property_name="x" * 400)
        again = SubjectKey(well_id=well.id, property_name="x" * 400)
        other = SubjectKey(well_id=well.id, property_name="y" * 400)
        assert first.storage_key() == again.storage_key()
        assert first.storage_key() != other.storage_key()

    def test_an_over_long_legacy_subject_is_refused_rather_than_cut(self, session, well) -> None:
        """Free text cannot be digested (it has no canonical form), so it is rejected loudly."""
        repository = EngineeringRepository(session)
        with pytest.raises(ValidationError):
            _record(
                repository,
                well_id=well.id,
                inputs={"v": {"value": 1.0, "subject_key": "n" * (MAX_RENDERED_LENGTH + 1)}},
            )

    def test_a_subject_exactly_at_the_limit_is_stored_literally(self, session, well) -> None:
        repository = EngineeringRepository(session)
        exact = "l" * MAX_RENDERED_LENGTH
        row, _ = _record(
            repository, well_id=well.id, inputs={"v": {"value": 1.0, "subject_key": exact}}
        )
        session.flush()
        [item] = repository.calculation_inputs(row.id)
        assert item.subject_key == exact and len(item.subject_key) == MAX_RENDERED_LENGTH


# --------------------------------------------------------------------------- Case E
class TestCaseERevisionAndSupersession:
    def test_a_superseded_source_makes_the_dependency_stale_and_keeps_the_history(
        self, session, well, tmp_path: Path
    ) -> None:
        """The forensic case the foundation exists for.

        source version -> calculation input -> revision/supersession -> currency check.  The old
        number stays exactly what it was (a decision may have been made on it); the *dependency*
        is reported STALE; and nothing is re-run or invalidated by the check.
        """
        repository = EngineeringRepository(session)
        document = Document(
            id="doc-impact-1",
            filename="mud_report.xlsx",
            identity_path=str(tmp_path / "mud_report.xlsx"),
            sha256="a" * 64,
            size_bytes=10,
            extension=".xlsx",
        )
        old = DocumentVersion(
            id="ver-old",
            document_id=document.id,
            version_number=1,
            sha256="a" * 64,
            size_bytes=10,
            source_path=str(tmp_path / "mud_report.xlsx"),
            is_current=False,
        )
        new = DocumentVersion(
            id="ver-new",
            document_id=document.id,
            version_number=2,
            sha256="b" * 64,
            size_bytes=11,
            source_path=str(tmp_path / "mud_report.xlsx"),
            is_current=True,
        )
        session.add_all([document, old, new])
        session.flush()

        key = subject_key(well_id=well.id, property_name="mud_weight", record_state="ACTUAL")
        row, _ = _record(
            repository,
            well_id=well.id,
            origin=KnowledgeOrigin.DERIVED.value,
            provenance=[{"document_version_id": old.id}],
            document_id=document.id,
            document_version_id=old.id,
            inputs={"mw": {"value": "10.2 ppg", "subject_key": key}},
        )
        session.flush()

        # The historical record is intact and reconstructable.
        stored = repository.get_calculation(row.id)
        assert stored.inputs["mw"]["value"] == "10.2 ppg"
        assert stored.document_version_id == old.id
        assert stored.status == "COMPUTED", "the check reports; it does not invalidate"

        report = repository.calculation_impact(key)
        assert report["resolved"] is True
        assert report["calculations"] == 1
        assert report["counts"][DEPENDENCY_STALE] == 1
        assert report["entries"][0]["dependency"] == DEPENDENCY_STALE
        assert report["entries"][0]["document_version_id"] == old.id

        # Pointing the same calculation at the current version reports CURRENT.
        current, _ = _record(
            repository,
            method_id="hydraulics.ecd",
            method_version="1.1",
            well_id=well.id,
            origin=KnowledgeOrigin.DERIVED.value,
            provenance=[{"document_version_id": new.id}],
            document_id=document.id,
            document_version_id=new.id,
            inputs={"mw": {"value": "10.4 ppg", "subject_key": key}},
        )
        session.flush()
        refreshed = repository.calculation_impact(key)
        assert refreshed["counts"][DEPENDENCY_STALE] == 1
        assert refreshed["counts"][DEPENDENCY_CURRENT] == 1
        assert {entry["calculation_id"] for entry in refreshed["entries"]} == {row.id, current.id}

    def test_a_superseded_calculation_is_still_findable_but_filterable(self, session, well) -> None:
        repository = EngineeringRepository(session)
        key = subject_key(well_id=well.id, property_name="mud_weight", record_state="ACTUAL")
        first, _ = _record(
            repository, well_id=well.id, inputs={"mw": {"value": "10.2 ppg", "subject_key": key}}
        )
        session.flush()
        second, _ = _record(
            repository,
            well_id=well.id,
            inputs={"mw": {"value": "10.4 ppg", "subject_key": key}},
            supersedes_id=first.id,
        )
        session.flush()
        assert {row.id for row in repository.calculations_using(key)} == {first.id, second.id}
        assert [row.id for row in repository.calculations_using(key, current_only=True)] == [
            second.id
        ]
        assert repository.get_calculation(first.id).status == "SUPERSEDED"
        assert repository.get_calculation(first.id).inputs["mw"]["value"] == "10.2 ppg"


# --------------------------------------------------------------------------- the query's promises
class TestImpactQueryProperties:
    def test_one_calculation_is_reported_once_however_many_inputs_name_the_subject(
        self, session, well
    ) -> None:
        repository = EngineeringRepository(session)
        key = subject_key(well_id=well.id, property_name="mud_weight", record_state="ACTUAL")
        row, _ = _record(
            repository,
            well_id=well.id,
            inputs={
                "mw_a": {"value": "10.2 ppg", "subject_key": key},
                "mw_b": {"value": "10.2 ppg", "subject_key": key},
            },
        )
        session.flush()
        found = [item.id for item in repository.calculations_using(key)]
        assert found == [row.id], "a calculation is affected once, not once per input"

    def test_ordering_is_deterministic_and_repeatable(self, session, well) -> None:
        repository = EngineeringRepository(session)
        key = subject_key(well_id=well.id, property_name="mud_weight", record_state="ACTUAL")
        for index in range(5):
            _record(
                repository,
                method_id=f"m.{index}",
                well_id=well.id,
                inputs={"mw": {"value": float(index), "subject_key": key}},
            )
        session.flush()
        runs = [[row.id for row in repository.calculations_using(key)] for _ in range(3)]
        assert runs[0] == runs[1] == runs[2]
        assert len(runs[0]) == 5

    def test_scope_narrows_the_answer(self, session, well, tmp_path: Path) -> None:
        from drilling_intelligence.wells.repository import WellRepository

        wells = WellRepository(session)
        wells.get_or_create_workspace(str(tmp_path), name="Impact")
        project = wells.get_or_create_project("Impact")
        other = wells.create_well("A-9", project_id=project.id)
        session.flush()

        repository = EngineeringRepository(session)
        key = subject_key(well_id=well.id, property_name="mud_weight", record_state="ACTUAL")
        mine, _ = _record(
            repository, well_id=well.id, inputs={"mw": {"value": 10.2, "subject_key": key}}
        )
        _record(
            repository,
            method_id="cross.well",
            well_id=other.id,
            inputs={"mw": {"value": 10.2, "subject_key": key}},
        )
        session.flush()
        assert len(repository.calculations_using(key)) == 2
        assert [row.id for row in repository.calculations_using(key, well_id=well.id)] == [mine.id]

    def test_unresolved_is_a_different_answer_from_not_affected(self, session, well) -> None:
        """The distinction the foundation exists to make."""
        repository = EngineeringRepository(session)
        row, _ = _record(
            repository,
            well_id=well.id,
            inputs={"c": {"value": 1.0, "source": "mud_report.xlsx!Summary!B9"}},
        )
        session.flush()

        unresolved = repository.calculation_impact("mud_report.xlsx!Summary!B9")
        assert unresolved["resolved"] is False
        assert unresolved["calculations"] == 1
        assert unresolved["counts"][DEPENDENCY_UNRESOLVED] == 1
        assert unresolved["entries"][0]["calculation_id"] == row.id

        nothing = repository.calculation_impact(
            subject_key(well_id=well.id, property_name="nothing_here", record_state="ACTUAL")
        )
        assert nothing["resolved"] is True
        assert nothing["calculations"] == 0
        assert nothing["entries"] == []

    def test_an_empty_subject_is_refused(self, session) -> None:
        repository = EngineeringRepository(session)
        with pytest.raises(ValidationError):
            repository.calculations_using("   ")
        with pytest.raises(ValidationError):
            repository.calculation_impact("")

    def test_the_impact_report_is_read_only(self, session, well) -> None:
        repository = EngineeringRepository(session)
        key = subject_key(well_id=well.id, property_name="mud_weight", record_state="ACTUAL")
        _record(repository, well_id=well.id, inputs={"mw": {"value": 10.2, "subject_key": key}})
        session.flush()
        before = _fingerprint(session)
        repository.calculation_impact(key)
        repository.calculations_using(key)
        session.flush()
        assert _fingerprint(session) == before, "an impact query must not write"

    def test_the_impact_report_issues_a_bounded_number_of_queries(self, session, db, well) -> None:
        repository = EngineeringRepository(session)
        key = subject_key(well_id=well.id, property_name="mud_weight", record_state="ACTUAL")
        for index in range(3):
            _record(
                repository,
                method_id=f"m.{index}",
                well_id=well.id,
                inputs={"mw": {"value": float(index), "subject_key": key}},
            )
        session.flush()
        baseline = _select_count(db.engine, lambda: repository.calculation_impact(key))
        for index in range(3, 12):
            _record(
                repository,
                method_id=f"m.{index}",
                well_id=well.id,
                inputs={"mw": {"value": float(index), "subject_key": key}},
            )
        session.flush()
        assert _select_count(db.engine, lambda: repository.calculation_impact(key)) == baseline, (
            "the query count must not grow with the number of affected records"
        )


# --------------------------------------------------------------------------- legacy & identity
class TestLegacySubjectsAndIdentityStability:
    def test_a_legacy_subject_is_preserved_verbatim_and_labelled(self, session, well) -> None:
        repository = EngineeringRepository(session)
        for raw in (
            "mud_report.xlsx!Summary!B9",
            "well:A-3|mud_weight",
            "just-a-token",
            "property:mud_weight|state:ACTUAL",
        ):
            row, _ = _record(
                repository,
                method_id=f"legacy.{abs(hash(raw)) % 1000}",
                well_id=well.id,
                inputs={"v": {"value": 1.0, "subject_key": raw}},
            )
            session.flush()
            [item] = repository.calculation_inputs(row.id)
            assert item.subject_key == raw, "the original text is never rewritten"
            assert item.subject_kind == LEGACY_KIND and item.subject_id is None
            assert [found.id for found in repository.calculations_using(raw)] == [row.id], (
                "a legacy row stays reachable by the exact text it holds"
            )

    def test_resolve_input_subject_classifies_without_guessing(self) -> None:
        assert resolve_input_subject("") == (None, None, None)
        assert resolve_input_subject("   ") == (None, None, None)
        assert resolve_input_subject("well:w-1|property:p|state:ACTUAL") == (
            "well:w-1|property:p|state:ACTUAL",
            "well",
            "w-1",
        )
        assert resolve_input_subject("mud_report.xlsx!B9") == (
            "mud_report.xlsx!B9",
            LEGACY_KIND,
            None,
        )

    def test_re_recording_is_a_no_op_and_identity_is_unchanged(self, session, well) -> None:
        """Normalising the input index must not change what a calculation *is*."""
        repository = EngineeringRepository(session)
        key = subject_key(well_id=well.id, property_name="mud_weight", record_state="ACTUAL")
        first, created_first = _record(
            repository, well_id=well.id, inputs={"mw": {"value": "10.2 ppg", "subject_key": key}}
        )
        session.flush()
        identity = first.identity_key
        second, created_second = _record(
            repository, well_id=well.id, inputs={"mw": {"value": "10.2 ppg", "subject_key": key}}
        )
        session.flush()
        assert created_first is True and created_second is False
        assert second.id == first.id
        assert second.identity_key == identity

    def test_identity_is_the_payload_not_the_normalised_index(self, session, well) -> None:
        """Two spellings of one subject are two *payloads*, so they stay two records.

        The index resolves them to one dependency - which is the point - but ``identity_key`` is a
        hash of what the caller stored, and rewriting it would change the identity of a record
        somebody may already have quoted.
        """
        repository = EngineeringRepository(session)
        key = subject_key(well_id=well.id, property_name="mud_weight", record_state="ACTUAL")
        first, _ = _record(
            repository, well_id=well.id, inputs={"mw": {"value": "10.2 ppg", "subject_key": key}}
        )
        second, _ = _record(
            repository,
            well_id=well.id,
            inputs={
                "mw": {"value": "10.2 ppg", "subject_key": key.replace("mud_weight", "MUD_WEIGHT")}
            },
        )
        session.flush()
        assert first.id != second.id, "different stored payloads remain different records"
        assert {row.id for row in repository.calculations_using(key)} == {first.id, second.id}


# --------------------------------------------------------------------------- provenance
class TestInputProvenance:
    def test_a_supplied_input_provenance_is_kept(self, session, well) -> None:
        repository = EngineeringRepository(session)
        key = subject_key(well_id=well.id, property_name="mud_weight", record_state="ACTUAL")
        row, _ = _record(
            repository,
            well_id=well.id,
            inputs={
                "mw": {
                    "value": "10.2 ppg",
                    "subject_key": key,
                    "provenance": {"document_version_id": "ver-1", "locator": "B9"},
                }
            },
        )
        session.flush()
        [item] = repository.calculation_inputs(row.id)
        assert item.provenance == {"document_version_id": "ver-1", "locator": "B9"}

    def test_an_input_inherits_the_records_own_citation_rather_than_storing_nothing(
        self, session, well
    ) -> None:
        """The reproduced gap: a realistic derived calculation left the input with NULL evidence."""
        repository = EngineeringRepository(session)
        key = subject_key(well_id=well.id, property_name="mud_weight", record_state="ACTUAL")
        row, _ = _record(
            repository,
            well_id=well.id,
            origin=KnowledgeOrigin.DERIVED.value,
            provenance=[{"document_version_id": "ver-77"}],
            document_id="doc-77",
            document_version_id="ver-77",
            inputs={"mw": {"value": "10.2 ppg", "subject_key": key}},
        )
        session.flush()
        [item] = repository.calculation_inputs(row.id)
        assert item.provenance == {
            "document_version_id": "ver-77",
            "document_id": "doc-77",
            "inherited_from": "calculation",
        }

    def test_nothing_is_manufactured_for_a_record_that_cites_nothing(self, session, well) -> None:
        repository = EngineeringRepository(session)
        key = subject_key(well_id=well.id, property_name="mud_weight", record_state="ACTUAL")
        row, _ = _record(
            repository, well_id=well.id, inputs={"mw": {"value": "10.2 ppg", "subject_key": key}}
        )
        session.flush()
        [item] = repository.calculation_inputs(row.id)
        assert item.provenance is None, "a MANUAL record with no citation invents none"


# --------------------------------------------------------------------------- doctor
class TestIntegrityDiagnostic:
    def test_an_unresolved_subject_is_reported(self, session, well) -> None:
        repository = EngineeringRepository(session)
        row, _ = _record(
            repository,
            well_id=well.id,
            inputs={"v": {"value": 1.0, "subject_key": "mud_report.xlsx!Summary!B9"}},
        )
        session.flush()
        problems = check_calculation_dependencies(session)
        assert [problem.detail["finding"] for problem in problems] == ["UNRESOLVED_SUBJECT"]
        assert problems[0].detail["calculation_id"] == row.id
        assert problems[0].table == "calculation_input"

    def test_a_stale_input_version_is_reported(self, session, well, tmp_path: Path) -> None:
        repository = EngineeringRepository(session)
        document = Document(
            id="doc-stale",
            filename="r.xlsx",
            identity_path=str(tmp_path / "r.xlsx"),
            sha256="c" * 64,
            size_bytes=1,
            extension=".xlsx",
        )
        old = DocumentVersion(
            id="ver-stale-old",
            document_id=document.id,
            version_number=1,
            sha256="c" * 64,
            size_bytes=1,
            source_path=str(tmp_path / "r.xlsx"),
            is_current=False,
        )
        new = DocumentVersion(
            id="ver-stale-new",
            document_id=document.id,
            version_number=2,
            sha256="d" * 64,
            size_bytes=2,
            source_path=str(tmp_path / "r.xlsx"),
            is_current=True,
        )
        session.add_all([document, old, new])
        session.flush()
        key = subject_key(well_id=well.id, property_name="mud_weight", record_state="ACTUAL")
        _record(
            repository,
            well_id=well.id,
            origin=KnowledgeOrigin.DERIVED.value,
            provenance=[{"document_version_id": old.id}],
            document_id=document.id,
            document_version_id=old.id,
            inputs={"mw": {"value": "10.2 ppg", "subject_key": key}},
        )
        session.flush()
        findings = [
            problem.detail["finding"] for problem in check_calculation_dependencies(session)
        ]
        assert findings == ["STALE_INPUT_VERSION"]

    def test_a_clean_workspace_reports_nothing(self, session, well) -> None:
        repository = EngineeringRepository(session)
        key = subject_key(well_id=well.id, property_name="mud_weight", record_state="ACTUAL")
        _record(repository, well_id=well.id, inputs={"mw": {"value": 10.2, "subject_key": key}})
        session.flush()
        assert check_calculation_dependencies(session) == []

    def test_the_check_is_read_only(self, session, well) -> None:
        repository = EngineeringRepository(session)
        _record(
            repository,
            well_id=well.id,
            inputs={"v": {"value": 1.0, "subject_key": "legacy!ref"}},
        )
        session.flush()
        before = _fingerprint(session)
        check_calculation_dependencies(session)
        session.flush()
        assert _fingerprint(session) == before


# --------------------------------------------------------------------------- post-0008 repairs
class TestSubjectGrammarRepairs:
    """Defects found by re-auditing 0008 against the repository rather than against its report."""

    def test_every_anchor_kind_round_trips_through_render_and_parse(self) -> None:
        """``document`` and ``project`` are both an anchor kind *and* a trailing field name.

        ``render`` writes an anchor first and those fields last, but ``parse`` used to key purely
        off the token, so ``document:doc-5|property:t`` came back as ``property:t|document:doc-5``.
        The round trip failed, ``is_canonical_subject`` therefore said False, and a perfectly good
        document-anchored subject was written off as unrecognisable legacy text - it could never
        be resolved to ``(document, doc-5)`` and never found by a change-impact query.
        """
        for kind in ANCHOR_KINDS:
            subject = SubjectKey(
                entity_type=kind, entity_id="x-1", property_name="p", record_state="ACTUAL"
            )
            rendered = subject.render()
            assert rendered.startswith(f"{kind}:x-1|"), rendered
            assert SubjectKey.parse(rendered).render() == rendered, kind
            assert is_canonical_subject(rendered), kind
            assert subject.anchor() == (kind, "x-1")

    def test_a_document_or_project_anchor_is_resolvable_end_to_end(self, session, well) -> None:
        repository = EngineeringRepository(session)
        for kind, identifier in (("document", "doc-5"), ("project", "prj-2")):
            key = SubjectKey(entity_type=kind, entity_id=identifier, property_name="title").render()
            row, _ = _record(
                repository,
                method_id=f"anchor.{kind}",
                well_id=well.id,
                inputs={"v": {"value": 1.0, "subject_key": key}},
            )
            session.flush()
            [item] = repository.calculation_inputs(row.id)
            assert (item.subject_kind, item.subject_id) == (kind, identifier)
            report = repository.calculation_impact(key)
            assert report["resolved"] is True
            assert [entry["calculation_id"] for entry in report["entries"]] == [row.id]

    def test_a_trailing_scope_field_is_still_a_field_not_an_anchor(self) -> None:
        """The disambiguation must be positional, not a blanket rule about the token."""
        subject = SubjectKey(well_id="w-1", property_name="p", project_id="prj-9")
        rendered = subject.render()
        assert rendered == "well:w-1|property:p|project:prj-9"
        parsed = SubjectKey.parse(rendered)
        assert parsed.project_id == "prj-9" and parsed.entity_type == ""
        assert subject.anchor() == ("well", "w-1"), "the well still owns the subject"


class TestMigratedWorkspaceLookups:
    """A workspace that came through 0008 holds text a workspace written today would not."""

    @staticmethod
    def _legacy_input(session, calculation_id: str, name: str, subject: str, kind, identifier):
        """A row as the 0008 backfill would have left it: original text, resolved reference."""
        session.add(
            CalculationInput(
                id=f"cain-{name}",
                calculation_id=calculation_id,
                name=name,
                value=1.0,
                unit="",
                dimension="",
                source_kind="knowledge",
                subject_key=subject,
                subject_kind=kind,
                subject_id=identifier,
            )
        )

    def test_a_historical_spelling_is_found_by_the_canonical_key(self, session, well) -> None:
        """The migration preserves text verbatim; the query must still resolve it.

        Normalising these rows in the migration would rewrite history to suit the reader, so the
        *read* path carries the normalization instead - and it does so by canonicalising the
        spellings actually recorded against the anchor, rather than re-encoding the rule in SQL.
        Re-encoding is what let the migration and the write path disagree in the first place.
        """
        repository = EngineeringRepository(session)
        row, _ = _record(repository, well_id=well.id, inputs={"placeholder": 1.0})
        session.flush()
        for index, spelling in enumerate(
            (
                f"well:{well.id}|property:MUD_WEIGHT|state:ACTUAL",
                f"well:{well.id}|property:mud weight|state:ACTUAL",
                f"well:{well.id}|property:Mud-Weight|state:actual",
            )
        ):
            self._legacy_input(session, row.id, f"old{index}", spelling, "well", well.id)
        session.flush()

        canonical = subject_key(well_id=well.id, property_name="mud_weight", record_state="ACTUAL")
        found = repository.calculations_using(canonical)
        assert [item.id for item in found] == [row.id], (
            "a migrated row must be findable by the canonical key, not only by its own spelling"
        )
        report = repository.calculation_impact(canonical)
        assert report["resolved"] is True and report["calculations"] == 1
        assert len(report["entries"]) == 3, "each historical spelling is its own input row"

    def test_a_different_property_on_the_same_anchor_is_not_swept_in(self, session, well) -> None:
        """Widening the match to historical spellings must not widen it to other subjects."""
        repository = EngineeringRepository(session)
        row, _ = _record(repository, well_id=well.id, inputs={"placeholder": 1.0})
        session.flush()
        self._legacy_input(
            session,
            row.id,
            "other",
            f"well:{well.id}|property:PORE_PRESSURE|state:ACTUAL",
            "well",
            well.id,
        )
        session.flush()
        assert (
            repository.calculations_using(
                subject_key(well_id=well.id, property_name="mud_weight", record_state="ACTUAL")
            )
            == []
        )
        assert (
            len(
                repository.calculations_using(
                    subject_key(
                        well_id=well.id, property_name="pore_pressure", record_state="ACTUAL"
                    )
                )
            )
            == 1
        )

    def test_a_row_the_backfill_left_legacy_is_still_reachable(self, session, well) -> None:
        """An escaped key is one the SQL backfill deliberately refused to parse.

        It stays ``legacy``, so ``doctor`` reports it as unresolved - but the calculation that
        depends on it must not become invisible, because the text is exact and matching it
        exactly invents nothing.
        """
        repository = EngineeringRepository(session)
        row, _ = _record(repository, well_id=well.id, inputs={"placeholder": 1.0})
        session.flush()
        escaped = SubjectKey(well_id="w|1", property_name="p").render()
        self._legacy_input(session, row.id, "esc", escaped, LEGACY_KIND, None)
        session.flush()
        assert [item.id for item in repository.calculations_using(escaped)] == [row.id]
        findings = [
            problem.detail["finding"]
            for problem in check_calculation_dependencies(session)
            if problem.detail.get("input") == "esc"
        ]
        assert findings == ["UNRESOLVED_SUBJECT"], "it is reachable, but honestly labelled"

    def test_the_query_count_does_not_grow_with_the_number_of_rows(self, session, db, well) -> None:
        """Resolving spellings reads the anchor's distinct keys - bounded, not per-row."""
        repository = EngineeringRepository(session)
        row, _ = _record(repository, well_id=well.id, inputs={"placeholder": 1.0})
        session.flush()
        canonical = subject_key(well_id=well.id, property_name="mud_weight", record_state="ACTUAL")
        self._legacy_input(session, row.id, "s0", canonical, "well", well.id)
        session.flush()
        baseline = _select_count(db.engine, lambda: repository.calculation_impact(canonical))
        for index in range(1, 10):
            self._legacy_input(
                session,
                row.id,
                f"s{index}",
                f"well:{well.id}|property:MUD_WEIGHT|state:ACTUAL",
                "well",
                well.id,
            )
        session.flush()
        assert (
            _select_count(db.engine, lambda: repository.calculation_impact(canonical)) == baseline
        )
