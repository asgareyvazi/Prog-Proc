"""End-to-end smoke of the new headless Program/Procedure authoring surfaces."""

from __future__ import annotations

import json
import sys
from io import StringIO
from pathlib import Path
from typing import Any

import pytest

from drilling_intelligence.cli.app import main
from drilling_intelligence.core.errors import ValidationError
from drilling_intelligence.engineering.authoring_cli import read_json
from drilling_intelligence.wells.repository import WellRepository


def _call(workspace, *argv: str, expect: int = 0) -> dict[str, Any]:
    stdout, stderr = StringIO(), StringIO()
    old_stdout, old_stderr = sys.stdout, sys.stderr
    sys.stdout, sys.stderr = stdout, stderr
    try:
        code = main([*argv, "--workspace", str(workspace.root), "--json"])
    finally:
        sys.stdout, sys.stderr = old_stdout, old_stderr
    assert code == expect, (code, stdout.getvalue(), stderr.getvalue())
    return json.loads(stdout.getvalue())


def _file(tmp_path: Path, name: str, payload: Any) -> str:
    path = tmp_path / name
    path.write_text(json.dumps(payload), encoding="utf-8")
    return str(path)


def test_cli_can_author_program_and_procedure_drafts_and_reopen_them(
    workspace, tmp_path: Path
) -> None:
    with workspace.database.unit_of_work() as session:
        hierarchy = WellRepository(session)
        project = hierarchy.get_or_create_project("CLI Engineering Project")
        field = hierarchy.get_or_create_field("CLI Engineering Field", project=project)
        well = hierarchy.create_well("CLI-WELL-1", project_id=project.id, field_id=field.id)

    program = _call(
        workspace,
        "programs",
        "create",
        "--content",
        _file(
            tmp_path,
            "p.json",
            {
                "title": "CLI synthetic drilling program",
                "code": "CLI-WELL-1-DRP",
                "author": "author-a",
                "well_id": well.id,
                "summary": "Synthetic fixture values only.",
            },
        ),
    )
    assert program["status"] == "DRAFT"
    program = _call(
        workspace,
        "programs",
        "update",
        program["id"],
        "--expected-updated-at",
        program["updated_at"],
        "--content",
        _file(tmp_path, "program-update.json", {"summary": "Updated source-entered summary."}),
    )
    section = _call(
        workspace,
        "programs",
        "section-add",
        program["id"],
        "--expected-updated-at",
        program["updated_at"],
        "--content",
        _file(
            tmp_path,
            "section.json",
            {
                "section_key": "design_basis",
                "title": "Design basis",
                "content": "Synthetic fixture; no engineering defaults.",
            },
        ),
    )
    target = _call(
        workspace,
        "programs",
        "target-add",
        program["id"],
        "--expected-updated-at",
        section["program_updated_at"],
        "--content",
        _file(
            tmp_path,
            "target.json",
            {
                "name": "12 1/4 in",
                "hole_size_in": 12.25,
                "planned_depth_md_value": 1200,
                "planned_depth_md_unit": "m",
            },
        ),
    )
    assert target["sequence"] == 1
    target2 = _call(
        workspace,
        "programs",
        "target-add",
        program["id"],
        "--expected-updated-at",
        target["updated_at"],
        "--content",
        _file(
            tmp_path,
            "target2.json",
            {"name": "8 1/2 in", "planned_depth_md_value": 3200, "planned_depth_md_unit": "m"},
        ),
    )
    updated_target = _call(
        workspace,
        "programs",
        "target-update",
        program["id"],
        target2["id"],
        "--expected-updated-at",
        target2["updated_at"],
        "--content",
        _file(
            tmp_path,
            "target-change.json",
            {"planned_depth_md_value": 3250, "planned_depth_md_unit": "m"},
        ),
    )
    reordered_targets = _call(
        workspace,
        "programs",
        "targets-reorder",
        program["id"],
        "--expected-updated-at",
        updated_target["updated_at"],
        "--order",
        _file(tmp_path, "target-order.json", [target2["id"], target["id"]]),
    )
    shown = _call(workspace, "programs", "show", program["id"])
    assert len(shown["sections"]) == 1 and len(shown["targets"]) == 2
    assert [item["id"] for item in shown["targets"]] == [target2["id"], target["id"]]
    assert shown["sections"][0]["id"] == section["id"]
    assert _call(workspace, "programs", "validate", program["id"])["ready_for_review"] is True
    submitted = _call(
        workspace,
        "programs",
        "submit",
        program["id"],
        "--expected-updated-at",
        reordered_targets["updated_at"],
        "--by",
        "author-a",
    )
    reviewed = _call(
        workspace,
        "programs",
        "review",
        program["id"],
        "--expected-updated-at",
        submitted["updated_at"],
        "--by",
        "reviewer-b",
        "--note",
        "Reviewed the structure; no safety or calculation assessment.",
    )
    approved = _call(
        workspace,
        "programs",
        "approve",
        program["id"],
        "--expected-updated-at",
        reviewed["updated_at"],
        "--by",
        "reviewer-b",
        "--note",
        "Fixture workflow review decision.",
    )
    assert approved["status"] == "APPROVED"
    assert approved["actor_strings_are_authenticated"] is False
    approved_show = _call(workspace, "programs", "show", program["id"])
    assert any(
        event["action"] == "engineering.technical_review.recorded"
        for event in approved_show["review_history"]
    )

    procedure = _call(
        workspace,
        "procedures",
        "create",
        "--content",
        _file(
            tmp_path,
            "proc.json",
            {
                "title": "CLI synthetic procedure",
                "code": "CLI-WELL-1-PROC-1",
                "procedure_type": "general",
                "description": "Fixture-only purpose and scope.",
                "created_by": "author-a",
                "well_id": well.id,
            },
        ),
    )
    step = _call(
        workspace,
        "procedures",
        "step-add",
        procedure["id"],
        "--expected-updated-at",
        procedure["updated_at"],
        "--content",
        _file(
            tmp_path,
            "step.json",
            {
                "title": "Record source status",
                "instructions": "Record only source-backed status.",
                "responsible_role": "Engineer",
                "acceptance_criteria": "Source is cited.",
                "hold_point": True,
            },
        ),
    )
    assert step["sequence"] == 1
    reopened = _call(workspace, "procedures", "show", procedure["id"])
    assert reopened["steps"][0]["id"] == step["id"]
    assert reopened["steps"][0]["hold_point"] is True
    assert _call(workspace, "procedures", "validate", procedure["id"])["ready_for_review"] is True
    submitted_proc = _call(
        workspace,
        "procedures",
        "submit",
        procedure["id"],
        "--expected-updated-at",
        step["updated_at"],
        "--by",
        "author-a",
    )
    reviewed_proc = _call(
        workspace,
        "procedures",
        "review",
        procedure["id"],
        "--expected-updated-at",
        submitted_proc["updated_at"],
        "--by",
        "reviewer-b",
        "--note",
        "Structure reviewed; no operational safety claim.",
    )
    approved_proc = _call(
        workspace,
        "procedures",
        "approve",
        procedure["id"],
        "--expected-updated-at",
        reviewed_proc["updated_at"],
        "--by",
        "reviewer-b",
        "--note",
        "Fixture procedure review decision.",
    )
    assert approved_proc["status"] == "APPROVED"
    assert approved_proc["actor_strings_are_authenticated"] is False


def test_authoring_json_rejects_duplicate_keys_unknown_fields_and_oversize(tmp_path: Path) -> None:
    duplicate = tmp_path / "duplicate.json"
    duplicate.write_text('{"title":"one","title":"two"}', encoding="utf-8")
    with pytest.raises(ValidationError, match="unambiguous"):
        read_json(str(duplicate))
    unknown = tmp_path / "unknown.json"
    unknown.write_text('{"unexpected":1}', encoding="utf-8")
    from drilling_intelligence.engineering.authoring_cli import _reject_unknown

    with pytest.raises(ValidationError, match="unknown field"):
        _reject_unknown(read_json(str(unknown)), {"title"}, label="program")
    oversized = tmp_path / "oversized.json"
    oversized.write_bytes(b" " * (1024 * 1024 + 1))
    with pytest.raises(ValidationError, match="1 MiB"):
        read_json(str(oversized))
