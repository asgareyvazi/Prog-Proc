"""Service-level authoring workflows for Programs and Procedures.

These methods are a deliberately conservative public authoring boundary over the existing
revisioned engineering records. They provide structured section/step persistence and lifecycle
preconditions; they do not assess engineering adequacy, authenticate the actor strings, or enable
unapproved calculations.
"""

from __future__ import annotations

import math
from datetime import UTC, datetime
from typing import Any

from sqlalchemy.orm import Session

from ..core.enums import ProcedureLifecycle, ProgramLifecycle
from ..core.errors import ValidationError
from ..database.audit import AuditLog
from ..database.models import ProcedureStep, ProgramSection, ProgramTarget
from .repository import EngineeringRepository


class EngineeringAuthoringService:
    """Single domain service for source-entered Program and Procedure drafts."""

    def __init__(self, session: Session) -> None:
        self.session = session
        self.repository = EngineeringRepository(session)

    @staticmethod
    def _token(row: Any) -> str:
        stamp = row.updated_at
        if isinstance(stamp, datetime):
            normalized = (
                stamp.replace(tzinfo=UTC) if stamp.tzinfo is None else stamp.astimezone(UTC)
            )
            return normalized.isoformat()
        return str(stamp)

    def _require_fresh(self, row: Any, expected_updated_at: str) -> None:
        if not expected_updated_at:
            raise ValidationError("a mutation requires --expected-updated-at from the latest show")
        actual = self._token(row)
        if actual != str(expected_updated_at):
            raise ValidationError(
                "stale engineering revision; reload it before editing",
                expected_updated_at=expected_updated_at,
                actual_updated_at=actual,
            )

    @staticmethod
    def _append_status_note(row: Any, label: str, note: str) -> None:
        prior = str(getattr(row, "status_note", "") or "").strip()
        addition = f"{label}: {note.strip()}"
        row.status_note = f"{prior}\n{addition}" if prior else addition

    @staticmethod
    def _require_sequence(fields: dict[str, Any], label: str) -> None:
        sequence = fields.get("sequence")
        if isinstance(sequence, bool) or not isinstance(sequence, int) or sequence < 1:
            raise ValidationError(f"{label} sequence must be a positive JSON integer")

    @staticmethod
    def _require_text_fields(
        fields: dict[str, Any], required: set[str], optional: set[str], label: str
    ) -> None:
        for key in required:
            if not isinstance(fields.get(key), str) or not fields[key].strip():
                raise ValidationError(f"{label} requires non-empty string field {key!r}")
        for key in optional:
            if key in fields and fields[key] is not None and not isinstance(fields[key], str):
                raise ValidationError(f"{label} field {key!r} must be a string or null")

    @staticmethod
    def _require_parameters(value: Any, label: str) -> None:
        if not isinstance(value, list) or any(not isinstance(item, dict) for item in value):
            raise ValidationError(f"{label} must be an array of objects")
        allowed = {"name", "value", "unit", "source_reference", "provenance"}
        for parameter in value:
            if set(parameter) - allowed:
                raise ValidationError(f"{label} entry has unknown keys")
            if (
                not all(
                    isinstance(parameter.get(key), str) and parameter[key].strip()
                    for key in ("name", "unit")
                )
                or "value" not in parameter
            ):
                raise ValidationError(
                    f"each {label} entry requires an explicit name, value, and unit"
                )
            if (
                "source_reference" in parameter
                and parameter["source_reference"] is not None
                and not isinstance(parameter["source_reference"], str)
            ):
                raise ValidationError(f"{label} source_reference must be a string or null")
            if "provenance" in parameter and (
                not isinstance(parameter["provenance"], list)
                or any(not isinstance(item, dict) for item in parameter["provenance"])
            ):
                raise ValidationError(f"{label} provenance must be an array of objects")

    @staticmethod
    def _require_draft(row: Any, kind: str) -> None:
        if not row.is_current or str(row.status) != "DRAFT":
            raise ValidationError(f"{kind} authoring is allowed only on the current DRAFT revision")

    def update_record_content(
        self, kind: str, record_id: str, *, expected_updated_at: str, changes: dict[str, Any]
    ) -> Any:
        record_kind = str(kind).strip().lower()
        if record_kind == "program":
            row = self.repository.get_program(record_id)
            allowed = {"title", "summary"}
        elif record_kind == "procedure":
            row = self.repository.get_procedure(record_id)
            allowed = {"title", "procedure_type", "description", "source_reference"}
        else:
            raise ValidationError("record_type must be 'program' or 'procedure'")
        self._require_fresh(row, expected_updated_at)
        self._require_draft(row, record_kind.title())
        unknown = sorted(set(changes) - allowed)
        if unknown:
            raise ValidationError(
                "unknown record content field(s)", fields=unknown, allowed=sorted(allowed)
            )
        for key, value in changes.items():
            if not isinstance(value, str):
                raise ValidationError(f"record content field {key!r} must be a string")
            if key in {"title", "procedure_type"} and not value.strip():
                raise ValidationError(f"record content field {key!r} cannot be empty")
            setattr(row, key, value.strip() if key in {"title", "procedure_type"} else value)
        row.updated_at = datetime.now(UTC)
        self.session.flush()
        return row

    def add_program_section(
        self, program_id: str, *, expected_updated_at: str, **fields: Any
    ) -> ProgramSection:
        program = self.repository.get_program(program_id)
        self._require_fresh(program, expected_updated_at)
        self._require_draft(program, "Program")
        if "sequence" not in fields:
            existing = self.repository.list_program_sections(program_id)
            fields["sequence"] = max((item.sequence for item in existing), default=0) + 1
        self._require_sequence(fields, "Program section")
        self._require_text_fields(
            fields,
            {"section_key", "title"},
            {"content", "completion_state", "source_reference", "origin"},
            "Program section",
        )
        if "section_key" in fields:
            from ..core.vocabulary import snake_token

            fields["section_key"] = snake_token(fields["section_key"])
            if not fields["section_key"]:
                raise ValidationError("section_key must be a non-empty stable token")
        if "provenance" in fields and (
            not isinstance(fields["provenance"], list)
            or any(not isinstance(item, dict) for item in fields["provenance"])
        ):
            raise ValidationError("Program section provenance must be an array of objects")
        if "completion_state" in fields:
            allowed_states = {
                "MISSING",
                "DRAFT",
                "SOURCE_PROVIDED",
                "EXTRACTED_UNVERIFIED",
                "NOT_APPLICABLE",
                "OWNER_DECISION_REQUIRED",
            }
            state = str(fields["completion_state"] or "").upper()
            if state not in allowed_states:
                raise ValidationError(
                    "invalid Program section completion_state", allowed=sorted(allowed_states)
                )
            fields["completion_state"] = state
        if (
            str(fields.get("completion_state", "DRAFT")).upper()
            in {
                "NOT_APPLICABLE",
                "SOURCE_PROVIDED",
                "EXTRACTED_UNVERIFIED",
            }
            and not str(fields.get("content") or "").strip()
        ):
            raise ValidationError(
                "this Program section state requires supporting content or a reason"
            )
        section = self.repository.add_program_section(program_id, **fields)
        program.updated_at = datetime.now(UTC)
        self.session.flush()
        return section

    def add_program_target(
        self, program_id: str, *, expected_updated_at: str, **fields: Any
    ) -> Any:
        program = self.repository.get_program(program_id)
        self._require_fresh(program, expected_updated_at)
        self._require_draft(program, "Program")
        targets = self.repository.list_targets(program_id)
        fields.setdefault("sequence", max((item.sequence for item in targets), default=0) + 1)
        self._require_sequence(fields, "Program target")
        self._require_text_fields(
            fields,
            set(),
            {
                "name",
                "casing_program",
                "formation_top",
                "planned_depth_md_unit",
                "planned_mud_weight_unit",
                "planned_cost_unit",
                "origin",
            },
            "Program target",
        )
        for collection in ("provenance",):
            if collection in fields and (
                not isinstance(fields[collection], list)
                or any(not isinstance(item, dict) for item in fields[collection])
            ):
                raise ValidationError(f"Program target {collection} must be an array of objects")
        if "attributes" in fields and not isinstance(fields["attributes"], dict):
            raise ValidationError("Program target attributes must be a JSON object")
        for value_key, unit_key in (
            ("planned_depth_md_value", "planned_depth_md_unit"),
            ("planned_mud_weight_value", "planned_mud_weight_unit"),
            ("planned_cost_value", "planned_cost_unit"),
        ):
            if fields.get(value_key) is not None and not str(fields.get(unit_key) or "").strip():
                raise ValidationError(
                    f"{unit_key} must be explicitly supplied with {value_key}; no unit is inferred"
                )
            if fields.get(value_key) is None:
                fields.setdefault(unit_key, "")
        for name in (
            "hole_size_in",
            "planned_depth_md_value",
            "planned_duration_days",
            "planned_mud_weight_value",
            "planned_npt_hours",
            "planned_cost_value",
        ):
            value = fields.get(name)
            if value is not None and (
                isinstance(value, bool)
                or not isinstance(value, int | float)
                or not math.isfinite(float(value))
            ):
                raise ValidationError(f"{name} must be a finite explicit JSON number")
            if value is not None and float(value) < 0:
                raise ValidationError(f"{name} cannot be negative")
        if fields.get("hole_size_in") is not None and float(fields["hole_size_in"]) == 0:
            raise ValidationError("hole_size_in must be greater than zero")
        target = self.repository.add_target(program_id, **fields)
        program.updated_at = datetime.now(UTC)
        self.session.flush()
        return target

    def update_program_target(
        self, program_id: str, target_id: str, *, expected_updated_at: str, changes: dict[str, Any]
    ) -> Any:
        program = self.repository.get_program(program_id)
        self._require_fresh(program, expected_updated_at)
        self._require_draft(program, "Program")
        target = self.session.get(ProgramTarget, str(target_id))
        if target is None or target.program_id != program.id:
            raise ValidationError("Program target does not belong to this Program revision")
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
        unknown = sorted(set(changes) - allowed)
        if unknown:
            raise ValidationError(
                "unknown Program target field(s)", fields=unknown, allowed=sorted(allowed)
            )
        self._require_text_fields(
            changes,
            set(),
            {
                "name",
                "casing_program",
                "formation_top",
                "planned_depth_md_unit",
                "planned_mud_weight_unit",
                "planned_cost_unit",
            },
            "Program target",
        )
        if "name" in changes and changes["name"] is None:
            raise ValidationError("Program target name cannot be null")
        if "sequence" in changes:
            self._require_sequence(changes, "Program target")
        for value_key, unit_key in (
            ("planned_depth_md_value", "planned_depth_md_unit"),
            ("planned_mud_weight_value", "planned_mud_weight_unit"),
            ("planned_cost_value", "planned_cost_unit"),
        ):
            effective_value = changes.get(value_key, getattr(target, value_key))
            if value_key in changes and changes[value_key] is None:
                changes.setdefault(unit_key, "")
            elif effective_value is not None:
                unit = changes.get(unit_key, getattr(target, unit_key))
                if value_key in changes and unit_key not in changes:
                    raise ValidationError(
                        f"{unit_key} must be explicitly supplied when {value_key} is set"
                    )
                if not isinstance(unit, str) or not unit.strip():
                    raise ValidationError(
                        f"{unit_key} must be explicit whenever {value_key} is set"
                    )
        for name in (
            "hole_size_in",
            "planned_depth_md_value",
            "planned_duration_days",
            "planned_mud_weight_value",
            "planned_npt_hours",
            "planned_cost_value",
        ):
            value = changes.get(name)
            if value is not None and (
                isinstance(value, bool)
                or not isinstance(value, int | float)
                or not math.isfinite(float(value))
                or float(value) < 0
            ):
                raise ValidationError(f"{name} must be a finite, non-negative JSON number")
        if changes.get("hole_size_in", target.hole_size_in) == 0:
            raise ValidationError("hole_size_in must be greater than zero")
        updated, _ = self.repository.update_target(target.id, **changes)
        program.updated_at = datetime.now(UTC)
        self.session.flush()
        return updated

    def reorder_program_targets(
        self, program_id: str, *, expected_updated_at: str, ordered_ids: list[str]
    ) -> list[Any]:
        program = self.repository.get_program(program_id)
        self._require_fresh(program, expected_updated_at)
        self._require_draft(program, "Program")
        rows = self.repository.list_targets(program_id)
        if set(ordered_ids) != {row.id for row in rows} or len(ordered_ids) != len(rows):
            raise ValidationError("ordered_ids must list every target exactly once")
        offset = len(rows) + 100
        for row in rows:
            row.sequence += offset
        self.session.flush()
        by_id = {row.id: row for row in rows}
        for sequence, row_id in enumerate(ordered_ids, 1):
            by_id[row_id].sequence = sequence
        program.updated_at = datetime.now(UTC)
        self.session.flush()
        return self.repository.list_targets(program_id)

    def update_program_section(
        self, program_id: str, section_id: str, *, expected_updated_at: str, changes: dict[str, Any]
    ) -> ProgramSection:
        program = self.repository.get_program(program_id)
        self._require_fresh(program, expected_updated_at)
        self._require_draft(program, "Program")
        row = self.session.get(ProgramSection, str(section_id))
        if row is None or row.program_id != program.id:
            raise ValidationError("Program section does not belong to this Program revision")
        allowed = {
            "section_key",
            "title",
            "content",
            "completion_state",
            "source_reference",
            "provenance",
        }
        unknown = sorted(set(changes) - allowed)
        if unknown:
            raise ValidationError(
                "unknown Program section field(s)", fields=unknown, allowed=sorted(allowed)
            )
        self._require_text_fields(
            changes,
            set(),
            {"section_key", "title", "content", "source_reference"},
            "Program section",
        )
        if "section_key" in changes:
            from ..core.vocabulary import snake_token

            if not isinstance(changes["section_key"], str):
                raise ValidationError("section_key must be a non-empty stable token")
            changes["section_key"] = snake_token(changes["section_key"])
            if not changes["section_key"]:
                raise ValidationError("section_key must be a non-empty stable token")
        if "title" in changes and not str(changes["title"] or "").strip():
            raise ValidationError("Program section title cannot be empty")
        if "content" in changes and changes["content"] is None:
            raise ValidationError(
                "Program section content must be a string; use an empty string to clear it"
            )
        if "completion_state" in changes:
            allowed_states = {
                "MISSING",
                "DRAFT",
                "SOURCE_PROVIDED",
                "EXTRACTED_UNVERIFIED",
                "NOT_APPLICABLE",
                "OWNER_DECISION_REQUIRED",
            }
            state = str(changes["completion_state"] or "").upper()
            if state not in allowed_states:
                raise ValidationError(
                    "section review/approval state cannot be self-assigned",
                    allowed=sorted(allowed_states),
                )
            changes["completion_state"] = state
        if (
            changes.get("completion_state") == "NOT_APPLICABLE"
            and not str(changes.get("content", row.content) or "").strip()
        ):
            raise ValidationError("NOT_APPLICABLE requires a recorded reason in section content")
        for key, value in changes.items():
            if (
                key in {"title", "content", "source_reference"}
                and value is not None
                and not isinstance(value, str)
            ):
                raise ValidationError(f"Program section field {key!r} must be a string")
            if key == "provenance" and (
                not isinstance(value, list) or any(not isinstance(item, dict) for item in value)
            ):
                raise ValidationError("provenance must be an array of objects")
            setattr(row, key, value)
        if not row.title.strip():
            raise ValidationError("Program section title cannot be empty")
        program.updated_at = datetime.now(UTC)
        self.session.flush()
        return row

    def reorder_program_sections(
        self, program_id: str, *, expected_updated_at: str, ordered_ids: list[str]
    ) -> list[ProgramSection]:
        program = self.repository.get_program(program_id)
        self._require_fresh(program, expected_updated_at)
        self._require_draft(program, "Program")
        rows = self.repository.list_program_sections(program_id)
        if set(ordered_ids) != {row.id for row in rows} or len(ordered_ids) != len(rows):
            raise ValidationError("ordered_ids must list every section exactly once")
        offset = len(rows) + 100
        for row in rows:
            row.sequence += offset
        self.session.flush()
        by_id = {row.id: row for row in rows}
        for sequence, row_id in enumerate(ordered_ids, 1):
            by_id[row_id].sequence = sequence
        program.updated_at = datetime.now(UTC)
        self.session.flush()
        return self.repository.list_program_sections(program_id)

    def add_procedure_step(
        self, procedure_id: str, *, expected_updated_at: str, **fields: Any
    ) -> ProcedureStep:
        procedure = self.repository.get_procedure(procedure_id)
        self._require_fresh(procedure, expected_updated_at)
        self._require_draft(procedure, "Procedure")
        if "sequence" not in fields:
            existing = self.repository.list_procedure_steps(procedure_id)
            fields["sequence"] = max((item.sequence for item in existing), default=0) + 1
        self._require_sequence(fields, "Procedure step")
        self._require_text_fields(
            fields,
            {"title", "instructions"},
            {
                "responsible_role",
                "equipment",
                "preconditions",
                "expected_result",
                "acceptance_criteria",
                "evidence_required",
                "safety_controls",
                "contingency",
                "source_reference",
            },
            "Procedure step",
        )
        if "hold_point" in fields and not isinstance(fields["hold_point"], bool):
            raise ValidationError("hold_point must be a JSON boolean")
        for collection in ("parameters", "provenance"):
            if collection in fields and (
                not isinstance(fields[collection], list)
                or any(not isinstance(item, dict) for item in fields[collection])
            ):
                raise ValidationError(f"Procedure step {collection} must be an array of objects")
        if "parameters" in fields:
            self._require_parameters(fields["parameters"], "Procedure step parameters")
        step = self.repository.add_procedure_step(procedure_id, **fields)
        procedure.updated_at = datetime.now(UTC)
        self.session.flush()
        return step

    def update_procedure_step(
        self, procedure_id: str, step_id: str, *, expected_updated_at: str, changes: dict[str, Any]
    ) -> ProcedureStep:
        procedure = self.repository.get_procedure(procedure_id)
        self._require_fresh(procedure, expected_updated_at)
        self._require_draft(procedure, "Procedure")
        row = self.session.get(ProcedureStep, str(step_id))
        if row is None or row.procedure_id != procedure.id:
            raise ValidationError("Procedure step does not belong to this Procedure revision")
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
        unknown = sorted(set(changes) - allowed)
        if unknown:
            raise ValidationError(
                "unknown Procedure step field(s)", fields=unknown, allowed=sorted(allowed)
            )
        for key, value in changes.items():
            if key == "parameters":
                self._require_parameters(value, "Procedure step parameters")
            elif key == "provenance":
                if not isinstance(value, list) or any(not isinstance(item, dict) for item in value):
                    raise ValidationError("provenance must be an array of objects")
            elif key == "hold_point":
                if not isinstance(value, bool):
                    raise ValidationError("hold_point must be a JSON boolean")
            elif value is not None and not isinstance(value, str):
                raise ValidationError(f"Procedure step field {key!r} must be a string or null")
            if key in {"title", "instructions"} and (
                not isinstance(value, str) or not value.strip()
            ):
                raise ValidationError(f"Procedure step {key} must be a non-empty string")
            setattr(row, key, value)
        procedure.updated_at = datetime.now(UTC)
        self.session.flush()
        return row

    def reorder_procedure_steps(
        self, procedure_id: str, *, expected_updated_at: str, ordered_ids: list[str]
    ) -> list[ProcedureStep]:
        procedure = self.repository.get_procedure(procedure_id)
        self._require_fresh(procedure, expected_updated_at)
        self._require_draft(procedure, "Procedure")
        rows = self.repository.list_procedure_steps(procedure_id)
        if set(ordered_ids) != {row.id for row in rows} or len(ordered_ids) != len(rows):
            raise ValidationError("ordered_ids must list every step exactly once")
        offset = len(rows) + 100
        for row in rows:
            row.sequence += offset
        self.session.flush()
        by_id = {row.id: row for row in rows}
        for sequence, row_id in enumerate(ordered_ids, 1):
            by_id[row_id].sequence = sequence
        procedure.updated_at = datetime.now(UTC)
        self.session.flush()
        return self.repository.list_procedure_steps(procedure_id)

    def validate_program(self, program_id: str) -> dict[str, Any]:
        program = self.repository.get_program(program_id)
        sections = self.repository.list_program_sections(program_id)
        targets = self.repository.list_targets(program_id)
        missing: list[str] = []
        if not program.title.strip():
            missing.append("title")
        if not (program.well_id or program.field_id or program.project_id):
            missing.append("scope")
        if not program.author:
            missing.append("author")
        if not sections:
            missing.append("structured_sections")
        if not targets:
            missing.append("planned_targets")
        unresolved = [
            f"section:{section.section_key}"
            for section in sections
            if section.completion_state
            in {"MISSING", "EXTRACTED_UNVERIFIED", "OWNER_DECISION_REQUIRED"}
        ]
        return {
            "record_type": "drilling_program",
            "record_id": program.id,
            "revision": program.revision,
            "status": str(program.status),
            "missing": missing,
            "unresolved_sections": unresolved,
            "ready_for_review": not missing and not unresolved,
            "engineering_adequacy_assessed": False,
            "approval_is_not_implied": True,
        }

    def validate_procedure(self, procedure_id: str) -> dict[str, Any]:
        procedure = self.repository.get_procedure(procedure_id)
        steps = self.repository.list_procedure_steps(procedure_id)
        missing: list[str] = []
        if not procedure.title.strip():
            missing.append("title")
        if not procedure.description.strip():
            missing.append("purpose_and_scope")
        if not (procedure.well_id or procedure.field_id or procedure.project_id):
            missing.append("scope")
        if not steps:
            missing.append("structured_steps")
        incomplete_steps = []
        for step in steps:
            gaps = [
                name
                for name, value in (
                    ("instructions", step.instructions),
                    ("responsible_role", step.responsible_role),
                    ("acceptance_criteria", step.acceptance_criteria),
                )
                if not str(value or "").strip()
            ]
            if gaps:
                incomplete_steps.append({"sequence": step.sequence, "missing": gaps})
        return {
            "record_type": "procedure",
            "record_id": procedure.id,
            "revision": procedure.revision,
            "status": str(procedure.status),
            "missing": missing,
            "incomplete_steps": incomplete_steps,
            "ready_for_review": not missing and not incomplete_steps,
            "engineering_adequacy_assessed": False,
            "approval_is_not_implied": True,
        }

    def submit(self, record_type: str, record_id: str, *, expected_updated_at: str, by: str) -> Any:
        kind = str(record_type).strip().lower()
        if kind == "program":
            row = self.repository.get_program(record_id)
            self._require_fresh(row, expected_updated_at)
            result = self.validate_program(record_id)
            if not result["ready_for_review"]:
                raise ValidationError(
                    "Program is incomplete for review submission", checklist=result
                )
            if not by or by != row.author:
                raise ValidationError("Program submission must be recorded by its declared author")
            return self.repository.set_program_status(
                record_id,
                ProgramLifecycle.IN_REVIEW,
                by=by,
                reason="submitted for technical review",
            )
        if kind == "procedure":
            row = self.repository.get_procedure(record_id)
            self._require_fresh(row, expected_updated_at)
            result = self.validate_procedure(record_id)
            if not result["ready_for_review"]:
                raise ValidationError(
                    "Procedure is incomplete for review submission", checklist=result
                )
            if not by or by != row.created_by:
                raise ValidationError(
                    "Procedure submission must be recorded by its declared author"
                )
            return self.repository.set_procedure_status(
                record_id,
                ProcedureLifecycle.IN_REVIEW,
                by=by,
                reason="submitted for technical review",
            )
        raise ValidationError("record_type must be 'program' or 'procedure'")

    def record_review(
        self, record_type: str, record_id: str, *, expected_updated_at: str, by: str, note: str
    ) -> Any:
        if not str(by or "").strip() or not str(note or "").strip():
            raise ValidationError(
                "a technical review requires a reviewer attribution and a reasoned note"
            )
        kind = str(record_type).strip().lower()
        if kind == "program":
            row = self.repository.get_program(record_id)
            self._require_fresh(row, expected_updated_at)
            if str(row.status) != str(ProgramLifecycle.IN_REVIEW):
                raise ValidationError("a Program can be reviewed only while IN_REVIEW")
            if row.author and by == row.author:
                raise ValidationError("the declared Program author cannot also be its reviewer")
            row.reviewer = by
            self._append_status_note(row, "Technical review", note)
        elif kind == "procedure":
            row = self.repository.get_procedure(record_id)
            self._require_fresh(row, expected_updated_at)
            if str(row.status) != str(ProcedureLifecycle.IN_REVIEW):
                raise ValidationError("a Procedure can be reviewed only while IN_REVIEW")
            if by == row.created_by:
                raise ValidationError("the declared Procedure author cannot also be its reviewer")
            row.reviewer = by
            row.reviewed_at = datetime.now(UTC)
            self._append_status_note(row, "Technical review", note)
        else:
            raise ValidationError("record_type must be 'program' or 'procedure'")
        AuditLog(self.session).record(
            action="engineering.technical_review.recorded",
            subject_type=kind,
            subject_id=str(row.id),
            actor=by,
            detail={
                "revision": int(row.revision),
                "note": note,
                "status_at_review": str(row.status),
                "actor_string_is_authenticated": False,
            },
        )
        self.session.flush()
        return row

    def decide(
        self,
        record_type: str,
        record_id: str,
        *,
        expected_updated_at: str,
        by: str,
        approved: bool,
        reason: str,
    ) -> Any:
        kind = str(record_type).strip().lower()
        if kind == "program":
            row = self.repository.get_program(record_id)
            self._require_fresh(row, expected_updated_at)
            if str(row.status) != str(ProgramLifecycle.IN_REVIEW) or not row.reviewer:
                raise ValidationError(
                    "a Program requires recorded technical review before a decision"
                )
            if by != row.reviewer or by == row.author:
                raise ValidationError(
                    "decision must match the recorded non-author reviewer attribution"
                )
            if not reason.strip():
                raise ValidationError("a review decision requires a reason")
            if approved:
                validation = self.validate_program(record_id)
                if not validation["ready_for_review"]:
                    raise ValidationError(
                        "Program no longer meets its review checklist", checklist=validation
                    )
                return self.repository.approve_program(record_id, by=by, note=reason)
            self._append_status_note(row, "Review decision (returned to draft)", reason)
            changed = self.repository.set_program_status(
                record_id, ProgramLifecycle.DRAFT, by=by, reason=reason
            )
            changed.reviewer = None
            self.session.flush()
            return changed
        if kind == "procedure":
            row = self.repository.get_procedure(record_id)
            self._require_fresh(row, expected_updated_at)
            if str(row.status) != str(ProcedureLifecycle.IN_REVIEW) or not row.reviewer:
                raise ValidationError(
                    "a Procedure requires recorded technical review before a decision"
                )
            if by != row.reviewer or by == row.created_by:
                raise ValidationError(
                    "decision must match the recorded non-author reviewer attribution"
                )
            if not reason.strip():
                raise ValidationError("a review decision requires a reason")
            if approved:
                validation = self.validate_procedure(record_id)
                if not validation["ready_for_review"]:
                    raise ValidationError(
                        "Procedure no longer meets its review checklist", checklist=validation
                    )
                return self.repository.approve_procedure(record_id, by=by, note=reason)
            self._append_status_note(row, "Review decision (returned to draft)", reason)
            changed = self.repository.set_procedure_status(
                record_id, ProcedureLifecycle.DRAFT, by=by, reason=reason
            )
            changed.reviewer = None
            changed.reviewed_at = None
            self.session.flush()
            return changed
        raise ValidationError("record_type must be 'program' or 'procedure'")

    def compare_programs(self, left_id: str, right_id: str) -> dict[str, Any]:
        left, right = self.repository.get_program(left_id), self.repository.get_program(right_id)
        if not left.code or left.code != right.code or left.well_id != right.well_id:
            raise ValidationError(
                "only revisions sharing a non-empty Program code and well may be compared"
            )
        left_sections = {r.sequence: r for r in self.repository.list_program_sections(left_id)}
        right_sections = {r.sequence: r for r in self.repository.list_program_sections(right_id)}
        left_targets = {r.sequence: r for r in self.repository.list_targets(left_id)}
        right_targets = {r.sequence: r for r in self.repository.list_targets(right_id)}
        section_changes = []
        for sequence in sorted(set(left_sections) | set(right_sections)):
            a, b = left_sections.get(sequence), right_sections.get(sequence)
            if (
                a is None
                or b is None
                or (a.title, a.content, a.completion_state)
                != (b.title, b.content, b.completion_state)
            ):
                section_changes.append(
                    {
                        "sequence": sequence,
                        "before": _section_payload(a),
                        "after": _section_payload(b),
                    }
                )
        target_changes = []
        for sequence in sorted(set(left_targets) | set(right_targets)):
            a, b = left_targets.get(sequence), right_targets.get(sequence)
            if a is None or b is None or _target_values(a) != _target_values(b):
                target_changes.append(
                    {"sequence": sequence, "before": _target_values(a), "after": _target_values(b)}
                )
        return {
            "record_type": "drilling_program_revision_comparison",
            "left": {"id": left.id, "revision": left.revision, "status": str(left.status)},
            "right": {"id": right.id, "revision": right.revision, "status": str(right.status)},
            "section_changes": section_changes,
            "target_changes": target_changes,
            "engineering_significance_assessed": False,
        }

    def compare_procedures(self, left_id: str, right_id: str) -> dict[str, Any]:
        left, right = (
            self.repository.get_procedure(left_id),
            self.repository.get_procedure(right_id),
        )
        if not left.code or left.code != right.code:
            raise ValidationError(
                "only revisions sharing a non-empty Procedure code may be compared"
            )
        a_steps = {r.sequence: r for r in self.repository.list_procedure_steps(left_id)}
        b_steps = {r.sequence: r for r in self.repository.list_procedure_steps(right_id)}
        changes = []
        for sequence in sorted(set(a_steps) | set(b_steps)):
            a, b = a_steps.get(sequence), b_steps.get(sequence)
            if a is None or b is None or _step_payload(a) != _step_payload(b):
                changes.append(
                    {"sequence": sequence, "before": _step_payload(a), "after": _step_payload(b)}
                )
        return {
            "record_type": "procedure_revision_comparison",
            "left": {"id": left.id, "revision": left.revision, "status": str(left.status)},
            "right": {"id": right.id, "revision": right.revision, "status": str(right.status)},
            "step_changes": changes,
            "engineering_significance_assessed": False,
        }


def _section_payload(row: ProgramSection | None) -> dict[str, Any] | None:
    if row is None:
        return None
    return {
        "id": row.id,
        "section_key": row.section_key,
        "title": row.title,
        "content": row.content,
        "completion_state": row.completion_state,
        "source_reference": row.source_reference,
        "provenance": row.provenance or [],
    }


def _target_values(row: Any) -> dict[str, Any] | None:
    if row is None:
        return None
    names = (
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
    )
    return {name: getattr(row, name) for name in names}


def _step_payload(row: ProcedureStep | None) -> dict[str, Any] | None:
    if row is None:
        return None
    names = (
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
    )
    return {name: getattr(row, name) for name in names}
