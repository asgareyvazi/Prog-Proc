"""The domain commands, run for real: the same answers the Python API gives, printed by ``drillintel``.

A CLI test that only checks that ``--help`` works proves nothing about a tool whose purpose is to be
piped into a script.  So every assertion here compares what a command printed against what the corpus
actually states - and against the service call the command is supposed to be a thin wrapper around, which
is the drift these commands exist to avoid.
"""

from __future__ import annotations

import json
import sys
from io import StringIO
from pathlib import Path

import pytest
from sqlalchemy import func, select
from tests.fixtures.fieldops import (
    TOTAL_NPT_HOURS,
    add_casing_program,
    field_id,
    ingest,
    promote,
    well_id_for,
)

from drilling_intelligence.cli.app import main
from drilling_intelligence.database.models import (
    DrillingProgram,
    NptRecord,
    ProgramTarget,
    WellSection,
)
from drilling_intelligence.engineering.repository import EngineeringRepository
from drilling_intelligence.wells.repository import WellRepository


@pytest.fixture
def ready(workspace) -> Path:
    """The corpus ingested, promoted and given a programme, so every command has something to say."""
    ingest(workspace)
    promote(workspace)
    add_casing_program(workspace)
    return workspace


def call(workspace, *argv: str, expect: int = 0) -> dict:
    """One ``--json`` command, and the document it printed.

    stdout is captured rather than a temporary file read back, because the promise ``--json`` makes is that
    the payload is the *only* thing on that stream - so a stray print from a library has to break this
    helper, which is exactly what :func:`tests.unit.test_cli.payload` checks for the other commands.
    """
    out, err = StringIO(), StringIO()
    saved = sys.stdout, sys.stderr
    sys.stdout, sys.stderr = out, err
    try:
        code = main([*argv, "--workspace", str(workspace.root), "--json"])
    finally:
        sys.stdout, sys.stderr = saved
    assert code == expect, (code, expect, out.getvalue()[:2000], err.getvalue()[:2000])
    text = out.getvalue()
    assert text.startswith("{"), text[:400]
    return json.loads(text)


def _capture(workspace, *argv: str) -> tuple[int, str, str]:
    """A command expected to fail: its code, its stdout document and its stderr text."""
    out, err = StringIO(), StringIO()
    saved = sys.stdout, sys.stderr
    sys.stdout, sys.stderr = out, err
    try:
        code = main([*argv, "--workspace", str(workspace.root), "--json"])
    finally:
        sys.stdout, sys.stderr = saved
    return code, out.getvalue(), err.getvalue()


def _capture_text(workspace, *argv: str) -> tuple[int, str, str]:
    """Human-mode output together with its exit code, for commands that legitimately exit 1."""
    out, err = StringIO(), StringIO()
    saved = sys.stdout, sys.stderr
    sys.stdout, sys.stderr = out, err
    try:
        code = main([*argv, "--workspace", str(workspace.root)])
    finally:
        sys.stdout, sys.stderr = saved
    return code, out.getvalue(), err.getvalue()


def _text(workspace, *argv: str) -> tuple[str, str]:
    """The same command without ``--json``: what a person at a terminal actually reads."""
    out, err = StringIO(), StringIO()
    saved = sys.stdout, sys.stderr
    sys.stdout, sys.stderr = out, err
    try:
        code = main([*argv, "--workspace", str(workspace.root)])
    finally:
        sys.stdout, sys.stderr = saved
    assert code == 0, (code, out.getvalue()[:800], err.getvalue()[:800])
    return out.getvalue(), err.getvalue()


def test_fields_summary_counts_what_the_corpus_states(ready) -> None:
    payload = call(ready, "fields", "summary", "--field", "North Cormorant")
    assert payload["npt_hours"] == TOTAL_NPT_HOURS
    assert payload["npt_rows"] == 5 and payload["npt_undated"] == 2
    assert payload["problems"] == 3 and payload["reports"] == 2
    assert payload["npt_by_category"]["stuck_pipe"]["hours"] == 28.75
    assert payload["problem_types"]["stuck_pipe"]["wells"] == 2
    assert payload["wells"] == 2


def test_fields_list_and_offsets_name_the_wells(ready) -> None:
    listing = call(ready, "fields", "list")
    assert listing["count"] == 1, listing
    (row,) = listing["fields"]
    assert row["name"] == "North Cormorant" and row["wells"] == 2
    assert row["id"] == field_id(ready)
    offsets = call(ready, "fields", "offsets", "--well", "A-3")
    assert [item["name"] for item in offsets["offsets"]] == ["B-11"]
    assert offsets["offsets"][0]["shared_problem_types"] == ["stuck_pipe"]
    assert offsets["offsets"][0]["npt_hours"] == 22.25


def test_timeline_lists_the_records_in_order_and_respects_the_window(ready) -> None:
    payload = call(ready, "timeline", "--well", "A-3")
    # 22: the operational records, plus both programmes - the corpus's own drilling program, which
    # promotion now reads, and the 8 1/2 in one the fixture writes.
    assert payload["count"] == 22, payload["count"]
    entries = payload["entries"]
    assert entries[0]["table"] == "ddr_report" and entries[0]["at"].startswith("2025-06-13")
    assert entries[-1]["at"] is None, "the undated records are last"
    assert any(entry["kind"] == "problem" for entry in entries)
    windowed = call(
        ready, "timeline", "--well", "A-3", "--since", "2025-06-13", "--until", "2025-06-14"
    )
    assert windowed["count"] == 9, windowed["count"]
    assert all(entry["at"] for entry in windowed["entries"])
    forced = call(
        ready,
        "timeline",
        "--well",
        "A-3",
        "--since",
        "2025-06-13",
        "--until",
        "2025-06-14",
        "--include-undated",
    )
    assert forced["count"] == 20, forced["count"]
    only_npt = call(ready, "timeline", "--well", "A-3", "--kind", "npt")
    assert {entry["kind"] for entry in only_npt["entries"]} == {"npt"}


def test_an_unknown_kind_is_an_error_the_cli_reports_without_a_traceback(ready) -> None:
    code, out, _err = _capture(ready, "timeline", "--well", "A-3", "--kind", "omens")
    assert code == 1
    payload = json.loads(out)
    assert payload["ok"] is False and payload["code"] == "VALIDATION"
    assert "no timeline kind named omens" in payload["message"]
    assert "known" in payload["context"], payload


def test_records_list_reads_one_table_and_records_summary_counts_them_all(ready) -> None:
    rows = call(ready, "records", "list", "--table", "npt", "--field", "North Cormorant")
    assert rows["count"] == 5
    assert rows["rows"][0]["duration_basis"] == "STATED"
    assert all(row["status"] == "CANDIDATE" for row in rows["rows"])
    problems = call(ready, "records", "list", "--table", "problem", "--well", "A-3")
    assert problems["count"] == 2
    operations = call(
        ready, "records", "list", "--table", "operation", "--field", "North Cormorant"
    )
    assert operations["count"] == 9, operations["count"]
    summary = call(ready, "records", "summary", "--field", "North Cormorant")
    assert summary["npt"]["rows"] == 5 and summary["npt"]["promoted"] == 5
    assert summary["npt"]["total_hours"] == TOTAL_NPT_HOURS
    assert summary["reports"] == 2 and summary["operations"] == 9 and summary["problems"] == 3
    assert summary["npt_by_status"] == {"CANDIDATE": 5}


def test_records_promote_is_idempotent_from_the_terminal(ready) -> None:
    again = call(ready, "records", "promote", "--field", "North Cormorant")
    assert again["totals"]["created"] == 0, again
    assert again["totals"]["unchanged"] == 24, again["totals"]
    assert again["totals"]["conflict"] == 0
    assert set(again["skipped"]) == {"ZERO_NPT", "TOTAL_ALREADY_COUNTED"}, again["skipped"]


def test_patterns_find_snapshot_and_recheck_a_snapshot(ready) -> None:
    found = call(ready, "patterns", "find", "--field", "North Cormorant")
    assert found["count"] == 1, found
    (stuck,) = found["patterns"]
    assert stuck["problem_type"] == "stuck_pipe" and stuck["occurrence_count"] == 2
    assert stuck["total_npt_hours"] == 28.75
    assert stuck["query"]["field_id"] == field_id(ready)
    assert (
        call(ready, "patterns", "find", "--field", "North Cormorant", "--min-wells", "3")["count"]
        == 0
    )
    snapshot = call(
        ready, "patterns", "snapshot", "--field", "North Cormorant", "--by", "k.adeyemi"
    )
    assert snapshot["created"] == 1 and snapshot["refreshed"] == 0, snapshot
    listed = call(ready, "patterns", "list", "--field", "North Cormorant")
    assert listed["count"] == 1 and listed["patterns"][0]["status"] == "CANDIDATE"
    pattern_id = listed["patterns"][0]["id"]
    fresh = call(ready, "patterns", "stale", pattern_id)
    assert fresh["stale"] is False and fresh["differences"] == {}
    confirmed = call(
        ready,
        "patterns",
        "confirm",
        pattern_id,
        "--by",
        "k.adeyemi",
        "--reason",
        "reviewed",
    )
    assert confirmed["status"] == "CONFIRMED"
    assert confirmed["attributes"]["status_history"][-1]["by"] == "k.adeyemi"
    advice = call(
        ready,
        "patterns",
        "recommend",
        pattern_id,
        "--statement",
        "Ream to bottom before tripping out",
        "--reason",
        "two stuck-bit events",
    )
    assert advice["status"] == "PROPOSED" and advice["pattern_id"] == pattern_id
    assert advice["query"]["problem_type"] == "stuck_pipe"


def test_an_unattributed_decision_is_refused_by_the_cli(ready) -> None:
    snapshot = call(ready, "patterns", "snapshot", "--field", "North Cormorant")
    pattern_id = snapshot["patterns"][0]["id"]
    code, out, _err = _capture(ready, "patterns", "confirm", pattern_id)
    assert code == 1, out
    payload = json.loads(out)
    # The CLI refuses an unattributed decision in its own words, because the flag is the thing that is
    # missing; the repository's stricter rule sits behind it for every other caller.
    assert "has to be attributed" in payload["message"]
    assert payload["context"]["hint"] == "pass --by <who reviewed it>"


def test_lessons_list_and_counts_report_the_review_state(ready) -> None:
    empty = call(ready, "lessons", "list", "--field", "North Cormorant")
    assert empty["count"] == 0, empty
    counts = call(ready, "lessons", "counts", "--field", "North Cormorant")
    assert counts["lessons"] == 0 and counts["practices"] == 0
    assert counts["without_evidence"] == 0 and counts["recommendations_open"] == 0
    practices = call(ready, "lessons", "practices", "--field", "North Cormorant")
    assert practices["count"] == 0


def test_doctor_reports_the_new_tables_without_crying_wolf(ready) -> None:
    code, out, _err = _capture(ready, "doctor")
    payload = json.loads(out)
    # The counts and the operational checks have to be on the document, and none of the findings may be
    # about the operational tables.  This workspace does have findings - its search index was never built,
    # because the fixture promoted straight into the registry, and two knowledge rows genuinely disagree -
    # so the exit code is 1 and the test says so rather than pretending the workspace is spotless.  What
    # matters is that the record counts sit in `notes`, which is the part that cannot flip the code.
    assert payload["integrity_problems"] == [], payload["integrity_problems"]
    assert payload["operational"]["npt"] == 5 and payload["operational"]["problems"] == 3
    assert payload["operational"]["reports"] == 2 and payload["operational"]["operations"] == 9
    assert payload["operational"]["lessons"] == 0
    assert payload["schema"]["up_to_date"] is True
    assert any(line.startswith("records    ") for line in payload["notes"]), payload["notes"]
    assert not [
        line for line in payload["findings"] if "npt_record" in line or "problem_occurrence" in line
    ], payload["findings"]
    assert code == 1, (code, payload["findings"])


def test_doctor_turns_a_broken_cross_well_link_into_a_finding(ready) -> None:
    """A hand-edited database is what `doctor` is for.

    The promotion pipeline never produces a link from an NPT record to an event in another well, but the
    schema cannot forbid it either - so the check is proven here by writing the bad link with raw SQL and
    asserting `doctor` finds it and refuses to exit 0.  Without the second half of that assertion, someone
    could drop the operational check from `doctor` and every other test would still pass.
    """
    from sqlalchemy import select, text

    from drilling_intelligence.database.models import NptRecord, WellEvent

    with ready.database.session() as session:
        # Several promoted NPT rows already cite an event and no problem is reported for them, which is
        # the clean case; exactly one of them is broken here.
        npt = session.scalars(
            select(NptRecord).where(NptRecord.event_id.is_not(None)).order_by(NptRecord.id).limit(1)
        ).one()
        other = session.scalars(
            select(WellEvent).where(WellEvent.well_id != npt.well_id).limit(1)
        ).one()
        foreign_event_id, well_id = other.id, npt.well_id
        session.execute(
            text("UPDATE npt_record SET event_id = :event WHERE id = :id"),
            {"event": foreign_event_id, "id": npt.id},
        )
        session.commit()
        row_id = npt.id
    assert session.get(NptRecord, row_id) is not None, "the row is still there, just wrongly linked"

    code, out, _err = _capture(ready, "doctor")
    payload = json.loads(out)
    assert code == 1
    found = [item for item in payload["integrity_problems"] if item["row_id"] == row_id]
    assert len(found) == 1, found
    assert found[0]["table"] == "npt_record"
    assert "another well" in found[0]["problem"], found[0]
    assert len(payload["integrity_problems"]) == 1, payload["integrity_problems"]
    assert well_id != foreign_event_id
    assert any(row_id in line for line in payload["findings"]), payload["findings"]


def test_a_command_without_a_scope_says_so(ready) -> None:
    code, out, _err = _capture(ready, "fields", "summary")
    assert code == 1
    payload = json.loads(out)
    assert payload["ok"] is False
    assert payload["message"] == "this command needs a scope"
    assert "--well" in payload["context"]["hint"]


def _record_a_dependency(workspace) -> tuple[str, str]:
    """One real calculation, recorded through the service, citing a canonical subject.

    Written through the engineering service rather than by hand so the row the CLI reads is the row a
    user would actually have: a real well id, a real transaction, a real canonical key.
    """
    from drilling_intelligence.core.ids import subject_key
    from drilling_intelligence.engineering.repository import EngineeringRepository

    well = well_id_for(workspace, "A-3")
    key = subject_key(well_id=well, property_name="mud_weight", record_state="ACTUAL")
    with workspace.database.unit_of_work() as session:
        row, _ = EngineeringRepository(session).record_calculation(
            method_id="hydraulics.ecd",
            method_version="1.0",
            inputs={"mw": {"value": "10.2 ppg", "subject_key": key}},
            outputs={"ecd_ppg": 11.4},
            well_id=well,
        )
        session.flush()
        return key, row.id


def test_records_impact_reports_the_dependency_and_says_it_resolved_the_subject(ready) -> None:
    key, calculation_id = _record_a_dependency(ready)
    payload = call(ready, "records", "impact", key)
    assert payload["resolved"] is True
    assert payload["subject_kind"] == "well"
    assert payload["calculations"] == 1
    assert payload["counts"]["CURRENT"] == 1
    assert [entry["calculation_id"] for entry in payload["entries"]] == [calculation_id]
    assert payload["entries"][0]["method_id"] == "hydraulics.ecd"


def test_records_impact_distinguishes_an_unrecognised_subject_from_an_unaffected_one(
    ready,
) -> None:
    """The distinction the command exists for, checked on the exit code as well as the text."""
    from drilling_intelligence.core.ids import subject_key

    _record_a_dependency(ready)
    unaffected = call(
        ready,
        "records",
        "impact",
        subject_key(well_id=well_id_for(ready, "A-3"), property_name="nothing_here"),
    )
    assert unaffected["resolved"] is True and unaffected["calculations"] == 0

    code, out, _ = _capture(ready, "records", "impact", "mud_report.xlsx!Summary!B9")
    unresolved = json.loads(out)
    assert unresolved["resolved"] is False, unresolved
    assert code == 1, "an unresolved subject is not a success"


def test_records_impact_prints_the_same_answer_as_text_and_as_json(ready) -> None:
    key, calculation_id = _record_a_dependency(ready)
    out, err = StringIO(), StringIO()
    saved = sys.stdout, sys.stderr
    sys.stdout, sys.stderr = out, err
    try:
        code = main(["records", "impact", key, "--workspace", str(ready.root)])
    finally:
        sys.stdout, sys.stderr = saved
    assert code == 0, err.getvalue()
    text = out.getvalue()
    assert not text.startswith("{"), "the default output is text, not JSON"
    assert calculation_id in text and "hydraulics.ecd" in text
    assert "CURRENT" in text
    assert json.loads(json.dumps(call(ready, "records", "impact", key)))["calculations"] == 1


def test_records_impact_does_not_write_anything(ready) -> None:
    """A read-only inspection, compared as a fingerprint of both calculation tables."""
    import hashlib

    from sqlalchemy import select

    from drilling_intelligence.database.models import Calculation, CalculationInput

    key, _ = _record_a_dependency(ready)

    def fingerprint() -> str:
        with ready.database.session() as session:
            rows = []
            for model in (Calculation, CalculationInput):
                for row in session.execute(select(model).order_by(model.id)).scalars():
                    rows.append(
                        sorted(
                            (str(k), str(v))
                            for k, v in row.__dict__.items()
                            if k != "_sa_instance_state"
                        )
                    )
        return hashlib.sha256(repr(rows).encode("utf-8")).hexdigest()

    before = fingerprint()
    call(ready, "records", "impact", key)
    call(ready, "records", "impact", "mud_report.xlsx!Summary!B9", expect=1)
    assert fingerprint() == before


# --------------------------------------------------------------- the planned domain, from a terminal
class TestPlannedDomainIsVisible:
    """A Digital Well Record that a person cannot read is not a record.

    The programme, its targets and the hole section it plans have been written from real documents
    since ADR-0017/0019, and ``plan_actual_summary`` has compared them since before that - but every
    one of those numbers was reachable only from Python.  ``records summary`` counted five operational
    tables and stopped, ``doctor`` printed the same five, and the comparison had no reader at all.
    These tests pin the terminal's view of the planned half.
    """

    def test_records_summary_counts_the_planned_half_too(self, ready) -> None:
        summary = call(ready, "records", "summary", "--field", "North Cormorant")
        planned = summary["planned"]
        # Two programmes, and both current: the promoted drilling programme and the fixture's
        # 8 1/2 in one are different codes, so neither supersedes the other.
        assert planned["programs"] == 2 and planned["programs_current"] == 2
        assert planned["targets"] == 2
        # Two sections in this fixture: the promoted 12 1/4 in one and the casing programme's 8 1/2 in.
        assert planned["sections"] == 2, planned
        assert planned["sections_with_actual_depth"] == 1, (
            "only the drilled section has a bottom depth; a promoted plan must not fabricate one"
        )
        # The operational half is untouched by the addition.
        assert summary["npt"]["rows"] == 5 and summary["reports"] == 2

    def test_records_summary_survives_a_scope_with_nothing_in_it(self, workspace) -> None:
        """A well that is programmed but not yet drilled still has to print.

        ``total_hours`` is None when no NPT row exists - "nothing recorded" is not "0 h lost" - and
        formatting that with :g raised TypeError, so the whole summary died on an empty scope.
        """
        ingest(workspace)
        summary = call(workspace, "records", "summary", "--well", "A-3")
        assert summary["npt"]["rows"] == 0 and summary["npt"]["total_hours"] is None
        assert summary["planned"] == {
            "sections": 0,
            "sections_with_actual_depth": 0,
            "programs": 0,
            "programs_current": 0,
            "targets": 0,
        }
        out, _err = _text(workspace, "records", "summary", "--well", "A-3")
        assert "no hours recorded" in out, out
        assert "planned  0 section(s)" in out, out

    def test_doctor_counts_the_programme_and_the_section(self, ready) -> None:
        code, out, _err = _capture(ready, "doctor")
        payload = json.loads(out)
        assert payload["operational"]["programs"] == 2
        assert payload["operational"]["programs_current"] == 2
        assert payload["operational"]["targets"] == 2
        assert payload["operational"]["sections"] == 2
        assert payload["integrity_problems"] == [], payload["integrity_problems"]
        assert code == 1, "the fixture's unbuilt index and knowledge conflicts still stand"

    def test_plan_actual_prints_the_comparison_the_repository_computes(self, ready) -> None:
        payload = call(ready, "records", "plan-actual", "--well", "A-3")
        assert payload["count"] == len(payload["rows"]) > 0
        with ready.database.read_only() as session:
            expected = EngineeringRepository(session).plan_actual_summary(
                well_id=well_id_for(ready, "A-3")
            )
        assert payload["rows"] == expected, "the CLI must not compute a second answer"
        depth = next(
            row
            for row in payload["rows"]
            if row["metric"] == "depth_md" and row["section"] == "12 1/4 in"
        )
        assert depth["planned"] == 10450.0
        assert depth["actual"] is None and depth["status"] == "NO_ACTUAL"

    def test_plan_actual_needs_a_scope_and_says_which_ones(self, ready) -> None:
        code, out, _err = _capture(ready, "records", "plan-actual")
        assert code == 1
        payload = json.loads(out)
        assert "one well, programme or section" in payload["message"]
        assert "--well" in payload["context"]["hint"]

    def test_plan_actual_refuses_a_metric_it_does_not_have(self, ready) -> None:
        code, out, _err = _capture(
            ready, "records", "plan-actual", "--well", "A-3", "--metric", "cost"
        )
        assert code == 1
        payload = json.loads(out)
        assert "no plan-versus-actual metric" in payload["message"]
        assert "depth_md" in payload["context"]["hint"]

    def test_plan_actual_filters_to_one_metric(self, ready) -> None:
        payload = call(ready, "records", "plan-actual", "--well", "A-3", "--metric", "depth_md")
        assert {row["metric"] for row in payload["rows"]} == {"depth_md"}
        assert payload["scope"]["metric"] == "depth_md"

    def test_plan_actual_scoped_to_a_programme_stays_in_its_own_well(self, ready) -> None:
        """C3's isolation, asserted at the surface a user actually touches."""
        from drilling_intelligence.core.enums import RecordState
        from drilling_intelligence.database.models import Well

        with ready.database.session() as session:
            other = WellRepository(session).get_or_create_section(
                session.get(Well, well_id_for(ready, "B-11")), "12 1/4 in", hole_size_in=12.25
            )
            WellRepository(session).update_section(
                other, {"bottom_depth": (7777.0, "ft")}, state=RecordState.ACTUAL
            )
            session.commit()
        with ready.database.read_only() as session:
            program = session.scalar(select(DrillingProgram))
        payload = call(ready, "records", "plan-actual", "--program", program.id)
        assert {row["well_id"] for row in payload["rows"]} == {well_id_for(ready, "A-3")}
        assert 7777.0 not in [row["actual"] for row in payload["rows"]]

    def test_plan_actual_on_an_unknown_programme_is_empty_not_everything(self, ready) -> None:
        payload = call(ready, "records", "plan-actual", "--program", "prog-nobody-wrote")
        assert payload["count"] == 0 and payload["rows"] == []
        assert payload["by_status"] == {}

    def test_plan_actual_says_so_when_there_is_nothing_to_compare(self, workspace) -> None:
        ingest(workspace)
        out, _err = _text(workspace, "records", "plan-actual", "--well", "A-3")
        assert "0 comparison row(s)" in out
        assert "no section in this scope has a plan or an actual" in out

    def test_plan_actual_writes_nothing(self, ready) -> None:
        """A read is a read: the comparison must not create the section it is about."""
        with ready.database.read_only() as session:
            before = [
                session.scalar(select(func.count()).select_from(model))
                for model in (WellSection, DrillingProgram, ProgramTarget, NptRecord)
            ]
        call(ready, "records", "plan-actual", "--well", "A-3")
        with ready.database.read_only() as session:
            after = [
                session.scalar(select(func.count()).select_from(model))
                for model in (WellSection, DrillingProgram, ProgramTarget, NptRecord)
            ]
        assert before == after


class TestPlanActualCombinedScopes:
    """The scope flags compose as an intersection at the surface a user actually types.

    ``--well``, ``--program`` and ``--section`` were freely combinable from the day the command
    existed, and the repository's programme-well confinement only applied when a programme was the
    *only* scope.  These cases run the real ``main()`` and assert returned rows, not exit codes.
    """

    @pytest.fixture
    def two_wells(self, ready):
        """B-11 gets a section named like A-3's, a drilled depth, and its own programme."""
        from drilling_intelligence.core.enums import RecordState
        from drilling_intelligence.database.models import Well

        with ready.database.session() as session:
            foreign = WellRepository(session).get_or_create_section(
                session.get(Well, well_id_for(ready, "B-11")), "12 1/4 in", hole_size_in=12.25
            )
            WellRepository(session).update_section(
                foreign, {"bottom_depth": (7777.0, "ft")}, state=RecordState.ACTUAL
            )
            engineering = EngineeringRepository(session)
            other = engineering.create_program(
                title="B-11 programme", code="B11-CMB", well_id=well_id_for(ready, "B-11")
            )
            engineering.add_target(
                other.id,
                name="12 1/4 in",
                section_id=foreign.id,
                planned_depth_md_value=8000.0,
                planned_depth_md_unit="ft",
            )
            session.commit()
            foreign_id, other_id = foreign.id, other.id
        with ready.database.read_only() as session:
            mine = session.scalar(
                select(DrillingProgram).where(
                    DrillingProgram.well_id == well_id_for(ready, "A-3"),
                    DrillingProgram.code.is_not(None),
                    DrillingProgram.title.notlike("%B-11%"),
                )
            )
            own_section = session.scalar(
                select(WellSection).where(
                    WellSection.well_id == well_id_for(ready, "A-3"),
                    WellSection.name == "12 1/4 in",
                )
            )
        return {
            "workspace": ready,
            "prog_a": mine.id,
            "prog_b": other_id,
            "sec_a": own_section.id,
            "sec_b": foreign_id,
        }

    def test_a_well_with_another_wells_programme_prints_nothing(self, two_wells) -> None:
        payload = call(
            two_wells["workspace"],
            "records",
            "plan-actual",
            "--well",
            "A-3",
            "--program",
            two_wells["prog_b"],
        )
        assert payload["count"] == 0 and payload["rows"] == []

    def test_a_foreign_actual_never_meets_this_programmes_plan(self, two_wells) -> None:
        payload = call(
            two_wells["workspace"],
            "records",
            "plan-actual",
            "--section",
            two_wells["sec_b"],
            "--program",
            two_wells["prog_a"],
        )
        assert payload["count"] == 0, payload["rows"]
        assert 7777.0 not in [row["actual"] for row in payload["rows"]]

    def test_consistent_scopes_still_answer(self, two_wells) -> None:
        payload = call(
            two_wells["workspace"],
            "records",
            "plan-actual",
            "--well",
            "A-3",
            "--program",
            two_wells["prog_a"],
            "--section",
            two_wells["sec_a"],
            "--metric",
            "depth_md",
        )
        assert [(row["section_id"], row["planned"]) for row in payload["rows"]] == [
            (two_wells["sec_a"], 10450.0)
        ]

    def test_an_unknown_programme_does_not_widen_a_section_scope(self, two_wells) -> None:
        payload = call(
            two_wells["workspace"],
            "records",
            "plan-actual",
            "--program",
            "prog-nobody-wrote",
            "--section",
            two_wells["sec_a"],
        )
        assert payload["count"] == 0 and payload["rows"] == []


def test_doctor_names_a_promoted_row_the_index_has_not_seen(ready) -> None:
    """An unsearchable promoted row is a finding, not a silence.

    ``doctor`` weighed only the document drift counters, so a lesson written after the last index
    build left the workspace with a row that ``search`` could not find while the index reported a
    clean bill of health.  The structured counters were already on the ``--json`` document; they
    simply never reached a finding or the terminal.
    """
    call(ready, "index", "rebuild")
    clean = json.loads(_capture(ready, "doctor")[1])
    assert not [line for line in clean["findings"] if "structured row(s)" in line], clean[
        "findings"
    ]

    with ready.database.session() as session:
        from drilling_intelligence.lessons.repository import LessonRepository

        LessonRepository(session).capture(
            lesson="Ream the cuttings bed before pulling out of hole.",
            title="Hole cleaning",
            well_id=well_id_for(ready, "A-3"),
            created_by="auditor",
        )
        session.commit()

    payload = json.loads(_capture(ready, "doctor")[1])
    assert payload["index"]["structured_missing"] == 1
    finding = [line for line in payload["findings"] if "structured row(s)" in line]
    assert finding, payload["findings"]
    assert "index rebuild" in finding[0]

    # Exit 1: a script must not read "unsearchable row" as a clean index.
    code, out, _err = _capture_text(ready, "index", "status")
    assert code == 1, out
    assert "structured drift: 1 not yet indexed" in out, out
    assert "rebuild recommended: yes" in out, out
