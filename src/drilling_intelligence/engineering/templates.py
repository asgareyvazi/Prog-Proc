"""Controlled authoring templates for Programs and Procedures.

A template only supplies *structure*: the ordered list of sections (Program) or the step
skeleton (Procedure) with empty content and an explicit ``OWNER_DECISION_REQUIRED`` /
``MISSING`` completion state. It never supplies engineering values, units, depths, mud
weights, durations, costs or safety wording - those must come from a controlled source or an
owner decision, entered per section/step by the author. Creating from a template therefore
produces a DRAFT with no inferred engineering content.
"""

from __future__ import annotations

from typing import Any

from ..core.errors import ValidationError

#: Explicit placeholder written into template steps. It is structure, not content: the author
#: must replace it with controlled source-backed text before submission, and validation still
#: reports the step incomplete until a real responsible role and acceptance criteria exist.
STEP_PLACEHOLDER = "PLACEHOLDER - enter controlled source-backed instruction text before submission"

PROGRAM_TEMPLATES: dict[str, dict[str, Any]] = {
    "standard_drilling_program": {
        "description": (
            "Standard vertical-well drilling program skeleton. Structure only; every "
            "engineering value must be entered from a controlled source."
        ),
        "sections": [
            ("cover", "Cover and document control"),
            ("design_basis", "Design basis and references"),
            ("well_profile", "Well profile and prognosis"),
            ("hole_and_casing", "Hole and casing program"),
            ("mud_program", "Mud program"),
            ("bit_and_bha", "Bit and BHA selection"),
            ("survey_program", "Survey program"),
            ("cementing", "Cementing program"),
            ("well_control", "Well control and HSE"),
            ("risks", "Risk register summary"),
            ("time_and_cost", "Time and cost estimate"),
            ("logistics", "Logistics and contingencies"),
        ],
    },
}

PROCEDURE_TEMPLATES: dict[str, dict[str, Any]] = {
    "standard_operational_procedure": {
        "description": (
            "Standard operational procedure skeleton: scope, roles, preconditions, ordered "
            "steps with hold points, acceptance criteria, contingency and records. Instruction "
            "and safety text must come from a controlled source."
        ),
        "steps": [
            {"title": "Scope and applicability", "hold_point": False},
            {"title": "Roles and responsibilities", "hold_point": False},
            {"title": "Preconditions and permits", "hold_point": True},
            {"title": "Equipment and materials", "hold_point": False},
            {"title": "Execution steps", "hold_point": True},
            {"title": "Acceptance criteria and verification", "hold_point": True},
            {"title": "Contingency and stop-work", "hold_point": True},
            {"title": "Records and close-out", "hold_point": False},
        ],
    },
}


#: Versioned template families. Each family holds immutable version records; the highest
#: version with status CURRENT is the default. Superseded versions stay readable and are
#: never mutated or deleted - creating from one marks the record provenance explicitly.
TEMPLATE_FAMILIES: dict[str, dict[str, Any]] = {
    "drilling_program": {
        "family": "drilling_program",
        "description": "Drilling program structure families (vertical-well skeleton).",
        "versions": [
            {
                "version": 1,
                "status": "SUPERSEDED",
                "template": "standard_drilling_program",
                "note": "Original 12-section skeleton; superseded by v2 which adds DDR reconciliation.",
            },
            {
                "version": 2,
                "status": "CURRENT",
                "template": "standard_drilling_program_v2",
                "note": "Adds a daily-drilling-report reconciliation section.",
            },
        ],
    },
    "operational_procedure": {
        "family": "operational_procedure",
        "description": "Operational procedure structure families.",
        "versions": [
            {
                "version": 1,
                "status": "CURRENT",
                "template": "standard_operational_procedure",
                "note": "8-step skeleton with hold points.",
            },
        ],
    },
}

#: v2 program template body (structure only).
PROGRAM_TEMPLATES["standard_drilling_program_v2"] = {
    "description": (
        "Standard vertical-well drilling program skeleton, v2: adds a DDR reconciliation "
        "section. Structure only; no engineering values are supplied."
    ),
    "sections": PROGRAM_TEMPLATES["standard_drilling_program"]["sections"]
    + [("ddr_reconciliation", "Daily drilling report reconciliation")],
}


def list_template_families(kind: str | None = None) -> dict[str, Any]:
    """List template families with their immutable version history."""
    families = TEMPLATE_FAMILIES
    if kind in {"program", "programs"}:
        families = {k: v for k, v in families.items() if k == "drilling_program"}
    elif kind in {"procedure", "procedures"}:
        families = {k: v for k, v in families.items() if k == "operational_procedure"}
    elif kind is not None:
        raise ValidationError("kind must be 'program', 'procedure', or None")
    out: dict[str, Any] = {}
    for name, fam in families.items():
        current = max(
            (v["version"] for v in fam["versions"] if v["status"] == "CURRENT"), default=None
        )
        out[name] = {
            "family": name,
            "description": fam["description"],
            "current_version": current,
            "versions": [
                {
                    "version": v["version"],
                    "status": v["status"],
                    "template": v["template"],
                    "note": v["note"],
                    "immutable": True,
                }
                for v in fam["versions"]
            ],
        }
    return out


def resolve_template_version(family: str, version: int | None = None) -> dict[str, Any]:
    """Resolve one immutable template version; default is the CURRENT version.

    Resolving a SUPERSEDED version is allowed (old documents stay creatable/readable) but
    the result is flagged ``superseded=True`` so callers can mark provenance honestly.
    """
    if family not in TEMPLATE_FAMILIES:
        raise ValidationError("unknown template family", available=sorted(TEMPLATE_FAMILIES))
    versions = TEMPLATE_FAMILIES[family]["versions"]
    if version is None:
        current = [v for v in versions if v["status"] == "CURRENT"]
        if not current:
            raise ValidationError(f"template family {family!r} has no CURRENT version")
        chosen = max(current, key=lambda v: v["version"])
    else:
        matches = [v for v in versions if v["version"] == int(version)]
        if not matches:
            raise ValidationError(
                f"template family {family!r} has no version {version}",
                available=sorted(v["version"] for v in versions),
            )
        chosen = matches[0]
    return {**chosen, "superseded": chosen["status"] != "CURRENT", "immutable": True}


def list_templates(kind: str) -> dict[str, Any]:
    kind = str(kind).strip().lower()
    if kind in {"program", "programs"}:
        return {
            name: {
                "sections": [s[1] for s in spec["sections"]],
                **{k: v for k, v in spec.items() if k != "sections"},
            }
            for name, spec in PROGRAM_TEMPLATES.items()
        }
    if kind in {"procedure", "procedures"}:
        return dict(PROCEDURE_TEMPLATES)
    raise ValidationError("kind must be 'program' or 'procedure'")


def create_from_template(
    service: Any,
    kind: str,
    template_name: str,
    *,
    content: dict[str, Any],
    template_version: int | None = None,
) -> Any:
    """Create a DRAFT record populated only with the template's structure.

    ``content`` supplies the operator-entered identity fields (title, code, well, author).
    No template field is ever written as engineering content: sections are created with
    ``completion_state=OWNER_DECISION_REQUIRED`` and empty content, steps with empty
    instructions and no acceptance criteria.
    """
    kind = str(kind).strip().lower()
    catalog = PROGRAM_TEMPLATES if kind in {"program", "programs"} else PROCEDURE_TEMPLATES
    singular = "program" if kind in {"program", "programs"} else "procedure"
    if template_name not in catalog:
        raise ValidationError(
            f"unknown {singular} template", available=sorted(catalog), got=template_name
        )
    spec = catalog[template_name]
    author_key = "author" if singular == "program" else "created_by"
    title = content.get("title")
    author = content.get(author_key)
    if not isinstance(title, str) or not title.strip():
        raise ValidationError("template create requires a non-empty title")
    if not isinstance(author, str) or not author.strip():
        raise ValidationError(f"template create requires non-empty {author_key!r} attribution")

    provenance = [
        {
            "template": template_name,
            "kind": "structure_only",
            **({"template_version": int(template_version)} if template_version is not None else {}),
        }
    ]
    if singular == "program":
        record = service.repository.create_program(
            title=title.strip(),
            code=str(content.get("code") or ""),
            summary=str(content.get("summary") or ""),
            well_id=content.get("well_id"),
            field_id=content.get("field_id"),
            project_id=content.get("project_id"),
            author=author,
            created_by=author,
            origin="TEMPLATE",
            provenance=provenance,
        )
        token = service._token(record)
        for sequence, (_key, section_title) in enumerate(spec["sections"], start=1):
            service.add_program_section(
                record.id,
                expected_updated_at=token,
                sequence=sequence,
                section_key=_key,
                title=section_title,
                content="",
                completion_state="OWNER_DECISION_REQUIRED",
                origin="TEMPLATE",
                provenance=[{"template": template_name, "section_key": _key}],
            )
            token = service._token(record)
    else:
        record = service.repository.create_procedure(
            title=title.strip(),
            code=str(content.get("code") or ""),
            procedure_type=str(content.get("procedure_type") or "general"),
            description=str(content.get("description") or ""),
            well_id=content.get("well_id"),
            field_id=content.get("field_id"),
            project_id=content.get("project_id"),
            source_reference=content.get("source_reference"),
            created_by=author,
            origin="TEMPLATE",
            provenance=provenance,
        )
        token = service._token(record)
        for sequence, step_spec in enumerate(spec["steps"], start=1):
            service.add_procedure_step(
                record.id,
                expected_updated_at=token,
                sequence=sequence,
                title=step_spec["title"],
                instructions=STEP_PLACEHOLDER,
                hold_point=bool(step_spec.get("hold_point", False)),
                provenance=[{"template": template_name, "step": step_spec["title"]}],
            )
            token = service._token(record)
    service.session.flush()
    return record
