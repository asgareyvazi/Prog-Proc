"""Strict headless authoring commands for revisioned Programs and Procedures."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from ..core.errors import ValidationError
from .repository import PROCEDURE_FIELDS, PROGRAM_FIELDS

_MAX_INPUT_BYTES = 1024 * 1024


def _pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate JSON key {key!r}")
        result[key] = value
    return result


def read_json(path: str, *, root_type: type = dict) -> Any:
    source = Path(path).expanduser()
    try:
        raw = source.read_bytes()
    except OSError as exc:
        raise ValidationError(
            f"cannot read authoring JSON input: {exc.__class__.__name__}"
        ) from exc
    if len(raw) > _MAX_INPUT_BYTES:
        raise ValidationError("authoring JSON input exceeds the 1 MiB limit", size_bytes=len(raw))
    try:
        text = raw.decode("utf-8")
        value = json.loads(
            text,
            object_pairs_hook=_pairs,
            parse_constant=lambda token: (_ for _ in ()).throw(
                ValueError(f"invalid constant {token}")
            ),
        )
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
        detail = str(exc)
        raise ValidationError(
            f"authoring JSON is not unambiguous UTF-8 JSON: {detail[:160]}"
        ) from exc
    if not isinstance(value, root_type):
        raise ValidationError(f"authoring JSON root must be {root_type.__name__}")
    return value


def _reject_unknown(data: dict[str, Any], allowed: set[str], *, label: str) -> None:
    unknown = sorted(set(data) - allowed)
    if unknown:
        raise ValidationError(
            f"{label} has unknown field(s)", fields=unknown, allowed=sorted(allowed)
        )


def _program_payload(row: Any, sections: list[Any], targets: list[Any]) -> dict[str, Any]:
    return {
        "id": row.id,
        "code": row.code,
        "title": row.title,
        "revision": row.revision,
        "revision_label": row.revision_label,
        "is_current": row.is_current,
        "status": str(row.status),
        "supersedes_id": row.supersedes_id,
        "project_id": row.project_id,
        "field_id": row.field_id,
        "well_id": row.well_id,
        "author": row.author,
        "reviewer": row.reviewer,
        "approver": row.approver,
        "summary": row.summary,
        "planned_spud_date": row.planned_spud_date,
        "planned_completion_date": row.planned_completion_date,
        "status_note": row.status_note,
        "created_at": row.created_at,
        "updated_at": row.updated_at,
        "sections": [
            {
                "id": s.id,
                "sequence": s.sequence,
                "key": s.section_key,
                "title": s.title,
                "content": s.content,
                "completion_state": s.completion_state,
                "source_reference": s.source_reference,
                "provenance": s.provenance or [],
            }
            for s in sections
        ],
        "targets": [
            {
                "id": t.id,
                **{
                    name: getattr(t, name)
                    for name in (
                        "sequence",
                        "name",
                        "section_id",
                        "hole_size_in",
                        "casing_program",
                        "formation_top",
                        "planned_depth_md_value",
                        "planned_depth_md_unit",
                        "planned_duration_days",
                        "planned_mud_weight_value",
                        "planned_mud_weight_unit",
                        "planned_npt_hours",
                        "planned_cost_value",
                        "planned_cost_unit",
                    )
                },
                "provenance": t.provenance or [],
            }
            for t in targets
        ],
    }


def _procedure_payload(row: Any, steps: list[Any]) -> dict[str, Any]:
    return {
        "id": row.id,
        "code": row.code,
        "title": row.title,
        "procedure_type": row.procedure_type,
        "revision": row.revision,
        "revision_label": row.revision_label,
        "is_current": row.is_current,
        "status": str(row.status),
        "supersedes_id": row.supersedes_id,
        "project_id": row.project_id,
        "field_id": row.field_id,
        "well_id": row.well_id,
        "section_id": row.section_id,
        "created_by": row.created_by,
        "reviewer": row.reviewer,
        "approved_by": row.approved_by,
        "description": row.description,
        "source_reference": row.source_reference,
        "effective_from": row.effective_from,
        "status_note": row.status_note,
        "created_at": row.created_at,
        "updated_at": row.updated_at,
        "program_revision_id": row.program_revision_id,
        "program_link_status": row.program_link_status,
        "steps": [
            {
                "id": s.id,
                **{
                    name: getattr(s, name)
                    for name in (
                        "sequence",
                        "title",
                        "instructions",
                        "responsible_role",
                        "equipment",
                        "preconditions",
                        "parameters",
                        "expected_result",
                        "acceptance_criteria",
                        "evidence_required",
                        "hold_point",
                        "safety_controls",
                        "contingency",
                        "source_reference",
                    )
                },
                "provenance": s.provenance or [],
            }
            for s in steps
        ],
    }


def _add_authoring_group(sub: Any, common: list[Any], kind: str, handler: Any) -> None:
    group = sub.add_parser(
        kind, help=f"author and review versioned drilling {kind}", parents=[common]
    )
    actions = group.add_subparsers(dest="authoring_action", required=True)
    create = actions.add_parser("create", help=f"create a DRAFT {kind[:-1]}", parents=[common])
    create.add_argument(
        "--content", required=True, help="bounded strict JSON object (UTF-8, max 1 MiB)"
    )
    create.set_defaults(handler=handler)

    listing = actions.add_parser("list", help=f"list current {kind}", parents=[common])
    listing.add_argument("--well", help="well name or ID")
    listing.add_argument("--status")
    listing.add_argument("--history", action="store_true")
    listing.add_argument("--limit", type=int, default=100)
    listing.set_defaults(handler=handler)

    for name, help_text in (
        ("show", "show one exact revision and its structured content"),
        ("validate", "show structural completeness; this is not technical approval"),
    ):
        action = actions.add_parser(name, help=help_text, parents=[common])
        action.add_argument("record_id")
        action.set_defaults(handler=handler)

    update = actions.add_parser(
        "update", help="edit top-level descriptive fields on the current DRAFT", parents=[common]
    )
    update.add_argument("record_id")
    update.add_argument("--content", required=True, help="strict JSON object of editable fields")
    update.add_argument("--expected-updated-at", required=True)
    update.set_defaults(handler=handler)

    revise = actions.add_parser(
        "revise", help="create a successor DRAFT; preserve prior revision", parents=[common]
    )
    revise.add_argument("record_id")
    revise.add_argument(
        "--by", required=True, help="declared author attribution (not authentication)"
    )
    revise.add_argument("--expected-updated-at", required=True)
    revise.add_argument("--content", help="optional strict JSON object of revision changes")
    revise.add_argument("--revision-label", default="")
    revise.set_defaults(handler=handler)

    compare = actions.add_parser(
        "compare",
        help="compare two exact revisions; no technical significance inferred",
        parents=[common],
    )
    compare.add_argument("left_id")
    compare.add_argument("right_id")
    compare.set_defaults(handler=handler)

    for name in ("submit", "review", "approve", "reject"):
        action = actions.add_parser(name, help=f"{name} this exact revision", parents=[common])
        action.add_argument("record_id")
        action.add_argument("--expected-updated-at", required=True)
        action.add_argument(
            "--by", required=True, help="declared actor attribution (not authentication)"
        )
        if name in {"review", "approve", "reject"}:
            action.add_argument("--note", required=True, help="reasoned review/decision note")
        action.set_defaults(handler=handler)

    if kind == "programs":
        singular = "program"
        linked = actions.add_parser(
            "linked-procedures",
            help="list procedures linked to this exact Program revision (and stale links)",
            parents=[common],
        )
        linked.add_argument("record_id")
        linked.set_defaults(handler=handler)
        for name in ("section-add", "target-add"):
            action = actions.add_parser(
                name, help=f"add structured {name.split('-')[0]} data to a draft", parents=[common]
            )
            action.add_argument("record_id")
            action.add_argument("--content", required=True, help="strict JSON object")
            action.add_argument("--expected-updated-at", required=True)
            action.set_defaults(handler=handler)
        target_update = actions.add_parser(
            "target-update",
            help="edit a planned target on a DRAFT; numeric units stay explicit",
            parents=[common],
        )
        target_update.add_argument("record_id")
        target_update.add_argument("target_id")
        target_update.add_argument("--content", required=True)
        target_update.add_argument("--expected-updated-at", required=True)
        target_update.set_defaults(handler=handler)
        target_reorder = actions.add_parser(
            "targets-reorder", help="reorder every target in a DRAFT", parents=[common]
        )
        target_reorder.add_argument("record_id")
        target_reorder.add_argument(
            "--order", required=True, help="JSON array of target IDs in desired order"
        )
        target_reorder.add_argument("--expected-updated-at", required=True)
        target_reorder.set_defaults(handler=handler)
        update = actions.add_parser(
            "section-update", help="edit a structured section on a DRAFT", parents=[common]
        )
        update.add_argument("record_id")
        update.add_argument("section_id")
        update.add_argument("--content", required=True)
        update.add_argument("--expected-updated-at", required=True)
        update.set_defaults(handler=handler)
        reorder = actions.add_parser(
            "sections-reorder", help="reorder all sections in a DRAFT", parents=[common]
        )
        reorder.add_argument("record_id")
        reorder.add_argument(
            "--order", required=True, help="JSON array of section IDs in desired order"
        )
        reorder.add_argument("--expected-updated-at", required=True)
        reorder.set_defaults(handler=handler)
    else:
        singular = "procedure"
        link = actions.add_parser(
            "link-program",
            help="link this DRAFT to one exact Program revision (never inferred)",
            parents=[common],
        )
        link.add_argument("record_id")
        link.add_argument(
            "--program", dest="program_id", required=True, help="exact Program record ID"
        )
        link.add_argument("--expected-updated-at", required=True)
        link.add_argument(
            "--by", required=True, help="declared actor attribution (not authentication)"
        )
        link.add_argument("--note", default="", help="reasoned link note")
        link.set_defaults(handler=handler)
        link_show = actions.add_parser(
            "program-link",
            help="show the exact Program revision link and its staleness status",
            parents=[common],
        )
        link_show.add_argument("record_id")
        link_show.set_defaults(handler=handler)
        action = actions.add_parser(
            "step-add", help="add a structured step to a DRAFT", parents=[common]
        )
        action.add_argument("record_id")
        action.add_argument("--content", required=True, help="strict JSON object")
        action.add_argument("--expected-updated-at", required=True)
        action.set_defaults(handler=handler)
        update = actions.add_parser(
            "step-update", help="edit a structured step on a DRAFT", parents=[common]
        )
        update.add_argument("record_id")
        update.add_argument("step_id")
        update.add_argument("--content", required=True)
        update.add_argument("--expected-updated-at", required=True)
        update.set_defaults(handler=handler)
        reorder = actions.add_parser(
            "steps-reorder", help="reorder all steps in a DRAFT", parents=[common]
        )
        reorder.add_argument("record_id")
        reorder.add_argument(
            "--order", required=True, help="JSON array of step IDs in desired order"
        )
        reorder.add_argument("--expected-updated-at", required=True)
        reorder.set_defaults(handler=handler)
    # Make the singular visible in generated help without adding a second lifecycle vocabulary.
    group.description = (
        f"Draft, edit, validate, review, revise and compare exact {singular} revisions."
    )


def add_program_procedure_commands(sub: Any, common: list[Any]) -> None:
    """Register the two engineering authoring command groups."""
    _add_authoring_group(sub, common, "programs", command_program_procedure)
    _add_authoring_group(sub, common, "procedures", command_program_procedure)


def command_program_procedure(args: Any) -> int:
    """Adapt strict CLI inputs to the shared authoring domain service."""
    from ..cli import app as cli
    from .authoring import EngineeringAuthoringService

    kind = "program" if args.command == "programs" else "procedure"
    action = args.authoring_action
    content: Any = None
    if getattr(args, "content", None):
        content = read_json(args.content)
    order: Any = None
    if getattr(args, "order", None):
        order = read_json(args.order, root_type=list)
        if any(not isinstance(item, str) for item in order):
            raise ValidationError("order file must contain only string record IDs")

    workspace = cli._open_workspace(args)
    payload: Any
    lines: list[str]
    with workspace.database.unit_of_work() as session:
        service = EngineeringAuthoringService(session)
        repo = service.repository
        if action == "create":
            allowed = (
                {
                    "title",
                    "code",
                    "summary",
                    "author",
                    "well_id",
                    "field_id",
                    "project_id",
                    "provenance",
                    "attributes",
                }
                if kind == "program"
                else {
                    "title",
                    "code",
                    "procedure_type",
                    "description",
                    "well_id",
                    "field_id",
                    "project_id",
                    "section_id",
                    "source_reference",
                    "provenance",
                    "attributes",
                    "created_by",
                }
            )
            _reject_unknown(content, allowed, label=kind)
            string_fields = (
                {"title", "code", "summary", "author", "well_id", "field_id", "project_id"}
                if kind == "program"
                else {
                    "title",
                    "code",
                    "procedure_type",
                    "description",
                    "well_id",
                    "field_id",
                    "project_id",
                    "section_id",
                    "source_reference",
                    "created_by",
                }
            )
            for field in string_fields:
                if (
                    field in content
                    and content[field] is not None
                    and not isinstance(content[field], str)
                ):
                    raise ValidationError(f"{kind} field {field!r} must be a string or null")
            if "provenance" in content and (
                not isinstance(content["provenance"], list)
                or any(not isinstance(item, dict) for item in content["provenance"])
            ):
                raise ValidationError(f"{kind} provenance must be an array of objects")
            if "attributes" in content and not isinstance(content["attributes"], dict):
                raise ValidationError(f"{kind} attributes must be a JSON object")
            if not isinstance(content.get("title"), str) or not content["title"].strip():
                raise ValidationError(f"{kind} create JSON requires a non-empty title")
            author_key = "author" if kind == "program" else "created_by"
            if not isinstance(content.get(author_key), str) or not content[author_key].strip():
                raise ValidationError(
                    f"{kind} create JSON requires non-empty {author_key!r} attribution"
                )
            if kind == "program":
                record = repo.create_program(**content, created_by=content.get("author") or "cli")
            else:
                record = repo.create_procedure(**content)
            payload = {
                "ok": True,
                "record_type": kind,
                "id": record.id,
                "revision": record.revision,
                "status": str(record.status),
                "updated_at": service._token(record),
                "actor_strings_are_authenticated": False,
            }
            lines = [f"Created {kind} DRAFT rev {record.revision}: {record.id}"]
        elif action == "list":
            well_id = cli._resolve_well_id(workspace, args.well) if args.well else ""
            if kind == "program":
                rows = repo.list_programs(
                    well_id=str(well_id or ""),
                    status=args.status or "",
                    include_superseded=args.history,
                    limit=args.limit,
                )
            else:
                rows = repo.list_procedures(
                    well_id=str(well_id or ""),
                    status=args.status or "",
                    include_superseded=args.history,
                    limit=args.limit,
                )
            payload = {
                "count": len(rows),
                "records": [
                    {
                        "id": row.id,
                        "code": row.code,
                        "title": row.title,
                        "revision": row.revision,
                        "status": str(row.status),
                        "is_current": row.is_current,
                        "well_id": row.well_id,
                        "updated_at": service._token(row),
                    }
                    for row in rows
                ],
            }
            lines = [f"rev {r.revision} · {r.status} · {r.title} · {r.id}" for r in rows] or [
                f"no {kind}s found"
            ]
        elif action in {"show", "validate"}:
            row = (
                repo.get_program(args.record_id)
                if kind == "program"
                else repo.get_procedure(args.record_id)
            )
            if action == "validate":
                payload = (
                    service.validate_program(row.id)
                    if kind == "program"
                    else service.validate_procedure(row.id)
                )
                lines = [
                    f"ready for review: {str(payload['ready_for_review']).lower()}",
                    "Structural completeness only; engineering adequacy and safety are not assessed.",
                ]
            else:
                payload = (
                    _program_payload(
                        row, repo.list_program_sections(row.id), repo.list_targets(row.id)
                    )
                    if kind == "program"
                    else _procedure_payload(row, repo.list_procedure_steps(row.id))
                )
                from ..database.audit import AuditLog

                payload["review_history"] = [
                    {
                        "at": event.at.isoformat(),
                        "actor": event.actor,
                        "action": event.action,
                        "detail": event.detail or {},
                    }
                    for event in reversed(AuditLog(session).trail(kind, row.id))
                ]
                if kind == "procedure":
                    payload["program_link"] = service.procedure_program_link(row.id)
                lines = [
                    f"{kind.title()} rev {row.revision} · {row.status} · {row.title} · {row.id}",
                    f"updated_at: {service._token(row)}",
                ]
        elif action == "section-add":
            allowed = {
                "sequence",
                "section_key",
                "title",
                "content",
                "completion_state",
                "source_reference",
                "provenance",
                "origin",
            }
            _reject_unknown(content, allowed, label="Program section")
            row = service.add_program_section(
                args.record_id, expected_updated_at=args.expected_updated_at, **content
            )
            payload = {
                "id": row.id,
                "sequence": row.sequence,
                "section_key": row.section_key,
                "title": row.title,
                "program_updated_at": service._token(repo.get_program(args.record_id)),
            }
            lines = [f"Added Program section {row.sequence}: {row.title}"]
        elif action == "target-update":
            allowed = {
                "sequence",
                "name",
                "hole_size_in",
                "casing_program",
                "formation_top",
                "planned_depth_md_value",
                "planned_depth_md_unit",
                "planned_duration_days",
                "planned_mud_weight_value",
                "planned_mud_weight_unit",
                "planned_npt_hours",
                "planned_cost_value",
                "planned_cost_unit",
            }
            _reject_unknown(content, allowed, label="Program target")
            row = service.update_program_target(
                args.record_id,
                args.target_id,
                expected_updated_at=args.expected_updated_at,
                changes=content,
            )
            payload = {
                "id": row.id,
                "sequence": row.sequence,
                "name": row.name,
                "updated_at": service._token(repo.get_program(args.record_id)),
            }
            lines = [f"Updated Program target {row.sequence}: {row.name}"]
        elif action == "targets-reorder":
            rows = service.reorder_program_targets(
                args.record_id, expected_updated_at=args.expected_updated_at, ordered_ids=order
            )
            payload = {
                "ordered_ids": [r.id for r in rows],
                "updated_at": service._token(repo.get_program(args.record_id)),
            }
            lines = ["Program targets reordered."]
        elif action == "section-update":
            allowed = {
                "section_key",
                "title",
                "content",
                "completion_state",
                "source_reference",
                "provenance",
            }
            _reject_unknown(content, allowed, label="Program section")
            row = service.update_program_section(
                args.record_id,
                args.section_id,
                expected_updated_at=args.expected_updated_at,
                changes=content,
            )
            payload = {
                "id": row.id,
                "sequence": row.sequence,
                "title": row.title,
                "program_updated_at": service._token(repo.get_program(args.record_id)),
            }
            lines = [f"Updated Program section: {row.title}"]
        elif action == "sections-reorder":
            rows = service.reorder_program_sections(
                args.record_id, expected_updated_at=args.expected_updated_at, ordered_ids=order
            )
            payload = {
                "ordered_ids": [r.id for r in rows],
                "updated_at": service._token(repo.get_program(args.record_id)),
            }
            lines = ["Program sections reordered."]
        elif action == "target-add":
            allowed = {
                "name",
                "section_id",
                "sequence",
                "hole_size_in",
                "casing_program",
                "formation_top",
                "planned_depth_md_value",
                "planned_depth_md_unit",
                "planned_duration_days",
                "planned_mud_weight_value",
                "planned_mud_weight_unit",
                "planned_npt_hours",
                "planned_cost_value",
                "planned_cost_unit",
                "provenance",
                "origin",
                "attributes",
            }
            _reject_unknown(content, allowed, label="Program target")
            row = service.add_program_target(
                args.record_id, expected_updated_at=args.expected_updated_at, **content
            )
            payload = {
                "id": row.id,
                "sequence": row.sequence,
                "name": row.name,
                "updated_at": service._token(repo.get_program(args.record_id)),
            }
            lines = [f"Added planned target {row.sequence}: {row.name}"]
        elif action == "step-add":
            allowed = {
                "sequence",
                "title",
                "instructions",
                "responsible_role",
                "equipment",
                "preconditions",
                "parameters",
                "expected_result",
                "acceptance_criteria",
                "evidence_required",
                "hold_point",
                "safety_controls",
                "contingency",
                "source_reference",
                "provenance",
            }
            _reject_unknown(content, allowed, label="Procedure step")
            row = service.add_procedure_step(
                args.record_id, expected_updated_at=args.expected_updated_at, **content
            )
            payload = {
                "id": row.id,
                "sequence": row.sequence,
                "title": row.title,
                "updated_at": service._token(repo.get_procedure(args.record_id)),
            }
            lines = [f"Added Procedure step {row.sequence}: {row.title}"]
        elif action == "step-update":
            allowed = {
                "title",
                "instructions",
                "responsible_role",
                "equipment",
                "preconditions",
                "parameters",
                "expected_result",
                "acceptance_criteria",
                "evidence_required",
                "hold_point",
                "safety_controls",
                "contingency",
                "source_reference",
                "provenance",
            }
            _reject_unknown(content, allowed, label="Procedure step")
            row = service.update_procedure_step(
                args.record_id,
                args.step_id,
                expected_updated_at=args.expected_updated_at,
                changes=content,
            )
            payload = {
                "id": row.id,
                "sequence": row.sequence,
                "title": row.title,
                "updated_at": service._token(repo.get_procedure(args.record_id)),
            }
            lines = [f"Updated Procedure step: {row.title}"]
        elif action == "steps-reorder":
            rows = service.reorder_procedure_steps(
                args.record_id, expected_updated_at=args.expected_updated_at, ordered_ids=order
            )
            payload = {
                "ordered_ids": [r.id for r in rows],
                "updated_at": service._token(repo.get_procedure(args.record_id)),
            }
            lines = ["Procedure steps reordered."]
        elif action == "update":
            allowed = (
                {"title", "summary"}
                if kind == "program"
                else {"title", "procedure_type", "description", "source_reference"}
            )
            _reject_unknown(content, allowed, label=f"{kind} content")
            row = service.update_record_content(
                kind, args.record_id, expected_updated_at=args.expected_updated_at, changes=content
            )
            payload = {"id": row.id, "title": row.title, "updated_at": service._token(row)}
            lines = [f"Updated {kind} draft content."]
        elif action == "revise":
            if content is None:
                content = {}
            row = (
                repo.get_program(args.record_id)
                if kind == "program"
                else repo.get_procedure(args.record_id)
            )
            service._require_fresh(row, args.expected_updated_at)
            if content is not None:
                allowed = set(PROGRAM_FIELDS if kind == "program" else PROCEDURE_FIELDS)
                _reject_unknown(content, allowed, label=kind)
            if kind == "program":
                content.setdefault("author", args.by)
            revised = (
                repo.revise_program(
                    row.id, by=args.by, changes=content or {}, revision_label=args.revision_label
                )
                if kind == "program"
                else repo.revise_procedure(
                    row.id, by=args.by, changes=content or {}, revision_label=args.revision_label
                )
            )
            payload = {
                "id": revised.id,
                "supersedes_id": revised.supersedes_id,
                "revision": revised.revision,
                "status": str(revised.status),
                "updated_at": service._token(revised),
            }
            lines = [f"Created {kind} rev {revised.revision} as DRAFT; prior revision preserved."]
        elif action == "compare":
            payload = (
                service.compare_programs(args.left_id, args.right_id)
                if kind == "program"
                else service.compare_procedures(args.left_id, args.right_id)
            )
            lines = [
                f"Read-only {kind} revision comparison; engineering significance is not assessed."
            ]
        elif action == "link-program":
            changed = service.link_procedure_to_program(
                args.record_id,
                args.program_id,
                expected_updated_at=args.expected_updated_at,
                by=args.by,
                note=getattr(args, "note", "") or "",
            )
            payload = {
                "id": changed.id,
                "program_revision_id": changed.program_revision_id,
                "program_link_status": changed.program_link_status,
                "updated_at": service._token(changed),
                "actor_strings_are_authenticated": False,
            }
            lines = [
                "Linked to one exact Program revision; no automatic relink on later Program revisions."
            ]
        elif action == "program-link":
            payload = service.procedure_program_link(args.record_id)
            lines = [f"Program link status: {payload['link_status']}"]
        elif action == "linked-procedures":
            payload = service.program_linked_procedures(args.record_id)
            lines = [
                f"{len(payload['linked_procedures'])} linked, {len(payload['stale_links'])} stale."
            ]
        elif action in {"submit", "review", "approve", "reject"}:
            row = (
                repo.get_program(args.record_id)
                if kind == "program"
                else repo.get_procedure(args.record_id)
            )
            if action == "submit":
                changed = service.submit(
                    kind, row.id, expected_updated_at=args.expected_updated_at, by=args.by
                )
            elif action == "review":
                changed = service.record_review(
                    kind,
                    row.id,
                    expected_updated_at=args.expected_updated_at,
                    by=args.by,
                    note=args.note,
                )
            else:
                changed = service.decide(
                    kind,
                    row.id,
                    expected_updated_at=args.expected_updated_at,
                    by=args.by,
                    approved=action == "approve",
                    reason=args.note,
                )
            payload = {
                "id": changed.id,
                "revision": changed.revision,
                "status": str(changed.status),
                "reviewer": changed.reviewer,
                "approved_by": getattr(changed, "approver", None)
                or getattr(changed, "approved_by", None),
                "updated_at": service._token(changed),
                "actor_strings_are_authenticated": False,
            }
            lines = [
                f"{kind.title()} rev {changed.revision} status: {changed.status}; actor attribution is not authentication."
            ]
        else:
            raise ValidationError(f"unsupported {kind} authoring action: {action}")

    cli._emit(payload, as_json=bool(args.json), lines=lines)
    return 0
