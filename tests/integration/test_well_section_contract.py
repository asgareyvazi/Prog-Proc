"""The WellSection contract: a plan is never mistaken for what happened.

These tests exist because of one measured failure.  A caller wrote a section's *planned* bottom depth
through ``update_section(..., state=PLANNED)``; ``plan_actual_summary`` then read that same column back
as the section's achieved depth and reported ``ON_PLAN`` with a variance of 0.0 for a hole nobody had
drilled.  Nothing in the schema was wrong - ``top_depth_value``/``bottom_depth_value`` have always been
the as-drilled interval, and the planned depth of a section has always lived on the program target that
governs it - the defect was that the repository accepted a plan into a column that reports fact.

So the contract proved here is:

*   a section's depth columns are the **as-drilled** ones, and a ``PLANNED`` write of them is refused
    with the instruction that says where the plan actually goes;
*   duration and mud weight keep their genuine planned/actual pairs, neither overwriting the other;
*   a section that came out of a document can show the document, and one a person entered says so;
*   a section is ``(well_id, name)`` and asking twice returns the same row;
*   a failure leaves nothing behind.

The suite also pins the four plan-versus-actual outcomes that matter, using the corpus's real promoted
program rather than hand-built rows, so "missing actual" stays visibly different from "on plan".
"""

from __future__ import annotations

import pytest
from sqlalchemy import func, select
from tests.fixtures.fieldops import ingest, promote, well_id_for

from drilling_intelligence.core.enums import KnowledgeOrigin, RecordState
from drilling_intelligence.core.errors import ValidationError
from drilling_intelligence.database.integrity import check_promoted_evidence
from drilling_intelligence.database.models import Document, DocumentVersion, Well, WellSection
from drilling_intelligence.engineering.repository import EngineeringRepository
from drilling_intelligence.wells.repository import WellRepository

PROGRAM_FILE = "well_a3_program_rev12.pdf"
SECTION_NAME = "12 1/4 in"
PLANNED_DEPTH_FT = 10450.0
PLANNED_MUD_PPG = 10.2


@pytest.fixture
def promoted(workspace):
    """The real corpus, ingested and promoted: a current program with one target, and no sections."""
    ingest(workspace)
    promote(workspace)
    return workspace


def _well(session, workspace) -> Well:
    return session.get(Well, well_id_for(workspace, "A-3"))


#: A section name the promoted program does not use, for the tests that need to build one by hand.
#: Since C2 the corpus's own 12 1/4 in section is created by promotion, so a helper that asked for
#: that name would be handed the promoted row and would be testing promotion rather than the contract.
MANUAL_SECTION_NAME = "9 7/8 in"


def _section(session, workspace, name: str = MANUAL_SECTION_NAME, **kwargs) -> WellSection:
    return WellRepository(session).get_or_create_section(
        _well(session, workspace), name, hole_size_in=9.875, **kwargs
    )


def _promoted_section(session, workspace) -> WellSection:
    """The 12 1/4 in section promotion created from the drilling program.

    The plan-versus-actual tests need the section the *programme* is about, because the planned side
    of the comparison comes from its target.  It already exists by the time these tests run.
    """
    return session.scalar(
        select(WellSection).where(
            WellSection.well_id == well_id_for(workspace, "A-3"),
            WellSection.name == SECTION_NAME,
        )
    )


def _depth_row(session, workspace) -> dict:
    rows = EngineeringRepository(session).plan_actual_summary(well_id=well_id_for(workspace, "A-3"))
    return next(row for row in rows if row["metric"] == "depth_md")


# -- the depth contract -------------------------------------------------------
class TestDepthContract:
    def test_a_planned_depth_is_refused_rather_than_stored_as_a_fact(self, promoted) -> None:
        """The measured defect, now a rejection: the plan has somewhere truthful to go."""
        with promoted.database.session() as session:
            section = _promoted_section(session, promoted)
            with pytest.raises(ValidationError) as caught:
                WellRepository(session).update_section(
                    section, {"bottom_depth": (PLANNED_DEPTH_FT, "ft")}, state=RecordState.PLANNED
                )
            message = str(caught.value)
            assert "bottom_depth" in message and "PLANNED" in message
            assert "planned_depth_md_value" in message, (
                "the refusal has to say where a planned depth belongs, or the caller will "
                "find somewhere worse to put it"
            )
            assert section.bottom_depth_value is None, "nothing was written"

    def test_a_planned_top_depth_is_refused_too(self, promoted) -> None:
        with promoted.database.session() as session:
            section = _promoted_section(session, promoted)
            with pytest.raises(ValidationError, match="top_depth"):
                WellRepository(session).update_section(
                    section, {"top_depth": (8500.0, "ft")}, state=RecordState.PLANNED
                )

    def test_an_actual_depth_is_stored_and_read_back_as_the_actual(self, promoted) -> None:
        with promoted.database.session() as session:
            section = _promoted_section(session, promoted)
            applied = WellRepository(session).update_section(
                section,
                {"top_depth": (8500.0, "ft"), "bottom_depth": (10390.0, "ft")},
                state=RecordState.ACTUAL,
            )
            session.commit()
            assert sorted(applied) == ["ACTUAL.bottom_depth", "ACTUAL.top_depth"]
            assert section.bottom_depth_value == 10390.0
            row = _depth_row(session, promoted)
            assert row["actual"] == 10390.0 and row["planned"] == PLANNED_DEPTH_FT

    def test_a_section_with_no_actual_depth_reports_no_actual_not_on_plan(self, promoted) -> None:
        """I2/I3: the whole point.  Missing is missing; it is not agreement with the plan."""
        with promoted.database.session() as session:
            session.commit()
            row = _depth_row(session, promoted)
        assert row["planned"] == PLANNED_DEPTH_FT, "the plan comes from the promoted target"
        assert row["actual"] is None
        assert row["variance"] is None
        assert row["status"] == "NO_ACTUAL", (
            "a hole nobody has drilled must never be reported as ON_PLAN"
        )

    def test_a_revised_plan_does_not_touch_the_depth_that_was_drilled(self, promoted) -> None:
        """I5: the plan moves, the hole does not."""
        with promoted.database.session() as session:
            section = _promoted_section(session, promoted)
            WellRepository(session).update_section(
                section, {"bottom_depth": (10390.0, "ft")}, state=RecordState.ACTUAL
            )
            target = EngineeringRepository(session).list_targets(
                session.scalar(select(func.min(WellSection.id)).where(WellSection.id == section.id))
                and _current_program_id(session)
            )[0]
            target.planned_depth_md_value = 10900.0
            session.commit()
            assert section.bottom_depth_value == 10390.0
            row = _depth_row(session, promoted)
        assert row["planned"] == 10900.0 and row["actual"] == 10390.0
        assert row["status"] == "VARIANCE"


def _current_program_id(session) -> str:
    from drilling_intelligence.database.models import DrillingProgram

    return session.scalar(select(DrillingProgram.id).where(DrillingProgram.is_current.is_(True)))


# -- the pairs that genuinely are pairs ---------------------------------------
class TestPlannedActualPairs:
    def test_planned_and_actual_mud_weight_are_independent(self, promoted) -> None:
        """I6: two columns, two meanings, neither overwriting the other."""
        with promoted.database.session() as session:
            section = _section(session, promoted)
            wells = WellRepository(session)
            wells.update_section(section, {"mud_weight": (10.2, "ppg")}, state=RecordState.PLANNED)
            wells.update_section(section, {"mud_weight": (10.6, "ppg")}, state=RecordState.ACTUAL)
            session.commit()
            assert section.planned_mud_weight_value == 10.2
            assert section.actual_mud_weight_value == 10.6

    def test_an_actual_duration_does_not_overwrite_the_planned_one(self, promoted) -> None:
        """I4."""
        with promoted.database.session() as session:
            section = _section(session, promoted)
            wells = WellRepository(session)
            wells.update_section(section, {"duration_days": 12.0}, state=RecordState.PLANNED)
            wells.update_section(section, {"duration_days": 14.5}, state=RecordState.ACTUAL)
            session.commit()
            assert section.planned_duration_days == 12.0
            assert section.actual_duration_days == 14.5

    def test_a_forecast_is_still_refused(self, promoted) -> None:
        """The pre-existing rule survives: only PLANNED and ACTUAL are pair targets."""
        with promoted.database.session() as session:
            section = _section(session, promoted)
            with pytest.raises(ValidationError, match="FORECAST"):
                WellRepository(session).update_section(
                    section, {"duration_days": 13.0}, state=RecordState.FORECAST
                )


# -- provenance ---------------------------------------------------------------
class TestProvenance:
    def test_a_section_read_from_a_document_carries_the_document(self, promoted) -> None:
        """I7: the section can answer "which source says this hole section exists"."""
        with promoted.database.session() as session:
            document = session.scalar(select(Document).where(Document.filename == PROGRAM_FILE))
            version = session.get(DocumentVersion, document.current_version_id)
            section = _section(
                session,
                promoted,
                origin=KnowledgeOrigin.DERIVED.value,
                provenance=[
                    {
                        "document_id": document.id,
                        "document_version_id": version.id,
                        "filename": PROGRAM_FILE,
                        "source_sha256": version.sha256,
                        "excerpt": "12 1/4 in section",
                        "locator": {"kind": "pdf", "page": 1},
                    }
                ],
                document_id=document.id,
                document_version_id=version.id,
            )
            session.commit()

            assert section.origin == KnowledgeOrigin.DERIVED.value
            assert section.document_id == document.id
            assert section.document_version_id == version.id
            cited = section.provenance[0]
            assert cited["source_sha256"] == version.sha256
            assert cited["locator"]["page"] == 1
            assert check_promoted_evidence(session) == [], (
                "a derived section that cites its source is not an integrity finding"
            )

    def test_a_derived_section_that_cites_nothing_is_refused(self, promoted) -> None:
        """I8: an origin is a promise, and the promise is checked where the row is written."""
        with (
            promoted.database.session() as session,
            pytest.raises(ValidationError, match="cite the source"),
        ):
            _section(session, promoted, origin=KnowledgeOrigin.DERIVED.value)

    def test_a_section_a_person_entered_is_manual_and_is_not_asked_for_evidence(
        self, promoted
    ) -> None:
        with promoted.database.session() as session:
            section = _section(session, promoted)
            session.commit()
            assert section.origin == KnowledgeOrigin.MANUAL.value
            assert list(section.provenance or []) == []
            assert check_promoted_evidence(session) == []

    def test_the_doctor_reports_a_derived_section_with_no_evidence(self, promoted) -> None:
        """The checker really is looking at this table, not passing it by."""
        with promoted.database.session() as session:
            section = _section(session, promoted)
            section.origin = KnowledgeOrigin.DERIVED.value  # bypass the repository on purpose
            session.flush()
            problems = check_promoted_evidence(session)
            assert [problem.table for problem in problems] == ["well_section"]
            assert "cites no evidence" in problems[0].problem
            session.rollback()


# -- identity -----------------------------------------------------------------
class TestIdentity:
    def test_the_same_section_asked_for_twice_is_one_row(self, promoted) -> None:
        """I9."""
        with promoted.database.session() as session:
            first = _section(session, promoted)
            session.commit()
            second = _section(session, promoted)
            session.commit()
            assert first.id == second.id
            same_name = session.scalars(
                select(WellSection).where(WellSection.name == MANUAL_SECTION_NAME)
            ).all()
            assert len(same_name) == 1, "asking twice must not add a second row"

    def test_an_existing_section_does_not_have_its_origin_restated(self, promoted) -> None:
        """A durable fact about the well is not re-sourced by the next document to mention it."""
        with promoted.database.session() as session:
            first = _section(session, promoted)
            session.commit()
            again = WellRepository(session).get_or_create_section(
                _well(session, promoted),
                MANUAL_SECTION_NAME,
                origin=KnowledgeOrigin.DERIVED.value,
                provenance=[{"document_id": "doc-other"}],
            )
            session.commit()
            assert again.id == first.id
            assert again.origin == KnowledgeOrigin.MANUAL.value
            assert list(again.provenance or []) == []

    def test_two_sections_of_the_same_hole_size_stay_distinct(self, promoted) -> None:
        """I10: the size is not the identity, so a sidetrack is its own row when it is named."""
        with promoted.database.session() as session:
            original = _promoted_section(session, promoted)
            sidetrack = WellRepository(session).get_or_create_section(
                _well(session, promoted), "12 1/4 in ST1", hole_size_in=12.25
            )
            session.commit()
            assert original.id != sidetrack.id
            assert original.hole_size_in == sidetrack.hole_size_in == 12.25

    def test_a_section_name_is_scoped_to_its_well(self, promoted) -> None:
        with promoted.database.session() as session:
            a3 = _promoted_section(session, promoted)
            b11 = WellRepository(session).get_or_create_section(
                session.get(Well, well_id_for(promoted, "B-11")), SECTION_NAME, hole_size_in=12.25
            )
            session.commit()
            assert a3.id != b11.id, "two wells may each have a 12 1/4 in section"

    def test_the_database_refuses_a_duplicate_name_on_one_well(self, promoted) -> None:
        """The identity is not merely a convention in the repository."""
        from sqlalchemy.exc import IntegrityError

        from drilling_intelligence.core.ids import new_id

        with promoted.database.session() as session:
            well = _well(session, promoted)
            _section(session, promoted)
            session.flush()
            session.add(
                WellSection(id=new_id("sec"), well_id=well.id, sequence=2, name=SECTION_NAME)
            )
            with pytest.raises(IntegrityError):
                session.flush()
            session.rollback()


# -- transactions -------------------------------------------------------------
class TestTransaction:
    def test_a_failed_section_write_leaves_nothing_behind(self, promoted) -> None:
        """I12: the section and its numbers land together or not at all."""
        with promoted.database.session() as session:
            before = session.scalar(select(func.count()).select_from(WellSection))
        assert before == 1, "the programme's own section, and nothing else yet"

        class Boom(RuntimeError):
            pass

        with pytest.raises(Boom), promoted.database.session() as session:
            section = _section(session, promoted)
            WellRepository(session).update_section(
                section, {"bottom_depth": (10390.0, "ft")}, state=RecordState.ACTUAL
            )
            raise Boom("the caller failed after the section was written")

        with promoted.database.session() as session:
            assert session.scalar(select(func.count()).select_from(WellSection)) == before, (
                "the section the failed write created must be gone"
            )

    def test_a_refused_planned_depth_does_not_poison_the_rest_of_the_write(self, promoted) -> None:
        """The refusal is a rejected write, not a half-applied one."""
        with promoted.database.session() as session:
            section = _section(session, promoted)
            with pytest.raises(ValidationError):
                WellRepository(session).update_section(
                    section,
                    {"duration_days": 12.0, "bottom_depth": (10450.0, "ft")},
                    state=RecordState.PLANNED,
                )
            assert section.planned_duration_days is None, (
                "the duration must not be applied by a call that was refused"
            )
            assert section.bottom_depth_value is None
            session.rollback()


# -- plan versus actual, end to end -------------------------------------------
class TestPlanActual:
    def _summary(self, workspace, *, actual: float | None):
        with workspace.database.session() as session:
            section = _promoted_section(session, workspace)
            if actual is not None:
                WellRepository(session).update_section(
                    section, {"bottom_depth": (actual, "ft")}, state=RecordState.ACTUAL
                )
            session.commit()
            return _depth_row(session, workspace)

    def test_planned_only_is_not_on_plan(self, promoted) -> None:
        row = self._summary(promoted, actual=None)
        assert (row["planned"], row["actual"], row["status"]) == (
            PLANNED_DEPTH_FT,
            None,
            "NO_ACTUAL",
        )

    def test_actual_equal_to_plan_is_on_plan(self, promoted) -> None:
        row = self._summary(promoted, actual=PLANNED_DEPTH_FT)
        assert row["status"] == "ON_PLAN" and row["variance"] == 0.0

    def test_actual_below_plan_is_a_variance(self, promoted) -> None:
        row = self._summary(promoted, actual=10390.0)
        assert row["status"] == "VARIANCE" and row["variance"] == pytest.approx(-60.0)

    def test_actual_beyond_plan_is_a_variance_too(self, promoted) -> None:
        row = self._summary(promoted, actual=10500.0)
        assert row["status"] == "VARIANCE" and row["variance"] == pytest.approx(50.0)

    def test_the_planned_mud_weight_comes_from_the_program_and_the_actual_stays_missing(
        self, promoted
    ) -> None:
        with promoted.database.session() as session:
            rows = {
                row["metric"]: row
                for row in EngineeringRepository(session).plan_actual_summary(
                    well_id=well_id_for(promoted, "A-3")
                )
            }
        mud = rows["mud_weight"]
        assert mud["planned"] == PLANNED_MUD_PPG and mud["unit"] == "ppg"
        assert mud["actual"] is None and mud["status"] == "NO_ACTUAL"


# -- the boundary C2 does not cross -------------------------------------------
class TestPopulationBoundary:
    """C2 populates the *planned* section and nothing else.

    The prerequisite task asserted that promotion created no sections at all; C2 is exactly the change
    that makes it create one, so what is pinned here is the new boundary - one section, from the
    programme, with no actual data and no speculative attachment of records that state no section.
    """

    def test_promotion_creates_only_the_section_the_programme_names(self, promoted) -> None:
        with promoted.database.read_only() as session:
            names = sorted(session.scalars(select(WellSection.name)))
        assert names == [SECTION_NAME]

    def test_promoting_again_creates_no_further_section(self, promoted) -> None:
        promote(promoted)
        promote(promoted)
        with promoted.database.read_only() as session:
            assert session.scalar(select(func.count()).select_from(WellSection)) == 1

    def test_no_actual_record_is_attached_without_its_own_section_evidence(self, promoted) -> None:
        """NPT, operations, events and problems state no section, so none of them gets one.

        Attaching them to the only section on the well would look complete and be a guess - and the
        guess would be silently wrong the moment a second section exists.
        """
        from drilling_intelligence.database.models import (
            NptRecord,
            ProblemOccurrence,
            WellEvent,
            WellOperation,
        )

        with promoted.database.read_only() as session:
            for model in (NptRecord, WellOperation, WellEvent, ProblemOccurrence):
                attached = [row.section_id for row in session.scalars(select(model))]
                assert attached and set(attached) == {None}, model.__tablename__

    def test_no_knowledge_item_is_attached_to_the_new_section(self, promoted) -> None:
        from drilling_intelligence.database.models import KnowledgeItem

        with promoted.database.read_only() as session:
            assert [
                row.id for row in session.scalars(select(KnowledgeItem)) if row.section_id
            ] == []
