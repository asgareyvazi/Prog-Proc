"""C2: the drilling program populates the hole section it is a plan for, and nothing else.

A program states that a 12 1/4 in hole is going to be drilled on this well.  That is a real, durable
fact about the well - it is what makes the section addressable before anyone spuds it - so promotion
now creates the ``WellSection`` and points the target at it.  What the program emphatically does *not*
state is what happened, so the section arrives with its as-drilled columns empty and stays that way
until an actual source supplies them.

The other half of this suite is about what C2 deliberately refuses to do.  The corpus's daily report
says ``Section: 12 1/4 in intermediate`` in prose; its only structured section-ish field is a
``hole_size_in`` of 12.25 whose excerpt is ``12 1/4 in bit`` - a *bit* size, in a document that never
names a section in a form the platform can read.  Attaching those NPT hours to the one section on the
well would look complete and be a guess, and the guess becomes silently wrong the day a second section
exists.  So they stay NULL, and these tests hold that line.
"""

from __future__ import annotations

import pytest
from sqlalchemy import func, select
from tests.fixtures.fieldops import ingest, promote, well_id_for

from drilling_intelligence.cli.app import main as main_entry
from drilling_intelligence.core.enums import KnowledgeOrigin
from drilling_intelligence.database.integrity import (
    check_promoted_evidence,
    check_well_hierarchy,
)
from drilling_intelligence.database.models import (
    Document,
    DocumentVersion,
    DrillingProgram,
    KnowledgeItem,
    NptRecord,
    ProblemOccurrence,
    ProgramTarget,
    Well,
    WellEvent,
    WellOperation,
    WellSection,
)
from drilling_intelligence.engineering.repository import EngineeringRepository
from drilling_intelligence.operations.service import OperationalService
from drilling_intelligence.wells.repository import WellRepository

PROGRAM_FILE = "well_a3_program_rev12.pdf"
SECTION_NAME = "12 1/4 in"
DDR_SECTION_TEXT = "12 1/4 in intermediate"
PLANNED_DEPTH_FT = 10450.0
HOLE_SIZE_IN = 12.25


@pytest.fixture
def promoted(workspace):
    ingest(workspace)
    promote(workspace)
    return workspace


def _section(workspace, name: str = SECTION_NAME) -> WellSection:
    with workspace.database.read_only() as session:
        return session.scalar(
            select(WellSection).where(
                WellSection.well_id == well_id_for(workspace, "A-3"), WellSection.name == name
            )
        )


def _target(workspace) -> ProgramTarget:
    with workspace.database.read_only() as session:
        return session.scalar(
            select(ProgramTarget)
            .join(DrillingProgram, DrillingProgram.id == ProgramTarget.program_id)
            .where(DrillingProgram.is_current.is_(True))
        )


def _publish_revision(workspace, *, section_td: str) -> None:
    """A second revision of the same programme, built by the corpus generator."""
    from tests.fixtures.generate import build_program_pdf

    from drilling_intelligence.ingestion.pipeline import IngestionPipeline

    build_program_pdf(workspace.root / "corpus" / PROGRAM_FILE, section_td=section_td)
    IngestionPipeline(
        settings=workspace.settings, workspace_root=workspace.root, database=workspace.database
    ).run(root=workspace.root / "corpus", well_id=well_id_for(workspace, "A-3"))


# -- A. program to section ----------------------------------------------------
class TestProgramCreatesItsSection:
    def test_the_programme_creates_the_section_it_plans(self, promoted) -> None:
        section = _section(promoted)
        assert section is not None
        assert section.name == SECTION_NAME
        assert section.hole_size_in == HOLE_SIZE_IN
        assert section.well_id == well_id_for(promoted, "A-3")

    def test_the_target_points_at_that_section(self, promoted) -> None:
        target, section = _target(promoted), _section(promoted)
        assert target.section_id == section.id
        assert target.name == SECTION_NAME, "the programme's own wording is still kept"

    def test_the_planned_depth_stays_on_the_target(self, promoted) -> None:
        """The section is not where a plan lives, and C2 does not move it there."""
        assert _target(promoted).planned_depth_md_value == PLANNED_DEPTH_FT
        section = _section(promoted)
        assert section.top_depth_value is None
        assert section.bottom_depth_value is None

    def test_a_plan_writes_no_actual_of_any_kind(self, promoted) -> None:
        """I18: the principal invariant - an intention must not arrive as an observation."""
        section = _section(promoted)
        assert section.actual_duration_days is None
        assert section.actual_mud_weight_value is None
        assert section.bottom_depth_value is None

    def test_a_plan_writes_no_planned_numbers_onto_the_section_either(self, promoted) -> None:
        """The target owns the plan; duplicating it onto the section would let the two disagree."""
        section = _section(promoted)
        assert section.planned_duration_days is None
        assert section.planned_mud_weight_value is None

    def test_the_section_is_derived_and_says_which_document_says_so(self, promoted) -> None:
        section = _section(promoted)
        assert section.origin == KnowledgeOrigin.DERIVED.value
        with promoted.database.read_only() as session:
            document = session.scalar(select(Document).where(Document.filename == PROGRAM_FILE))
            version = session.get(DocumentVersion, document.current_version_id)
        assert section.document_id == document.id
        assert section.document_version_id == version.id

    def test_the_section_provenance_is_the_document_not_the_clock(self, promoted) -> None:
        """A complete chain: section to document version to the bytes on disk, and a locator."""
        import hashlib

        section = _section(promoted)
        cited = (section.provenance or [])[0]
        with promoted.database.read_only() as session:
            version = session.get(DocumentVersion, section.document_version_id)
        on_disk = hashlib.sha256((promoted.root / "corpus" / PROGRAM_FILE).read_bytes()).hexdigest()
        assert cited["document_version_id"] == version.id
        assert cited["source_sha256"] == version.sha256 == on_disk
        assert cited["locator"]["page"] == 1
        assert cited["excerpt"]

    def test_the_doctor_stays_clean(self, promoted) -> None:
        with promoted.database.read_only() as session:
            assert check_promoted_evidence(session) == []
            assert check_well_hierarchy(session) == []


# -- B. idempotence -----------------------------------------------------------
class TestIdempotence:
    def test_promoting_three_times_creates_one_section(self, promoted) -> None:
        first = _section(promoted).id
        ids = [first]
        for _ in range(2):
            promote(promoted)
            ids.append(_section(promoted).id)
        assert len(set(ids)) == 1, ids
        with promoted.database.read_only() as session:
            assert session.scalar(select(func.count()).select_from(WellSection)) == 1

    def test_the_target_link_is_stable_across_repeats(self, promoted) -> None:
        before = _target(promoted).section_id
        promote(promoted)
        promote(promoted)
        assert _target(promoted).section_id == before

    def test_repeating_does_not_grow_the_provenance(self, promoted) -> None:
        before = list(_section(promoted).provenance or [])
        promote(promoted)
        assert list(_section(promoted).provenance or []) == before


# -- C. revision --------------------------------------------------------------
class TestRevision:
    def test_a_new_revision_reuses_the_same_logical_section(self, promoted) -> None:
        """The plan is revised; the hole is not.  One section, two programmes."""
        before = _section(promoted).id
        _publish_revision(promoted, section_td="10,900")
        promote(promoted)

        with promoted.database.read_only() as session:
            sections = list(session.scalars(select(WellSection)))
            programs = sorted(
                session.scalars(select(DrillingProgram)), key=lambda row: row.revision
            )
            links = {
                row.planned_depth_md_value: row.section_id
                for row in session.scalars(select(ProgramTarget))
            }
        assert [row.id for row in sections] == [before], "no second section for a second revision"
        assert [(row.revision, row.is_current) for row in programs] == [(1, False), (2, True)]
        assert set(links) == {PLANNED_DEPTH_FT, 10900.0}
        assert set(links.values()) == {before}, "both revisions describe the same hole"

    def test_the_superseded_plan_keeps_its_own_number(self, promoted) -> None:
        _publish_revision(promoted, section_td="10,900")
        promote(promoted)
        with promoted.database.read_only() as session:
            old = session.scalar(
                select(DrillingProgram).where(DrillingProgram.is_current.is_(False))
            )
            target = session.scalar(select(ProgramTarget).where(ProgramTarget.program_id == old.id))
        assert old.status == "SUPERSEDED"
        assert target.planned_depth_md_value == PLANNED_DEPTH_FT

    def test_a_revision_does_not_restate_where_the_section_came_from(self, promoted) -> None:
        """Provenance belongs to the document that first named the hole, and is not overwritten."""
        before = _section(promoted)
        cited_before = list(before.provenance or [])
        version_before = before.document_version_id
        _publish_revision(promoted, section_td="10,900")
        promote(promoted)
        after = _section(promoted)
        assert list(after.provenance or []) == cited_before
        assert after.document_version_id == version_before

    def test_a_revision_does_not_touch_actual_data(self, promoted) -> None:
        from drilling_intelligence.core.enums import RecordState

        with promoted.database.session() as session:
            section = session.get(WellSection, _section(promoted).id)
            WellRepository(session).update_section(
                section, {"bottom_depth": (10390.0, "ft")}, state=RecordState.ACTUAL
            )
            session.commit()
        _publish_revision(promoted, section_td="10,900")
        promote(promoted)
        assert _section(promoted).bottom_depth_value == 10390.0


# -- D. actual data -----------------------------------------------------------
class TestActualSide:
    def test_an_actual_depth_can_be_added_to_the_planned_section(self, promoted) -> None:
        from drilling_intelligence.core.enums import RecordState

        with promoted.database.session() as session:
            section = session.get(WellSection, _section(promoted).id)
            WellRepository(session).update_section(
                section, {"bottom_depth": (10390.0, "ft")}, state=RecordState.ACTUAL
            )
            session.commit()
        section = _section(promoted)
        assert section.bottom_depth_value == 10390.0
        assert _target(promoted).planned_depth_md_value == PLANNED_DEPTH_FT, "plan untouched"

    def test_a_section_that_exists_before_its_programme_keeps_its_actuals(self, workspace) -> None:
        """§32: actual arrives first.  The programme must adopt the section, not overwrite it."""
        from drilling_intelligence.core.enums import RecordState

        ingest(workspace)
        with workspace.database.session() as session:
            well = session.get(Well, well_id_for(workspace, "A-3"))
            section = WellRepository(session).get_or_create_section(
                well, SECTION_NAME, hole_size_in=HOLE_SIZE_IN
            )
            WellRepository(session).update_section(
                section, {"bottom_depth": (10390.0, "ft")}, state=RecordState.ACTUAL
            )
            session.commit()
            existing_id = section.id

        promote(workspace)

        section = _section(workspace)
        assert section.id == existing_id, "the programme adopted the section already on the well"
        assert section.bottom_depth_value == 10390.0, "the drilled depth survived promotion"
        assert section.origin == KnowledgeOrigin.MANUAL.value, (
            "a section that already existed is not re-sourced by the document that mentions it"
        )
        assert _target(workspace).section_id == existing_id


# -- E. ambiguity: what C2 refuses to guess -----------------------------------
class TestAmbiguityStaysUnresolved:
    def test_the_daily_report_states_a_section_only_in_prose(self, promoted) -> None:
        """The evidence behind the refusal, asserted rather than asserted-about.

        The DDR's text says "Section: 12 1/4 in intermediate", but the only structured field the
        extractor produces from it is a hole size whose excerpt is a *bit* size.  There is no section
        field to match on, which is why nothing is attached.
        """
        from drilling_intelligence.database.models import Extraction

        with promoted.database.read_only() as session:
            document = session.scalar(
                select(Document).where(Document.filename == "daily_drilling_report_well-a3.docx")
            )
            artefact = session.scalar(
                select(Extraction).where(
                    Extraction.document_version_id == document.current_version_id
                )
            )
        payload = artefact.document_json or {}
        assert DDR_SECTION_TEXT in (payload.get("text") or ""), "the prose does name a section"
        names = {field["name"] for field in payload.get("extracted_fields", [])}
        assert "section" not in names and "section_name" not in names, (
            "there is no structured section field to match on"
        )
        hole = next(
            field for field in payload["extracted_fields"] if field["name"] == "hole_size_in"
        )
        assert hole["provenance"]["excerpt"] == "12 1/4 in bit", (
            "the one section-shaped number is a bit size, not a section declaration"
        )

    def test_the_two_spellings_are_not_silently_merged(self, promoted) -> None:
        """ "12 1/4 in" and "12 1/4 in intermediate" are left as two names, not one guess."""
        assert _section(promoted, SECTION_NAME) is not None
        assert _section(promoted, DDR_SECTION_TEXT) is None, (
            "C2 does not invent a name grammar; the DDR spelling creates nothing"
        )

    def test_records_that_state_no_section_are_left_unattached(self, promoted) -> None:
        """I21 and §26: a matching hole size is not evidence that a record happened in a section."""
        with promoted.database.read_only() as session:
            for model in (NptRecord, WellOperation, WellEvent, ProblemOccurrence):
                rows = list(session.scalars(select(model)))
                assert rows, f"{model.__tablename__} should have promoted rows to check"
                assert {row.section_id for row in rows} == {None}, model.__tablename__

    def test_no_knowledge_or_lesson_is_attached_speculatively(self, promoted) -> None:
        from drilling_intelligence.database.models import LessonLearned

        with promoted.database.read_only() as session:
            assert [
                row.id for row in session.scalars(select(KnowledgeItem)) if row.section_id
            ] == []
            assert [
                row.id for row in session.scalars(select(LessonLearned)) if row.section_id
            ] == []

    def test_a_programme_with_no_well_creates_no_section(self, workspace) -> None:
        ingest(workspace)
        with workspace.database.session() as session:
            document = session.scalar(select(Document).where(Document.filename == PROGRAM_FILE))
            document_id = str(document.id)
            document.well_id = None
            session.commit()
        OperationalService.for_workspace(workspace).promote(document_id=document_id)
        with workspace.database.read_only() as session:
            assert session.scalar(select(func.count()).select_from(WellSection)) == 0


# -- F. identity --------------------------------------------------------------
class TestIdentity:
    def test_another_well_gets_its_own_section(self, promoted) -> None:
        with promoted.database.session() as session:
            other = WellRepository(session).get_or_create_section(
                session.get(Well, well_id_for(promoted, "B-11")),
                SECTION_NAME,
                hole_size_in=HOLE_SIZE_IN,
            )
            session.commit()
            assert other.id != _section(promoted).id

    def test_the_same_hole_size_under_another_name_is_another_section(self, promoted) -> None:
        with promoted.database.session() as session:
            sidetrack = WellRepository(session).get_or_create_section(
                session.get(Well, well_id_for(promoted, "A-3")),
                "12 1/4 in ST1",
                hole_size_in=HOLE_SIZE_IN,
            )
            session.commit()
            assert sidetrack.id != _section(promoted).id

    def test_the_promoted_section_does_not_collide_on_sequence(self, promoted) -> None:
        """Two sections of one well sharing a sequence is an integrity finding; it must not happen."""
        with promoted.database.session() as session:
            WellRepository(session).get_or_create_section(
                session.get(Well, well_id_for(promoted, "A-3")), "8 1/2 in", hole_size_in=8.5
            )
            session.commit()
            assert check_well_hierarchy(session) == []
            sequences = sorted(
                row.sequence
                for row in session.scalars(
                    select(WellSection).where(WellSection.well_id == well_id_for(promoted, "A-3"))
                )
            )
        assert sequences == [1, 2]


# -- G. transaction -----------------------------------------------------------
class TestTransaction:
    def test_a_failure_while_attaching_the_target_leaves_no_section(self, workspace) -> None:
        """§29: the section, the target and the link land together or not at all."""
        from drilling_intelligence.operations import promote as promote_module

        ingest(workspace)
        original = promote_module.EngineeringRepository.add_target

        def boom(*args, **kwargs):
            raise RuntimeError("target refused")

        promote_module.EngineeringRepository.add_target = boom
        try:
            with pytest.raises(RuntimeError, match="target refused"):
                OperationalService.for_workspace(workspace).promote_workspace()
        finally:
            promote_module.EngineeringRepository.add_target = original

        with workspace.database.read_only() as session:
            assert session.scalar(select(func.count()).select_from(WellSection)) == 0
            assert session.scalar(select(func.count()).select_from(DrillingProgram)) == 0


# -- H. plan versus actual ----------------------------------------------------
class TestPlanActualSummary:
    def _rows(self, workspace) -> dict:
        with workspace.database.read_only() as session:
            return {
                row["metric"]: row
                for row in EngineeringRepository(session).plan_actual_summary(
                    well_id=well_id_for(workspace, "A-3")
                )
                if row["section"] == SECTION_NAME
            }

    def test_the_promoted_section_is_comparable_without_any_test_setup(self, promoted) -> None:
        """The point of C2: plan-versus-actual has a section to speak about, straight from ingest."""
        rows = self._rows(promoted)
        assert rows, "the promoted section must appear in the comparison"
        assert rows["depth_md"]["planned"] == PLANNED_DEPTH_FT
        assert rows["depth_md"]["target_id"] == _target(promoted).id
        assert rows["depth_md"]["section_id"] == _section(promoted).id

    def test_planned_only_is_not_on_plan(self, promoted) -> None:
        depth = self._rows(promoted)["depth_md"]
        assert depth["actual"] is None and depth["status"] == "NO_ACTUAL"

    @pytest.mark.parametrize(
        ("actual", "status"),
        [(PLANNED_DEPTH_FT, "ON_PLAN"), (10390.0, "VARIANCE"), (10500.0, "VARIANCE")],
    )
    def test_an_actual_depth_is_compared_against_the_plan(
        self, promoted, actual: float, status: str
    ) -> None:
        from drilling_intelligence.core.enums import RecordState

        with promoted.database.session() as session:
            section = session.get(WellSection, _section(promoted).id)
            WellRepository(session).update_section(
                section, {"bottom_depth": (actual, "ft")}, state=RecordState.ACTUAL
            )
            session.commit()
        depth = self._rows(promoted)["depth_md"]
        assert depth["planned"] == PLANNED_DEPTH_FT
        assert depth["actual"] == actual
        assert depth["status"] == status

    def test_the_planned_depth_never_comes_from_the_section(self, promoted) -> None:
        """Even with an as-drilled depth stored, the plan is read from the target."""
        from drilling_intelligence.core.enums import RecordState

        with promoted.database.session() as session:
            section = session.get(WellSection, _section(promoted).id)
            WellRepository(session).update_section(
                section, {"bottom_depth": (9999.0, "ft")}, state=RecordState.ACTUAL
            )
            target = session.get(ProgramTarget, _target(promoted).id)
            target.planned_depth_md_value = None
            session.commit()
        depth = self._rows(promoted)["depth_md"]
        assert depth["planned"] is None, "no plan means no plan, not the section's own depth"
        assert depth["actual"] == 9999.0
        assert depth["status"] == "NO_PLAN"


# -- I. the production path ---------------------------------------------------
class TestProductionPath:
    def test_the_terminal_creates_the_section(self, workspace) -> None:
        """CLI to service to promoter to repository to database, with no test-only setup."""
        import json
        import sys
        from io import StringIO

        from drilling_intelligence.cli.app import main

        ingest(workspace)
        out, err = StringIO(), StringIO()
        saved = sys.stdout, sys.stderr
        sys.stdout, sys.stderr = out, err
        try:
            code = main(["records", "promote", "--workspace", str(workspace.root), "--json"])
        finally:
            sys.stdout, sys.stderr = saved
        assert code == 0, (code, out.getvalue()[:800], err.getvalue()[:800])
        payload = json.loads(out.getvalue())
        assert payload["counts"]["target"]["created"] == 1

        section = _section(workspace)
        assert section is not None and section.origin == KnowledgeOrigin.DERIVED.value
        assert _target(workspace).section_id == section.id
        assert section.bottom_depth_value is None

    def test_the_terminals_own_corpus_does_not_compare_across_wells(self, workspace) -> None:
        """The cross-well leak, reached the way a user reaches it - and refused.

        C2's promoter names a section after its hole (``12 1/4 in``), so two wells promoted in one
        workspace end up with the same section name.  ``plan_actual_summary(program_id=...)`` scoped
        its targets to the programme but its *sections* to the whole workspace, and the name fallback
        in ``_match_target`` then compared B-11's drilled depth against A-3's plan.  Nothing here
        constructs that situation by hand: ingest and the CLI produce it.
        """
        import sys
        from io import StringIO

        from drilling_intelligence.core.enums import RecordState
        from drilling_intelligence.database.models import Well

        ingest(workspace)
        out, err = StringIO(), StringIO()
        saved = sys.stdout, sys.stderr
        sys.stdout, sys.stderr = out, err
        try:
            code = main_entry(["records", "promote", "--workspace", str(workspace.root), "--json"])
        finally:
            sys.stdout, sys.stderr = saved
        assert code == 0, (code, err.getvalue()[:400])

        # The other well reaches the same hole and records what it drilled.
        with workspace.database.session() as session:
            other = WellRepository(session).get_or_create_section(
                session.get(Well, well_id_for(workspace, "B-11")),
                SECTION_NAME,
                hole_size_in=12.25,
            )
            WellRepository(session).update_section(
                other, {"bottom_depth": (7777.0, "ft")}, state=RecordState.ACTUAL
            )
            session.commit()

        with workspace.database.read_only() as session:
            program = session.scalar(select(DrillingProgram))
            rows = EngineeringRepository(session).plan_actual_summary(program_id=program.id)
            assert {row["well_id"] for row in rows} == {well_id_for(workspace, "A-3")}, (
                "B-11's section must not be compared against A-3's programme"
            )
            depth = next(row for row in rows if row["metric"] == "depth_md")
            assert depth["planned"] == PLANNED_DEPTH_FT
            assert depth["actual"] is None and depth["status"] == "NO_ACTUAL", (
                "7777.0 ft was drilled on another well and is not this plan's actual"
            )
