"""Procedures, programs and the plan the well was drilled against.

Three tables that all answer the same question in a different tense.  A **program** is what the
operator intends (a persistent versioned record rather than a PDF's properties, because "which
revision are we drilling" has to be answerable without opening a file); a **procedure** is how a
piece of that plan is done, with its own revision chain and its own approvals; a **target** is the
number the program committed to for one section, which is what makes plan-versus-actual a join over
columns that already exist instead of a comparison somebody has to remember to do.

The rules that matter here are the boring ones:

*   **A revision is a new row, never an edit.**  Superseding writes the old row's ``is_current`` to
    false and its status to ``SUPERSEDED``, and copies every field the caller did not mention - so a
    revision that only changes the mud target still carries the title, the scope and the references of
    the thing it replaced.  An in-place edit would leave no answer to "what did we believe before".
*   **One current revision per code, enforced by the database.**  The partial unique index added by
    migration 0004 makes two current revisions impossible to write;
    :func:`~drilling_intelligence.database.integrity.check_revision_chains` checks what a constraint
    cannot (a superseded row still claiming to be current, a chain that points at itself).
*   **Approval is attributed or it has not happened.**  ``by=`` is required, and a record cannot
    approve itself into existence: the transition goes through the lifecycle, which refuses every
    illegal jump with the list of what *is* allowed.
*   **A plan without an actual is reported as missing, not as a variance of zero.**  This is the one
    place where a nil number would be read as a fact ("we drilled to plan"), so every comparison row
    carries a ``status`` saying which side was missing
    (:meth:`EngineeringRepository.plan_actual_summary`).
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from typing import Any, NamedTuple

from sqlalchemy import delete as sa_delete
from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from ..core.enums import (
    CalculationStatus,
    ConfirmationStatus,
    KnowledgeOrigin,
    KnowledgeRelationType,
    ProcedureLifecycle,
    ProgramLifecycle,
    RecordState,
)
from ..core.errors import UnitError, ValidationError
from ..core.hashing import sha256_obj
from ..core.ids import new_id
from ..core.lifecycle import PROCEDURE_LIFECYCLE, PROGRAM_LIFECYCLE
from ..core.units import Quantity, resolve_unit
from ..core.vocabulary import snake_token
from ..database.integrity import create_knowledge_relation
from ..database.models import (
    Calculation,
    CalculationInput,
    DdrReport,
    Document,
    DocumentVersion,
    DrillingProgram,
    Field,
    LessonLearned,
    NptRecord,
    ProcedureRecord,
    ProgramTarget,
    Project,
    RiskRecord,
    Well,
    WellOperation,
    WellSection,
)
from ..operations.repository import set_record_status

__all__ = [
    "PLAN_ACTUAL_METRICS",
    "PROCEDURE_FIELDS",
    "PROGRAM_FIELDS",
    "TARGET_FIELDS",
    "EngineeringRepository",
]


class Metric(NamedTuple):
    """One row of the plan-versus-actual comparison: where the plan lives, where the fact lives."""

    label: str
    planned_column: str
    planned_unit_column: str
    actual_key: str
    default_unit: str | None


#: The plan-versus-actual pairs, in the order a report lists them.  ``actual_key`` names the entry in
#: :meth:`EngineeringRepository._section_actuals`; the depth pair is deliberate - a section stores one
#: bottom depth (the as-drilled one) and the program stores the planned one, which is exactly the
#: comparison a post-well review opens with.
PLAN_ACTUAL_METRICS: tuple[Metric, ...] = (
    Metric("depth_md", "planned_depth_md_value", "planned_depth_md_unit", "depth_md", "m"),
    Metric("duration_days", "planned_duration_days", "", "duration_days", "d"),
    Metric("mud_weight", "planned_mud_weight_value", "planned_mud_weight_unit", "mud_weight", None),
    Metric("npt_hours", "planned_npt_hours", "", "npt_hours", "h"),
)

#: The fields a caller may change through a revision.  ``revision``, ``supersedes_id`` and
#: ``is_current`` are the chain's own and belong to :meth:`EngineeringRepository.revise_procedure`;
#: the approval columns are written only by the methods that also validate the transition, and
#: ``origin``/``created_by`` describe who made the record rather than what it says.
PROCEDURE_FIELDS: frozenset[str] = frozenset(
    {
        "title",
        "procedure_type",
        "description",
        "revision_label",
        "project_id",
        "field_id",
        "well_id",
        "section_id",
        "source_reference",
        "effective_from",
        "document_id",
        "document_version_id",
        "provenance",
        "attributes",
    }
)
PROGRAM_FIELDS: frozenset[str] = frozenset(
    {
        "title",
        "summary",
        "revision_label",
        "project_id",
        "field_id",
        "well_id",
        "author",
        "planned_spud_date",
        "planned_completion_date",
        "document_id",
        "document_version_id",
        "provenance",
        "attributes",
    }
)
#: The numbers a program commits a section to - the left-hand side of plan-versus-actual.
TARGET_FIELDS: frozenset[str] = frozenset(
    {
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
)


def _bounded(limit: int) -> int | None:
    """A ``LIMIT`` value, with ``0`` meaning "do not limit".

    A list is paginated for a screen and unpaginated for a summary, from the same query.  Reading
    ``limit=0`` as ``LIMIT 0`` would report an empty field, which is a wrong answer to a question that
    has a right one.
    """
    return None if int(limit) <= 0 else int(limit)


def _token(value: object, fallback: str = "general") -> str:
    """A stable snake_case token for an open-vocabulary column, with a documented fallback."""
    token = snake_token(value)
    return token or fallback


class EngineeringRepository:
    """Versioned, scoped, evidence-carrying procedures and programs.

    One repository for both because they share the machinery that makes a versioned record safe - the
    revision chain, the lifecycle, the scope columns and the reference edges - and a caller linking a
    procedure to the program it belongs to is doing one thing, not two.
    """

    def __init__(self, session: Session) -> None:
        self.session = session

    # -- scope ----------------------------------------------------------------
    def _check_scope(self, **scope: str) -> None:
        """Refuse a record whose scope contradicts the hierarchy it was copied from.

        The scope columns are denormalised for querying, which means they *can* disagree with the well
        they point at.  A disagreement caught at write time is a two-second error; the same
        disagreement sitting in a table is a field report that quietly excludes a well.
        """
        well_id = str(scope.get("well_id") or "")
        field_id = str(scope.get("field_id") or "")
        project_id = str(scope.get("project_id") or "")
        section_id = str(scope.get("section_id") or "")
        if section_id:
            if not well_id:
                raise ValidationError(
                    "a section-scoped record needs well_id too",
                    hint="the section is inside a well; without it the scope cannot be checked",
                )
            section = self.session.get(WellSection, section_id)
            if section is None:
                raise ValidationError(f"no section {section_id!r}")
            if str(section.well_id) != well_id:
                raise ValidationError(
                    "the section belongs to another well",
                    section_well_id=str(section.well_id),
                )
        if well_id:
            well = self.session.get(Well, well_id)
            if well is None:
                raise ValidationError(f"no well {well_id!r}")
            for label, wanted in (("field_id", field_id), ("project_id", project_id)):
                stored = str(getattr(well, label, "") or "")
                if wanted and stored and stored != wanted:
                    raise ValidationError(
                        f"well {well.name} is not in {label} {wanted!r}",
                        actual=stored,
                    )
        if field_id and self.session.get(Field, field_id) is None:
            raise ValidationError(f"no field {field_id!r}")
        if project_id and self.session.get(Project, project_id) is None:
            raise ValidationError(f"no project {project_id!r}")

    # -- procedures -----------------------------------------------------------
    def create_procedure(
        self,
        *,
        title: str,
        code: str = "",
        procedure_type: str = "general",
        description: str = "",
        status: ProcedureLifecycle | str | None = None,
        created_by: str = "system",
        origin: str = KnowledgeOrigin.MANUAL.value,
        provenance: Sequence[Mapping[str, Any]] | None = None,
        attributes: Mapping[str, Any] | None = None,
        **scope: str,
    ) -> ProcedureRecord:
        """Write the first revision of a procedure.

        A procedure may legitimately have no code: a field's "how we clean a hole" note is a record
        whether or not anybody set up a numbering scheme.  With a code, "which revision is current" is
        what the database enforces; without one, ``revision`` still counts, but there is nothing to be
        current *among*.
        """
        if not str(title or "").strip():
            raise ValidationError("a procedure needs a title")
        self._check_scope(**scope)
        row = ProcedureRecord(
            id=new_id("proc"),
            code=str(code or "").strip() or None,
            title=str(title).strip()[:400],
            procedure_type=_token(procedure_type),
            revision=1,
            is_current=True,
            status=str(
                PROCEDURE_LIFECYCLE.parse(status)
                if status is not None
                else PROCEDURE_LIFECYCLE.initial
            ),
            description=str(description or ""),
            project_id=str(scope.get("project_id") or "") or None,
            field_id=str(scope.get("field_id") or "") or None,
            well_id=str(scope.get("well_id") or "") or None,
            section_id=str(scope.get("section_id") or "") or None,
            created_by=str(created_by or "") or "system",
            origin=str(getattr(origin, "value", origin)),
            provenance=[dict(item) for item in provenance or ()],
            attributes=dict(attributes or {}),
        )
        self.session.add(row)
        self.session.flush()
        return row

    def get_procedure(self, procedure_id: str) -> ProcedureRecord:
        row = self.session.get(ProcedureRecord, str(procedure_id))
        if row is None:
            raise ValidationError(f"no procedure {procedure_id!r}")
        return row

    def current_procedure(self, code: str) -> ProcedureRecord | None:
        """The current revision of ``code``, or ``None`` when the field has no such procedure."""
        key = str(code or "").strip()
        if not key:
            return None
        return self.session.execute(
            select(ProcedureRecord)
            .where(ProcedureRecord.code == key, ProcedureRecord.is_current.is_(True))
            .order_by(ProcedureRecord.revision.desc(), ProcedureRecord.id)
        ).scalar_one_or_none()

    def revision_chain(self, procedure_id: str) -> list[ProcedureRecord]:
        """Every revision of this procedure's code, oldest first.

        Walked by ``supersedes_id`` rather than by revision number, because a chain that was assembled
        by hand can have gaps or duplicates in its numbering and the caller still deserves the true
        order - and because a reused code must not merge two unrelated chains.
        """
        row = self.get_procedure(procedure_id)
        if not row.code:
            return [row]
        # Start from the head so the walk is one-directional even when the caller names a superseded
        # revision.
        head = self.current_procedure(str(row.code)) or row
        chain: list[ProcedureRecord] = []
        seen: set[str] = set()
        cursor: ProcedureRecord | None = head
        while cursor is not None and cursor.id not in seen:
            seen.add(cursor.id)
            chain.append(cursor)
            cursor = (
                self.session.get(ProcedureRecord, str(cursor.supersedes_id))
                if cursor.supersedes_id
                else None
            )
        return list(reversed(chain))

    def revise_procedure(
        self,
        procedure_id: str,
        *,
        by: str,
        changes: Mapping[str, Any] | None = None,
        revision_label: str = "",
    ) -> ProcedureRecord:
        """Supersede ``procedure_id`` with a new revision carrying ``changes``.

        Unmentioned fields are copied from the superseded row: a revision that fixes one paragraph is
        still the same procedure in every other respect, and requiring a caller to restate everything
        is how a revision quietly loses a reference.  The new row starts back at ``DRAFT`` - an
        approved document that changes has to be approved again, and no amount of copying can transfer
        a person's judgement about different text.
        """
        source = self.get_procedure(procedure_id)
        if not source.is_current:
            raise ValidationError(
                "only the current revision of a procedure can be superseded",
                hint=f"revision {source.revision} is marked superseded; revise the current one",
                current_revision=source.revision,
            )
        if not str(by or "").strip():
            raise ValidationError("a revision needs an author", hint="pass by=<who revised it>")
        payload = dict(changes or {})
        unknown = sorted(set(payload) - PROCEDURE_FIELDS)
        if unknown:
            raise ValidationError(
                f"procedure has no updatable field named {', '.join(unknown)}",
                allowed=sorted(PROCEDURE_FIELDS),
            )
        scope = {
            key: str(payload.get(key) or "")
            for key in ("well_id", "field_id", "project_id", "section_id")
        }
        self._check_scope(**scope)
        row = ProcedureRecord(
            id=new_id("proc"),
            code=source.code,
            title=str(payload.get("title") or source.title)[:400],
            procedure_type=_token(payload.get("procedure_type") or source.procedure_type),
            revision=int(source.revision or 1) + 1,
            revision_label=str(revision_label or payload.get("revision_label") or "") or None,
            is_current=True,
            supersedes_id=source.id,
            status=str(ProcedureLifecycle.DRAFT),
            description=str(payload.get("description", source.description) or ""),
            project_id=scope["project_id"] or source.project_id or None,
            field_id=scope["field_id"] or source.field_id or None,
            well_id=scope["well_id"] or source.well_id or None,
            section_id=scope["section_id"] or source.section_id or None,
            source_reference=str(payload.get("source_reference") or source.source_reference or "")
            or None,
            effective_from=payload.get("effective_from") or source.effective_from,
            document_id=str(payload.get("document_id") or source.document_id or "") or None,
            document_version_id=(
                str(payload.get("document_version_id") or source.document_version_id or "") or None
            ),
            provenance=(
                [dict(item) for item in payload["provenance"]]
                if payload.get("provenance") is not None
                else list(source.provenance or [])
            ),
            origin=str(source.origin),
            created_by=by,
            attributes=dict(payload.get("attributes") or source.attributes or {}),
        )
        source.is_current = False
        # Superseding is a mechanical consequence of a new revision, not a review decision, so the old
        # row's status is written directly: ``SUPERSEDED`` says "this is no longer the row to follow",
        # which is as true of a draft that was rewritten as of an approved document that was.
        source.status = str(ProcedureLifecycle.SUPERSEDED)
        self.session.add(row)
        self.session.flush()
        return row

    def set_procedure_status(
        self,
        procedure_id: str,
        new_status: ProcedureLifecycle | str,
        *,
        by: str = "",
        reason: str = "",
    ) -> ProcedureRecord:
        """Move a procedure along its lifecycle, recording who decided and why."""
        row = self.get_procedure(procedure_id)
        set_record_status(
            self.session,
            row,
            new_status,
            by=by,
            reason=reason,
            lifecycle=PROCEDURE_LIFECYCLE,
        )
        return row

    def approve_procedure(
        self,
        procedure_id: str,
        *,
        by: str,
        note: str = "",
        effective_from: object = None,
    ) -> ProcedureRecord:
        """Approve the current revision; the lifecycle decides whether that is possible.

        The approval is deliberately not copied to earlier or later revisions.  "Rev 3 was approved,
        then rev 4 was written" is the fact a reader needs, and a field that silently followed the
        chain would read as though somebody had approved revision 4's text too.
        """
        row = self.get_procedure(procedure_id)
        was_approved = str(row.status) == str(ProcedureLifecycle.APPROVED)
        if not was_approved:
            row = self.set_procedure_status(
                procedure_id, ProcedureLifecycle.APPROVED, by=by, reason=note
            )
        if not str(by or "").strip():  # pragma: no cover - the lifecycle already refused
            raise ValidationError("an approval needs an approver")
        row.approved_by = by
        row.approved_at = datetime.now(UTC)
        row.reviewer = by
        row.reviewed_at = row.approved_at
        if effective_from is not None:
            row.effective_from = effective_from  # type: ignore[assignment]
        self.session.flush()
        return row

    def reference_procedure(
        self,
        procedure_id: str,
        *,
        program_id: str = "",
        standard_document_ids: Sequence[str] = (),
        document_ids: Sequence[str] = (),
        offset_well_ids: Sequence[str] = (),
        lesson_ids: Sequence[str] = (),
        calculation_ids: Sequence[str] = (),
        risk_ids: Sequence[str] = (),
        provenance: Sequence[Mapping[str, Any]] | None = None,
    ) -> dict[str, int]:
        """Attach the things a procedure is based on, as edges in the one knowledge graph.

        Returns the counts per relation, because "this procedure references three documents" is worth
        asserting in a test and worth showing on a screen - and a silently missing link would otherwise
        only be noticed by somebody who already suspected it.
        """
        row = self.get_procedure(procedure_id)
        targets: list[tuple[str, str, str]] = []
        if program_id:
            self.get_program(program_id)
            targets.append(
                (KnowledgeRelationType.PROCEDURE_BASED_ON_PROGRAM.value, "program", str(program_id))
            )
        for document_id in standard_document_ids:
            targets.append(
                (
                    KnowledgeRelationType.PROCEDURE_CITES_STANDARD.value,
                    "document_version",
                    self._current_version_of_document(str(document_id)),
                )
            )
        for document_id in document_ids:
            targets.append(
                (
                    KnowledgeRelationType.PROCEDURE_CITES_DOCUMENT.value,
                    "document_version",
                    self._current_version_of_document(str(document_id)),
                )
            )
        for well_id in offset_well_ids:
            if self.session.get(Well, str(well_id)) is None:
                raise ValidationError(f"no well {well_id!r}")
            targets.append(
                (KnowledgeRelationType.PROCEDURE_OBSERVES_WELL.value, "well", str(well_id))
            )
        for lesson_id in lesson_ids:
            if self.session.get(LessonLearned, str(lesson_id)) is None:
                raise ValidationError(f"no lesson {lesson_id!r}")
            targets.append(
                (KnowledgeRelationType.PROCEDURE_ADDRESSES_LESSON.value, "lesson", str(lesson_id))
            )
        for calculation_id in calculation_ids:
            if self.session.get(Calculation, str(calculation_id)) is None:
                raise ValidationError(f"no calculation {calculation_id!r}")
            targets.append(
                (
                    KnowledgeRelationType.PROCEDURE_USES_CALCULATION.value,
                    "calculation",
                    str(calculation_id),
                )
            )
        for risk_id in risk_ids:
            if self.session.get(RiskRecord, str(risk_id)) is None:
                raise ValidationError(f"no risk {risk_id!r}")
            targets.append(
                (KnowledgeRelationType.PROCEDURE_ADDRESSES_RISK.value, "risk", str(risk_id))
            )
        counts: dict[str, int] = {}
        for relation, target_type, target_id in targets:
            create_knowledge_relation(
                self.session,
                source_type="procedure",
                source_id=row.id,
                relation=relation,
                target_type=target_type,
                target_id=target_id,
                provenance=[dict(item) for item in provenance or ()],
                note="procedure reference",
            )
            counts[relation] = counts.get(relation, 0) + 1
        return counts

    def _current_version_of_document(self, document_id: str) -> str:
        """The document's current version id.

        An edge points at a *version*, never at a document, because "the procedure cites API RP 59" is
        only checkable against the revision that was actually read.  A document with no version row is
        a bug in the ingest path, so it is refused here rather than papered over with the document id.
        """
        document = self.session.get(Document, str(document_id))
        if document is None:
            raise ValidationError(f"no document {document_id!r}")
        version_id = str(getattr(document, "current_version_id", "") or "")
        if not version_id or self.session.get(DocumentVersion, version_id) is None:
            raise ValidationError(
                f"document {document_id!r} has no current version",
                hint="a procedure cites a version; re-ingest the file first",
            )
        return version_id

    def list_procedures(
        self,
        *,
        well_id: str = "",
        field_id: str = "",
        project_id: str = "",
        section_id: str = "",
        procedure_type: str = "",
        status: str = "",
        include_superseded: bool = False,
        limit: int = 200,
    ) -> list[ProcedureRecord]:
        statement = select(ProcedureRecord)
        if well_id:
            statement = statement.where(ProcedureRecord.well_id == well_id)
        if section_id:
            statement = statement.where(ProcedureRecord.section_id == section_id)
        if field_id or project_id:
            scoped_wells = select(Well.id).where(
                *([Well.field_id == field_id] if field_id else [Well.project_id == project_id])
            )
            direct = (
                ProcedureRecord.field_id == field_id
                if field_id
                else ProcedureRecord.project_id == project_id
            )
            statement = statement.where(or_(direct, ProcedureRecord.well_id.in_(scoped_wells)))
        if procedure_type:
            statement = statement.where(ProcedureRecord.procedure_type == _token(procedure_type))
        if status:
            statement = statement.where(
                ProcedureRecord.status == str(PROCEDURE_LIFECYCLE.parse(status))
            )
        if not include_superseded:
            statement = statement.where(ProcedureRecord.is_current.is_(True))
        return list(
            self.session.execute(
                statement.order_by(ProcedureRecord.code, ProcedureRecord.revision.desc()).limit(
                    _bounded(limit)
                )
            ).scalars()
        )

    def procedures_for_well(
        self, well_id: str, *, include_field: bool = True, include_project: bool = True
    ) -> list[ProcedureRecord]:
        """The procedures that apply to a well: its own, plus its field's when that is asked for.

        ``include_field`` is an argument rather than a rule because the two questions are different:
        "what was written for this well" (a compliance answer) and "what governs this well" (an
        engineering one).  A screen that flattened them would let a field-wide template look like a
        well-specific instruction.
        """
        well = self.session.get(Well, str(well_id))
        if well is None:
            raise ValidationError(f"no well {well_id!r}")
        scopes = [ProcedureRecord.well_id == well.id]
        if include_field and well.field_id:
            scopes.append(ProcedureRecord.field_id == well.field_id)
        if include_project and well.project_id:
            scopes.append(ProcedureRecord.project_id == well.project_id)
        return list(
            self.session.execute(
                select(ProcedureRecord)
                .where(ProcedureRecord.is_current.is_(True), or_(*scopes))
                .order_by(ProcedureRecord.code, ProcedureRecord.revision.desc())
            ).scalars()
        )

    # -- programs -------------------------------------------------------------
    def create_program(
        self,
        *,
        title: str,
        code: str = "",
        summary: str = "",
        status: ProgramLifecycle | str | None = None,
        author: str = "",
        created_by: str = "system",
        origin: str = KnowledgeOrigin.MANUAL.value,
        provenance: Sequence[Mapping[str, Any]] | None = None,
        attributes: Mapping[str, Any] | None = None,
        **scope: str,
    ) -> DrillingProgram:
        if not str(title or "").strip():
            raise ValidationError("a program needs a title")
        self._check_scope(**{key: value for key, value in scope.items() if key != "section_id"})
        row = DrillingProgram(
            id=new_id("prog"),
            code=str(code or "").strip() or None,
            title=str(title).strip()[:400],
            revision=1,
            is_current=True,
            status=str(
                PROGRAM_LIFECYCLE.parse(status) if status is not None else PROGRAM_LIFECYCLE.initial
            ),
            summary=str(summary or ""),
            author=str(author or "") or None,
            project_id=str(scope.get("project_id") or "") or None,
            field_id=str(scope.get("field_id") or "") or None,
            well_id=str(scope.get("well_id") or "") or None,
            created_by=str(created_by or "") or "system",
            origin=str(getattr(origin, "value", origin)),
            provenance=[dict(item) for item in provenance or ()],
            attributes=dict(attributes or {}),
        )
        self.session.add(row)
        self.session.flush()
        return row

    def get_program(self, program_id: str) -> DrillingProgram:
        row = self.session.get(DrillingProgram, str(program_id))
        if row is None:
            raise ValidationError(f"no program {program_id!r}")
        return row

    def revise_program(
        self,
        program_id: str,
        *,
        by: str,
        changes: Mapping[str, Any] | None = None,
        revision_label: str = "",
    ) -> DrillingProgram:
        """Supersede a program with a new revision, carrying its targets across.

        The targets are *copied*, not shared: a revision that changes the 8½ in section's mud weight
        must not silently change what revision 3 said, because revision 3 is the document somebody
        approved.  Copying is what keeps "what did we plan at the time" answerable forever.
        """
        source = self.get_program(program_id)
        if not source.is_current:
            raise ValidationError(
                "only the current revision of a program can be superseded",
                current_revision=source.revision,
            )
        if not str(by or "").strip():
            raise ValidationError("a revision needs an author", hint="pass by=<who revised it>")
        payload = dict(changes or {})
        unknown = sorted(set(payload) - PROGRAM_FIELDS)
        if unknown:
            raise ValidationError(
                f"program has no updatable field named {', '.join(unknown)}",
                allowed=sorted(PROGRAM_FIELDS),
            )
        row = DrillingProgram(
            id=new_id("prog"),
            code=source.code,
            title=str(payload.get("title") or source.title)[:400],
            revision=int(source.revision or 1) + 1,
            revision_label=str(revision_label or payload.get("revision_label") or "") or None,
            is_current=True,
            supersedes_id=source.id,
            status=str(ProgramLifecycle.DRAFT),
            summary=str(payload.get("summary", source.summary) or ""),
            author=str(payload.get("author") or source.author or "") or None,
            project_id=str(payload.get("project_id") or source.project_id or "") or None,
            field_id=str(payload.get("field_id") or source.field_id or "") or None,
            well_id=str(payload.get("well_id") or source.well_id or "") or None,
            planned_spud_date=payload.get("planned_spud_date") or source.planned_spud_date,
            planned_completion_date=(
                payload.get("planned_completion_date") or source.planned_completion_date
            ),
            document_id=str(payload.get("document_id") or source.document_id or "") or None,
            document_version_id=(
                str(payload.get("document_version_id") or source.document_version_id or "") or None
            ),
            provenance=(
                [dict(item) for item in payload["provenance"]]
                if payload.get("provenance") is not None
                else list(source.provenance or [])
            ),
            origin=str(source.origin),
            created_by=by,
            attributes=dict(payload.get("attributes") or source.attributes or {}),
        )
        source.is_current = False
        # The old revision stops being the one to follow whether or not anybody approved it; see
        # :meth:`revise_procedure` for why this is not a lifecycle transition.
        source.status = str(ProgramLifecycle.SUPERSEDED)
        self.session.add(row)
        self.session.flush()
        for target in self.list_targets(source.id):
            self.session.add(
                ProgramTarget(
                    id=new_id("targ"),
                    program_id=row.id,
                    section_id=target.section_id,
                    sequence=target.sequence,
                    name=target.name,
                    hole_size_in=target.hole_size_in,
                    casing_program=target.casing_program,
                    formation_top=target.formation_top,
                    planned_depth_md_value=target.planned_depth_md_value,
                    planned_depth_md_unit=target.planned_depth_md_unit,
                    planned_duration_days=target.planned_duration_days,
                    planned_mud_weight_value=target.planned_mud_weight_value,
                    planned_mud_weight_unit=target.planned_mud_weight_unit,
                    planned_npt_hours=target.planned_npt_hours,
                    planned_cost_value=target.planned_cost_value,
                    planned_cost_unit=target.planned_cost_unit,
                    provenance=list(target.provenance or []),
                    origin=str(target.origin),
                    attributes=dict(target.attributes or {}),
                )
            )
        self.session.flush()
        return row

    def set_program_status(
        self,
        program_id: str,
        new_status: ProgramLifecycle | str,
        *,
        by: str = "",
        reason: str = "",
    ) -> DrillingProgram:
        row = self.get_program(program_id)
        target = set_record_status(
            self.session,
            row,
            new_status,
            by=by,
            reason=reason,
            lifecycle=PROGRAM_LIFECYCLE,
        )
        # ``submitted_at`` records the moment the program went into review, which is the timestamp an
        # approver is implicitly commenting on; the shared status helper does not know this column.
        if str(target) == str(ProgramLifecycle.IN_REVIEW) and row.submitted_at is None:
            row.submitted_at = datetime.now(UTC)
            self.session.flush()
        return row

    def approve_program(
        self, program_id: str, *, by: str, note: str = "", at: object = None
    ) -> DrillingProgram:
        row = self.get_program(program_id)
        if str(row.status) != str(ProgramLifecycle.APPROVED):
            row = self.set_program_status(program_id, ProgramLifecycle.APPROVED, by=by, reason=note)
        if not str(by or "").strip():  # pragma: no cover - the lifecycle already refused
            raise ValidationError("an approval needs an approver")
        row.approver = by
        row.approved_at = at or datetime.now(UTC)  # type: ignore[assignment]
        self.session.flush()
        return row

    def list_programs(
        self,
        *,
        well_id: str = "",
        field_id: str = "",
        project_id: str = "",
        status: str = "",
        include_superseded: bool = False,
        limit: int = 200,
    ) -> list[DrillingProgram]:
        statement = select(DrillingProgram)
        if well_id:
            statement = statement.where(DrillingProgram.well_id == well_id)
        for label, value in (("field_id", field_id), ("project_id", project_id)):
            if not value:
                continue
            scoped_wells = select(Well.id).where(getattr(Well, label) == value)
            statement = statement.where(
                or_(
                    getattr(DrillingProgram, label) == value,
                    DrillingProgram.well_id.in_(scoped_wells),
                )
            )
        if status:
            statement = statement.where(
                DrillingProgram.status == str(PROGRAM_LIFECYCLE.parse(status))
            )
        if not include_superseded:
            statement = statement.where(DrillingProgram.is_current.is_(True))
        return list(
            self.session.execute(
                statement.order_by(DrillingProgram.code, DrillingProgram.revision.desc()).limit(
                    _bounded(limit)
                )
            ).scalars()
        )

    def add_target(
        self,
        program_id: str,
        *,
        name: str = "",
        section_id: str = "",
        sequence: int = 0,
        provenance: Sequence[Mapping[str, Any]] | None = None,
        origin: str = KnowledgeOrigin.MANUAL.value,
        attributes: Mapping[str, Any] | None = None,
        **values: Any,
    ) -> ProgramTarget:
        """One section's plan, in the same column pairs the section itself uses.

        ``section_id`` may be empty: a program is written before the hole exists, and a plan for "the
        8½ in section" is real even when no section row has been created yet.  When it *is* given it
        is validated, because a target attached to another well's section would be compared against the
        wrong actuals and never say so.
        """
        program = self.get_program(program_id)
        unknown = sorted(set(values) - TARGET_FIELDS)
        if unknown:
            raise ValidationError(
                f"program target has no field named {', '.join(unknown)}",
                allowed=sorted(TARGET_FIELDS),
            )
        section: WellSection | None = None
        if section_id:
            section = self.session.get(WellSection, str(section_id))
            if section is None:
                raise ValidationError(f"no section {section_id!r}")
            if program.well_id and str(section.well_id) != str(program.well_id):
                raise ValidationError(
                    "the section belongs to another well than the program's",
                    program_well_id=str(program.well_id),
                    section_well_id=str(section.well_id),
                )
        row = ProgramTarget(
            id=new_id("targ"),
            program_id=program.id,
            section_id=section.id if section is not None else None,
            sequence=int(sequence or 0),
            name=str(name or "")[:200] or None,
            provenance=[dict(item) for item in provenance or ()],
            origin=str(getattr(origin, "value", origin)),
            attributes=dict(attributes or {}),
        )
        for field in TARGET_FIELDS - {"sequence"}:
            if values.get(field) is not None:
                setattr(row, field, values[field])
        self.session.add(row)
        self.session.flush()
        return row

    def update_target(self, target_id: str, **values: Any) -> tuple[ProgramTarget, dict[str, Any]]:
        """Change one planned number in place, and say which ones moved.

        A target is not itself versioned: the versioning lives on the program, so a change to an
        approved plan goes through :meth:`revise_program` (which copies the targets).  This method is
        for drafting a revision that has not been approved yet, which is why it reports what it
        changed instead of pretending to be the whole history.
        """
        row = self.session.get(ProgramTarget, str(target_id))
        if row is None:
            raise ValidationError(f"no program target {target_id!r}")
        unknown = sorted(set(values) - TARGET_FIELDS)
        if unknown:
            raise ValidationError(
                f"program target has no field named {', '.join(unknown)}",
                allowed=sorted(TARGET_FIELDS),
            )
        applied: dict[str, Any] = {}
        for field, value in values.items():
            setattr(row, field, value)
            applied[field] = value
        self.session.flush()
        return row, applied

    def list_targets(self, program_id: str) -> list[ProgramTarget]:
        return list(
            self.session.execute(
                select(ProgramTarget)
                .where(ProgramTarget.program_id == str(program_id))
                .order_by(ProgramTarget.sequence, ProgramTarget.name, ProgramTarget.id)
            ).scalars()
        )

    def programs_for_well(self, well_id: str) -> list[DrillingProgram]:
        """The current programs that govern a well: its own, then its field's and project's.

        Ordered most specific first, and *not* merged into a single "the program": a well drilled to a
        field template with a well-specific addendum has two documents in play, and an answer that
        flattened them would hide which one a number came from.
        """
        well = self.session.get(Well, str(well_id))
        if well is None:
            raise ValidationError(f"no well {well_id!r}")
        scopes = [DrillingProgram.well_id == well.id]
        for label, value in (("field_id", well.field_id), ("project_id", well.project_id)):
            if value:
                scopes.append(getattr(DrillingProgram, label) == value)
        rows = list(
            self.session.execute(
                select(DrillingProgram)
                .where(DrillingProgram.is_current.is_(True), or_(*scopes))
                .order_by(DrillingProgram.revision.desc(), DrillingProgram.id)
            ).scalars()
        )
        rows.sort(key=lambda row: 0 if row.well_id == well.id else (1 if row.field_id else 2))
        return rows

    # -- plan versus actual ---------------------------------------------------
    def plan_actual_summary(
        self, *, well_id: str = "", section_id: str = "", program_id: str = ""
    ) -> list[dict[str, Any]]:
        """Compare each section's planned numbers with its actual ones, where both exist.

        The comparison is arithmetic on columns the platform already holds - the program's targets and
        the section's planned/actual pairs - and it never imputes: a missing plan or a missing actual is
        a row whose ``status`` says which, so a screen can show "no actual recorded" instead of a zero
        that reads as "on plan".
        """
        if not (well_id or section_id or program_id):
            raise ValidationError(
                "plan-vs-actual needs a scope",
                hint="pass well_id, section_id or program_id",
            )
        section_statement = select(WellSection)
        if well_id:
            section_statement = section_statement.where(WellSection.well_id == well_id)
        if section_id:
            section_statement = section_statement.where(WellSection.id == section_id)
        sections = list(
            self.session.execute(
                section_statement.order_by(WellSection.sequence, WellSection.id)
            ).scalars()
        )
        target_statement = select(ProgramTarget)
        if program_id:
            target_statement = target_statement.where(ProgramTarget.program_id == program_id)
        elif well_id:
            target_statement = target_statement.where(
                ProgramTarget.program_id.in_(
                    select(DrillingProgram.id).where(DrillingProgram.well_id == well_id)
                )
            )
        targets = list(
            self.session.execute(
                target_statement.order_by(ProgramTarget.sequence, ProgramTarget.id)
            ).scalars()
        )
        payload: list[dict[str, Any]] = []
        for section in sections:
            match = self._match_target(section, targets)
            actuals = self._section_actuals(section)
            for metric in PLAN_ACTUAL_METRICS:
                planned = None if match is None else getattr(match, metric.planned_column, None)
                actual = actuals.get(metric.actual_key)
                unit = metric.default_unit
                if metric.planned_unit_column and match is not None:
                    unit = getattr(match, metric.planned_unit_column, None) or unit
                payload.append(
                    {
                        "well_id": section.well_id,
                        "section_id": section.id,
                        "section": section.name,
                        "metric": metric.label,
                        "planned": None if planned is None else float(planned),
                        "actual": None if actual is None else float(actual),
                        "unit": unit,
                        "variance": (
                            round(float(actual) - float(planned), 6)
                            if planned is not None and actual is not None
                            else None
                        ),
                        "status": self._plan_actual_status(match, planned, actual),
                        "program_id": None if match is None else match.program_id,
                        "target_id": None if match is None else match.id,
                    }
                )
        return payload

    @staticmethod
    def _match_target(
        section: WellSection, targets: Sequence[ProgramTarget]
    ) -> ProgramTarget | None:
        """The target that describes this section, by id first and by name only as a fallback.

        Matching on the section id is exact; matching on the name is what a program written before the
        well was spudded has to fall back to.  A name match is still reported without its id, so a
        reader can tell the two apart rather than trusting a join that was really a guess.
        """
        for target in targets:
            if target.section_id and str(target.section_id) == str(section.id):
                return target
        wanted = str(section.name or "").strip().lower()
        if not wanted:
            return None
        for target in targets:
            if str(target.name or "").strip().lower() == wanted:
                return target
        return None

    @staticmethod
    def _plan_actual_status(target: ProgramTarget | None, planned: Any, actual: Any) -> str:
        if target is None:
            return "NO_TARGET"
        if planned is None:
            return "NO_PLAN"
        if actual is None:
            return "NO_ACTUAL"
        return "ON_PLAN" if float(planned) == float(actual) else "VARIANCE"

    def _section_actuals(self, section: WellSection) -> dict[str, Any]:
        """The numbers a section actually achieved, including the one only the records know.

        Depth is the section's bottom depth, duration its actual days, mud weight its actual column -
        and NPT hours are summed from the NPT rows dated in the section, because there is no "actual
        NPT" column on a section and the events are the honest definition of one.  A section with no
        dated rows yields ``None``, not 0: "nothing was recorded" and "nothing was lost" are different
        claims about the world.
        """
        depth = getattr(section, "bottom_depth_value", None)
        hours = self.session.execute(
            select(func.sum(NptRecord.duration_hours))
            .where(NptRecord.section_id == section.id)
            .where(NptRecord.duration_hours.is_not(None))
        ).scalar_one_or_none()
        return {
            "depth_md": None if depth is None else float(depth),
            "duration_days": getattr(section, "actual_duration_days", None),
            "mud_weight": getattr(section, "actual_mud_weight_value", None),
            "npt_hours": None if hours is None else round(float(hours), 4),
        }

    def record_calculation(
        self,
        *,
        method_id: str,
        method_version: str = "",
        calculation_type: str = "",
        record_state: RecordState | str = RecordState.CURRENT,
        well_id: str = "",
        section_id: str = "",
        project_id: str = "",
        source_id: str = "",
        inputs: Mapping[str, Any] | None = None,
        outputs: Mapping[str, Any] | None = None,
        assumptions: Sequence[Any] | None = None,
        validation: Mapping[str, Any] | None = None,
        uncertainty: Mapping[str, Any] | None = None,
        confidence: float | None = None,
        status: CalculationStatus | str = CalculationStatus.COMPUTED,
        error: str = "",
        provenance: Sequence[Mapping[str, Any]] | None = None,
        origin: str = KnowledgeOrigin.MANUAL.value,
        document_id: str = "",
        document_version_id: str = "",
        triggered_by: str = "cli",
        reviewer: str = "",
        approval_note: str = "",
        supersedes_id: str = "",
        identity_key: str = "",
        created_by: str = "system",
        attributes: Mapping[str, Any] | None = None,
    ) -> tuple[Calculation, bool]:
        """Store an engineering number with the method that produced it - and compute nothing.

        ``calculation`` and ``calculation_input`` have been in the schema since 0001 and had a read path and
        no write path, so the honest answer to "what backs this number" was a table nobody could fill.  This
        is persistence only, exactly as the phase boundary requires: the arithmetic happened elsewhere (a
        hydraulics sheet, a torque-and-drag model, an adapter), and what is recorded here is the claim - the
        method and its version, the inputs with their units, the outputs, and the evidence the inputs came
        from.

        Four rules make the row worth trusting:

        *   ``method_id`` is required, because a number with no named method cannot be re-run, reviewed or
            superseded; an empty ``method_version`` says the version is unknown rather than implying one;
        *   a row that is not hand-entered (``origin`` EXTRACTED or DERIVED) has to cite provenance - the
            same storage invariant ``put_fact`` and every promoted operational record obey, enforced here
            rather than in a caller;
        *   identity is content-addressed, so re-recording the same claim returns the stored row
            (``created`` false) instead of appending a twin: a batch that runs twice must not double-count a
            casing check, and the database's unique index is what makes that promise hold under a race;
        *   different content is a *new* row, never an update - an engineering result stays what a decision
            was made on, and ``supersedes_id`` is how the newer one points back, marking the older one
            ``SUPERSEDED`` rather than deleting it.

        ``inputs`` are also indexed one row per entry into ``calculation_input``, which is what turns
        "which calculations used this document field?" into :meth:`calculations_using` instead of a scan.
        """
        method = str(method_id or "").strip()
        if not method:
            raise ValidationError(
                "an engineering record has to name its method",
                hint="pass method_id=<what computed this> (and method_version if it has one)",
            )
        state = RecordState(str(getattr(record_state, "value", record_state)))
        provenance_list = [dict(item) for item in (provenance or [])]
        origin_value = str(getattr(origin, "value", origin))
        if origin_value != KnowledgeOrigin.MANUAL.value and not provenance_list:
            raise ValidationError(
                f"an engineering record with origin {origin_value} has to cite its evidence",
                hint="pass provenance=[...] naming the document version or knowledge item",
                method_id=method,
            )
        content = {
            "method_id": method,
            "method_version": str(method_version or ""),
            "calculation_type": _token(calculation_type, ""),
            "record_state": state.value,
            "well_id": well_id or None,
            "section_id": section_id or None,
            "project_id": project_id or None,
            "inputs": dict(inputs or {}),
            "outputs": dict(outputs) if outputs is not None else None,
            "assumptions": list(assumptions or []),
            "validation": dict(validation or {}),
            "provenance": provenance_list,
        }
        key = str(identity_key or "").strip() or "calc:" + sha256_obj(content)[:32]
        existing = self.session.scalar(
            select(Calculation).where(Calculation.identity_key == key).limit(1)
        )
        if existing is not None:
            return existing, False
        row = Calculation(
            id=new_id("calc"),
            method_id=method,
            method_version=str(method_version or ""),
            calculation_type=str(content["calculation_type"]),
            record_state=state.value,
            well_id=well_id or None,
            section_id=section_id or None,
            project_id=project_id or None,
            source_id=source_id or None,
            inputs=content["inputs"],
            outputs=content["outputs"],
            assumptions=content["assumptions"],
            validation=content["validation"],
            uncertainty=dict(uncertainty) if uncertainty is not None else None,
            confidence=float(confidence) if confidence is not None else None,
            provenance=provenance_list,
            status=str(getattr(status, "value", status)),
            revision=1,
            supersedes_id=supersedes_id or None,
            reviewer=reviewer or None,
            approval_note=approval_note or None,
            triggered_by=str(triggered_by or "cli"),
            error=error or None,
            origin=origin_value,
            created_by=str(created_by or "system"),
            identity_key=key,
            document_id=document_id or None,
            document_version_id=document_version_id or None,
            attributes=dict(attributes or {}),
        )
        self.session.add(row)
        self.session.flush()
        self._index_calculation_inputs(row)
        if supersedes_id:
            parent = self.get_calculation(supersedes_id)
            parent.status = str(CalculationStatus.SUPERSEDED)
            row.revision = int(parent.revision or 1) + 1
            self.session.flush()
        return row, True

    def _index_calculation_inputs(self, row: Calculation) -> int:
        """One ``calculation_input`` row per input, derived from the payload on every write.

        The index is rebuilt from ``inputs`` rather than edited on its own, so it cannot disagree with the
        JSON it summarises.  A bare number is still listed (it is what the record was computed from); an
        input that names a subject is the one that makes change-impact analysis possible.
        """
        self.session.execute(
            sa_delete(CalculationInput).where(CalculationInput.calculation_id == row.id)
        )
        added = 0
        for name, value in (row.inputs or {}).items():
            entry = value if isinstance(value, Mapping) else {"value": value}
            raw = entry.get("value")
            number = None
            parsed: Quantity | None = None
            if isinstance(raw, int | float) and not isinstance(raw, bool):
                number = float(raw)
            elif isinstance(raw, str):
                try:
                    parsed = Quantity.parse(raw)
                    number = float(parsed.value)
                except (UnitError, ValueError):
                    number = None
            # A unit stated only inside the value ("10.2 ppg") still counts as stated, and the registry
            # says what it means: the dimension column exists so a later query compares like with like
            # rather than trusting two callers to spell `ppg` and `MUD_WEIGHT` the same way.  A symbol the
            # registry does not know leaves the column empty - an unknown unit is not a licence to guess.
            unit_text = str(entry.get("unit") or (parsed.unit.symbol if parsed is not None else ""))
            dimension_text = str(entry.get("dimension") or "")
            if not dimension_text and unit_text:
                try:
                    dimension_text = resolve_unit(unit_text).dimension.value
                except (UnitError, ValueError, KeyError):
                    dimension_text = ""
            subject = str(entry.get("subject_key") or entry.get("source") or "").strip()
            self.session.add(
                CalculationInput(
                    id=new_id("cain"),
                    calculation_id=row.id,
                    name=str(name)[:80],
                    value=number,
                    unit=unit_text[:24],
                    dimension=dimension_text[:32],
                    source_kind=str(
                        entry.get("source_kind") or ("knowledge" if subject else "user")
                    )[:24],
                    subject_key=subject[:300] or None,
                    provenance=dict(entry.get("provenance") or {}) or None,
                )
            )
            added += 1
        self.session.flush()
        return added

    def get_calculation(self, calculation_id: str) -> Calculation:
        row = self.session.get(Calculation, str(calculation_id))
        if row is None:
            raise ValidationError("no such engineering record", calculation_id=calculation_id)
        return row

    def calculation_inputs(self, calculation_id: str) -> list[CalculationInput]:
        return list(
            self.session.execute(
                select(CalculationInput)
                .where(CalculationInput.calculation_id == str(calculation_id))
                .order_by(CalculationInput.name)
            ).scalars()
        )

    def calculations_using(self, subject_key: str) -> list[Calculation]:
        """Every record that consumed this input - the change-impact question, answered by index.

        A subject key is a document field, a knowledge item or a well attribute, named the way the input
        named it.  When that source changes, this is how a person finds the numbers that have to be
        re-run, without a scan of every payload in the workspace.
        """
        wanted = str(subject_key or "").strip()
        if not wanted:
            raise ValidationError(
                "no input subject was named", hint="pass the subject key to look up"
            )
        return list(
            self.session.execute(
                select(Calculation)
                .join(CalculationInput, CalculationInput.calculation_id == Calculation.id)
                .where(CalculationInput.subject_key == wanted)
                .order_by(Calculation.created_at.desc(), Calculation.id.desc())
            ).scalars()
        )

    # -- calculations ---------------------------------------------------------
    def calculations_for(
        self,
        *,
        well_id: str = "",
        section_id: str = "",
        project_id: str = "",
        status: str = "",
        current_only: bool = False,
        limit: int = 200,
    ) -> list[Calculation]:
        """The calculation records in scope, newest first.

        The ``calculation`` and ``calculation_input`` tables are the record an engineering number is
        stored in - method and version, inputs with units, outputs, assumptions, a validation status and
        a provenance - and nothing in this build computes them: a number gets here from an adapter or a
        tool that did the arithmetic elsewhere, never from a language model.  This method is the read
        path, so a procedure, a program or a well screen can show *which* calculation backs a number
        without the engineering layer keeping a second copy of it, and :meth:`record_calculation` is the
        write path that gets it in.

        ``current_only`` means "a record nobody has superseded", decided by the chain rather than by the
        ``status`` column, which is a statement about the row and can therefore be wrong about it.
        """
        statement = select(Calculation)
        if well_id:
            statement = statement.where(Calculation.well_id == well_id)
        if section_id:
            statement = statement.where(Calculation.section_id == section_id)
        if project_id:
            statement = statement.where(Calculation.project_id == project_id)
        if status:
            statement = statement.where(Calculation.status == str(CalculationStatus.parse(status)))
        if current_only:
            # "Not superseded" is not the same as "did not supersede": the child in a chain is the current
            # one, and it is the one that points back.  Excluding the referenced ids is what makes the
            # second revision the answer, and it needs no extra column that could drift out of step.
            superseded = select(Calculation.supersedes_id).where(
                Calculation.supersedes_id.is_not(None)
            )
            statement = statement.where(Calculation.id.not_in(superseded))
        return list(
            self.session.execute(
                statement.order_by(Calculation.created_at.desc(), Calculation.id.desc()).limit(
                    _bounded(limit)
                )
            ).scalars()
        )

    # -- the operational records this layer has to talk to -------------------
    def set_operation_status(
        self,
        operation_id: str,
        new_status: ConfirmationStatus | str,
        *,
        by: str = "",
        reason: str = "",
    ) -> str:
        """Confirm or reject an operation row, on the same machine the operational layer uses."""
        row = self.session.get(WellOperation, str(operation_id))
        if row is None:
            raise ValidationError(f"no operation {operation_id!r}")
        return set_record_status(self.session, row, new_status, by=by, reason=reason)

    def link_procedure_to_report(
        self, procedure_id: str, report_id: str, *, note: str = ""
    ) -> None:
        """Say that the day's work was done under this procedure.

        The edge is the only honest place for it: a report row that carried a procedure id would be one
        more claim about intent that nobody wrote down, and a procedure that listed its own reports
        would drift the moment a report was re-promoted from its source file.
        """
        row = self.get_procedure(procedure_id)
        report = self.session.get(DdrReport, str(report_id))
        if report is None:
            raise ValidationError(f"no report {report_id!r}")
        create_knowledge_relation(
            self.session,
            source_type="procedure",
            source_id=row.id,
            relation=KnowledgeRelationType.PROCEDURE_REQUIRES_KNOWLEDGE.value,
            target_type="ddr_report",
            target_id=report.id,
            note=note or "procedure applied on this report",
        )
