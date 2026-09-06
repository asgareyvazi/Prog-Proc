"""The intelligence layer answers questions about the corpus - and never invents a number to fill a gap.

These tests run against the real generated files, promoted into operations, events, NPT and problems, so
every expectation below is a fact about the corpus rather than a fact about a fixture built to match the
code:

*   a timeline is the union of the dated records, in date order, with the undated ones listed after them
    carrying the wording their source used - a missing date is never turned into "today";
*   the field's numbers (hours, occurrences, affected wells) come back from SQL over the promoted rows,
    and a well with no timed NPT says ``None`` rather than a reassuring ``0.0``;
*   a recurring pattern is a grouping of problem rows that needs more than one well before it is
    interesting, and each candidate carries the query that produced it;
*   a snapshot is the only thing this package writes.  It is written once per grouping, it is not silently
    rewritten when the data moves - a re-run reports the difference - and only a person can confirm it.
"""

from __future__ import annotations

from datetime import date

import pytest
from tests.fixtures.fieldops import (
    TOTAL_NPT_HOURS,
    add_casing_program,
    fetch,
    field_id,
    ingest,
    promote,
    well_id_for,
)

from drilling_intelligence.core.enums import (
    ConfirmationStatus,
    KnowledgeRelationType,
    RecommendationLifecycle,
)
from drilling_intelligence.core.errors import ValidationError
from drilling_intelligence.database.models import (
    FieldPattern,
    KnowledgeRelation,
    Recommendation,
    WellSection,
)
from drilling_intelligence.engineering.repository import EngineeringRepository
from drilling_intelligence.intelligence.field import FieldIntelligence
from drilling_intelligence.intelligence.patterns import (
    find_recurring,
    get_pattern,
    link_rows,
    propose_recommendation,
    set_pattern_status,
    snapshot,
    staleness,
)
from drilling_intelligence.intelligence.service import IntelligenceService
from drilling_intelligence.intelligence.timeline import build_timeline
from drilling_intelligence.lessons.repository import LessonRepository
from drilling_intelligence.operations.repository import OperationsRepository

#: A-3's own history, as the promoted rows give it: five records on 13 June, four on the 14th, the
#: programme written just now, and eleven records the files never dated.
A3_ENTRIES = 21
A3_DATED = 10
#: ...and inside a two-day window, the eleven undated ones are not part of the answer at all.
A3_WINDOW = A3_DATED - 1


@pytest.fixture
def field_workspace(workspace):
    """The corpus, promoted, with one cased section and its programme targets on top."""
    ingest(workspace)
    promote(workspace)
    add_casing_program(workspace)
    return workspace


def describe(entries) -> list[tuple]:
    return [
        (str(entry.at) if entry.at is not None else None, entry.kind, entry.table, entry.title)
        for entry in entries
    ]


# -- the timeline -------------------------------------------------------------
def test_the_timeline_is_every_dated_record_of_the_well_in_order(field_workspace) -> None:
    with field_workspace.database.session() as session:
        well = well_id_for(field_workspace, "A-3")
        entries = build_timeline(session, well_id=well)
    dated = [entry for entry in entries if entry.at is not None]
    assert len(entries) == A3_ENTRIES and len(dated) == A3_DATED, describe(entries)
    stamps = [entry.at for entry in dated]
    assert stamps == sorted(stamps), "the dated part reads oldest first"
    assert entries[: len(dated)] == dated, "the undated records come after, never interleaved"
    # The daily report, the operations the NPT rows began inside, the events, the NPT records, the
    # problems and the programme: six tables of the same well, one list.
    assert {entry.kind for entry in entries} == {
        "well",
        "report",
        "operation",
        "event",
        "npt",
        "problem",
        "program",
    }
    assert all(entry.row_id and entry.well_id for entry in entries), (
        "every entry can be clicked through"
    )
    assert all(entry.title for entry in entries), "nothing arrives as a bare timestamp"
    assert all(entry.table for entry in entries), (
        "and nothing arrives without a table to read it from"
    )
    assert [entry.table for entry in dated[:5]] == [
        "ddr_report",
        "well_operation",
        "well_event",
        "npt_record",
        "problem_occurrence",
    ]


def test_a_record_without_a_date_is_listed_and_says_so(field_workspace) -> None:
    with field_workspace.database.session() as session:
        entries = build_timeline(session, well_id=well_id_for(field_workspace, "A-3"))
    reports = [entry for entry in entries if entry.kind == "report"]
    assert len(reports) == 2, describe(reports)
    dated = [entry for entry in reports if entry.at is not None]
    undated = [entry for entry in reports if entry.at is None]
    assert len(dated) == 1 and len(undated) == 1
    # The CSV states 2025-06-13; the .docx states "14 June 2025" in words and nothing parseable, so its
    # entry carries no timestamp and keeps the wording instead.  The two reports are the same day's facts,
    # which is exactly why neither entry is dated by the other.
    assert dated[0].at.date() == date(2025, 6, 13)
    assert undated[0].at is None
    assert undated[0].text == "14 June 2025"
    assert undated[0].provenance, "an undated entry still says where it came from"
    milestones = [entry for entry in entries if entry.kind == "well"]
    assert [entry.title for entry in milestones] == ["Spud: A-3", "Completion: A-3"]
    assert all(entry.text == "no date recorded" for entry in milestones)


def test_a_window_answers_with_the_records_that_can_be_placed_in_it(field_workspace) -> None:
    with field_workspace.database.session() as session:
        well = well_id_for(field_workspace, "A-3")
        everything = build_timeline(session, well_id=well)
        window = build_timeline(
            session, well_id=well, since=date(2025, 6, 13), until=date(2025, 6, 14)
        )
        forced = build_timeline(
            session,
            well_id=well,
            since=date(2025, 6, 13),
            until=date(2025, 6, 14),
            include_undated=True,
        )
        dated_only = build_timeline(session, well_id=well, include_undated=False)
    assert len(window) == A3_WINDOW, describe(window)
    assert all(entry.at is not None for entry in window)
    assert len(forced) == len(everything) - 1, "the 2026 programme is still outside the window"
    assert sum(1 for entry in forced if entry.at is None) == A3_ENTRIES - A3_DATED
    assert all(entry.at is not None for entry in dated_only)
    assert len(dated_only) == A3_DATED
    only_npt = build_timeline(session, well_id=well, kinds=["npt"])
    assert {entry.kind for entry in only_npt} == {"npt"}
    assert len(only_npt) == 4, describe(only_npt)
    with pytest.raises(ValidationError, match="no timeline kind"):
        build_timeline(session, well_id=well, kinds=["guesswork"])


def test_the_timeline_is_repeatable_and_a_field_scope_leaves_out_per_well_gaps(
    field_workspace,
) -> None:
    with field_workspace.database.session() as session:
        well = well_id_for(field_workspace, "A-3")
        other = well_id_for(field_workspace, "B-11")
        first = describe(build_timeline(session, well_id=well))
        assert first == describe(build_timeline(session, well_id=well)), (
            "no clock, no insertion order"
        )
        field_entries = build_timeline(session, field_id=field_id(field_workspace))
    assert len(field_entries) == 23 and len(build_timeline(session, well_id=other)) == 6
    assert {entry.well_id for entry in field_entries} == {well, other}
    # A field-wide list of every well whose spud date nobody wrote down is noise, so the milestone rows
    # belong to a well scope only - and the field answer is the sum of the wells' dated records.
    assert not any(entry.kind == "well" for entry in field_entries)
    assert len(field_entries) == A3_DATED + 4 + 9, "14 dated, 9 undated"
    with pytest.raises(ValidationError, match="no well"):
        build_timeline(session, well_id="well-does-not-exist")
    # A valid kind with nothing recorded yet is an empty answer, not an error.
    assert build_timeline(session, well_id=well, kinds=["lesson"]) == []


# -- the field's numbers ------------------------------------------------------
def test_the_field_hours_are_the_ones_the_files_state(field_workspace) -> None:
    with field_workspace.database.session() as session:
        field = field_id(field_workspace)
        numbers = FieldIntelligence(session).npt(field_id=field)
        window = FieldIntelligence(session).npt(
            field_id=field, since=date(2025, 6, 13), until=date(2025, 6, 14)
        )
    assert numbers["rows"] == 5 and numbers["undated"] == 2
    assert numbers["total_hours"] == TOTAL_NPT_HOURS, (
        "59.25 is the sum of the records, not of incidents"
    )
    assert numbers["unknown_duration"] == 0
    assert numbers["by_category"] == {
        "stuck_pipe": {
            "records": 2,
            "hours": 28.75,
            "wells": 2,
            "unknown_duration": 0,
            "first_seen_at": "2025-04-02T00:00:00",
            "last_seen_at": "2025-06-13T00:00:00",
        },
        "equipment_failure": {
            "records": 1,
            "hours": 12.0,
            "wells": 1,
            "unknown_duration": 0,
            "first_seen_at": "2025-06-14T00:00:00",
            "last_seen_at": "2025-06-14T00:00:00",
        },
        # The .docx breakdown rows carry the code "NPT", which names no category: they are counted, and
        # their hours are not quietly folded into the two categories the CSV named.
        "other": {
            "records": 2,
            "hours": 18.5,
            "wells": 1,
            "unknown_duration": 0,
            "first_seen_at": None,
            "last_seen_at": None,
        },
    }
    assert window["rows"] == 2 and window["total_hours"] == 18.5
    # The window is answered with the two rows it can hold; `undated` still reports the two the .docx
    # left without a date, because "what the window could not see" is the number a reader needs beside a
    # windowed total, not a contradiction of it.
    assert window["undated"] == 2
    a3_hours = numbers["by_well"][well_id_for(field_workspace, "A-3")]
    assert a3_hours["hours"] == 37.0 and a3_hours["records"] == 4, a3_hours
    # A per-well breakdown has no "how many wells" answer, so it omits the key rather than nulling it.
    assert set(a3_hours) == {
        "hours",
        "records",
        "unknown_duration",
        "first_seen_at",
        "last_seen_at",
    }


def test_problems_are_counted_with_the_hours_behind_them(field_workspace) -> None:
    with field_workspace.database.session() as session:
        field = field_id(field_workspace)
        problems = FieldIntelligence(session).problems(field_id=field)
        events = FieldIntelligence(session).events(field_id=field)
    assert problems["occurrences"] == 3 and problems["wells"] == 2, problems
    stuck = problems["by_type"]["stuck_pipe"]
    assert stuck["occurrences"] == 2 and stuck["wells"] == 2
    assert stuck["first_seen_at"] == "2025-04-02T00:00:00"
    assert stuck["last_seen_at"] == "2025-06-13T00:00:00"
    # The hours a problem cost follow whichever link the data carries - a named NPT row, or the shared
    # event - which is the only reason B-11's 22.25 h is in this number instead of it reading 6.5.
    assert stuck["npt_hours"] == 28.75, stuck
    assert stuck["root_cause_known"] == 0, "the promoter never resolves a root cause"
    assert problems["by_type"]["equipment_failure"]["npt_hours"] == 12.0
    assert events["events"] == 3
    assert events["by_type"] == {"stuck_pipe": 2, "equipment_failure": 1}
    assert events["by_category"]["npt"]["wells"] == 2
    assert len(events["by_category"]["npt"]["well_ids"]) == 2
    assert events["by_category"]["npt"]["types"] == {"stuck_pipe": 2, "equipment_failure": 1}
    # Nobody wrote a severity for these events, so the answer is a stated absence rather than a default.
    assert events["by_severity"] == {"not_stated": 3}


def test_a_well_without_timed_npt_says_none_and_not_zero(workspace) -> None:
    ingest(workspace, wells=("A-3", "B-11", "C-2"))
    promote(workspace)
    with workspace.database.session() as session:
        numbers = FieldIntelligence(session).wells(field_id=field_id(workspace))
    by_name = {row["name"]: row for row in numbers["wells"]}
    assert set(by_name) == {"A-3", "B-11", "C-2"}
    assert by_name["A-3"]["npt_hours"] == 37.0
    assert by_name["B-11"]["npt_hours"] == 22.25
    assert by_name["C-2"]["npt_hours"] is None, (
        "nothing to add up is not the same fact as zero hours"
    )
    assert by_name["C-2"]["problems"] == 0 and by_name["C-2"]["reports"] == 0
    assert by_name["C-2"]["sections"] == 0
    assert numbers["count"] == 3


def test_an_offset_well_is_chosen_on_recorded_attributes(field_workspace) -> None:
    with field_workspace.database.session() as session:
        a3, b11 = (well_id_for(field_workspace, name) for name in ("A-3", "B-11"))
        intelligence = FieldIntelligence(session)
        from_a = intelligence.offset_candidates(a3)
        from_b = intelligence.offset_candidates(b11)
    assert [row["name"] for row in from_a] == ["B-11"], from_a
    assert from_a[0]["shared_problem_types"] == ["stuck_pipe"]
    assert from_a[0]["shared_hole_sizes"] == [], "the corpus states no hole size on these problems"
    assert from_a[0]["problems"] == 1
    assert from_a[0]["npt_hours"] == 22.25, "B-11's hours, reached through the event link"
    assert from_a[0]["first_seen_at"] == "2025-04-02T00:00:00"
    assert [row["name"] for row in from_b] == ["A-3"]
    # A-3 has both problem types; the candidate row reports what the two wells share, not everything
    # either of them has.
    assert from_b[0]["shared_problem_types"] == ["stuck_pipe"]
    assert from_b[0]["problems"] == 2


def test_sections_and_their_plan_are_read_from_the_rows_that_exist(field_workspace) -> None:
    with field_workspace.database.session() as session:
        field = field_id(field_workspace)
        section = fetch(field_workspace, WellSection)[0]
        intelligence = FieldIntelligence(session)
        on_section = intelligence.section_problem_history(str(section.id))
        per_well = intelligence.well_problem_history(well_id_for(field_workspace, "A-3"))
        summary = intelligence.summary(field_id=field)
    # The corpus's problems came from an NPT sheet, which states no section, so the section has no problem
    # history: an honest empty list rather than the whole well's hours borrowed by a loose join.
    assert on_section == []
    assert len(per_well) == 2
    assert {row["problem_type"] for row in per_well} == {"stuck_pipe", "equipment_failure"}
    assert all(row["section_id"] is None for row in per_well)
    assert summary["wells"] == 2 and summary["npt_hours"] == TOTAL_NPT_HOURS
    assert summary["problems"] == 3 and summary["reports"] == 2 and summary["npt_rows"] == 5
    # The summary is not a second, cheaper implementation of the same questions.
    assert summary["npt_by_category"]["stuck_pipe"]["hours"] == 28.75
    assert summary["problem_types"]["stuck_pipe"]["occurrences"] == 2
    assert summary["events_by_category"] == {"npt": 3}
    assert summary["lessons"] == 0


def test_a_programme_target_and_a_drilled_section_are_committed_and_measured(
    field_workspace,
) -> None:
    with field_workspace.database.session() as session:
        rows = EngineeringRepository(session).plan_actual_summary(
            well_id=well_id_for(field_workspace, "A-3")
        )
    assert len(rows) == 4, rows  # one row per metric the comparison knows about
    by_metric = {row["metric"]: row for row in rows}
    assert {row["section"] for row in rows} == {"8 1/2 in"}
    duration = by_metric["duration_days"]
    assert duration["planned"] == 12.0 and duration["actual"] == 14.5 and duration["unit"] == "d"
    assert duration["variance"] == pytest.approx(2.5) and duration["status"] == "VARIANCE"
    depth = by_metric["depth_md"]
    assert depth["planned"] == 9850.0 == depth["actual"] and depth["unit"] == "m"
    assert depth["variance"] == 0.0 and depth["status"] == "ON_PLAN"
    mud = by_metric["mud_weight"]
    assert (
        mud["planned"] == 11.4 and mud["actual"] == 11.9 and mud["variance"] == pytest.approx(0.5)
    )
    # The programme states no NPT budget for this section, and the section has no timed NPT of its own:
    # the honest answer is a row that says so, not a zero that reads as "on plan".
    npt = by_metric["npt_hours"]
    assert npt["planned"] is None and npt["actual"] is None and npt["status"] == "NO_PLAN"
    assert npt["variance"] is None
    assert len({row["program_id"] for row in rows}) == 1


def test_the_service_answers_the_same_questions_as_the_package(field_workspace) -> None:
    service = IntelligenceService.for_workspace(field_workspace)
    field = service.resolve_field("North Cormorant")
    assert field == field_id(field_workspace)
    assert service.resolve_field(field) == field
    with pytest.raises(ValidationError, match="no field"):
        service.resolve_field("South Cormorant")
    well = well_id_for(field_workspace, "A-3")
    assert {row.id for row in service.wells_of(field_id=field)} == {
        well,
        well_id_for(field_workspace, "B-11"),
    }
    timeline = service.timeline(well_id=well)
    with field_workspace.database.session() as session:
        assert describe(timeline) == describe(build_timeline(session, well_id=well))
        assert service.npt(field_id=field)["total_hours"] == TOTAL_NPT_HOURS
        assert service.problems(field_id=field)["occurrences"] == 3
        assert service.events(field_id=field)["events"] == 3
        assert service.lessons(field_id=field, approved_only=False)["count"] == 0
        assert len(
            service.timeline(well_id=well, since=date(2025, 6, 13), until=date(2025, 6, 14))
        ) == (A3_WINDOW)
        with pytest.raises(ValidationError, match="needs field_id or project_id"):
            service.summary()
        with pytest.raises(ValidationError, match="needs field_id or project_id"):
            service.wells()


# -- recurring patterns -------------------------------------------------------
def test_a_recurring_pattern_needs_more_than_one_well_and_carries_its_query(
    field_workspace,
) -> None:
    with field_workspace.database.session() as session:
        field = field_id(field_workspace)
        candidates = find_recurring(session, field_id=field)
        assert len(candidates) == 1, candidates
        (stuck,) = candidates
        assert stuck["problem_type"] == "stuck_pipe"
        assert stuck["hole_size_in"] is None
        assert stuck["occurrence_count"] == 2 and stuck["well_count"] == 2
        assert stuck["event_count"] == 2
        assert stuck["total_npt_hours"] == 28.75
        assert stuck["query"] == {
            "field_id": field,
            "project_id": None,
            "problem_type": "stuck_pipe",
            "hole_size_in": None,
            "since": None,
            "until": None,
        }
        # One well is not a pattern, and neither is one occurrence in two wells.
        assert find_recurring(session, field_id=field, min_wells=3) == []
        assert find_recurring(session, field_id=field, min_occurrences=3) == []
        loose = find_recurring(session, field_id=field, min_occurrences=1, min_wells=1, limit=0)
        assert [row["problem_type"] for row in loose] == ["stuck_pipe", "equipment_failure"], loose
        assert len(find_recurring(session, field_id=field, min_occurrences=1, limit=1)) == 1


def test_a_snapshot_is_taken_once_and_refreshed_rather_than_duplicated(field_workspace) -> None:
    with field_workspace.database.session() as session:
        field = field_id(field_workspace)
        candidate = find_recurring(session, field_id=field)[0]
        row = snapshot(session, candidate, note="from the June review", link_evidence=False)
        session.commit()
        pattern_id = row.id
        assert row.status == ConfirmationStatus.CANDIDATE.value
        assert row.occurrence_count == 2 and row.well_count == 2 and row.event_count == 2
        assert row.total_npt_hours == 28.75
        assert row.problem_type == "stuck_pipe"
        assert row.field_id == field
        assert row.detected_by == "intelligence"
        assert row.note == "from the June review"
        assert row.query == candidate["query"]
        assert sorted(row.well_ids) == sorted(
            [well_id_for(field_workspace, "A-3"), well_id_for(field_workspace, "B-11")]
        )
        assert len(row.evidence) == 2
        assert {entry["problem_type"] for entry in row.evidence} == {"stuck_pipe"}
        assert all(entry["problem_id"] for entry in row.evidence)
        assert row.first_seen_at is not None and row.stale_at is None
        again = snapshot(session, candidate, note="taken twice", link_evidence=False)
        session.commit()
        assert again.id == pattern_id, "the signature is the identity, so a re-run refreshes"
        assert len(fetch(field_workspace, FieldPattern)) == 1
        assert get_pattern(session, pattern_id).id == pattern_id
        with pytest.raises(ValidationError, match="query"):
            snapshot(session, {"problem_type": "stuck_pipe"})


def test_the_snapshot_links_to_the_wells_and_to_the_rows_it_counted(field_workspace) -> None:
    with field_workspace.database.session() as session:
        row = snapshot(session, find_recurring(session, field_id=field_id(field_workspace))[0])
        session.commit()
        counts = link_rows(session, row)
        session.commit()
    assert counts == {"wells": 2, "evidence": 2}, counts
    relations = fetch(field_workspace, KnowledgeRelation, source_type="pattern")
    assert sorted(relation.relation for relation in relations) == [
        KnowledgeRelationType.PATTERN_CITES_EVIDENCE.value,
        KnowledgeRelationType.PATTERN_CITES_EVIDENCE.value,
        KnowledgeRelationType.PATTERN_SEEN_IN_WELL.value,
        KnowledgeRelationType.PATTERN_SEEN_IN_WELL.value,
    ]
    assert {relation.target_type for relation in relations} == {"well", "problem_occurrence"}
    assert {relation.source_type for relation in relations} == {"pattern"}
    # Re-linking is idempotent: the edges are the same edges, not a second copy of the argument.
    with field_workspace.database.session() as session:
        again = link_rows(session, get_pattern(session, row.id))
        session.commit()
    assert again == {"wells": 2, "evidence": 2}, again
    assert len(fetch(field_workspace, KnowledgeRelation, source_type="pattern")) == 4


def test_moving_data_stales_a_snapshot_and_reports_the_difference(field_workspace) -> None:
    with field_workspace.database.session() as session:
        field = field_id(field_workspace)
        row = snapshot(session, find_recurring(session, field_id=field)[0], link_evidence=False)
        session.commit()
        pattern_id = row.id
        before = staleness(session, pattern_id)
        assert before["stale"] is False and before["differences"] == {}
        assert before["found"] is True and before["query"]["problem_type"] == "stuck_pipe"

        OperationsRepository(session).record_problem(
            well_id=well_id_for(field_workspace, "B-11"),
            problem_type="stuck_pipe",
            description="bit left in the hole at 9 100 ft MD",
            occurred_at=date(2025, 7, 1),
        )
        session.commit()
        after = staleness(session, pattern_id)
        assert after["stale"] is True, after
        assert after["differences"]["occurrence_count"] == {"stored": 2, "now": 3}, after
        # Only the numbers that moved are reported: the well count was 2 before and is 2 now, and a
        # difference report full of "no change" would train a reader to ignore it.
        assert "well_count" not in after["differences"]
        assert set(after["differences"]) == {"occurrence_count"}, after["differences"]
        # The snapshot keeps the number it was taken with: a reviewed figure is not edited underneath
        # whoever reviewed it.
        assert get_pattern(session, pattern_id).occurrence_count == 2
        service = IntelligenceService.for_workspace(field_workspace)
        flagged = service.pattern_staleness(pattern_id)
        assert flagged["stale"] is True
        # The service owns its own transaction (it is called from a CLI, where nobody is holding a
        # session), so the caller's read has to expire before it sees the flag the service committed.
        session.expire_all()
        current = get_pattern(session, pattern_id)
        assert current.stale_at is not None
        assert current.stale_snapshot["occurrence_count"]["now"] == 3
        live = next(
            item
            for item in find_recurring(session, field_id=field)
            if item["problem_type"] == "stuck_pipe"
        )
        snapshot(session, live, link_evidence=False)
        session.commit()
        refreshed = get_pattern(session, pattern_id)
        assert refreshed.occurrence_count == 3 and refreshed.stale_at is None
        # The third occurrence was written by hand and names no NPT record, so it adds an occurrence and
        # no hours: the refreshed total is still the 28.75 h the files support.
        assert refreshed.total_npt_hours == 28.75, refreshed.total_npt_hours


def test_only_a_person_can_move_a_pattern_and_the_decision_is_attributed(field_workspace) -> None:
    with field_workspace.database.session() as session:
        pattern_id = snapshot(
            session, find_recurring(session, field_id=field_id(field_workspace))[0]
        ).id
        session.commit()
        with pytest.raises(ValidationError, match="without an author"):
            set_pattern_status(session, pattern_id, ConfirmationStatus.CONFIRMED.value)
        row = set_pattern_status(
            session, pattern_id, ConfirmationStatus.CONFIRMED.value, by="k.adeyemi"
        )
        session.commit()
        assert row.status == ConfirmationStatus.CONFIRMED.value
        entry = row.attributes["status_history"][-1]
        assert entry["by"] == "k.adeyemi" and entry["from"] == "CANDIDATE" and entry["reason"] == ""
        # Confirming twice is not a second decision: an identical status leaves the history alone.
        repeated = set_pattern_status(
            session, pattern_id, ConfirmationStatus.CONFIRMED.value, by="someone.else"
        )
        session.commit()
        assert repeated.status == ConfirmationStatus.CONFIRMED.value
        assert len(repeated.attributes["status_history"]) == 1
        back = set_pattern_status(
            session,
            pattern_id,
            ConfirmationStatus.REJECTED.value,
            by="m.halden",
            reason="both occurrences are the same trip",
        )
        session.commit()
        assert back.status == ConfirmationStatus.REJECTED.value
        assert back.attributes["status_history"][-1]["reason"].startswith("both occurrences")


def test_a_recommendation_is_proposed_once_and_waits_for_a_decision(field_workspace) -> None:
    with field_workspace.database.session() as session:
        field = field_id(field_workspace)
        row = snapshot(session, find_recurring(session, field_id=field)[0])
        session.commit()
        first = propose_recommendation(
            session,
            row.id,
            statement="Ream to the bottom before tripping out on 8 1/2 in sections",
            reason="two stuck-bit events on two wells, 28.75 h lost",
        )
        session.commit()
        assert first.status == RecommendationLifecycle.PROPOSED.value
        assert first.pattern_id == row.id
        assert first.field_id == field
        assert first.generated_by == "intelligence"
        assert first.statement.startswith("Ream to the bottom")
        assert first.query["problem_type"] == "stuck_pipe"
        assert first.evidence, "advice with nothing under it is an opinion"
        assert first.decided_by is None and first.decided_at is None
        again = propose_recommendation(
            session,
            row.id,
            statement="Ream to the bottom before tripping out on 8 1/2 in sections",
            reason="two stuck-bit events on two wells, 28.75 h lost",
        )
        session.commit()
        assert again.id == first.id, "the same advice about the same pattern is one row"
        assert again.statement == first.statement
        # Different words are different advice: a person who declined one phrasing has to be able to be
        # handed another without the first row being rewritten behind them.
        reworded = propose_recommendation(
            session, row.id, statement="Short-trip and ream in stages"
        )
        session.commit()
        assert reworded.id != first.id
        assert len(fetch(field_workspace, Recommendation)) == 2
        repository = LessonRepository(session)
        assert len(repository.list_recommendations(field_id=field)) == 2
        with pytest.raises(ValidationError, match="needs a person"):
            # ``by`` is a required keyword so that no caller can forget to attribute the decision; the
            # repository still refuses an empty name, because an attributed-looking blank is worse.
            repository.decide_recommendation(
                first.id, RecommendationLifecycle.ACCEPTED.value, by="  "
            )
        with pytest.raises(ValidationError, match="needs a reason"):
            repository.decide_recommendation(
                first.id, RecommendationLifecycle.DECLINED.value, by="k"
            )
        service = IntelligenceService.for_workspace(field_workspace)
        payload = service.recommend(
            row.id, statement="Ream to the bottom before tripping out on 8 1/2 in sections"
        )
        assert payload["id"] == first.id, "the service proposes, it does not duplicate"
        decided = repository.decide_recommendation(
            first.id, RecommendationLifecycle.ACCEPTED.value, by="k.adeyemi"
        )
        session.commit()
        assert decided.status == RecommendationLifecycle.ACCEPTED.value
        assert decided.decided_by == "k.adeyemi" and decided.decided_at is not None
        assert decided.decline_reason in (None, "")
        # One decision does not decide the other proposal, and a declined one says why.
        declined = repository.decide_recommendation(
            reworded.id,
            RecommendationLifecycle.DECLINED.value,
            by="m.halden",
            reason="we already ream; the problem is the hole angle",
        )
        session.commit()
        assert declined.status == RecommendationLifecycle.DECLINED.value
        assert declined.decline_reason.startswith("we already ream")
        assert get_pattern(session, row.id).status == ConfirmationStatus.CANDIDATE.value


def test_the_service_snapshots_the_field_and_counts_what_it_touched(field_workspace) -> None:
    service = IntelligenceService.for_workspace(field_workspace)
    field = service.resolve_field("North Cormorant")
    outcome = service.snapshot_patterns(field_id=field)
    assert outcome["candidates"] == 1 and outcome["created"] == 1, outcome
    assert outcome["refreshed"] == 0
    assert outcome["scope"] == {"field_id": field, "project_id": None}
    assert outcome["patterns"][0]["occurrence_count"] == 2
    again = service.snapshot_patterns(field_id=field)
    assert again["created"] == 0 and again["refreshed"] == 1, again
    listed = service.list_patterns(field_id=field)
    assert len(listed) == 1 and listed[0].status == ConfirmationStatus.CANDIDATE.value
    assert service.list_patterns(field_id=field, status=ConfirmationStatus.CONFIRMED.value) == []
    assert service.list_patterns(field_id=field, stale_only=True) == []
    assert service.patterns(field_id=field, min_wells=9) == []
    stale = service.pattern_staleness(listed[0].id)
    assert stale["stale"] is False and stale["found"] is True
    with pytest.raises(ValidationError, match="no pattern"):
        service.pattern_staleness("pat-nope")
    linked = service.relink_patterns(listed[0].id)
    assert linked == {"wells": 2, "evidence": 2}, linked
