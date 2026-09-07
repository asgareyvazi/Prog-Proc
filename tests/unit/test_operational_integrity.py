"""The operational integrity checks, each proven by breaking exactly one rule.

These are the invariants the schema cannot state - who contains whom, which revision is current, whether a
link joins two records of the same hole, and whether a row that came out of a document can still show it.
A checker that never fires is worth nothing, so every test here creates the violation itself and asserts the
problem names the row that caused it; and one test asserts the clean case is silent, because a check that
cries on a healthy database trains people to ignore ``doctor``.
"""

from __future__ import annotations

import pytest
from sqlalchemy import select, text

from drilling_intelligence.core.enums import KnowledgeOrigin, RecordState
from drilling_intelligence.core.ids import new_id
from drilling_intelligence.database.integrity import (
    check_cross_well_links,
    check_operational_integrity,
    check_promoted_evidence,
    check_revision_chains,
    check_well_hierarchy,
)
from drilling_intelligence.database.models import (
    DdrReport,
    Document,
    DocumentVersion,
    DrillingProgram,
    NptRecord,
    ProblemOccurrence,
    ProcedureRecord,
    WellEvent,
    WellSection,
)


def _problems(problems) -> list[tuple[str, str]]:
    return sorted((item.table, item.problem) for item in problems)


@pytest.fixture
def field_well(session):
    """A project, a field and two wells, built by hand so the test can corrupt one of them."""
    from drilling_intelligence.wells.repository import WellRepository

    repository = WellRepository(session)
    repository.get_or_create_workspace("integrity-test", name="Integrity")
    project = repository.get_or_create_project("Integrity")
    field = repository.get_or_create_field("Integrity", project=project)
    a3 = repository.create_well("A-3", project_id=project.id, field_id=field.id)
    b11 = repository.create_well("B-11", project_id=project.id, field_id=field.id)
    session.flush()
    return {"project": project, "field": field, "a3": a3, "b11": b11}


def test_a_healthy_workspace_says_nothing(field_well, session) -> None:
    assert check_operational_integrity(session) == [], [
        str(item) for item in check_operational_integrity(session)
    ]


def test_a_well_in_another_projects_field_is_reported(field_well, session) -> None:
    from drilling_intelligence.wells.repository import WellRepository

    other = WellRepository(session).get_or_create_project("Orphan")
    field_well["a3"].project_id = other.id
    session.flush()
    problems = check_well_hierarchy(session)
    assert ("well", "is in a field that belongs to another project") in _problems(problems), (
        problems
    )
    assert problems[0].detail["field_project_id"] == field_well["project"].id


def test_a_section_that_does_not_go_downwards_is_reported(field_well, session) -> None:
    session.add(
        WellSection(
            id=new_id("sec"),
            well_id=field_well["a3"].id,
            sequence=1,
            name="shallow",
            top_depth_value=9000.0,
            top_depth_unit="m",
            bottom_depth_value=3500.0,
            bottom_depth_unit="m",
            planned_duration_days=-1.0,
        )
    )
    session.flush()
    problems = _problems(check_well_hierarchy(session))
    assert ("well_section", "bottom depth is not below its top") in problems
    assert ("well_section", "states a negative duration") in problems


def test_two_sections_cannot_share_an_order(field_well, session) -> None:
    for name in ("upper", "lower"):
        session.add(
            WellSection(id=new_id("sec"), well_id=field_well["a3"].id, sequence=1, name=name)
        )
    session.flush()
    problems = check_well_hierarchy(session)
    assert len([item for item in problems if item.problem.startswith("shares a sequence")]) == 1
    assert problems[0].detail["well_id"] == field_well["a3"].id


def _procedure(
    session, *, code: str, revision: int, current: bool, supersedes: str = ""
) -> ProcedureRecord:
    row = ProcedureRecord(
        id=new_id("proc"),
        code=code,
        title=f"{code} rev {revision}",
        revision=revision,
        is_current=current,
        supersedes_id=supersedes or None,
        origin=KnowledgeOrigin.MANUAL.value,
    )
    session.add(row)
    session.flush()
    return row


def test_a_superseded_revision_still_claiming_to_be_current_is_reported(
    field_well, session
) -> None:
    first = _procedure(session, code="NCF-100", revision=1, current=True)
    _procedure(session, code="NCF-100", revision=2, current=False, supersedes=first.id)
    problems = _problems(check_revision_chains(session))
    assert ("procedure_record", "is superseded and still marked current") in problems


def test_a_dangling_link_needs_a_database_whose_constraints_are_off(tmp_path) -> None:
    """The dangling branches of these checks are for a file, not for a live workspace.

    This project turns SQLite's foreign keys on, so a link to a row that does not exist cannot be written at
    all - a fact worth pinning, because the alternative would be a checker quietly doing the schema's job.
    What the checker *is* for is a restored or hand-edited database file where enforcement was not in force
    when the rows went in, so that is the scenario built here: a plain SQLite database, same schema, no
    pragmas, and a link to nothing.
    """
    from sqlalchemy import create_engine
    from sqlalchemy.exc import IntegrityError
    from sqlalchemy.orm import Session

    from drilling_intelligence.database.models import Base
    from drilling_intelligence.database.session import Database

    with pytest.raises(IntegrityError):
        # The project's own engine factory is what turns foreign keys on, so "the database refuses this"
        # has to be asked of the project's database and not of a bare engine.
        live_database = Database.from_url(f"sqlite:///{tmp_path / 'live.db'}")
        live_database.create_all()
        with live_database.session() as live:
            # The same insert that the checker would catch is refused outright once the pragmas are on;
            # this proves the schema, and not only the checker, is the first line of defence.
            live.add(
                ProcedureRecord(
                    id=new_id("proc"),
                    code="NCF-100",
                    title="orphan",
                    revision=2,
                    is_current=False,
                    supersedes_id="proc-never-written",
                    origin=KnowledgeOrigin.MANUAL.value,
                )
            )
            live.flush()
        live_database.dispose()

    corrupt = create_engine(f"sqlite:///{tmp_path / 'corrupt.db'}")
    Base.metadata.create_all(corrupt)
    with Session(corrupt) as session:
        assert session.execute(text("PRAGMA foreign_keys")).scalar_one() == 0, (
            "no enforcement in this file"
        )
        session.add_all(
            [
                ProcedureRecord(
                    id=new_id("proc"),
                    code="NCF-100",
                    title="orphan",
                    revision=2,
                    is_current=False,
                    supersedes_id="proc-never-written",
                    origin=KnowledgeOrigin.MANUAL.value,
                ),
                ProblemOccurrence(
                    id=new_id("prob"),
                    # A well id this file has never heard of, which is only writable because nothing here
                    # enforces anything: exactly the state a restored database can be in.
                    well_id="well-not-here",
                    problem_type="stuck_pipe",
                    description="the bit was not where we left it",
                    npt_id="npt-not-here",
                    origin=KnowledgeOrigin.MANUAL.value,
                ),
            ]
        )
        session.flush()
        problems = check_revision_chains(session) + check_cross_well_links(session)
        found = sorted(item.problem for item in problems)
        assert found == [
            "cites a revision that does not exist",
            "links a npt_record that does not exist",
        ], [str(item) for item in problems]
        session.rollback()
    corrupt.dispose()


def test_a_revision_cycle_is_reported(field_well, session) -> None:
    one = ProcedureRecord(
        id=new_id("proc"),
        code="NCF-102",
        title="one",
        revision=1,
        is_current=False,
        supersedes_id=None,
        origin="MANUAL",
    )
    two = ProcedureRecord(
        id=new_id("proc"),
        code="NCF-102",
        title="two",
        revision=2,
        is_current=False,
        supersedes_id=None,
        origin="MANUAL",
    )
    session.add_all([one, two])
    session.flush()
    # Written as an update rather than at insertion, because each row's id has to exist first.
    one.supersedes_id = two.id
    two.supersedes_id = one.id
    session.flush()
    problems = check_revision_chains(session)
    cycles = [item for item in problems if item.problem == "revision chain is a cycle"]
    assert len(cycles) >= 1, problems
    assert set(cycles[0].detail["cycle"]) == {one.id, two.id}


def test_a_forked_revision_is_refused_before_the_checker_has_to_notice_it(
    field_well, session
) -> None:
    """Two current revisions of one code is impossible here, and that is the design, not the checker's job.

    A partial unique index (``uq_program_one_current``, ``uq_procedure_one_current``) keeps one current row
    per code in every table with a chain, which is a stronger guarantee than a check an application runs
    afterwards.  The checker still reports it, because a database restored from a copy that lost its
    indexes is a real thing that happens to real workspaces.
    """
    from sqlalchemy.exc import IntegrityError

    with pytest.raises(IntegrityError):
        for revision in (1, 2):
            session.add(
                DrillingProgram(
                    id=new_id("prog"),
                    code="NCF-P",
                    title=f"programme rev {revision}",
                    revision=revision,
                    is_current=True,
                    well_id=field_well["a3"].id,
                    origin=KnowledgeOrigin.MANUAL.value,
                )
            )
        session.flush()
    session.rollback()
    assert not check_revision_chains(session), "a refused write leaves nothing for doctor to find"


def test_a_link_to_another_wells_record_is_reported(field_well, session) -> None:
    event = WellEvent(
        id=new_id("evt"),
        well_id=field_well["b11"].id,
        event_type="stuck_pipe",
        category="npt",
        occurred_at_text="14 June 2025",
        origin=KnowledgeOrigin.MANUAL.value,
    )
    session.add(event)
    session.flush()
    session.add(
        NptRecord(
            id=new_id("npt"),
            well_id=field_well["a3"].id,
            category="stuck_pipe",
            duration_hours=6.5,
            duration_basis="STATED",
            event_id=event.id,
            origin=KnowledgeOrigin.MANUAL.value,
        )
    )
    session.flush()
    problems = check_cross_well_links(session)
    assert len(problems) == 1, [str(item) for item in problems]
    assert problems[0].table == "npt_record"
    assert "another well" in problems[0].problem
    assert problems[0].detail == {
        "event_id": event.id,
        "well_id": field_well["a3"].id,
        "other_well_id": field_well["b11"].id,
    }


def test_a_section_of_another_well_is_reported(field_well, session) -> None:
    section = WellSection(
        id=new_id("sec"), well_id=field_well["b11"].id, sequence=1, name="12 1/4 in"
    )
    session.add(section)
    session.flush()
    session.add(
        NptRecord(
            id=new_id("npt"),
            well_id=field_well["a3"].id,
            category="lost_circulation",
            section_id=section.id,
            origin=KnowledgeOrigin.MANUAL.value,
        )
    )
    session.flush()
    problems = _problems(check_cross_well_links(session))
    assert ("npt_record", "is filed under a section of another well") in problems


def test_a_report_pointing_at_another_documents_version_is_reported(field_well, session) -> None:
    def new_document(name: str) -> tuple[Document, DocumentVersion]:
        document = Document(
            id=new_id("doc"),
            identity_path=name,
            filename=name,
            sha256="0" * 64,
            well_id=field_well["a3"].id,
        )
        version = DocumentVersion(
            id=new_id("dv"),
            document_id=document.id,
            version_number=1,
            source_path=name,
            sha256="0" * 64,
        )
        session.add_all([document, version])
        session.flush()
        document.current_version_id = version.id
        session.flush()
        return document, version

    one, one_version = new_document("a.txt")
    _other, two_version = new_document("b.txt")
    session.add(
        DdrReport(
            id=new_id("rpt"),
            well_id=field_well["a3"].id,
            document_id=one.id,
            document_version_id=two_version.id,
            report_date_text="14 June 2025",
            record_state=RecordState.CURRENT.value,
            origin=KnowledgeOrigin.DERIVED.value,
            provenance=[{"kind": "text", "excerpt": "x", "method": "manual"}],
        )
    )
    session.flush()
    assert one.current_version_id == one_version.id, "the fixture itself is sound"
    problems = _problems(check_cross_well_links(session))
    assert ("ddr_report", "names a document that is not its version's document") in problems


def test_a_derived_row_without_evidence_is_reported_and_a_hand_written_one_is_not(
    field_well, session
) -> None:
    session.add_all(
        [
            NptRecord(
                id=new_id("npt"),
                well_id=field_well["a3"].id,
                category="stuck_pipe",
                duration_hours=1.0,
                duration_basis="STATED",
                origin=KnowledgeOrigin.DERIVED.value,
                provenance=[],
            ),
            NptRecord(
                id=new_id("npt"),
                well_id=field_well["a3"].id,
                category="stuck_pipe",
                duration_hours=1.0,
                duration_basis="STATED",
                origin=KnowledgeOrigin.MANUAL.value,
                provenance=[],
            ),
            NptRecord(
                id=new_id("npt"),
                well_id=field_well["a3"].id,
                category="stuck_pipe",
                duration_hours=1.0,
                duration_basis="STATED",
                origin=KnowledgeOrigin.DERIVED.value,
                provenance=[{"kind": "csv", "excerpt": "6.5", "method": "table"}],
            ),
        ]
    )
    session.flush()
    problems = check_promoted_evidence(session)
    assert len(problems) == 1, [str(item) for item in problems]
    assert problems[0].table == "npt_record"
    assert "cites no evidence" in problems[0].problem
    # The whole point of the check: the count of derived rows is not what matters, the count without a
    # source is, and a healthy workspace has none of them.
    assert len(session.execute(select(NptRecord)).scalars().all()) == 3
