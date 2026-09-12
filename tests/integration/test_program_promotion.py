"""Promoting a drilling program: the planned half of the engineering domain, from the real corpus.

Until this path existed the platform could ingest a program, classify it, extract it and search it -
and still have nothing to compare a drilled section against, because ``drilling_program`` and
``program_target`` had no writer at all.  Every assertion here is against
``well_a3_program_rev12.pdf`` as the corpus generator actually builds it, promoted through the same
service the terminal calls, because a plan that only a repository-direct test can produce is not a
production path.

The negative half matters as much as the positive one: a program that states two hole sizes, or a
number with no unit, must produce *nothing* and say why.  A wrong planned depth is worse than a
missing one - it becomes a variance somebody schedules work against.
"""

from __future__ import annotations

import sys
from io import StringIO
from typing import Any

import pytest
from sqlalchemy import select
from tests.fixtures.fieldops import field_id, ingest, promote, well_id_for

from drilling_intelligence.cli.app import main
from drilling_intelligence.core.enums import KnowledgeOrigin, ProgramLifecycle
from drilling_intelligence.database.models import (
    Document,
    DrillingProgram,
    NptRecord,
    ProcedureRecord,
    ProgramTarget,
    WellOperation,
    WellSection,
)
from drilling_intelligence.engineering.repository import EngineeringRepository
from drilling_intelligence.operations.program import find_program_plan, section_name_from
from drilling_intelligence.operations.service import OperationalService

PROGRAM_FILE = "well_a3_program_rev12.pdf"
#: What the program's first section states, as the generator writes the page.
PLANNED_DEPTH_FT = 10450.0
PLANNED_MUD_PPG = 10.2
HOLE_SIZE_IN = 12.25
SECTION_NAME = "12 1/4 in"


@pytest.fixture
def promoted(workspace):
    """The real corpus, ingested and promoted through the production service."""
    ingest(workspace)
    promote(workspace)
    return workspace


def _program(workspace) -> DrillingProgram:
    with workspace.database.read_only() as session:
        row = session.scalar(select(DrillingProgram))
        assert row is not None, "the corpus states a drilling program and promotion should write it"
        return row


def _targets(workspace, program_id: str) -> list[ProgramTarget]:
    with workspace.database.read_only() as session:
        return list(
            session.scalars(
                select(ProgramTarget)
                .where(ProgramTarget.program_id == program_id)
                .order_by(ProgramTarget.sequence)
            )
        )


def _revise_program_pdf(workspace, old: str, new: str) -> None:
    """Publish a new revision of the program: the same page, one planned number changed.

    The text is replaced on the page rather than in the file's bytes because a PDF's text lives in a
    compressed stream.  The result is a real text-layer PDF read by the same extractor as the first
    revision, which is what makes the supersession assertion meaningful.
    """
    from tests.fixtures.generate import build_program_pdf

    path = workspace.root / "corpus" / PROGRAM_FILE
    before = path.read_bytes()
    # Rebuilt by the corpus generator with one number changed, rather than edited in place: a PDF's
    # text lives in a compressed stream, and redacting a value out of a justified line leaves the
    # sentence broken across text blocks - the extractor would then be reading a damaged page rather
    # than a new revision, and the test would prove nothing about supersession.
    build_program_pdf(path, section_td=new)
    assert path.read_bytes() != before, f"the revision to {new!r} did not change the file"
    assert old != new


def _reingest(workspace) -> None:
    """Run the real pipeline again over the corpus as it now stands.

    Not :func:`tests.fixtures.fieldops.ingest`: that re-registers the wells (which the unique index
    on ``(project_id, name)`` rightly refuses) and rebuilds the files, undoing the revision.
    """
    from drilling_intelligence.ingestion.pipeline import IngestionPipeline

    result = IngestionPipeline(
        settings=workspace.settings,
        workspace_root=workspace.root,
        database=workspace.database,
    ).run(root=workspace.root / "corpus", well_id=well_id_for(workspace, "A-3"))
    assert result.ok, result.error


def _artefact(workspace, filename: str) -> dict[str, Any]:
    from drilling_intelligence.database.models import Extraction

    with workspace.database.read_only() as session:
        document = session.scalar(select(Document).where(Document.filename == filename))
        assert document is not None, filename
        extraction = session.scalar(
            select(Extraction).where(Extraction.document_version_id == document.current_version_id)
        )
        return dict((extraction.document_json if extraction else None) or {})


# ============================================================== the extraction contract (unit-ish)
class TestExtractionContract:
    def test_the_real_program_states_one_section_and_its_planned_numbers(self, workspace) -> None:
        """The contract reads the page the generator writes - depth, mud weight and hole size."""
        ingest(workspace)
        plan = find_program_plan(_artefact(workspace, PROGRAM_FILE))
        assert len(plan.sections) == 1, plan
        section = plan.sections[0]
        assert section.name == SECTION_NAME
        assert section.hole_size_in == HOLE_SIZE_IN
        # 10,450 ft is where the section is drilled *to*; 8,500 ft is the shoe it starts from, and the
        # deeper of the two is the section's TD.  Reading the shallower one would plan the interval
        # that was already cased.
        assert section.planned_depth_md_value == PLANNED_DEPTH_FT
        assert section.planned_depth_md_unit == "ft"
        assert section.planned_mud_weight_value == PLANNED_MUD_PPG
        assert section.planned_mud_weight_unit == "ppg"

    def test_the_design_mud_weight_is_read_and_the_contingency_limit_is_not(
        self, workspace
    ) -> None:
        """ "Do not exceed 11.4 ppg" is a limit, not the mud the program specifies.

        The distinction is the corpus's own (``NEGATIVE_TRUTH`` in the generator), and it is the
        extractor that keeps it: this test pins that promotion inherits the distinction rather than
        re-deriving it from the prose and getting it wrong.
        """
        ingest(workspace)
        plan = find_program_plan(_artefact(workspace, PROGRAM_FILE))
        assert plan.sections[0].planned_mud_weight_value == PLANNED_MUD_PPG
        assert plan.sections[0].planned_mud_weight_value != 11.4

    def test_the_ecd_target_is_not_filed_as_the_design_mud_weight(self, workspace) -> None:
        """10.6 ppg is an equivalent circulating density, a different quantity from the mud mixed.

        The artefact states both on the same line; only ``mud_weight`` becomes the planned column,
        because ``equivalent_mud_weight`` is a dynamic density and comparing it with a mixed mud
        weight would be comparing two different quantities.
        """
        ingest(workspace)
        payload = _artefact(workspace, PROGRAM_FILE)
        assert any(
            item["name"] == "equivalent_mud_weight" and item["value"] == 10.6
            for item in payload["extracted_fields"]
        ), "the corpus states an ECD target, so this test is checking a real distinction"
        assert find_program_plan(payload).sections[0].planned_mud_weight_value == PLANNED_MUD_PPG

    def test_the_reader_is_deterministic_over_the_same_artefact(self, workspace) -> None:
        ingest(workspace)
        payload = _artefact(workspace, PROGRAM_FILE)
        first, second = find_program_plan(payload), find_program_plan(payload)
        assert first == second

    def test_every_planned_number_carries_the_locator_it_was_read_from(self, workspace) -> None:
        ingest(workspace)
        section = find_program_plan(_artefact(workspace, PROGRAM_FILE)).sections[0]
        assert len(section.provenance) == 3, section.provenance
        for item in section.provenance:
            assert item["document_version_id"]
            assert item["filename"] == PROGRAM_FILE
            assert item["excerpt"]
            locator = item["locator"]
            assert locator["page"] == 1
            assert locator["bbox"], (
                "a planned number without a box cannot be pointed at on the page"
            )

    def test_an_artefact_that_states_no_hole_size_plans_nothing_and_says_so(
        self, workspace
    ) -> None:
        ingest(workspace)
        plan = find_program_plan(_artefact(workspace, "mud_report_well-a3.xlsx"))
        assert not plan.sections
        assert [entry["reason"] for entry in plan.skipped] == ["NO_SECTION_STATED"]

    def test_two_hole_sizes_refuse_rather_than_guess(self) -> None:
        """A program planning two sections needs a layout read, which this reader will not do."""
        payload = {
            "extracted_fields": [
                {
                    "name": "hole_size_in",
                    "value": 12.25,
                    "unit": "in",
                    "quality": "VALID",
                    "provenance": {"excerpt": "12 1/4 in section"},
                },
                {
                    "name": "hole_size_in",
                    "value": 8.5,
                    "unit": "in",
                    "quality": "VALID",
                    "provenance": {"excerpt": "8 1/2 in section"},
                },
                {"name": "depth_md", "value": 9000.0, "unit": "ft", "quality": "VALID"},
            ]
        }
        plan = find_program_plan(payload)
        assert not plan.sections
        assert plan.skipped[0]["reason"] == "AMBIGUOUS_SECTIONS"
        assert "8.5" in plan.skipped[0]["detail"] and "12.25" in plan.skipped[0]["detail"]

    def test_a_field_the_extractor_could_not_verify_is_not_planned(self) -> None:
        payload = {
            "extracted_fields": [
                {
                    "name": "hole_size_in",
                    "value": 12.25,
                    "unit": "in",
                    "quality": "UNVERIFIED",
                    "provenance": {"excerpt": "12 1/4 in"},
                }
            ]
        }
        assert find_program_plan(payload).skipped[0]["reason"] == "NO_SECTION_STATED"

    def test_a_depth_without_a_unit_is_refused_rather_than_defaulted(self) -> None:
        """The column would otherwise take its ``m`` default for a number read off a foot program."""
        payload = {
            "extracted_fields": [
                {
                    "name": "hole_size_in",
                    "value": 12.25,
                    "unit": "in",
                    "quality": "VALID",
                    "provenance": {"excerpt": "12 1/4 in section"},
                },
                {"name": "depth_md", "value": 10450.0, "unit": "", "quality": "VALID"},
            ]
        }
        plan = find_program_plan(payload)
        assert "DEPTH_WITHOUT_UNIT" in {entry["reason"] for entry in plan.skipped}
        # The hole size is still a planned number, so the section survives - but the unitless depth
        # is simply absent rather than silently stored as metres.
        assert plan.sections[0].planned_depth_md_value is None
        assert "planned_depth_md_value" not in plan.sections[0].values()

    def test_a_named_section_with_no_planned_number_is_not_a_target(self) -> None:
        payload = {
            "extracted_fields": [
                {
                    "name": "hole_size_in",
                    "value": 12.25,
                    "unit": "in",
                    "quality": "VALID",
                    "provenance": {"excerpt": "12 1/4 in section"},
                }
            ]
        }
        plan = find_program_plan(payload)
        assert plan.sections, "the hole size itself is a planned number"
        assert plan.sections[0].values() == {"hole_size_in": 12.25}

    @pytest.mark.parametrize(
        ("excerpt", "size", "expected"),
        [
            ("12 1/4 in section", 12.25, "12 1/4 in"),
            ("8 1/2 in hole", 8.5, "8 1/2 in"),
            ("  12 1/4  in   interval ", 12.25, "12 1/4 in"),
            ("", 8.5, "8.5 in"),
            ("", None, ""),
        ],
    )
    def test_the_section_is_named_the_way_the_document_named_it(
        self, excerpt: str, size: float | None, expected: str
    ) -> None:
        assert section_name_from(excerpt, size) == expected


# ================================================================== promotion into the domain
class TestPromotion:
    def test_the_corpus_program_becomes_a_program_row_and_a_target(self, promoted) -> None:
        program = _program(promoted)
        targets = _targets(promoted, program.id)
        assert len(targets) == 1
        target = targets[0]
        assert target.name == SECTION_NAME
        assert target.hole_size_in == HOLE_SIZE_IN
        assert target.planned_depth_md_value == PLANNED_DEPTH_FT
        assert target.planned_depth_md_unit == "ft"
        assert target.planned_mud_weight_value == PLANNED_MUD_PPG
        assert target.planned_mud_weight_unit == "ppg"

    def test_the_program_is_scoped_to_the_well_the_document_belongs_to(self, promoted) -> None:
        program = _program(promoted)
        assert program.well_id == well_id_for(promoted, "A-3")
        assert program.field_id == field_id(promoted)
        assert program.project_id

    def test_a_promoted_plan_arrives_as_a_draft_nobody_has_reviewed(self, promoted) -> None:
        """The page says "Status: APPROVED"; the database does not take the document's word for it.

        Promotion is a machine reading a file, which is the same standing every other derived row
        gets (``CANDIDATE`` there, ``DRAFT`` here): an approval is a person's act, recorded by the
        method that validates the transition.
        """
        program = _program(promoted)
        assert program.status == str(ProgramLifecycle.DRAFT)
        assert program.origin == KnowledgeOrigin.DERIVED.value
        assert program.created_by == "promoter"
        assert program.approver is None and program.approved_at is None

    def test_the_document_revision_is_recorded_verbatim_and_not_renumbered(self, promoted) -> None:
        program = _program(promoted)
        assert program.attributes["document_revision"] == "Rev 12"
        # ``revision`` counts supersessions inside this database and starts at 1; "Rev 12" is what the
        # file calls itself.  Conflating them would claim eleven revisions nobody has.
        assert program.revision == 1
        assert program.is_current is True

    def test_no_target_invents_a_number_the_program_never_stated(self, promoted) -> None:
        target = _targets(promoted, _program(promoted).id)[0]
        assert target.planned_duration_days is None
        assert target.planned_npt_hours is None, (
            "a plan with no NPT allowance is not an allowance of 0"
        )
        assert target.planned_cost_value is None
        assert target.formation_top is None
        assert target.casing_program is None


# ========================================================================= provenance and identity
class TestProvenance:
    def test_the_program_names_the_exact_document_version_it_was_read_from(self, promoted) -> None:
        program = _program(promoted)
        with promoted.database.read_only() as session:
            document = session.scalar(select(Document).where(Document.filename == PROGRAM_FILE))
            assert program.document_id == document.id
            assert program.document_version_id == document.current_version_id

    def test_a_target_can_be_traced_back_to_a_page_and_a_box(self, promoted) -> None:
        """The question a citation audit asks: which source version produced this planned number?"""
        target = _targets(promoted, _program(promoted).id)[0]
        assert target.provenance, (
            "a planned number with no provenance is an assertion nobody can check"
        )
        with promoted.database.read_only() as session:
            document = session.scalar(select(Document).where(Document.filename == PROGRAM_FILE))
        for item in target.provenance:
            assert item["document_id"] == document.id
            assert item["document_version_id"] == document.current_version_id
            assert item["filename"] == PROGRAM_FILE
            assert item["source_sha256"]
            assert item["locator"]["page"] == 1

    def test_the_target_says_which_artefact_field_produced_which_column(self, promoted) -> None:
        sources = _targets(promoted, _program(promoted).id)[0].attributes["field_sources"]
        assert sources == {
            "hole_size_in": "hole_size_in",
            "planned_depth_md_value": "depth_md",
            "planned_mud_weight_value": "mud_weight",
        }

    def test_the_target_carries_a_content_addressed_identity(self, promoted) -> None:
        identity = _targets(promoted, _program(promoted).id)[0].attributes["identity_key"]
        assert identity.startswith("promote:")


# ============================================================================== idempotence
class TestIdempotence:
    def test_promoting_twice_confirms_the_rows_instead_of_duplicating_them(self, promoted) -> None:
        first = _program(promoted).id
        again = promote(promoted)
        assert again["counts"]["program"] == {"created": 0, "unchanged": 1, "conflict": 0}
        assert again["counts"]["target"] == {"created": 0, "unchanged": 1, "conflict": 0}
        with promoted.database.read_only() as session:
            assert len(list(session.scalars(select(DrillingProgram)))) == 1
            assert len(list(session.scalars(select(ProgramTarget)))) == 1
            assert session.scalar(select(DrillingProgram)).id == first, (
                "the same row, not a new one"
            )

    def test_a_third_pass_still_writes_nothing(self, promoted) -> None:
        promote(promoted)
        third = promote(promoted)
        assert third["totals"]["created"] == 0, third["counts"]
        assert third["totals"]["conflict"] == 0, third["counts"]


# ======================================================================== revision / supersession
class TestRevision:
    def test_a_new_version_supersedes_the_old_plan_without_erasing_it(self, promoted) -> None:
        """Revision 13 arrives as a new version of the same file.

        The old program keeps its row, its target and its provenance and stops being current; the new
        one points back at it.  "What did we plan at the time" stays answerable, which is the whole
        reason the plan is versioned rather than updated in place.
        """
        original = _program(promoted)
        original_id, original_version = original.id, original.document_version_id
        # A genuinely different revision of the same file: the planned TD moves deeper.
        _revise_program_pdf(promoted, "10,450", "10,900")
        _reingest(promoted)
        promote(promoted)

        with promoted.database.read_only() as session:
            rows = list(session.scalars(select(DrillingProgram).order_by(DrillingProgram.revision)))
        assert len(rows) == 2, "the revision is a second program, not an edit of the first"
        old, new = rows
        assert old.id == original_id
        assert old.is_current is False
        assert old.status == str(ProgramLifecycle.SUPERSEDED)
        assert new.is_current is True
        assert new.supersedes_id == old.id
        assert new.revision == 2
        assert new.document_version_id != original_version

    def test_the_superseded_plan_keeps_the_number_it_stated(self, promoted) -> None:
        original_target = _targets(promoted, _program(promoted).id)[0]
        assert original_target.planned_depth_md_value == PLANNED_DEPTH_FT
        _revise_program_pdf(promoted, "10,450", "10,900")
        _reingest(promoted)
        promote(promoted)

        with promoted.database.read_only() as session:
            rows = list(session.scalars(select(DrillingProgram).order_by(DrillingProgram.revision)))
            targets = {
                row.id: list(
                    session.scalars(select(ProgramTarget).where(ProgramTarget.program_id == row.id))
                )
                for row in rows
            }
        old, new = rows
        assert targets[old.id][0].planned_depth_md_value == PLANNED_DEPTH_FT, (
            "the plan somebody drilled against must not be rewritten by a later revision"
        )
        assert targets[new.id][0].planned_depth_md_value == 10900.0
        # And the old plan still says which version stated it, so its provenance survives too.
        assert targets[old.id][0].provenance[0]["document_version_id"] == old.document_version_id


# ========================================================================= the production path
class TestProductionPath:
    def test_the_terminal_promotes_a_program_with_no_test_only_setup(self, workspace) -> None:
        """``drillintel ingest`` then ``drillintel records promote`` - the two commands a user runs."""

        def call(*argv: str) -> dict:
            out, err = StringIO(), StringIO()
            saved = sys.stdout, sys.stderr
            sys.stdout, sys.stderr = out, err
            try:
                code = main([*argv, "--workspace", str(workspace.root), "--json"])
            finally:
                sys.stdout, sys.stderr = saved
            assert code == 0, (code, out.getvalue()[:800], err.getvalue()[:800])
            import json

            return json.loads(out.getvalue())

        ingest(workspace)  # the corpus has to exist on disk; the pipeline itself is the real one
        payload = call("records", "promote")
        assert payload["counts"]["program"]["created"] == 1, payload["counts"]
        assert payload["counts"]["target"]["created"] == 1, payload["counts"]
        with workspace.database.read_only() as session:
            assert session.scalar(select(ProgramTarget)).planned_depth_md_value == PLANNED_DEPTH_FT

    def test_plan_versus_actual_is_reachable_once_a_section_exists(self, promoted) -> None:
        """The comparison the planned side existed for.

        The target is matched to the drilled section by *name* - the program was written before the
        hole was drilled, so it carries no ``section_id`` - and that fallback is the existing
        behaviour of :meth:`EngineeringRepository._match_target`, not something added here.
        """
        well = well_id_for(promoted, "A-3")
        with promoted.database.session() as session:
            from drilling_intelligence.database.models import Well
            from drilling_intelligence.wells.repository import WellRepository

            row = session.get(Well, well)
            section = WellRepository(session).get_or_create_section(row, SECTION_NAME, sequence=1)
            session.commit()
            rows = EngineeringRepository(session).plan_actual_summary(well_id=well)
        by_metric = {entry["metric"]: entry for entry in rows}
        depth = by_metric["depth_md"]
        assert depth["planned"] == PLANNED_DEPTH_FT
        assert depth["unit"] == "ft"
        assert depth["target_id"] and depth["program_id"]
        assert depth["status"] in {"NO_ACTUAL", "VARIANCE", "ON_PLAN"}
        assert section.id


# =============================================================================== the boundaries
class TestBoundaries:
    def test_ingestion_alone_writes_no_planned_engineering_row(self, workspace) -> None:
        """Promotion stays an explicit act: ``ingest`` indexes files, it does not assert a plan."""
        ingest(workspace)
        with workspace.database.read_only() as session:
            assert list(session.scalars(select(DrillingProgram))) == []
            assert list(session.scalars(select(ProgramTarget))) == []

    def test_promoting_the_plan_does_not_touch_the_actual_records(self, promoted) -> None:
        """Plan and actual stay separate: the planned depth must not become a drilled one."""
        with promoted.database.read_only() as session:
            npt = [(row.id, row.duration_hours) for row in session.scalars(select(NptRecord))]
            operations = [(row.id, row.label) for row in session.scalars(select(WellOperation))]
        promote(promoted)
        with promoted.database.read_only() as session:
            assert [
                (row.id, row.duration_hours) for row in session.scalars(select(NptRecord))
            ] == npt
            assert [
                (row.id, row.label) for row in session.scalars(select(WellOperation))
            ] == operations

    def test_no_section_row_is_invented_by_promoting_a_plan(self, promoted) -> None:
        """A program is written before the hole exists; it must not create the hole.

        ``section_id`` stays empty and the plan is still real - the model documents exactly this, and
        inventing a section here would be fabricating a drilled interval from an intention.
        """
        with promoted.database.read_only() as session:
            assert list(session.scalars(select(WellSection))) == []
        assert _targets(promoted, _program(promoted).id)[0].section_id is None

    def test_no_procedure_is_written_from_a_program(self, promoted) -> None:
        """The program states a plan; it is not a procedure library, and none is inferred from it."""
        with promoted.database.read_only() as session:
            assert list(session.scalars(select(ProcedureRecord))) == []

    def test_a_program_without_a_well_writes_nothing_and_reports_why(self, workspace) -> None:
        ingest(workspace)
        with workspace.database.session() as session:
            document = session.scalar(select(Document).where(Document.filename == PROGRAM_FILE))
            document_id = str(document.id)
            document.well_id = None
            session.commit()
        outcome = OperationalService.for_workspace(workspace).promote(document_id=document_id)
        assert outcome.error == "NO_WELL"
        assert [entry["reason"] for entry in outcome.skipped] == ["NO_WELL"]
        with workspace.database.read_only() as session:
            assert list(session.scalars(select(DrillingProgram))) == []

    def test_a_failed_target_leaves_no_half_written_program(self, workspace, monkeypatch) -> None:
        """The service owns the transaction, so a target that cannot be written takes the plan with it."""
        ingest(workspace)
        from drilling_intelligence.operations import promote as promote_module

        def boom(*args: Any, **kwargs: Any) -> Any:
            raise RuntimeError("target refused")

        monkeypatch.setattr(promote_module.EngineeringRepository, "add_target", boom, raising=True)
        with pytest.raises(RuntimeError, match="target refused"):
            OperationalService.for_workspace(workspace).promote_workspace()
        with workspace.database.read_only() as session:
            assert list(session.scalars(select(DrillingProgram))) == [], (
                "a program whose target could not be written must not survive the rollback"
            )
            assert list(session.scalars(select(ProgramTarget))) == []
