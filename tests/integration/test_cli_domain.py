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
from tests.fixtures.fieldops import TOTAL_NPT_HOURS, add_casing_program, field_id, ingest, promote

from drilling_intelligence.cli.app import main


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
    assert payload["count"] == 21, payload["count"]
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
    assert again["totals"]["unchanged"] == 22, again["totals"]
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
