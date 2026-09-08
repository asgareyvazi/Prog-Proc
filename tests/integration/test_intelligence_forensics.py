"""P8 forensic certification of the intelligence layer.

Every test here is adversarial and database-backed: a real SQLite database, real rows, and expected
values computed by hand from the fixture - never derived from the code under test.  The suite locks in
the failure modes a derived layer has historically invented: scope leakage between fields and projects,
a windowed snapshot judged against the whole field, a mistyped date silently becoming "no filter",
NULL collapsed to zero, the same downtime counted twice, a ranking that depends on insertion order,
and a read path that writes.

The canonical world (section 21 of the certification brief):

    Project Alpha
        Field A: A-1, A-2
        Field B: B-1
    Project Beta
        Field C: C-1
"""

from __future__ import annotations

import hashlib
import json
from contextlib import contextmanager
from datetime import UTC, date, datetime, timedelta, timezone
from typing import Any

import pytest
from sqlalchemy import MetaData, Table, event, select
from sqlalchemy import inspect as sa_inspect

from drilling_intelligence.core.errors import ValidationError
from drilling_intelligence.database.models import (
    Field,
    FieldPattern,
    LessonLearned,
    NptRecord,
    ProblemDefinition,
    ProblemOccurrence,
    Project,
    Recommendation,
    Well,
    WellEvent,
    WellOperation,
    WellSection,
)
from drilling_intelligence.intelligence.patterns import (
    evidence_for,
    find_recurring,
    get_pattern,
    propose_recommendation,
    set_pattern_status,
    signature_for,
    snapshot,
    staleness,
)
from drilling_intelligence.intelligence.service import IntelligenceService
from drilling_intelligence.intelligence.timeline import TIMELINE_KINDS

# -- the canonical world ---------------------------------------------------------

#: The NPT rows, by hand: (id, well, category, started, hours, event_id)
NPT_ROWS: tuple[tuple[str, str, str, Any, float | None, str | None], ...] = (
    ("npt-a1a", "well-a1", "stuck_pipe", datetime(2025, 1, 10, 8, 0), 5.0, None),
    ("npt-a1b", "well-a1", "stuck_pipe", datetime(2025, 1, 11, 9, 0), 7.0, "evt-a1"),
    ("npt-a2a", "well-a2", "stuck_pipe", datetime(2025, 1, 12, 10, 0), 4.0, None),
    ("npt-a2b", "well-a2", "lost_circ", datetime(2025, 1, 12, 11, 0), 3.0, "evt-a2"),
    ("npt-a1u", "well-a1", "equipment_failure", None, None, None),  # undated, no duration
    (
        "npt-a1x",
        "well-a1",
        "equipment_failure",
        datetime(2025, 1, 13, 12, 0),
        None,
        None,
    ),  # no duration
    (
        "npt-a2z",
        "well-a2",
        "equipment_failure",
        datetime(2025, 1, 13, 13, 0),
        0.0,
        None,
    ),  # a stated zero
    ("npt-c1a", "well-c1", "stuck_pipe", datetime(2025, 3, 1, 8, 0), 6.0, None),
)
#: Field A: 7 rows, 19.0 h stated, 2 rows without a duration, 1 undated row.
FIELD_A_NPT_ROWS = 7
FIELD_A_NPT_HOURS = 19.0
#: Problem occurrences, by hand: (id, well, type, hole, at, npt_id, event_id)
PROBLEM_ROWS: tuple[tuple[str, str, str, float | None, Any, str | None, str | None], ...] = (
    ("prob-a1a", "well-a1", "stuck_pipe", 8.5, datetime(2025, 1, 10, 8, 0), "npt-a1a", None),
    ("prob-a1b", "well-a1", "stuck_pipe", 8.5, datetime(2025, 1, 11, 9, 0), None, "evt-a1"),
    ("prob-a2a", "well-a2", "stuck_pipe", 8.5, datetime(2025, 1, 12, 10, 0), "npt-a2a", "evt-a2"),
    ("prob-b1a", "well-b1", "stuck_pipe", 12.25, datetime(2025, 2, 1, 8, 0), None, None),
    ("prob-c1a", "well-c1", "stuck_pipe", 8.5, datetime(2025, 3, 1, 8, 0), "npt-c1a", None),
)
#: The stuck-pipe hours behind Field A's occurrences, independently: 5 + 7 + 4.
FIELD_A_STUCK_HOURS = 16.0

PROVENANCE_ENTRY = {"kind": "file", "name": "npt_summary_2025-01.csv", "line": 3}


def _add(session, obj) -> None:
    session.add(obj)
    session.flush()


def build_world(workspace, *, order: str = "normal") -> dict[str, Any]:
    """The canonical world, through the models, with dependency-ordered flushes.

    ``order`` is "normal" or "reversed": the same rows in a different insertion order, for the
    determinism tests.  The rows are identical either way - only the order of the INSERTs changes.
    """
    with workspace.database.unit_of_work() as session:
        _add(session, Project(id="proj-alpha", name="Alpha"))
        _add(session, Project(id="proj-beta", name="Beta"))
        _add(session, Field(id="fld-a", name="Field A", project_id="proj-alpha"))
        _add(session, Field(id="fld-b", name="Field B", project_id="proj-alpha"))
        _add(session, Field(id="fld-c", name="Field C", project_id="proj-beta"))
        wells = [
            Well(id="well-a1", name="A-1", field_id="fld-a", project_id="proj-alpha"),
            Well(
                id="well-a2",
                name="A-2",
                field_id="fld-a",
                project_id="proj-alpha",
                spud_date=datetime(2025, 1, 5, 6, 0),
            ),
            Well(id="well-b1", name="B-1", field_id="fld-b", project_id="proj-alpha"),
            Well(id="well-c1", name="C-1", field_id="fld-c", project_id="proj-beta"),
        ]
        if order == "reversed":
            wells = wells[::-1]
        for well in wells:
            _add(session, well)
        for ptype in ("stuck_pipe", "lost_circ", "equipment_failure"):
            _add(
                session,
                ProblemDefinition(
                    id=f"pdef-{ptype}", canonical_key=ptype, problem_type=ptype, name=ptype
                ),
            )
        events = [
            WellEvent(
                id="evt-a1",
                well_id="well-a1",
                category="operational",
                event_type="stuck_pipe",
                label="bit stuck",
                occurred_at=datetime(2025, 1, 11, 9, 0),
            ),
            WellEvent(
                id="evt-a2",
                well_id="well-a2",
                category="operational",
                event_type="stuck_pipe",
                label="stick while drilling",
                occurred_at=datetime(2025, 1, 12, 10, 0),
            ),
        ]
        if order == "reversed":
            events = events[::-1]
        for row in events:
            _add(session, row)
        npt_rows = [
            NptRecord(
                id=nid,
                well_id=wid,
                category=cat,
                started_at=at,
                duration_hours=hours,
                event_id=eid,
                provenance=[dict(PROVENANCE_ENTRY)] if nid == "npt-a1a" else [],
            )
            for nid, wid, cat, at, hours, eid in NPT_ROWS
        ]
        prob_rows = [
            ProblemOccurrence(
                id=pid,
                well_id=wid,
                problem_definition_id=f"pdef-{ptype}",
                problem_type=ptype,
                hole_size_in=hole,
                occurred_at=at,
                npt_id=nid,
                event_id=eid,
                provenance=[dict(PROVENANCE_ENTRY)] if pid == "prob-a1a" else [],
            )
            for pid, wid, ptype, hole, at, nid, eid in PROBLEM_ROWS
        ]
        if order == "reversed":
            npt_rows = npt_rows[::-1]
            prob_rows = prob_rows[::-1]
        for row in npt_rows:
            _add(session, row)
        for row in prob_rows:
            _add(session, row)
        for lesson in (
            LessonLearned(
                id="les-a1",
                code="LL-2025-001",
                title="Ream before tripping",
                lesson="Ream to the bottom before tripping out on 8 1/2 in sections",
                status="APPROVED",
                well_id="well-a1",
                field_id="fld-a",
                problem_type="stuck_pipe",
                approved_by="k.adeyemi",
                approved_at=datetime(2025, 1, 20, 12, 0),
                created_at=datetime(2025, 1, 15, 12, 0),
                provenance=[dict(PROVENANCE_ENTRY)],
            ),
            LessonLearned(
                id="les-a2",
                title="Watch the pump pressure",
                lesson="Watch the pump pressure after the last ream",
                status="CANDIDATE",
                well_id="well-a2",
                field_id="fld-a",
                created_at=datetime(2025, 1, 16, 12, 0),
            ),
            LessonLearned(
                id="les-fa",
                title="Field-wide: log the ream passes",
                lesson="Log every ream pass with depth and duration",
                status="APPROVED",
                field_id="fld-a",
                approved_by="m.halden",
                approved_at=datetime(2025, 1, 21, 12, 0),
                created_at=datetime(2025, 1, 17, 12, 0),
            ),
            LessonLearned(
                id="les-b1",
                title="Field B: nothing yet",
                lesson="A candidate lesson on B-1",
                status="CANDIDATE",
                well_id="well-b1",
                field_id="fld-b",
                created_at=datetime(2025, 2, 2, 12, 0),
            ),
        ):
            _add(session, lesson)
        return {}


@pytest.fixture
def world(workspace) -> dict[str, Any]:
    return build_world(workspace)


@pytest.fixture
def service(workspace) -> IntelligenceService:
    return IntelligenceService.for_workspace(workspace)


@contextmanager
def workspace_session(service: Any):
    """A context manager over the service's database: the shape every write test needs."""
    with service.database.session() as session:
        yield session


def db_fingerprint(database) -> str:
    """The row content of every table, hashed: the strictest 'nothing changed' statement available."""
    digest = hashlib.sha256()
    metadata = MetaData()
    with database.engine.connect() as connection:
        for name in sorted(sa_inspect(database.engine).get_table_names()):
            table = Table(name, metadata, autoload_with=database.engine)
            rows = [tuple(row) for row in connection.execute(select(table))]
            digest.update(name.encode())
            digest.update(json.dumps(rows, sort_keys=True, default=str).encode())
    return digest.hexdigest()


# -- scope isolation --------------------------------------------------------------


def test_a_field_query_never_sees_another_field(world, service) -> None:
    out = service.npt(field_id="fld-a")
    assert out["rows"] == FIELD_A_NPT_ROWS
    assert out["total_hours"] == FIELD_A_NPT_HOURS
    assert set(out["by_well"]) == {"well-a1", "well-a2"}
    problems = service.problems(field_id="fld-a")
    assert problems["occurrences"] == 3, "Field A has three stuck-pipe occurrences and nothing else"
    assert set(problems["by_type"]["stuck_pipe"]["well_ids"]) == {"well-a1", "well-a2"}
    events = service.events(field_id="fld-a")
    assert events["events"] == 2
    lessons = service.lessons(field_id="fld-a", approved_only=False)
    assert {row["id"] for row in lessons["lessons"]} == {"les-a1", "les-a2", "les-fa"}
    summary = service.summary(field_id="fld-a")
    assert summary["wells"] == 2 and summary["npt_rows"] == FIELD_A_NPT_ROWS
    assert summary["npt_hours"] == FIELD_A_NPT_HOURS and summary["problems"] == 3


def test_a_project_query_sees_its_fields_and_only_them(world, service) -> None:
    out = service.npt(project_id="proj-alpha")
    # Field A's seven rows; B-1 has no NPT row at all.
    assert out["rows"] == FIELD_A_NPT_ROWS
    assert set(out["by_well"]) == {"well-a1", "well-a2"}
    c_only = service.npt(project_id="proj-beta")
    assert c_only["rows"] == 1 and c_only["total_hours"] == 6.0
    assert set(c_only["by_well"]) == {"well-c1"}


def test_a_pattern_never_groups_across_fields(world, service) -> None:
    patterns = service.patterns(field_id="fld-a")
    assert len(patterns) == 1
    (stuck,) = patterns
    assert stuck["problem_type"] == "stuck_pipe" and stuck["hole_size_in"] == 8.5
    assert stuck["occurrence_count"] == 3 and stuck["well_count"] == 2
    assert stuck["total_npt_hours"] == FIELD_A_STUCK_HOURS
    # B-1's stuck pipe is a 12 1/2 in problem on one well: under the default thresholds it is an
    # anecdote, and even below them it must never join Field A's grouping.
    assert service.patterns(field_id="fld-b") == []
    loose = service.patterns(field_id="fld-b", min_occurrences=1, min_wells=1)
    assert loose[0]["hole_size_in"] == 12.25 and loose[0]["occurrence_count"] == 1
    assert service.patterns(field_id="fld-c", min_occurrences=1, min_wells=1)[0]["well_count"] == 1


def test_the_timeline_is_scoped_to_the_wells_that_belong(world, service) -> None:
    field_entries = service.timeline(field_id="fld-a")
    # A lesson written against the field names no well; everything else is on one of the two wells.
    assert {entry.well_id for entry in field_entries if entry.well_id} == {"well-a1", "well-a2"}
    project_entries = service.timeline(project_id="proj-beta")
    assert {entry.well_id for entry in project_entries if entry.well_id} == {"well-c1"}
    well_entries = service.timeline(well_id="well-b1")
    assert {entry.well_id for entry in well_entries if entry.well_id} == {"well-b1"}


def test_a_named_well_is_the_whole_scope_not_a_union(world, service) -> None:
    # The caller asked for one well: the field must not ride along and add its own rows.
    out = service.npt(field_id="fld-a", well_id="well-a1")
    assert out["rows"] == 4, out["by_well"]
    assert out["total_hours"] == 12.0
    assert set(out["by_well"]) == {"well-a1"}
    problems = service.problems(field_id="fld-a", well_id="well-a1")
    assert problems["occurrences"] == 2
    events = service.events(field_id="fld-a", well_id="well-a1")
    assert events["events"] == 1
    lessons = service.lessons(field_id="fld-a", well_id="well-a1", approved_only=False)
    assert {row["id"] for row in lessons["lessons"]} == {"les-a1"}


def test_a_well_outside_the_named_field_does_not_drag_the_field_in(world, service) -> None:
    # B-1 is not in Field A.  Asking "Field A, well B-1" must answer about B-1 - which has no NPT -
    # and must not answer with Field A's numbers under a scope label that names a well.
    out = service.npt(field_id="fld-a", well_id="well-b1")
    assert out["rows"] == 0 and out["total_hours"] == 0.0
    assert out["by_well"] == {}
    problems = service.problems(field_id="fld-a", well_id="well-b1")
    assert problems["occurrences"] == 1
    assert problems["by_type"]["stuck_pipe"]["well_ids"] == ["well-b1"]


# -- timeline --------------------------------------------------------------------


def test_timeline_ordering_is_total_under_identical_timestamps(world, service) -> None:
    entries = service.timeline(well_id="well-a1", kinds=("npt", "problem"))
    # 2025-01-10T08:00 holds an NPT and its problem at the same instant: the kind order decides,
    # and the order the service used is the one the comparator publishes.
    assert [(entry.kind, entry.row_id) for entry in entries[:2]] == [
        ("npt", "npt-a1a"),
        ("problem", "prob-a1a"),
    ], entries
    # Undated rows come after every dated one.
    assert [entry.row_id for entry in entries[-1:]] == ["npt-a1u"]
    assert entries[-1].at is None


def test_timeline_is_repeatable_and_independent_of_call_count(world, service) -> None:
    first = [entry.to_dict() for entry in service.timeline(well_id="well-a1")]
    second = [entry.to_dict() for entry in service.timeline(well_id="well-a1")]
    assert first == second


def test_a_windowed_timeline_excludes_undated_and_an_unbounded_one_lists_them(
    world, service
) -> None:
    whole = service.timeline(well_id="well-a1")
    assert any(entry.row_id == "npt-a1u" for entry in whole)
    windowed = service.timeline(well_id="well-a1", since="2025-01-10", until="2025-01-11")
    assert all(entry.at is not None for entry in windowed)
    ids = {entry.row_id for entry in windowed}
    assert {"npt-a1a", "prob-a1a", "npt-a1b", "prob-a1b"} <= ids
    assert "npt-a1x" not in ids, "13 January is outside the window"


def test_undated_records_carry_their_own_wording_not_a_fabricated_date(world, service) -> None:
    entry = next(
        entry for entry in service.timeline(well_id="well-a1") if entry.row_id == "npt-a1u"
    )
    assert entry.at is None
    assert entry.to_dict()["at"] is None, "an undated record must not be given a date"


def test_a_mistyped_date_bound_is_an_error_not_a_silently_dropped_filter(world, service) -> None:
    for call in (
        lambda: service.npt(field_id="fld-a", since="not-a-date"),
        lambda: service.problems(field_id="fld-a", until="June-ish"),
        lambda: service.events(field_id="fld-a", since="2025-13-45"),
        lambda: service.timeline(well_id="well-a1", since="whenever"),
        lambda: service.patterns(field_id="fld-a", since="garbage"),
    ):
        with pytest.raises(ValidationError):
            call()
    # ...and the unfiltered answers are still available to the caller who meant no bound.
    assert service.patterns(field_id="fld-a", until="") == service.patterns(field_id="fld-a")


def test_aware_and_naive_bounds_mean_the_same_instant(world, service) -> None:
    aware = datetime(2025, 6, 1, 3, 0, tzinfo=timezone(timedelta(hours=3)))
    # 2025-06-01T03:00+03:00 is 2025-06-01T00:00Z; nothing in June exists, so every answer is
    # empty either way - the point is that the aware bound neither crashes nor widens the window.
    assert service.timeline(well_id="well-a1", since=aware) == []
    assert service.npt(field_id="fld-a", since=aware)["rows"] == 0
    assert service.patterns(field_id="fld-a", since=aware) == []
    # A bound that actually includes data: the aware form of a day before everything.
    assert service.timeline(well_id="well-a1", since=datetime(2025, 1, 1, tzinfo=UTC))


def test_a_date_without_a_time_covers_the_whole_day_in_every_answer(world, service) -> None:
    # 13 January carries npt-a1x (A-1, 12:00, no duration) and npt-a2z (A-2, 13:00, a stated 0.0 h).
    npt = service.npt(field_id="fld-a", since="2025-01-13", until="2025-01-13")
    assert npt["rows"] == 2
    assert npt["total_hours"] == 0.0 and npt["unknown_duration"] == 1
    timeline = service.timeline(well_id="well-a1", since="2025-01-13", until="2025-01-13")
    assert [entry.row_id for entry in timeline] == ["npt-a1x"]
    patterns = service.patterns(
        field_id="fld-a", since="2025-01-12", until="2025-01-12", min_occurrences=1, min_wells=1
    )
    assert patterns and patterns[0]["occurrence_count"] == 1
    assert patterns[0]["first_seen_at"] == "2025-01-12T10:00:00"
    assert patterns[0]["last_seen_at"] == "2025-01-12T10:00:00"


def test_a_reversed_window_is_empty_not_an_error(world, service) -> None:
    assert service.npt(field_id="fld-a", since="2025-02-01", until="2025-01-01")["rows"] == 0
    assert (
        service.problems(field_id="fld-a", since="2025-02-01", until="2025-01-01")["occurrences"]
        == 0
    )
    assert service.timeline(well_id="well-a1", since="2025-02-01", until="2025-01-01") == []


def test_the_timeline_carries_real_provenance_and_never_fabricates_ids(world, service) -> None:
    entry = next(
        entry for entry in service.timeline(well_id="well-a1") if entry.row_id == "npt-a1a"
    )
    assert entry.provenance == (PROVENANCE_ENTRY,)
    # No document row exists behind any of these records: the entry says "", it does not invent a version.
    assert entry.document_version_id == ""
    # The well milestone on A-2 comes from the well row itself (spud is dated; completion is not, and
    # an unbounded timeline lists that at the end rather than dropping it).
    well_entries = service.timeline(well_id="well-a2", kinds=("well",))
    spud = next(entry for entry in well_entries if entry.at is not None)
    assert spud.row_id == "well-a2" and spud.at == datetime(2025, 1, 5, 6, 0)
    # Field scope has no per-well milestones: a field list of spud gaps is noise, not information.
    assert all(entry.kind != "well" for entry in service.timeline(field_id="fld-a"))


# -- NULL is not zero -------------------------------------------------------------


def test_a_well_without_npt_says_none_and_not_zero(world, service) -> None:
    rows = {row["name"]: row for row in service.wells(field_id="fld-a")["wells"]}
    assert rows["A-1"]["npt_hours"] == 12.0
    assert rows["A-2"]["npt_hours"] == 7.0
    rows_b = {row["name"]: row for row in service.wells(field_id="fld-b")["wells"]}
    assert rows_b["B-1"]["npt_hours"] is None, "no NPT row at all is not 0.0 hours"


def test_no_duration_is_never_a_zero_hour(world, service) -> None:
    out = service.npt(field_id="fld-a")
    assert out["unknown_duration"] == 2
    assert out["undated"] == 1
    a1 = out["by_well"]["well-a1"]
    assert a1["records"] == 4 and a1["hours"] == 12.0 and a1["unknown_duration"] == 2
    # A stated zero is still a number: A-2's equipment_failure row says 0.0 h and it counts.
    a2 = out["by_well"]["well-a2"]
    assert a2["records"] == 3 and a2["hours"] == 7.0 and a2["unknown_duration"] == 0


def test_a_problem_type_with_no_linked_hours_says_none(world, service) -> None:
    out = service.problems(field_id="fld-b")
    assert out["by_type"]["stuck_pipe"]["npt_hours"] is None, (
        "B-1's stuck pipe names no NPT row; 0.0 would claim a measurement that does not exist"
    )
    assert out["by_type"]["stuck_pipe"]["occurrences"] == 1


def test_patterns_and_snapshots_keep_the_same_none_convention(world, service) -> None:
    patterns = service.patterns(field_id="fld-b", min_occurrences=1, min_wells=1)
    assert patterns[0]["total_npt_hours"] is None
    with workspace_session(service) as session:
        row = snapshot(session, patterns[0])
        assert row.total_npt_hours is None


# -- NPT direct / event interaction -----------------------------------------------


def test_a_problem_is_charged_once_per_linked_record_by_hand(world, service) -> None:
    # The five occurrences and their hours, worked out from the fixture:
    #   prob-a1a -> npt-a1a             5.0  (direct)
    #   prob-a1b -> npt-a1b via evt-a1  7.0  (event-derived, no direct link)
    #   prob-a2a -> npt-a2a             4.0  (direct; evt-a2 also carries npt-a2b, 3.0 h of
    #                                          lost_circ - a different downtime, and it stays out)
    #   prob-b1a -> nothing               None
    #   prob-c1a -> npt-c1a             6.0  (direct)
    out = service.problems(field_id="fld-a")
    assert out["by_type"]["stuck_pipe"]["npt_hours"] == FIELD_A_STUCK_HOURS
    history = {row["id"]: row for row in service.well_problem_history("well-a1")}
    assert history["prob-a1a"]["npt_id"] == "npt-a1a"
    assert history["prob-a1b"]["npt_id"] is None and history["prob-a1b"]["event_id"] == "evt-a1"
    assert all(row["problem_type"] == "stuck_pipe" for row in history.values())


def test_the_field_total_counts_each_npt_row_exactly_once(world, service) -> None:
    out = service.npt(field_id="fld-a")
    # Eight rows exist in the whole database; Field A owns seven.  The total is the sum of the rows,
    # not of the problems that cite them - prob-a2a's event also carries npt-a2b, and that row is
    # counted once, here, as lost_circ, not twice through its event.
    assert out["rows"] == FIELD_A_NPT_ROWS
    assert out["total_hours"] == FIELD_A_NPT_HOURS
    assert out["by_category"]["stuck_pipe"]["hours"] == 16.0
    assert out["by_category"]["lost_circ"]["hours"] == 3.0


def test_one_event_with_two_npt_rows_charges_the_problem_only_its_own(world, service) -> None:
    # prob-a2a is linked directly to npt-a2a (4.0 h).  The same event also carries npt-a2b (3.0 h,
    # lost_circ): the direct link wins, so the problem is charged 4.0 h, not 7.0.
    out = service.problems(field_id="fld-a", well_id="well-a2")
    assert out["by_type"]["stuck_pipe"]["npt_hours"] == 4.0


def test_two_problems_citing_one_npt_each_get_the_full_hours(world, service) -> None:
    with workspace_session(service) as session:
        # npt-c1a (6.0 h) is cited by prob-c1a; cite it from a second problem as well.
        session.add(
            ProblemOccurrence(
                id="prob-c1b",
                well_id="well-c1",
                problem_definition_id="pdef-lost_circ",
                problem_type="lost_circ",
                hole_size_in=8.5,
                occurred_at=datetime(2025, 3, 1, 9, 0),
                npt_id="npt-c1a",
            )
        )
        session.commit()
    out = service.problems(field_id="fld-c")
    # Attribution overlap is visible, not hidden: both types see the 6.0 h.
    assert out["by_type"]["stuck_pipe"]["npt_hours"] == 6.0
    assert out["by_type"]["lost_circ"]["npt_hours"] == 6.0
    # ...while the field total still counts the row once.
    assert service.npt(field_id="fld-c")["total_hours"] == 6.0


# -- problem history ---------------------------------------------------------------


def test_well_problem_history_is_the_wells_rows_in_depth_time_order(world, service) -> None:
    history = service.well_problem_history("well-a1")
    assert [row["id"] for row in history] == ["prob-a1a", "prob-a1b"]
    assert all(row["well_id"] == "well-a1" for row in history)
    # Provenance travels with the row.
    assert history[0]["provenance"] == [PROVENANCE_ENTRY]
    assert service.well_problem_history("well-b1")[0]["hole_size_in"] == 12.25
    # A well nobody recorded is an empty history, not an error: the absence is the answer.
    assert service.well_problem_history("well-does-not-exist") == []


def test_section_problem_history_scopes_to_the_section(world, service) -> None:
    with workspace_session(service) as session:
        session.add(WellSection(id="sec-a1-1", well_id="well-a1", sequence=1, name="12 1/4 in"))
        session.add(WellSection(id="sec-b1-1", well_id="well-b1", sequence=1, name="12 1/4 in"))
        session.flush()
        session.execute(
            ProblemOccurrence.__table__.update()
            .where(ProblemOccurrence.id == "prob-a1a")
            .values(section_id="sec-a1-1", depth_from_value=1200.0)
        )
        session.execute(
            ProblemOccurrence.__table__.update()
            .where(ProblemOccurrence.id == "prob-b1a")
            .values(section_id="sec-b1-1", depth_from_value=900.0)
        )
        session.commit()
    assert [row["id"] for row in service.section_problem_history("sec-a1-1")] == ["prob-a1a"]
    assert [row["id"] for row in service.section_problem_history("sec-b1-1")] == ["prob-b1a"]
    assert service.section_problem_history("sec-does-not-exist") == []


def test_operation_events_lists_the_events_the_operation_carries(world, service) -> None:
    with workspace_session(service) as session:
        session.add(
            WellOperation(
                id="op-a1-1",
                well_id="well-a1",
                label="drill 8 1/2",
                operation_type="drilling",
                started_at=datetime(2025, 1, 10, 6, 0),
            )
        )
        session.flush()
        session.execute(
            WellEvent.__table__.update()
            .where(WellEvent.id.in_(["evt-a1", "evt-a2"]))
            .values(operation_id="op-a1-1")
        )
        session.commit()
    rows = service.operation_events("op-a1-1")
    assert [row["id"] for row in rows] == ["evt-a1", "evt-a2"]
    assert {row["well_id"] for row in rows} == {"well-a1", "well-a2"}
    assert service.operation_events("op-does-not-exist") == []


# -- offsets -----------------------------------------------------------------------


def test_offsets_are_scoped_to_the_wells_own_field(world, service) -> None:
    rows = service.offsets("well-a1")
    # Inside Field A the only other well is A-2; B-1 shares the type but lives in another field and
    # must not appear at field level.
    assert [row["well_id"] for row in rows] == ["well-a2"]
    (top,) = rows
    assert top["shared_problem_types"] == ["stuck_pipe"]
    assert top["shared_hole_sizes"] == [8.5]
    assert top["problems"] == 1 and top["npt_hours"] == 4.0


def test_offsets_at_project_level_reach_the_second_field_and_only_it(world, service) -> None:
    rows = service.offsets("well-a1", same_field_only=False)
    # A-2 shares the hole as well as the type, so it ranks first; B-1 shares only the type.
    assert [row["well_id"] for row in rows] == ["well-a2", "well-b1"]
    assert rows[1]["shared_problem_types"] == ["stuck_pipe"] and rows[1]["shared_hole_sizes"] == []
    assert rows[1]["npt_hours"] is None, "B-1 has no timed NPT to compare; that is not 0.0"
    assert service.offsets("well-c1", same_field_only=False) == [], (
        "C-1's project is Beta, which has no other wells"
    )


def test_a_well_without_a_field_has_no_offsets_rather_than_a_fabricated_set(world, service) -> None:
    with workspace_session(service) as session:
        session.add(Well(id="well-orphan", name="Orphan", field_id=None, project_id=None))
        session.flush()
        session.add(
            ProblemOccurrence(
                id="prob-orphan",
                well_id="well-orphan",
                problem_definition_id="pdef-stuck_pipe",
                problem_type="stuck_pipe",
                hole_size_in=8.5,
                occurred_at=datetime(2025, 4, 1, 8, 0),
            )
        )
        session.commit()
    assert service.offsets("well-orphan") == []
    assert service.offsets("well-orphan", same_field_only=False) == []
    # And the orphan never appears in anybody else's comparison set.
    assert all(row["well_id"] != "well-orphan" for row in service.offsets("well-a1"))
    assert all(
        row["well_id"] != "well-orphan" for row in service.offsets("well-a1", same_field_only=False)
    )


def test_offset_ranking_ties_break_on_name_then_id(world, service) -> None:
    with workspace_session(service) as session:
        for name in ("Z-9", "M-5"):
            session.add(
                Well(
                    id=f"well-{name.lower()}", name=name, field_id="fld-a", project_id="proj-alpha"
                )
            )
            session.flush()
            session.add(
                ProblemOccurrence(
                    id=f"prob-{name.lower()}",
                    well_id=f"well-{name.lower()}",
                    problem_definition_id="pdef-stuck_pipe",
                    problem_type="stuck_pipe",
                    hole_size_in=8.5,
                    occurred_at=datetime(2025, 4, 2, 8, 0),
                )
            )
            session.commit()
    rows = service.offsets("well-a1")
    # A-2, M-5 and Z-9 are tied on shared attributes (one type, one hole); the order must be the
    # recorded name, not the order the rows happened to be inserted.
    names = [row["name"] for row in rows]
    assert names == sorted(names), names


def test_offset_limit_zero_means_all_and_negative_does_not_shrink(world, service) -> None:
    all_rows = service.offsets("well-a1", limit=0)
    assert all_rows == service.offsets("well-a1", limit=-5)
    assert service.offsets("well-a1", limit=1) == all_rows[:1]


def test_offsets_never_include_the_well_itself(world, service) -> None:
    assert all(row["well_id"] != "well-a1" for row in service.offsets("well-a1"))


def test_offsets_refuse_an_unknown_well(world, service) -> None:
    with pytest.raises(ValidationError):
        service.offsets("well-does-not-exist")


# -- patterns: identity, grouping, thresholds, windows ------------------------------


def test_pattern_signature_is_stable_and_semantic(world, service) -> None:
    base = {"field_id": "fld-a", "problem_type": "stuck_pipe", "hole_size_in": 8.5}
    assert signature_for(**base) == signature_for(
        hole_size_in=8.5, problem_type="stuck_pipe", field_id="fld-a"
    )
    # Each identity-bearing attribute, mutated one at a time, changes the identity.
    assert signature_for(**base) != signature_for(
        field_id="fld-b", problem_type="stuck_pipe", hole_size_in=8.5
    )
    assert signature_for(**base) != signature_for(
        field_id="fld-a", project_id="proj-beta", problem_type="stuck_pipe", hole_size_in=8.5
    )
    assert signature_for(**base) != signature_for(
        field_id="fld-a", problem_type="lost_circ", hole_size_in=8.5
    )
    assert signature_for(**base) != signature_for(
        field_id="fld-a", problem_type="stuck_pipe", hole_size_in=12.25
    )
    assert signature_for(**base) != signature_for(
        field_id="fld-a", problem_type="stuck_pipe", hole_size_in=8.5, since="2025-01-01"
    )
    assert signature_for(**base) != signature_for(
        field_id="fld-a", problem_type="stuck_pipe", hole_size_in=8.5, until="2025-01-31"
    )
    # ...while parameter order and empty-string noise do not.
    assert signature_for(**base) == signature_for(
        problem_type="stuck_pipe", field_id="fld-a", hole_size_in=8.5, since="", until=""
    )
    # Evidence is measurement, not identity: the signature never sees it.
    assert signature_for(**base, evidence=[{"problem_id": "x"}]) == signature_for(**base)


def test_grouping_never_merges_different_types_or_holes(world, service) -> None:
    patterns = service.patterns(field_id="fld-a", min_occurrences=1, min_wells=1)
    keys = {(row["problem_type"], row["hole_size_in"]) for row in patterns}
    assert keys == {("stuck_pipe", 8.5)}, (
        "one type at one hole size is one grouping; equipment NPT rows are not problems"
    )
    project = service.patterns(project_id="proj-alpha", min_occurrences=1, min_wells=1)
    project_keys = {(row["problem_type"], row["hole_size_in"]) for row in project}
    assert project_keys == {("stuck_pipe", 8.5), ("stuck_pipe", 12.25)}


def test_threshold_boies_are_exact(world, service) -> None:
    # stuck_pipe in Field A: 3 occurrences, 2 wells.
    assert service.patterns(field_id="fld-a", min_occurrences=3, min_wells=2)
    assert not service.patterns(field_id="fld-a", min_occurrences=4, min_wells=2)
    assert service.patterns(field_id="fld-a", min_occurrences=2, min_wells=2)
    assert not service.patterns(field_id="fld-a", min_occurrences=2, min_wells=3)
    # One occurrence, one well: below the defaults, an anecdote.
    assert service.patterns(field_id="fld-b", min_occurrences=1, min_wells=1)
    assert not service.patterns(field_id="fld-b")
    # min 0 behaves as 1, never as "no threshold": a grouping still needs its rows to exist.
    assert service.patterns(field_id="fld-b", min_occurrences=0, min_wells=0)


def test_window_boundaries_are_inclusive(world, service) -> None:
    both = service.patterns(
        field_id="fld-a",
        since="2025-01-10T08:00:00",
        until="2025-01-12T10:00:00",
        min_occurrences=1,
        min_wells=1,
    )
    assert both[0]["occurrence_count"] == 3
    assert both[0]["first_seen_at"] == "2025-01-10T08:00:00"
    assert both[0]["last_seen_at"] == "2025-01-12T10:00:00"
    only_since = service.patterns(
        field_id="fld-a", since="2025-01-11", min_occurrences=1, min_wells=1
    )
    assert only_since[0]["occurrence_count"] == 2
    only_until = service.patterns(
        field_id="fld-a", until="2025-01-11", min_occurrences=1, min_wells=1
    )
    assert only_until[0]["occurrence_count"] == 2
    empty = service.patterns(
        field_id="fld-a", since="2025-05-01", until="2025-05-02", min_occurrences=1, min_wells=1
    )
    assert empty == []


def test_pattern_ranking_is_deterministic_under_a_tie(world, service) -> None:
    with workspace_session(service) as session:
        # Give lost_circ the same shape as stuck_pipe: three occurrences on the same two wells.
        for pid, wid, at in (
            ("prob-a1y", "well-a1", datetime(2025, 1, 10, 8, 30)),
            ("prob-a1z", "well-a1", datetime(2025, 1, 11, 9, 30)),
            ("prob-a2y", "well-a2", datetime(2025, 1, 12, 10, 30)),
        ):
            session.add(
                ProblemOccurrence(
                    id=pid,
                    well_id=wid,
                    problem_definition_id="pdef-lost_circ",
                    problem_type="lost_circ",
                    hole_size_in=8.5,
                    occurred_at=at,
                )
            )
            session.commit()
    rows = service.patterns(field_id="fld-a", min_occurrences=1, min_wells=1)
    counts = [(row["problem_type"], row["occurrence_count"]) for row in rows]
    # Tied on occurrences (3 vs 3): the hours break the tie, so stuck_pipe (16.0 h) leads the
    # grouping that has none.  The same call says the same thing again.
    assert counts == [("stuck_pipe", 3), ("lost_circ", 3)]
    assert rows == service.patterns(field_id="fld-a", min_occurrences=1, min_wells=1)


def test_every_pattern_is_explained_by_rows_that_exist(world, service) -> None:
    with workspace_session(service) as session:
        (stuck,) = find_recurring(session, field_id="fld-a")
        assert stuck["occurrence_count"] == 3
        # The evidence the snapshot will store: every id a real row, inside the scope.
        evidence = evidence_for(
            session, field_id="fld-a", problem_type="stuck_pipe", hole_size_in=8.5
        )
        assert len(evidence) == 3
        assert {entry["well_id"] for entry in evidence} == {"well-a1", "well-a2"}
        for entry in evidence:
            row = session.get(ProblemOccurrence, entry["problem_id"])
            assert row is not None and row.well_id == entry["well_id"]


# -- snapshots ---------------------------------------------------------------------


def test_a_snapshot_is_created_once_and_refreshed_not_duplicated(world, service) -> None:
    with workspace_session(service) as session:
        row = snapshot(session, find_recurring(session, field_id="fld-a")[0])
        session.commit()
        pattern_id = row.id
        assert row.status == "CANDIDATE"
        assert row.occurrence_count == 3 and row.well_count == 2
        assert row.total_npt_hours == FIELD_A_STUCK_HOURS
        assert sorted(row.well_ids) == ["well-a1", "well-a2"]
        again = snapshot(session, find_recurring(session, field_id="fld-a")[0])
        session.commit()
        assert again.id == pattern_id
        assert len(session.execute(select(FieldPattern)).scalars().all()) == 1
        # A refresh keeps the status a person set.
        set_pattern_status(session, pattern_id, "CONFIRMED", by="k.adeyemi")
        session.commit()
        refreshed = snapshot(session, find_recurring(session, field_id="fld-a")[0])
        session.commit()
        assert refreshed.status == "CONFIRMED"


def test_snapshot_numbers_are_frozen_when_the_source_moves(world, service) -> None:
    with workspace_session(service) as session:
        row = snapshot(session, find_recurring(session, field_id="fld-a")[0])
        session.commit()
        pattern_id = row.id
        session.add(
            ProblemOccurrence(
                id="prob-a2b",
                well_id="well-a2",
                problem_definition_id="pdef-stuck_pipe",
                problem_type="stuck_pipe",
                hole_size_in=8.5,
                occurred_at=datetime(2025, 1, 14, 8, 0),
            )
        )
        session.commit()
    with workspace_session(service) as session:
        stored = get_pattern(session, pattern_id)
    assert stored.occurrence_count == 3, "a snapshot does not rewrite itself when the data moves"
    with workspace_session(service) as session:
        report = staleness(session, pattern_id)
    assert report["stale"] is True and report["found"] is True
    assert report["differences"] == {"occurrence_count": {"stored": 3, "now": 4}}


def test_a_windowed_snapshot_is_judged_against_its_window(world, service) -> None:
    with workspace_session(service) as session:
        candidate = find_recurring(
            session,
            field_id="fld-a",
            since=date(2025, 1, 1),
            until=date(2025, 1, 31),
            min_occurrences=1,
            min_wells=1,
        )[0]
        row = snapshot(session, candidate)
        session.commit()
        pattern_id = row.id
        assert row.occurrence_count == 3
        # A new occurrence in March is outside the stored window: the snapshot is still true.
        session.add(
            ProblemOccurrence(
                id="prob-a1c",
                well_id="well-a1",
                problem_definition_id="pdef-stuck_pipe",
                problem_type="stuck_pipe",
                hole_size_in=8.5,
                occurred_at=datetime(2025, 3, 15, 8, 0),
            )
        )
        session.commit()
    with workspace_session(service) as session:
        report = staleness(session, pattern_id)
    assert report["stale"] is False and report["differences"] == {}, (
        "the window did not move; the snapshot is not stale just because the field did"
    )
    # ...and an occurrence inside the window does stale it.
    with workspace_session(service) as session:
        session.add(
            ProblemOccurrence(
                id="prob-a1d",
                well_id="well-a1",
                problem_definition_id="pdef-stuck_pipe",
                problem_type="stuck_pipe",
                hole_size_in=8.5,
                occurred_at=datetime(2025, 1, 20, 8, 0),
            )
        )
        session.commit()
    with workspace_session(service) as session:
        report = staleness(session, pattern_id)
    assert report["stale"] is True
    assert report["differences"]["occurrence_count"] == {"stored": 3, "now": 4}


def test_a_snapshot_of_a_deleted_grouping_reports_not_found_not_zero(world, service) -> None:
    with workspace_session(service) as session:
        row = snapshot(
            session, find_recurring(session, field_id="fld-b", min_occurrences=1, min_wells=1)[0]
        )
        session.commit()
        pattern_id = row.id
        session.delete(session.get(ProblemOccurrence, "prob-b1a"))
        session.commit()
    with workspace_session(service) as session:
        report = staleness(session, pattern_id)
    assert report["found"] is False and report["stale"] is True
    assert report["differences"]["occurrence_count"] == {"stored": 1, "now": None}
    with workspace_session(service) as session:
        stored = get_pattern(session, pattern_id)
    assert stored.occurrence_count == 1, "the stored figure is what was counted, not a re-read"


def test_snapshot_well_ids_cover_the_whole_grouping_not_the_evidence_cap(world, service) -> None:
    with workspace_session(service) as session:
        for index in range(25):
            session.add(
                Well(
                    id=f"well-m{index:02d}",
                    name=f"M-{index:02d}",
                    field_id="fld-a",
                    project_id="proj-alpha",
                )
            )
            session.flush()
            session.add(
                ProblemOccurrence(
                    id=f"prob-m{index:02d}",
                    well_id=f"well-m{index:02d}",
                    problem_definition_id="pdef-stuck_pipe",
                    problem_type="stuck_pipe",
                    hole_size_in=8.5,
                    occurred_at=datetime(2025, 5, 1, 0, index % 60),
                )
            )
            session.commit()
        candidate = next(
            c
            for c in find_recurring(session, field_id="fld-a", min_occurrences=1, min_wells=1)
            if c["problem_type"] == "stuck_pipe"
        )
        assert candidate["well_count"] == 27, "A-1, A-2 and the twenty-five M-wells"
        row = snapshot(session, candidate)
        session.commit()
    assert row.well_count == 27
    assert len(row.well_ids) == 27, "the 'which wells' answer must name every well it counted"
    assert {"well-a1", "well-a2"} <= set(row.well_ids)
    assert len(row.evidence) <= 20, "the evidence column stays capped; well_ids is what is not"


def test_the_stale_flag_is_written_once_and_a_refresh_clears_it(world, service) -> None:
    with workspace_session(service) as session:
        row = snapshot(session, find_recurring(session, field_id="fld-a")[0])
        session.commit()
        pattern_id = row.id
    assert service.pattern_staleness(pattern_id)["stale"] is False
    with workspace_session(service) as session:
        session.add(
            ProblemOccurrence(
                id="prob-a2c",
                well_id="well-a2",
                problem_definition_id="pdef-stuck_pipe",
                problem_type="stuck_pipe",
                hole_size_in=8.5,
                occurred_at=datetime(2025, 1, 15, 8, 0),
            )
        )
        session.commit()
    assert service.pattern_staleness(pattern_id)["stale"] is True
    with workspace_session(service) as session:
        stamp = get_pattern(session, pattern_id).stale_at
    assert stamp is not None
    assert service.pattern_staleness(pattern_id)["stale"] is True
    with workspace_session(service) as session:
        # The traceable date is not rolled forward by every re-check.
        assert get_pattern(session, pattern_id).stale_at == stamp
    # A re-snapshot clears the flag and stores the new numbers.
    outcome = service.snapshot_patterns(field_id="fld-a")
    assert outcome["refreshed"] == 1 and outcome["created"] == 0
    with workspace_session(service) as session:
        row = get_pattern(session, pattern_id)
    assert row.stale_at is None and row.occurrence_count == 4


# -- pattern status / confirmation ---------------------------------------------------


def test_only_a_person_can_confirm_a_pattern_and_transitions_are_validated(world, service) -> None:
    with workspace_session(service) as session:
        row = snapshot(session, find_recurring(session, field_id="fld-a")[0])
        session.commit()
        pattern_id = row.id
        with pytest.raises(ValidationError, match="author"):
            set_pattern_status(session, pattern_id, "CONFIRMED")
        with pytest.raises(ValidationError, match="not a"):
            set_pattern_status(session, pattern_id, "CERTIFIED", by="k")
    service.confirm_pattern(pattern_id, "CONFIRMED", by="k.adeyemi", reason="reviewed the rows")
    with workspace_session(service) as session:
        assert get_pattern(session, pattern_id).status == "CONFIRMED"
    # REJECTED is the other legal move from CONFIRMED...
    service.confirm_pattern(pattern_id, "REJECTED", by="k.adeyemi", reason="better source found")
    with workspace_session(service) as session:
        assert get_pattern(session, pattern_id).status == "REJECTED"
    # ...but REJECTED cannot jump straight to CONFIRMED: it goes back to CANDIDATE first.
    with pytest.raises(ValidationError):
        service.confirm_pattern(pattern_id, "CONFIRMED", by="k.adeyemi")
    service.confirm_pattern(pattern_id, "CANDIDATE", by="k.adeyemi")
    # Re-affirming the same state is a no-op, not an error and not a new decision.
    service.confirm_pattern(pattern_id, "CANDIDATE", by="k.adeyemi")
    with workspace_session(service) as session:
        assert get_pattern(session, pattern_id).status == "CANDIDATE"


# -- recommendations ------------------------------------------------------------------


def test_a_recommendation_requires_a_confirmed_pattern(world, service) -> None:
    with workspace_session(service) as session:
        row = snapshot(session, find_recurring(session, field_id="fld-a")[0])
        session.commit()
        pattern_id = row.id
    with pytest.raises(ValidationError, match="confirmed pattern"):
        service.recommend(pattern_id, statement="ream before tripping")
    service.confirm_pattern(pattern_id, "CONFIRMED", by="k.adeyemi")
    payload = service.recommend(pattern_id, statement="ream before tripping")
    with workspace_session(service) as session:
        row = session.get(Recommendation, payload["id"])
        assert row.status == "PROPOSED"
        assert row.pattern_id == pattern_id and row.field_id == "fld-a"
        assert row.decided_by is None and row.decided_at is None
        assert row.query["problem_type"] == "stuck_pipe"
        # The evidence names rows that exist.
        for entry in row.evidence:
            assert session.get(ProblemOccurrence, entry["problem_id"]) is not None
        # The same advice again is the same row, decision and all.
        again = propose_recommendation(session, pattern_id, statement="ream before tripping")
        session.commit()
        assert again.id == row.id
        assert len(session.execute(select(Recommendation)).scalars().all()) == 1
    # Generated advice stays a proposal: nothing in the platform promotes it.
    assert payload["status"] == "PROPOSED"


# -- determinism -----------------------------------------------------------------------


def test_the_same_data_in_a_different_insertion_order_gives_the_same_answers(workspace) -> None:
    build_world(workspace, order="reversed")
    from drilling_intelligence.wells.workspace import Workspace

    sibling = Workspace.create(
        workspace.root.parent / "workspace-sibling", workspace.settings, name="Sibling"
    )
    build_world(sibling, order="normal")
    first = IntelligenceService.for_workspace(workspace)
    second = IntelligenceService.for_workspace(sibling)
    for method in ("npt", "problems", "events", "summary"):
        assert getattr(first, method)(field_id="fld-a") == getattr(second, method)(field_id="fld-a")
    assert first.lessons(field_id="fld-a", approved_only=False) == second.lessons(
        field_id="fld-a", approved_only=False
    )
    assert first.patterns(field_id="fld-a") == second.patterns(field_id="fld-a")
    assert first.offsets("well-a1") == second.offsets("well-a1")
    assert [e.to_dict() for e in first.timeline(well_id="well-a1")] == [
        e.to_dict() for e in second.timeline(well_id="well-a1")
    ]


# -- read-only / transaction ownership ---------------------------------------------------


def test_every_read_api_leaves_the_database_untouched(world, service) -> None:
    before = db_fingerprint(service.database)
    service.timeline(well_id="well-a1")
    service.timeline(field_id="fld-a")
    service.wells(field_id="fld-a")
    service.npt(field_id="fld-a")
    service.problems(field_id="fld-a")
    service.events(field_id="fld-a")
    service.lessons(field_id="fld-a")
    service.well_problem_history("well-a1")
    service.section_problem_history("sec-does-not-exist")
    service.operation_events("op-does-not-exist")
    service.offsets("well-a1")
    service.offsets("well-a1", same_field_only=False)
    service.summary(field_id="fld-a")
    service.patterns(field_id="fld-a")
    service.list_patterns()
    assert db_fingerprint(service.database) == before


def test_a_read_does_not_commit_the_callers_pending_work(world, service) -> None:
    with service.database.session() as caller:
        caller.get(NptRecord, "npt-a1a").duration_hours = 99.0  # the caller's in-flight change
        payload = service.npt(field_id="fld-a", session=caller)
        # The read runs inside the caller's transaction, so it sees the in-flight value...
        assert payload["total_hours"] == 113.0
        # ...while the database still holds the committed value: the read did not commit it.
        with service.database.read_only() as other:
            assert other.get(NptRecord, "npt-a1a").duration_hours == 5.0
    # Closing the caller's session rolls the in-flight change back; the committed value survives.
    with service.database.read_only() as other:
        assert other.get(NptRecord, "npt-a1a").duration_hours == 5.0
    # ...and the caller still owns the commit.
    with service.database.session() as caller:
        caller.get(NptRecord, "npt-a1a").duration_hours = 99.0
        caller.commit()
    with service.database.read_only() as other:
        assert other.get(NptRecord, "npt-a1a").duration_hours == 99.0


def test_pattern_staleness_on_a_caller_session_is_the_callers_to_commit(world, service) -> None:
    with workspace_session(service) as session:
        row = snapshot(session, find_recurring(session, field_id="fld-a")[0])
        session.commit()
        pattern_id = row.id
        session.add(
            ProblemOccurrence(
                id="prob-a1e",
                well_id="well-a1",
                problem_definition_id="pdef-stuck_pipe",
                problem_type="stuck_pipe",
                hole_size_in=8.5,
                occurred_at=datetime(2025, 1, 16, 8, 0),
            )
        )
        session.commit()
    with service.database.session() as caller:
        report = service.pattern_staleness(pattern_id, session=caller)
        assert report["stale"] is True
        caller.rollback()
    with service.database.read_only() as other:
        assert other.get(FieldPattern, pattern_id).stale_at is None, (
            "the flag was written in the caller's transaction; rolling it back unwrites it"
        )


# -- query efficiency ---------------------------------------------------------------------


def test_find_recurring_is_not_one_query_per_grouping(world, service) -> None:
    with workspace_session(service) as session:
        for index in range(25):
            session.add(
                Well(
                    id=f"well-q{index:02d}",
                    name=f"Q-{index:02d}",
                    field_id="fld-a",
                    project_id="proj-alpha",
                )
            )
            session.flush()
            session.add(
                ProblemDefinition(
                    id=f"pdef-q{index:02d}",
                    canonical_key=f"type_q{index:02d}",
                    problem_type=f"type_q{index:02d}",
                    name=f"Q{index:02d}",
                )
            )
            session.add(
                ProblemOccurrence(
                    id=f"prob-q{index:02d}a",
                    well_id=f"well-q{index:02d}",
                    problem_definition_id=f"pdef-q{index:02d}",
                    problem_type=f"type_q{index:02d}",
                    hole_size_in=8.5,
                    occurred_at=datetime(2025, 6, 1, 0, index % 60),
                )
            )
            session.flush()
            session.add(
                ProblemOccurrence(
                    id=f"prob-q{index:02d}b",
                    well_id=f"well-q{index:02d}",
                    problem_definition_id=f"pdef-q{index:02d}",
                    problem_type=f"type_q{index:02d}",
                    hole_size_in=12.25,
                    occurred_at=datetime(2025, 6, 2, 0, index % 60),
                )
            )
            session.commit()
    counts: dict[str, int] = {}

    def _count(_conn, _cursor, _statement, _params, _context, _exec_options):
        counts["n"] = counts.get("n", 0) + 1

    event.listen(service.database.engine, "before_cursor_execute", _count)
    try:
        with service.database.session() as active:
            rows = find_recurring(active, field_id="fld-a", min_occurrences=1, min_wells=1, limit=0)
    finally:
        event.remove(service.database.engine, "before_cursor_execute", _count)
    assert len(rows) == 51, "twenty-five single-type groups at two holes, plus stuck_pipe"
    assert counts["n"] <= 4, (
        f"the grouping is a fixed number of queries, not one round trip per group: {counts}"
    )


def test_offsets_is_not_one_query_per_candidate(world, service) -> None:
    with workspace_session(service) as session:
        for index in range(20):
            session.add(
                Well(
                    id=f"well-o{index:02d}",
                    name=f"O-{index:02d}",
                    field_id="fld-a",
                    project_id="proj-alpha",
                )
            )
            session.flush()
            session.add(
                ProblemOccurrence(
                    id=f"prob-o{index:02d}",
                    well_id=f"well-o{index:02d}",
                    problem_definition_id="pdef-stuck_pipe",
                    problem_type="stuck_pipe",
                    hole_size_in=8.5,
                    occurred_at=datetime(2025, 7, 1, 0, index % 60),
                )
            )
            session.commit()
    counts: dict[str, int] = {}

    def _count(_conn, _cursor, _statement, _params, _context, _exec_options):
        counts["n"] = counts.get("n", 0) + 1

    event.listen(service.database.engine, "before_cursor_execute", _count)
    try:
        rows = service.offsets("well-a1", same_field_only=True, limit=0)
    finally:
        event.remove(service.database.engine, "before_cursor_execute", _count)
    assert len(rows) == 21, "A-2 and the twenty O-wells, all in Field A"
    assert counts["n"] <= 5, f"the candidates are one batch, not one round trip per well: {counts}"


# -- output contract / edge cases -----------------------------------------------------------


def test_public_payloads_are_plain_data_not_orm_objects(world, service) -> None:
    for payload in (
        service.npt(field_id="fld-a"),
        service.problems(field_id="fld-a"),
        service.events(field_id="fld-a"),
        service.lessons(field_id="fld-a"),
        service.wells(field_id="fld-a"),
        service.summary(field_id="fld-a"),
        service.patterns(field_id="fld-a"),
    ):
        _assert_plain(payload, "payload")
    for entry in service.timeline(well_id="well-a1"):
        _assert_plain(entry.to_dict(), "timeline entry")


def _assert_plain(value: Any, label: str) -> None:
    if isinstance(value, dict):
        for item in value.values():
            _assert_plain(item, label)
    elif isinstance(value, (list, tuple)):
        for item in value:
            _assert_plain(item, label)
    else:
        module = type(value).__module__
        assert not module.startswith("sqlalchemy"), f"{label} leaks {type(value).__name__}"


def test_an_empty_field_answers_zero_rows_not_zero_hours_disguised(world, service) -> None:
    with workspace_session(service) as session:
        session.add(Field(id="fld-empty", name="Empty", project_id="proj-alpha"))
        session.commit()
    out = service.npt(field_id="fld-empty")
    assert out["rows"] == 0 and out["total_hours"] == 0.0 and out["unknown_duration"] == 0
    assert out["by_category"] == {} and out["by_well"] == {}
    assert service.problems(field_id="fld-empty")["occurrences"] == 0
    assert service.events(field_id="fld-empty")["events"] == 0
    assert service.patterns(field_id="fld-empty") == []
    assert service.wells(field_id="fld-empty")["count"] == 0
    assert service.summary(field_id="fld-empty")["wells"] == 0


def test_unknown_scopes_are_refused_not_guessed(world, service) -> None:
    with pytest.raises(ValidationError):
        service.npt()  # no scope at all
    with pytest.raises(ValidationError):
        service.timeline()
    with pytest.raises(ValidationError):
        service.timeline(well_id="well-does-not-exist")
    with pytest.raises(ValidationError):
        service.summary()
    with pytest.raises(ValidationError):
        service.patterns()
    # A well that exists but has no records is a valid, empty answer.
    with workspace_session(service) as session:
        session.add(Well(id="well-quiet", name="Quiet", field_id="fld-a", project_id="proj-alpha"))
        session.commit()
    assert service.well_problem_history("well-quiet") == []
    assert service.npt(well_id="well-quiet")["rows"] == 0


def test_limit_zero_means_no_limit_in_the_list_apis(world, service) -> None:
    with workspace_session(service) as session:
        snapshot(session, find_recurring(session, field_id="fld-a")[0])
        session.commit()
        session.add(
            ProblemOccurrence(
                id="prob-a2d",
                well_id="well-a2",
                problem_definition_id="pdef-stuck_pipe",
                problem_type="stuck_pipe",
                hole_size_in=12.25,
                occurred_at=datetime(2025, 1, 17, 8, 0),
            )
        )
        session.commit()
        candidate = next(
            c
            for c in find_recurring(
                session,
                field_id="fld-a",
                problem_type="stuck_pipe",
                min_occurrences=1,
                min_wells=1,
            )
            if c["hole_size_in"] == 12.25
        )
        snapshot(session, candidate)
        session.commit()
    all_rows = service.list_patterns(limit=0)
    assert [row.id for row in all_rows] == [row.id for row in service.list_patterns(limit=-3)]
    assert len(all_rows) == 2
    assert [row.id for row in service.list_patterns(limit=1)] == [all_rows[0].id]


def test_lesson_revisions_do_not_double_count(world, service) -> None:
    with workspace_session(service) as session:
        # A superseded revision of LL-2025-001: the older one stops being current.
        current = session.get(LessonLearned, "les-a1")
        current.revision = 2
        session.flush()
        session.add(
            LessonLearned(
                id="les-a1-r1",
                code="LL-2025-001",
                revision=1,
                is_current=False,
                title="Ream before tripping (rev 1)",
                lesson="An older wording of the same lesson",
                status="APPROVED",
                well_id="well-a1",
                field_id="fld-a",
                problem_type="stuck_pipe",
                approved_by="k.adeyemi",
                approved_at=datetime(2025, 1, 19, 12, 0),
                created_at=datetime(2025, 1, 14, 12, 0),
            )
        )
        session.flush()
        current.supersedes_id = "les-a1-r1"
        session.commit()
    # The 'what the field has learnt' answer carries the lesson once, under its current revision.
    lessons = service.lessons(field_id="fld-a", approved_only=False)
    current_ones = [row["id"] for row in lessons["lessons"] if row["code"] == "LL-2025-001"]
    assert current_ones == ["les-a1"]
    # The timeline is the history surface: the older revision's own dated facts (created, approved)
    # are real events and stay listed, rather than being silently dropped.
    lesson_ids = {entry.row_id for entry in service.timeline(well_id="well-a1", kinds=("lesson",))}
    assert {
        "les-a1:captured",
        "les-a1:approved",
        "les-a1-r1:captured",
        "les-a1-r1:approved",
    } <= lesson_ids


def test_kinds_filter_narrows_the_tables_visited(world, service) -> None:
    npt_only = service.timeline(well_id="well-a1", kinds=("npt",))
    assert all(entry.kind == "npt" for entry in npt_only)
    assert {entry.row_id for entry in npt_only} == {"npt-a1a", "npt-a1b", "npt-a1u", "npt-a1x"}
    with pytest.raises(ValidationError, match="kind"):
        service.timeline(well_id="well-a1", kinds=("horoscope",))
    assert set(TIMELINE_KINDS) == {
        "well",
        "program",
        "procedure",
        "report",
        "operation",
        "event",
        "npt",
        "problem",
        "lesson",
    }
