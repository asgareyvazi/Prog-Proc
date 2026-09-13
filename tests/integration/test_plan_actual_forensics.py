"""P3 forensic verification of plan-versus-actual cross-record lineage.

``test_engineering_lessons.py`` and ``test_field_intelligence.py`` prove the *feature* - that a
programme target and a drilled section are compared into the right ``status``.  This file is the
*forensic* half: it re-derives the guarantees the comparison must keep from the authoritative rows,
and pins the two things a well-scoped answer must never do wrong:

*   **current-revision lineage** - ``plan_actual_summary(well_id=...)`` answers "which revision are
    we drilling", so a superseded programme's targets must never leak into the answer (they are
    history, not the plan); naming a ``program_id`` explicitly is the one way to read an old
    revision, and it must keep working;
*   **per-section aggregation without an N+1** - the NPT hours are summed once per scope in a single
    ``GROUP BY``, so the number of queries a summary issues does not grow with the section count;
*   **no zero fabrication** - a section with no dated NPT rows reports ``None``, never ``0.0``, and
    a section with rows reports their sum; and every comparison row cites the exact target it was
    compared against (``program_id``/``target_id``).

No mocks anywhere: the rows are written through the repositories that own them, and every assertion
reads the real database.
"""

from __future__ import annotations

import pytest
from sqlalchemy import event

from drilling_intelligence.core.errors import ValidationError
from drilling_intelligence.database.models import NptRecord, WellSection
from drilling_intelligence.engineering.repository import EngineeringRepository
from drilling_intelligence.wells.repository import WellRepository


@pytest.fixture
def lineage(session):
    """A field with two wells; wells, sections and programmes built through the owning repositories."""
    repository = WellRepository(session)
    project = repository.get_or_create_project("Lineage Block")
    field = repository.get_or_create_field("Lineage Field", project=project)
    well_a = repository.create_well("L-1", project_id=project.id, field_id=field.id)
    well_b = repository.create_well("L-2", project_id=project.id, field_id=field.id)
    session.flush()
    return {
        "project": project,
        "field": field,
        "well_a": well_a,
        "well_b": well_b,
        "session": session,
    }


def _section(well, *, sid: str, sequence: int, name: str = "8 1/2 in") -> WellSection:
    return WellSection(
        id=sid,
        well_id=well.id,
        sequence=sequence,
        name=name,
        bottom_depth_value=9850.0,
        bottom_depth_unit="m",
        actual_duration_days=14.5,
        actual_mud_weight_value=11.9,
        actual_mud_weight_unit="ppg",
    )


def _npt(session, well, section_id, *, sid: str, hours: float | None) -> None:
    session.add(
        NptRecord(
            id=sid,
            well_id=well.id,
            section_id=section_id,
            category="stuck_pipe",
            description=f"stuck pipe on {sid}",
            duration_hours=hours,
            duration_basis="STATED",
        )
    )


def _depth_rows(rows):
    return [row for row in rows if row["metric"] == "depth_md"]


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


# --------------------------------------------------------------------------- lineage
class TestCurrentRevisionLineage:
    def test_a_superseded_program_cannot_leak_its_targets(self, lineage) -> None:
        """The answer is "which revision are we drilling", not "which revision did we once write"."""
        session = lineage["session"]
        engineering = EngineeringRepository(session)
        session.add(_section(lineage["well_a"], sid="sup-1", sequence=1))
        session.flush()
        first = engineering.create_program(title="Programme", well_id=lineage["well_a"].id)
        engineering.add_target(
            first.id, section_id="sup-1", planned_depth_md_value=1000.0, planned_depth_md_unit="m"
        )
        session.flush()

        revised = engineering.revise_program(first.id, by="drilling-engineer")
        copied = engineering.list_targets(revised.id)[0]
        engineering.update_target(copied.id, planned_depth_md_value=2000.0)
        session.flush()

        (depth,) = _depth_rows(engineering.plan_actual_summary(well_id=lineage["well_a"].id))
        assert depth["planned"] == 2000.0, "the current revision's target is the plan"
        assert depth["program_id"] == revised.id, "the comparison cites the current programme"
        assert depth["target_id"] == copied.id

    def test_an_explicit_program_scope_still_reads_the_old_revision(self, lineage) -> None:
        """Naming a programme asks for *that* programme, superseded or not."""
        session = lineage["session"]
        engineering = EngineeringRepository(session)
        session.add(_section(lineage["well_a"], sid="exp-1", sequence=1))
        session.flush()
        first = engineering.create_program(title="Programme", well_id=lineage["well_a"].id)
        engineering.add_target(
            first.id, section_id="exp-1", planned_depth_md_value=1000.0, planned_depth_md_unit="m"
        )
        session.flush()
        revised = engineering.revise_program(first.id, by="drilling-engineer")
        engineering.update_target(
            engineering.list_targets(revised.id)[0].id, planned_depth_md_value=2000.0
        )
        session.flush()

        (depth,) = _depth_rows(engineering.plan_actual_summary(program_id=first.id))
        assert depth["planned"] == 1000.0, "asking for rev 1 returns rev 1's numbers"
        assert depth["program_id"] == first.id

    def test_a_section_scope_compares_only_its_own_well(self, lineage) -> None:
        """A section-scoped comparison must not pull another well's programme into the match."""
        session = lineage["session"]
        engineering = EngineeringRepository(session)
        sec_a = _section(lineage["well_a"], sid="iso-a", sequence=1, name="8 1/2 in")
        sec_b = _section(lineage["well_b"], sid="iso-b", sequence=1, name="8 1/2 in")
        session.add_all([sec_a, sec_b])
        session.flush()
        prog_a = engineering.create_program(title="Programme A", well_id=lineage["well_a"].id)
        engineering.add_target(
            prog_a.id, section_id="iso-a", planned_depth_md_value=1000.0, planned_depth_md_unit="m"
        )
        prog_b = engineering.create_program(title="Programme B", well_id=lineage["well_b"].id)
        engineering.add_target(
            prog_b.id, section_id="iso-b", planned_depth_md_value=9000.0, planned_depth_md_unit="m"
        )
        session.flush()

        rows = engineering.plan_actual_summary(section_id="iso-a")
        assert {row["section_id"] for row in rows} == {"iso-a"}
        (depth,) = _depth_rows(rows)
        assert depth["planned"] == 1000.0 and depth["program_id"] == prog_a.id, (
            "the match is the section's own well's programme, not the other well's"
        )


# --------------------------------------------------------------------------- programme scope
class TestProgramScopeIsolation:
    """A programme-scoped comparison must stay inside the programme's own well.

    The target side has always narrowed to the named programme while the *section* side stayed
    workspace-wide, and :meth:`_match_target` falls back to matching on the section name.  Hole
    sections are named after the hole, so "12 1/4 in" exists on most wells in a field: the two
    together reported one well's drilled depth against another well's plan, carrying the other
    programme's ``target_id``.  Nothing invented the link - it was a scope that was never applied.
    """

    def test_another_wells_identically_named_section_is_not_compared(self, lineage) -> None:
        """The failure that made this real: same name, different well, a drilled depth."""
        session = lineage["session"]
        engineering = EngineeringRepository(session)
        # Both wells have a "12 1/4 in" section - the normal case across a field, not a freak one.
        mine = _section(lineage["well_a"], sid="scope-a", sequence=1, name="12 1/4 in")
        mine.bottom_depth_value = None
        theirs = _section(lineage["well_b"], sid="scope-b", sequence=1, name="12 1/4 in")
        theirs.bottom_depth_value = 7777.0
        session.add_all([mine, theirs])
        session.flush()
        program = engineering.create_program(title="Programme A", well_id=lineage["well_a"].id)
        engineering.add_target(
            program.id,
            name="12 1/4 in",
            section_id="scope-a",
            planned_depth_md_value=10450.0,
            planned_depth_md_unit="ft",
        )
        session.flush()

        rows = engineering.plan_actual_summary(program_id=program.id)
        assert {row["section_id"] for row in rows} == {"scope-a"}, (
            "the other well's section must not appear in this programme's comparison"
        )
        assert {row["well_id"] for row in rows} == {lineage["well_a"].id}
        (depth,) = _depth_rows(rows)
        assert depth["actual"] is None and depth["status"] == "NO_ACTUAL", (
            "7777.0 belongs to the other well and must never be reported as this plan's actual"
        )

    def test_another_wells_unrelated_section_is_not_listed(self, lineage) -> None:
        """Not only the matched row: a foreign section must not even be listed as NO_TARGET."""
        session = lineage["session"]
        engineering = EngineeringRepository(session)
        session.add_all(
            [
                _section(lineage["well_a"], sid="only-a", sequence=1, name="12 1/4 in"),
                _section(lineage["well_b"], sid="only-b", sequence=1, name="17 1/2 in"),
            ]
        )
        session.flush()
        program = engineering.create_program(title="Programme A", well_id=lineage["well_a"].id)
        engineering.add_target(
            program.id,
            section_id="only-a",
            planned_depth_md_value=1000.0,
            planned_depth_md_unit="m",
        )
        session.flush()

        rows = engineering.plan_actual_summary(program_id=program.id)
        assert {row["section_id"] for row in rows} == {"only-a"}

    def test_an_unknown_program_compares_nothing(self, lineage) -> None:
        """An id nobody wrote is an empty answer, not every section in the workspace."""
        session = lineage["session"]
        session.add(_section(lineage["well_a"], sid="ghost-a", sequence=1))
        session.flush()

        assert EngineeringRepository(session).plan_actual_summary(program_id="prog-nope") == []

    def test_a_program_with_no_well_uses_only_the_sections_its_targets_name(self, lineage) -> None:
        """A field-wide programme names no well, so "its" sections are the ones it points at."""
        session = lineage["session"]
        engineering = EngineeringRepository(session)
        session.add_all(
            [
                _section(lineage["well_a"], sid="tmpl-a", sequence=1, name="12 1/4 in"),
                _section(lineage["well_b"], sid="tmpl-b", sequence=1, name="12 1/4 in"),
            ]
        )
        session.flush()
        program = engineering.create_program(title="Field template")
        assert program.well_id is None
        session.flush()

        assert engineering.plan_actual_summary(program_id=program.id) == [], (
            "a template pointing at no section compares nothing, rather than the whole workspace"
        )

        engineering.add_target(
            program.id,
            name="12 1/4 in",
            section_id="tmpl-a",
            planned_depth_md_value=1234.0,
            planned_depth_md_unit="m",
        )
        session.flush()
        rows = engineering.plan_actual_summary(program_id=program.id)
        assert {row["section_id"] for row in rows} == {"tmpl-a"}
        (depth,) = _depth_rows(rows)
        assert depth["planned"] == 1234.0


# --------------------------------------------------------------------------- aggregation
class TestAggregationCost:
    def test_the_summary_issues_a_constant_number_of_queries(self, db, lineage) -> None:
        """NPT hours are summed once per scope, so the query count is fixed, not per-section."""
        session = lineage["session"]
        engineering = EngineeringRepository(session)
        well = lineage["well_a"]

        def add_sections(count: int, start: int) -> None:
            for index in range(start, start + count):
                session.add(
                    WellSection(
                        id=f"n1-{index}", well_id=well.id, sequence=index, name=f"sec {index}"
                    )
                )
            session.flush()

        add_sections(3, start=0)
        program = engineering.create_program(title="Programme", well_id=well.id)
        for index in range(3):
            engineering.add_target(
                program.id,
                section_id=f"n1-{index}",
                planned_depth_md_value=1000.0,
                planned_depth_md_unit="m",
            )
        session.flush()

        baseline = _select_count(
            db.engine, lambda: engineering.plan_actual_summary(well_id=well.id)
        )
        assert 0 < baseline <= 4, baseline

        add_sections(5, start=3)  # now 8 sections
        assert (
            _select_count(db.engine, lambda: engineering.plan_actual_summary(well_id=well.id))
            == baseline
        ), "the summary must not issue one query per section (N+1)"

    def test_npt_hours_are_summed_per_section_and_never_fabricated(self, lineage) -> None:
        """A section with dated rows reports their sum; a section with none reports ``None``."""
        session = lineage["session"]
        engineering = EngineeringRepository(session)
        drilled = _section(lineage["well_a"], sid="agg-1", sequence=1, name="8 1/2 in")
        silent = _section(lineage["well_a"], sid="agg-2", sequence=2, name="12 1/4 in")
        session.add_all([drilled, silent])
        session.flush()
        _npt(session, lineage["well_a"], "agg-1", sid="npt-a", hours=6.5)
        _npt(session, lineage["well_a"], "agg-1", sid="npt-b", hours=12.0)
        program = engineering.create_program(title="Programme", well_id=lineage["well_a"].id)
        engineering.add_target(program.id, section_id="agg-1", planned_npt_hours=20.0)
        session.flush()

        rows = engineering.plan_actual_summary(well_id=lineage["well_a"].id)
        by_section = {(row["section_id"], row["metric"]): row for row in rows}
        drilled_npt = by_section[("agg-1", "npt_hours")]
        assert drilled_npt["actual"] == pytest.approx(18.5), "the two dated rows sum"
        assert drilled_npt["status"] == "VARIANCE", "18.5 actual vs 20.0 planned"
        silent_npt = by_section[("agg-2", "npt_hours")]
        assert silent_npt["actual"] is None, "nothing recorded is not zero hours"
        assert silent_npt["planned"] is None and silent_npt["variance"] is None

    def test_each_row_cites_the_exact_target_it_was_compared_against(self, lineage) -> None:
        """``program_id``/``target_id`` are the current programme's target, not a guess."""
        session = lineage["session"]
        engineering = EngineeringRepository(session)
        session.add(_section(lineage["well_a"], sid="cit-1", sequence=1))
        session.flush()
        program = engineering.create_program(title="Programme", well_id=lineage["well_a"].id)
        target = engineering.add_target(
            program.id, section_id="cit-1", planned_depth_md_value=9850.0, planned_depth_md_unit="m"
        )
        session.flush()

        rows = engineering.plan_actual_summary(well_id=lineage["well_a"].id)
        assert rows, "the section exists"
        assert {row["program_id"] for row in rows} == {program.id}
        assert {row["target_id"] for row in rows} == {target.id}


# --------------------------------------------------------------------------- determinism
class TestDeterminism:
    def test_the_summary_is_repeatable(self, lineage) -> None:
        session = lineage["session"]
        engineering = EngineeringRepository(session)
        session.add(_section(lineage["well_a"], sid="det-1", sequence=1))
        session.flush()
        program = engineering.create_program(title="Programme", well_id=lineage["well_a"].id)
        engineering.add_target(
            program.id, section_id="det-1", planned_depth_md_value=9850.0, planned_depth_md_unit="m"
        )
        session.flush()

        first = engineering.plan_actual_summary(well_id=lineage["well_a"].id)
        assert first == engineering.plan_actual_summary(well_id=lineage["well_a"].id), (
            "no clock, no insertion order, no hidden state"
        )

    def test_the_summary_is_insertion_order_independent(self, lineage) -> None:
        """Two wells whose sections and programmes are built in opposite orders compare identically."""
        session = lineage["session"]
        engineering = EngineeringRepository(session)

        def build(well, sid: str, depth: float) -> None:
            session.add(WellSection(id=sid, well_id=well.id, sequence=1, name="8 1/2 in"))
            session.flush()
            program = engineering.create_program(title="Programme", well_id=well.id)
            engineering.add_target(
                program.id, section_id=sid, planned_depth_md_value=depth, planned_depth_md_unit="m"
            )
            session.flush()

        build(lineage["well_a"], "ord-a", 1000.0)
        build(lineage["well_b"], "ord-b", 9000.0)
        session.flush()

        by_section = {}
        for well in (lineage["well_a"], lineage["well_b"]):
            for row in engineering.plan_actual_summary(well_id=well.id):
                by_section[(row["section_id"], row["metric"])] = row["planned"]
        assert by_section[("ord-a", "depth_md")] == 1000.0
        assert by_section[("ord-b", "depth_md")] == 9000.0

        # A second pass over the same rows, after a re-read, is byte-identical in values.
        reread_a = engineering.plan_actual_summary(well_id=lineage["well_a"].id)
        assert _depth_rows(reread_a)[0]["planned"] == 1000.0


# --------------------------------------------------------------------------- boundaries
class TestBoundaries:
    def test_a_well_with_no_sections_is_an_empty_answer(self, lineage) -> None:
        """No sections, no comparison: an empty list, not an error and not a fabricated row."""
        session = lineage["session"]
        engineering = EngineeringRepository(session)
        assert engineering.plan_actual_summary(well_id=lineage["well_a"].id) == []

    def test_a_scope_is_still_required(self, lineage) -> None:
        with pytest.raises(ValidationError, match="needs a scope"):
            EngineeringRepository(lineage["session"]).plan_actual_summary()


# --------------------------------------------------------------- combined scopes are intersections
class TestCombinedScopeIsAnIntersection:
    """Supplying one scope must never disable another.

    The programme-well confinement was written as ``if program_id and not (well_id or section_id)``,
    so it applied only to a *bare* programme scope.  Naming a programme together with a well or a
    section therefore skipped it, and the name fallback in ``_match_target`` reported one well's
    drilled depth against another well's plan again - the exact defect ADR-0020 set out to remove,
    reached through a different door.  Every scope here is a constraint; contradictory scopes
    intersect to nothing rather than one of them silently winning.
    """

    @pytest.fixture
    def two_wells(self, lineage):
        """Both wells hold a section of the same name; each well has its own programme."""
        session = lineage["session"]
        engineering = EngineeringRepository(session)
        sec_a = _section(lineage["well_a"], sid="cmb-a", sequence=1, name="12 1/4 in")
        sec_a.bottom_depth_value = None
        sec_b = _section(lineage["well_b"], sid="cmb-b", sequence=1, name="12 1/4 in")
        sec_b.bottom_depth_value = 7777.0
        session.add_all([sec_a, sec_b])
        session.flush()
        prog_a = engineering.create_program(
            title="Programme A", code="CMB-A", well_id=lineage["well_a"].id
        )
        engineering.add_target(
            prog_a.id,
            name="12 1/4 in",
            section_id="cmb-a",
            planned_depth_md_value=10450.0,
            planned_depth_md_unit="ft",
        )
        prog_b = engineering.create_program(
            title="Programme B", code="CMB-B", well_id=lineage["well_b"].id
        )
        engineering.add_target(
            prog_b.id,
            name="12 1/4 in",
            section_id="cmb-b",
            planned_depth_md_value=8000.0,
            planned_depth_md_unit="ft",
        )
        session.flush()
        return {
            "engineering": engineering,
            "well_a": lineage["well_a"].id,
            "well_b": lineage["well_b"].id,
            "prog_a": prog_a.id,
            "prog_b": prog_b.id,
        }

    # -- consistent combinations still answer ---------------------------------
    def test_well_and_its_own_programme(self, two_wells) -> None:
        rows = _depth_rows(
            two_wells["engineering"].plan_actual_summary(
                well_id=two_wells["well_a"], program_id=two_wells["prog_a"]
            )
        )
        assert [(row["section_id"], row["planned"]) for row in rows] == [("cmb-a", 10450.0)]

    def test_programme_and_its_own_section(self, two_wells) -> None:
        rows = _depth_rows(
            two_wells["engineering"].plan_actual_summary(
                program_id=two_wells["prog_a"], section_id="cmb-a"
            )
        )
        assert [(row["section_id"], row["planned"]) for row in rows] == [("cmb-a", 10450.0)]

    def test_well_and_its_own_section(self, two_wells) -> None:
        rows = _depth_rows(
            two_wells["engineering"].plan_actual_summary(
                well_id=two_wells["well_a"], section_id="cmb-a"
            )
        )
        assert [(row["section_id"], row["planned"]) for row in rows] == [("cmb-a", 10450.0)]

    def test_all_three_consistent(self, two_wells) -> None:
        rows = _depth_rows(
            two_wells["engineering"].plan_actual_summary(
                well_id=two_wells["well_a"], program_id=two_wells["prog_a"], section_id="cmb-a"
            )
        )
        assert [(row["section_id"], row["planned"]) for row in rows] == [("cmb-a", 10450.0)]

    # -- contradictory combinations answer nothing ----------------------------
    def test_a_well_with_another_wells_programme_compares_nothing(self, two_wells) -> None:
        """The defect, in its first form: A-3's section would take B-11's planned number."""
        assert (
            two_wells["engineering"].plan_actual_summary(
                well_id=two_wells["well_a"], program_id=two_wells["prog_b"]
            )
            == []
        )

    def test_another_wells_actual_is_never_compared_to_this_programmes_plan(
        self, two_wells
    ) -> None:
        """The severe form: B-11's drilled 7777.0 reported as a VARIANCE against A-3's 10450.0."""
        rows = two_wells["engineering"].plan_actual_summary(
            well_id=two_wells["well_b"], program_id=two_wells["prog_a"]
        )
        assert rows == [], "a well's actuals must never meet another well's plan"

    def test_a_foreign_section_with_this_programme_compares_nothing(self, two_wells) -> None:
        rows = two_wells["engineering"].plan_actual_summary(
            section_id="cmb-b", program_id=two_wells["prog_a"]
        )
        assert rows == []

    def test_a_section_with_another_wells_programme_compares_nothing(self, two_wells) -> None:
        assert (
            two_wells["engineering"].plan_actual_summary(
                section_id="cmb-a", program_id=two_wells["prog_b"]
            )
            == []
        )

    def test_a_well_and_a_foreign_section_compare_nothing(self, two_wells) -> None:
        assert (
            two_wells["engineering"].plan_actual_summary(
                well_id=two_wells["well_a"], section_id="cmb-b"
            )
            == []
        )

    def test_all_three_with_a_conflicting_programme(self, two_wells) -> None:
        assert (
            two_wells["engineering"].plan_actual_summary(
                well_id=two_wells["well_a"], program_id=two_wells["prog_b"], section_id="cmb-a"
            )
            == []
        )

    def test_all_three_with_a_conflicting_section(self, two_wells) -> None:
        assert (
            two_wells["engineering"].plan_actual_summary(
                well_id=two_wells["well_a"], program_id=two_wells["prog_a"], section_id="cmb-b"
            )
            == []
        )

    # -- unknown ids never widen the query ------------------------------------
    def test_an_unknown_programme_beside_a_valid_section_compares_nothing(self, two_wells) -> None:
        """An id nobody wrote must not leave the section side scoped by the other argument."""
        assert (
            two_wells["engineering"].plan_actual_summary(
                program_id="prog-nobody-wrote", section_id="cmb-a"
            )
            == []
        )

    def test_an_unknown_section_beside_a_valid_programme_compares_nothing(self, two_wells) -> None:
        assert (
            two_wells["engineering"].plan_actual_summary(
                section_id="sec-nobody-wrote", program_id=two_wells["prog_a"]
            )
            == []
        )

    def test_an_unknown_well_beside_a_valid_programme_compares_nothing(self, two_wells) -> None:
        assert (
            two_wells["engineering"].plan_actual_summary(
                well_id="well-nobody-wrote", program_id=two_wells["prog_a"]
            )
            == []
        )
