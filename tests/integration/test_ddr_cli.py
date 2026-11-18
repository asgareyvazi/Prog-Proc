"""End-to-end tests for the headless DDR authoring workflow.

The CLI deliberately delegates every state transition to DdrAuthoringService and
ReviewActionService. These tests assert that its JSON and human entry points share the
same persisted lifecycle as the desktop consumer, without introducing CLI-only rules.
"""

from __future__ import annotations

import json
import sys
from io import StringIO
from pathlib import Path
from typing import Any

from sqlalchemy import inspect, select

from drilling_intelligence.cli.app import main
from drilling_intelligence.database.models import DdrReport, WellOperation
from drilling_intelligence.operations.ddr import DdrAuthoringService


def _call(workspace, *argv: str, expect: int = 0) -> dict[str, Any]:
    stdout, stderr = StringIO(), StringIO()
    original = sys.stdout, sys.stderr
    sys.stdout, sys.stderr = stdout, stderr
    try:
        code = main([*argv, "--workspace", str(workspace.root), "--json"])
    finally:
        sys.stdout, sys.stderr = original
    assert code == expect, (code, stdout.getvalue(), stderr.getvalue())
    payload = json.loads(stdout.getvalue())
    assert isinstance(payload, dict)
    return payload


def _call_human(workspace, *argv: str, expect: int = 0) -> str:
    stdout, stderr = StringIO(), StringIO()
    original = sys.stdout, sys.stderr
    sys.stdout, sys.stderr = stdout, stderr
    try:
        code = main([*argv, "--workspace", str(workspace.root)])
    finally:
        sys.stdout, sys.stderr = original
    assert code == expect, (code, stdout.getvalue(), stderr.getvalue())
    return stdout.getvalue()


def _write_content(path: Path, *, summary: str = "Operator-entered shift report") -> Path:
    payload = {
        "report_date": "2026-11-07",
        "report_number": "DDR-2026-311",
        "shift": "Day",
        "summary": summary,
        "activities": [
            {
                "operation_type": "drilling",
                "label": "Drilled the 12 1/4 in interval",
                "description": "Wording entered by the operator; no measurements inferred.",
                "period_text": "06:00-18:00",
            },
            {
                "operation_type": "tripping",
                "label": "Pulled out for inspection",
                "description": "Operator-entered activity.",
                "period_text": "18:00-19:30",
            },
        ],
    }
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def test_cli_authors_submits_reviews_and_revises_the_same_authoritative_ddr(
    workspace, committed_well_id: str, tmp_path: Path
) -> None:
    content = _write_content(tmp_path / "day-shift.json")
    created = _call(
        workspace,
        "ddr",
        "create",
        "--well",
        "A-3",
        "--content",
        str(content),
        "--by",
        "author-1",
        "--reason",
        "Start the daily report",
    )
    report_id = created["report_id"]
    assert created["well_id"] == committed_well_id
    assert created["authoring_state"] == "DRAFT"
    assert created["status"] == "CANDIDATE"
    assert len(created["activities"]) == 2
    assert all(not activity["operation_id"] for activity in created["activities"])
    with workspace.database.read_only() as session:
        assert not list(
            session.scalars(select(WellOperation).where(WellOperation.report_id == report_id))
        ), "draft authoring must not create operational records"

    current_list = _call(workspace, "ddr", "list", "--well", "A-3")
    assert current_list["count"] == 1
    assert current_list["reports"][0]["report_id"] == report_id
    shown = _call(workspace, "ddr", "show", report_id)
    assert shown["updated_at"] == created["updated_at"]
    assert _call(workspace, "ddr", "actions", report_id)["actions"] == []

    changed_content = _write_content(
        tmp_path / "day-shift-corrected.json", summary="Corrected operator-entered summary"
    )
    saved = _call(
        workspace,
        "ddr",
        "save",
        report_id,
        "--content",
        str(changed_content),
        "--expected-updated-at",
        created["updated_at"],
        "--by",
        "author-1",
        "--reason",
        "Correct the report text",
    )
    assert saved["summary"] == "Corrected operator-entered summary"
    assert saved["updated_at"] != created["updated_at"]

    stale = _call(
        workspace,
        "ddr",
        "save",
        report_id,
        "--content",
        str(content),
        "--expected-updated-at",
        created["updated_at"],
        "--by",
        "author-1",
        "--reason",
        "Stale editor must be refused",
        expect=1,
    )
    assert stale["ok"] is False
    assert "changed since it was loaded" in stale["message"]
    assert DdrAuthoringService(workspace.database).get(report_id)["summary"] == saved["summary"]

    submitted = _call(
        workspace,
        "ddr",
        "submit",
        report_id,
        "--expected-updated-at",
        saved["updated_at"],
        "--by",
        "author-1",
        "--reason",
        "Ready for independent review",
    )
    assert submitted["authoring_state"] == "SUBMITTED"
    assert submitted["status"] == "CANDIDATE"
    assert all(activity["operation_id"] for activity in submitted["activities"])
    with workspace.database.read_only() as session:
        operations = list(
            session.scalars(select(WellOperation).where(WellOperation.report_id == report_id))
        )
    assert len(operations) == 2
    assert {row.status for row in operations} == {"CANDIDATE"}
    available = _call(workspace, "ddr", "actions", report_id)
    assert {item["action_id"] for item in available["actions"]} == {"confirm", "reject"}

    stale_revision = _call(
        workspace,
        "ddr",
        "decide",
        report_id,
        "--well",
        "A-3",
        "--action",
        "confirm",
        "--expected-status",
        submitted["status"],
        "--expected-revision",
        str(submitted["revision_number"] + 1),
        "--expected-updated-at",
        submitted["updated_at"],
        "--by",
        "reviewer-2",
        "--reason",
        "Stale revision must be refused",
        expect=1,
    )
    assert stale_revision["ok"] is False
    assert "revision changed" in stale_revision["message"]

    self_decision = _call(
        workspace,
        "ddr",
        "decide",
        report_id,
        "--well",
        "A-3",
        "--action",
        "confirm",
        "--expected-status",
        submitted["status"],
        "--expected-revision",
        str(submitted["revision_number"]),
        "--expected-updated-at",
        submitted["updated_at"],
        "--by",
        "author-1",
        "--reason",
        "Author cannot review own report",
        expect=1,
    )
    assert self_decision["ok"] is False
    assert "different from its author" in self_decision["message"]

    confirmed = _call(
        workspace,
        "ddr",
        "decide",
        report_id,
        "--well",
        "A-3",
        "--action",
        "confirm",
        "--expected-status",
        submitted["status"],
        "--expected-revision",
        str(submitted["revision_number"]),
        "--expected-updated-at",
        submitted["updated_at"],
        "--by",
        "reviewer-2",
        "--reason",
        "Reviewed against the shift evidence",
    )
    assert confirmed["action"]["status"] == "CONFIRMED"
    assert confirmed["report"]["status"] == "CONFIRMED"
    # Reviewing the report does not silently review each candidate operation.
    with workspace.database.read_only() as session:
        assert {
            row.status
            for row in session.scalars(
                select(WellOperation).where(WellOperation.report_id == report_id)
            )
        } == {"CANDIDATE"}
        stored = session.get(DdrReport, report_id)
        assert stored is not None and stored.status == "CONFIRMED"

    revision = _call(
        workspace,
        "ddr",
        "revise",
        report_id,
        "--expected-updated-at",
        confirmed["report"]["updated_at"],
        "--by",
        "author-1",
        "--reason",
        "Correct the report after review",
    )
    assert revision["revision_number"] == 2
    assert revision["supersedes_report_id"] == report_id
    assert revision["authoring_state"] == "DRAFT"
    assert revision["status"] == "CANDIDATE"
    assert revision["is_current"] is True
    assert _call(workspace, "ddr", "actions", revision["report_id"])["actions"] == []

    history = _call(workspace, "ddr", "list", "--well", "A-3", "--history")
    assert history["count"] == 2
    assert {row["revision_number"] for row in history["reports"]} == {1, 2}
    current_only = _call(workspace, "ddr", "list", "--well", "A-3")
    assert current_only["count"] == 1
    assert current_only["reports"][0]["report_id"] == revision["report_id"]


def test_cli_ddr_human_output_displays_operator_entered_text(
    workspace, committed_well_id: str, tmp_path: Path
) -> None:
    del committed_well_id
    content = _write_content(tmp_path / "human-readable.json")
    created = _call_human(
        workspace,
        "ddr",
        "create",
        "--well",
        "A-3",
        "--content",
        str(content),
        "--by",
        "author",
        "--reason",
        "human output check",
    )
    assert "DDR draft created" in created
    assert "Operator-entered shift report" in created
    assert "Drilled the 12 1/4 in interval" in created


def test_cli_ddr_content_refuses_ambiguous_and_unknown_json_without_writing(
    workspace, committed_well_id: str, tmp_path: Path
) -> None:
    del committed_well_id  # the create must fail before a well lookup or database write
    invalid_payloads = (
        '{"report_date":"2026-11-07","report_date":"2026-11-08"}',
        '{"report_date":"2026-11-07","unexpected":"guess"}',
        '{"report_date":"2026-11-07","summary":42}',
        '{"report_date":"2026-11-07","activities":[{"operation_type":"drilling","operation_type":"guess","label":"ambiguous"}]}',
        '{"report_date":"2026-11-07","activities":[{"operation_type":"drilling","label":"valid","depth_md":12.0}]}',
        "[]",
    )
    for index, text in enumerate(invalid_payloads):
        path = tmp_path / f"invalid-{index}.json"
        path.write_text(text, encoding="utf-8")
        result = _call(
            workspace,
            "ddr",
            "create",
            "--well",
            "A-3",
            "--content",
            str(path),
            "--by",
            "author",
            "--reason",
            "must fail safely",
            expect=1,
        )
        assert result["ok"] is False
    with workspace.database.read_only() as session:
        assert session.scalar(select(DdrReport.id)) is None


def test_cli_ddr_save_requires_a_complete_snapshot_and_refuses_partial_updates(
    workspace, committed_well_id: str, tmp_path: Path
) -> None:
    content = _write_content(tmp_path / "draft.json")
    draft = _call(
        workspace,
        "ddr",
        "create",
        "--well",
        "A-3",
        "--content",
        str(content),
        "--by",
        "author",
        "--reason",
        "Create draft",
    )
    partial = tmp_path / "partial.json"
    partial.write_text('{"report_date":"2026-11-07","summary":"partial"}', encoding="utf-8")
    refused = _call(
        workspace,
        "ddr",
        "save",
        draft["report_id"],
        "--content",
        str(partial),
        "--expected-updated-at",
        draft["updated_at"],
        "--by",
        "author",
        "--reason",
        "must not clear omitted fields",
        expect=1,
    )
    assert refused["ok"] is False
    assert "missing required field" in refused["message"]
    still_draft = DdrAuthoringService(workspace.database).get(draft["report_id"])
    assert still_draft["summary"] == "Operator-entered shift report"
    assert len(still_draft["activities"]) == 2


def test_cli_ddr_content_size_limit_is_checked_before_json_parsing(
    workspace, committed_well_id: str, tmp_path: Path
) -> None:
    del committed_well_id
    path = tmp_path / "too-large.json"
    path.write_bytes(b" " * (1_048_576 + 1))
    result = _call(
        workspace,
        "ddr",
        "create",
        "--well",
        "A-3",
        "--content",
        str(path),
        "--by",
        "author",
        "--reason",
        "must fail before parse",
        expect=1,
    )
    assert "1 MiB" in result["message"]

    too_many = tmp_path / "too-many-activities.json"
    too_many.write_text(
        json.dumps(
            {
                "report_date": "2026-11-07",
                "activities": [
                    {"operation_type": "drilling", "label": "bounded activity"} for _ in range(201)
                ],
            }
        ),
        encoding="utf-8",
    )
    excessive = _call(
        workspace,
        "ddr",
        "create",
        "--well",
        "A-3",
        "--content",
        str(too_many),
        "--by",
        "author",
        "--reason",
        "must fail before persistence",
        expect=1,
    )
    assert "at most 200 activities" in excessive["message"]
    with workspace.database.read_only() as session:
        assert session.scalar(select(DdrReport.id)) is None


def test_invalid_ddr_json_does_not_bootstrap_an_unopened_workspace_database(tmp_path: Path) -> None:
    """A rejected snapshot must fail before lazy schema creation touches the workspace DB."""
    from drilling_intelligence.wells.workspace import Workspace

    fresh = Workspace.create(tmp_path / "unopened-workspace", name="Unopened workspace")
    database_path = fresh.database_path
    assert not database_path.exists()
    invalid = tmp_path / "duplicate-key.json"
    invalid.write_text('{"report_date":"2026-11-07","report_date":"2026-11-08"}', encoding="utf-8")

    result = _call(
        fresh,
        "ddr",
        "create",
        "--well",
        "A-3",
        "--content",
        str(invalid),
        "--by",
        "author",
        "--reason",
        "malformed snapshots must have no database side effect",
        expect=1,
    )
    assert result["ok"] is False
    assert "duplicate JSON key" in result["message"]
    assert not database_path.exists()


def _sqlite_fingerprint(database) -> tuple[tuple[str, tuple[tuple[str, ...], ...]], ...]:
    names = sorted(inspect(database.engine).get_table_names())
    with database.engine.connect() as connection:
        return tuple(
            (
                name,
                tuple(
                    tuple(str(value) for value in row)
                    for row in connection.exec_driver_sql(
                        f'SELECT * FROM "{name}" ORDER BY rowid'  # noqa: S608
                    ).fetchall()
                ),
            )
            for name in names
        )


def test_cli_ddr_compare_is_scoped_read_only_stable_and_reports_ambiguity(
    workspace, committed_well_id: str
) -> None:
    service = DdrAuthoringService(workspace.database)
    predecessor = service.create_draft(
        well_id=committed_well_id,
        report_date="2026-11-07",
        summary="Operator summary",
        activities=[
            {"operation_type": "drilling", "label": "First", "description": "Exact text"},
            {"operation_type": "tripping", "label": "Last", "description": "Last text"},
        ],
        actor="declared-author",
        reason="Create comparison predecessor",
    )
    submitted = service.submit(
        predecessor["report_id"],
        expected_updated_at=predecessor["updated_at"],
        actor="declared-author",
        reason="Submit comparison predecessor",
    )
    successor = service.create_revision(
        predecessor["report_id"],
        expected_updated_at=submitted["updated_at"],
        actor="declared-author",
        reason="Create comparison successor",
    )
    successor = service.save_draft(
        successor["report_id"],
        expected_updated_at=successor["updated_at"],
        report_date="2026-11-07",
        summary="Operator summary",
        activities=[
            {"operation_type": "drilling", "label": "First", "description": "Exact text"},
            {"operation_type": "cementing", "label": "Inserted", "description": "Exact insert"},
            {"operation_type": "tripping", "label": "Last", "description": "Last text"},
        ],
        actor="declared-author",
        reason="Insert one explicitly authored activity",
    )

    before = _sqlite_fingerprint(workspace.database)
    first = _call(
        workspace,
        "ddr",
        "compare",
        predecessor["report_id"],
        successor["report_id"],
    )
    second = _call(
        workspace,
        "ddr",
        "compare",
        predecessor["report_id"],
        successor["report_id"],
    )
    assert first == second
    assert first["ok"] is True
    comparison = first["comparison"]
    assert comparison["status"] == "OK"
    assert comparison["read_only"] is True
    assert comparison["approval_evaluated"] is False
    assert [item["status"] for item in comparison["activities"]["items"]] == [
        "UNCHANGED",
        "ADDED",
        "UNCHANGED",
    ]
    assert comparison["activities"]["items"][1]["successor"]["label"] == "Inserted"
    assert _sqlite_fingerprint(workspace.database) == before

    mismatched = _call(
        workspace,
        "ddr",
        "compare",
        successor["report_id"],
        predecessor["report_id"],
        expect=1,
    )
    assert "does not name the requested predecessor" in mismatched["message"]
    assert str(workspace.root) not in json.dumps(mismatched)
    assert _sqlite_fingerprint(workspace.database) == before

    repeated = service.create_draft(
        well_id=committed_well_id,
        report_date="2026-11-07",
        activities=[
            {"operation_type": "drilling", "label": "Repeated"},
            {"operation_type": "drilling", "label": "Repeated"},
        ],
        actor="declared-author",
        reason="Create a duplicate-content comparison case",
    )
    repeated_submitted = service.submit(
        repeated["report_id"],
        expected_updated_at=repeated["updated_at"],
        actor="declared-author",
        reason="Submit duplicate-content predecessor",
    )
    repeated_successor = service.create_revision(
        repeated["report_id"],
        expected_updated_at=repeated_submitted["updated_at"],
        actor="declared-author",
        reason="Create duplicate-content successor",
    )
    ambiguous = _call(
        workspace,
        "ddr",
        "compare",
        repeated["report_id"],
        repeated_successor["report_id"],
        expect=3,
    )
    assert ambiguous["ok"] is False
    assert ambiguous["comparison"]["status"] == "AMBIGUOUS"
    assert ambiguous["comparison"]["has_changes"] is None
